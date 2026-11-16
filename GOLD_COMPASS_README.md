# Gold Trade Compass — XAUUSD spot research prototype

**Standalone read-only research files** on `feature/gold-compass-spot-h1-m15`. Nothing here connects to FundingPips/MT5, submits orders, enables broker EA, sends alerts, or depends on TradingView/Pine.

## Completed code

- `gold_compass/engine.py`: H1 closed-candle EMA20 directional bias, M15 breakout/retest/closed confirmation, M5 *post-M15-close* confirmation on prior M5 extreme. M5 is an additional gate, not a new setup.
- `gold_compass/dukascopy.py`: downloads **HISTORICAL ONLY** paired spot XAUUSD 1m BID and ASK public Dukascopy candle files; validates LZMA / 24-byte record, UTC timestamps and point scale 1000; builds H1/M15/M5 from the same complete 1-minute buckets. Missing minutes are never fabricated. No API token or account needed. 14 calendar days max per request.
- `gold_compass/import_csv.py`: offline import of a 1-minute independent spot BID/ASK CSV with explicitly UTC timestamps; provenance is user-supplied and must be verified separately.
- `gold_compass/backtest.py`: conservative offline bar replay. Decision at M5 close, simulated entry at NEXT M5 open, BUY pays ASK/SELL receives BID; BUY exits use BID, SELL exits use ASK; stop wins when one bar hits both stop and TP. Tracks SL, TP, SL_AMBIGUOUS, TIMEOUT, P/L, risk and UTC daily limits.
- `gold_compass/viewer.html`: Arabic responsive **local file** report viewer. Upload a JSON report from your device. It is NOT a live price dashboard; the file is parsed locally in your browser and is not uploaded to us.
- `tests/test_gold_compass.py` and `tests/test_gold_compass_data.py`: tests for inputs, M5 after M15 close, decoded BID/ASK data, incomplete-bar rejection, and stop-first backtest logic.

## Offline research workflow (no broker credentials)

Python 3.12+ and standard library only are enough for these new modules.

1. Download completed UTC historical dates (e.g. not today). 14-day maximum, one batch at a time. Real attempts on 2026-11-16 timed out / returned HTTP 503 from GitHub Actions, including after retries; this source is currently UNRELIABLE in that environment. Remote historical endpoints may fail or throttle. **Never treat such failure as an empty signal.**
   `python -m gold_compass.dukascopy --start 2026-11-02 --end 2026-11-15 --out gold_spot_history.json`
2. Replay conservative research (no execution):
   `python -m gold_compass.backtest gold_spot_history.json --out gold_backtest_report.json`
Alternative when the provider returns HTTP 503: import a real, independently obtained M1 historical Spot XAUUSD BID/ASK CSV with the exact columns:
`timestamp,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close`
Timestamps must have a timezone offset or Z. Files are **not assumed authentic** by merely supplying these columns.

`python -m gold_compass.import_csv spot_bid_ask_m1.csv --out gold_spot_csv_history.json`
`python -m gold_compass.backtest gold_spot_csv_history.json --out gold_backtest_report.json`

3. Open `gold_compass/viewer.html` locally in a browser and choose the generated `gold_backtest_report.json`.
4. Run unit tests locally:
   `python -m pytest tests/test_gold_compass.py tests/test_gold_compass_data.py -q`

## What is NOT claimed or implemented

- **GitHub Actions ran the offline test suite successfully (19 tests passing on commit 7cff390), but real historical Dukascopy attempts failed with timeout / HTTP 503. No real-data backtest or validated win rate exists yet.** Automatic provider smoke attempts are now disabled to avoid hammering the provider.
- No verified live spot feed or real-time monitoring; no Telegram or iPhone notifications; no published website; no broker access.
- This is **not** the final user-approved A+ strategy. The example stop uses M15 retest structure and TP uses a hypothetical 2R target, which has not been validated as an available nearby target.
- Backtest dollar P/L assumes 100 troy ounces per standard lot and a 0.07 lot size. It estimates $0.10/oz adverse slippage at entry and exit and $7 per standard lot round trip in illustrative fees; these are NOT verified FundingPips prices or commissions. Results should not be used to make real-money decisions until source accuracy, contract specifications, intrabar gaps, practical fills, data coverage, and independent held-out performance are validated.
- The first-trade +$70 feasibility rule, realistic news/session control, and full FundingPips rules are NOT implemented. A 2R hypothetical target under a $30 risk cap may never meet the $70 first-trade constraint; **do not describe this replay as fully compliant**.
- Data provenance is based on download URLs and local file metadata, not a cryptographic guarantee. The core engine checks a caller-supplied source tag, which is NOT sufficient on its own to prove data authenticity.
- Existing `fxbot` and its optional automated trading/scanning workflows are not changed. Do **not** enable its broker connectors or trading features for FundingPips.
- The draft Pull Request stays unmerged until tests and quality checks are completed.

## Data caveats

Dukascopy provides a distinct historical quote stream. The platform uses source-specific prices that may differ from FundingPips or Exness. Historical BID and ASK must be compared with other trusted sources, and missing/partial days and rate limits must fail visibly. M15/M5 candle timestamps are **bar opens in UTC**, not decision times. Same-bar stop+target outcomes are conservatively classified as SL_AMBIGUOUS.
