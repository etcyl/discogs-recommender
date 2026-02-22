import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CHANNELS_FILE = DATA_DIR / "channels.json"

MAX_CHANNELS = 20
MAX_NAME_LENGTH = 100

VALID_SOURCE_TYPES = {"discogs", "spotify"}
VALID_MODES = {"play_playlist", "similar_songs", "new_discoveries", "themed"}

DEFAULT_CHANNEL = {
    "id": "my-collection",
    "name": "My Collection",
    "source_type": "discogs",
    "source_data": {},
    "mode": "similar_songs",
    "discovery": 30,
    "created_at": "2026-01-01T00:00:00",
    "is_default": True,
}


def _sanitize_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    cleaned = "".join(c for c in name if c.isprintable())
    return cleaned.strip()[:MAX_NAME_LENGTH]


def load_channels() -> list[dict]:
    """Load channels from disk. Ensures default channel always exists."""
    channels = _load_json_file(CHANNELS_FILE)
    dirty = False
    if not any(c.get("id") == "my-collection" for c in channels):
        channels.insert(0, DEFAULT_CHANNEL.copy())
        dirty = True
    # Migrate: add discovery field if missing
    for ch in channels:
        if "discovery" not in ch:
            ch["discovery"] = 30
            dirty = True
    if dirty:
        _atomic_write_json(CHANNELS_FILE, channels)
    return channels


def get_channel(channel_id: str) -> Optional[dict]:
    channels = load_channels()
    for ch in channels:
        if ch.get("id") == channel_id:
            return ch
    return None


def create_channel(name: str, source_type: str, source_data: dict,
                   mode: str, discovery: int = 30) -> dict:
    name = _sanitize_name(name)
    if not name:
        raise ValueError("Channel name is required")
    if source_type not in VALID_SOURCE_TYPES:
        raise ValueError(f"Invalid source_type: {source_type}")
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode}")
    discovery = max(0, min(100, int(discovery)))

    channels = load_channels()
    if len(channels) >= MAX_CHANNELS:
        raise ValueError(f"Maximum of {MAX_CHANNELS} channels reached")

    channel = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "source_type": source_type,
        "source_data": source_data,
        "mode": mode,
        "discovery": discovery,
        "created_at": datetime.now().isoformat(),
        "is_default": False,
    }
    channels.append(channel)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(CHANNELS_FILE, channels)
    return channel


def rename_channel(channel_id: str, new_name: str) -> dict:
    new_name = _sanitize_name(new_name)
    if not new_name:
        raise ValueError("Channel name is required")

    channels = load_channels()
    for ch in channels:
        if ch["id"] == channel_id:
            ch["name"] = new_name
            _atomic_write_json(CHANNELS_FILE, channels)
            return ch
    raise ValueError(f"Channel not found: {channel_id}")


def update_channel_discovery(channel_id: str, discovery: int) -> dict:
    discovery = max(0, min(100, int(discovery)))
    channels = load_channels()
    for ch in channels:
        if ch["id"] == channel_id:
            ch["discovery"] = discovery
            _atomic_write_json(CHANNELS_FILE, channels)
            return ch
    raise ValueError(f"Channel not found: {channel_id}")


def delete_channel(channel_id: str) -> bool:
    channels = load_channels()
    for ch in channels:
        if ch["id"] == channel_id:
            if ch.get("is_default"):
                raise ValueError("Cannot delete the default channel")
            channels.remove(ch)
            _atomic_write_json(CHANNELS_FILE, channels)
            return True
    raise ValueError(f"Channel not found: {channel_id}")


def _load_json_file(filepath: Path) -> list[dict]:
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


def _atomic_write_json(filepath: Path, data) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(filepath.parent), suffix=".tmp", prefix=".channels_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=True)
        if filepath.exists():
            filepath.unlink()
        os.rename(tmp_path, str(filepath))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
