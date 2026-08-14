#!/usr/bin/env python3
"""Check whether recommended tracks actually exist.

Structural metrics (artist diversity, decade spread) say nothing about
whether a model invented the song. This resolves every pick in a bench run
against public music catalogues and reports a hallucination rate.

Lookup order per track:
  1. Deezer search           — artist + track, exact-ish match
  2. iTunes Search API       — fallback
  3. MusicBrainz             — fallback, authoritative but slow (1 req/sec)

A track counts as VERIFIED if some catalogue returns a result whose artist
and title both match closely enough. UNVERIFIED means no catalogue had it —
usually a hallucination, occasionally something genuinely too obscure to be
indexed, which is why the report separates "unverified" from "wrong".

  python tools/verify_runs.py                # every run in bench/runs
  python tools/verify_runs.py --match schema
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
RUNS_DIR = BASE_DIR / "bench" / "runs"
OUT = BASE_DIR / "bench" / "verification.md"
CACHE_PATH = BASE_DIR / "bench" / ".verify_cache.json"

MATCH_THRESHOLD = 0.82


def norm(s: str) -> str:
    """Fold accents, drop punctuation and parenthetical suffixes, lowercase."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\((?:feat|ft|with)[^)]*\)", " ", s)
    s = re.sub(r"\b(remaster(ed)?|mono|stereo|version|edit|mix|pt\.?|part)\b", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similar(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _hit(artist, title, cand_artist, cand_title) -> float | None:
    """Return the combined score if this candidate plausibly is the track."""
    a = similar(artist, cand_artist)
    t = similar(title, cand_title)
    if a >= 0.75 and t >= MATCH_THRESHOLD:
        return round((a + t) / 2, 3)
    return None


def check_deezer(client: httpx.Client, artist: str, title: str):
    r = client.get("https://api.deezer.com/search",
                   params={"q": f'artist:"{artist}" track:"{title}"', "limit": 5})
    if r.status_code != 200:
        return None
    for hit in (r.json().get("data") or []):
        score = _hit(artist, title,
                     hit.get("artist", {}).get("name", ""), hit.get("title", ""))
        if score:
            return {"source": "deezer", "score": score,
                    "artist": hit.get("artist", {}).get("name", ""),
                    "title": hit.get("title", "")}
    return None


def check_itunes(client: httpx.Client, artist: str, title: str):
    r = client.get("https://itunes.apple.com/search",
                   params={"term": f"{artist} {title}", "entity": "song", "limit": 8})
    if r.status_code != 200:
        return None
    for hit in (r.json().get("results") or []):
        score = _hit(artist, title, hit.get("artistName", ""), hit.get("trackName", ""))
        if score:
            return {"source": "itunes", "score": score,
                    "artist": hit.get("artistName", ""), "title": hit.get("trackName", "")}
    return None


def check_musicbrainz(client: httpx.Client, artist: str, title: str):
    # MusicBrainz asks for <=1 request/second and a descriptive User-Agent.
    time.sleep(1.1)
    query = f'artist:"{artist}" AND recording:"{title}"'
    r = client.get("https://musicbrainz.org/ws/2/recording",
                   params={"query": query, "limit": 5, "fmt": "json"},
                   headers={"User-Agent": "discogs-recommender-bench/1.0 "
                                          "(https://github.com/etcyl/discogs-recommender)"})
    if r.status_code != 200:
        return None
    for rec in (r.json().get("recordings") or []):
        credits = rec.get("artist-credit") or []
        cand_artist = credits[0].get("name", "") if credits else ""
        score = _hit(artist, title, cand_artist, rec.get("title", ""))
        if score:
            return {"source": "musicbrainz", "score": score,
                    "artist": cand_artist, "title": rec.get("title", "")}
    return None


def verify_track(client: httpx.Client, cache: dict, artist: str, title: str) -> dict:
    key = f"{norm(artist)}|{norm(title)}"
    if key in cache:
        return cache[key]

    result = {"verified": False, "source": None, "score": None,
              "matched_as": None}
    for check in (check_deezer, check_itunes, check_musicbrainz):
        try:
            hit = check(client, artist, title)
        except Exception:
            continue
        if hit:
            result = {"verified": True, "source": hit["source"], "score": hit["score"],
                      "matched_as": f'{hit["artist"]} — {hit["title"]}'}
            break

    cache[key] = result
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--match", default="")
    p.add_argument("-o", "--out", type=Path, default=OUT)
    args = p.parse_args()

    cache = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}

    runs = []
    for path in sorted(RUNS_DIR.glob("*.json")):
        if args.match and args.match not in path.stem:
            continue
        try:
            runs.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except json.JSONDecodeError:
            continue

    if not runs:
        print("no runs found")
        return 1

    lines = ["# Do the recommended songs actually exist?\n",
             "Every pick resolved against Deezer, then iTunes, then MusicBrainz. "
             "A track is **verified** when a catalogue returns a close artist+title "
             "match. **Unverified** means no catalogue had it — usually invented, "
             "occasionally just too obscure to be indexed.\n"]

    summary = []
    details = []

    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        for path, run in runs:
            provider = run["provider"]
            songs = run.get("songs", [])
            print(f"{provider} — {len(songs)} tracks")
            unverified = []
            verified = 0

            for s in songs:
                artist = (s.get("artist") or "").strip()
                title = (s.get("title") or "").strip()
                if not artist or not title:
                    unverified.append((artist, title, s.get("year", "?")))
                    continue
                res = verify_track(client, cache, artist, title)
                if res["verified"]:
                    verified += 1
                else:
                    unverified.append((artist, title, s.get("year", "?")))
                print(f"  {'ok ' if res['verified'] else 'MISS'} {artist} — {title}")

            total = len(songs)
            rate = round(100 * verified / total, 1) if total else 0.0
            summary.append((provider, total, verified, len(unverified), rate))

            details.append(f"\n### `{provider}` — {len(unverified)} unverified of {total}\n")
            if unverified:
                for a, t, y in unverified:
                    details.append(f"- {a} — {t} ({y})")
            else:
                details.append("_Every pick resolved to a real recording._")

            CACHE_PATH.write_text(json.dumps(cache, indent=0), encoding="utf-8")

    lines.append("## Summary\n")
    lines.append("| Provider | Tracks | Verified | Unverified | Verified % |")
    lines.append("|---|---|---|---|---|")
    for provider, total, ver, unver, rate in sorted(summary, key=lambda r: -r[4]):
        lines.append(f"| `{provider}` | {total} | {ver} | {unver} | **{rate}%** |")

    lines.append("\n## Unverified picks\n")
    lines.extend(details)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n-> {args.out}")
    for provider, total, ver, unver, rate in sorted(summary, key=lambda r: -r[4]):
        print(f"  {provider:28s} {rate:5.1f}% verified ({unver} unverified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
