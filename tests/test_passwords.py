"""Tests for services/passwords.py."""
import pytest

from services import passwords
from services.passwords import PasswordError

GOOD = "correct horse battery"


class TestHashAndVerify:
    def test_round_trip(self):
        assert passwords.verify_password(GOOD, passwords.hash_password(GOOD))

    def test_wrong_password_fails(self):
        assert not passwords.verify_password("wrong horse battery",
                                             passwords.hash_password(GOOD))

    def test_hash_is_salted(self):
        """Two hashes of the same password must differ."""
        assert passwords.hash_password(GOOD) != passwords.hash_password(GOOD)

    def test_hash_does_not_contain_the_password(self):
        assert GOOD not in passwords.hash_password(GOOD)

    def test_format_is_self_describing(self):
        stored = passwords.hash_password(GOOD)
        scheme, n, r, p, salt, digest = stored.split("$")
        assert scheme == "scrypt"
        assert int(n) == passwords.SCRYPT_N
        assert len(bytes.fromhex(salt)) == passwords.SALT_BYTES
        assert len(bytes.fromhex(digest)) == passwords.KEY_BYTES

    @pytest.mark.parametrize("stored", [
        "", "notahash", "scrypt$bad", "md5$1$1$1$aa$bb",
        "scrypt$x$8$1$aa$bb", "scrypt$16384$8$1$zz$bb",
    ])
    def test_malformed_hash_fails_closed(self, stored):
        """A corrupt row must refuse the login, not raise."""
        assert passwords.verify_password(GOOD, stored) is False

    def test_empty_password_never_verifies(self):
        assert not passwords.verify_password("", passwords.hash_password(GOOD))

    def test_absurdly_long_password_is_refused(self):
        """Unbounded input into scrypt is a denial-of-service."""
        assert not passwords.verify_password("x" * 100_000,
                                             passwords.hash_password(GOOD))


class TestNeedsRehash:
    def test_current_parameters_do_not_need_rehash(self):
        assert not passwords.needs_rehash(passwords.hash_password(GOOD))

    def test_weaker_parameters_need_rehash(self):
        assert passwords.needs_rehash("scrypt$1024$8$1$aa$bb")

    def test_unknown_scheme_needs_rehash(self):
        assert passwords.needs_rehash("md5$whatever")
        assert passwords.needs_rehash("")


class TestValidate:
    def test_accepts_a_reasonable_passphrase(self):
        passwords.validate(GOOD)

    @pytest.mark.parametrize("bad,reason", [
        ("", "empty"),
        ("short", "too short"),
        ("password123", "common"),
        ("aaaaaaaaaaaa", "too few distinct characters"),
        ("abababababab", "repeated pattern"),
        ("x" * 500, "too long"),
    ])
    def test_rejects_weak_passwords(self, bad, reason):
        with pytest.raises(PasswordError):
            passwords.validate(bad)

    def test_hash_password_refuses_weak_input(self):
        with pytest.raises(PasswordError):
            passwords.hash_password("short")


class TestUsername:
    @pytest.mark.parametrize("name,expected", [
        ("Sam", "sam"), ("sam.smith", "sam.smith"),
        ("sam_1", "sam_1"), ("  Sam  ", "sam"), ("a-b", "a-b"),
    ])
    def test_accepts_and_normalises(self, name, expected):
        assert passwords.validate_username(name) == expected

    @pytest.mark.parametrize("bad", [
        "", "a", "x" * 40, "sam smith", "sam@home", "sam/../etc", "<script>",
        None, 42,
    ])
    def test_rejects_bad_usernames(self, bad):
        with pytest.raises(PasswordError):
            passwords.validate_username(bad)
