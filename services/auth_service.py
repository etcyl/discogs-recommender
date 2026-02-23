import hmac
import logging
import secrets
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.responses import Response

from services.database import get_db

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

COOKIE_NAME = "session_id"
COOKIE_MAX_AGE = 30 * 24 * 60 * 60  # 30 days


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=COOKIE_MAX_AGE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def create_admin_user(display_name: str, discogs_username: str, discogs_token: str) -> dict:
    """Create the admin user. Idempotent — returns existing admin if one exists."""
    existing = get_admin_user()
    if existing:
        return dict(existing)

    user_id = uuid.uuid4().hex
    now = datetime.utcnow().isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (id, display_name, discogs_username, discogs_token, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (user_id, display_name, discogs_username, discogs_token, now),
        )
        conn.commit()
        logger.info("Admin user created: %s (%s)", display_name, user_id)
        return {"id": user_id, "display_name": display_name, "discogs_username": discogs_username,
                "discogs_token": discogs_token, "is_admin": 1, "created_at": now}
    finally:
        conn.close()


def get_admin_user() -> dict | None:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE is_admin = 1 LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user(user_id: str) -> dict | None:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_user_from_invite(token: str, display_name: str,
                            discogs_username: str = "", discogs_token: str = "") -> dict:
    """Validate invite token, create user, mark token as used."""
    invite = get_invite(token)
    if not invite:
        raise ValueError("Invalid or expired invite link.")

    user_id = uuid.uuid4().hex
    now = datetime.utcnow().isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (id, display_name, discogs_username, discogs_token, is_admin, created_at, allowed_models) "
            "VALUES (?, ?, ?, ?, 0, ?, 'ollama')",
            (user_id, display_name, discogs_username or None, discogs_token or None, now),
        )
        conn.execute(
            "UPDATE invite_tokens SET used_by = ?, is_active = 0 WHERE token = ?",
            (user_id, token),
        )
        conn.commit()
        logger.info("User created from invite: %s (%s)", display_name, user_id)
        return {"id": user_id, "display_name": display_name,
                "discogs_username": discogs_username or None,
                "discogs_token": discogs_token or None,
                "is_admin": 0, "created_at": now}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def create_session(user_id: str, max_age_days: int = 30) -> str:
    session_id = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    expires = now + timedelta(days=max_age_days)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO sessions (session_id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, now.isoformat(), expires.isoformat()),
        )
        conn.commit()
        return session_id
    finally:
        conn.close()


def validate_session(session_id: str) -> dict | None:
    """Return user dict if session is valid, else None."""
    if not session_id:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT s.user_id, s.expires_at, u.* FROM sessions s "
            "JOIN users u ON s.user_id = u.id "
            "WHERE s.session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            return None
        user = dict(row)
        if user.get("is_suspended"):
            return None
        return user
    finally:
        conn.close()


def delete_session(session_id: str) -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def cleanup_expired_sessions() -> None:
    now = datetime.utcnow().isoformat()
    conn = get_db()
    try:
        result = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.commit()
        if result.rowcount:
            logger.info("Cleaned up %d expired sessions", result.rowcount)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Invite management
# ---------------------------------------------------------------------------

def create_invite(created_by: str, expires_hours: int = 72, label: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    expires = now + timedelta(hours=expires_hours)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO invite_tokens (token, created_by, label, created_at, expires_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (token, created_by, label[:100], now.isoformat(), expires.isoformat()),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def get_invite(token: str) -> dict | None:
    if not token:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM invite_tokens WHERE token = ? AND is_active = 1",
            (token,),
        ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
            return None
        return dict(row)
    finally:
        conn.close()


def list_invites(created_by: str) -> list[dict]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT t.*, u.display_name AS used_by_name "
            "FROM invite_tokens t "
            "LEFT JOIN users u ON t.used_by = u.id "
            "WHERE t.created_by = ? ORDER BY t.created_at DESC",
            (created_by,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def revoke_invite(token: str) -> None:
    conn = get_db()
    try:
        conn.execute("UPDATE invite_tokens SET is_active = 0 WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def update_invite_label(token: str, label: str) -> None:
    conn = get_db()
    try:
        conn.execute("UPDATE invite_tokens SET label = ? WHERE token = ?",
                     (label[:100], token))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# User management (admin)
# ---------------------------------------------------------------------------

def list_users() -> list[dict]:
    """List all non-admin users."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM users WHERE is_admin = 0 ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def suspend_user(user_id: str) -> None:
    conn = get_db()
    try:
        conn.execute("UPDATE users SET is_suspended = 1 WHERE id = ? AND is_admin = 0",
                     (user_id,))
        # Invalidate all sessions for this user
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def unsuspend_user(user_id: str) -> None:
    conn = get_db()
    try:
        conn.execute("UPDATE users SET is_suspended = 0 WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def delete_user(user_id: str) -> None:
    """Delete a non-admin user and their sessions."""
    conn = get_db()
    try:
        # Don't allow deleting admin
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return
        if row["is_admin"]:
            raise ValueError("Cannot delete admin user")
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE invite_tokens SET used_by = NULL WHERE used_by = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        logger.info("Deleted user: %s", user_id)
    finally:
        conn.close()


def rename_user(user_id: str, display_name: str) -> None:
    conn = get_db()
    try:
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?",
                     (display_name[:100], user_id))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Model access control
# ---------------------------------------------------------------------------

ALL_MODELS = {"claude-sonnet", "claude-haiku", "ollama"}


def get_allowed_models(user: dict) -> set[str]:
    """Return the set of AI model IDs this user may use."""
    if user.get("is_admin"):
        return set(ALL_MODELS)
    raw = user.get("allowed_models", "all")
    if raw == "all":
        return set(ALL_MODELS)
    return {m.strip() for m in raw.split(",") if m.strip() in ALL_MODELS}


def update_user_allowed_models(user_id: str, allowed_models: str) -> None:
    """Update which AI models a user can access."""
    if allowed_models != "all":
        models = [m.strip() for m in allowed_models.split(",") if m.strip()]
        for m in models:
            if m not in ALL_MODELS:
                raise ValueError(f"Invalid model: {m}")
        allowed_models = ",".join(models)
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET allowed_models = ? WHERE id = ? AND is_admin = 0",
            (allowed_models, user_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bootstrap and migration
# ---------------------------------------------------------------------------

def ensure_admin_exists() -> dict:
    """Auto-create admin user from .env config on first run.

    If admin already exists, sync credentials from .env so changes
    to DISCOGS_USERNAME/DISCOGS_TOKEN take effect without DB edits.
    """
    from config import settings
    admin = get_admin_user()
    if admin:
        # Sync credentials from .env in case they changed
        if (admin.get("discogs_username") != settings.discogs_username
                or admin.get("discogs_token") != settings.discogs_token):
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE users SET display_name = ?, discogs_username = ?, discogs_token = ? "
                    "WHERE id = ?",
                    (settings.discogs_username, settings.discogs_username,
                     settings.discogs_token, admin["id"]),
                )
                conn.commit()
                logger.info("Synced admin credentials from .env")
            finally:
                conn.close()
            admin["display_name"] = settings.discogs_username
            admin["discogs_username"] = settings.discogs_username
            admin["discogs_token"] = settings.discogs_token
        return admin
    return create_admin_user(
        display_name=settings.discogs_username,
        discogs_username=settings.discogs_username,
        discogs_token=settings.discogs_token,
    )


def migrate_admin_data() -> None:
    """Move existing data/*.json files into the admin's per-user directory.

    If both root and admin-dir copies exist, merge them (append root items
    into admin-dir, then delete the root copy) so no data is lost.
    """
    import json as _json

    admin = get_admin_user()
    if not admin:
        return

    admin_dir = DATA_DIR / admin["id"]
    files_to_migrate = [
        "thumbs.json", "dislikes.json", "history.json",
        "rec_history.json", "channels.json",
    ]

    migrated = False
    for filename in files_to_migrate:
        src = DATA_DIR / filename
        dst = admin_dir / filename
        if not src.exists():
            continue

        admin_dir.mkdir(parents=True, exist_ok=True)

        if not dst.exists():
            # Simple move — first migration
            shutil.move(str(src), str(dst))
            migrated = True
        else:
            # Both exist — merge root into admin dir then delete root copy
            try:
                src_data = _json.loads(src.read_text(encoding="utf-8"))
                dst_data = _json.loads(dst.read_text(encoding="utf-8"))

                if isinstance(src_data, list) and isinstance(dst_data, list):
                    # Deduplicate by checking exact match
                    existing = {_json.dumps(item, sort_keys=True) for item in dst_data}
                    for item in src_data:
                        if _json.dumps(item, sort_keys=True) not in existing:
                            dst_data.append(item)
                    dst.write_text(_json.dumps(dst_data, indent=2, ensure_ascii=True), encoding="utf-8")
                elif isinstance(src_data, dict) and isinstance(dst_data, dict):
                    for k, v in src_data.items():
                        if k not in dst_data:
                            dst_data[k] = v
                    dst.write_text(_json.dumps(dst_data, indent=2, ensure_ascii=True), encoding="utf-8")

                src.unlink()
                migrated = True
                logger.info("Merged root-level %s into admin directory", filename)
            except Exception:
                logger.warning("Failed to merge %s, leaving root copy in place", filename)

    if migrated:
        logger.info("Migrated existing data files to admin directory: %s", admin_dir)
