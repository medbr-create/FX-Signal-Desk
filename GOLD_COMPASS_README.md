# Gold Trade Compass — Safe experimental prototype

This is an **independent, offline, read-only** H1/M15 prototype on the branch `feature/gold-compass-spot-h1-m15`.

## What exists

- Deterministic closed-candle H1 EMA20 direction and M15 breakout -> retest -> confirmation candidate review.
- Freshness guard and OHLC validation; no future/incomplete candle use.
- Conservative fixed 0.07 standard-lot **assumption** with a default $30 estimate cap and absolute $45 cap.
- Read-only JSON CLI: `python -m gold_compass.scan OHLC.json`.
- Unit tests: `python -m unittest tests.test_gold_compass -v`.
- A `REVIEW` result is **not a confirmed trade**; it requires independent structural target, quote, costs, contract and broker rule checks.

## Data input

**No spot price API has been connected yet.** A real trusted provider or a verified historical export is required, and you must verify whether the values are spot BID vs ASK vs MID. This module deliberately rejects the original Yahoo `GC=F` futures symbol pretending to be XAUUSD.

Input JSON example structure (replace entries with **real, UTC, time-zone-tagged** candle open times):
```json
{
  "source": "dukascopy_spot_bid",
  "symbol": "XAUUSD",
  "h1": [{"timestamp":"2026-11-16T15:00:00Z","open":4000,"high":4002,"low":3997,"close":4001}],
  "m15": [{"timestamp":"2026-11-16T15:45:00Z","open":4000,"high":4001,"low":3998,"close":4000.5}]
}
```
The example is intentionally too short to produce a review. Supply 45+ **fully closed** candles per timeframe. Candle timestamps mean **bar start**, not bar close. The exact `source` tag is a provenance assertion by the caller, not an independent authenticity check.

## Important restrictions

- The original `fxbot` workflows/auto-trading are **not changed or enabled** by this prototype. Do not use them for FundingPips.
- No MT5/FundingPips login, password, EA, account connection, automation or order routing.
- No live alerts, hosting or performance claim yet.
- No strategy backtest or win rate yet. First validate historical spot data, quote basis, spreads, commissions, slippage, tick size, and any same-bar ambiguity.
- Keep **TradingView / Pine Script** out of this implementation.
- Synthetic tests only establish code behavior, **not trading profitability**.

The module intentionally prioritizes a reliable input/analysis boundary over an attractive but misleading live interface.
