import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
THUMBS_FILE = DATA_DIR / "thumbs.json"
DISLIKES_FILE = DATA_DIR / "dislikes.json"
HISTORY_FILE = DATA_DIR / "history.json"
REC_HISTORY_FILE = DATA_DIR / "rec_history.json"

MAX_FIELD_LENGTH = 500
MAX_LIST_ITEMS = 20
MAX_THUMBS_ENTRIES = 500
MAX_DISLIKES_ENTRIES = 500
MAX_HISTORY_ENTRIES = 2000
MAX_REC_HISTORY_ENTRIES = 1000


def _resolve_paths(data_dir: Path | None = None):
    """Return (data_dir, thumbs, dislikes, history, rec_history) paths."""
    d = data_dir or DATA_DIR
    return (d, d / "thumbs.json", d / "dislikes.json",
            d / "history.json", d / "rec_history.json")


def _sanitize_string(value: str, max_length: int = MAX_FIELD_LENGTH) -> str:
    """Sanitize a string field: strip, truncate, remove control characters."""
    if not isinstance(value, str):
        return ""
    # Remove null bytes and control characters (CWE-20, CWE-138)
    cleaned = "".join(c for c in value if c.isprintable() or c in ("\n", "\t"))
    return cleaned.strip()[:max_length]


def _sanitize_string_list(items: list, max_items: int = MAX_LIST_ITEMS) -> list[str]:
    """Sanitize a list of strings."""
    if not isinstance(items, list):
        return []
    return [_sanitize_string(item) for item in items[:max_items] if isinstance(item, str)]


# Regex to strip parenthetical/bracket suffixes (remastered, deluxe, feat, etc.)
_PAREN_STRIP = re.compile(
    r'[\(\[](remaster(ed)?|deluxe(\s+edition)?|bonus\s+track'
    r'|expanded(\s+edition)?|anniversary(\s+edition)?'
    r'|single(\s+version)?|mono|stereo|demo|remix'
    r'|feat\.?\s*[^)\]]*|ft\.?\s*[^)\]]*'
    r'|\d{4}\s*(remaster(ed)?|mix|version|edition)?'
    r'|official\s*(music\s*)?video|official\s*audio'
    r'|lyric\s*video|lyrics?|audio|visuali[sz]er'
    r')[^)\]]*[\)\]]',
    re.IGNORECASE,
)

# Trailing dash/colon suffixes like " - Remastered 2011", " - 2016 Remaster"
_TRAIL_STRIP = re.compile(
    r'\s*[-:]\s*(remaster(ed)?(\s*\d{4})?|\d{4}\s*remaster(ed)?'
    r'|single\s*version'
    r'|deluxe(\s*edition)?|bonus\s*track|mono(\s*mix)?'
    r'|stereo(\s*mix)?|demo(\s*version)?|remix'
    r'|original(\s*mix)?|radio\s*edit|album\s*version'
    r'|extended(\s*(mix|version))?)\s*$',
    re.IGNORECASE,
)

# Non-alphanumeric characters (for aggressive normalization)
_NON_ALNUM = re.compile(r'[^a-z0-9\s]')
_MULTI_SPACE = re.compile(r'\s+')


def _normalize_for_match(text: str) -> str:
    """Aggressively normalize a song title or artist name for fuzzy matching.

    Strips parenthetical suffixes, trailing qualifiers, punctuation,
    and leading 'the ' from artist names.
    """
    if not text:
        return ""
    t = text.lower().strip()
    # Strip parenthetical/bracket content (remastered, feat., etc.)
    t = _PAREN_STRIP.sub('', t)
    # Strip trailing " - Remastered 2011" style suffixes
    t = _TRAIL_STRIP.sub('', t)
    # Remove non-alphanumeric (punctuation, accents approx)
    t = _NON_ALNUM.sub(' ', t)
    # Collapse whitespace
    t = _MULTI_SPACE.sub(' ', t).strip()
    return t


def _normalize_artist(artist: str) -> str:
    """Normalize artist: strip 'the ' prefix and punctuation."""
    n = _normalize_for_match(artist)
    if n.startswith("the "):
        n = n[4:]
    return n


def normalize_song_key(artist: str, title: str) -> tuple[str, str]:
    """Return a normalized (artist, title) tuple for fuzzy matching."""
    return (_normalize_artist(artist), _normalize_for_match(title))


def load_thumbs(data_dir: Path | None = None) -> list[dict]:
    """Load all thumbed-up songs from disk."""
    _, thumbs_file, _, _, _ = _resolve_paths(data_dir)
    if not thumbs_file.exists():
        return []
    try:
        raw = thumbs_file.read_text(encoding="utf-8")
        if len(raw) > 5 * 1024 * 1024:  # 5 MB limit (CWE-400)
            return []
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        return data
    except (json.JSONDecodeError, IOError, OSError):
        return []


def save_thumb(artist: str, title: str, album: str = "",
               genres: list[str] = None, styles: list[str] = None,
               match_attributes: list[str] = None,
               match_score: int | None = None,
               data_dir: Path | None = None) -> dict:
    """Append a thumbed-up song and return the entry."""
    # Sanitize all inputs (CWE-20)
    artist = _sanitize_string(artist)
    title = _sanitize_string(title)
    album = _sanitize_string(album)
    genres = _sanitize_string_list(genres or [])
    styles = _sanitize_string_list(styles or [])
    match_attributes = _sanitize_string_list(match_attributes or [])

    if not artist or not title:
        raise ValueError("artist and title are required")

    d, thumbs_file, _, _, _ = _resolve_paths(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    thumbs = load_thumbs(data_dir)

    # Enforce max entries (CWE-400, CWE-770)
    if len(thumbs) >= MAX_THUMBS_ENTRIES:
        thumbs = thumbs[-(MAX_THUMBS_ENTRIES - 1):]  # keep newest, make room for 1

    # Don't duplicate
    for t in thumbs:
        if (t.get("artist", "").lower() == artist.lower()
                and t.get("title", "").lower() == title.lower()):
            return t

    entry = {
        "artist": artist,
        "title": title,
        "album": album,
        "genres": genres,
        "styles": styles,
        "match_attributes": match_attributes,
        "match_score": min(100, max(0, int(match_score))) if match_score is not None else None,
        "timestamp": datetime.now().isoformat(),
    }
    thumbs.append(entry)

    # Atomic write to prevent corruption (CWE-367)
    _atomic_write_json(thumbs_file, thumbs)

    return entry


def get_thumbs_summary(max_entries: int = 50,
                       data_dir: Path | None = None) -> str:
    """Format thumbs history for Claude prompt."""
    max_entries = min(max(1, max_entries), MAX_THUMBS_ENTRIES)
    thumbs = load_thumbs(data_dir)
    if not thumbs:
        return "No liked songs yet."
    recent = thumbs[-max_entries:]
    lines = []
    for t in recent:
        genre_part = ""
        if t.get("genres"):
            genre_part = f" ({', '.join(t['genres'][:2])})"
        lines.append(f"  - {t.get('artist', '?')} - {t.get('title', '?')}{genre_part}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dislikes
# ---------------------------------------------------------------------------

def _load_json_file(filepath: Path) -> list[dict]:
    """Generic loader for JSON list files."""
    if not filepath.exists():
        return []
    try:
        raw = filepath.read_text(encoding="utf-8")
        if len(raw) > 5 * 1024 * 1024:
            return []
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        return data
    except (json.JSONDecodeError, IOError, OSError):
        return []


def load_dislikes(data_dir: Path | None = None) -> list[dict]:
    """Load all disliked songs from disk."""
    _, _, dislikes_file, _, _ = _resolve_paths(data_dir)
    return _load_json_file(dislikes_file)


def save_dislike(artist: str, title: str, album: str = "",
                 genres: list[str] = None, styles: list[str] = None,
                 match_attributes: list[str] = None,
                 match_score: int | None = None,
                 data_dir: Path | None = None) -> dict:
    """Save a disliked song and return the entry."""
    artist = _sanitize_string(artist)
    title = _sanitize_string(title)
    album = _sanitize_string(album)
    genres = _sanitize_string_list(genres or [])
    styles = _sanitize_string_list(styles or [])
    match_attributes = _sanitize_string_list(match_attributes or [])

    if not artist or not title:
        raise ValueError("artist and title are required")

    d, _, dislikes_file, _, _ = _resolve_paths(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    dislikes = load_dislikes(data_dir)

    if len(dislikes) >= MAX_DISLIKES_ENTRIES:
        dislikes = dislikes[-(MAX_DISLIKES_ENTRIES - 1):]

    for dl in dislikes:
        if (dl.get("artist", "").lower() == artist.lower()
                and dl.get("title", "").lower() == title.lower()):
            return dl

    entry = {
        "artist": artist,
        "title": title,
        "album": album,
        "genres": genres,
        "styles": styles,
        "match_attributes": match_attributes,
        "match_score": min(100, max(0, int(match_score))) if match_score is not None else None,
        "timestamp": datetime.now().isoformat(),
    }
    dislikes.append(entry)
    _atomic_write_json(dislikes_file, dislikes)
    return entry


def get_dislikes_summary(max_entries: int = 30,
                         data_dir: Path | None = None) -> str:
    """Format dislikes history for Claude prompt."""
    max_entries = min(max(1, max_entries), MAX_DISLIKES_ENTRIES)
    dislikes = load_dislikes(data_dir)
    if not dislikes:
        return ""
    recent = dislikes[-max_entries:]
    lines = []
    for d in recent:
        lines.append(f"  - {d.get('artist', '?')} - {d.get('title', '?')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Play History
# ---------------------------------------------------------------------------

def load_history(data_dir: Path | None = None) -> list[dict]:
    """Load play history from disk."""
    _, _, _, history_file, _ = _resolve_paths(data_dir)
    return _load_json_file(history_file)


def save_play(artist: str, title: str, album: str = "",
              genres: list[str] = None, styles: list[str] = None,
              data_dir: Path | None = None) -> dict:
    """Record a played song in history."""
    artist = _sanitize_string(artist)
    title = _sanitize_string(title)
    album = _sanitize_string(album)
    genres = _sanitize_string_list(genres or [])
    styles = _sanitize_string_list(styles or [])

    if not artist or not title:
        raise ValueError("artist and title are required")

    d, _, _, history_file, _ = _resolve_paths(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    history = load_history(data_dir)

    if len(history) >= MAX_HISTORY_ENTRIES:
        history = history[-(MAX_HISTORY_ENTRIES - 1):]

    entry = {
        "artist": artist,
        "title": title,
        "album": album,
        "genres": genres,
        "styles": styles,
        "played_at": datetime.now().isoformat(),
    }
    history.append(entry)
    _atomic_write_json(history_file, history)
    return entry


def get_play_history_summary(max_entries: int = 100,
                             recent_days: int = 30,
                             data_dir: Path | None = None) -> str:
    """Format recent play history for Claude prompt, deduped by artist+title."""
    max_entries = min(max(1, max_entries), MAX_HISTORY_ENTRIES)
    history = load_history(data_dir)
    if not history:
        return ""

    cutoff = datetime.now() - timedelta(days=recent_days)
    recent = []
    for h in reversed(history):  # newest first
        try:
            played_at = datetime.fromisoformat(h.get("played_at", ""))
            if played_at < cutoff:
                break
        except (ValueError, TypeError):
            continue
        recent.append(h)

    if not recent:
        return ""

    seen = set()
    lines = []
    for h in recent:
        key = f"{h.get('artist', '').lower()}|{h.get('title', '').lower()}"
        if key not in seen:
            seen.add(key)
            lines.append(
                f"  - {h.get('artist', '?')} - {h.get('title', '?')} [{h.get('album', '')}]")
        if len(lines) >= max_entries:
            break

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Recommendation History
# ---------------------------------------------------------------------------

def load_rec_history(data_dir: Path | None = None) -> list[dict]:
    """Load recommendation history from disk."""
    _, _, _, _, rec_history_file = _resolve_paths(data_dir)
    return _load_json_file(rec_history_file)


def save_recommendations(items: list[dict], source: str = "genre",
                         data_dir: Path | None = None) -> None:
    """Record a batch of recommendations that were shown to the user."""
    if not items:
        return

    d, _, _, _, rec_history_file = _resolve_paths(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    rec_history = load_rec_history(data_dir)

    now = datetime.now().isoformat()
    for item in items:
        artist = _sanitize_string(item.get("artist", ""))
        title = _sanitize_string(item.get("title", "") or item.get("album", ""))
        album = _sanitize_string(item.get("album", ""))
        if not artist:
            continue
        rec_history.append({
            "artist": artist,
            "title": title,
            "album": album,
            "source": source,
            "recommended_at": now,
        })

    if len(rec_history) > MAX_REC_HISTORY_ENTRIES:
        rec_history = rec_history[-MAX_REC_HISTORY_ENTRIES:]

    _atomic_write_json(rec_history_file, rec_history)


def get_recently_recommended_artists(days: int = 14,
                                     data_dir: Path | None = None) -> set[str]:
    """Return set of lowercased artist names recommended in the last N days."""
    rec_history = load_rec_history(data_dir)
    if not rec_history:
        return set()

    cutoff = datetime.now() - timedelta(days=days)
    artists = set()
    for r in reversed(rec_history):
        try:
            rec_at = datetime.fromisoformat(r.get("recommended_at", ""))
            if rec_at < cutoff:
                break
        except (ValueError, TypeError):
            continue
        artist = r.get("artist", "").lower().strip()
        if artist:
            artists.add(artist)
    return artists


def get_rec_history_summary(max_entries: int = 200,
                            data_dir: Path | None = None) -> str:
    """Format recent recommendation history for Claude prompt (artist - title)."""
    max_entries = min(max(1, max_entries), MAX_REC_HISTORY_ENTRIES)
    rec_history = load_rec_history(data_dir)
    if not rec_history:
        return ""

    seen = set()
    lines = []
    for r in reversed(rec_history):
        key = f"{r.get('artist', '').lower()}|{r.get('title', '').lower()}"
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"  - {r.get('artist', '?')} - {r.get('title', r.get('album', '?'))}")
        if len(lines) >= max_entries:
            break

    return "\n".join(lines)


def get_rec_history_set(max_entries: int = 200,
                        data_dir: Path | None = None) -> set[tuple[str, str]]:
    """Return set of (artist_lower, title_lower) tuples from recent recommendations.

    Includes both exact and normalized keys for fuzzy matching.
    """
    rec_history = load_rec_history(data_dir)
    if not rec_history:
        return set()

    result = set()
    count = 0
    for r in reversed(rec_history[-max_entries * 2:]):
        artist = r.get("artist", "").lower().strip()
        title = r.get("title", "").lower().strip()
        if artist and title:
            result.add((artist, title))
            result.add(normalize_song_key(artist, title))
            count += 1
        if count >= max_entries:
            break
    return result


def get_thumbs_set(data_dir: Path | None = None) -> set[tuple[str, str]]:
    """Return set of (artist_lower, title_lower) from all liked songs.

    Includes both exact and normalized keys for fuzzy matching.
    """
    liked = load_thumbs(data_dir)
    result = set()
    for t in liked:
        artist = t.get("artist", "").lower().strip()
        title = t.get("title", "").lower().strip()
        if artist and title:
            result.add((artist, title))
            result.add(normalize_song_key(artist, title))
    return result


def get_history_set(max_entries: int = 300,
                    data_dir: Path | None = None) -> set[tuple[str, str]]:
    """Return set of (artist_lower, title_lower) from recent play history.

    Includes both exact and normalized keys for fuzzy matching.
    """
    history = load_history(data_dir)
    if not history:
        return set()
    result = set()
    count = 0
    for h in reversed(history[-max_entries * 2:]):
        artist = h.get("artist", "").lower().strip()
        title = h.get("title", "").lower().strip()
        if artist and title:
            result.add((artist, title))
            result.add(normalize_song_key(artist, title))
            count += 1
        if count >= max_entries:
            break
    return result


def get_dislikes_set(data_dir: Path | None = None) -> set[tuple[str, str]]:
    """Return set of (artist_lower, title_lower) from all disliked songs.

    Includes both exact and normalized keys for fuzzy matching.
    """
    dislikes = load_dislikes(data_dir)
    result = set()
    for d in dislikes:
        artist = d.get("artist", "").lower().strip()
        title = d.get("title", "").lower().strip()
        if artist and title:
            result.add((artist, title))
            result.add(normalize_song_key(artist, title))
    return result


def _atomic_write_json(filepath: Path, data) -> None:
    """Write JSON atomically using temp file + rename (CWE-367)."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(filepath.parent), suffix=".tmp", prefix=".thumbs_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=True)
        # On Windows, we need to remove the target first if it exists
        if filepath.exists():
            filepath.unlink()
        os.rename(tmp_path, str(filepath))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
