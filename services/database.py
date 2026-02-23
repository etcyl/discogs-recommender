import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_DIR = Path(__file__).resolve().parent.parent / "data"
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
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN is_suspended INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE invite_tokens ADD COLUMN label TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE users ADD COLUMN allowed_models TEXT NOT NULL DEFAULT 'all'",
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
        conn.commit()
        logger.info("Database initialized at %s", DB_PATH)
    finally:
        conn.close()
