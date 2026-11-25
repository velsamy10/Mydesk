"""
Vercel Python entrypoint (WSGI).
Local desktop use remains: python nifty_monitor.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("VERCEL", "1")

from auth_store import (  # noqa: E402
    COOKIE_NAME,
    auth_enabled,
    authenticate,
    client_ip,
    create_session,
    current_user,
    destroy_session,
    ensure_superadmin,
    is_admin_username,
    list_user_logs,
    list_users_safe,
    parse_cookies,
    read_json_body,
    register_user,
    resolve_user,
    session_cookie_header,
)

ensure_superadmin()


def _cfg_from_query(qs: dict[str, list[str]]) -> dict:
    cfg: dict = {}
    if "range" in qs:
        try:
            cfg["range"] = int(qs["range"][0])
        except ValueError:
            pass
    if "strike" in qs:
        try:
            cfg["strike"] = int(qs["strike"][0])
        except ValueError:
            pass
    if "expiry" in qs and qs["expiry"][0]:
        cfg["expiry"] = qs["expiry"][0]
    return cfg


def _send(
    start_response,
    status: str,
    body: bytes,
    content_type: str,
    extra_headers: list[tuple[str, str]] | None = None,
):
    headers = [
        ("Content-Type", content_type),
        ("Cache-Control", "no-store"),
        ("Content-Length", str(len(body))),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def _json(
    start_response,
    code: int,
    obj,
    extra_headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    body = json.dumps(obj, default=str).encode("utf-8")
    status = {
        200: "200 OK",
        201: "201 Created",
        204: "204 No Content",
        400: "400 Bad Request",
        401: "401 Unauthorized",
        403: "403 Forbidden",
        404: "404 Not Found",
        500: "500 Internal Server Error",
    }.get(code, f"{code} OK")
    return _send(start_response, status, body, "application/json; charset=utf-8", extra_headers)


def _file(path: Path, content_type: str) -> tuple[bytes, str] | None:
    if path.exists():
        return path.read_bytes(), content_type
    return None


def _redirect(start_response, location: str) -> list[bytes]:
    start_response("302 Found", [("Location", location), ("Cache-Control", "no-store"), ("Content-Length", "0")])
    return [b""]


def app(environ, start_response):
    method = (environ.get("REQUEST_METHOD") or "GET").upper()
    path = environ.get("PATH_INFO") or "/"
    qs = parse_qs(environ.get("QUERY_STRING") or "")
    raw_user = current_user(environ)
    user = resolve_user(raw_user)
    ip = client_ip(environ)
    need_login = auth_enabled()

    if method == "OPTIONS" and path.startswith("/api/"):
        start_response(
            "204 No Content",
            [
                ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
                ("Access-Control-Allow-Headers", "Content-Type"),
                ("Access-Control-Allow-Credentials", "true"),
                ("Content-Length", "0"),
            ],
        )
        return [b""]

    if path == "/favicon.svg" and method == "GET":
        hit = _file(ROOT / "public" / "favicon.svg", "image/svg+xml; charset=utf-8")
        if hit:
            return _send(start_response, "200 OK", hit[0], hit[1])
        return _send(start_response, "404 Not Found", b"missing", "text/plain; charset=utf-8")

    if path in ("/login.html", "/login") and method == "GET":
        if not need_login or raw_user:
            return _redirect(start_response, "/")
        hit = _file(ROOT / "public" / "login.html", "text/html; charset=utf-8")
        if hit:
            return _send(start_response, "200 OK", hit[0], hit[1])
        return _send(start_response, "404 Not Found", b"login missing", "text/plain; charset=utf-8")

    if path in ("/register.html", "/register") and method == "GET":
        if not need_login or raw_user:
            return _redirect(start_response, "/")
        hit = _file(ROOT / "public" / "register.html", "text/html; charset=utf-8")
        if hit:
            return _send(start_response, "200 OK", hit[0], hit[1])
        return _send(start_response, "404 Not Found", b"register missing", "text/plain; charset=utf-8")

    if path in ("/admin-logs.html", "/admin-logs") and method == "GET":
        if need_login and not raw_user:
            return _redirect(start_response, "/login.html")
        if need_login and not is_admin_username(raw_user):
            return _redirect(start_response, "/")
        hit = _file(ROOT / "public" / "admin-logs.html", "text/html; charset=utf-8")
        if hit:
            return _send(start_response, "200 OK", hit[0], hit[1])
        return _send(start_response, "404 Not Found", b"admin logs missing", "text/plain; charset=utf-8")

    if path == "/api/register" and method == "POST":
        body = read_json_body(environ)
        ok, msg = register_user(str(body.get("username") or ""), str(body.get("password") or ""), ip=ip)
        if not ok:
            return _json(start_response, 400, {"ok": False, "error": msg})
        return _json(start_response, 201, {"ok": True, "message": msg})

    if path == "/api/login" and method == "POST":
        body = read_json_body(environ)
        ok, name_or_err = authenticate(str(body.get("username") or ""), str(body.get("password") or ""), ip=ip)
        if not ok:
            return _json(start_response, 401, {"ok": False, "error": name_or_err})
        cookie = create_session(name_or_err)
        return _json(
            start_response,
            200,
            {"ok": True, "username": name_or_err, "is_admin": is_admin_username(name_or_err)},
            [("Set-Cookie", session_cookie_header(cookie))],
        )

    if path == "/api/logout" and method in ("GET", "POST"):
        cookies = parse_cookies(environ)
        destroy_session(cookies.get(COOKIE_NAME), ip=ip)
        return _json(
            start_response,
            200,
            {"ok": True},
            [("Set-Cookie", session_cookie_header("", clear=True))],
        )

    if path == "/api/me" and method == "GET":
        if need_login and not raw_user:
            return _json(start_response, 401, {"ok": False, "authenticated": False, "auth_enabled": True})
        admin = bool(raw_user and is_admin_username(raw_user))
        return _json(
            start_response,
            200,
            {
                "ok": True,
                "authenticated": True,
                "auth_enabled": need_login,
                "username": user,
                "is_admin": admin,
                "role": "admin" if admin else ("guest" if not need_login and not raw_user else "user"),
            },
        )

    if path == "/api/admin/logs" and method == "GET":
        if need_login and not raw_user:
            return _json(start_response, 401, {"ok": False, "error": "Login required"})
        if need_login and not is_admin_username(raw_user):
            return _json(start_response, 403, {"ok": False, "error": "Admin only"})
        limit = 200
        if "limit" in qs:
            try:
                limit = int(qs["limit"][0])
            except ValueError:
                pass
        return _json(start_response, 200, {"ok": True, "logs": list_user_logs(limit)})

    if path == "/api/admin/users" and method == "GET":
        if need_login and not raw_user:
            return _json(start_response, 401, {"ok": False, "error": "Login required"})
        if need_login and not is_admin_username(raw_user):
            return _json(start_response, 403, {"ok": False, "error": "Admin only"})
        return _json(start_response, 200, {"ok": True, "users": list_users_safe()})

    if path in ("/", "/index.html") and method == "GET":
        if need_login and not raw_user:
            return _redirect(start_response, "/login.html")
        hit = _file(ROOT / "public" / "index.html", "text/html; charset=utf-8")
        if hit:
            return _send(start_response, "200 OK", hit[0], hit[1])
        return _send(start_response, "404 Not Found", b"index.html missing", "text/plain; charset=utf-8")

    if path == "/api/state" and method == "GET":
        if need_login and not raw_user:
            return _json(start_response, 401, {"ok": False, "error": "Login required", "signal": {"call": "WAIT"}})
        try:
            from nifty_monitor import APP_VERSION, ist_session_status

            return _json(
                start_response,
                200,
                {
                    "updated_at": None,
                    "error": None,
                    "payload": None,
                    "cpr": None,
                    "vwap_ctx": None,
                    "vix": None,
                    "session": ist_session_status(),
                    "gift_nifty": None,
                    "premium": None,
                    "signal": {
                        "call": "WAIT",
                        "strength": "Idle",
                        "reasons": ["Click Refresh now for live data (manual only — no auto poll)"],
                        "buyers_sellers": "—",
                    },
                    "app_version": APP_VERSION,
                    "runtime": "vercel",
                    "username": user,
                    "is_admin": bool(raw_user and is_admin_username(raw_user)),
                    "auth_enabled": need_login,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _json(start_response, 500, {"error": str(exc), "signal": {"call": "WAIT"}})

    if path == "/api/refresh" and method in ("GET", "POST"):
        if need_login and not raw_user:
            return _json(start_response, 401, {"ok": False, "error": "Login required", "signal": {"call": "WAIT"}})
        try:
            from nifty_monitor import build_snapshot

            snap = build_snapshot(_cfg_from_query(qs))
            snap["ok"] = True
            snap["username"] = user
            snap["is_admin"] = bool(raw_user and is_admin_username(raw_user))
            snap["auth_enabled"] = need_login
            return _json(start_response, 200, snap)
        except Exception as exc:  # noqa: BLE001
            return _json(start_response, 500, {"ok": False, "error": str(exc), "signal": {"call": "WAIT"}})

    return _json(start_response, 404, {"error": "not found", "path": path})
