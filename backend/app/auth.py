"""Small stateless signed sessions for the local mock-account login flow."""

import base64
import hashlib
import hmac
import json
import time

SESSION_TTL_SECONDS = 8 * 60 * 60


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_session_token(email: str, secret: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    now = int(time.time())
    header = _encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _encode(json.dumps({"sub": email, "iat": now, "exp": now + ttl_seconds}, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_encode(signature)}"


def read_session_token(token: str, secret: str) -> str | None:
    if not secret:
        return None
    try:
        header, payload, signature = token.split(".")
        signing_input = f"{header}.{payload}".encode("ascii")
        expected = _encode(hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if not isinstance(data.get("sub"), str) or int(data.get("exp", 0)) <= int(time.time()):
            return None
        return data["sub"]
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
