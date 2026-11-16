"""Import fully documented, independently sourced historical XAUUSD M1 BID and ASK CSV.

Offline only; no broker account login, no MT5, no external network or order API.
The file's source and values are assertions from the user, not authenticated market data.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path

from .dukascopy import make_bars, pair_minutes
from .engine import CSV_SOURCE

REQUIRED = ("timestamp", "bid_open", "bid_high", "bid_low", "bid_close",
            "ask_open", "ask_high", "ask_low", "ask_close")


def _quote(row: dict[str, str], prefix: str, stamp: str) -> dict:
    bar = {k: float(row[f"{prefix}_{k}"]) for k in ("open", "high", "low", "close")}
    if (not all(isfinite(v) and v > 0 for v in bar.values())
        or bar["high"] < max(bar["open"], bar["close"])
        or bar["low"] > min(bar["open"], bar["close"])):
        raise ValueError(f"Invalid {prefix.upper()} OHLC at {stamp}")
    return {"timestamp": stamp, **bar}


def from_csv_bytes(data: bytes) -> dict:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded") from exc
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames or any(name not in reader.fieldnames for name in REQUIRED):
        raise ValueError("CSV requires timestamp and BID/ASK open/high/low/close columns")
    bid, ask = [], []
    seen = set()
    for line, row in enumerate(reader, start=2):
        if any(row.get(k) in (None, "") for k in REQUIRED):
            raise ValueError(f"Missing BID/ASK values on CSV line {line}")
        try:
            t = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid timestamp on line {line}") from exc
        if t.tzinfo is None:
            raise ValueError("All timestamps must contain Z or a timezone offset; never guess broker timezone")
        t = t.astimezone(timezone.utc)
        if t.second != 0 or t.microsecond != 0:
            raise ValueError(f"Expected 1-minute timestamp at line {line}")
        if t > datetime.now(timezone.utc):
            raise ValueError("Future timestamps not permitted")
        stamp = t.isoformat()
        if stamp in seen:
            raise ValueError(f"Duplicate timestamp on line {line}")
        seen.add(stamp)
        b, a = _quote(row, "bid", stamp), _quote(row, "ask", stamp)
        if any(a[x] < b[x] for x in ("open", "close")):
            raise ValueError(f"ASK price lower than BID on line {line}")
        bid.append(b)
        ask.append(a)
    if not bid:
        raise ValueError("Empty historical CSV")
    minutes = pair_minutes(bid, ask)
    if any((datetime.fromisoformat(b["timestamp"]) - datetime.fromisoformat(a["timestamp"])) <
           timedelta(minutes=1) for a, b in zip(minutes, minutes[1:])):
        raise ValueError("Overlapping/unsorted one-minute rows")
    return {
        "source": CSV_SOURCE, "symbol": "XAUUSD", "quote_basis": "bid_ohlc_with_ask",
        "mode": "HISTORICAL_ONLY", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": sha256(data).hexdigest(),
        "source_disclaimer": "User-supplied data, externally unverified",
        "m1": minutes, "m5": make_bars(minutes, 5),
        "m15": make_bars(minutes, 15), "h1": make_bars(minutes, 60)
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Offline spot XAUUSD bid/ask M1 CSV importer")
    p.add_argument("csv_file")
    p.add_argument("--out", default="gold_spot_csv_history.json")
    args = p.parse_args()
    payload = from_csv_bytes(Path(args.csv_file).read_bytes())
    dest = Path(args.out)
    if dest.exists():
        raise FileExistsError(f"Refusing to overwrite: {dest}")
    dest.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Imported {len(payload['m1'])} paired minutes -> "
          f"{len(payload['m5'])} M5, {len(payload['m15'])} M15, {len(payload['h1'])} H1. "
          "Research only; spot source unverified.")


if __name__ == "__main__":
    main()
