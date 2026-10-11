"""Public spot OHLC feasibility probe; never imports a trading app or .env.

Counts research patterns and structural price feasibility, NOT trades or P&L.
Quotes/spread/fees are explicit assumptions, not MT5 execution prices.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

import pandas as pd
import requests

API = "https://api.exchange.coinbase.com"
ASSETS = {"BTC": {"product": "BTC-USD", "pct": .015, "spread_bps": 2, "target_r": 3},
          "ETH": {"product": "ETH-USD", "pct": .02, "spread_bps": 4, "target_r": 2.5}}


def utc_stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def fetch_minutes(product, start, end, session):
    rows = {}
    for cursor in range(start, end, 300 * 60):
        upper = min(cursor + 300 * 60, end)
        response = session.get(API + f"/products/{product}/candles", params={
            "granularity": 60, "start": utc_stamp(cursor), "end": utc_stamp(upper)}, timeout=(5, 25))
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, list):
            raise ValueError("invalid candle response")
        for row in body:
            stamp, low, high, opened, closed, volume = row
            if cursor <= stamp < upper and stamp % 60 == 0:
                rows[stamp] = [stamp, opened, high, low, closed, volume]
        time.sleep(.12)
    return pd.DataFrame(sorted(rows.values()), columns=["time", "open", "high", "low", "close", "volume"])


def aggregate(minutes, seconds):
    """Only complete, continuous, aligned source buckets become closed bars."""
    if seconds not in (300, 900, 3600):
        raise ValueError("unsupported closed timeframe")
    previous = None
    for row in minutes.itertuples(index=False):
        stamp = row.time
        if (not math.isfinite(stamp) or stamp != int(stamp) or stamp % 60
                or previous is not None and stamp <= previous):
            raise ValueError("unordered/duplicate/invalid source clock")
        prices = (row.open, row.high, row.low, row.close)
        if (any(not math.isfinite(value) or value <= 0 for value in prices)
                or row.high < max(row.open, row.low, row.close)
                or row.low > min(row.open, row.high, row.close)
                or not math.isfinite(row.volume) or row.volume < 0):
            raise ValueError("invalid source OHLC/volume")
        previous = stamp
    work = minutes.copy()
    work["bucket"] = work.time // seconds * seconds
    bars = []
    for stamp, group in work.groupby("bucket", sort=True):
        expected = list(range(int(stamp), int(stamp) + seconds, 60))
        if list(group.time) != expected:
            continue
        bars.append([int(stamp), group.open.iloc[0], group.high.max(), group.low.min(),
                     group.close.iloc[-1], group.volume.sum()])
    result = pd.DataFrame(bars, columns=["time", "open", "high", "low", "close", "volume"])
    if not result.empty:
        previous = result.close.shift()
        ranges = pd.concat([result.high - result.low, (result.high - previous).abs(),
                            (result.low - previous).abs()], axis=1).max(axis=1)
        result["atr"] = ranges.rolling(14, min_periods=14).mean()
        result["ema21"] = result.close.ewm(span=21, adjust=False).mean()
    return result


def analyze(minutes, asset):
    """Use only bars closed by checkpoint; later prices affect drift counters only."""
    settings = ASSETS[asset]
    m5, m15, h1 = [aggregate(minutes, step) for step in (300, 900, 3600)]
    counts = {"evaluated": 0, "neutral_h1": 0, "chase_rejected": 0, "structure_over_cap": 0,
              "structurally_feasible": 0, "gold_price_8_would_reject": 0,
              "fee_adjusted_rr_below_1_5": 0, "drift60_invalid": 0, "drift120_invalid": 0}
    hypotheses = {"trend_reclaim": 0, "breakout_retest": 0, "sweep_reclaim": 0}
    atr_pct, risks_pct, delay_samples = [], [], {60: 0, 120: 0}
    quote_by_time = minutes.set_index("time").open.to_dict()
    for index in range(30, len(m5)):
        decision = int(m5.iloc[index].time) + 300
        f15 = m15[m15.time + 900 <= decision]
        fh = h1[h1.time + 3600 <= decision]
        if len(f15) < 25 or len(fh) < 25 or decision not in quote_by_time:
            continue
        # Gapped protection window / stale M15/H1 are skipped, never treated as valid.
        recent = m5.iloc[index-2:index+1]
        if (list(recent.time) != list(range(decision-900, decision, 300))
                or f15.time.iloc[-1] != decision // 900 * 900 - 900
                or fh.time.iloc[-1] != decision // 3600 * 3600 - 3600):
            continue
        counts["evaluated"] += 1
        atr = float(f15.atr.iloc[-1])
        close = float(f15.close.iloc[-1])
        atr_pct.append(100 * atr / close)
        short, long = fh.close.tail(5).mean(), fh.close.tail(20).mean()
        last_h = fh.close.iloc[-1]
        side = 1 if short > long and last_h >= long else -1 if short < long and last_h <= long else 0
        if not side:
            counts["neutral_h1"] += 1
            continue
        current5, previous5 = m5.iloc[index], m5.iloc[index-1]
        ema = float(f15.ema21.iloc[-1])
        prior = m5.iloc[index-12:index]
        if side == 1:
            hypotheses["sweep_reclaim"] += int(current5.low < prior.low.min() and current5.close > prior.low.min())
            hypotheses["trend_reclaim"] += int(previous5.close <= ema and current5.close > ema)
        else:
            hypotheses["sweep_reclaim"] += int(current5.high > prior.high.max() and current5.close < prior.high.max())
            hypotheses["trend_reclaim"] += int(previous5.close >= ema and current5.close < ema)
        # Prior M15 closed breakout, followed by a new M5 level retest after that close.
        breakout = f15.iloc[-1]
        box = f15.iloc[-17:-1]
        level = box.high.max() if side == 1 else box.low.min()
        if current5.time >= breakout.time + 900 and side * (breakout.close - level) > .05 * atr:
            touched = current5.low <= level + .25 * atr if side == 1 else current5.high >= level - .25 * atr
            hypotheses["breakout_retest"] += int(touched and side * (current5.close - level) > 0)
        # These targets are hypothetical R limits, not obstacle-proven proposals.
        mid = float(quote_by_time[decision])
        spread = mid * settings["spread_bps"] / 10000
        entry = mid + side * spread / 2
        buffer = .15 * atr  # no broker tick_size data in this spot probe
        extreme = float(recent.low.min() if side == 1 else recent.high.max())
        stop = min(extreme-buffer, entry-.6*atr) if side == 1 else max(extreme+buffer, entry+.6*atr)
        risk = side * (entry-stop)
        cap = min(2.5 * atr, entry * settings["pct"])
        if abs(entry - ema) > 1.5 * atr:
            counts["chase_rejected"] += 1
            continue
        if not 0 < risk <= cap:
            counts["structure_over_cap"] += 1
            continue
        trigger_side = mid - side * spread/2
        if side * (trigger_side-stop) < 2 * buffer or spread > .25*atr:
            continue
        counts["structurally_feasible"] += 1
        counts["gold_price_8_would_reject"] += int(risk > 8)
        risks_pct.append(100*risk/entry)
        target = entry + side * settings["target_r"] * risk
        # Assumed roundtrip 10 bps taker fee, only a sensitivity probe.
        fee = entry*.001
        counts["fee_adjusted_rr_below_1_5"] += int((settings["target_r"]*risk-fee)/(risk+fee) < 1.5)
        for delay in (60, 120):
            later_mid = quote_by_time.get(decision+delay)
            if later_mid is None:
                continue
            delay_samples[delay] += 1
            later_spread = later_mid*settings["spread_bps"]/10000
            later_entry = later_mid+side*later_spread/2
            later_clearance = side*(later_mid-side*later_spread/2-stop)
            later_risk = side*(later_entry-stop)
            rr = side*(target-later_entry)/later_risk if later_risk > 0 else -1
            invalid = (not 0 < later_risk <= cap or rr < 1.5 or later_clearance < 2*buffer
                       or abs(later_entry-ema) > 1.5*atr or later_spread > .25*atr)
            counts[f"drift{delay}_invalid"] += int(invalid)
    median = lambda values: float(pd.Series(values).median()) if values else None
    return {"asset": asset, "counts": counts, "research_pattern_counts": hypotheses,
            "delay_samples": delay_samples, "median_m15_atr_pct": median(atr_pct),
            "median_protective_risk_pct": median(risks_pct), "m1_rows": len(minutes),
            "m5_rows": len(m5), "m15_rows": len(m15), "h1_rows": len(h1),
            "assumptions": {"spread_bps": settings["spread_bps"], "roundtrip_fee_bps": 10,
                            "quotes": "next M1 spot open, synthetic bid/ask",
                            "target": "R-only hypothetical, no obstacle/AI confirmation"},
            "not_measured": ["CFD contracts/margin/lot feasibility", "funding/OI/liquidation", "AI decisions/cost", "P&L/trade count"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true", help="Public anonymous Coinbase GETs only")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    if not 2 <= args.days <= 14:
        parser.error("days must be 2..14")
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    metadata = None
    if args.download:
        with requests.Session() as session:
            response = session.get(API+"/time", timeout=(5, 20))
            response.raise_for_status()
            end = int(response.json()["epoch"]) // 60 * 60
            start = end - args.days*86400
            metadata = {"source": API, "start_utc": utc_stamp(start), "end_utc_exclusive": utc_stamp(end),
                        "downloaded_utc": utc_stamp(time.time()), "products": {}}
            for asset, settings in ASSETS.items():
                print("fetch", asset, flush=True)
                frame = fetch_minutes(settings["product"], start, end, session)
                raw = frame.to_csv(index=False).encode("utf-8")
                (args.cache_dir/f"{asset}.csv").write_bytes(raw)
                metadata["products"][asset] = {"rows": len(frame), "sha256": hashlib.sha256(raw).hexdigest()}
            (args.cache_dir/"source.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    results = {asset: analyze(pd.read_csv(args.cache_dir/f"{asset}.csv"), asset) for asset in ASSETS}
    result = {"source": metadata or json.loads((args.cache_dir/"source.json").read_text(encoding="utf-8")),
              "meaning": "research feasibility counts, never trades, AI performance or CFD profit", "results": results}
    (args.cache_dir/"probe.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
