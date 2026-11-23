"""Password hashing helpers using only Python's standard library."""

import base64
import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 600_000
PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"{PASSWORD_HASH_ALGORITHM}${PBKDF2_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, rounds, salt_text, digest_text = password_hash.split("$", 3)
        iterations = int(rounds)
        if (
            algorithm != PASSWORD_HASH_ALGORITHM
            or not 100_000 <= iterations <= 2_000_000
        ):
            return False
        salt = _b64decode(salt_text)
        expected = _b64decode(digest_text)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False
