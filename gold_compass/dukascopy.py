"""Historical XAUUSD spot BID+ASK one-minute Dukascopy importer.

READ ONLY. Fetches public Dukascopy historical candle files without broker credentials.
No trading, alerts, strategies, MT5 or FundingPips access.
Data is for research/backtesting, not a guaranteed real-time feed.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import lzma
from pathlib import Path
import struct
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .engine import SOURCE

RECORD = struct.Struct(">IIIIIf")  # seconds, OPEN, CLOSE, LOW, HIGH, volume
PRICE_SCALE = 1000.0               # Dukascopy XAUUSD price integer points
BASE = "https://datafeed.dukascopy.com/datafeed/XAUUSD"
MAX_DAYS = 14
MINUTE = timedelta(minutes=1)


def _validate_date_range(start: date, end: date) -> None:
    if end < start:
        raise ValueError("End day must be on or after start day")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError(f"At most {MAX_DAYS} historical calendar days per request")
    if end >= datetime.now(timezone.utc).date():
        raise ValueError("Only fully completed past UTC days are supported, not live/today")


def candle_url(day: date, side: str) -> str:
    if side not in ("BID", "ASK"):
        raise ValueError("Side must be BID or ASK")
    return f"{BASE}/{day:%Y}/{day.month - 1:02d}/{day:%d}/{side}_candles_min_1.bi5"


def _decode(compressed: bytes, day: date) -> list[dict]:
    if not compressed:
        return []
    try:
        raw = lzma.decompress(compressed)
    except lzma.LZMAError as exc:
        raise ValueError(f"Invalid compressed Dukascopy candles: {exc}") from exc
    if len(raw) % RECORD.size:
        raise ValueError("Corrupt Dukascopy candle file: nonintegral 24-byte records")
    base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    out: dict[datetime, dict] = {}
    for offset in range(0, len(raw), RECORD.size):
        secs, op, cl, lo, hi, volume = RECORD.unpack_from(raw, offset)
        if secs >= 86400 or secs % 60:
            raise ValueError("Unexpected candle timestamp in Dukascopy data")
        prices = [v / PRICE_SCALE for v in (op, cl, lo, hi)]
        if not (100.0 < min(prices) <= max(prices) < 20000.0):
            raise ValueError("Out-of-range XAUUSD prices: verify the integer point scale")
        if hi < max(op, cl, lo) or lo > min(op, cl, hi):
            raise ValueError("Invalid Dukascopy candle OHLC")
        ts = base + timedelta(seconds=secs)
        if ts in out:
            raise ValueError("Duplicate minute candle")
        out[ts] = dict(timestamp=ts.isoformat(), open=prices[0], high=prices[3],
                       low=prices[2], close=prices[1], volume=float(volume))
    return [out[k] for k in sorted(out)]


def download_day(day: date, side: str, *, timeout: int = 28, attempts: int = 3) -> tuple[list[dict] | None, str]:
    """Retry transient failures; a 404 means no quoted data that day, never fake prices."""
    url = candle_url(day, side)
    request = Request(url, headers={"User-Agent": "GoldCompassResearch/0.1"})
    if not 1 <= attempts <= 5:
        raise ValueError("attempts must be between 1 and 5")
    last_error = None
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                compressed = response.read()
            return _decode(compressed, day), url
        except HTTPError as exc:
            if exc.code == 404:
                return None, url
            if exc.code not in (408, 429, 500, 502, 503, 504):
                raise RuntimeError(f"Dukascopy {side} {day} HTTP {exc.code}") from exc
            last_error = f"HTTP {exc.code}"
        except (URLError, TimeoutError, ConnectionError) as exc:
            last_error = str(exc)
        if attempt < attempts - 1:
            # Respect public provider by backing off after a temporary rate-limit/outage.
            time.sleep(min(2 ** (attempt + 1), 8))
    raise RuntimeError(f"Dukascopy {side} {day} unavailable after {attempts} tries: {last_error}")


def pair_minutes(bid: list[dict], ask: list[dict]) -> list[dict]:
    """Join only timestamps with BID and ASK; never manufacture missing quote sides."""
    bmap = {r["timestamp"]: r for r in bid}
    amap = {r["timestamp"]: r for r in ask}
    common = sorted(set(bmap) & set(amap))
    if bid and ask and len(common) < 0.95 * min(len(bid), len(ask)):
        raise ValueError("Bid/ask timestamps mismatch on >5% of candles")
    out = []
    for ts in common:
        b, a = bmap[ts], amap[ts]
        if a["close"] < b["close"] or a["low"] > a["high"]:
            raise ValueError("Impossible BID/ASK prices")
        out.append({"timestamp": ts, "bid": b, "ask": a,
                    "spread_close": round(a["close"] - b["close"], 4)})
    return out


def _floor_time(dt: datetime, step: int) -> datetime:
    return dt.replace(minute=(dt.minute // step) * step, second=0, microsecond=0)


def make_bars(minute_quotes: list[dict], step: int) -> list[dict]:
    """Aggregate *complete* UTC buckets; missing minutes reject that bucket."""
    if step not in (5, 15, 60):
        raise ValueError("Only 5, 15 or 60 minute bars supported")
    groups: dict[datetime, list[dict]] = {}
    for q in minute_quotes:
        ts = datetime.fromisoformat(q["timestamp"])
        groups.setdefault(_floor_time(ts, step), []).append(q)
    out = []
    for start in sorted(groups):
        rows = sorted(groups[start], key=lambda r: r["timestamp"])
        expected = [start + timedelta(minutes=i) for i in range(step)]
        if [datetime.fromisoformat(r["timestamp"]) for r in rows] != expected:
            continue
        bids = [r["bid"] for r in rows]
        asks = [r["ask"] for r in rows]
        out.append({
            "timestamp": start.isoformat(),
            "open": bids[0]["open"],
            "high": max(r["high"] for r in bids),
            "low": min(r["low"] for r in bids),
            "close": bids[-1]["close"],
            "ask_open": asks[0]["open"],
            "ask_high": max(r["high"] for r in asks),
            "ask_low": min(r["low"] for r in asks),
            "ask_close": asks[-1]["close"],
            "spread_close": round(asks[-1]["close"] - bids[-1]["close"], 4),
        })
    return out


def build_historical_payload(start: date, end: date) -> dict:
    _validate_date_range(start, end)
    minutes = []
    used_urls = []
    skipped = []
    day = start
    while day <= end:
        bid, bid_url = download_day(day, "BID")
        ask, ask_url = download_day(day, "ASK")
        if bid is None and ask is None:
            skipped.append(day.isoformat())
        elif bid is None or ask is None:
            raise RuntimeError(f"Only one quote side downloaded for {day}; rejecting partial day")
        else:
            minutes.extend(pair_minutes(bid, ask))
            used_urls.extend([bid_url, ask_url])
        day += timedelta(days=1)
    if not minutes:
        raise RuntimeError("No paired historical XAUUSD bid/ask minutes available in date range")
    return {
        "source": SOURCE, "symbol": "XAUUSD", "quote_basis": "bid_ohlc_with_ask",
        "mode": "HISTORICAL_ONLY", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "source_urls": used_urls, "missing_days": skipped,
        "m1": minutes, "m5": make_bars(minutes, 5),
        "m15": make_bars(minutes, 15), "h1": make_bars(minutes, 60),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Research-only XAUUSD BID/ASK historical importer")
    p.add_argument("--start", required=True, help="Past UTC YYYY-MM-DD")
    p.add_argument("--end", required=True, help="Past UTC YYYY-MM-DD")
    p.add_argument("--out", default="gold_spot_history.json")
    args = p.parse_args()
    payload = build_historical_payload(date.fromisoformat(args.start), date.fromisoformat(args.end))
    output = Path(args.out)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {output}")
    output.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(payload['m1'])} paired M1 candles; {len(payload['m5'])} M5, "
          f"{len(payload['m15'])} M15, {len(payload['h1'])} H1 to {output}")


if __name__ == "__main__":
    main()
