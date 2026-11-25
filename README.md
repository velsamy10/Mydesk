# MyDesk — NIFTY Monitor v1

Local (and Vercel-ready) NIFTY options helper: PCR, CPR, liquidity, VWAP, IV, India VIX.  
**Manual Refresh only** — no timed NSE polling.

## Run locally

```bat
run_monitor.bat
```

Or:

```bat
python -u nifty_monitor.py --port 8765 --range 3
```

Open **http://127.0.0.1:8765/** → **Register** → **Log in** → **Refresh now**.

Accounts are stored in `data/users.json` with PBKDF2 password hashes (never plaintext).

Requires Python 3.10+ on PATH.

## Main files

| File | Purpose |
|------|---------|
| `nifty_monitor.py` | Local UI + signal logic |
| `app.py` + `pyproject.toml` | Vercel WSGI entrypoint (`app:app`) |
| `external_option_chain_service.py` | NSE option-chain fetch |
| `public/` + `vercel.json` | Static UI + function config |
| `*.pine.txt` | TradingView indicators |

## Notes

- After hours, bid/ask may be empty and OI may show not usable — normal.
- Vercel: NSE may block cloud IPs or hit function timeouts; local run is the reliable path for live chain data.
