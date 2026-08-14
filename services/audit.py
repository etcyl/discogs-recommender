"""Append-only audit log for AI generation.

Every playlist the app generates is recorded: which model produced it, under
what settings, what came back, and what the fact check made of it. The point
is that a recommendation should be traceable after the fact — you can ask
"where did this song come from and did anything back it up?" and get an answer
without re-running anything.

Two tables, both append-only in normal operation:

  generation_runs   one row per generation — model, settings, counts, timing
  generation_items  one row per recommended song — including ones that were
                    dropped, so a filtered-out recommendation is still on the
                    record rather than vanishing

Nothing in here raises at the caller. An audit log that can take the app down
with it is worse than one that occasionally misses a row, so every failure —
including failing to open the database at all — is logged and swallowed.
Reads are exposed through /audit, scoped to the account that owns the runs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from services.database import get_db

logger = logging.getLogger(__name__)

# How long rows are kept. The log is for accountability, not analytics — it
# does not need to grow without bound.
RETENTION_DAYS = 90

_SCHEMA = """
CREATE TABLE IF NOT EXISTS generation_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    channel_id        TEXT NOT NULL,
    channel_name      TEXT NOT NULL DEFAULT '',
    source_type       TEXT NOT NULL,
    mode              TEXT NOT NULL DEFAULT '',
    ai_model          TEXT NOT NULL,
    prompt_tier       TEXT NOT NULL DEFAULT 'auto',
    prompt_sha256     TEXT NOT NULL DEFAULT '',
    discovery         INTEGER,
    era_from          INTEGER,
    era_to            INTEGER,
    deep_cuts         INTEGER NOT NULL DEFAULT 0,
    num_requested     INTEGER NOT NULL DEFAULT 0,
    num_generated     INTEGER NOT NULL DEFAULT 0,
    num_verified      INTEGER NOT NULL DEFAULT 0,
    num_corrected     INTEGER NOT NULL DEFAULT 0,
    num_unverified    INTEGER NOT NULL DEFAULT 0,
    num_dropped       INTEGER NOT NULL DEFAULT 0,
    verification_policy TEXT NOT NULL DEFAULT 'flag',
    duration_ms       INTEGER,
    app_version       TEXT NOT NULL DEFAULT '',
    notes             TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS generation_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES generation_runs(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    artist        TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    album         TEXT NOT NULL DEFAULT '',
    year          TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    match_score   INTEGER,
    obscurity     INTEGER,
    credit_claim  TEXT NOT NULL DEFAULT '',
    verify_status TEXT NOT NULL DEFAULT 'skipped',
    verify_source TEXT NOT NULL DEFAULT '',
    verify_conf   REAL,
    matched_as    TEXT NOT NULL DEFAULT '',
    kept          INTEGER NOT NULL DEFAULT 1,
    drop_reason   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_runs_user_time ON generation_runs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_run ON generation_items(run_id);
"""

# Applied after the schema; each is expected to fail once the column exists.
_MIGRATIONS = [
    "ALTER TABLE generation_items ADD COLUMN drop_reason TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE generation_runs ADD COLUMN num_unresolved INTEGER NOT NULL DEFAULT 0",
]


@contextmanager
def _db(operation: str):
    """Yield a connection, or None when the database can't serve this call.

    Opening the database can fail on its own — a full disk, a permissions
    change, a locked file — so that has to be handled here rather than at the
    call site. Callers check for None and give up quietly.
    """
    conn = None
    try:
        conn = get_db()
    except (sqlite3.Error, OSError) as e:
        logger.warning("Audit %s: cannot open the database: %s", operation, e)
        yield None
        return
    try:
        yield conn
    except (sqlite3.Error, OSError) as e:
        logger.warning("Audit %s failed: %s", operation, e)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise _Unavailable from e
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


class _Unavailable(Exception):
    """Internal marker: the audit store could not service this call."""


def init_audit_db() -> None:
    try:
        with _db("init") as conn:
            if conn is None:
                return
            conn.executescript(_SCHEMA)
            for sql in _MIGRATIONS:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists
            conn.commit()
    except _Unavailable:
        pass


def prompt_fingerprint(system_prompt: str, user_prompt: str) -> str:
    """A stable hash of the exact prompt pair that produced a run.

    The prompts themselves are not stored — they embed the listener's
    collection and listening history, and the log should not become a second
    copy of that. The hash is enough to prove two runs used the same prompt,
    or to tie a run to a prompt you still have.
    """
    h = hashlib.sha256()
    h.update((system_prompt or "").encode("utf-8"))
    h.update(b"\x00")   # unambiguous boundary: ("ab","c") != ("a","bc")
    h.update((user_prompt or "").encode("utf-8"))
    return h.hexdigest()


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _item_rows(run_id: int, songs_kept: list[dict],
               songs_dropped: list[dict],
               songs_unresolved: list[dict] | None = None) -> list[tuple]:
    """One row per recommendation, kept or not.

    Three fates: kept, dropped by the accuracy check, or dropped because no
    playable video could be matched. All three are recorded — a recommendation
    that never reached the listener is exactly the thing an audit log exists
    to preserve.
    """
    rows = []
    tagged = ([(s, 1, "") for s in songs_kept]
              + [(s, 0, "unverified") for s in songs_dropped]
              + [(s, 0, "no-playable-match") for s in (songs_unresolved or [])])
    for position, (song, kept, drop_reason) in enumerate(tagged):
        v = song.get("verification") or {}
        matched = ""
        if v.get("matched_artist") or v.get("matched_title"):
            matched = f'{v.get("matched_artist", "")} — {v.get("matched_title", "")}'
        rows.append((
            run_id, position,
            str(song.get("artist", ""))[:300],
            str(song.get("title", ""))[:300],
            str(song.get("album", ""))[:300],
            str(song.get("year", ""))[:16],
            str(song.get("reason", ""))[:1000],
            _as_int(song.get("match_score")),
            _as_int(song.get("obscurity_score")),
            str(song.get("credit_connection", ""))[:500],
            v.get("status", "skipped"), v.get("source", ""),
            v.get("confidence"), matched[:300], kept, drop_reason))
    return rows


def record_run(*, user_id: str, channel_id: str, channel_name: str = "",
               source_type: str, mode: str = "", ai_model: str,
               prompt_tier: str = "auto", prompt_sha256: str = "",
               discovery: int | None = None,
               era_from: int | None = None, era_to: int | None = None,
               deep_cuts: bool = False,
               num_requested: int = 0,
               songs_kept: list[dict] | None = None,
               songs_dropped: list[dict] | None = None,
               songs_unresolved: list[dict] | None = None,
               verification_summary: dict | None = None,
               duration_ms: int | None = None,
               app_version: str = "", notes: str = "") -> int | None:
    """Write one run and its items. Returns the run id, or None on failure."""
    songs_kept = songs_kept or []
    songs_dropped = songs_dropped or []
    songs_unresolved = songs_unresolved or []
    vs = verification_summary or {}

    try:
        with _db("write") as conn:
            if conn is None:
                return None
            cur = conn.execute(
                """INSERT INTO generation_runs
                   (created_at, user_id, channel_id, channel_name, source_type,
                    mode, ai_model, prompt_tier, prompt_sha256, discovery,
                    era_from, era_to, deep_cuts, num_requested, num_generated,
                    num_verified, num_corrected, num_unverified, num_dropped,
                    num_unresolved, verification_policy, duration_ms,
                    app_version, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 user_id, channel_id, channel_name, source_type, mode,
                 ai_model, prompt_tier, prompt_sha256, discovery,
                 era_from, era_to, int(bool(deep_cuts)), num_requested,
                 # Everything the model produced, however it ended up.
                 len(songs_kept) + len(songs_dropped) + len(songs_unresolved),
                 vs.get("verified", 0), vs.get("corrected", 0),
                 vs.get("unverified", 0), vs.get("dropped", 0),
                 len(songs_unresolved),
                 vs.get("policy", "flag"), duration_ms, app_version, notes))
            run_id = cur.lastrowid

            rows = _item_rows(run_id, songs_kept, songs_dropped, songs_unresolved)
            if rows:
                conn.executemany(
                    """INSERT INTO generation_items
                       (run_id, position, artist, title, album, year, reason,
                        match_score, obscurity, credit_claim, verify_status,
                        verify_source, verify_conf, matched_as, kept, drop_reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)

            conn.commit()
            return run_id
    except _Unavailable:
        return None


def list_runs(user_id: str | None = None, limit: int = 50,
              offset: int = 0) -> list[dict]:
    """Most recent runs first. user_id=None returns every user's (admin only)."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    try:
        with _db("read") as conn:
            if conn is None:
                return []
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM generation_runs WHERE user_id = ? "
                    "ORDER BY id DESC LIMIT ? OFFSET ?",
                    (user_id, limit, offset)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM generation_runs ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()
            return [dict(r) for r in rows]
    except _Unavailable:
        return []


def get_run(run_id: int, user_id: str | None = None) -> dict | None:
    """One run with its items. Pass user_id to scope access to that owner."""
    try:
        with _db("read") as conn:
            if conn is None:
                return None
            if user_id:
                row = conn.execute(
                    "SELECT * FROM generation_runs WHERE id = ? AND user_id = ?",
                    (run_id, user_id)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM generation_runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return None
            items = conn.execute(
                "SELECT * FROM generation_items WHERE run_id = ? ORDER BY position",
                (run_id,)).fetchall()
            run = dict(row)
            run["items"] = [dict(i) for i in items]
            return run
    except _Unavailable:
        return None


def stats(user_id: str | None = None) -> dict:
    """Aggregate accuracy per model — the numbers that justify the guardrail."""
    try:
        with _db("stats") as conn:
            if conn is None:
                return {"by_model": []}
            where, params = ("WHERE user_id = ?", (user_id,)) if user_id else ("", ())
            rows = conn.execute(
                f"""SELECT ai_model,
                           COUNT(*)            AS runs,
                           SUM(num_generated)  AS generated,
                           SUM(num_verified)   AS verified,
                           SUM(num_corrected)  AS corrected,
                           SUM(num_unverified) AS unverified,
                           SUM(num_dropped)    AS dropped,
                           SUM(num_unresolved) AS unresolved,
                           AVG(duration_ms)    AS avg_ms
                    FROM generation_runs {where}
                    GROUP BY ai_model ORDER BY runs DESC""", params).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                checked = ((d["verified"] or 0) + (d["corrected"] or 0)
                           + (d["unverified"] or 0))
                d["verified_pct"] = (
                    round(100 * ((d["verified"] or 0) + (d["corrected"] or 0))
                          / checked, 1) if checked else None)
                out.append(d)
            return {"by_model": out}
    except _Unavailable:
        return {"by_model": []}


def prune(retention_days: int = RETENTION_DAYS) -> int:
    """Delete runs older than the retention window. Returns rows removed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)) \
        .isoformat(timespec="seconds")
    try:
        with _db("prune") as conn:
            if conn is None:
                return 0
            # get_db sets foreign_keys=ON, so items cascade.
            cur = conn.execute(
                "DELETE FROM generation_runs WHERE created_at < ?", (cutoff,))
            conn.commit()
            if cur.rowcount:
                logger.info("Audit prune removed %d runs older than %d days",
                            cur.rowcount, retention_days)
            return cur.rowcount
    except _Unavailable:
        return 0


def export_run(run_id: int, user_id: str | None = None) -> str:
    """A run as pretty JSON, for handing to someone outside the app."""
    run = get_run(run_id, user_id)
    return json.dumps(run or {}, indent=2, ensure_ascii=False)
