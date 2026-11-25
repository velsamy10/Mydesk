"""
User auth for mydesk — users.json + PBKDF2 password hashes + signed session cookies.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

ROOT = Path(__file__).resolve().parent
_DATA_DIR = Path(os.environ["VERCEL_DATA_DIR"]) if os.environ.get("VERCEL_DATA_DIR") else (
    Path("/tmp/mydesk_auth") if os.environ.get("VERCEL") else ROOT / "data"
)
_DATA_DIR.mkdir(parents=True, exist_ok=True)

USERS_PATH = _DATA_DIR / "users.json"
SESSIONS_PATH = _DATA_DIR / "sessions.json"
COOKIE_NAME = "mydesk_session"
PBKDF2_ITERATIONS = 260_000
SESSION_DAYS = 14

_SECRET = os.environ.get("MYDESK_AUTH_SECRET") or "mydesk-dev-secret-change-me"
_LOCK = threading.Lock()

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_users() -> dict[str, Any]:
    data = _read_json(USERS_PATH, {"users": []})
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        return {"users": []}
    return data


def _load_sessions() -> dict[str, Any]:
    data = _read_json(SESSIONS_PATH, {"sessions": {}})
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return {"sessions": {}}
    return data


def hash_password(password: str, salt: bytes | None = None) -> str:
    """One-way PBKDF2-HMAC-SHA256 (stored as encrypted/hashed form, never plaintext)."""
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_hex, digest_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(got, expected)


def register_user(username: str, password: str) -> tuple[bool, str]:
    username = (username or "").strip()
    password = password or ""
    if not _USERNAME_RE.match(username):
        return False, "Username must be 3–32 chars (letters, numbers, . _ -)."
    if len(password) < 6:
        return False, "Password must be at least 6 characters."
    if len(password) > 128:
        return False, "Password is too long."

    with _LOCK:
        data = _load_users()
        for u in data["users"]:
            if str(u.get("username", "")).lower() == username.lower():
                return False, "Username already registered."
        data["users"].append(
            {
                "username": username,
                "password_hash": hash_password(password),
                "created_at": _now_iso(),
            }
        )
        _write_json(USERS_PATH, data)
    return True, "Registered successfully. You can log in."


def authenticate(username: str, password: str) -> tuple[bool, str]:
    username = (username or "").strip()
    password = password or ""
    with _LOCK:
        data = _load_users()
        for u in data["users"]:
            if str(u.get("username", "")).lower() == username.lower():
                if verify_password(password, str(u.get("password_hash") or "")):
                    return True, str(u["username"])
                return False, "Invalid username or password."
    return False, "Invalid username or password."


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    sig = hmac.new(_SECRET.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    cookie_val = f"{token}.{sig}"
    with _LOCK:
        data = _load_sessions()
        # drop expired
        now = datetime.now(timezone.utc)
        keep: dict[str, Any] = {}
        for k, v in data["sessions"].items():
            try:
                exp = datetime.fromisoformat(str(v.get("expires")))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp > now:
                    keep[k] = v
            except (TypeError, ValueError):
                continue
        keep[token] = {"username": username, "expires": expires, "created_at": _now_iso()}
        data["sessions"] = keep
        _write_json(SESSIONS_PATH, data)
    return cookie_val


def destroy_session(cookie_val: str | None) -> None:
    token = _token_from_cookie(cookie_val)
    if not token:
        return
    with _LOCK:
        data = _load_sessions()
        data["sessions"].pop(token, None)
        _write_json(SESSIONS_PATH, data)


def _token_from_cookie(cookie_val: str | None) -> str | None:
    if not cookie_val or "." not in cookie_val:
        return None
    token, sig = cookie_val.rsplit(".", 1)
    expect = hmac.new(_SECRET.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    return token


def user_from_cookie(cookie_val: str | None) -> str | None:
    token = _token_from_cookie(cookie_val)
    if not token:
        return None
    with _LOCK:
        data = _load_sessions()
        sess = data["sessions"].get(token)
        if not sess:
            return None
        try:
            exp = datetime.fromisoformat(str(sess.get("expires")))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= datetime.now(timezone.utc):
                data["sessions"].pop(token, None)
                _write_json(SESSIONS_PATH, data)
                return None
        except (TypeError, ValueError):
            return None
        return str(sess.get("username") or "") or None


def parse_cookies(environ_or_header: dict | str | None) -> dict[str, str]:
    if isinstance(environ_or_header, dict):
        raw = environ_or_header.get("HTTP_COOKIE") or ""
    else:
        raw = environ_or_header or ""
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = unquote(v.strip())
    return out


def session_cookie_header(value: str, clear: bool = False) -> str:
    if clear:
        return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
    max_age = SESSION_DAYS * 24 * 3600
    return f"{COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"


def read_json_body(environ) -> dict[str, Any]:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    raw = environ["wsgi.input"].read(length) if length > 0 else b""
    if not raw:
        return {}
    ctype = (environ.get("CONTENT_TYPE") or "").lower()
    if "application/json" in ctype:
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
    # form-urlencoded
    qs = parse_qs(raw.decode("utf-8", errors="replace"))
    return {k: (v[0] if v else "") for k, v in qs.items()}


def current_user(environ) -> str | None:
    cookies = parse_cookies(environ)
    return user_from_cookie(cookies.get(COOKIE_NAME))
