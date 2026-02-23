"""Tests for services/auth_service.py and services/database.py."""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from services.database import init_db, get_db, DB_PATH
from services import auth_service


@pytest.fixture(autouse=True)
def use_temp_db(tmp_path):
    """Redirect the database to a temp directory for every test."""
    test_db = tmp_path / "users.db"
    with patch("services.database.DB_DIR", tmp_path), \
         patch("services.database.DB_PATH", test_db), \
         patch("services.auth_service.DATA_DIR", tmp_path):
        init_db()
        yield tmp_path


class TestDatabase:
    def test_init_creates_tables(self, use_temp_db):
        conn = get_db()
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {r["name"] for r in tables}
            assert "users" in table_names
            assert "sessions" in table_names
            assert "invite_tokens" in table_names
        finally:
            conn.close()

    def test_init_is_idempotent(self, use_temp_db):
        init_db()
        init_db()
        conn = get_db()
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            assert len([r for r in tables if r["name"] == "users"]) == 1
        finally:
            conn.close()


class TestAdminUser:
    def test_create_admin(self):
        user = auth_service.create_admin_user("admin", "admin_user", "token123")
        assert user["display_name"] == "admin"
        assert user["is_admin"] == 1
        assert user["discogs_username"] == "admin_user"

    def test_create_admin_idempotent(self):
        user1 = auth_service.create_admin_user("admin", "admin_user", "token123")
        user2 = auth_service.create_admin_user("admin2", "admin_user2", "token456")
        assert user1["id"] == user2["id"]

    def test_get_admin_user(self):
        auth_service.create_admin_user("admin", "admin_user", "token123")
        admin = auth_service.get_admin_user()
        assert admin is not None
        assert admin["is_admin"] == 1

    def test_get_admin_user_none(self):
        assert auth_service.get_admin_user() is None

    def test_get_user(self):
        user = auth_service.create_admin_user("admin", "admin_user", "token123")
        found = auth_service.get_user(user["id"])
        assert found is not None
        assert found["id"] == user["id"]

    def test_get_user_missing(self):
        assert auth_service.get_user("nonexistent") is None


class TestSessions:
    def test_create_and_validate_session(self):
        user = auth_service.create_admin_user("admin", "u", "t")
        session_id = auth_service.create_session(user["id"])
        assert session_id is not None

        validated = auth_service.validate_session(session_id)
        assert validated is not None
        assert validated["id"] == user["id"]

    def test_validate_expired_session(self):
        user = auth_service.create_admin_user("admin", "u", "t")
        session_id = auth_service.create_session(user["id"], max_age_days=0)

        # Manually expire the session
        conn = get_db()
        try:
            past = (datetime.utcnow() - timedelta(hours=1)).isoformat()
            conn.execute("UPDATE sessions SET expires_at = ? WHERE session_id = ?",
                         (past, session_id))
            conn.commit()
        finally:
            conn.close()

        assert auth_service.validate_session(session_id) is None

    def test_validate_nonexistent_session(self):
        assert auth_service.validate_session("nonexistent") is None

    def test_validate_empty_session(self):
        assert auth_service.validate_session("") is None
        assert auth_service.validate_session(None) is None

    def test_delete_session(self):
        user = auth_service.create_admin_user("admin", "u", "t")
        session_id = auth_service.create_session(user["id"])
        auth_service.delete_session(session_id)
        assert auth_service.validate_session(session_id) is None

    def test_cleanup_expired_sessions(self):
        user = auth_service.create_admin_user("admin", "u", "t")
        session_id = auth_service.create_session(user["id"])

        conn = get_db()
        try:
            past = (datetime.utcnow() - timedelta(hours=1)).isoformat()
            conn.execute("UPDATE sessions SET expires_at = ? WHERE session_id = ?",
                         (past, session_id))
            conn.commit()
        finally:
            conn.close()

        auth_service.cleanup_expired_sessions()
        # Verify the session row was deleted
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?",
                               (session_id,)).fetchone()
            assert row is None
        finally:
            conn.close()


class TestInvites:
    def test_create_invite(self):
        user = auth_service.create_admin_user("admin", "u", "t")
        token = auth_service.create_invite(user["id"])
        assert token is not None
        assert len(token) > 10

    def test_get_valid_invite(self):
        user = auth_service.create_admin_user("admin", "u", "t")
        token = auth_service.create_invite(user["id"])
        invite = auth_service.get_invite(token)
        assert invite is not None
        assert invite["token"] == token
        assert invite["is_active"] == 1

    def test_get_expired_invite(self):
        user = auth_service.create_admin_user("admin", "u", "t")
        token = auth_service.create_invite(user["id"], expires_hours=0)

        # Manually expire
        conn = get_db()
        try:
            past = (datetime.utcnow() - timedelta(hours=1)).isoformat()
            conn.execute("UPDATE invite_tokens SET expires_at = ? WHERE token = ?",
                         (past, token))
            conn.commit()
        finally:
            conn.close()

        assert auth_service.get_invite(token) is None

    def test_get_nonexistent_invite(self):
        assert auth_service.get_invite("nonexistent") is None

    def test_get_empty_invite(self):
        assert auth_service.get_invite("") is None

    def test_revoke_invite(self):
        user = auth_service.create_admin_user("admin", "u", "t")
        token = auth_service.create_invite(user["id"])
        auth_service.revoke_invite(token)
        assert auth_service.get_invite(token) is None

    def test_list_invites(self):
        user = auth_service.create_admin_user("admin", "u", "t")
        auth_service.create_invite(user["id"])
        auth_service.create_invite(user["id"])
        invites = auth_service.list_invites(user["id"])
        assert len(invites) == 2

    def test_create_user_from_invite(self):
        admin = auth_service.create_admin_user("admin", "u", "t")
        token = auth_service.create_invite(admin["id"])
        user = auth_service.create_user_from_invite(token, "Bob")
        assert user["display_name"] == "Bob"
        assert user["is_admin"] == 0

        # Token should be used
        assert auth_service.get_invite(token) is None

    def test_create_user_from_invite_with_discogs(self):
        admin = auth_service.create_admin_user("admin", "u", "t")
        token = auth_service.create_invite(admin["id"])
        user = auth_service.create_user_from_invite(
            token, "Alice", discogs_username="alice123", discogs_token="tok123")
        assert user["discogs_username"] == "alice123"
        assert user["discogs_token"] == "tok123"

    def test_create_user_from_invalid_invite(self):
        with pytest.raises(ValueError, match="Invalid or expired"):
            auth_service.create_user_from_invite("bad_token", "Bob")

    def test_create_user_from_used_invite(self):
        admin = auth_service.create_admin_user("admin", "u", "t")
        token = auth_service.create_invite(admin["id"])
        auth_service.create_user_from_invite(token, "Bob")
        with pytest.raises(ValueError, match="Invalid or expired"):
            auth_service.create_user_from_invite(token, "Alice")


class TestMigrateAdminData:
    def test_migrates_files(self, use_temp_db):
        admin = auth_service.create_admin_user("admin", "u", "t")
        data_dir = use_temp_db

        # Create fake data files in root data dir
        (data_dir / "thumbs.json").write_text("[]")
        (data_dir / "channels.json").write_text("[]")

        auth_service.migrate_admin_data()

        admin_dir = data_dir / admin["id"]
        assert (admin_dir / "thumbs.json").exists()
        assert (admin_dir / "channels.json").exists()
        # Originals should be moved (not copied)
        assert not (data_dir / "thumbs.json").exists()
        assert not (data_dir / "channels.json").exists()

    def test_migration_idempotent(self, use_temp_db):
        admin = auth_service.create_admin_user("admin", "u", "t")
        data_dir = use_temp_db
        admin_dir = data_dir / admin["id"]
        admin_dir.mkdir(parents=True, exist_ok=True)

        # Already migrated
        (admin_dir / "thumbs.json").write_text('[{"test": true}]')

        auth_service.migrate_admin_data()
        # Should not overwrite existing
        assert '[{"test": true}]' in (admin_dir / "thumbs.json").read_text()

    def test_no_admin_noop(self):
        # No admin exists, should not crash
        auth_service.migrate_admin_data()


class TestModelAccess:
    def test_admin_gets_all_models(self):
        user = {"is_admin": 1, "allowed_models": "ollama"}
        assert auth_service.get_allowed_models(user) == {"claude-sonnet", "claude-haiku", "ollama"}

    def test_all_default(self):
        user = {"is_admin": 0, "allowed_models": "all"}
        assert auth_service.get_allowed_models(user) == {"claude-sonnet", "claude-haiku", "ollama"}

    def test_missing_field_defaults_to_all(self):
        user = {"is_admin": 0}
        assert auth_service.get_allowed_models(user) == {"claude-sonnet", "claude-haiku", "ollama"}

    def test_comma_separated(self):
        user = {"is_admin": 0, "allowed_models": "claude-sonnet,claude-haiku"}
        assert auth_service.get_allowed_models(user) == {"claude-sonnet", "claude-haiku"}

    def test_single_model(self):
        user = {"is_admin": 0, "allowed_models": "ollama"}
        assert auth_service.get_allowed_models(user) == {"ollama"}

    def test_invalid_models_filtered(self):
        user = {"is_admin": 0, "allowed_models": "ollama,fake-model"}
        assert auth_service.get_allowed_models(user) == {"ollama"}

    def test_update_allowed_models(self, use_temp_db):
        admin = auth_service.create_admin_user("admin", "u", "t")
        token = auth_service.create_invite(admin["id"])
        invited = auth_service.create_user_from_invite(token, "Bob")

        auth_service.update_user_allowed_models(invited["id"], "ollama")
        user = auth_service.get_user(invited["id"])
        assert user["allowed_models"] == "ollama"

    def test_update_rejects_invalid_model(self, use_temp_db):
        admin = auth_service.create_admin_user("admin", "u", "t")
        token = auth_service.create_invite(admin["id"])
        invited = auth_service.create_user_from_invite(token, "Bob")

        with pytest.raises(ValueError, match="Invalid model"):
            auth_service.update_user_allowed_models(invited["id"], "gpt-4")

    def test_cannot_change_admin_models(self, use_temp_db):
        admin = auth_service.create_admin_user("admin", "u", "t")
        auth_service.update_user_allowed_models(admin["id"], "ollama")
        # Admin should be unchanged (AND is_admin = 0 prevents update)
        user = auth_service.get_user(admin["id"])
        assert user.get("allowed_models", "all") == "all"
