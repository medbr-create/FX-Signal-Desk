"""Gold Trade Compass: strictly read-only XAUUSD H1/M15/M5 review engine.

Independent from the original FX Signal Desk scanner and all broker adapters.
No network calls, trading orders, account access, or notifications in this module.
Candle timestamps are UTC *opening* times; the current candle is never analyzed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

SOURCE = "dukascopy_spot_bid"
CSV_SOURCE = "user_supplied_bid_ask_csv"
ALLOWED_SOURCES = frozenset({SOURCE, CSV_SOURCE})
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
    """Return REVIEW only after closed H1 bias, M15 setup, and M5 entry confirmation.

    This is an exploratory deterministic filter, NOT a validated profitable strategy.
    'tp' is a hypothetical RR target, never asserted to be a structural target.
    """
    now = _utc(now or datetime.now(timezone.utc))
    if payload.get("source") not in ALLOWED_SOURCES or payload.get("symbol") != SYMBOL:
        return {"state": "NO_DATA", "reason": "Only independently labeled Spot XAUUSD Dukascopy or user-supplied BID/ASK CSV data accepted"}
    try:
        h1 = _closed(payload.get("h1", []), 60, now)
        m15 = _closed(payload.get("m15", []), 15, now)
        m5 = _closed(payload.get("m5", []), 5, now)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return {"state": "NO_DATA", "reason": f"Rejected malformed candles: {exc}"}
    if len(h1) < 45 or len(m15) < 45 or len(m5) < 45:
        return {"state": "NO_DATA", "reason": "Insufficient CLOSED candles",
                "h1_bars": len(h1), "m15_bars": len(m15), "m5_bars": len(m5)}
    h_age = (now - (h1[-1]["timestamp"] + timedelta(hours=1))).total_seconds() / 60
    m_age = (now - (m15[-1]["timestamp"] + timedelta(minutes=15))).total_seconds() / 60
    five_age = (now - (m5[-1]["timestamp"] + timedelta(minutes=5))).total_seconds() / 60
    if h_age > 75 or m_age > 30 or five_age > 10:
        return {"state": "NO_DATA", "reason": "Stale prices: no current analysis",
                "h1_age_min": round(h_age, 1), "m15_age_min": round(m_age, 1),
                "m5_age_min": round(five_age, 1)}
    closes = [b["close"] for b in h1]
    current_ema, last_ema = _ema(closes, 20)[-2:]
    side = "BUY" if closes[-1] > last_ema > current_ema else ("SELL" if closes[-1] < last_ema < current_ema else None)
    if side is None:
        return _wait("H1 has no aligned close and rising/falling EMA20")
    a = _atr(m15)
    a5 = _atr(m5)
    if not a or not a5:
        return _wait("M15 / M5 ATR unavailable")
    m15_close_time = m15[-1]["timestamp"] + timedelta(minutes=15)
    m5_close_time = m5[-1]["timestamp"] + timedelta(minutes=5)
    if m5_close_time <= m15_close_time:
        return _wait("M15 setup is ready; waiting for a fully CLOSED M5 trigger")
    if m5_close_time > m15_close_time + timedelta(minutes=15):
        return _wait("M5 trigger window expired; wait for new M15 confirmation")
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
        trigger, previous5 = m5[-1], m5[-2]
        trigger_body = abs(trigger["close"] - trigger["open"])
        if trigger_body < 0.25 * a5:
            return _wait("M5 trigger candle body too small", bias=side)
        if side == "BUY":
            triggered = trigger["close"] > trigger["open"] and trigger["close"] > previous5["high"] + 0.02 * a5
            holding = trigger["close"] >= level - tol
        else:
            triggered = trigger["close"] < trigger["open"] and trigger["close"] < previous5["low"] - 0.02 * a5
            holding = trigger["close"] <= level + tol
        if not holding:
            return _wait("M5 closed back beyond M15 breakout zone", bias=side)
        if not triggered:
            return _wait("Waiting for M5 close through previous M5 high/low", bias=side)
        # M5 refines timing, but does NOT silently widen the structural M15 SL.
        entry = trigger["close"]
        original_risk = ((confirm["close"] - sl) if side == "BUY" else (sl - confirm["close"]))
        if original_risk <= 0:
            return _wait("Invalid M15 reference stop", bias=side)
        moved = ((entry - confirm["close"]) if side == "BUY" else (confirm["close"] - entry))
        if moved > 0.50 * original_risk:
            return _wait("M5 entry chased more than 0.5R from M15 confirmation", bias=side)
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
            "state": "REVIEW", "direction": side, "symbol": SYMBOL, "source": payload["source"],
            "last_closed_m15_utc": m15_close_time.isoformat(),
            "last_closed_m5_utc": m5_close_time.isoformat(),
            "m15_setup_reference": round(confirm["close"], 3),
            "m5_trigger": "closed candle break of previous M5 extreme",
            "setup_id": f"{side}:{b['timestamp'].isoformat()}:{level:.3f}",
            "entry_reference": round(entry, 3), "sl_reference": round(sl, 3),
            "hypothetical_rr2_tp": round(hypothetical_tp, 3),
            "estimated_risk_usd": round(risk, 2), "assumed_lot": LOT,
            "breakout_level": round(level, 3), "h1_bias": side,
            "warnings": ["REVIEW only: no orders or automatic alerts", "M5 confirmation is an unvalidated entry filter",
                         "Target is a hypothetical 2R example, not a proven structural TP",
                         "Spread, slippage, commission and actual contract specifications are NOT accounted for"],
        }
    return _wait("No valid M15 breakout + retest before latest closed confirmation; M5 cannot create a setup", bias=side)


if __name__ == "__main__":
    raise SystemExit("Run: python -m gold_compass.scan FILE.json")
