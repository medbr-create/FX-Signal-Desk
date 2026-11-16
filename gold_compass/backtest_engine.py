"""Cautious, read-only M5-bar replay for Gold Compass research.

It requires independent BID+ASK candle data from the historical importer. No
broker account, no order execution, no optimization and no profitability claim.
"""
from __future__ import annotations

from datetime import timedelta
from math import isfinite
from .engine import analyze, _utc

MAX_HOLD_BARS = 72  # six hours on M5
MAX_ENTRIES_PER_DAY = 4
MAX_CONSECUTIVE_LOSSES = 2
LOT_OZ_ASSUMPTION = 7.0  # 0.07 lot x 100 oz; MUST verify broker contract
SLIPPAGE_PER_SIDE_USD_PER_OZ = 0.20  # deliberately configurable research assumption


def _at_or_before(bars: list[dict], cutoff):
    return [b for b in bars if _utc(b["timestamp"]) + _duration(bars) <= cutoff]


def _duration(bars):
    # This helper is only used with explicit TF arrays; don't infer tick size from missing bars.
    raise RuntimeError("Use explicit durations in replay()")


def replay(payload: dict, *, slippage_per_side=SLIPPAGE_PER_SIDE_USD_PER_OZ) -> dict:
    if payload.get("source") != "dukascopy_spot_bid" or payload.get("symbol") != "XAUUSD":
        raise ValueError("Expected authenticated research input labelled as XAUUSD spot BID")
    if not isfinite(slippage_per_side) or slippage_per_side < 0:
        raise ValueError("Invalid slippage assumption")
    bid = payload
    asks = payload.get("ask", {})
    if any(not isinstance(bid.get(tf), list) or not isinstance(asks.get(tf), list)
           for tf in ("h1", "m15", "m5")):
        raise ValueError("Missing bid/ask history")
    times = {}
    for tf, minutes in (("h1", 60), ("m15", 15), ("m5", 5)):
        b, a = bid[tf], asks[tf]
        ts_b = [_utc(x["timestamp"]) for x in b]
        ts_a = [_utc(x["timestamp"]) for x in a]
        if not ts_b or ts_b != ts_a or len(ts_b) != len(set(ts_b)) or ts_b != sorted(ts_b):
            raise ValueError(f"BID/ASK bars are missing, unsorted or misaligned: {tf}")
        for row in b + a:
            nums = [float(row[k]) for k in ("open", "high", "low", "close")]
            if (not all(isfinite(v) and v > 0 for v in nums)
                or nums[1] < max(nums[0], nums[2], nums[3])
                or nums[2] > min(nums[0], nums[1], nums[3])):
                raise ValueError(f"Malformed BID/ASK prices: {tf}")
        # A physically impossible negative spread invalidates the feed.
        if any(float(ask["close"]) < float(bid_["close"]) for bid_, ask in zip(b, a)):
            raise ValueError(f"Negative BID/ASK close spread: {tf}")
        times[tf] = ts_b

    results = []
    seen = set()
    active = None
    day_key = None
    entries_today = 0
    consecutive_losses = 0
    today_net = 0.0

    # Candidate signal after M5 candle closes; entry at NEXT M5 open (no look-ahead).
    for i in range(1, len(bid["m5"]) - 1):
        ts = times["m5"][i]
        now = ts + timedelta(minutes=5)
        day = now.date()
        if day != day_key:
            day_key = day
            entries_today = 0
            consecutive_losses = 0
            today_net = 0.0

        candle = bid["m5"][i]
        ask_candle = asks["m5"][i]
        if active is not None:
            # Open positions were filled at the next candle open, never at the trigger close.
            d = active["direction"]
            sl = active["sl"]
            tp = active["tp"]
            if d == "BUY":
                stop_hit = float(candle["low"]) <= sl
                target_hit = float(candle["high"]) >= tp
            else:
                # Closing a short requires ASK prices, not the BID chart.
                stop_hit = float(ask_candle["high"]) >= sl
                target_hit = float(ask_candle["low"]) <= tp
            expiry = i >= active["expiry_index"]
            if stop_hit or target_hit:
                # Both in one candle -> stop first, conservative and explicit.
                outcome = "SL" if stop_hit else "TP"
                exit_px = sl if stop_hit else tp
            elif expiry:
                outcome = "TIME"
                exit_px = float(candle["close"]) if d == "BUY" else float(ask_candle["close"])
            else:
                continue
            exit_filled = exit_px - slippage_per_side if d == "BUY" else exit_px + slippage_per_side
            pnl = (exit_filled - active["entry"]) if d == "BUY" else (active["entry"] - exit_filled)
            pnl = round(pnl * LOT_OZ_ASSUMPTION, 2)
            active.update(exit_utc=now.isoformat(), exit_price=round(exit_filled, 3),
                          pnl_usd=pnl, outcome=outcome,
                          ambiguous_same_bar=bool(stop_hit and target_hit))
            results.append(active)
            today_net += pnl
            consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0
            active = None
            continue

        if entries_today >= MAX_ENTRIES_PER_DAY or consecutive_losses >= MAX_CONSECUTIVE_LOSSES or today_net >= 70:
            continue

        h1_cut = [x for x, t in zip(bid["h1"], times["h1"]) if t + timedelta(hours=1) <= now]
        m15_cut = [x for x, t in zip(bid["m15"], times["m15"]) if t + timedelta(minutes=15) <= now]
        m5_cut = bid["m5"][:i + 1]
        if len(h1_cut) < 45 or len(m15_cut) < 45 or len(m5_cut) < 45:
            continue
        decision = analyze({
            "source":"dukascopy_spot_bid", "symbol":"XAUUSD",
            "h1":h1_cut[-70:], "m15":m15_cut[-90:], "m5":m5_cut[-90:]
        }, now=now)
        if decision["state"] != "REVIEW" or decision["setup_id"] in seen:
            continue
        # Never claim a setup is tradable purely because the filter emitted REVIEW.
        seen.add(decision["setup_id"])
        nxt_bid = bid["m5"][i + 1]
        nxt_ask = asks["m5"][i + 1]
        nxt_start = times["m5"][i + 1]
        if nxt_start != now:  # gaps/market closure: refuse a delayed fill
            continue
        d = decision["direction"]
        entry = float(nxt_ask["open"]) + slippage_per_side if d == "BUY" else float(nxt_bid["open"]) - slippage_per_side
        spread = max(0.0, float(nxt_ask["open"]) - float(nxt_bid["open"]))
        sl_ref = float(decision["sl_reference"])
        tp_ref = float(decision["hypothetical_rr2_tp"])
        sl = sl_ref if d == "BUY" else sl_ref + spread
        tp = tp_ref if d == "BUY" else tp_ref + spread
        per_oz_risk = (entry - sl) if d == "BUY" else (sl - entry)
        per_oz_reward = (tp - entry) if d == "BUY" else (entry - tp)
        if per_oz_risk <= 0 or per_oz_reward <= 0 or per_oz_risk * LOT_OZ_ASSUMPTION > 30:
            continue
        active = {
            "setup_id":decision["setup_id"],"direction":d,
            "signal_utc":now.isoformat(),"entry_utc":nxt_start.isoformat(),
            "entry":round(entry,3),"sl":round(sl,3),"tp":round(tp,3),
            "estimated_risk_usd":round(per_oz_risk * LOT_OZ_ASSUMPTION,2),
            "estimated_rr_after_costs":round(per_oz_reward / per_oz_risk,2),
            "expiry_index":min(i + 1 + MAX_HOLD_BARS, len(bid["m5"]) - 1)
        }
        entries_today += 1

    if active is not None:
        # An open, unfinished trade is not a win/loss.
        active["outcome"] = "OPEN"
        results.append(active)
    wins = sum(x["outcome"] == "TP" for x in results)
    losses = sum(x["outcome"] == "SL" for x in results)
    closed = [x for x in results if x["outcome"] in ("TP","SL","TIME")]
    for trade in results:
        trade.pop("expiry_index", None)
    return {
        "symbol":"XAUUSD", "mode":"RESEARCH_ONLY", "strategy":"H1+M15+M5 research prototype",
        "source":bid["source"], "spread":"Historical bid and ask",
        "slippage_per_side_usd_per_oz":slippage_per_side,
        "assumptions":["0.07 lot x 100 oz/lot is unverified","No broker commissions, swap, platform latency or funding rules modelled",
                       "SL before TP when same M5 bar touches both","Next bar opening execution is an approximation",
                       "Target is hypothetical 2R, not verified structural feasibility",
                       "No claim of profitability or FundingPips compliance"],
        "trades":results, "completed":len(closed),
        "tp":wins,"sl":losses,"time":sum(x["outcome"]=="TIME" for x in results),
        "open":sum(x["outcome"]=="OPEN" for x in results),
        "win_rate_pct_tp_sl_only":round(100 * wins / (wins+losses),2) if wins+losses else None,
        "total_simulated_pnl_usd":round(sum(x["pnl_usd"] for x in closed),2),
    }
