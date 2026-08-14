"""Password hashing and strength checks.

Uses scrypt from the standard library. It is memory-hard, so unlike a plain
salted SHA it does not get cheaper on a GPU, and it needs no third-party
dependency — which matters for an app whose whole selling point is that it
runs with nothing installed.

Stored format is self-describing so the parameters can be raised later without
invalidating existing hashes:

    scrypt$n$r$p$<salt-hex>$<hash-hex>
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

# ~64 MB and roughly 100ms on a modern desktop. High enough to make offline
# cracking expensive, low enough that a login does not feel slow.
SCRYPT_N = 2 ** 16
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

MIN_LENGTH = 10
MAX_LENGTH = 200          # scrypt on unbounded input is a denial-of-service

# Passwords that a network neighbour would try first.
_COMMON = {
    "password", "password1", "password123", "12345678", "123456789",
    "1234567890", "qwertyuiop", "letmein123", "iloveyou1", "admin1234",
    "welcome123", "changeme1", "musiclover", "discogs123",
}


class PasswordError(ValueError):
    """Raised when a proposed password is not acceptable."""


def hash_password(password: str) -> str:
    """Return a self-describing scrypt hash. Raises PasswordError if weak."""
    validate(password)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=KEY_BYTES, maxmem=128 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash.

    Returns False for anything malformed rather than raising, so a corrupt row
    fails the login instead of failing the request.
    """
    if not password or not stored:
        return False
    if len(password) > MAX_LENGTH:
        return False
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"),
                                salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p),
                                dklen=len(hash_hex) // 2,
                                maxmem=128 * 1024 * 1024)
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def needs_rehash(stored: str) -> bool:
    """True when a stored hash used weaker parameters than we now use."""
    try:
        scheme, n, r, p, _, _ = stored.split("$")
    except (ValueError, AttributeError):
        return True
    return (scheme != "scrypt" or int(n) < SCRYPT_N
            or int(r) < SCRYPT_R or int(p) < SCRYPT_P)


def validate(password: str) -> None:
    """Raise PasswordError if the password is too weak to accept.

    Length first, because it does more for strength than composition rules do.
    The rest only rules out the passwords an attacker guesses first; it does
    not demand a symbol-and-a-digit ritual that pushes people toward
    "Password1!".
    """
    if not isinstance(password, str) or not password:
        raise PasswordError("Please choose a password.")
    if len(password) < MIN_LENGTH:
        raise PasswordError(
            f"Password must be at least {MIN_LENGTH} characters. "
            "A short phrase you'll remember works well.")
    if len(password) > MAX_LENGTH:
        raise PasswordError(f"Password must be under {MAX_LENGTH} characters.")
    lowered = password.lower()
    if lowered in _COMMON:
        raise PasswordError("That password is too common. Please pick another.")
    if len(set(password)) < 5:
        raise PasswordError("That password repeats too few characters.")
    if re.fullmatch(r"(.+?)\1+", password):
        raise PasswordError("That password is just a repeated pattern.")


USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{2,32}$")


def validate_username(username: str) -> str:
    """Normalise and check a login name. Returns the stored form (lowercase)."""
    if not isinstance(username, str):
        raise PasswordError("Please choose a username.")
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise PasswordError(
            "Username must be 2-32 characters, letters, numbers, dot, dash or "
            "underscore.")
    return username.lower()
