"""Conservative OFFLINE research replay on paired BID/ASK M5 candles.

This module never accesses a broker, sends alerts, or executes orders.
Results evaluate a *hypothetical* 2R research target, NOT the user's final
A+ discretionary strategy or its $70 first-trade requirement.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import json
from math import isfinite

from .engine import SOURCE, LOT, OZ_PER_LOT_ASSUMPTION, RISK_USD_CAP, analyze


def _close_time(bar: dict, minutes: int) -> datetime:
    return datetime.fromisoformat(bar["timestamp"].replace("Z", "+00:00")) + timedelta(minutes=minutes)


def _check_asks(payload: dict) -> None:
    if payload.get("source") != SOURCE or payload.get("symbol") != "XAUUSD":
        raise ValueError("Backtest requires Dukascopy Spot XAUUSD labeled candles")
    if payload.get("quote_basis") != "bid_ohlc_with_ask" or payload.get("mode") != "HISTORICAL_ONLY":
        raise ValueError("Backtest requires verified-form paired historical BID and ASK OHLC")
    for key in ("h1", "m15", "m5"):
        bars = payload.get(key)
        if not isinstance(bars, list):
            raise ValueError(f"Missing {key} candles")
        for row in bars:
            for name in ("open", "high", "low", "close", "ask_open", "ask_high", "ask_low", "ask_close"):
                if name not in row or not isfinite(float(row[name])) or float(row[name]) <= 0:
                    raise ValueError(f"Missing or invalid {key}.{name}")
            if row["ask_close"] < row["close"]:
                raise ValueError("ASK close lower than BID close")
    if len(payload["m5"]) < 100 or len(payload["h1"]) < 45:
        raise ValueError("Insufficient historical data to backtest")


def _resolve_trade(bars: list[dict], start: int, side: str, entry: float,
                   sl: float, target: float, max_holding: int, exit_slippage: float) -> dict:
    """Conservatively resolve BUY on BID exits, SELL on ASK exits.

    Ambiguous stop+target in the same OHLC bar is classified SL_AMBIGUOUS.
    """
    jmax = min(start + max_holding, len(bars))
    for j in range(start, jmax):
        b = bars[j]
        if side == "BUY":
            stop_hit, target_hit = b["low"] <= sl, b["high"] >= target
            bad_price, good_price = sl - exit_slippage, target - exit_slippage
        else:
            stop_hit, target_hit = b["ask_high"] >= sl, b["ask_low"] <= target
            bad_price, good_price = sl + exit_slippage, target + exit_slippage
        if stop_hit:
            return {"exit_idx": j, "exit_price": bad_price,
                    "outcome": "SL_AMBIGUOUS" if target_hit else "SL"}
        if target_hit:
            return {"exit_idx": j, "exit_price": good_price, "outcome": "TP"}
    j = jmax - 1
    if side == "BUY":
        price = bars[j]["close"] - exit_slippage
    else:
        price = bars[j]["ask_close"] + exit_slippage
    return {"exit_idx": j, "exit_price": price, "outcome": "TIMEOUT"}


def backtest(payload: dict, *, max_holding_bars: int = 24,
             entry_slippage_usd_oz: float = 0.10,
             exit_slippage_usd_oz: float = 0.10,
             commission_usd_per_lot_roundtrip: float = 7.0) -> dict:
    """Backtest from only closed price bars, fill at NEXT M5 open with quote-side spread.

    Prices are historical spot BID/ASK quotes, not FundingPips execution.
    Default execution costs are *illustrative*, not the user's actual fees.
    """
    _check_asks(payload)
    if (not (1 <= max_holding_bars <= 288)
        or any(not isfinite(float(v)) or float(v) < 0 for v in
               (entry_slippage_usd_oz, exit_slippage_usd_oz, commission_usd_per_lot_roundtrip))):
        raise ValueError("Invalid execution-cost or holding settings")
    m5 = sorted(payload["m5"], key=lambda r: r["timestamp"])
    m15, h1 = payload["m15"], payload["h1"]
    taken = []
    seen = set()
    reason_counts: dict[str, int] = {}
    day = None
    trades_today = 0
    consecutive_losses = 0
    i = 0
    while i < len(m5) - 1:
        decision = _close_time(m5[i], 5)
        if day != decision.date():
            day = decision.date()
            trades_today, consecutive_losses = 0, 0
        if trades_today >= 4 or consecutive_losses >= 2:
            i += 1
            continue
        # Send only historical information visible at decision time.
        snapshot = {
            "source": SOURCE, "symbol": "XAUUSD",
            "h1": [b for b in h1 if _close_time(b, 60) <= decision][-260:],
            "m15": [b for b in m15 if _close_time(b, 15) <= decision][-300:],
            "m5": m5[max(0, i - 400):i + 1],
        }
        result = analyze(snapshot, now=decision)
        if result.get("state") != "REVIEW":
            reason = result.get("reason", result.get("state", "UNKNOWN"))
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            i += 1
            continue
        setup_id = result["setup_id"]
        if setup_id in seen:
            i += 1
            continue
        seen.add(setup_id)
        next_bar = m5[i + 1]
        side, sl = result["direction"], result["sl_reference"]
        # Adverse slippage applied at fill; BID/ASK spread is already in actual quote side.
        fill = ((next_bar["ask_open"] + entry_slippage_usd_oz) if side == "BUY"
                else (next_bar["open"] - entry_slippage_usd_oz))
        d = (fill - sl) if side == "BUY" else (sl - fill)
        risk = d * LOT * OZ_PER_LOT_ASSUMPTION
        if d <= 0 or risk > RISK_USD_CAP:
            reason_counts["Post-fill risk invalid or exceeds $30"] = reason_counts.get("Post-fill risk invalid or exceeds $30", 0) + 1
            i += 1
            continue
        target = fill + (2.0 * d if side == "BUY" else -2.0 * d)
        resolved = _resolve_trade(m5, i + 1, side, fill, sl, target,
                                  max_holding_bars, exit_slippage_usd_oz)
        exit_price = resolved["exit_price"]
        pnl = (((exit_price - fill) if side == "BUY" else (fill - exit_price))
               * LOT * OZ_PER_LOT_ASSUMPTION
               - commission_usd_per_lot_roundtrip * LOT)
        taken.append({
            "setup_id": setup_id, "signal_utc": decision.isoformat(),
            "filled_utc": next_bar["timestamp"], "closed_utc": _close_time(m5[resolved["exit_idx"]], 5).isoformat(),
            "side": side, "entry_bid_or_ask": round(fill, 3),
            "sl": round(sl, 3), "hypothetical_tp_2r": round(target, 3),
            "outcome": resolved["outcome"], "pnl_usd": round(pnl, 2),
            "planned_risk_usd": round(risk, 2), "filled_on_next_m5": True
        })
        trades_today += 1
        consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0
        # No overlapping trades. Observe the market only after the previous trade exited.
        i = resolved["exit_idx"] + 1
    tp = sum(t["outcome"] == "TP" for t in taken)
    sl = sum(t["outcome"] in ("SL", "SL_AMBIGUOUS") for t in taken)
    timeout = sum(t["outcome"] == "TIMEOUT" for t in taken)
    return {
        "state": "HISTORICAL_RESEARCH_ONLY", "symbol": "XAUUSD",
        "source": SOURCE, "quote_basis": payload["quote_basis"],
        "time_zone_for_daily_limits": "UTC", "trade_count": len(taken),
        "tp_count": tp, "sl_count_including_ambiguous": sl,
        "timeout_count": timeout,
        "tp_vs_sl_win_rate_pct": round(100 * tp / (tp + sl), 2) if tp + sl else None,
        "net_pnl_usd_estimate": round(sum(t["pnl_usd"] for t in taken), 2),
        "trades": taken, "no_signal_reasons": reason_counts,
        "assumptions": {
            "lot": LOT, "oz_per_standard_lot": OZ_PER_LOT_ASSUMPTION,
            "max_risk_usd": RISK_USD_CAP,
            "next_m5_open_fill": True,
            "entry_slippage_usd_per_oz": entry_slippage_usd_oz,
            "exit_slippage_usd_per_oz": exit_slippage_usd_oz,
            "commission_usd_per_lot_round_trip": commission_usd_per_lot_roundtrip,
            "fixed_hypothetical_target": "2R (NOT validated structural target)",
            "same_bar_both_hits": "SL first",
            "first_trade_$70_goal": "NOT IMPLEMENTED; final strategy is not validated"
        },
        "limitations": [
            "Not a claim of profitability or an executable signal",
            "Historical spot quotes differ from FundingPips pricing and conditions",
            "No gap fill simulation beyond next available M5 open",
            "Needs larger held-out history, overnight/session rules and cost calibration before any alerts"
        ]
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Offline spot gold backtest - never trades")
    p.add_argument("history_json")
    p.add_argument("--out", default="gold_backtest_report.json")
    args = p.parse_args()
    data = json.loads(Path(args.history_json).read_text(encoding="utf-8"))
    report = backtest(data)
    output = Path(args.out)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing report: {output}")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Research only: {report['trade_count']} trades; TP={report['tp_count']}, "
          f"SL={report['sl_count_including_ambiguous']}, TIMEOUT={report['timeout_count']}. "
          f"TP/SL-only win rate={report['tp_vs_sl_win_rate_pct']}%; saved {output}")


if __name__ == "__main__":
    main()
