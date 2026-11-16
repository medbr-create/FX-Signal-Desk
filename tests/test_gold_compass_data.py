"""Offline research-data/quote replay unit tests; no network, broker, or live alerts."""
import lzma
import struct
from datetime import date, datetime, timedelta, timezone
import unittest

from gold_compass.dukascopy import _decode, candle_url, make_bars, pair_minutes
from gold_compass.backtest import _resolve_trade, backtest


def minute(ts, bid_open=4000.0, ask_open=4000.3):
    def quote(price):
        return {"timestamp": ts.isoformat(), "open": price, "high": price + 0.5,
                "low": price - 0.5, "close": price + 0.1}
    bid, ask = quote(bid_open), quote(ask_open)
    return {"timestamp": ts.isoformat(), "bid": bid, "ask": ask,
            "spread_close": round(ask["close"] - bid["close"], 4)}


class DukascopyResearchDataTests(unittest.TestCase):
    def test_zero_based_month_and_two_quote_sides(self):
        self.assertEqual(candle_url(date(2026, 11, 12), "BID"),
                         "https://datafeed.dukascopy.com/datafeed/XAUUSD/2026/10/12/BID_candles_min_1.bi5")
        with self.assertRaises(ValueError):
            candle_url(date(2026, 11, 12), "MID")

    def test_decode_bi5_minute_order_open_close_low_high(self):
        # Fixture generated exclusively for unit test, never presented as market history.
        packed = struct.pack(">IIIIIf", 60, 4000500, 4000800, 4000200, 4001200, 3.0)
        rows = _decode(lzma.compress(packed), date(2026, 11, 12))
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["open"], 4000.5)
        self.assertAlmostEqual(rows[0]["close"], 4000.8)
        self.assertAlmostEqual(rows[0]["low"], 4000.2)
        self.assertAlmostEqual(rows[0]["high"], 4001.2)
        self.assertEqual(rows[0]["timestamp"], "2026-11-12T00:01:00+00:00")

    def test_decode_corrupt_record_rejected(self):
        with self.assertRaises(ValueError):
            _decode(lzma.compress(b"broken"), date(2026, 11, 12))

    def test_resample_does_not_make_up_missing_minutes(self):
        start = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
        first5 = [minute(start + timedelta(minutes=i)) for i in range(5)]
        self.assertEqual(len(make_bars(first5, 5)), 1)
        self.assertEqual(len(make_bars(first5[1:], 5)), 0)
        self.assertEqual(len(make_bars(first5, 15)), 0)

    def test_pair_requires_overlap_and_bid_ask(self):
        ts = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
        d = minute(ts)
        paired = pair_minutes([d["bid"]], [d["ask"]])
        self.assertEqual(len(paired), 1)
        self.assertGreater(paired[0]["spread_close"], 0)


class GoldCompassReplayTests(unittest.TestCase):
    def test_stop_first_if_same_bar_touches_both_long(self):
        ts = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
        row = minute(ts)
        bar = {"timestamp": row["timestamp"], "open": 4000, "high": 4005, "low": 3995,
               "close": 4000, "ask_open": 4000.3, "ask_high": 4005.3, "ask_low": 3995.3, "ask_close": 4000.3}
        r = _resolve_trade([bar], 0, "BUY", 4000.3, 3998, 4004, 1, 0.1)
        self.assertEqual(r["outcome"], "SL_AMBIGUOUS")
        self.assertAlmostEqual(r["exit_price"], 3997.9)

    def test_short_exits_on_ask_not_bid(self):
        ts = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
        bar = {"timestamp": ts.isoformat(), "open": 3999.7, "ask_open": 4000.0, "high": 4000, "low": 3999,
               "close": 3999.5, "ask_high": 4000.4, "ask_low": 3999.4, "ask_close": 3999.8}
        # Bid high never touches 4000.2, but ask high does, so SELL SL is hit.
        r = _resolve_trade([bar], 0, "SELL", 4000, 4000.2, 3990, 1, 0.1)
        self.assertEqual(r["outcome"], "SL")

    def test_worse_stop_gap_fill_long(self):
        ts = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
        bar = {"timestamp": ts.isoformat(), "open": 3996.0, "high": 4000,
               "low": 3995, "close": 3997, "ask_open": 3996.3,
               "ask_high": 4000.3, "ask_low": 3995.3, "ask_close": 3997.3}
        r = _resolve_trade([bar], 0, "BUY", 4000.3, 3998, 4004, 1, 0.1)
        self.assertEqual(r["outcome"], "SL")
        self.assertAlmostEqual(r["exit_price"], 3995.9)

    def test_rejects_unpaired_or_futures_backtest(self):
        with self.assertRaises(ValueError):
            backtest({"source": "GC=F", "symbol": "XAUUSD"})
        with self.assertRaises(ValueError):
            backtest({"source": "dukascopy_spot_bid", "symbol": "XAUUSD",
                      "quote_basis": "bid_only", "mode": "HISTORICAL_ONLY"})


if __name__ == "__main__":
    unittest.main()
