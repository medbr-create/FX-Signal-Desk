"""Gold Trade Compass: strictly read-only XAUUSD H1/M15 review engine.

Independent from the original FX Signal Desk scanner and all broker adapters.
No network calls, trading orders, account access, or notifications in this module.
Candle timestamps are UTC *opening* times; the current candle is never analyzed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

SOURCE = "dukascopy_spot_bid"
SYMBOL = "XAUUSD"
LOT = 0.07
OZ_PER_LOT_ASSUMPTION = 100.0  # Check this against the broker contract BEFORE using any amounts.
RISK_USD_CAP = 30.0
ABSOLUTE_RISK_USD_LIMIT = 45.0
MIN_RR = 2.0
LOOKBACK = 12


def _utc(value: Any) -> datetime:
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(float(value) / (1000 if value > 1e11 else 1), tz=timezone.utc)
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Candle timestamp must include timezone")
    return dt.astimezone(timezone.utc)


def _closed(rows: list[dict], length: int, now: datetime) -> list[dict]:
    out = {}
    for row in rows:
        ts = _utc(row["timestamp"])
        v = {name: float(row[name]) for name in ("open", "high", "low", "close")}
        if not all(isfinite(x) and x > 0 for x in v.values()):
            raise ValueError("Non-finite or non-positive OHLC")
        if v["high"] < max(v["open"], v["close"], v["low"]) or v["low"] > min(v["open"], v["close"], v["high"]):
            raise ValueError("Invalid high/low in market data")
        if ts + timedelta(minutes=length) <= now:
            out[ts] = {"timestamp": ts, **v}
    ordered = [out[t] for t in sorted(out)]
    if any((b["timestamp"] - a["timestamp"]) < timedelta(minutes=length) for a, b in zip(ordered, ordered[1:])):
        raise ValueError("Overlapping candle timestamps")
    return ordered


def _ema(closes: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1)
    level = sum(closes[:period]) / period
    values = [level]
    for price in closes[period:]:
        level += alpha * (price - level)
        values.append(level)
    return values


def _atr(bars: list[dict], n: int = 14) -> float:
    tr = []
    for before, current in zip(bars[-n - 1:-1], bars[-n:]):
        tr.append(max(current["high"] - current["low"],
                      abs(current["high"] - before["close"]),
                      abs(current["low"] - before["close"])))
    return sum(tr) / len(tr) if tr else 0.0


def _wait(reason: str, **kwargs) -> dict:
    return {"state": "WAIT", "reason": reason, **kwargs}


def analyze(payload: dict, now: datetime | str | None = None) -> dict:
    """Return a REVIEW candidate only after an H1 bias and M15 breakout/retest/confirmation.

    This is an exploratory deterministic filter, NOT a validated profitable strategy.
    'tp' is a hypothetical RR target, never asserted to be a structural target.
    """
    now = _utc(now or datetime.now(timezone.utc))
    if payload.get("source") != SOURCE or payload.get("symbol") != SYMBOL:
        return {"state": "NO_DATA", "reason": "Only explicitly identified spot XAUUSD Dukascopy BID data accepted"}
    try:
        h1 = _closed(payload.get("h1", []), 60, now)
        m15 = _closed(payload.get("m15", []), 15, now)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return {"state": "NO_DATA", "reason": f"Rejected malformed candles: {exc}"}
    if len(h1) < 45 or len(m15) < 45:
        return {"state": "NO_DATA", "reason": "Insufficient CLOSED candles", "h1_bars": len(h1), "m15_bars": len(m15)}
    h_age = (now - (h1[-1]["timestamp"] + timedelta(hours=1))).total_seconds() / 60
    m_age = (now - (m15[-1]["timestamp"] + timedelta(minutes=15))).total_seconds() / 60
    if h_age > 75 or m_age > 30:
        return {"state": "NO_DATA", "reason": "Stale prices: no current analysis", "h1_age_min": round(h_age, 1), "m15_age_min": round(m_age, 1)}
    closes = [b["close"] for b in h1]
    current_ema, last_ema = _ema(closes, 20)[-2:]
    side = "BUY" if closes[-1] > last_ema > current_ema else ("SELL" if closes[-1] < last_ema < current_ema else None)
    if side is None:
        return _wait("H1 has no aligned close and rising/falling EMA20")
    a = _atr(m15)
    if not a:
        return _wait("M15 ATR unavailable")
    last_idx = len(m15) - 1
    confirm, retest = m15[last_idx], m15[last_idx - 1]
    body = abs(confirm["close"] - confirm["open"])
    if body < 0.25 * a:
        return _wait("M15 confirmation candle body too small", bias=side)
    if side == "BUY" and not (confirm["close"] > confirm["open"] and confirm["close"] > retest["high"] + 0.02 * a):
        return _wait("No CLOSED M15 bullish confirmation after retest", bias=side)
    if side == "SELL" and not (confirm["close"] < confirm["open"] and confirm["close"] < retest["low"] - 0.02 * a):
        return _wait("No CLOSED M15 bearish confirmation after retest", bias=side)

    # Find a fresh breakout from an earlier, frozen 12-bar reference level.
    # Retest must occur on the bar immediately before the confirming bar.
    for i in range(last_idx - 2, max(LOOKBACK - 1, last_idx - 9), -1):
        prior = m15[i - LOOKBACK:i]
        if len(prior) != LOOKBACK:
            continue
        b = m15[i]
        level = max(x["high"] for x in prior) if side == "BUY" else min(x["low"] for x in prior)
        tol = a * 0.30
        if side == "BUY":
            broken = b["close"] > level + a * 0.04 and b["open"] <= level + tol
            touched = retest["low"] <= level + tol and retest["high"] >= level - tol
            valid_retest = retest["close"] >= level - tol
            intervening = all(x["close"] >= level - tol for x in m15[i + 1:last_idx])
            sl = retest["low"] - 0.15 * a
        else:
            broken = b["close"] < level - a * 0.04 and b["open"] >= level - tol
            touched = retest["high"] >= level - tol and retest["low"] <= level + tol
            valid_retest = retest["close"] <= level + tol
            intervening = all(x["close"] <= level + tol for x in m15[i + 1:last_idx])
            sl = retest["high"] + 0.15 * a
        if not (broken and touched and valid_retest and intervening):
            continue
        entry = confirm["close"]
        stop_distance = (entry - sl) if side == "BUY" else (sl - entry)
        if stop_distance <= 0:
            return _wait("Invalid structural stop", bias=side)
        risk = stop_distance * LOT * OZ_PER_LOT_ASSUMPTION
        if risk > ABSOLUTE_RISK_USD_LIMIT:
            return _wait("Absolute risk cap exceeded", bias=side, estimated_risk_usd=round(risk, 2))
        if risk > RISK_USD_CAP:
            return _wait("Default $30 risk cap exceeded; do not increase lots", bias=side, estimated_risk_usd=round(risk, 2))
        # Structural target must be validated independently before real trading.
        hypothetical_tp = entry + (1 if side == "BUY" else -1) * stop_distance * MIN_RR
        return {
            "state": "REVIEW", "direction": side, "symbol": SYMBOL, "source": SOURCE,
            "last_closed_m15_utc": (confirm["timestamp"] + timedelta(minutes=15)).isoformat(),
            "setup_id": f"{side}:{b['timestamp'].isoformat()}:{level:.3f}",
            "entry_reference": round(entry, 3), "sl_reference": round(sl, 3),
            "hypothetical_rr2_tp": round(hypothetical_tp, 3),
            "estimated_risk_usd": round(risk, 2), "assumed_lot": LOT,
            "breakout_level": round(level, 3), "h1_bias": side,
            "warnings": ["REVIEW only: no orders or automatic alerts", "Target is a hypothetical 2R example, not a proven structural TP",
                         "Spread, slippage, commission and actual contract specifications are NOT accounted for"],
        }
    return _wait("No valid M15 breakout + retest before latest closed confirmation", bias=side)


if __name__ == "__main__":
    raise SystemExit("Run: python -m gold_compass.scan FILE.json")
