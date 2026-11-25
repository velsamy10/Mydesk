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


def _send(start_response, status: str, body: bytes, content_type: str):
    start_response(
        status,
        [
            ("Content-Type", content_type),
            ("Cache-Control", "no-store"),
            ("Access-Control-Allow-Origin", "*"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


def _json(start_response, code: int, obj) -> list[bytes]:
    body = json.dumps(obj, default=str).encode("utf-8")
    status = {200: "200 OK", 204: "204 No Content", 404: "404 Not Found", 500: "500 Internal Server Error"}.get(
        code, f"{code} OK"
    )
    return _send(start_response, status, body, "application/json; charset=utf-8")


def app(environ, start_response):
    method = (environ.get("REQUEST_METHOD") or "GET").upper()
    path = environ.get("PATH_INFO") or "/"
    qs = parse_qs(environ.get("QUERY_STRING") or "")

    if method == "OPTIONS" and path.startswith("/api/"):
        start_response(
            "204 No Content",
            [
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
                ("Access-Control-Allow-Headers", "Content-Type"),
                ("Content-Length", "0"),
            ],
        )
        return [b""]

    if path == "/favicon.svg" and method == "GET":
        fav = ROOT / "public" / "favicon.svg"
        if fav.exists():
            return _send(start_response, "200 OK", fav.read_bytes(), "image/svg+xml; charset=utf-8")
        return _send(start_response, "404 Not Found", b"missing", "text/plain; charset=utf-8")

    if path in ("/", "/index.html") and method == "GET":
        html_path = ROOT / "public" / "index.html"
        if html_path.exists():
            raw = html_path.read_bytes()
            return _send(start_response, "200 OK", raw, "text/html; charset=utf-8")
        return _send(start_response, "404 Not Found", b"index.html missing", "text/plain; charset=utf-8")

    if path == "/api/state" and method == "GET":
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
                    "premium": None,
                    "signal": {
                        "call": "WAIT",
                        "strength": "Idle",
                        "reasons": ["Click Refresh now for live data (manual only — no auto poll)"],
                        "buyers_sellers": "—",
                    },
                    "app_version": APP_VERSION,
                    "runtime": "vercel",
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _json(start_response, 500, {"error": str(exc), "signal": {"call": "WAIT"}})

    if path == "/api/refresh" and method in ("GET", "POST"):
        try:
            from nifty_monitor import build_snapshot

            snap = build_snapshot(_cfg_from_query(qs))
            snap["ok"] = True
            return _json(start_response, 200, snap)
        except Exception as exc:  # noqa: BLE001
            return _json(start_response, 500, {"ok": False, "error": str(exc), "signal": {"call": "WAIT"}})

    return _json(start_response, 404, {"error": "not found", "path": path})
