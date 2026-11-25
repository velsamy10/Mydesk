# NIFTY Intraday Pro – Phase 1 External Option Chain Integration

## Hard limitation (read first)

**Pine Script cannot call the NSE API, open HTTP/WebSocket connections, or read a local JSON file.**  
TradingView alerts can send data *out* (webhook). They cannot pull an option-chain response *into* a running indicator.

Therefore this project **separates**:

| Layer | Responsibility | Runs where |
|---|---|---|
| **External Option Chain Service** | Session cookie, NSE fetch, ATM band, PCR, Change OI from consecutive snapshots, freshness, JSON contract | Your PC (Python / PowerShell) |
| **Pine indicator** | EMA, volume pivots, S/R, score engine, labels, alerts on the 3m NIFTY chart | TradingView |
| **Bridge** | How selected fields from the contract reach Pine | Documented below – never “NSE → Pine” direct |

This document is the **integration contract** for Phase 1. Phases 2–10 consume this contract; they do not invent a second OI source.

---

## 1. External data architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  NSE India (option-chain pages + JSON APIs)                     │
│  Requires browser-like session cookie; Akamai may block bots    │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTPS (cookie session)
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  External Option Chain Service  (this repo)                     │
│  File: external_option_chain_service.py                         │
│                                                                 │
│  1. Warm session: GET /option-chain                             │
│  2. Expiries: GET /api/option-chain-contract-info?symbol=NIFTY  │
│  3. Chain: GET /api/option-chain-v3?type=Indices&symbol=NIFTY   │
│            &expiry=<dd-Mon-yyyy>                                │
│  4. Build ATM ± N strike band                                   │
│  5. Compare to previous local snapshot → Change OI              │
│  6. Write OptionChainDataSchema.json payload to disk            │
│  7. Print TradingView bridge block (paste fields)               │
└───────────────┬─────────────────────────────┬───────────────────┘
                │                             │
                ▼                             ▼
   option_chain_live.json          Console / watch loop
   option_chain_prev.json          (BUY CE/PE helper optional)
                │
                │  Bridge (choose one – see §5)
                ▼
┌─────────────────────────────────────────────────────────────────┐
│  TradingView Pine v6  (NiftyIntradayPro – later phases)         │
│  • Path TV-OI: request.security(…_OI) when feed exists          │
│  • Path Bridge: typed / text-area inputs refreshed by trader    │
│  • Path Hybrid: Pine = price action; Service = OI decision      │
└─────────────────────────────────────────────────────────────────┘
```

### NSE endpoints used (verified in this project)

| Step | URL | Notes |
|---|---|---|
| Session | `https://www.nseindia.com/option-chain` | Sets cookies; required first |
| Expiries | `https://www.nseindia.com/api/option-chain-contract-info?symbol=NIFTY` | `expiryDates` as `dd-Mon-yyyy` |
| Chain | `https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=…` | Needs expiry; without it returns `{}` |

**Do not use** the old `option-chain-indices` URL – it returns 404 (observed Nov 2026).

### Session, rate limits, stale data

| Concern | Policy |
|---|---|
| Session | Fresh cookie jar every process start; reuse opener for contract-info + chain in one run |
| Rate limit | Default poll **≥ 180 seconds**. Never faster than 60s. NSE / Akamai will Access-Deny aggressive clients |
| User-Agent | Desktop Chrome UA + `Referer: …/option-chain` |
| Stale | If `records.timestamp` age > `max_stale_seconds` (default 300), set `freshness = "stale"` and `usable_for_signals = false` |
| Empty / blocked | `status = "error"`, empty `strikes`, Pine must WAIT |
| Snapshot Change OI | Always vs **previous successful local file**, not vs chart candles |

---

## 2. Data schema

Canonical file: `OptionChainDataSchema.json`  
Live output file written by the service: `option_chain_live.json`  
Previous snapshot kept as: `option_chain_prev.json`

### Required top-level fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | e.g. `"1.0"` |
| `status` | string | `ok` \| `error` \| `partial` |
| `symbol` | string | `NIFTY` |
| `expiry` | string | NSE format `dd-Mon-yyyy` |
| `expiry_yymmdd` | string | TradingView style `261117` |
| `spot` | number | Index underlying value |
| `atm_strike` | number | Rounded to strike interval |
| `strike_interval` | number | 50 for NIFTY |
| `strike_range` | number | N in ATM ± N (3 / 5 / 10) |
| `chain_timestamp` | string | NSE `records.timestamp` |
| `fetched_at` | string | ISO local time when service wrote the file |
| `freshness` | string | `fresh` \| `stale` \| `unknown` |
| `usable_for_signals` | boolean | false if error/stale/missing band |
| `strikes[]` | array | One row per strike in the band |
| `aggregates` | object | Band totals, PCR, Change OI |
| `bridge` | object | Fields sized for Pine paste inputs |

### Per-strike row

| Field | Meaning |
|---|---|
| `strike` | Strike price |
| `ce_oi` / `pe_oi` | Open interest (contracts) |
| `ce_chg_day` / `pe_chg_day` | NSE `changeinOpenInterest` (session day change) |
| `ce_chg_snap` / `pe_chg_snap` | vs previous local snapshot (Phase 2 primary Change OI) |
| `ce_ltp` / `pe_ltp` | Last traded premium |
| `ce_ltp_chg_snap` / `pe_ltp_chg_snap` | Premium change vs previous snapshot |

### Aggregates (ATM ± N)

| Field | Formula |
|---|---|
| `total_ce_oi` | Σ CE OI |
| `total_pe_oi` | Σ PE OI |
| `pcr` | total_pe_oi / total_ce_oi (null if CE OI = 0) |
| `pcr_prev` | PCR from previous snapshot |
| `pcr_chg` | pcr − pcr_prev |
| `pcr_direction` | `rising` \| `falling` \| `sideways` \| `unknown` |
| `ce_chg_oi` / `pe_chg_oi` | Σ snapshot Change OI |
| `ce_chg_oi_pct` / `pe_chg_oi_pct` | vs previous band totals |
| `max_ce_oi_strike` | Resistance proxy |
| `max_pe_oi_strike` | Support proxy |

---

## 3. Update frequency

| Mode | Interval | Use |
|---|---|---|
| Default watch | **180 s** (3 minutes) | Matches 3m candle decision cadence |
| Manual | On demand | Before session / after large move |
| Minimum | 60 s | Only for debugging; risk of Access Denied |

Pine signals (later phases) use **confirmed 3m bar close**. OI inputs should be refreshed at least once per bar when using the bridge path.

---

## 4. Error handling contract

| Condition | Service behaviour | Pine must do (Phases 6–7) |
|---|---|---|
| HTTP / Access Denied | `status=error`, message set | Force WAIT; no CE/PE |
| Missing expiry / empty data | `status=error` | WAIT |
| Band incomplete (gaps) | `status=partial`, list missing strikes | WAIT if usable_for_signals false |
| Timestamp older than max_stale | `freshness=stale`, usable=false | WAIT |
| First run (no prev snapshot) | snap deltas = null; day chg still filled | Allow PCR level; mark Change OI unknown |
| Spot missing | error | WAIT |

**Never** invent OI, PCR, or LTP. Prefer `null` + WAIT over guesswork.

---

## 5. TradingView integration methods (feasible only)

### Method A – TradingView listed option / `_OI` symbols (native, limited)

- Tickers like `NSE:NIFTY261117C26000` and `NSE:NIFTY261117C26000_OI`.
- Pine: `request.security(sym, "1D", close, ignore_invalid_symbol=true)`.
- **Reality in this project:** many NSE option `_OI` series return empty → dashboard shows Unavailable.
- Suitable as a **fallback bias**, not as the primary live Change OI source.

### Method B – External service + Pine bridge inputs (recommended for this project)

1. Run `external_option_chain_service.py` (or `--watch 180`).
2. Service prints a **Bridge block** and writes `option_chain_live.json`.
3. Trader pastes into Pine inputs (Phase 8 dashboard / Phase 2 engine):
   - Near-ATM PCR
   - CE Change OI / PE Change OI (snapshot)
   - PCR direction
   - Chain timestamp / freshness flag
4. Pine combines those with EMA, volume, S/R on the chart.

This is **manual or semi-manual**. It is honest: data does not “stream” into Pine.

Optional later automation (out of Phase 1 scope): OS automation that copies bridge values – still not an NSE→Pine API.

### Method C – Hybrid decision support (already partially built)

- Service: OI/PCR → BUY CE / BUY PE / WAIT (see `nse_oi_runtime.py`).
- Pine: EMA + volume pivots + S/R + stretch/stop filters (`Options_Assistant_v3`).
- Trader takes a call only when **both** agree.

### Method D – Not supported (do not claim)

- Direct `request.http` / `fetch` from Pine to NSE  
- Webhook response into Pine  
- Uploading `option_chain_live.json` as a chart series  
- Inventing fake ticker names that look live  

### Method E – Commercial / vendor TV data (optional future)

If a vendor already publishes NIFTY OI as TradingView symbols, document those tickers and map them in Pine.  
Until verified with real non-empty series on your account, **do not** treat vendor symbols as live.

---

## 6. Symbol conventions (when using Method A)

| Item | Convention | Example |
|---|---|---|
| Index | `NSE:NIFTY` | spot chart |
| CE option | `NSE:{ROOT}{YYMMDD}C{STRIKE}` | `NSE:NIFTY261117C26000` |
| PE option | `NSE:{ROOT}{YYMMDD}P{STRIKE}` | `NSE:NIFTY261117P26000` |
| OI series | append `_OI` | `NSE:NIFTY261117C26000_OI` |
| Expiry | calendar `YYMMDD` | 17 Nov 2026 → `261117` |
| Strike | integer, no decimals for NIFTY 50-step | `26000` |

Publishing requirements for **custom** symbols: only TradingView / exchange data vendors can list new feeds. This project does **not** publish custom symbols.

---

## 7. Separation of concerns

| File | Role |
|---|---|
| `external_option_chain_service.py` | Phase 1 service – fetch + schema write |
| `OptionChainDataSchema.json` | Contract + JSON Schema + example payload |
| `option_chain_live.json` | Last successful payload (runtime, not source) |
| `option_chain_prev.json` | Previous payload for snapshot Change OI |
| `nse_oi_runtime.py` / `.ps1` | Existing research helper (kept); may call same APIs |
| `NiftyIntradayPro.pine` | Later phases – consumes bridge / TV-OI, never NSE HTTP |
| `OI_Strike_Band_v1.pine.txt` | TV `_OI` band viewer only; not the live NSE path |

---

## 8. Example option-chain response

See the `example_payload` object inside `OptionChainDataSchema.json`.  
That example is **illustrative**, shaped like a real NSE-derived snapshot (spot ~25045, expiry 17-Nov-2026). It is **not** a live feed and must not be hard-coded into the production Pine indicator as market data.

---

## 9. How to run the Phase 1 service

```text
# One shot
python external_option_chain_service.py --range 5

# Watch every 3 minutes
python external_option_chain_service.py --watch 180 --range 5

# Specific expiry
python external_option_chain_service.py --expiry 17-Nov-2026 --range 5
```

Requirements: Python 3.10+ from python.org with PATH enabled.  
If the shell gets Akamai **Access Denied**, open `https://www.nseindia.com/option-chain` in a normal browser first, or run the fetch from an environment that can hold NSE cookies (Cursor IDE browser worked previously when the shell did not).

---

## 10. Phase 1 acceptance criteria

- [x] Architecture documented without claiming NSE → Pine direct push  
- [x] Schema file with fields for spot, ATM, interval, CE/PE OI, Change OI, LTP, timestamp  
- [x] Service writes schema-compliant JSON and keeps previous snapshot  
- [x] Freshness / usable_for_signals flags defined  
- [x] TradingView bridge methods listed with honest limits  
- [ ] Live fetch validated on this PC (depends on NSE access + Python install)

**Phase 1 is architecturally complete.** Runtime validation of a live NSE pull on this machine is environment-dependent and is checked when Python + NSE access succeed.

---

## 11. What comes next

| Phase | Depends on this contract |
|---|---|
| 2 | Aggregates PCR ATM±5, snap Change OI, PCR direction (service + Pine bridge inputs) |
| 3 | Buildup / unwinding using strike LTP + OI deltas from schema |
| 4–5 | Pure Pine (EMA, volume pivots) |
| 6–7 | Score engine + 3m signals; refuse trade if `usable_for_signals` false |
| 8–9 | Dashboard + alerts including PCR / Change OI from bridge |
| 10 | Testing checklist including stale / missing OI |
