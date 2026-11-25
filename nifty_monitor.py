#!/usr/bin/env python3
"""
NIFTY Signal Monitor v1 – PCR + CPR + OI + liquidity + VWAP/IV + VIX + premium risk.

  python nifty_monitor.py
  python nifty_monitor.py --port 8765

Manual Refresh only (no background / timed NSE hits).
Opens http://127.0.0.1:8765
Intraday research helper only — not financial advice. No broker auto-trade.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

APP_VERSION = "1"
# Fixed IST offset (no tzdata package required on Windows)
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from external_option_chain_service import (  # noqa: E402
    LIVE_PATH,
    atm_strike,
    fetch_once,
)

_DATA_DIR = Path(os.environ["VERCEL_DATA_DIR"]) if os.environ.get("VERCEL_DATA_DIR") else (
    Path("/tmp/nifty_monitor") if os.environ.get("VERCEL") else ROOT
)
if os.environ.get("VERCEL") or os.environ.get("VERCEL_DATA_DIR"):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
IV_BASELINE_PATH = _DATA_DIR / "iv_session_baseline.json"

STATE_LOCK = threading.Lock()
STATE: dict[str, Any] = {
    "updated_at": None,
    "error": None,
    "payload": None,
    "cpr": None,
    "vwap_ctx": None,
    "vix": None,
    "session": None,
    "gift_nifty": None,
    "premium": None,
    "signal": None,
}
CFG = {
    "range": 3,
    "step": 50,
    "refresh": 60,
    "max_stale": 300,
    "expiry": None,
    "strike": None,
}


def _yahoo_chart(symbol_enc: str, interval: str, range_: str) -> dict[str, Any]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol_enc}?interval={interval}&range={range_}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise RuntimeError(f"Yahoo chart empty for {symbol_enc}")
    return result[0]


def ist_session_status() -> dict[str, Any]:
    """NSE cash F&O hours (IST): open 09:15–15:30; new entries to 14:45."""
    now = datetime.now(IST)
    t = now.time()
    weekday = now.weekday()  # Mon=0 … Sun=6
    is_weekday = weekday < 5
    market_open = is_weekday and dtime(9, 15) <= t <= dtime(15, 30)
    entry_ok = is_weekday and dtime(9, 15) <= t <= dtime(14, 45)
    if not is_weekday:
        phase = "Weekend — market closed"
    elif t < dtime(9, 15):
        phase = "Pre-open — wait for 09:15 IST"
    elif t <= dtime(14, 45):
        phase = "Live — entries allowed"
    elif t <= dtime(15, 30):
        phase = "Live — no new entries (square-off window)"
    else:
        phase = "After hours — chain/bid-ask often stale"
    return {
        "now_ist": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "weekday": now.strftime("%A"),
        "market_open": market_open,
        "entry_ok": entry_ok,
        "phase": phase,
        "note": "Bid/ask + fresh OI need market_open=true. After hours → WAIT/unknown liquidity is normal.",
    }


def _parse_mc_num(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def fetch_gift_nifty(nifty_spot: float | None = None) -> dict[str, Any]:
    """
    GIFT Nifty (ex-SGX Nifty) indicative quote via Moneycontrol live index page.
    Useful overnight / pre-open cue vs NSE Nifty spot.
    """
    url = "https://www.moneycontrol.com/live-index/gift-nifty?symbol=in%3Bgsx"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://www.moneycontrol.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=18) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    def _field(name: str) -> str | None:
        m = re.search(rf'"{name}"\s*:\s*"([^"]+)"', html, re.I)
        return m.group(1) if m else None

    last = _parse_mc_num(_field("lastprice"))
    if last is None:
        raise RuntimeError("GIFT Nifty quote not found")
    chg = _parse_mc_num(_field("change"))
    chg_pct = _parse_mc_num(_field("percentchange"))
    open_ = _parse_mc_num(_field("open"))
    high = _parse_mc_num(_field("high"))
    low = _parse_mc_num(_field("low"))
    prev = _parse_mc_num(_field("prevclose"))
    updated = _field("lastupdated") or _field("lastupd")
    basis = None
    basis_pct = None
    if nifty_spot is not None and float(nifty_spot) > 0:
        basis = round(last - float(nifty_spot), 2)
        basis_pct = round(basis / float(nifty_spot) * 100.0, 3)
    # GIFT / NSE IX roughly ~21h; soft window for traders (IST)
    now = datetime.now(IST)
    t = now.time()
    # Broad active window excluding the usual mid-day break ~15:40–16:35 IST
    in_break = dtime(15, 40) <= t <= dtime(16, 35)
    session_active = not in_break
    return {
        "ltp": round(last, 2),
        "change": round(chg, 2) if chg is not None else None,
        "change_pct": round(chg_pct, 2) if chg_pct is not None else None,
        "open": round(open_, 2) if open_ is not None else None,
        "high": round(high, 2) if high is not None else None,
        "low": round(low, 2) if low is not None else None,
        "prev_close": round(prev, 2) if prev is not None else None,
        "updated": updated,
        "nifty_spot": round(float(nifty_spot), 2) if nifty_spot is not None else None,
        "basis": basis,
        "basis_pct": basis_pct,
        "session_active": session_active,
        "session_note": "NSE IX ~21h (break ~15:40–16:35 IST)" if session_active else "Likely mid-session break (~15:40–16:35 IST)",
        "source": "Moneycontrol GIFT Nifty (indicative/derived)",
        "note": "Indicative CFD/index feed — may differ from NSE IX futures print.",
    }


def fetch_india_vix() -> dict[str, Any]:
    """India VIX via Yahoo ^INDIAVIX — fear gauge for option buyers."""
    r0 = _yahoo_chart("%5EINDIAVIX", "1d", "10d")
    meta = r0.get("meta") or {}
    ts = r0.get("timestamp") or []
    q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    closes = q.get("close") or []
    rows: list[tuple[int, float]] = []
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        rows.append((int(t), float(c)))
    if not rows:
        raise RuntimeError("No India VIX closes")
    last = rows[-1][1]
    prev = rows[-2][1] if len(rows) >= 2 else None
    # Prefer meta regularMarketPrice when present
    live = meta.get("regularMarketPrice")
    if live is not None:
        last = float(live)
    chg = round(last - prev, 2) if prev is not None else None
    chg_pct = round((last - prev) / prev * 100.0, 2) if prev and prev > 0 else None
    # Elevated fear: VIX high absolute or sharp daily rise
    elevated = last >= 18.0 or (chg is not None and chg >= 1.5)
    return {
        "vix": round(last, 2),
        "vix_prev": round(prev, 2) if prev is not None else None,
        "vix_chg": chg,
        "vix_chg_pct": chg_pct,
        "elevated": elevated,
        "source": "Yahoo ^INDIAVIX",
        "note": "High/rising VIX = fear — option premiums expensive; buyers face crush risk if VIX falls.",
    }


def update_iv_baseline(atm_iv: float | None) -> dict[str, Any]:
    """Session IV baseline vs today's samples (simple IV rank for the day)."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    data: dict[str, Any] = {"date": today, "open_atm_iv": None, "samples": []}
    if IV_BASELINE_PATH.exists():
        try:
            loaded = json.loads(IV_BASELINE_PATH.read_text(encoding="utf-8"))
            if loaded.get("date") == today:
                data = loaded
        except (OSError, json.JSONDecodeError):
            pass
    samples: list[float] = [float(x) for x in (data.get("samples") or []) if x is not None]
    if atm_iv is not None:
        if data.get("open_atm_iv") is None:
            data["open_atm_iv"] = float(atm_iv)
        samples.append(float(atm_iv))
        # Cap history
        samples = samples[-120:]
        data["samples"] = samples
        data["date"] = today
        try:
            IV_BASELINE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass
    open_iv = data.get("open_atm_iv")
    vs_open = round(float(atm_iv) - float(open_iv), 2) if atm_iv is not None and open_iv is not None else None
    day_min = min(samples) if samples else None
    day_max = max(samples) if samples else None
    # Percentile of current vs today's samples
    pctile = None
    if atm_iv is not None and len(samples) >= 3:
        below = sum(1 for x in samples if x <= float(atm_iv))
        pctile = round(below / len(samples) * 100.0, 1)
    rich_vs_open = vs_open is not None and vs_open >= 1.5
    rich_pctile = pctile is not None and pctile >= 85.0
    return {
        "date": today,
        "open_atm_iv": open_iv,
        "atm_iv": atm_iv,
        "vs_open": vs_open,
        "day_min": round(day_min, 2) if day_min is not None else None,
        "day_max": round(day_max, 2) if day_max is not None else None,
        "sample_count": len(samples),
        "day_percentile": pctile,
        "rich_vs_session": rich_vs_open or rich_pctile,
        "note": "IV rank here = vs today's own prints (not 1-year). High percentile = expensive vs this session.",
    }


def build_premium_risk(payload: dict[str, Any]) -> dict[str, Any]:
    """
    ATM premium snapshot + rough risk (ATM delta ≈ 0.5).
    Not a pricing model — spot±50pt premium estimate for option buyers.
    """
    atm = payload.get("atm_strike") or payload.get("centre_strike")
    rows = payload.get("strikes") or []
    row = next((r for r in rows if atm is not None and abs(float(r.get("strike") or 0) - float(atm)) < 0.1), None)
    if not row:
        return {"error": "ATM strike row missing"}
    ce_ltp = row.get("ce_ltp")
    pe_ltp = row.get("pe_ltp")
    ce_chg = row.get("ce_ltp_chg_snap")
    pe_chg = row.get("pe_ltp_chg_snap")
    # Rough ATM deltas
    ce_delta = 0.50
    pe_delta = -0.50
    spot_move = 50.0
    return {
        "strike": float(atm) if atm is not None else None,
        "ce_ltp": ce_ltp,
        "pe_ltp": pe_ltp,
        "ce_ltp_chg_snap": ce_chg,
        "pe_ltp_chg_snap": pe_chg,
        "approx_ce_delta": ce_delta,
        "approx_pe_delta": pe_delta,
        "est_ce_pnl_spot_up_50": round(ce_delta * spot_move, 1),
        "est_ce_pnl_spot_down_50": round(ce_delta * -spot_move, 1),
        "est_pe_pnl_spot_up_50": round(pe_delta * spot_move, 1),
        "est_pe_pnl_spot_down_50": round(pe_delta * -spot_move, 1),
        "note": "Rough ATM Δ≈0.5. Real option P&L also moves with IV (vega) and time (theta). Prefer BAG fills.",
    }


def fetch_nifty_prev_ohlc() -> dict[str, Any]:
    """Previous completed daily bar for NIFTY via Yahoo (^NSEI)."""
    r0 = _yahoo_chart("%5ENSEI", "1d", "10d")
    ts = r0.get("timestamp") or []
    q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    highs = q.get("high") or []
    lows = q.get("low") or []
    closes = q.get("close") or []
    rows: list[dict[str, Any]] = []
    for i, t in enumerate(ts):
        h, l, c = highs[i] if i < len(highs) else None, lows[i] if i < len(lows) else None, closes[i] if i < len(closes) else None
        if h is None or l is None or c is None:
            continue
        rows.append({"ts": int(t), "high": float(h), "low": float(l), "close": float(c)})
    if len(rows) < 2:
        raise RuntimeError("Need at least 2 daily NIFTY bars for CPR")
    # Last row may be today's incomplete bar; use previous completed day
    prev = rows[-2]
    return {
        "date": datetime.fromtimestamp(prev["ts"]).strftime("%Y-%m-%d"),
        "high": prev["high"],
        "low": prev["low"],
        "close": prev["close"],
        "source": "Yahoo ^NSEI previous daily bar",
    }


def fetch_nifty_vwap_context(spot: float | None) -> dict[str, Any]:
    """
    Session VWAP + 5m/15m resistance-support from Yahoo ^NSEI 5-minute bars.
    Break = spot holds above prior 5m (or 15m-window) high / below prior low.
    """
    r0 = _yahoo_chart("%5ENSEI", "5m", "1d")
    ts = r0.get("timestamp") or []
    q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    highs = q.get("high") or []
    lows = q.get("low") or []
    closes = q.get("close") or []
    vols = q.get("volume") or []
    bars: list[dict[str, float]] = []
    for i, t in enumerate(ts):
        h = highs[i] if i < len(highs) else None
        l = lows[i] if i < len(lows) else None
        c = closes[i] if i < len(closes) else None
        v = vols[i] if i < len(vols) else 0
        if h is None or l is None or c is None:
            continue
        bars.append(
            {
                "ts": float(t),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v or 0),
                "hlc3": (float(h) + float(l) + float(c)) / 3.0,
            }
        )
    if len(bars) < 4:
        raise RuntimeError("Need at least 4 five-minute bars for VWAP / S-R")

    # Session VWAP (volume-weighted); fall back to mean HLC3 if volume missing
    num = sum(b["hlc3"] * b["volume"] for b in bars)
    den = sum(b["volume"] for b in bars)
    if den > 0:
        vwap = num / den
        vwap_method = "volume_weighted"
    else:
        vwap = sum(b["hlc3"] for b in bars) / len(bars)
        vwap_method = "hlc3_mean"

    spot_f = float(spot) if spot is not None else float(bars[-1]["close"])
    # Prior windows exclude the latest (forming) bar
    prior = bars[:-1]
    win5 = prior[-12:] if len(prior) >= 12 else prior  # ~1h of 5m
    win15 = prior[-36:] if len(prior) >= 36 else prior  # ~3h ≈ 15m structure proxy
    res_5m = max(b["high"] for b in win5)
    sup_5m = min(b["low"] for b in win5)
    res_15m = max(b["high"] for b in win15)
    sup_15m = min(b["low"] for b in win15)
    prev_close = float(prior[-1]["close"]) if prior else spot_f

    # Clear break: spot beyond level and previous bar was not already through
    broke_res_5m = spot_f > res_5m and prev_close <= res_5m
    broke_sup_5m = spot_f < sup_5m and prev_close >= sup_5m
    broke_res_15m = spot_f > res_15m and prev_close <= res_15m
    broke_sup_15m = spot_f < sup_15m and prev_close >= sup_15m
    # Hold above/below after a break (still useful for CE/PE bias)
    holds_above_res_5m = spot_f > res_5m
    holds_below_sup_5m = spot_f < sup_5m

    above_vwap = spot_f > vwap
    below_vwap = spot_f < vwap
    spot_vs_vwap = round(spot_f - vwap, 2)

    return {
        "vwap": round(vwap, 2),
        "spot": round(spot_f, 2),
        "spot_vs_vwap": spot_vs_vwap,
        "above_vwap": above_vwap,
        "below_vwap": below_vwap,
        "vwap_method": vwap_method,
        "res_5m": round(res_5m, 2),
        "sup_5m": round(sup_5m, 2),
        "res_15m": round(res_15m, 2),
        "sup_15m": round(sup_15m, 2),
        "broke_res_5m": broke_res_5m,
        "broke_sup_5m": broke_sup_5m,
        "broke_res_15m": broke_res_15m,
        "broke_sup_15m": broke_sup_15m,
        "holds_above_res_5m": holds_above_res_5m,
        "holds_below_sup_5m": holds_below_sup_5m,
        "bar_count": len(bars),
        "source": "Yahoo ^NSEI 5m (1d)",
    }


def calc_cpr(high: float, low: float, close: float, spot: float | None) -> dict[str, Any]:
    pivot = (high + low + close) / 3.0
    bc_raw = (high + low) / 2.0
    tc_raw = (pivot - bc_raw) + pivot
    cpr_top = max(tc_raw, bc_raw)
    cpr_bot = min(tc_raw, bc_raw)
    rng = high - low
    r1 = 2.0 * pivot - low
    s1 = 2.0 * pivot - high
    r2 = pivot + rng
    s2 = pivot - rng
    width_pct = ((cpr_top - cpr_bot) / close * 100.0) if close else None
    cpr_type = "Normal"
    if width_pct is not None:
        if width_pct <= 0.35:
            cpr_type = "Narrow"
        elif width_pct >= 0.75:
            cpr_type = "Wide"
    bias = "unknown"
    if spot is not None:
        if spot > cpr_top:
            bias = "Bullish (above CPR)"
        elif spot < cpr_bot:
            bias = "Bearish (below CPR)"
        else:
            bias = "Sideways (inside CPR)"
    return {
        "pivot": round(pivot, 2),
        "tc": round(tc_raw, 2),
        "bc": round(bc_raw, 2),
        "cpr_top": round(cpr_top, 2),
        "cpr_bot": round(cpr_bot, 2),
        "r1": round(r1, 2),
        "s1": round(s1, 2),
        "r2": round(r2, 2),
        "s2": round(s2, 2),
        "pdh": round(high, 2),
        "pdl": round(low, 2),
        "pdc": round(close, 2),
        "width_pct": round(width_pct, 3) if width_pct is not None else None,
        "type": cpr_type,
        "bias": bias,
    }


def decide_signal(
    payload: dict[str, Any],
    cpr: dict[str, Any] | None,
    vwap_ctx: dict[str, Any] | None = None,
    vix: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    premium: dict[str, Any] | None = None,
    iv_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine PCR, OI, CPR, VWAP/S-R, IV/VIX, liquidity, session → call."""
    agg = payload.get("aggregates") or {}
    br = payload.get("bridge") or {}
    spot = payload.get("spot")
    pcr = agg.get("pcr")
    pcr_dir = agg.get("pcr_direction") or "unknown"
    pcr_chg = agg.get("pcr_chg")
    ce_d = agg.get("ce_chg_oi")
    pe_d = agg.get("pe_chg_oi")
    ce_day = agg.get("ce_chg_day_total")
    pe_day = agg.get("pe_chg_day_total")
    # Prefer snapshot deltas; fall back to day change
    ce_chg = ce_d if ce_d is not None else ce_day
    pe_chg = pe_d if pe_d is not None else pe_day
    iv = payload.get("iv") or agg.get("iv") or {}
    if iv_baseline:
        iv = dict(iv)
        iv["baseline"] = iv_baseline
        if iv_baseline.get("rich_vs_session"):
            iv["iv_rich"] = True

    score = 0
    reasons: list[str] = []

    if session:
        reasons.append(f"Session: {session.get('phase')} ({session.get('now_ist')})")

    if not payload.get("usable_for_signals"):
        why = ["OI data stale or unusable — refresh later"]
        if session and not session.get("market_open"):
            why.append(session.get("note") or "Market closed — wait for live session")
        return {
            "call": "WAIT",
            "score": 0,
            "strength": "Blocked",
            "reasons": why,
            "buyers_sellers": "Unknown",
            "vwap_ctx": vwap_ctx,
            "iv": iv,
            "vix": vix,
            "session": session,
            "premium": premium,
        }

    # Buyers / sellers proxy from Change OI
    buyers_sellers = "Mixed"
    if ce_chg is not None and pe_chg is not None:
        if pe_chg > ce_chg and pe_chg > 0:
            score += 2
            buyers_sellers = "Put writers / PE buildup (supports CE)"
            reasons.append(f"PE Change OI {pe_chg:+d} > CE {ce_chg:+d}")
        elif ce_chg > pe_chg and ce_chg > 0:
            score -= 2
            buyers_sellers = "Call writers / CE buildup (supports PE)"
            reasons.append(f"CE Change OI {ce_chg:+d} > PE {pe_chg:+d}")
        elif pe_chg < 0 and ce_chg >= 0:
            score -= 1
            buyers_sellers = "PE unwinding"
            reasons.append("PE OI falling")
        elif ce_chg < 0 and pe_chg >= 0:
            score += 1
            buyers_sellers = "CE unwinding"
            reasons.append("CE OI falling")
        else:
            reasons.append("Change OI mixed / flat")

    # PCR
    if pcr is not None:
        if pcr >= 1.1:
            score += 2
            reasons.append(f"PCR {pcr:.2f} high (PE heavy)")
        elif pcr <= 0.85:
            score -= 2
            reasons.append(f"PCR {pcr:.2f} low (CE heavy)")
        else:
            reasons.append(f"PCR {pcr:.2f} neutral")
    if pcr_dir == "rising":
        score += 1
        reasons.append("PCR rising")
    elif pcr_dir == "falling":
        score -= 1
        reasons.append("PCR falling")

    # CPR structure + price
    if cpr and spot is not None:
        top, bot, piv = cpr["cpr_top"], cpr["cpr_bot"], cpr["pivot"]
        if spot > top:
            score += 2
            reasons.append(f"Spot above CPR Top {top}")
        elif spot < bot:
            score -= 2
            reasons.append(f"Spot below CPR Bot {bot}")
        else:
            reasons.append(f"Spot inside CPR ({bot}–{top}) — balance")
            # Inside CPR: dampen strong calls
            if abs(score) >= 3:
                score = 1 if score > 0 else -1
                reasons.append("Inside CPR — conviction reduced")
        if cpr.get("type") == "Narrow":
            reasons.append("Narrow CPR — breakout day bias")
        elif cpr.get("type") == "Wide":
            reasons.append("Wide CPR — range day caution")
            if abs(score) == 2:
                score = 1 if score > 0 else -1

    # Spot vs previous live file
    prev = None
    try:
        if LIVE_PATH.exists():
            # previous already rotated into prev file by fetch_once; use bridge spot vs option_chain_prev
            from external_option_chain_service import PREV_PATH, load_json

            prev = load_json(PREV_PATH)
    except Exception:
        prev = None
    if prev and prev.get("spot") and spot:
        spot_chg = float(spot) - float(prev["spot"])
        if spot_chg > 25:
            score += 1
            reasons.append(f"Spot up {spot_chg:.1f} vs last poll")
        elif spot_chg < -25:
            score -= 1
            reasons.append(f"Spot down {spot_chg:.1f} vs last poll")

    # Spot vs VWAP + 5m/15m resistance-support breaks
    if vwap_ctx and "vwap" in vwap_ctx:
        vwap = vwap_ctx["vwap"]
        vs = vwap_ctx.get("spot_vs_vwap")
        if vwap_ctx.get("above_vwap"):
            score += 1
            reasons.append(f"Spot above VWAP {vwap} (Δ {vs})")
        elif vwap_ctx.get("below_vwap"):
            score -= 1
            reasons.append(f"Spot below VWAP {vwap} (Δ {vs})")
        if vwap_ctx.get("broke_res_5m") or vwap_ctx.get("broke_res_15m"):
            score += 2
            which = "5m" if vwap_ctx.get("broke_res_5m") else "15m"
            lvl = vwap_ctx.get("res_5m") if which == "5m" else vwap_ctx.get("res_15m")
            reasons.append(f"Broke {which} resistance {lvl}")
        elif vwap_ctx.get("holds_above_res_5m"):
            score += 1
            reasons.append(f"Holds above 5m resistance {vwap_ctx.get('res_5m')}")
        if vwap_ctx.get("broke_sup_5m") or vwap_ctx.get("broke_sup_15m"):
            score -= 2
            which = "5m" if vwap_ctx.get("broke_sup_5m") else "15m"
            lvl = vwap_ctx.get("sup_5m") if which == "5m" else vwap_ctx.get("sup_15m")
            reasons.append(f"Broke {which} support {lvl}")
        elif vwap_ctx.get("holds_below_sup_5m"):
            score -= 1
            reasons.append(f"Holds below 5m support {vwap_ctx.get('sup_5m')}")
        # Dampen CE if below VWAP / PE if above VWAP
        if score >= 2 and vwap_ctx.get("below_vwap"):
            score = 1
            reasons.append("Below VWAP — CE conviction reduced")
        if score <= -2 and vwap_ctx.get("above_vwap"):
            score = -1
            reasons.append("Above VWAP — PE conviction reduced")

    # IV / session IV rank + India VIX — caution for option buyers (not direction)
    iv_warning = None
    if iv.get("atm_iv") is not None:
        base_bit = ""
        bl = iv.get("baseline") or iv_baseline or {}
        if bl.get("vs_open") is not None:
            base_bit = f" vs open {bl.get('vs_open'):+} (pctl {bl.get('day_percentile')})"
        reasons.append(
            f"ATM IV {iv.get('atm_iv')}% (CE {iv.get('ce_iv')} / PE {iv.get('pe_iv')})"
            + (f" Δsnap {iv.get('atm_iv_chg'):+}" if iv.get("atm_iv_chg") is not None else "")
            + base_bit
        )
    if iv.get("iv_rich") or (iv_baseline or {}).get("rich_vs_session"):
        iv_warning = "IV rich vs session — costly premium / crush risk for buyers"
        reasons.append(iv_warning)
        iv["iv_rich"] = True

    if vix and vix.get("vix") is not None:
        reasons.append(
            f"India VIX {vix.get('vix')}"
            + (f" (Δ {vix.get('vix_chg'):+})" if vix.get("vix_chg") is not None else "")
        )
        if vix.get("elevated"):
            reasons.append("VIX elevated — premiums expensive; prefer WAIT / smaller size")
            if not iv_warning:
                iv_warning = "VIX elevated — option buyer caution"
            iv["iv_rich"] = True

    if premium and premium.get("ce_ltp") is not None:
        reasons.append(
            f"ATM premium CE {premium.get('ce_ltp')} / PE {premium.get('pe_ltp')} "
            f"(≈Δ0.5 → CE ~{premium.get('est_ce_pnl_spot_up_50')} if spot +50)"
        )

    if score >= 3:

        call, strength = "BUY CE", "Strong"
    elif score >= 2:
        call, strength = "BUY CE", "Moderate"
    elif score <= -3:
        call, strength = "BUY PE", "Strong"
    elif score <= -2:
        call, strength = "BUY PE", "Moderate"
    else:
        call, strength = "WAIT", "Weak / mixed"

    # IV rich: cut Moderate buys to WAIT; Strong → Moderate
    if iv.get("iv_rich") and call in ("BUY CE", "BUY PE"):
        if strength.startswith("Moderate") or strength.startswith("Weak"):
            reasons.append(f"{call} blocked by rich IV — wait for cooler premium")
            call, strength = "WAIT", "IV rich"
            score = 0
        elif strength == "Strong":
            strength = "Moderate (IV rich)"
            reasons.append("Strong setup but IV elevated — size down / prefer BAG")

    # Bid vs offer + spread: wide/thin books (esp. ITM) → costly entry/exit
    liq = payload.get("liquidity") or (payload.get("aggregates") or {}).get("liquidity") or {}
    atm_ce = liq.get("atm_ce") or {}
    atm_pe = liq.get("atm_pe") or {}
    itm_ce_list = [q for q in (liq.get("itm_ce") or []) if q]
    itm_pe_list = [q for q in (liq.get("itm_pe") or []) if q]
    if not itm_ce_list:
        itm_ce_list = [q for q in (liq.get("nearest_itm_ce"), liq.get("second_itm_ce")) if q]
    if not itm_pe_list:
        itm_pe_list = [q for q in (liq.get("nearest_itm_pe"), liq.get("second_itm_pe")) if q]
    itm_ce = itm_ce_list[0] if itm_ce_list else {}
    itm_pe = itm_pe_list[0] if itm_pe_list else {}
    liquidity_warning = None

    def _liq_note(label: str, q: dict[str, Any]) -> str | None:
        if not q:
            return None
        spr = q.get("spread")
        pct = q.get("spread_pct")
        bq, aq = q.get("bid_qty"), q.get("ask_qty")
        grade = q.get("liquidity") or "unknown"
        imb = q.get("depth_imbalance")
        side_hint = ""
        if imb is not None:
            if imb > 0.25:
                side_hint = "; bid qty > offer (more resting buys)"
            elif imb < -0.25:
                side_hint = "; offer qty > bid (more resting sells)"
        base = (
            f"{label} K{q.get('strike')}: bid {q.get('bid')}×{bq} vs ask {q.get('ask')}×{aq} "
            f"spread {spr} ({pct}% ) [{grade}]{side_hint}"
        )
        return base

    for label, q in (("ATM CE", atm_ce), ("ATM PE", atm_pe)):
        note = _liq_note(label, q)
        if note:
            reasons.append(note)
    for i, q in enumerate(itm_ce_list, 1):
        note = _liq_note(f"ITM CE #{i}", q)
        if note and q.get("liquidity") in ("thin", "poor", "unknown"):
            reasons.append(note)
    for i, q in enumerate(itm_pe_list, 1):
        note = _liq_note(f"ITM PE #{i}", q)
        if note and q.get("liquidity") in ("thin", "poor", "unknown"):
            reasons.append(note)

    target = atm_ce if call == "BUY CE" else atm_pe if call == "BUY PE" else None
    itm_targets = itm_ce_list if call == "BUY CE" else itm_pe_list if call == "BUY PE" else []
    if call in ("BUY CE", "BUY PE") and target:
        grade = target.get("liquidity")
        if grade == "poor":
            liquidity_warning = (
                f"{call} blocked: ATM spread {target.get('spread')} "
                f"({target.get('spread_pct')}%) / thin depth — costly entry-exit"
            )
            reasons.append(liquidity_warning)
            call, strength = "WAIT", "Liquidity poor"
            score = 0
        elif grade == "thin":
            liquidity_warning = (
                f"{call} caution: ATM bid–ask thin — spread {target.get('spread')} "
                f"({target.get('spread_pct')}%), L1 {target.get('bid_qty')}/{target.get('ask_qty')}"
            )
            reasons.append(liquidity_warning)
            if strength == "Strong":
                strength = "Moderate (thin book)"
            else:
                strength = "Weak (thin book)"
        poor_itms = [q for q in itm_targets if q.get("liquidity") == "poor"]
        if poor_itms and call != "WAIT":
            strikes = ", ".join(str(int(q["strike"])) for q in poor_itms if q.get("strike") is not None)
            reasons.append(f"ITM strike(s) {strikes} poor liquidity — avoid chasing ITM")

    return {
        "call": call,
        "score": score,
        "strength": strength,
        "reasons": reasons,
        "buyers_sellers": buyers_sellers,
        "pcr": pcr,
        "pcr_chg": pcr_chg,
        "pcr_direction": pcr_dir,
        "ce_chg": ce_chg,
        "pe_chg": pe_chg,
        "liquidity": liq,
        "liquidity_warning": liquidity_warning,
        "vwap_ctx": vwap_ctx,
        "iv": iv,
        "iv_warning": iv_warning,
        "vix": vix,
        "session": session,
        "premium": premium,
    }


def build_snapshot(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    One-shot snapshot for local server or Vercel serverless.
    Always fetches fresh data (no background poll on Vercel).
    """
    c = dict(CFG)
    if cfg:
        c.update(cfg)
    err = None
    payload = None
    cpr = None
    vwap_ctx = None
    vix = None
    session = None
    gift_nifty = None
    premium = None
    iv_baseline = None
    signal = None
    try:
        session = ist_session_status()
        payload = fetch_once(
            c.get("expiry"),
            int(c.get("range") or 3),
            int(c.get("step") or 50),
            int(c.get("max_stale") or 300),
            c.get("strike"),
        )
        spot = payload.get("spot")
        try:
            ohlc = fetch_nifty_prev_ohlc()
            cpr = calc_cpr(ohlc["high"], ohlc["low"], ohlc["close"], float(spot) if spot else None)
            cpr["prev_date"] = ohlc["date"]
            cpr["source"] = ohlc["source"]
        except Exception as cpr_exc:  # noqa: BLE001
            cpr = {"error": str(cpr_exc)}
        try:
            vwap_ctx = fetch_nifty_vwap_context(float(spot) if spot else None)
        except Exception as vwap_exc:  # noqa: BLE001
            vwap_ctx = {"error": str(vwap_exc)}
        try:
            gift_nifty = fetch_gift_nifty(float(spot) if spot else None)
        except Exception as gift_exc:  # noqa: BLE001
            gift_nifty = {"error": str(gift_exc)}
        try:
            vix = fetch_india_vix()
        except Exception as vix_exc:  # noqa: BLE001
            vix = {"error": str(vix_exc)}
        try:
            premium = build_premium_risk(payload)
        except Exception as prem_exc:  # noqa: BLE001
            premium = {"error": str(prem_exc)}
        atm_iv = (payload.get("iv") or {}).get("atm_iv")
        try:
            iv_baseline = update_iv_baseline(float(atm_iv) if atm_iv is not None else None)
        except Exception:
            iv_baseline = None
        signal = decide_signal(
            payload,
            cpr if cpr and "cpr_top" in cpr else None,
            vwap_ctx if vwap_ctx and "vwap" in vwap_ctx else None,
            vix if vix and vix.get("vix") is not None else None,
            session,
            premium if premium and "ce_ltp" in premium else None,
            iv_baseline,
        )
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        if session is None:
            try:
                session = ist_session_status()
            except Exception:
                session = None
    return {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "error": err,
        "payload": payload,
        "cpr": cpr,
        "vwap_ctx": vwap_ctx,
        "vix": vix,
        "session": session,
        "gift_nifty": gift_nifty,
        "premium": premium,
        "signal": signal,
        "app_version": APP_VERSION,
        "runtime": "vercel" if os.environ.get("VERCEL") else "local",
    }


def refresh_once() -> None:
    snap = build_snapshot()
    with STATE_LOCK:
        STATE.update(snap)


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>MyDesk</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml"/>
<link rel="apple-touch-icon" href="/favicon.svg"/>
<style>
  :root {
    --bg: #0f1419;
    --panel: #1a222c;
    --line: #2c3845;
    --text: #e8eef4;
    --muted: #8b9aab;
    --green: #1faa6e;
    --red: #e04a4a;
    --amber: #d4a017;
    --ce: #1faa6e;
    --pe: #e04a4a;
    --wait: #c9a227;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
    background: radial-gradient(1200px 600px at 10% -10%, #1a2836 0%, var(--bg) 55%);
    color: var(--text); min-height: 100vh;
  }
  header {
    display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between;
    padding: 18px 24px; border-bottom: 1px solid var(--line);
  }
  header h1 { margin: 0; font-size: 1.25rem; font-weight: 650; letter-spacing: 0.02em; }
  header .meta { color: var(--muted); font-size: 0.85rem; }
  .btn {
    background: #243040; color: var(--text); border: 1px solid var(--line);
    padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: 600;
  }
  .btn:hover { background: #2d3d50; }
  main { padding: 20px 24px 40px; display: grid; gap: 16px; }
  .grid { display: grid; gap: 16px; grid-template-columns: repeat(12, 1fr); }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px;
  }
  .card h2 { margin: 0 0 12px; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); font-weight: 600; }
  .span-4 { grid-column: span 4; } .span-6 { grid-column: span 6; } .span-8 { grid-column: span 8; } .span-12 { grid-column: span 12; }
  @media (max-width: 980px) {
    .span-4, .span-6, .span-8 { grid-column: span 12; }
  }
  .call {
    font-size: 2.4rem; font-weight: 750; line-height: 1.1; margin: 4px 0 8px;
  }
  .call.ce { color: var(--ce); } .call.pe { color: var(--pe); } .call.wait { color: var(--wait); }
  .kv { display: grid; grid-template-columns: 1fr auto; gap: 8px 12px; font-size: 0.95rem; }
  .kv span:first-child { color: var(--muted); }
  .pos { color: var(--green); font-weight: 650; }
  .neg { color: var(--red); font-weight: 650; }
  .neu { color: var(--muted); }
  table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
  th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: right; }
  th { color: var(--muted); font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; }
  td:first-child, th:first-child { text-align: left; }
  tr.centre { background: rgba(212, 160, 23, 0.12); }
  ul.reasons { margin: 0; padding-left: 18px; color: var(--text); }
  ul.reasons li { margin: 4px 0; color: #c5d0db; }
  .err { background: #3a1c1c; border-color: #7a3030; color: #ffb4b4; padding: 12px 14px; border-radius: 8px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.75rem; font-weight: 650; }
  .badge.ok { background: rgba(31,170,110,.2); color: var(--green); }
  .badge.bad { background: rgba(224,74,74,.2); color: var(--red); }
  .badge.warn { background: rgba(212,160,23,.2); color: var(--amber); }
  .liq-ok { color: var(--green); font-weight: 650; }
  .liq-thin { color: var(--amber); font-weight: 650; }
  .liq-poor { color: var(--red); font-weight: 650; }
  .liq-unknown { color: var(--muted); }
  .card-head {
    display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 12px;
  }
  .card-head h2 { margin: 0; }
  .why-link {
    flex: 0 0 auto; border: none; background: none; padding: 0;
    color: #9ec9ff; font-size: 0.8rem; font-weight: 650; cursor: pointer;
    text-decoration: underline; text-underline-offset: 2px;
  }
  .why-link:hover { color: #c5ddff; }
  .modal-backdrop {
    display: none; position: fixed; inset: 0; z-index: 50;
    background: rgba(6, 10, 14, 0.72); align-items: center; justify-content: center;
    padding: 20px;
  }
  .modal-backdrop.open { display: flex; }
  .modal {
    width: min(480px, 100%); background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; padding: 18px 18px 14px; box-shadow: 0 16px 40px rgba(0,0,0,.45);
  }
  .modal-head {
    display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px;
  }
  .modal-head h3 { margin: 0; font-size: 1rem; font-weight: 700; }
  .modal-close {
    border: 1px solid var(--line); background: #243040; color: var(--text);
    border-radius: 6px; padding: 4px 10px; cursor: pointer; font-weight: 650;
  }
  .modal-close:hover { background: #2d3d50; }
</style>
</head>
<body>
<header>
  <div>
    <h1>MyDesk <span style="color:var(--muted);font-weight:600">monitor</span></h1>
    <div class="meta" id="meta">Click Refresh now for live data (manual only)…</div>
  </div>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <span class="meta" id="userLabel"></span>
    <a class="btn" id="adminLogsBtn" href="/admin-logs.html" style="display:none">User logs</a>
    <button class="btn" id="refreshBtn" type="button">Refresh now</button>
    <button class="btn" id="logoutBtn" type="button">Log out</button>
  </div>
</header>
<main>
  <div id="error" class="err" style="display:none"></div>
  <div class="grid">
    <section class="card span-4">
      <div class="card-head">
        <h2>Call suggestion</h2>
        <button class="why-link" id="whyCallBtn" type="button">why</button>
      </div>
      <div class="call wait" id="call">WAIT</div>
      <div class="kv">
        <span>Strength</span><span id="strength">—</span>
        <span>Score</span><span id="score">—</span>
        <span>Buyers / sellers</span><span id="flow">—</span>
      </div>
    </section>
    <section class="card span-4">
      <h2>PCR (ATM ± band)</h2>
      <div class="kv">
        <span>PCR</span><span id="pcr">—</span>
        <span>PCR change</span><span id="pcrChg">—</span>
        <span>Direction</span><span id="pcrDir">—</span>
        <span>CE Change OI</span><span id="ceChg">—</span>
        <span>PE Change OI</span><span id="peChg">—</span>
      </div>
    </section>
    <section class="card span-4">
      <h2>CPR (prev day)</h2>
      <div class="kv">
        <span>Bias</span><span id="cprBias">—</span>
        <span>Type</span><span id="cprType">—</span>
        <span>Top / Pivot / Bot</span><span id="cprLevels">—</span>
        <span>R1 / S1</span><span id="cprRS">—</span>
        <span>Prev day</span><span id="cprDate">—</span>
      </div>
    </section>
    <section class="card span-6">
      <h2>Spot / VWAP / 5m–15m S-R</h2>
      <div class="kv">
        <span>Spot vs VWAP</span><span id="vwapDelta">—</span>
        <span>VWAP</span><span id="vwapVal">—</span>
        <span>5m Res / Sup</span><span id="sr5">—</span>
        <span>15m Res / Sup</span><span id="sr15">—</span>
        <span>Break / hold</span><span id="srBreak">—</span>
      </div>
    </section>
    <section class="card span-6">
      <h2>Implied volatility (ATM)</h2>
      <div class="kv">
        <span>ATM IV</span><span id="atmIv">—</span>
        <span>CE IV / PE IV</span><span id="sideIv">—</span>
        <span>IV change / vs open</span><span id="ivChg">—</span>
        <span>Day IV percentile</span><span id="ivPct">—</span>
        <span>IV warning</span><span id="ivWarn">—</span>
      </div>
    </section>
    <section class="card span-4">
      <h2>GIFT Nifty</h2>
      <div class="kv">
        <span>LTP</span><span id="giftLtp">—</span>
        <span>Change</span><span id="giftChg">—</span>
        <span>Open / High / Low</span><span id="giftOhl">—</span>
        <span>vs Nifty spot</span><span id="giftBasis">—</span>
        <span>Session</span><span id="giftSess">—</span>
      </div>
    </section>
    <section class="card span-4">
      <h2>India VIX</h2>
      <div class="kv">
        <span>VIX</span><span id="vixVal">—</span>
        <span>Change</span><span id="vixChg">—</span>
        <span>Elevated</span><span id="vixElev">—</span>
      </div>
    </section>
    <section class="card span-4">
      <h2>Premium risk (ATM ≈Δ0.5)</h2>
      <div class="kv">
        <span>CE / PE LTP</span><span id="premLtp">—</span>
        <span>LTP Δsnap</span><span id="premChg">—</span>
        <span>If spot +50</span><span id="premUp">—</span>
        <span>If spot −50</span><span id="premDn">—</span>
      </div>
    </section>
    <section class="card span-12">
      <h2>Strike band — OI &amp; Change OI</h2>
      <div style="overflow-x:auto">
        <table>
          <thead>
            <tr>
              <th>Strike</th>
              <th>CE OI</th>
              <th>CE Δday</th>
              <th>CE Δsnap</th>
              <th>PE OI</th>
              <th>PE Δday</th>
              <th>PE Δsnap</th>
              <th>PCR</th>
            </tr>
          </thead>
          <tbody id="strikes"></tbody>
        </table>
      </div>
    </section>
    <section class="card span-12">
      <h2>ATM / ITM liquidity — strike, order block, BAG / FAG</h2>
      <p style="margin:0 0 12px;color:var(--muted);font-size:0.85rem" id="liqNote"></p>
      <div style="overflow-x:auto">
        <table>
          <thead>
            <tr>
              <th>Leg</th>
              <th>Strike</th>
              <th>Mny</th>
              <th>Bid×qty</th>
              <th>Ask×qty</th>
              <th>Sprd</th>
              <th>Liq</th>
              <th>Order block</th>
              <th>BAG</th>
              <th>FAG</th>
              <th>Prefer</th>
            </tr>
          </thead>
          <tbody id="liqKv"></tbody>
        </table>
      </div>
      <div class="kv" style="margin-top:14px;max-width:720px">
        <span>ATM CE depth (bid/ask qty)</span><span id="ceDepth">—</span>
        <span>ATM CE spread</span><span id="ceSpread">—</span>
        <span>ATM PE depth (bid/ask qty)</span><span id="peDepth">—</span>
        <span>ATM PE spread</span><span id="peSpread">—</span>
        <span>Liquidity warning</span><span id="liqWarn">—</span>
      </div>
    </section>
    <section class="card span-12">
      <h2>Strike band — bid / ask / spread</h2>
      <div style="overflow-x:auto">
        <table>
          <thead>
            <tr>
              <th>Strike</th>
              <th>CE mny</th>
              <th>CE bid×qty</th>
              <th>CE ask×qty</th>
              <th>CE sprd</th>
              <th>CE block</th>
              <th>CE BAG</th>
              <th>CE FAG</th>
              <th>CE liq</th>
              <th>PE mny</th>
              <th>PE bid×qty</th>
              <th>PE ask×qty</th>
              <th>PE sprd</th>
              <th>PE block</th>
              <th>PE BAG</th>
              <th>PE FAG</th>
              <th>PE liq</th>
            </tr>
          </thead>
          <tbody id="liqStrikes"></tbody>
        </table>
      </div>
    </section>
  </div>
</main>
<div class="modal-backdrop" id="whyModal" role="dialog" aria-modal="true" aria-labelledby="whyModalTitle">
  <div class="modal">
    <div class="modal-head">
      <h3 id="whyModalTitle">Why this call</h3>
      <button class="modal-close" id="whyModalClose" type="button">Close</button>
    </div>
    <ul class="reasons" id="reasonsPopup"><li>Waiting for data…</li></ul>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
function chgClass(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return 'neu';
  const n = Number(v);
  if (n > 0) return 'pos';
  if (n < 0) return 'neg';
  return 'neu';
}
function fmtChg(v) {
  if (v === null || v === undefined) return 'n/a';
  const n = Number(v);
  if (Number.isNaN(n)) return 'n/a';
  return (n > 0 ? '+' : '') + n.toLocaleString('en-IN');
}
function fmtNum(v) {
  if (v === null || v === undefined) return '—';
  return Number(v).toLocaleString('en-IN');
}
function fmtPct(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return (n > 0 ? '+' : '') + n.toFixed(3);
}
function fmtPx(v) {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(2);
}
function liqClass(g) {
  if (g === 'ok') return 'liq-ok';
  if (g === 'thin') return 'liq-thin';
  if (g === 'poor') return 'liq-poor';
  return 'liq-unknown';
}
function fmtDepth(bid, bq, ask, aq) {
  return `${fmtPx(bid)}×${fmtNum(bq)} / ${fmtPx(ask)}×${fmtNum(aq)}`;
}
function fmtSpread(spr, pct, grade) {
  if (spr === null || spr === undefined) return '—';
  const g = grade || '';
  return `<span class="${liqClass(g)}">${fmtPx(spr)} (${pct != null ? Number(pct).toFixed(2) + '%' : '—'}) ${g}</span>`;
}
async function ensureAuth() {
  const r = await fetch('/api/me', { credentials: 'same-origin' });
  if (r.status === 401) {
    location.href = '/login.html';
    return null;
  }
  const d = await r.json();
  if (!d.ok) {
    location.href = '/login.html';
    return null;
  }
  $('userLabel').textContent = d.username || '';
  if (d.is_admin) $('adminLogsBtn').style.display = 'inline-block';
  return d;
}
async function load() {
  try {
    let d = window.__applyState || null;
    window.__applyState = null;
    if (!d) {
      const r = await fetch('/api/state', { credentials: 'same-origin' });
      if (r.status === 401) { location.href = '/login.html'; return; }
      d = await r.json();
    }
    if (d.error) {
      $('error').style.display = 'block';
      $('error').textContent = d.error;
    } else {
      $('error').style.display = 'none';
    }
    const p = d.payload || {};
    const agg = p.aggregates || {};
    const sig = d.signal || {};
    const cpr = d.cpr || {};
    const call = sig.call || 'WAIT';
    $('call').textContent = call;
    $('call').className = 'call ' + (call === 'BUY CE' ? 'ce' : call === 'BUY PE' ? 'pe' : 'wait');
    $('strength').textContent = sig.strength || '—';
    $('score').textContent = sig.score ?? '—';
    $('flow').textContent = sig.buyers_sellers || '—';

    const pcr = agg.pcr ?? sig.pcr;
    $('pcr').textContent = pcr != null ? Number(pcr).toFixed(3) : '—';
    const pcrChg = agg.pcr_chg ?? sig.pcr_chg;
    $('pcrChg').textContent = pcrChg != null ? fmtPct(pcrChg) : 'n/a';
    $('pcrChg').className = chgClass(pcrChg);
    $('pcrDir').textContent = agg.pcr_direction || sig.pcr_direction || '—';

    const ceChg = sig.ce_chg ?? agg.ce_chg_oi ?? agg.ce_chg_day_total;
    const peChg = sig.pe_chg ?? agg.pe_chg_oi ?? agg.pe_chg_day_total;
    $('ceChg').textContent = fmtChg(ceChg);
    $('ceChg').className = chgClass(ceChg);
    $('peChg').textContent = fmtChg(peChg);
    $('peChg').className = chgClass(peChg);

    if (cpr.error) {
      $('cprBias').textContent = 'CPR unavailable';
      $('cprType').textContent = cpr.error;
      $('cprLevels').textContent = '—';
      $('cprRS').textContent = '—';
      $('cprDate').textContent = '—';
    } else {
      $('cprBias').textContent = cpr.bias || '—';
      $('cprType').textContent = (cpr.type || '—') + (cpr.width_pct != null ? ` (${cpr.width_pct}%)` : '');
      $('cprLevels').textContent = `${cpr.cpr_top} / ${cpr.pivot} / ${cpr.cpr_bot}`;
      $('cprRS').textContent = `${cpr.r1} / ${cpr.s1}`;
      $('cprDate').textContent = cpr.prev_date || '—';
    }

    const vwap = d.vwap_ctx || sig.vwap_ctx || {};
    if (vwap.error) {
      $('vwapDelta').textContent = 'VWAP unavailable';
      $('vwapDelta').className = 'neu';
      $('vwapVal').textContent = vwap.error;
      $('sr5').textContent = '—';
      $('sr15').textContent = '—';
      $('srBreak').textContent = '—';
    } else {
      const vs = vwap.spot_vs_vwap;
      $('vwapDelta').textContent = vs != null ? ((vs > 0 ? '+' : '') + Number(vs).toFixed(2) + (vwap.above_vwap ? ' (above)' : vwap.below_vwap ? ' (below)' : '')) : '—';
      $('vwapDelta').className = vs > 0 ? 'pos' : vs < 0 ? 'neg' : 'neu';
      $('vwapVal').textContent = vwap.vwap != null ? Number(vwap.vwap).toFixed(2) : '—';
      $('sr5').textContent = `${vwap.res_5m ?? '—'} / ${vwap.sup_5m ?? '—'}`;
      $('sr15').textContent = `${vwap.res_15m ?? '—'} / ${vwap.sup_15m ?? '—'}`;
      const br = [];
      if (vwap.broke_res_5m) br.push('Broke 5m res');
      if (vwap.broke_res_15m) br.push('Broke 15m res');
      if (vwap.broke_sup_5m) br.push('Broke 5m sup');
      if (vwap.broke_sup_15m) br.push('Broke 15m sup');
      if (vwap.holds_above_res_5m && !vwap.broke_res_5m) br.push('Holds > 5m res');
      if (vwap.holds_below_sup_5m && !vwap.broke_sup_5m) br.push('Holds < 5m sup');
      $('srBreak').textContent = br.length ? br.join(' · ') : 'None';
      $('srBreak').className = (vwap.broke_res_5m || vwap.broke_res_15m) ? 'pos' : (vwap.broke_sup_5m || vwap.broke_sup_15m) ? 'neg' : 'neu';
    }

    const iv = (p.iv) || (agg.iv) || sig.iv || {};
    const ivBase = iv.baseline || {};
    $('atmIv').textContent = iv.atm_iv != null ? Number(iv.atm_iv).toFixed(2) + '%' : '—';
    $('atmIv').className = iv.iv_rich ? 'liq-poor' : 'neu';
    $('sideIv').textContent = `${iv.ce_iv != null ? Number(iv.ce_iv).toFixed(2) + '%' : '—'} / ${iv.pe_iv != null ? Number(iv.pe_iv).toFixed(2) + '%' : '—'}`;
    const ivChg = iv.atm_iv_chg;
    const vsOpen = ivBase.vs_open;
    $('ivChg').textContent = [
      ivChg != null ? ('snap ' + (ivChg > 0 ? '+' : '') + Number(ivChg).toFixed(2)) : null,
      vsOpen != null ? ('open ' + (vsOpen > 0 ? '+' : '') + Number(vsOpen).toFixed(2)) : null
    ].filter(Boolean).join(' · ') || 'n/a';
    $('ivChg').className = chgClass(ivChg != null ? ivChg : vsOpen);
    $('ivPct').textContent = ivBase.day_percentile != null ? Number(ivBase.day_percentile).toFixed(0) + '% of today' : '—';
    const ivWarn = sig.iv_warning || (iv.iv_rich ? 'IV rich — costly premium' : null);
    $('ivWarn').textContent = ivWarn || 'None';
    $('ivWarn').className = ivWarn ? 'liq-poor' : 'neu';

    const gift = d.gift_nifty || {};
    if (gift.error) {
      $('giftLtp').textContent = 'unavailable';
      $('giftChg').textContent = gift.error;
      $('giftOhl').textContent = '—';
      $('giftBasis').textContent = '—';
      $('giftSess').textContent = '—';
    } else {
      $('giftLtp').textContent = gift.ltp != null ? Number(gift.ltp).toFixed(2) : '—';
      $('giftChg').textContent = gift.change != null
        ? ((gift.change > 0 ? '+' : '') + Number(gift.change).toFixed(2) + (gift.change_pct != null ? ` (${Number(gift.change_pct).toFixed(2)}%)` : ''))
        : '—';
      $('giftChg').className = chgClass(gift.change);
      $('giftOhl').textContent = `${gift.open ?? '—'} / ${gift.high ?? '—'} / ${gift.low ?? '—'}`;
      if (gift.basis != null) {
        $('giftBasis').textContent = (gift.basis > 0 ? '+' : '') + Number(gift.basis).toFixed(2)
          + (gift.basis_pct != null ? ` (${Number(gift.basis_pct).toFixed(3)}%)` : '');
        $('giftBasis').className = chgClass(gift.basis);
      } else {
        $('giftBasis').textContent = '—';
        $('giftBasis').className = 'neu';
      }
      $('giftSess').textContent = gift.session_note || (gift.updated ? ('upd ' + gift.updated) : '—');
      $('giftSess').className = gift.session_active ? 'pos' : 'liq-thin';
    }

    const vix = d.vix || sig.vix || {};
    if (vix.error) {
      $('vixVal').textContent = 'unavailable';
      $('vixChg').textContent = vix.error;
      $('vixElev').textContent = '—';
    } else {
      $('vixVal').textContent = vix.vix != null ? Number(vix.vix).toFixed(2) : '—';
      $('vixVal').className = vix.elevated ? 'liq-poor' : 'neu';
      $('vixChg').textContent = vix.vix_chg != null ? ((vix.vix_chg > 0 ? '+' : '') + Number(vix.vix_chg).toFixed(2) + (vix.vix_chg_pct != null ? ` (${vix.vix_chg_pct}%)` : '')) : '—';
      $('vixChg').className = chgClass(vix.vix_chg);
      $('vixElev').textContent = vix.elevated ? 'Yes — caution' : 'No';
      $('vixElev').className = vix.elevated ? 'liq-poor' : 'pos';
    }

    const prem = d.premium || sig.premium || {};
    if (prem.error) {
      $('premLtp').textContent = '—';
      $('premChg').textContent = prem.error;
      $('premUp').textContent = '—';
      $('premDn').textContent = '—';
    } else {
      $('premLtp').textContent = `${prem.ce_ltp ?? '—'} / ${prem.pe_ltp ?? '—'}`;
      $('premChg').innerHTML = `<span class="${chgClass(prem.ce_ltp_chg_snap)}">${fmtChg(prem.ce_ltp_chg_snap)}</span> / <span class="${chgClass(prem.pe_ltp_chg_snap)}">${fmtChg(prem.pe_ltp_chg_snap)}</span>`;
      $('premUp').textContent = `CE ~${prem.est_ce_pnl_spot_up_50} · PE ~${prem.est_pe_pnl_spot_up_50}`;
      $('premDn').textContent = `CE ~${prem.est_ce_pnl_spot_down_50} · PE ~${prem.est_pe_pnl_spot_down_50}`;
    }

    const reasons = sig.reasons || [];
    $('reasonsPopup').innerHTML = reasons.length
      ? reasons.map(x => `<li>${x}</li>`).join('')
      : '<li>No reasons yet</li>';

    const centre = p.centre_strike ?? p.atm_strike;
    const rows = p.strikes || [];
    $('strikes').innerHTML = rows.map(r => {
      const k = r.strike;
      const isC = centre != null && Math.abs(Number(k) - Number(centre)) < 0.1;
      const pcrS = (r.ce_oi > 0 && r.pe_oi != null) ? (r.pe_oi / r.ce_oi).toFixed(2) : 'n/a';
      return `<tr class="${isC ? 'centre' : ''}">
        <td>${isC ? '★ ' : ''}${k}</td>
        <td>${fmtNum(r.ce_oi)}</td>
        <td class="${chgClass(r.ce_chg_day)}">${fmtChg(r.ce_chg_day)}</td>
        <td class="${chgClass(r.ce_chg_snap)}">${fmtChg(r.ce_chg_snap)}</td>
        <td>${fmtNum(r.pe_oi)}</td>
        <td class="${chgClass(r.pe_chg_day)}">${fmtChg(r.pe_chg_day)}</td>
        <td class="${chgClass(r.pe_chg_snap)}">${fmtChg(r.pe_chg_snap)}</td>
        <td>${pcrS}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="8">No strike data</td></tr>';

    const liq = p.liquidity || (agg.liquidity) || sig.liquidity || {};
    const atmCe = liq.atm_ce || {};
    const atmPe = liq.atm_pe || {};
    let itmCeList = Array.isArray(liq.itm_ce) ? liq.itm_ce.filter(Boolean) : [];
    let itmPeList = Array.isArray(liq.itm_pe) ? liq.itm_pe.filter(Boolean) : [];
    if (!itmCeList.length) itmCeList = [liq.nearest_itm_ce, liq.second_itm_ce].filter(Boolean);
    if (!itmPeList.length) itmPeList = [liq.nearest_itm_pe, liq.second_itm_pe].filter(Boolean);
    $('ceDepth').textContent = `${fmtNum(atmCe.bid_qty)} / ${fmtNum(atmCe.ask_qty)}`;
    $('peDepth').textContent = `${fmtNum(atmPe.bid_qty)} / ${fmtNum(atmPe.ask_qty)}`;
    $('ceSpread').innerHTML = fmtSpread(atmCe.spread, atmCe.spread_pct, atmCe.liquidity);
    $('peSpread').innerHTML = fmtSpread(atmPe.spread, atmPe.spread_pct, atmPe.liquidity);
    const warn = sig.liquidity_warning;
    $('liqWarn').textContent = warn || 'None';
    $('liqWarn').className = warn ? 'liq-poor' : 'neu';

    function preferClass(p) {
      if (p === 'BAG') return 'liq-ok';
      if (p === 'FAG') return 'liq-thin';
      if (p === 'SKIP') return 'liq-poor';
      return 'neu';
    }
    function liqOrderRow(label, q) {
      if (!q || q.strike == null) return '';
      const prefer = q.order_prefer || '—';
      const title = q.order_note ? ` title="${String(q.order_note).replace(/"/g, '&quot;')}"` : '';
      return `<tr${title}>
        <td>${label}</td>
        <td><strong>${Number(q.strike)}</strong></td>
        <td>${q.moneyness || '—'}</td>
        <td>${fmtPx(q.bid)}×${fmtNum(q.bid_qty)}</td>
        <td>${fmtPx(q.ask)}×${fmtNum(q.ask_qty)}</td>
        <td>${fmtPx(q.spread)}${q.spread_pct != null ? ' (' + Number(q.spread_pct).toFixed(2) + '%)' : ''}</td>
        <td class="${liqClass(q.liquidity)}">${q.liquidity || '—'}</td>
        <td>${q.order_block || (fmtPx(q.bid) + ' – ' + fmtPx(q.ask))}</td>
        <td class="liq-ok">${q.bag != null ? fmtPx(q.bag) : '—'}</td>
        <td class="liq-thin">${q.fag != null ? fmtPx(q.fag) : '—'}</td>
        <td class="${preferClass(prefer)}">${prefer}</td>
      </tr>`;
    }
    const liqBody = [];
    liqBody.push(liqOrderRow('ATM CE', atmCe));
    liqBody.push(liqOrderRow('ATM PE', atmPe));
    itmCeList.forEach((q, i) => liqBody.push(liqOrderRow(`ITM CE #${i + 1}`, q)));
    itmPeList.forEach((q, i) => liqBody.push(liqOrderRow(`ITM PE #${i + 1}`, q)));
    $('liqKv').innerHTML = liqBody.join('') || '<tr><td colspan="11">No liquidity data</td></tr>';
    $('liqNote').textContent = liq.note || 'Order block = bid–ask zone. BAG = Buy At Gap (limit near mid). FAG = Fill At Ask. Prefer BAG; FAG only if urgent.';

    $('liqStrikes').innerHTML = rows.map(r => {
      const k = r.strike;
      const isC = centre != null && Math.abs(Number(k) - Number(centre)) < 0.1;
      return `<tr class="${isC ? 'centre' : ''}">
        <td>${isC ? '★ ' : ''}${k}</td>
        <td>${r.ce_moneyness || '—'}</td>
        <td>${fmtPx(r.ce_bid)}×${fmtNum(r.ce_bid_qty)}</td>
        <td>${fmtPx(r.ce_ask)}×${fmtNum(r.ce_ask_qty)}</td>
        <td>${fmtPx(r.ce_spread)}</td>
        <td>${r.ce_order_block || '—'}</td>
        <td class="liq-ok">${r.ce_bag != null ? fmtPx(r.ce_bag) : '—'}</td>
        <td class="liq-thin">${r.ce_fag != null ? fmtPx(r.ce_fag) : '—'}</td>
        <td class="${liqClass(r.ce_liquidity)}">${r.ce_liquidity || '—'}</td>
        <td>${r.pe_moneyness || '—'}</td>
        <td>${fmtPx(r.pe_bid)}×${fmtNum(r.pe_bid_qty)}</td>
        <td>${fmtPx(r.pe_ask)}×${fmtNum(r.pe_ask_qty)}</td>
        <td>${fmtPx(r.pe_spread)}</td>
        <td>${r.pe_order_block || '—'}</td>
        <td class="liq-ok">${r.pe_bag != null ? fmtPx(r.pe_bag) : '—'}</td>
        <td class="liq-thin">${r.pe_fag != null ? fmtPx(r.pe_fag) : '—'}</td>
        <td class="${liqClass(r.pe_liquidity)}">${r.pe_liquidity || '—'}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="17">No strike data</td></tr>';

    const fresh = p.freshness || '—';
    const usable = p.usable_for_signals ? '<span class="badge ok">usable</span>' : '<span class="badge bad">not usable</span>';
    const freshBadge = fresh === 'fresh' ? '<span class="badge ok">fresh</span>' : `<span class="badge warn">${fresh}</span>`;
    const atmLiq = atmCe.liquidity || atmPe.liquidity;
    const liqBadge = atmLiq === 'ok' ? '<span class="badge ok">ATM liq ok</span>'
      : atmLiq === 'thin' ? '<span class="badge warn">ATM thin</span>'
      : atmLiq === 'poor' ? '<span class="badge bad">ATM poor liq</span>' : '';
    const sessBadge = (d.session || {}).entry_ok ? '<span class="badge ok">entries OK</span>'
      : (d.session || {}).market_open ? '<span class="badge warn">live / no new entries</span>'
      : '<span class="badge bad">after hours</span>';
    $('meta').innerHTML = `v1 · Updated ${d.updated_at || '—'} · Spot ${p.spot ?? '—'} · ATM ${p.atm_strike ?? '—'} · Expiry ${p.expiry || '—'} · NSE ${p.chain_timestamp || '—'} · ${freshBadge} ${usable} ${liqBadge} ${sessBadge}`;
  } catch (e) {
    $('error').style.display = 'block';
    $('error').textContent = String(e);
  }
}
$('refreshBtn').onclick = async () => {
  $('refreshBtn').disabled = true;
  $('meta').textContent = 'Fetching NSE / CPR / VWAP…';
  try {
    const r = await fetch('/api/refresh', { method: 'POST', credentials: 'same-origin' });
    if (r.status === 401) { location.href = '/login.html'; return; }
    const d = await r.json();
    if (d && (d.signal || d.payload !== undefined)) {
      window.__applyState = d;
    }
    await load();
  } catch (e) {
    $('error').style.display = 'block';
    $('error').textContent = String(e);
  }
  $('refreshBtn').disabled = false;
};
$('logoutBtn').onclick = async () => {
  await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' });
  location.href = '/login.html';
};
function openWhyModal() { $('whyModal').classList.add('open'); }
function closeWhyModal() { $('whyModal').classList.remove('open'); }
$('whyCallBtn').onclick = openWhyModal;
$('whyModalClose').onclick = closeWhyModal;
$('whyModal').onclick = (e) => { if (e.target === $('whyModal')) closeWhyModal(); };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeWhyModal(); });
// Manual refresh only — no timed /api hits
(async () => {
  const me = await ensureAuth();
  if (me) await load();
})();
</script>
</body>
</html>
"""


class LocalMonitorHandler(BaseHTTPRequestHandler):
    """Local desktop server only. Named so Vercel does not treat this file as an entrypoint."""

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, obj: Any, set_cookie: str | None = None) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                obj = json.loads(raw.decode("utf-8"))
                return obj if isinstance(obj, dict) else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
        qs = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
        return {k: (v[0] if v else "") for k, v in qs.items()}

    def _current_user(self) -> str | None:
        from auth_store import COOKIE_NAME, parse_cookies, user_from_cookie

        cookies = parse_cookies(self.headers.get("Cookie"))
        return user_from_cookie(cookies.get(COOKIE_NAME))

    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For") or ""
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _serve_public(self, name: str, content_type: str) -> bool:
        path = ROOT / "public" / name
        if not path.exists():
            return False
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        user = self._current_user()

        if path == "/favicon.svg":
            if self._serve_public("favicon.svg", "image/svg+xml; charset=utf-8"):
                return
            self.send_error(404)
            return

        if path in ("/login.html", "/login"):
            if user:
                self._redirect("/")
                return
            if self._serve_public("login.html", "text/html; charset=utf-8"):
                return
            self.send_error(404)
            return

        if path in ("/register.html", "/register"):
            if user:
                self._redirect("/")
                return
            if self._serve_public("register.html", "text/html; charset=utf-8"):
                return
            self.send_error(404)
            return

        if path in ("/admin-logs.html", "/admin-logs"):
            from auth_store import is_admin_username

            if not user:
                self._redirect("/login.html")
                return
            if not is_admin_username(user):
                self._redirect("/")
                return
            if self._serve_public("admin-logs.html", "text/html; charset=utf-8"):
                return
            self.send_error(404)
            return

        if path == "/api/me":
            from auth_store import is_admin_username

            if not user:
                self._json(401, {"ok": False, "authenticated": False})
                return
            admin = is_admin_username(user)
            self._json(
                200,
                {
                    "ok": True,
                    "authenticated": True,
                    "username": user,
                    "is_admin": admin,
                    "role": "admin" if admin else "user",
                },
            )
            return

        if path == "/api/admin/logs":
            from auth_store import is_admin_username, list_user_logs

            if not user:
                self._json(401, {"ok": False, "error": "Login required"})
                return
            if not is_admin_username(user):
                self._json(403, {"ok": False, "error": "Admin only"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            limit = 200
            if "limit" in qs:
                try:
                    limit = int(qs["limit"][0])
                except ValueError:
                    pass
            self._json(200, {"ok": True, "logs": list_user_logs(limit)})
            return

        if path == "/api/admin/users":
            from auth_store import is_admin_username, list_users_safe

            if not user:
                self._json(401, {"ok": False, "error": "Login required"})
                return
            if not is_admin_username(user):
                self._json(403, {"ok": False, "error": "Admin only"})
                return
            self._json(200, {"ok": True, "users": list_users_safe()})
            return

        if path == "/api/logout":
            from auth_store import COOKIE_NAME, destroy_session, parse_cookies, session_cookie_header

            cookies = parse_cookies(self.headers.get("Cookie"))
            destroy_session(cookies.get(COOKIE_NAME), ip=self._client_ip())
            self._json(200, {"ok": True}, set_cookie=session_cookie_header("", clear=True))
            return

        if path in ("/", "/index.html"):
            if not user:
                self._redirect("/login.html")
                return
            public = ROOT / "public" / "index.html"
            html = public.read_text(encoding="utf-8") if public.exists() else HTML
            self._html(html)
            return

        if path.startswith("/api/state"):
            if not user:
                self._json(401, {"ok": False, "error": "Login required", "signal": {"call": "WAIT"}})
                return
            with STATE_LOCK:
                out = dict(STATE)
                out["username"] = user
                self._json(200, out)
            return

        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/register":
            from auth_store import register_user

            body = self._read_json_body()
            ok, msg = register_user(
                str(body.get("username") or ""),
                str(body.get("password") or ""),
                ip=self._client_ip(),
            )
            self._json(201 if ok else 400, {"ok": ok, "message": msg} if ok else {"ok": False, "error": msg})
            return

        if path == "/api/login":
            from auth_store import authenticate, create_session, is_admin_username, session_cookie_header

            body = self._read_json_body()
            ok, name_or_err = authenticate(
                str(body.get("username") or ""),
                str(body.get("password") or ""),
                ip=self._client_ip(),
            )
            if not ok:
                self._json(401, {"ok": False, "error": name_or_err})
                return
            cookie = create_session(name_or_err)
            self._json(
                200,
                {"ok": True, "username": name_or_err, "is_admin": is_admin_username(name_or_err)},
                set_cookie=session_cookie_header(cookie),
            )
            return

        if path == "/api/logout":
            from auth_store import COOKIE_NAME, destroy_session, parse_cookies, session_cookie_header

            cookies = parse_cookies(self.headers.get("Cookie"))
            destroy_session(cookies.get(COOKIE_NAME), ip=self._client_ip())
            self._json(200, {"ok": True}, set_cookie=session_cookie_header("", clear=True))
            return

        if path.startswith("/api/refresh"):
            user = self._current_user()
            if not user:
                self._json(401, {"ok": False, "error": "Login required", "signal": {"call": "WAIT"}})
                return
            refresh_once()
            with STATE_LOCK:
                out = dict(STATE)
                out["ok"] = True
                out["username"] = user
                self._json(200, out)
            return

        self.send_error(404)


def main() -> int:
    ap = argparse.ArgumentParser(description="NIFTY Monitor v1 — manual refresh only")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--range", type=int, choices=[3, 5, 10], default=3)
    ap.add_argument("--strike", type=int, default=None)
    ap.add_argument("--expiry", default=None)
    args = ap.parse_args()
    CFG["range"] = args.range
    CFG["strike"] = args.strike
    CFG["expiry"] = args.expiry

    host = "127.0.0.1"
    httpd = ThreadingHTTPServer((host, args.port), LocalMonitorHandler)
    url = f"http://{host}:{args.port}/"

    # No background NSE poll — user clicks Refresh now
    with STATE_LOCK:
        STATE["updated_at"] = None
        STATE["session"] = ist_session_status()
        STATE["signal"] = {
            "call": "WAIT",
            "strength": "Idle",
            "reasons": ["Click Refresh now for live NSE / CPR / VWAP data"],
            "buyers_sellers": "—",
        }
    from auth_store import ensure_superadmin

    ensure_superadmin()
    print(f"MyDesk v{APP_VERSION} running at {url}", flush=True)
    print("Login required — Register / Log in (admin: superadmin). Ctrl+C to stop.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
