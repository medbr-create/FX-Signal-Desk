"""Offline deterministic tests; no funding account, API key, or trading platform needed."""
import unittest
from datetime import datetime, timedelta, timezone

from gold_compass.engine import analyze, SOURCE

NOW = datetime(2026, 11, 16, 16, 5, tzinfo=timezone.utc)


def bar(t, o, h, l, c):
    return dict(timestamp=t.isoformat(), open=o, high=h, low=l, close=c)


def fixture(buy=True):
    # Unit-test-only fabricated candles, NEVER live market prices.
    hstart = datetime(2026, 11, 14, 4, tzinfo=timezone.utc)
    h1 = []
    for i in range(60):
        p = (995.0 + i * 0.1) if buy else (1005.0 - i * 0.1)
        h1.append(bar(hstart + timedelta(hours=i), p, p + 0.35, p - 0.35, p + (0.25 if buy else -0.25)))
    start = datetime(2026, 11, 16, 1, tzinfo=timezone.utc)
    m15 = []
    for i in range(57):
        m15.append(bar(start + timedelta(minutes=i * 15), 1000.1, 1000.5, 999.7, 1000.2))
    if buy:
        m15.extend([bar(start + timedelta(minutes=57 * 15), 1000.1, 1001.3, 999.9, 1001.2),
                    bar(start + timedelta(minutes=58 * 15), 1000.8, 1001.0, 1000.4, 1000.7),
                    bar(start + timedelta(minutes=59 * 15), 1000.75, 1002.0, 1000.7, 1001.5)])
    else:
        for b in m15:
            b.update(open=1000.1, high=1000.5, low=999.7, close=1000.0)
        m15.extend([bar(start + timedelta(minutes=57 * 15), 1000.0, 1000.2, 998.8, 999.0),
                    bar(start + timedelta(minutes=58 * 15), 999.2, 999.6, 999.0, 999.3),
                    bar(start + timedelta(minutes=59 * 15), 999.3, 999.4, 998.5, 998.7)])
    # M5 is the execution-only trigger; 16:00-16:05 is after the 15:45-16:00 M15 confirmation.
    fstart = datetime(2026, 11, 16, 10, 55, tzinfo=timezone.utc)
    m5 = []
    for i in range(61):
        t = fstart + timedelta(minutes=i * 5)
        if buy:
            m5.append(bar(t, 1001.2, 1001.55, 1001.0, 1001.3))
        else:
            m5.append(bar(t, 999.4, 999.6, 999.25, 999.3))
    if buy:
        m5.append(bar(datetime(2026, 11, 16, 16, tzinfo=timezone.utc),
                      1001.5, 1002.15, 1001.45, 1001.9))
    else:
        m5.append(bar(datetime(2026, 11, 16, 16, tzinfo=timezone.utc),
                      999.1, 999.2, 998.55, 998.75))
    return {"source": SOURCE, "symbol": "XAUUSD", "h1": h1, "m15": m15, "m5": m5}


class GoldCompassEngineTests(unittest.TestCase):
    def test_rejects_futures_and_unknown_source(self):
        data = fixture()
        data["source"] = "GC=F"
        self.assertEqual(analyze(data, now=NOW)["state"], "NO_DATA")

    def test_bullish_breakout_retest_review(self):
        result = analyze(fixture(), now=NOW)
        self.assertEqual(result["state"], "REVIEW", result)
        self.assertEqual(result["direction"], "BUY")
        self.assertEqual(result["last_closed_m5_utc"], NOW.isoformat())
        self.assertLessEqual(result["estimated_risk_usd"], 30)
        self.assertGreater(result["sl_reference"], 0)

    def test_bearish_breakout_retest_m5_review(self):
        result = analyze(fixture(buy=False), now=NOW)
        self.assertEqual(result["state"], "REVIEW", result)
        self.assertEqual(result["direction"], "SELL")

    def test_no_lookahead_from_open_candle(self):
        data = fixture()
        # Add a dramatic but NOT closed candle. It must be discarded.
        data["m15"].append(bar(datetime(2026, 11, 16, 16, tzinfo=timezone.utc), 1002, 5000, 0.01, 4000))
        # Future M5 candle must be ignored as well.
        data["m5"].append(bar(datetime(2026, 11, 16, 16, 5, tzinfo=timezone.utc), 1002, 4000, 1, 3000))
        self.assertEqual(analyze(data, now=NOW)["setup_id"], analyze(fixture(), now=NOW)["setup_id"])

    def test_stale_data_blocks(self):
        self.assertEqual(analyze(fixture(), now=NOW + timedelta(hours=3))["state"], "NO_DATA")

    def test_invalid_ohlc_blocks(self):
        data = fixture()
        data["m15"][-1]["high"] = 1
        self.assertEqual(analyze(data, now=NOW)["state"], "NO_DATA")

    def test_m5_missing_blocks(self):
        data = fixture()
        data["m5"] = []
        self.assertEqual(analyze(data, now=NOW)["state"], "NO_DATA")

    def test_m5_wrong_direction_waits(self):
        data = fixture()
        data["m5"][-1].update(open=1001.6, high=1001.7, low=1001.1, close=1001.2)
        result = analyze(data, now=NOW)
        self.assertEqual(result["state"], "WAIT", result)

    def test_m5_stale_blocks(self):
        data = fixture()
        # Drop most recently closed M5 candles while M15 remains fresh
        data["m5"] = data["m5"][:-4]
        self.assertEqual(analyze(data, now=NOW)["state"], "NO_DATA")

    def test_missing_candles_blocks(self):
        data = fixture()
        data["m15"] = []
        self.assertEqual(analyze(data, now=NOW)["state"], "NO_DATA")

    def test_wrong_symbol_blocks(self):
        data = fixture()
        data["symbol"] = "XAUUSD-FUTURES"
        self.assertEqual(analyze(data, now=NOW)["state"], "NO_DATA")


if __name__ == "__main__":
    unittest.main()
