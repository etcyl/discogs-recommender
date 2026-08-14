"""Seeing what everyone else in the house is listening to.

Two things are shared between accounts, and only these two:

  * **what someone is playing now** (and what they played last), which is
    presence — one row per person, overwritten as they play, and
  * **their liked songs**, which is the list they built by pressing the
    thumbs-up.

Everything else stays private. Nobody sees anyone else's play history,
dislikes, audit log, Discogs collection, or the channels they built.

Sharing is per account and can be turned off, which switches off both
directions for that person: they stop broadcasting and they stop seeing.
Watching other people without being visible yourself is the sort of asymmetry
that makes a household feature feel like surveillance.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from services import thumbs
from services.database import get_db
from services.paths import data_dir

logger = logging.getLogger(__name__)

# How recently someone must have started a track to count as listening now.
# Long enough to cover a long song, short enough that a closed laptop stops
# claiming to be playing.
LIVE_WINDOW_MINUTES = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_now_playing(user_id: str, artist: str, title: str, album: str = "",
                    video_id: str = "", channel_name: str = "") -> None:
    """Record what this user just started playing. Never raises."""
    if not user_id or not (artist or title):
        return
    try:
        conn = get_db()
    except sqlite3.Error as e:
        logger.debug("now-playing: cannot open database: %s", e)
        return
    try:
        conn.execute(
            "INSERT INTO now_playing (user_id, artist, title, album, video_id,"
            " channel_name, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET artist=excluded.artist, "
            "title=excluded.title, album=excluded.album, "
            "video_id=excluded.video_id, channel_name=excluded.channel_name, "
            "updated_at=excluded.updated_at",
            (user_id, str(artist)[:300], str(title)[:300], str(album)[:300],
             str(video_id)[:32], str(channel_name)[:100], _now()))
        conn.commit()
    except sqlite3.Error as e:
        logger.debug("now-playing write failed: %s", e)
    finally:
        conn.close()


def clear_now_playing(user_id: str) -> None:
    """Forget what someone was playing — used when they turn sharing off."""
    try:
        conn = get_db()
    except sqlite3.Error:
        return
    try:
        conn.execute("DELETE FROM now_playing WHERE user_id = ?", (user_id,))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _is_live(updated_at: str) -> bool:
    try:
        when = datetime.fromisoformat(updated_at)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when < timedelta(minutes=LIVE_WINDOW_MINUTES)


def household(viewer: dict) -> list[dict]:
    """Everyone else in the house, with what they're playing.

    Returns [] when the viewer has sharing off — the switch is symmetric.
    """
    if not viewer or not viewer.get("share_activity", 1):
        return []

    try:
        conn = get_db()
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT u.id, u.display_name, u.login_name, u.is_admin, "
            "       n.artist, n.title, n.album, n.video_id, n.channel_name, "
            "       n.updated_at "
            "FROM users u LEFT JOIN now_playing n ON n.user_id = u.id "
            "WHERE u.id != ? AND u.share_activity = 1 AND u.is_suspended = 0 "
            "ORDER BY u.display_name",
            (viewer["id"],)).fetchall()
    except sqlite3.Error as e:
        logger.warning("household read failed: %s", e)
        return []
    finally:
        conn.close()

    people = []
    for r in rows:
        d = dict(r)
        track = None
        if d.get("title") or d.get("artist"):
            track = {
                "artist": d["artist"], "title": d["title"],
                "album": d["album"], "videoId": d["video_id"],
                "channel": d["channel_name"], "at": d["updated_at"],
                "live": _is_live(d["updated_at"]),
            }
        people.append({
            "id": d["id"],
            "name": d["display_name"] or d["login_name"] or "Someone",
            "track": track,
            "liked_count": len(liked_songs(d["id"])),
        })
    return people


def can_view(viewer: dict, target_id: str) -> bool:
    """Whether `viewer` may see `target_id`'s shared lists.

    Both sides must have sharing on. Viewing yourself is always allowed.
    """
    if not viewer:
        return False
    if viewer["id"] == target_id:
        return True
    if not viewer.get("share_activity", 1):
        return False
    try:
        conn = get_db()
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT share_activity, is_suspended FROM users WHERE id = ?",
            (target_id,)).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return bool(row and row["share_activity"] and not row["is_suspended"])


def liked_songs(user_id: str) -> list[dict]:
    """Someone's thumbs-up list, newest first. Read-only."""
    if not user_id:
        return []
    try:
        songs = thumbs.load_thumbs(data_dir=data_dir() / user_id)
    except Exception as e:
        logger.debug("could not read likes for %s: %s", user_id, e)
        return []
    return list(reversed(songs))


def display_name(user_id: str) -> str:
    try:
        conn = get_db()
    except sqlite3.Error:
        return "Someone"
    try:
        row = conn.execute(
            "SELECT display_name, login_name FROM users WHERE id = ?",
            (user_id,)).fetchone()
        if not row:
            return "Someone"
        return row["display_name"] or row["login_name"] or "Someone"
    except sqlite3.Error:
        return "Someone"
    finally:
        conn.close()


def set_sharing(user_id: str, enabled: bool) -> None:
    """Turn activity sharing on or off, clearing presence when turning off."""
    try:
        conn = get_db()
    except sqlite3.Error:
        return
    try:
        conn.execute("UPDATE users SET share_activity = ? WHERE id = ?",
                     (int(bool(enabled)), user_id))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    if not enabled:
        clear_now_playing(user_id)
