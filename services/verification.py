"""Catalogue-backed fact checking for AI-generated recommendations.

A language model asked for music recommendations will confidently return songs
that do not exist. Measured on this app's own prompts, the share of picks that
resolve to a real recording ranged from 44% to 100% depending on the model
(see bench/verification.md). The failures are not obvious noise — they are
"real artist, wrong song" (`Portishead — Silent Shout` is a The Knife track),
"real pairing, wrong record", and near-miss artist names (`Galaxie 2000`).
All three read as authoritative in the UI.

This module resolves each recommendation against public music catalogues and
reports what it found, so the app can flag or drop the ones nothing backs up.

Design notes:

* **No API keys.** Deezer, iTunes Search and MusicBrainz are all open. The
  first two are also already used elsewhere in this app for album art, so in
  the common path this adds no new upstream dependency.
* **Absence of evidence is reported as such.** A miss is `UNVERIFIED`, never
  "fake" — a genuinely obscure private press may be in no catalogue. Callers
  decide what to do about that, and the default is to disclose rather than
  delete.
* **A near match is a correction, not a pass.** If the catalogue has
  `Tujiko Noriko` where the model said `Tujiko Norne`, that is worth surfacing
  rather than silently accepting.
"""
from __future__ import annotations

import difflib
import logging
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from enum import Enum

import httpx

from services.cache import cache

logger = logging.getLogger(__name__)

CACHE_TTL = 7 * 24 * 3600   # catalogue membership barely changes
REQUEST_TIMEOUT = 8.0
MAX_WORKERS = 6


class Status(str, Enum):
    VERIFIED = "verified"        # catalogue has it, as stated
    CORRECTED = "corrected"      # catalogue has it under a slightly different name
    UNVERIFIED = "unverified"    # no catalogue had it
    SKIPPED = "skipped"          # checking was disabled or the input was unusable


class Policy(str, Enum):
    OFF = "off"        # don't check at all
    FLAG = "flag"      # check and annotate, show everything (default)
    STRICT = "strict"  # check and drop anything unverified


VALID_POLICIES = {p.value for p in Policy}


@dataclass
class Result:
    status: Status = Status.SKIPPED
    source: str = ""            # deezer | itunes | musicbrainz
    confidence: float = 0.0     # 0..1, how close the catalogue match was
    matched_artist: str = ""
    matched_title: str = ""
    checked_sources: list[str] = field(default_factory=list)
    # What the model actually claimed, captured before anything downstream
    # rewrites it. The YouTube resolver overwrites artist/title from the video
    # title, so without this the badge could end up describing a different
    # string than the one it checked.
    claimed_as: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

# Suffixes that describe a *version* rather than a different song. Stripping
# them keeps "Song (2011 Remaster)" matching "Song".
_VERSION_NOISE = re.compile(
    r"\b(remaster(ed)?|remix|mono|stereo|version|edit|mix|live|demo|"
    r"radio|single|album|deluxe|bonus|reissue|instrumental|explicit|clean)\b"
)
_FEAT = re.compile(r"\((?:feat|ft|featuring|with)[^)]*\)|\bfeat\.?\s.*$", re.IGNORECASE)


def normalize(text: str) -> str:
    """Fold to a comparable form: no accents, no punctuation, no version noise."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = _FEAT.sub(" ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = _VERSION_NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    """0..1 similarity between two names, after normalisation."""
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Containment counts as a strong match: catalogues append things like
    # "- 2011 Remaster" that normalisation doesn't always reach.
    if a in b or b in a:
        return 0.95
    return difflib.SequenceMatcher(None, a, b).ratio()


# An artist has to match harder than a title: getting the artist wrong is the
# failure mode that matters ("Portishead — Silent Shout"), whereas titles vary
# legitimately across pressings.
ARTIST_FLOOR = 0.80
TITLE_FLOOR = 0.82
EXACT_FLOOR = 0.97


def _score(artist, title, cand_artist, cand_title) -> tuple[float, bool] | None:
    """Return (confidence, is_exact) if the candidate plausibly is this track."""
    a = similarity(artist, cand_artist)
    t = similarity(title, cand_title)
    if a < ARTIST_FLOOR or t < TITLE_FLOOR:
        return None
    return round((a + t) / 2, 3), (a >= EXACT_FLOOR and t >= EXACT_FLOOR)


# ---------------------------------------------------------------------------
# Catalogue lookups
#
# Each returns (matched_artist, matched_title, confidence, is_exact) or None.
# Any network or shape failure returns None so one flaky catalogue can never
# turn a real track into a hallucination — it just falls through to the next.
# ---------------------------------------------------------------------------

def _deezer(client: httpx.Client, artist: str, title: str):
    r = client.get("https://api.deezer.com/search",
                   params={"q": f'artist:"{artist}" track:"{title}"', "limit": 5})
    if r.status_code != 200:
        return None
    for hit in (r.json().get("data") or []):
        scored = _score(artist, title,
                        (hit.get("artist") or {}).get("name", ""), hit.get("title", ""))
        if scored:
            return ((hit.get("artist") or {}).get("name", ""), hit.get("title", ""),
                    scored[0], scored[1])
    return None


def _itunes(client: httpx.Client, artist: str, title: str):
    r = client.get("https://itunes.apple.com/search",
                   params={"term": f"{artist} {title}", "entity": "song", "limit": 8})
    if r.status_code != 200:
        return None
    for hit in (r.json().get("results") or []):
        scored = _score(artist, title, hit.get("artistName", ""), hit.get("trackName", ""))
        if scored:
            return (hit.get("artistName", ""), hit.get("trackName", ""),
                    scored[0], scored[1])
    return None


# MusicBrainz asks for at most one request per second from a given client.
# It is the last resort precisely because of that, and because its coverage of
# obscure and non-commercial releases is the best of the three — which is what
# makes a miss here meaningful.
_MB_LOCK = threading.Lock()
_MB_LAST = [0.0]
MB_MIN_INTERVAL = 1.1
MB_USER_AGENT = ("discogs-recommender/1.0 "
                 "(+https://github.com/etcyl/discogs-recommender)")


def _musicbrainz(client: httpx.Client, artist: str, title: str):
    with _MB_LOCK:
        wait = MB_MIN_INTERVAL - (time.monotonic() - _MB_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _MB_LAST[0] = time.monotonic()

    # Escape Lucene syntax so a title like `Song [Mix]` can't break the query.
    def esc(s: str) -> str:
        return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s)

    r = client.get("https://musicbrainz.org/ws/2/recording",
                   params={"query": f'artist:"{esc(artist)}" AND recording:"{esc(title)}"',
                           "limit": 5, "fmt": "json"},
                   headers={"User-Agent": MB_USER_AGENT})
    if r.status_code != 200:
        return None
    for rec in (r.json().get("recordings") or []):
        credits = rec.get("artist-credit") or []
        cand_artist = credits[0].get("name", "") if credits else ""
        scored = _score(artist, title, cand_artist, rec.get("title", ""))
        if scored:
            return (cand_artist, rec.get("title", ""), scored[0], scored[1])
    return None


# Order matters: cheapest and broadest first, MusicBrainz last because it is
# rate-limited to one request a second. Referenced by name rather than by
# function object so the set stays swappable at runtime — binding the objects
# here would freeze them at import time.
CATALOGUE_ORDER = ("deezer", "itunes", "musicbrainz")

_CATALOGUE_FNS = {
    "deezer": "_deezer",
    "itunes": "_itunes",
    "musicbrainz": "_musicbrainz",
}


def _catalogues():
    """Yield (name, fn) for each catalogue, resolved at call time."""
    module = globals()
    for name in CATALOGUE_ORDER:
        fn = module.get(_CATALOGUE_FNS[name])
        if fn is not None:
            yield name, fn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_song(artist: str, title: str, client: httpx.Client | None = None) -> Result:
    """Resolve one track against the catalogues. Cached for a week."""
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist or not title:
        return Result(status=Status.SKIPPED)

    cache_key = f"verify:{normalize(artist)}|{normalize(title)}"[:250]
    cached = cache.get(cache_key)
    if cached is not None:
        return Result(status=Status(cached["status"]), source=cached["source"],
                      confidence=cached["confidence"],
                      matched_artist=cached["matched_artist"],
                      matched_title=cached["matched_title"],
                      checked_sources=cached["checked_sources"])

    owns_client = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True)
    checked: list[str] = []
    result = Result(status=Status.UNVERIFIED, checked_sources=checked)
    try:
        for name, fn in _catalogues():
            try:
                hit = fn(client, artist, title)
            except Exception as e:
                logger.debug("verification: %s lookup failed for %r/%r: %s",
                             name, artist, title, e)
                continue
            checked.append(name)
            if hit:
                m_artist, m_title, confidence, exact = hit
                result = Result(
                    status=Status.VERIFIED if exact else Status.CORRECTED,
                    source=name, confidence=confidence,
                    matched_artist=m_artist, matched_title=m_title,
                    checked_sources=list(checked),
                )
                break
    finally:
        if owns_client:
            client.close()

    cache.set(cache_key, result.to_dict(), ttl=CACHE_TTL)
    return result


def reconcile(songs: list[dict]) -> int:
    """Restore catalogue identity where YouTube resolution overwrote it.

    The YouTube resolver treats the video title as the source of truth and
    rewrites artist/title from it. That corrects model typos, but when it
    matches the wrong video it silently turns a good recommendation into a
    wrong one — a real example being a recommendation rewritten to
    `Behind the Song: "Vine Street" by Randy Newman — featuring Van Dyke Parks`,
    which is a podcast episode, not a song.

    Where a catalogue independently confirmed the track, its name for it beats
    a YouTube video title. The original video title is kept on the song as
    `ytTitle` and the substitution is recorded, so nothing is hidden.

    Returns the number of songs reconciled.
    """
    fixed = 0
    for song in songs:
        v = song.get("verification") or {}
        if v.get("status") not in (Status.VERIFIED.value, Status.CORRECTED.value):
            continue
        m_artist, m_title = v.get("matched_artist", ""), v.get("matched_title", "")
        if not m_artist or not m_title:
            continue

        current = f'{song.get("artist", "")} {song.get("title", "")}'
        catalogue = f"{m_artist} {m_title}"
        if similarity(current, catalogue) >= 0.7:
            continue  # the rewrite is close enough — leave it

        logger.info("Reconciled a YouTube rewrite: %r -> %r", current, catalogue)
        song["displayCorrectedFrom"] = f'{song.get("artist", "")} — {song.get("title", "")}'
        song["artist"] = m_artist
        song["title"] = m_title
        fixed += 1
    return fixed


def verify_songs(songs: list[dict], policy: str = Policy.FLAG.value,
                 max_workers: int = MAX_WORKERS) -> tuple[list[dict], dict]:
    """Annotate songs with a `verification` block, honouring the policy.

    Returns (songs, summary). Under STRICT, unverified songs are removed from
    the returned list — but they are still counted in the summary, and the
    caller is expected to log that, so a dropped recommendation is never
    invisible.

    Annotation never raises: if every catalogue is unreachable the songs come
    back marked SKIPPED and nothing is dropped. Failing closed here would mean
    an internet hiccup silently empties a playlist.
    """
    summary = {"policy": policy, "checked": 0, "verified": 0, "corrected": 0,
               "unverified": 0, "skipped": 0, "dropped": 0}

    if policy == Policy.OFF.value or not songs:
        for s in songs:
            s["verification"] = Result(status=Status.SKIPPED).to_dict()
        summary["skipped"] = len(songs)
        return songs, summary

    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        def _check(song: dict) -> dict:
            try:
                res = verify_song(song.get("artist", ""), song.get("title", ""), client)
            except Exception as e:
                logger.warning("verification failed for %r: %s", song.get("title"), e)
                res = Result(status=Status.SKIPPED)
            res.claimed_as = f'{song.get("artist", "")} — {song.get("title", "")}'
            song["verification"] = res.to_dict()
            return song

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            songs = list(pool.map(_check, songs))

    for s in songs:
        summary["checked"] += 1
        summary[s["verification"]["status"]] += 1

    if policy == Policy.STRICT.value:
        kept = [s for s in songs
                if s["verification"]["status"] != Status.UNVERIFIED.value]
        summary["dropped"] = len(songs) - len(kept)
        if summary["dropped"]:
            logger.info(
                "verification[strict]: dropped %d of %d unverified recommendations: %s",
                summary["dropped"], len(songs),
                [f'{s.get("artist")} - {s.get("title")}' for s in songs
                 if s["verification"]["status"] == Status.UNVERIFIED.value][:10])
        songs = kept

    logger.info("verification[%s]: %d checked — %d verified, %d corrected, "
                "%d unverified, %d dropped",
                policy, summary["checked"], summary["verified"],
                summary["corrected"], summary["unverified"], summary["dropped"])
    return songs, summary
