import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

from services.paths import data_dir

DB_DIR = data_dir()
DB_PATH = DB_DIR / "users.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    discogs_username TEXT,
    discogs_token TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_suspended INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- What each person is listening to right now, for the household view.
-- One row per user, overwritten as they play — this is presence, not history,
-- and history already lives in each user's own history.json.
CREATE TABLE IF NOT EXISTS now_playing (
    user_id      TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    artist       TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    album        TEXT NOT NULL DEFAULT '',
    video_id     TEXT NOT NULL DEFAULT '',
    channel_name TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invite_tokens (
    token TEXT PRIMARY KEY,
    created_by TEXT NOT NULL REFERENCES users(id),
    used_by TEXT REFERENCES users(id),
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);
"""


def get_db() -> sqlite3.Connection:
    """Return a connection to the users database."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN is_suspended INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE invite_tokens ADD COLUMN label TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE users ADD COLUMN allowed_models TEXT NOT NULL DEFAULT 'all'",
    # Username + password sign-in, so people other than the machine's owner
    # can use the app over the local network.
    "ALTER TABLE users ADD COLUMN login_name TEXT",
    "ALTER TABLE users ADD COLUMN password_hash TEXT",
    "ALTER TABLE users ADD COLUMN failed_logins INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN locked_until TEXT",
    "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0",
    # Whether this account shows what it's playing to the rest of the
    # household. On by default — the feature is opt-out, not opt-in, because
    # everyone here is in the same house and asked for it — but it is a
    # switch, not a fact of life.
    "ALTER TABLE users ADD COLUMN share_activity INTEGER NOT NULL DEFAULT 1",
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    # Login names are the lookup key for sign-in and must be unique. A partial
    # index keeps the constraint off rows that have no login name.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_name "
    "ON users(login_name) WHERE login_name IS NOT NULL",
]


def init_db() -> None:
    """Create tables if they don't exist, and run migrations."""
    conn = get_db()
    try:
        conn.executescript(_SCHEMA)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # Column already exists
        for sql in _INDEXES:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as e:
                # A duplicate login_name in existing data would block the
                # unique index. Say so rather than failing silently.
                logger.warning("Could not create index (%s): %s", e, sql)
        conn.commit()
        logger.info("Database initialized at %s", DB_PATH)
    finally:
        conn.close()
