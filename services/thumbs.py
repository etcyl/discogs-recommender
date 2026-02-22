import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
THUMBS_FILE = DATA_DIR / "thumbs.json"
DISLIKES_FILE = DATA_DIR / "dislikes.json"
HISTORY_FILE = DATA_DIR / "history.json"

MAX_FIELD_LENGTH = 500
MAX_LIST_ITEMS = 20
MAX_THUMBS_ENTRIES = 500
MAX_DISLIKES_ENTRIES = 500
MAX_HISTORY_ENTRIES = 2000


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


def load_thumbs() -> list[dict]:
    """Load all thumbed-up songs from disk."""
    if not THUMBS_FILE.exists():
        return []
    try:
        raw = THUMBS_FILE.read_text(encoding="utf-8")
        if len(raw) > 5 * 1024 * 1024:  # 5 MB limit (CWE-400)
            return []
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        return data
    except (json.JSONDecodeError, IOError, OSError):
        return []


def save_thumb(artist: str, title: str, album: str = "",
               genres: list[str] = None, styles: list[str] = None) -> dict:
    """Append a thumbed-up song and return the entry."""
    # Sanitize all inputs (CWE-20)
    artist = _sanitize_string(artist)
    title = _sanitize_string(title)
    album = _sanitize_string(album)
    genres = _sanitize_string_list(genres or [])
    styles = _sanitize_string_list(styles or [])

    if not artist or not title:
        raise ValueError("artist and title are required")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    thumbs = load_thumbs()

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
        "timestamp": datetime.now().isoformat(),
    }
    thumbs.append(entry)

    # Atomic write to prevent corruption (CWE-367)
    _atomic_write_json(THUMBS_FILE, thumbs)

    return entry


def get_thumbs_summary(max_entries: int = 50) -> str:
    """Format thumbs history for Claude prompt."""
    max_entries = min(max(1, max_entries), MAX_THUMBS_ENTRIES)
    thumbs = load_thumbs()
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


def load_dislikes() -> list[dict]:
    """Load all disliked songs from disk."""
    return _load_json_file(DISLIKES_FILE)


def save_dislike(artist: str, title: str, album: str = "",
                 genres: list[str] = None, styles: list[str] = None) -> dict:
    """Save a disliked song and return the entry."""
    artist = _sanitize_string(artist)
    title = _sanitize_string(title)
    album = _sanitize_string(album)
    genres = _sanitize_string_list(genres or [])
    styles = _sanitize_string_list(styles or [])

    if not artist or not title:
        raise ValueError("artist and title are required")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dislikes = load_dislikes()

    if len(dislikes) >= MAX_DISLIKES_ENTRIES:
        dislikes = dislikes[-(MAX_DISLIKES_ENTRIES - 1):]

    for d in dislikes:
        if (d.get("artist", "").lower() == artist.lower()
                and d.get("title", "").lower() == title.lower()):
            return d

    entry = {
        "artist": artist,
        "title": title,
        "album": album,
        "genres": genres,
        "styles": styles,
        "timestamp": datetime.now().isoformat(),
    }
    dislikes.append(entry)
    _atomic_write_json(DISLIKES_FILE, dislikes)
    return entry


def get_dislikes_summary(max_entries: int = 30) -> str:
    """Format dislikes history for Claude prompt."""
    max_entries = min(max(1, max_entries), MAX_DISLIKES_ENTRIES)
    dislikes = load_dislikes()
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

def load_history() -> list[dict]:
    """Load play history from disk."""
    return _load_json_file(HISTORY_FILE)


def save_play(artist: str, title: str, album: str = "",
              genres: list[str] = None, styles: list[str] = None) -> dict:
    """Record a played song in history."""
    artist = _sanitize_string(artist)
    title = _sanitize_string(title)
    album = _sanitize_string(album)
    genres = _sanitize_string_list(genres or [])
    styles = _sanitize_string_list(styles or [])

    if not artist or not title:
        raise ValueError("artist and title are required")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history()

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
    _atomic_write_json(HISTORY_FILE, history)
    return entry


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
