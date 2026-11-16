#!/usr/bin/env node
/**
 * Download historical XAUUSD SPOT bid+ask candles independently from Dukascopy.
 * No broker connection, no trading, no notifications.
 * Usage: npm install && node gold_compass/fetch_spot.mjs 2026-11-09 2026-11-16
 */
import fs from "node:fs";
import path from "node:path";
import { getHistoricalRates } from "dukascopy-node";

const [from, to, output] = process.argv.slice(2);
const datePattern = /^\d{4}-\d{2}-\d{2}$/;
if (!datePattern.test(from || "") || !datePattern.test(to || "")) {
  console.error("Usage: node gold_compass/fetch_spot.mjs YYYY-MM-DD YYYY-MM-DD [output.json]");
  process.exit(2);
}
const fromMs = Date.parse(from + "T00:00:00Z");
const toMs = Date.parse(to + "T00:00:00Z");
if (!Number.isFinite(fromMs) || !Number.isFinite(toMs) || toMs <= fromMs ||
    toMs - fromMs > 45 * 86400_000 || toMs > Date.now() + 86400_000) {
  console.error("Invalid date range (maximum 45 days; end date exclusive)");
  process.exit(2);
}
const makeConfig = (timeframe, priceType) => ({
  instrument: "xauusd",
  dates: {from, to},
  timeframe, priceType, format: "json", volumes: false,
  ignoreFlats: true, batchSize: 3, pauseBetweenBatchesMs: 1500,
  retryCount: 3, pauseBetweenRetriesMs: 1000,
  useCache: true, cacheFolderPath: ".dukascopy-cache"
});

function clean(candles, interval) {
  if (!Array.isArray(candles)) throw new Error("Expected Dukascopy JSON candle array");
  const step = {h1:3600000,m15:900000,m5:300000}[interval];
  const seen = new Map();
  for (const candle of candles) {
    const t = Number(candle.timestamp);
    const vals = ["open","high","low","close"].map(k => Number(candle[k]));
    if (!Number.isFinite(t) || t % step !== 0 || vals.some(v=>!Number.isFinite(v)||v<=0) ||
        vals[1] < Math.max(vals[0], vals[2], vals[3]) ||
        vals[2] > Math.min(vals[0], vals[1], vals[3])) {
      throw new Error("Invalid Dukascopy OHLC or UTC alignment for "+interval);
    }
    if (t >= fromMs && t < toMs) seen.set(t, {
      timestamp: new Date(t).toISOString(), open: vals[0], high: vals[1],
      low: vals[2], close: vals[3]
    });
  }
  return [...seen.entries()].sort((a,b)=>a[0]-b[0]).map(x=>x[1]);
}

try {
  const result = {source:"dukascopy_spot_bid",symbol:"XAUUSD",
    quote_basis:"bid_ohlc_with_ask",mode:"HISTORICAL_ONLY",
    period_utc:{from,to},generated_at_utc:new Date().toISOString(),
    source_disclaimer:"Dukascopy spot BID/ASK is independent of FundingPips quotes",
    h1:[],m15:[],m5:[]};
  for (const interval of ["h1","m15","m5"]) {
    console.error("Fetching",interval,"BID and ASK");
    const bidBars = clean(await getHistoricalRates(makeConfig(interval, "bid")), interval);
    const askBars = clean(await getHistoricalRates(makeConfig(interval, "ask")), interval);
    const bidTimes = new Set(bidBars.map(b=>b.timestamp));
    const askTimes = new Set(askBars.map(b=>b.timestamp));
    if (bidBars.length < 45 || askBars.length < 45 ||
        bidTimes.size !== askTimes.size || [...bidTimes].some(t=>!askTimes.has(t))) {
      throw new Error("Missing/misaligned BID/ASK data for "+interval+"; refusing to export");
    }
    const askLookup = new Map(askBars.map(a=>[a.timestamp,a]));
    result[interval] = bidBars.map(b => {
      const a = askLookup.get(b.timestamp);
      if (a.open < b.open || a.close < b.close) {
        throw new Error("Negative BID/ASK spread for "+interval+" "+b.timestamp);
      }
      return {...b,ask_open:a.open,ask_high:a.high,ask_low:a.low,ask_close:a.close,
        spread_close:Number((a.close-b.close).toFixed(4))};
    });
  }
  const outfile = output || `gold-compass-${from}-${to}.json`;
  fs.mkdirSync(path.dirname(path.resolve(outfile)), {recursive:true});
  if (fs.existsSync(outfile)) throw new Error("Refusing to overwrite existing historical snapshot");
  fs.writeFileSync(outfile,JSON.stringify(result,null,2));
  console.error("Saved independent spot BID/ASK historical snapshot to",outfile);
  console.error("Historical RESEARCH only. No alerts, no FundingPips access.");
} catch(e) {
  console.error("Spot download failed; refusing to emit partial history:",e?.message||String(e));
  process.exit(1);
}
