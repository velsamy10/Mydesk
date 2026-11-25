#!/usr/bin/env python3
"""
NIFTY Intraday Pro – Phase 1 External Option Chain Service

Fetches NIFTY option-chain OI from NSE, builds ATM ± N band aggregates,
computes Change OI vs the previous local snapshot, and writes a payload
that matches OptionChainDataSchema.json.

Pine Script cannot call this service. Use --watch and paste bridge fields
into the TradingView indicator (Method B in ExternalOptionChainService.md).

  python external_option_chain_service.py
  python external_option_chain_service.py --range 3
  python external_option_chain_service.py --strike 25200 --range 3
  python external_option_chain_service.py --watch 180 --range 3

Default band = current ATM ± 3 strikes (7 rows) with day Change OI and snapshot Change OI.
Requires Python 3.10+ (python.org installer with PATH). Research helper only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

BASE = "https://www.nseindia.com"
INFO = "https://www.nseindia.com/api/option-chain-contract-info?symbol=NIFTY"
CHAIN = "https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry={expiry}"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parent
# Vercel serverless FS is read-only except /tmp
_DATA = Path(os.environ["VERCEL_DATA_DIR"]) if os.environ.get("VERCEL_DATA_DIR") else (
    Path("/tmp/nifty_option_chain") if os.environ.get("VERCEL") else ROOT
)
if os.environ.get("VERCEL") or os.environ.get("VERCEL_DATA_DIR"):
    _DATA.mkdir(parents=True, exist_ok=True)
LIVE_PATH = _DATA / "option_chain_live.json"
PREV_PATH = _DATA / "option_chain_prev.json"
SCHEMA_VERSION = "1.0"
DEFAULT_STEP = 50
DEFAULT_STALE = 300
PCR_SIDEWAYS = 0.02
# Bid–ask liquidity: absolute ₹ and % of mid; ITM often thinner
SPREAD_OK_PCT = 1.5
SPREAD_THIN_PCT = 3.0
SPREAD_POOR_PCT = 5.0
SPREAD_OK_ABS = 2.0
SPREAD_THIN_ABS = 4.0
SPREAD_POOR_ABS = 8.0
DEPTH_THIN_TOP = 200  # L1 bid+ask qty
DEPTH_THIN_TOTAL = 5000


def _fnum(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _inum(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _iv_of(opt: dict[str, Any]) -> float | None:
    """NSE impliedVolatility as %; None if missing/zero."""
    try:
        v = opt.get("impliedVolatility")
        if v is None:
            return None
        f = float(v)
        return round(f, 2) if f > 0 else None
    except (TypeError, ValueError):
        return None


def moneyness_of(side: str, strike: float, spot: float, atm: float | None = None) -> str:
    # Use rounded ATM strike so 25800 vs spot 25800.6 is ATM, not micro-ITM
    if atm is not None and abs(strike - float(atm)) < 0.1:
        return "ATM"
    if abs(strike - spot) < 0.01:
        return "ATM"
    if side == "CE":
        return "ITM" if strike < spot else "OTM"
    return "ITM" if strike > spot else "OTM"


def grade_liquidity(
    spread: float | None,
    spread_pct: float | None,
    bid_qty: int,
    ask_qty: int,
    tot_buy: int,
    tot_sell: int,
    moneyness: str,
) -> str:
    """ok / thin / poor / unknown — ITM gets stricter % thresholds."""
    if spread is None or spread_pct is None:
        return "unknown"
    top = bid_qty + ask_qty
    book = tot_buy + tot_sell
    thin_depth = top < DEPTH_THIN_TOP or book < DEPTH_THIN_TOTAL
    # ITM: treat 3%+ or ₹4+ as poor sooner
    poor_pct = SPREAD_THIN_PCT if moneyness == "ITM" else SPREAD_POOR_PCT
    poor_abs = SPREAD_THIN_ABS if moneyness == "ITM" else SPREAD_POOR_ABS
    thin_pct = SPREAD_OK_PCT if moneyness == "ITM" else SPREAD_THIN_PCT
    thin_abs = SPREAD_OK_ABS if moneyness == "ITM" else SPREAD_THIN_ABS
    if spread_pct >= poor_pct or spread >= poor_abs or (moneyness == "ITM" and thin_depth and spread_pct >= SPREAD_OK_PCT):
        return "poor"
    if spread_pct >= thin_pct or spread >= thin_abs or thin_depth:
        return "thin"
    return "ok"


def extract_side_quote(
    opt: dict[str, Any],
    strike: float,
    spot: float,
    side: str,
    atm: float | None = None,
) -> dict[str, Any]:
    """NSE L1: buyPrice1/sellPrice1 + quantities; spread & bid vs offer depth."""
    bid = _fnum(opt.get("buyPrice1"))
    ask = _fnum(opt.get("sellPrice1"))
    bid_qty = _inum(opt.get("buyQuantity1"))
    ask_qty = _inum(opt.get("sellQuantity1"))
    tot_buy = _inum(opt.get("totalBuyQuantity"))
    tot_sell = _inum(opt.get("totalSellQuantity"))
    ltp = _fnum(opt.get("lastPrice"))
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (ltp if ltp > 0 else None)
    spread = round(ask - bid, 2) if bid > 0 and ask > 0 else None
    spread_pct = round(spread / mid * 100.0, 2) if spread is not None and mid and mid > 0 else None
    depth_sum = bid_qty + ask_qty
    # + = more bid qty resting; − = more offer qty
    imbalance = round((bid_qty - ask_qty) / depth_sum, 3) if depth_sum > 0 else None
    mny = moneyness_of(side, strike, spot, atm)
    liq = grade_liquidity(spread, spread_pct, bid_qty, ask_qty, tot_buy, tot_sell, mny)
    order = recommend_order(bid if bid > 0 else None, ask if ask > 0 else None, mid, liq, imbalance)
    return {
        "bid": bid if bid > 0 else None,
        "ask": ask if ask > 0 else None,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "total_buy_qty": tot_buy,
        "total_sell_qty": tot_sell,
        "mid": round(mid, 2) if mid else None,
        "spread": spread,
        "spread_pct": spread_pct,
        "depth_imbalance": imbalance,
        "moneyness": mny,
        "liquidity": liq,
        "strike": float(strike),
        "order_block": order["order_block"],
        "order_block_low": order["order_block_low"],
        "order_block_high": order["order_block_high"],
        "bag": order["bag"],  # Buy At Gap — patient limit inside spread
        "fag": order["fag"],  # Fill At Ask — aggressive buy at offer
        "order_prefer": order["prefer"],
        "order_note": order["note"],
    }


def recommend_order(
    bid: float | None,
    ask: float | None,
    mid: float | None,
    liquidity: str,
    imbalance: float | None,
) -> dict[str, Any]:
    """
    Order block = bid–ask zone for limit work.
    BAG = Buy At Gap (limit near mid / inside spread).
    FAG = Fill At Ask (pay the offer for immediate fill).
    """
    if bid is None or ask is None or mid is None or bid <= 0 or ask <= 0:
        return {
            "order_block": None,
            "order_block_low": None,
            "order_block_high": None,
            "bag": None,
            "fag": None,
            "prefer": "SKIP",
            "note": "No bid/ask — do not place order",
        }
    # Round to 0.05 (NIFTY option tick)
    def _tick(x: float) -> float:
        return round(round(x / 0.05) * 0.05, 2)

    bag = _tick(mid)
    # Keep BAG strictly inside the block when possible
    if bag <= bid:
        bag = _tick(bid + 0.05)
    if bag >= ask:
        bag = _tick(ask - 0.05)
    if bag <= bid or bag >= ask:
        bag = _tick(mid)
    fag = _tick(ask)
    block = f"{_tick(bid)} – {_tick(ask)}"

    prefer = "BAG"
    note = f"Limit in order block {block}; start BAG {_tick(bag)}, step up toward ask if unfilled"
    if liquidity == "poor":
        prefer = "SKIP"
        note = f"Wide/thin book — avoid; if must, BAG only {_tick(bag)} inside {block}, never chase FAG"
    elif liquidity == "thin":
        prefer = "BAG"
        note = f"Thin depth — prefer BAG {_tick(bag)}; use FAG {_tick(fag)} only if trade is urgent"
    elif imbalance is not None and imbalance < -0.35:
        # Heavy offer resting — ask may be sticky; mid still fine; FAG ok
        prefer = "BAG"
        note = f"Offer-heavy book — BAG {_tick(bag)} first; FAG {_tick(fag)} if need fill"
    elif imbalance is not None and imbalance > 0.35:
        prefer = "BAG"
        note = f"Bid-heavy book — BAG {_tick(bag)} may fill; FAG {_tick(fag)} if chasing"
    else:
        prefer = "BAG"
        note = f"Liquid — prefer BAG {_tick(bag)}; FAG {_tick(fag)} only for instant fill"

    return {
        "order_block": block,
        "order_block_low": _tick(bid),
        "order_block_high": _tick(ask),
        "bag": _tick(bag),
        "fag": fag,
        "prefer": prefer,
        "note": note,
    }


def _opener() -> urllib.request.OpenerDirector:
    jar = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(jar)
    req = urllib.request.Request(
        BASE + "/option-chain",
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    opener.open(req, timeout=25).read()
    return opener


def _get_json(opener: urllib.request.OpenerDirector, url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/option-chain",
        },
    )
    with opener.open(req, timeout=25) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        if "Access Denied" in raw or "errors.edgesuite.net" in raw:
            raise RuntimeError("NSE Access Denied (Akamai). Open nseindia.com/option-chain in a browser, wait, retry.")
        return json.loads(raw)


def expiry_to_yymmdd(expiry: str) -> str:
    dt = datetime.strptime(expiry, "%d-%b-%Y")
    return dt.strftime("%y%m%d")


def pick_expiry(info: dict[str, Any], wanted: str | None) -> str:
    expiries: list[str] = list(info.get("expiryDates") or [])
    if not expiries:
        raise RuntimeError("No expiry dates in NSE contract-info response")
    if wanted:
        if wanted not in expiries:
            raise RuntimeError(f"Expiry {wanted!r} not in {expiries[:8]}…")
        return wanted
    today = datetime.now().date()
    live: list[str] = []
    for e in expiries:
        try:
            if datetime.strptime(e, "%d-%b-%Y").date() >= today:
                live.append(e)
        except ValueError:
            live.append(e)
    return live[0] if live else expiries[0]


def atm_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def parse_chain_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def freshness_of(chain_ts: str | None, max_stale: int) -> tuple[str, bool]:
    dt = parse_chain_ts(chain_ts)
    if dt is None:
        return "unknown", False
    age = (datetime.now() - dt).total_seconds()
    if age < 0:
        # NSE clock vs local clock skew – treat as fresh if within 2 minutes ahead
        return ("fresh", True) if age > -120 else ("unknown", False)
    if age <= max_stale:
        return "fresh", True
    return "stale", False


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def error_payload(message: str, strike_range: int, step: int, max_stale: int) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "message": message,
        "symbol": "NIFTY",
        "expiry": "",
        "expiry_yymmdd": "",
        "spot": None,
        "atm_strike": None,
        "strike_interval": step,
        "strike_range": strike_range,
        "chain_timestamp": None,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "freshness": "unknown",
        "max_stale_seconds": max_stale,
        "usable_for_signals": False,
        "source": {
            "provider": "nseindia.com",
            "endpoints": [BASE + "/option-chain", INFO, CHAIN],
            "note": "Live fetch failed. Do not invent OI values.",
        },
        "previous_snapshot": {"available": False, "fetched_at": None, "chain_timestamp": None},
        "strikes": [],
        "aggregates": {
            "strike_count_expected": strike_range * 2 + 3,
            "strike_count_actual": 0,
            "missing_strikes": [],
            "total_ce_oi": None,
            "total_pe_oi": None,
            "pcr": None,
            "pcr_prev": None,
            "pcr_chg": None,
            "pcr_direction": "unknown",
            "ce_chg_oi": None,
            "pe_chg_oi": None,
            "ce_chg_oi_pct": None,
            "pe_chg_oi_pct": None,
            "ce_chg_day_total": None,
            "pe_chg_day_total": None,
            "max_ce_oi_strike": None,
            "max_pe_oi_strike": None,
        },
        "bridge": {
            "pcr": None,
            "pcr_direction": "unknown",
            "ce_chg_oi": None,
            "pe_chg_oi": None,
            "ce_chg_oi_pct": None,
            "pe_chg_oi_pct": None,
            "atm_strike": None,
            "spot": None,
            "freshness": "unknown",
            "usable_for_signals": False,
            "chain_timestamp": None,
            "paste_line": "PCR=n/a|DIR=unknown|CE_dOI=n/a|PE_dOI=n/a|ATM=n/a|SPOT=n/a|FRESH=unknown|USABLE=0",
        },
    }


def build_payload(
    data: dict[str, Any],
    expiry: str,
    strike_range: int,
    step: int,
    max_stale: int,
    prev: dict[str, Any] | None,
    centre_strike: int | None = None,
) -> dict[str, Any]:
    records = data.get("records") or {}
    spot_raw = records.get("underlyingValue")
    if spot_raw is None:
        raise RuntimeError("NSE chain missing records.underlyingValue")
    spot = float(spot_raw)
    chain_ts = records.get("timestamp")
    atm = atm_strike(spot, step)
    centre = int(centre_strike) if centre_strike else atm
    centre = atm_strike(float(centre), step)
    # Band = centre ± range, plus one extra ITM wing each side (CE below, PE above)
    expected = [centre + i * step for i in range(-strike_range, strike_range + 1)]
    extra_itm_ce = centre - (strike_range + 1) * step
    extra_itm_pe = centre + (strike_range + 1) * step
    if extra_itm_ce not in expected:
        expected = [extra_itm_ce] + expected
    if extra_itm_pe not in expected:
        expected = expected + [extra_itm_pe]

    by_strike: dict[int, dict[str, Any]] = {}
    for rec in records.get("data") or []:
        k = rec.get("strikePrice")
        if k is None:
            continue
        ki = int(float(k))
        ce = rec.get("CE") or {}
        pe = rec.get("PE") or {}
        kf = float(ki)
        ce_q = extract_side_quote(ce, kf, spot, "CE", atm)
        pe_q = extract_side_quote(pe, kf, spot, "PE", atm)
        by_strike[ki] = {
            "strike": kf,
            "ce_oi": int(ce.get("openInterest") or 0),
            "pe_oi": int(pe.get("openInterest") or 0),
            "ce_chg_day": int(ce.get("changeinOpenInterest") or 0),
            "pe_chg_day": int(pe.get("changeinOpenInterest") or 0),
            "ce_ltp": float(ce.get("lastPrice") or 0),
            "pe_ltp": float(pe.get("lastPrice") or 0),
            "ce_iv": _iv_of(ce),
            "pe_iv": _iv_of(pe),
            "ce_bid": ce_q["bid"],
            "ce_ask": ce_q["ask"],
            "ce_bid_qty": ce_q["bid_qty"],
            "ce_ask_qty": ce_q["ask_qty"],
            "ce_total_buy_qty": ce_q["total_buy_qty"],
            "ce_total_sell_qty": ce_q["total_sell_qty"],
            "ce_spread": ce_q["spread"],
            "ce_spread_pct": ce_q["spread_pct"],
            "ce_depth_imbalance": ce_q["depth_imbalance"],
            "ce_moneyness": ce_q["moneyness"],
            "ce_liquidity": ce_q["liquidity"],
            "ce_order_block": ce_q["order_block"],
            "ce_bag": ce_q["bag"],
            "ce_fag": ce_q["fag"],
            "ce_order_prefer": ce_q["order_prefer"],
            "pe_bid": pe_q["bid"],
            "pe_ask": pe_q["ask"],
            "pe_bid_qty": pe_q["bid_qty"],
            "pe_ask_qty": pe_q["ask_qty"],
            "pe_total_buy_qty": pe_q["total_buy_qty"],
            "pe_total_sell_qty": pe_q["total_sell_qty"],
            "pe_spread": pe_q["spread"],
            "pe_spread_pct": pe_q["spread_pct"],
            "pe_depth_imbalance": pe_q["depth_imbalance"],
            "pe_moneyness": pe_q["moneyness"],
            "pe_liquidity": pe_q["liquidity"],
            "pe_order_block": pe_q["order_block"],
            "pe_bag": pe_q["bag"],
            "pe_fag": pe_q["fag"],
            "pe_order_prefer": pe_q["order_prefer"],
        }

    prev_map: dict[int, dict[str, Any]] = {}
    if prev and prev.get("status") in ("ok", "partial") and prev.get("strikes"):
        for r in prev["strikes"]:
            prev_map[int(r["strike"])] = r

    strikes: list[dict[str, Any]] = []
    missing: list[float] = []
    for k in expected:
        row = by_strike.get(k)
        if not row:
            missing.append(float(k))
            continue
        p = prev_map.get(k)
        out = dict(row)
        if p:
            out["ce_chg_snap"] = out["ce_oi"] - int(p.get("ce_oi") or 0)
            out["pe_chg_snap"] = out["pe_oi"] - int(p.get("pe_oi") or 0)
            out["ce_ltp_chg_snap"] = round(out["ce_ltp"] - float(p.get("ce_ltp") or 0), 2)
            out["pe_ltp_chg_snap"] = round(out["pe_ltp"] - float(p.get("pe_ltp") or 0), 2)
            out["ce_iv_chg_snap"] = (
                round(float(out["ce_iv"]) - float(p["ce_iv"]), 2)
                if out.get("ce_iv") is not None and p.get("ce_iv") is not None
                else None
            )
            out["pe_iv_chg_snap"] = (
                round(float(out["pe_iv"]) - float(p["pe_iv"]), 2)
                if out.get("pe_iv") is not None and p.get("pe_iv") is not None
                else None
            )
        else:
            out["ce_chg_snap"] = None
            out["pe_chg_snap"] = None
            out["ce_ltp_chg_snap"] = None
            out["pe_ltp_chg_snap"] = None
            out["ce_iv_chg_snap"] = None
            out["pe_iv_chg_snap"] = None
        strikes.append(out)

    total_ce = sum(r["ce_oi"] for r in strikes)
    total_pe = sum(r["pe_oi"] for r in strikes)
    pcr = (total_pe / total_ce) if total_ce > 0 else None
    pcr_prev = None
    if prev and (prev.get("aggregates") or {}).get("pcr") is not None:
        pcr_prev = float(prev["aggregates"]["pcr"])
    pcr_chg = (pcr - pcr_prev) if pcr is not None and pcr_prev is not None else None
    if pcr_chg is None:
        pcr_dir = "unknown"
    elif pcr_chg >= PCR_SIDEWAYS:
        pcr_dir = "rising"
    elif pcr_chg <= -PCR_SIDEWAYS:
        pcr_dir = "falling"
    else:
        pcr_dir = "sideways"

    has_snap = any(r.get("ce_chg_snap") is not None for r in strikes)
    if has_snap:
        ce_chg_oi = sum(int(r["ce_chg_snap"] or 0) for r in strikes)
        pe_chg_oi = sum(int(r["pe_chg_snap"] or 0) for r in strikes)
        prev_ce = sum(int(prev_map[int(r["strike"])].get("ce_oi") or 0) for r in strikes if int(r["strike"]) in prev_map)
        prev_pe = sum(int(prev_map[int(r["strike"])].get("pe_oi") or 0) for r in strikes if int(r["strike"]) in prev_map)
        ce_chg_pct = (ce_chg_oi / prev_ce * 100.0) if prev_ce > 0 else None
        pe_chg_pct = (pe_chg_oi / prev_pe * 100.0) if prev_pe > 0 else None
    else:
        ce_chg_oi = pe_chg_oi = None
        ce_chg_pct = pe_chg_pct = None

    max_ce_k = max(strikes, key=lambda r: r["ce_oi"])["strike"] if strikes else None
    max_pe_k = max(strikes, key=lambda r: r["pe_oi"])["strike"] if strikes else None

    # Liquidity focus: ATM + 2 nearest ITM CE and 2 nearest ITM PE (thin books / wide spreads)
    atm_row = next((r for r in strikes if abs(float(r["strike"]) - float(atm)) < 0.1), None)
    # CE ITM: strikes below ATM, nearest first (highest strike first)
    itm_ce_rows = sorted(
        [r for r in strikes if r.get("ce_moneyness") == "ITM"],
        key=lambda r: float(r["strike"]),
        reverse=True,
    )[:2]
    # PE ITM: strikes above ATM, nearest first (lowest strike first)
    itm_pe_rows = sorted(
        [r for r in strikes if r.get("pe_moneyness") == "ITM"],
        key=lambda r: float(r["strike"]),
    )[:2]
    itm_ce = itm_ce_rows[0] if itm_ce_rows else None
    itm_ce_2 = itm_ce_rows[1] if len(itm_ce_rows) > 1 else None
    itm_pe = itm_pe_rows[0] if itm_pe_rows else None
    itm_pe_2 = itm_pe_rows[1] if len(itm_pe_rows) > 1 else None

    def _liq_snap(row: dict[str, Any] | None, prefix: str) -> dict[str, Any] | None:
        if not row:
            return None
        # Prefer fields already on side quote; fall back to recomputing from bid/ask
        bid = row.get(f"{prefix}_bid")
        ask = row.get(f"{prefix}_ask")
        mid = None
        if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
            mid = round((float(bid) + float(ask)) / 2.0, 2)
        order = recommend_order(
            float(bid) if bid is not None else None,
            float(ask) if ask is not None else None,
            mid,
            str(row.get(f"{prefix}_liquidity") or "unknown"),
            row.get(f"{prefix}_depth_imbalance"),
        )
        return {
            "strike": row["strike"],
            "bid": bid,
            "ask": ask,
            "bid_qty": row.get(f"{prefix}_bid_qty"),
            "ask_qty": row.get(f"{prefix}_ask_qty"),
            "spread": row.get(f"{prefix}_spread"),
            "spread_pct": row.get(f"{prefix}_spread_pct"),
            "depth_imbalance": row.get(f"{prefix}_depth_imbalance"),
            "moneyness": row.get(f"{prefix}_moneyness"),
            "liquidity": row.get(f"{prefix}_liquidity"),
            "ltp": row.get(f"{prefix}_ltp"),
            "order_block": order["order_block"],
            "order_block_low": order["order_block_low"],
            "order_block_high": order["order_block_high"],
            "bag": order["bag"],
            "fag": order["fag"],
            "order_prefer": order["prefer"],
            "order_note": order["note"],
        }

    poor_ce = sum(1 for r in strikes if r.get("ce_liquidity") == "poor")
    poor_pe = sum(1 for r in strikes if r.get("pe_liquidity") == "poor")
    thin_ce = sum(1 for r in strikes if r.get("ce_liquidity") in ("thin", "poor"))
    thin_pe = sum(1 for r in strikes if r.get("pe_liquidity") in ("thin", "poor"))

    # ATM IV (NSE impliedVolatility %) — caution filter for option buyers
    ce_iv = atm_row.get("ce_iv") if atm_row else None
    pe_iv = atm_row.get("pe_iv") if atm_row else None
    atm_iv = None
    if ce_iv is not None and pe_iv is not None:
        atm_iv = round((float(ce_iv) + float(pe_iv)) / 2.0, 2)
    elif ce_iv is not None:
        atm_iv = float(ce_iv)
    elif pe_iv is not None:
        atm_iv = float(pe_iv)
    prev_atm = (prev or {}).get("iv") or {}
    prev_atm_iv = prev_atm.get("atm_iv")
    atm_iv_chg = (
        round(float(atm_iv) - float(prev_atm_iv), 2)
        if atm_iv is not None and prev_atm_iv is not None
        else None
    )
    # Rich if IV jumped vs last poll, or absolute ATM IV elevated for NIFTY weeklies
    iv_rich = False
    if atm_iv_chg is not None and atm_iv_chg >= 1.5:
        iv_rich = True
    if atm_iv is not None and atm_iv >= 18.0:
        iv_rich = True
    iv = {
        "ce_iv": ce_iv,
        "pe_iv": pe_iv,
        "atm_iv": atm_iv,
        "atm_iv_prev": prev_atm_iv,
        "atm_iv_chg": atm_iv_chg,
        "ce_iv_chg": atm_row.get("ce_iv_chg_snap") if atm_row else None,
        "pe_iv_chg": atm_row.get("pe_iv_chg_snap") if atm_row else None,
        "iv_rich": iv_rich,
        "note": "IV = NSE impliedVolatility %. Rising/high IV = expensive premium for buyers (crush risk).",
    }

    liquidity = {
        "atm_ce": _liq_snap(atm_row, "ce"),
        "atm_pe": _liq_snap(atm_row, "pe"),
        "nearest_itm_ce": _liq_snap(itm_ce, "ce"),
        "second_itm_ce": _liq_snap(itm_ce_2, "ce"),
        "nearest_itm_pe": _liq_snap(itm_pe, "pe"),
        "second_itm_pe": _liq_snap(itm_pe_2, "pe"),
        "itm_ce": [_liq_snap(r, "ce") for r in itm_ce_rows],
        "itm_pe": [_liq_snap(r, "pe") for r in itm_pe_rows],
        "band_poor_ce": poor_ce,
        "band_poor_pe": poor_pe,
        "band_thin_ce": thin_ce,
        "band_thin_pe": thin_pe,
        "note": (
            "Strike + order block (bid–ask). BAG=Buy At Gap (limit near mid). "
            "FAG=Fill At Ask (pay offer). Prefer BAG; FAG only if urgent. ITM often thinner."
        ),
    }

    fresh, fresh_ok = freshness_of(chain_ts if isinstance(chain_ts, str) else None, max_stale)
    expected_n = strike_range * 2 + 3  # ± range plus one extra ITM CE + one extra ITM PE
    actual_n = len(strikes)
    if actual_n == 0:
        status = "error"
        usable = False
        msg = "No strikes returned in ATM band"
    elif missing:
        status = "partial"
        usable = fresh_ok and actual_n >= max(3, expected_n // 2)
        msg = f"Missing strikes: {missing}"
    else:
        status = "ok"
        usable = fresh_ok
        msg = None

    # First snapshot: PCR level usable if fresh, but Change OI unknown
    if not has_snap and status in ("ok", "partial"):
        msg = (msg + "; " if msg else "") + "No previous snapshot yet – Change OI unknown until next poll"

    paste = (
        f"PCR={pcr if pcr is not None else 'n/a'}|"
        f"DIR={pcr_dir}|"
        f"CE_dOI={ce_chg_oi if ce_chg_oi is not None else 'n/a'}|"
        f"PE_dOI={pe_chg_oi if pe_chg_oi is not None else 'n/a'}|"
        f"ATM={atm}|SPOT={spot}|FRESH={fresh}|USABLE={1 if usable else 0}|"
        f"TS={chain_ts or 'n/a'}"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "message": msg,
        "symbol": "NIFTY",
        "expiry": expiry,
        "expiry_yymmdd": expiry_to_yymmdd(expiry),
        "spot": spot,
        "atm_strike": float(atm),
        "centre_strike": float(centre),
        "strike_interval": float(step),
        "strike_range": strike_range,
        "chain_timestamp": chain_ts,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "freshness": fresh,
        "max_stale_seconds": max_stale,
        "usable_for_signals": usable,
        "source": {
            "provider": "nseindia.com",
            "endpoints": [
                BASE + "/option-chain",
                INFO,
                CHAIN.format(expiry=urllib.parse.quote(expiry)),
            ],
            "note": "Live NSE-derived payload written by external_option_chain_service.py",
        },
        "previous_snapshot": {
            "available": bool(prev and prev.get("status") in ("ok", "partial")),
            "fetched_at": (prev or {}).get("fetched_at"),
            "chain_timestamp": (prev or {}).get("chain_timestamp"),
        },
        "strikes": strikes,
        "aggregates": {
            "strike_count_expected": expected_n,
            "strike_count_actual": actual_n,
            "missing_strikes": missing,
            "total_ce_oi": total_ce,
            "total_pe_oi": total_pe,
            "pcr": round(pcr, 4) if pcr is not None else None,
            "pcr_prev": round(pcr_prev, 4) if pcr_prev is not None else None,
            "pcr_chg": round(pcr_chg, 4) if pcr_chg is not None else None,
            "pcr_direction": pcr_dir,
            "ce_chg_oi": ce_chg_oi,
            "pe_chg_oi": pe_chg_oi,
            "ce_chg_oi_pct": round(ce_chg_pct, 3) if ce_chg_pct is not None else None,
            "pe_chg_oi_pct": round(pe_chg_pct, 3) if pe_chg_pct is not None else None,
            "ce_chg_day_total": sum(int(r["ce_chg_day"] or 0) for r in strikes),
            "pe_chg_day_total": sum(int(r["pe_chg_day"] or 0) for r in strikes),
            "max_ce_oi_strike": max_ce_k,
            "max_pe_oi_strike": max_pe_k,
            "liquidity": liquidity,
            "iv": iv,
        },
        "liquidity": liquidity,
        "iv": iv,
        "bridge": {
            "pcr": round(pcr, 4) if pcr is not None else None,
            "pcr_direction": pcr_dir,
            "ce_chg_oi": ce_chg_oi,
            "pe_chg_oi": pe_chg_oi,
            "ce_chg_oi_pct": round(ce_chg_pct, 3) if ce_chg_pct is not None else None,
            "pe_chg_oi_pct": round(pe_chg_pct, 3) if pe_chg_pct is not None else None,
            "atm_strike": float(atm),
            "centre_strike": float(centre),
            "spot": spot,
            "freshness": fresh,
            "usable_for_signals": usable,
            "chain_timestamp": chain_ts,
            "paste_line": paste,
        },
    }


def fetch_once(
    expiry: str | None,
    strike_range: int,
    step: int,
    max_stale: int,
    centre_strike: int | None = None,
) -> dict[str, Any]:
    prev = load_json(LIVE_PATH) or load_json(PREV_PATH)
    try:
        opener = _opener()
        info = _get_json(opener, INFO)
        use = pick_expiry(info, expiry)
        data = _get_json(opener, CHAIN.format(expiry=urllib.parse.quote(use)))
        if not (data.get("records") or {}).get("data"):
            raise RuntimeError("Empty option-chain-v3 payload (check expiry / session)")
        payload = build_payload(data, use, strike_range, step, max_stale, prev, centre_strike)
    except Exception as exc:  # noqa: BLE001 – surface any fetch failure in schema
        payload = error_payload(str(exc), strike_range, step, max_stale)

    # Rotate snapshots only on usable or partial success with strikes
    if payload.get("status") in ("ok", "partial") and payload.get("strikes"):
        if LIVE_PATH.exists():
            try:
                PREV_PATH.write_text(LIVE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        save_json(LIVE_PATH, payload)
    else:
        save_json(LIVE_PATH, payload)
    return payload


def _fmt_chg(v: Any) -> str:
    if v is None:
        return "n/a"
    n = int(v)
    return f"{n:+d}" if n != 0 else "0"


def print_summary(payload: dict[str, Any]) -> None:
    agg = payload.get("aggregates") or {}
    br = payload.get("bridge") or {}
    centre = payload.get("centre_strike", payload.get("atm_strike"))
    atm = payload.get("atm_strike")
    print("=" * 72)
    print(f"NIFTY OI band | {payload.get('fetched_at')}")
    print(f"status={payload.get('status')}  freshness={payload.get('freshness')}  usable={payload.get('usable_for_signals')}")
    if payload.get("message"):
        print(f"message: {payload['message']}")
    print(
        f"expiry={payload.get('expiry')}  spot={payload.get('spot')}  "
        f"ATM={atm}  centre={centre}  range=±{payload.get('strike_range')} "
        f"({payload.get('strike_range', 0) * 2 + 1} strikes)"
    )
    print(f"NSE ts={payload.get('chain_timestamp')}")
    print("-" * 72)
    print(
        f"{'Strike':>8}  {'CE OI':>10}  {'CE Δday':>10}  {'CE Δsnap':>10}  "
        f"{'PE OI':>10}  {'PE Δday':>10}  {'PE Δsnap':>10}  {'PCR':>6}"
    )
    for r in payload.get("strikes") or []:
        k = int(r.get("strike") or 0)
        mark = "*" if centre is not None and abs(float(k) - float(centre)) < 0.1 else " "
        ce = r.get("ce_oi")
        pe = r.get("pe_oi")
        pcr = (pe / ce) if ce and pe is not None and ce > 0 else None
        print(
            f"{mark}{k:7d}  {int(ce or 0):10d}  {_fmt_chg(r.get('ce_chg_day')):>10}  {_fmt_chg(r.get('ce_chg_snap')):>10}  "
            f"{int(pe or 0):10d}  {_fmt_chg(r.get('pe_chg_day')):>10}  {_fmt_chg(r.get('pe_chg_snap')):>10}  "
            f"{(f'{pcr:.2f}' if pcr is not None else 'n/a'):>6}"
        )
    print("-" * 72)
    print(
        f"{'Strike':>8}  {'CE Bid':>8}  {'CE Ask':>8}  {'Sprd':>6}  {'BidQty':>7}  {'AskQty':>7}  {'Liq':>5}  "
        f"{'PE Bid':>8}  {'PE Ask':>8}  {'Sprd':>6}  {'BidQty':>7}  {'AskQty':>7}  {'Liq':>5}"
    )
    for r in payload.get("strikes") or []:
        k = int(r.get("strike") or 0)
        mark = "*" if centre is not None and abs(float(k) - float(centre)) < 0.1 else " "

        def _px(v: Any) -> str:
            return f"{float(v):.2f}" if v is not None else "n/a"

        print(
            f"{mark}{k:7d}  {_px(r.get('ce_bid')):>8}  {_px(r.get('ce_ask')):>8}  "
            f"{_px(r.get('ce_spread')):>6}  {int(r.get('ce_bid_qty') or 0):7d}  {int(r.get('ce_ask_qty') or 0):7d}  "
            f"{str(r.get('ce_liquidity') or '?'):>5}  "
            f"{_px(r.get('pe_bid')):>8}  {_px(r.get('pe_ask')):>8}  "
            f"{_px(r.get('pe_spread')):>6}  {int(r.get('pe_bid_qty') or 0):7d}  {int(r.get('pe_ask_qty') or 0):7d}  "
            f"{str(r.get('pe_liquidity') or '?'):>5}"
        )
    liq = payload.get("liquidity") or agg.get("liquidity") or {}
    atm_ce = liq.get("atm_ce") or {}
    atm_pe = liq.get("atm_pe") or {}
    itm_ce_list = liq.get("itm_ce") or []
    itm_pe_list = liq.get("itm_pe") or []
    if not itm_ce_list:
        itm_ce_list = [x for x in (liq.get("nearest_itm_ce"), liq.get("second_itm_ce")) if x]
    if not itm_pe_list:
        itm_pe_list = [x for x in (liq.get("nearest_itm_pe"), liq.get("second_itm_pe")) if x]
    print("-" * 72)
    print(f"Band PCR={agg.get('pcr')} ({agg.get('pcr_direction')})  CE_OI={agg.get('total_ce_oi')}  PE_OI={agg.get('total_pe_oi')}")
    print(f"Band Change OI snapshot: CE={_fmt_chg(agg.get('ce_chg_oi'))}  PE={_fmt_chg(agg.get('pe_chg_oi'))}")
    print(f"Band Change OI day (NSE): CE={_fmt_chg(agg.get('ce_chg_day_total'))}  PE={_fmt_chg(agg.get('pe_chg_day_total'))}")
    print(f"Max CE OI strike={agg.get('max_ce_oi_strike')}  Max PE OI strike={agg.get('max_pe_oi_strike')}")
    print(
        f"ATM CE spread={atm_ce.get('spread')} ({atm_ce.get('spread_pct')}%) "
        f"bid/ask qty={atm_ce.get('bid_qty')}/{atm_ce.get('ask_qty')} liq={atm_ce.get('liquidity')} | "
        f"ATM PE spread={atm_pe.get('spread')} ({atm_pe.get('spread_pct')}%) "
        f"bid/ask qty={atm_pe.get('bid_qty')}/{atm_pe.get('ask_qty')} liq={atm_pe.get('liquidity')}"
    )
    for i, itm_ce in enumerate(itm_ce_list, 1):
        print(
            f"ITM CE #{i} {itm_ce.get('strike')}: spread={itm_ce.get('spread')} "
            f"({itm_ce.get('spread_pct')}%) depth {itm_ce.get('bid_qty')}/{itm_ce.get('ask_qty')} → {itm_ce.get('liquidity')}"
        )
    for i, itm_pe in enumerate(itm_pe_list, 1):
        print(
            f"ITM PE #{i} {itm_pe.get('strike')}: spread={itm_pe.get('spread')} "
            f"({itm_pe.get('spread_pct')}%) depth {itm_pe.get('bid_qty')}/{itm_pe.get('ask_qty')} → {itm_pe.get('liquidity')}"
        )
    print("-" * 72)
    print("BRIDGE PASTE (TradingView Method B):")
    print(br.get("paste_line"))
    print(f"Wrote: {LIVE_PATH.name}  (* = centre strike)")
    print("=" * 72)


def main() -> int:
    ap = argparse.ArgumentParser(description="NSE option-chain OI for current strike ± N (default ±3)")
    ap.add_argument("--expiry", default=None, help="NSE expiry dd-Mon-yyyy")
    ap.add_argument("--range", type=int, choices=[3, 5, 10], default=3, help="Centre ± N strikes (default 3 → 7 rows)")
    ap.add_argument("--strike", type=int, default=None, help="Centre strike (default = ATM from spot)")
    ap.add_argument("--step", type=int, default=DEFAULT_STEP, help="Strike interval (NIFTY=50)")
    ap.add_argument("--max-stale", type=int, default=DEFAULT_STALE, help="Seconds before freshness=stale")
    ap.add_argument("--watch", type=int, default=0, help="Poll every N seconds (min 60). 0 = once")
    args = ap.parse_args()

    watch = int(args.watch)
    if watch and watch < 60:
        print("Refusing watch < 60s (NSE rate / Akamai). Use 180 for 3m cadence.", file=sys.stderr)
        return 2

    while True:
        payload = fetch_once(args.expiry, args.range, args.step, args.max_stale, args.strike)
        print_summary(payload)
        if not watch:
            return 0 if payload.get("status") != "error" else 1
        print(f"Sleeping {watch}s…\n")
        time.sleep(watch)


if __name__ == "__main__":
    raise SystemExit(main())
