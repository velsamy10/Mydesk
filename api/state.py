"""Legacy /api route kept for local parity; Vercel uses app.py (see pyproject.toml)."""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("VERCEL", "1")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        try:
            from nifty_monitor import APP_VERSION, ist_session_status

            body = json.dumps(
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
                default=str,
            ).encode("utf-8")
            code = 200
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"error": str(exc), "signal": {"call": "WAIT"}}).encode("utf-8")
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return
