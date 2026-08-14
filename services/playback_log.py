"""A record of tracks that failed to play.

"Some songs aren't playing" is not a debuggable report. This turns it into
one: which track, on whose account, from which channel, and what the player
actually said.

Most failures are not the app's fault and not fixable by retrying — a rights
holder disables embedding, or the upload is taken down. What matters is
knowing *which* tracks so a playlist can be repaired rather than quietly
losing a song every few minutes.

YouTube's IFrame API reports these:

    2    the request contained an invalid parameter
    5    the HTML5 player could not play it
    100  the video was removed, or made private
    101  the owner does not allow embedded playback
    150  same as 101

101 and 150 are by far the most common, and they are why a track can be
perfectly real, correctly matched, and still silent.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from services.database import get_db

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playback_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    event        TEXT NOT NULL,          -- error | recovered | unavailable
    artist       TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    video_id     TEXT NOT NULL DEFAULT '',
    error_code   INTEGER,
    channel_id   TEXT NOT NULL DEFAULT '',
    channel_name TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_playback_user_time
    ON playback_events(user_id, created_at DESC);
"""

# What each code means to someone reading the page, not to a developer.
ERROR_MEANING = {
    2: "The player was given a bad video reference.",
    5: "The browser's player couldn't play this video.",
    100: "The video was removed or made private on YouTube.",
    101: "The uploader doesn't allow this video to be played outside YouTube.",
    150: "The uploader doesn't allow this video to be played outside YouTube.",
}


def init_playback_db() -> None:
    try:
        conn = get_db()
    except sqlite3.Error as e:
        logger.warning("playback log: cannot open database: %s", e)
        return
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    except sqlite3.Error as e:
        logger.warning("playback log: schema failed: %s", e)
    finally:
        conn.close()


def record(user_id: str, event: str, artist: str = "", title: str = "",
           video_id: str = "", error_code: int | None = None,
           channel_id: str = "", channel_name: str = "",
           detail: str = "") -> None:
    """Append one playback event. Never raises — this is bookkeeping."""
    if not user_id or event not in ("error", "recovered", "unavailable"):
        return
    try:
        conn = get_db()
    except sqlite3.Error:
        return
    try:
        conn.execute(
            "INSERT INTO playback_events (created_at, user_id, event, artist,"
            " title, video_id, error_code, channel_id, channel_name, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), user_id,
             event, str(artist)[:300], str(title)[:300], str(video_id)[:32],
             error_code, str(channel_id)[:64], str(channel_name)[:100],
             str(detail)[:300]))
        conn.commit()
    except sqlite3.Error as e:
        logger.debug("playback log write failed: %s", e)
    finally:
        conn.close()


def recent(user_id: str | None = None, limit: int = 200) -> list[dict]:
    """Recent events, newest first. user_id=None returns everyone's."""
    limit = max(1, min(int(limit), 1000))
    try:
        conn = get_db()
    except sqlite3.Error:
        return []
    try:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM playback_events WHERE user_id = ? "
                "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT p.*, u.display_name FROM playback_events p "
                "LEFT JOIN users u ON u.id = p.user_id "
                "ORDER BY p.id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meaning"] = ERROR_MEANING.get(d.get("error_code"),
                                             "The player reported a problem.")
            out.append(d)
        return out
    except sqlite3.Error as e:
        logger.warning("playback log read failed: %s", e)
        return []
    finally:
        conn.close()


def summary(user_id: str | None = None) -> dict:
    """Counts by reason, and the tracks that fail most often."""
    try:
        conn = get_db()
    except sqlite3.Error:
        return {"by_reason": [], "worst": [], "total": 0, "recovered": 0}
    try:
        where, params = ("WHERE user_id = ?", (user_id,)) if user_id else ("", ())

        by_reason = [dict(r) for r in conn.execute(
            f"""SELECT error_code, COUNT(*) AS n FROM playback_events
                {where}{' AND' if where else 'WHERE'} event = 'error'
                GROUP BY error_code ORDER BY n DESC""", params).fetchall()]
        for row in by_reason:
            row["meaning"] = ERROR_MEANING.get(row["error_code"],
                                               "Unrecognised player error.")

        worst = [dict(r) for r in conn.execute(
            f"""SELECT artist, title, video_id, COUNT(*) AS n,
                       MAX(error_code) AS error_code
                FROM playback_events
                {where}{' AND' if where else 'WHERE'} event = 'error'
                GROUP BY lower(artist), lower(title)
                ORDER BY n DESC, artist LIMIT 50""", params).fetchall()]
        for row in worst:
            row["meaning"] = ERROR_MEANING.get(row["error_code"], "")

        totals = conn.execute(
            f"""SELECT
                  SUM(CASE WHEN event = 'error' THEN 1 ELSE 0 END) AS errors,
                  SUM(CASE WHEN event = 'recovered' THEN 1 ELSE 0 END) AS recovered
                FROM playback_events {where}""", params).fetchone()

        return {"by_reason": by_reason, "worst": worst,
                "total": (totals["errors"] or 0) if totals else 0,
                "recovered": (totals["recovered"] or 0) if totals else 0}
    except sqlite3.Error as e:
        logger.warning("playback summary failed: %s", e)
        return {"by_reason": [], "worst": [], "total": 0, "recovered": 0}
    finally:
        conn.close()


def failing_tracks(user_id: str | None = None, min_failures: int = 1) -> set:
    """(artist, title) pairs that have failed, for repairing a playlist."""
    return {(r["artist"].lower().strip(), r["title"].lower().strip())
            for r in summary(user_id)["worst"] if r["n"] >= min_failures}


def prune(retention_days: int = RETENTION_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=retention_days)).isoformat(timespec="seconds")
    try:
        conn = get_db()
    except sqlite3.Error:
        return 0
    try:
        cur = conn.execute("DELETE FROM playback_events WHERE created_at < ?",
                           (cutoff,))
        conn.commit()
        return cur.rowcount
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
