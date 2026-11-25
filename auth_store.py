"""
User auth for MyDesk — users.json + PBKDF2 password hashes + signed session cookies + user logs.
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
LOGS_PATH = _DATA_DIR / "user_logs.json"
COOKIE_NAME = "mydesk_session"
PBKDF2_ITERATIONS = 260_000
SESSION_DAYS = 14
MAX_LOGS = 2000

SUPERADMIN_USERNAME = "superadmin"
SUPERADMIN_PASSWORD = os.environ.get("MYDESK_SUPERADMIN_PASSWORD") or "Admin$123"

_SECRET = os.environ.get("MYDESK_AUTH_SECRET") or "mydesk-dev-secret-change-me"
_LOCK = threading.RLock()

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


def _load_logs() -> dict[str, Any]:
    data = _read_json(LOGS_PATH, {"logs": []})
    if not isinstance(data, dict) or not isinstance(data.get("logs"), list):
        return {"logs": []}
    return data


def hash_password(password: str, salt: bytes | None = None) -> str:
    """One-way PBKDF2-HMAC-SHA256 (never store plaintext)."""
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


def append_user_log(
    action: str,
    username: str | None = None,
    ok: bool = True,
    detail: str = "",
    ip: str | None = None,
) -> None:
    entry = {
        "ts": _now_iso(),
        "action": action,
        "username": username or "",
        "ok": bool(ok),
        "detail": detail or "",
        "ip": ip or "",
    }
    with _LOCK:
        data = _load_logs()
        logs = data["logs"]
        logs.append(entry)
        if len(logs) > MAX_LOGS:
            data["logs"] = logs[-MAX_LOGS:]
        _write_json(LOGS_PATH, data)


def list_user_logs(limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 200), MAX_LOGS))
    with _LOCK:
        logs = list(_load_logs()["logs"])
    logs.reverse()
    return logs[:limit]


def list_users_safe() -> list[dict[str, Any]]:
    with _LOCK:
        data = _load_users()
        out = []
        for u in data["users"]:
            out.append(
                {
                    "username": u.get("username"),
                    "role": u.get("role") or "user",
                    "created_at": u.get("created_at"),
                }
            )
        return out


def get_user_record(username: str) -> dict[str, Any] | None:
    username = (username or "").strip()
    with _LOCK:
        for u in _load_users()["users"]:
            if str(u.get("username", "")).lower() == username.lower():
                return dict(u)
    return None


def is_admin_username(username: str | None) -> bool:
    if not username:
        return False
    rec = get_user_record(username)
    return bool(rec and str(rec.get("role") or "").lower() == "admin")


def ensure_superadmin() -> None:
    """Create or refresh the built-in admin account (hashed password in users.json)."""
    with _LOCK:
        data = _load_users()
        found = None
        for u in data["users"]:
            if str(u.get("username", "")).lower() == SUPERADMIN_USERNAME.lower():
                found = u
                break
        if found is None:
            data["users"].append(
                {
                    "username": SUPERADMIN_USERNAME,
                    "password_hash": hash_password(SUPERADMIN_PASSWORD),
                    "role": "admin",
                    "created_at": _now_iso(),
                }
            )
            _write_json(USERS_PATH, data)
            return
        need_write = False
        if str(found.get("role") or "").lower() != "admin":
            found["role"] = "admin"
            need_write = True
        if found.get("username") != SUPERADMIN_USERNAME:
            found["username"] = SUPERADMIN_USERNAME
            need_write = True
        stored = str(found.get("password_hash") or "")
        if not stored or not verify_password(SUPERADMIN_PASSWORD, stored):
            found["password_hash"] = hash_password(SUPERADMIN_PASSWORD)
            need_write = True
        if need_write:
            _write_json(USERS_PATH, data)


def register_user(username: str, password: str, ip: str | None = None) -> tuple[bool, str]:
    username = (username or "").strip()
    password = password or ""
    if not _USERNAME_RE.match(username):
        append_user_log("register", username, False, "invalid username", ip)
        return False, "Username must be 3–32 chars (letters, numbers, . _ -)."
    if len(password) < 6:
        append_user_log("register", username, False, "password too short", ip)
        return False, "Password must be at least 6 characters."
    if len(password) > 128:
        append_user_log("register", username, False, "password too long", ip)
        return False, "Password is too long."
    if username.lower() == SUPERADMIN_USERNAME.lower():
        append_user_log("register", username, False, "reserved username", ip)
        return False, "Username is reserved."

    with _LOCK:
        data = _load_users()
        for u in data["users"]:
            if str(u.get("username", "")).lower() == username.lower():
                append_user_log("register", username, False, "already exists", ip)
                return False, "Username already registered."
        data["users"].append(
            {
                "username": username,
                "password_hash": hash_password(password),
                "role": "user",
                "created_at": _now_iso(),
            }
        )
        _write_json(USERS_PATH, data)
    append_user_log("register", username, True, "registered", ip)
    return True, "Registered successfully. You can log in."


def authenticate(username: str, password: str, ip: str | None = None) -> tuple[bool, str]:
    username = (username or "").strip()
    password = password or ""
    with _LOCK:
        data = _load_users()
        for u in data["users"]:
            if str(u.get("username", "")).lower() == username.lower():
                if verify_password(password, str(u.get("password_hash") or "")):
                    name = str(u["username"])
                    append_user_log("login", name, True, "ok", ip)
                    return True, name
                append_user_log("login", username, False, "bad password", ip)
                return False, "Invalid username or password."
    append_user_log("login", username, False, "unknown user", ip)
    return False, "Invalid username or password."


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    sig = hmac.new(_SECRET.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    cookie_val = f"{token}.{sig}"
    with _LOCK:
        data = _load_sessions()
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


def destroy_session(cookie_val: str | None, ip: str | None = None) -> None:
    token = _token_from_cookie(cookie_val)
    if not token:
        return
    username = ""
    with _LOCK:
        data = _load_sessions()
        sess = data["sessions"].pop(token, None)
        if sess:
            username = str(sess.get("username") or "")
        _write_json(SESSIONS_PATH, data)
    if username:
        append_user_log("logout", username, True, "ok", ip)


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
    qs = parse_qs(raw.decode("utf-8", errors="replace"))
    return {k: (v[0] if v else "") for k, v in qs.items()}


def client_ip(environ_or_handler_headers=None, environ: dict | None = None) -> str:
    env = environ
    if env is None and isinstance(environ_or_handler_headers, dict) and "REMOTE_ADDR" in environ_or_handler_headers:
        env = environ_or_handler_headers
    if env:
        fwd = env.get("HTTP_X_FORWARDED_FOR") or ""
        if fwd:
            return fwd.split(",")[0].strip()
        return str(env.get("REMOTE_ADDR") or "")
    return ""


def current_user(environ) -> str | None:
    cookies = parse_cookies(environ)
    return user_from_cookie(cookies.get(COOKIE_NAME))


# Seed admin account when module loads (local + Vercel)
ensure_superadmin()
