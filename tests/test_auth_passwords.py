"""Tests for password sign-in in services/auth_service.py."""
import pytest

from services import auth_service, passwords
from services.auth_service import AuthError

PW = "correct horse battery"


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    from services import database
    monkeypatch.setattr(database, "DB_DIR", tmp_path)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "auth.db")
    database.init_db()
    yield


def make(login="sam", pw=PW, name="Sam"):
    return auth_service.create_account(display_name=name, login_name=login,
                                       password=pw)


class TestCreateAccount:
    def test_creates_a_non_admin_account(self):
        user = make()
        assert user["login_name"] == "sam"
        assert user["display_name"] == "Sam"
        assert not user["is_admin"]

    def test_password_is_not_stored_in_the_clear(self):
        user = make()
        assert PW not in (user["password_hash"] or "")
        assert user["password_hash"].startswith("scrypt$")

    def test_username_is_normalised(self):
        assert make(login="  SAM  ")["login_name"] == "sam"

    def test_duplicate_username_is_refused(self):
        make(login="sam")
        with pytest.raises(AuthError, match="already taken"):
            make(login="SAM")

    def test_weak_password_is_refused(self):
        with pytest.raises(passwords.PasswordError):
            make(pw="short")

    def test_invalid_username_is_refused(self):
        with pytest.raises(passwords.PasswordError):
            make(login="sam smith")

    def test_defaults_to_local_models_only(self):
        """A guest shouldn't be able to spend the owner's API credits."""
        assert auth_service.get_allowed_models(make()) == {"ollama"}


class TestAuthenticate:
    def test_correct_credentials(self):
        make()
        assert auth_service.authenticate("sam", PW)["login_name"] == "sam"

    def test_username_is_case_insensitive(self):
        make()
        assert auth_service.authenticate("SAM", PW)

    def test_wrong_password(self):
        make()
        with pytest.raises(AuthError, match="Incorrect username or password"):
            auth_service.authenticate("sam", "wrong horse battery")

    def test_unknown_user_gives_the_same_message(self):
        """Different wording would let someone enumerate accounts."""
        make()
        with pytest.raises(AuthError, match="Incorrect username or password"):
            auth_service.authenticate("nobody", PW)

    def test_empty_credentials(self):
        make()
        with pytest.raises(AuthError):
            auth_service.authenticate("", "")

    def test_suspended_account_cannot_sign_in(self):
        user = make()
        auth_service.suspend_user(user["id"])
        with pytest.raises(AuthError, match="suspended"):
            auth_service.authenticate("sam", PW)


class TestLockout:
    def test_locks_after_repeated_failures(self):
        make()
        for _ in range(auth_service.MAX_FAILED_LOGINS):
            with pytest.raises(AuthError):
                auth_service.authenticate("sam", "wrong horse battery")
        # Even the right password is refused while locked.
        with pytest.raises(AuthError, match="Too many failed attempts"):
            auth_service.authenticate("sam", PW)

    def test_a_success_clears_the_failure_count(self):
        make()
        for _ in range(auth_service.MAX_FAILED_LOGINS - 1):
            with pytest.raises(AuthError):
                auth_service.authenticate("sam", "wrong horse battery")
        auth_service.authenticate("sam", PW)
        assert auth_service.get_user_by_login("sam")["failed_logins"] == 0

    def test_lockout_expires(self, monkeypatch):
        from datetime import datetime, timedelta
        user = make()
        past = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
        from services.database import get_db
        conn = get_db()
        conn.execute("UPDATE users SET locked_until = ? WHERE id = ?",
                     (past, user["id"]))
        conn.commit()
        conn.close()
        assert auth_service.authenticate("sam", PW)


class TestPasswordChanges:
    def test_change_requires_the_current_password(self):
        user = make()
        with pytest.raises(AuthError, match="Current password is incorrect"):
            auth_service.change_password(user["id"], "nope nope nope", "new phrase here")

    def test_change_then_sign_in_with_the_new_one(self):
        user = make()
        auth_service.change_password(user["id"], PW, "a different phrase")
        with pytest.raises(AuthError):
            auth_service.authenticate("sam", PW)
        assert auth_service.authenticate("sam", "a different phrase")

    def test_admin_reset_forces_a_change_and_signs_out(self):
        user = make()
        session = auth_service.create_session(user["id"])
        auth_service.set_password(user["id"], "temporary phrase", must_change=True)
        assert auth_service.validate_session(session) is None
        assert auth_service.authenticate("sam", "temporary phrase")["must_change_password"]

    def test_weak_new_password_is_refused(self):
        user = make()
        with pytest.raises(passwords.PasswordError):
            auth_service.change_password(user["id"], PW, "short")


class TestLoginName:
    def test_assigns_a_username(self):
        admin = auth_service.create_admin_user("Owner", "owner", "tok")
        assert auth_service.set_login_name(admin["id"], "Owner") == "owner"

    def test_refuses_a_taken_username(self):
        make(login="sam")
        admin = auth_service.create_admin_user("Owner", "owner", "tok")
        with pytest.raises(AuthError, match="already taken"):
            auth_service.set_login_name(admin["id"], "sam")
