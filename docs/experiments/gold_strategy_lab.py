"""Offline gold strategy experiments. No MT5, dotenv, HTTP, or order API.

Run from any directory; CSV inputs are the project's cached bid OHLC history.
Rules are hypotheses, not replicas of the cited papers or production strategies.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
VERSION = "gold-strategy-lab-v1"
PERIODS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}
NAMES = {
    "continuation": "M5趋势延续", "compression": "M15压缩突破",
    "sweep": "M5失败突破反转", "session": "欧美时段突破",
    "session_retest": "欧美时段突破回踩", "swing": "H1波段突破",
}


@dataclass(frozen=True)
class Signal:
    time: int  # UTC close time: all evidence is closed by this instant.
    strategy: str
    side: int  # 1 buy, -1 sell
    stop: float
    target: float
    atr: float
    max_hold_minutes: int
    reference: float
    trail: bool = False
    session_key: str = ""


@dataclass(frozen=True)
class Costs:
    name: str
    spread_multiplier: float = 1.0
    slippage: float = 0.02  # adverse absolute price per market/stop fill
    round_trip_fee: float = 0.0  # account dollars for 0.01 lot
    rollover_fee: float = 0.20  # assumed adverse dollars per NY 17:00 crossing
    delay_seconds: int = 0


COSTS = [Costs("historical"), Costs("double_cost", 2.0, 0.10, 0.10, 0.50),
         Costs("delay_60s", delay_seconds=60),
         Costs("double_cost_delay_60s", 2.0, 0.10, 0.10, 0.50, 60)]


def outward(side: int, price: float) -> float:
    return float(Decimal(str(price)).quantize(Decimal("0.01"),
                 rounding=ROUND_FLOOR if side == 1 else ROUND_CEILING))


def load_cache(directory: Path):
    meta_path = directory / "rates_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    offset = float(meta["server_offset"])
    point = float(meta["point"])
    contract = float(meta["contract_size"])
    if not all(np.isfinite(x) and x > 0 for x in [point, contract]):
        raise ValueError("invalid point/contract_size")
    if not np.isfinite(offset) or abs(offset) > 14:
        raise ValueError("invalid server_offset")
    data, audit = {}, {}
    for name, seconds in PERIODS.items():
        path = directory / ("rates_" + name + ".csv")
        d = pd.read_csv(path)
        if len(d) == 0:
            raise ValueError("empty " + name)
        required = ["time", "open", "high", "low", "close"]
        if not set(required) <= set(d):
            raise ValueError("missing OHLC in " + name)
        vals = d[required].to_numpy(dtype=float)
        if not np.isfinite(vals).all() or (d.time.diff().dropna() <= 0).any():
            raise ValueError("non-finite/unsorted/duplicate rates in " + name)
        if (d.time % seconds != 0).any():
            raise ValueError("non-aligned timestamps in " + name)
        if ((d.high < d[["open", "close", "low"]].max(axis=1)) |
                (d.low > d[["open", "close", "high"]].min(axis=1))).any():
            raise ValueError("invalid OHLC in " + name)
        d["time"] = (d.time - offset * 3600).astype("int64")
        if "spread" in d:
            spread = d.spread.astype(float)
            if not np.isfinite(spread).all() or (spread < 0).any():
                raise ValueError("invalid spread in " + name)
            d["spread_abs"] = spread.where(spread > 0, float(meta.get("spread_now", 33))) * point
        else:
            d["spread_abs"] = float(meta.get("spread_now", 33)) * point
        audit[name] = {"rows": len(d), "start_utc": utc_text(int(d.time.iloc[0])),
                       "end_utc": utc_text(int(d.time.iloc[-1] + seconds)),
                       "gaps": int((d.time.diff() > seconds).sum()),
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        data[name] = d.reset_index(drop=True)
    audit["metadata_sha256"] = hashlib.sha256(meta_path.read_bytes()).hexdigest()
    return data, meta, audit


def utc_text(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def epoch(text: str) -> int:
    t = pd.Timestamp(text)
    if t.tzinfo is None:
        t = t.tz_localize("Asia/Shanghai")
    return int(t.timestamp())


def features(d: pd.DataFrame, seconds: int) -> pd.DataFrame:
    f = d.copy()
    c = d.close
    prev = c.shift()
    tr = pd.concat([d.high - d.low, (d.high - prev).abs(), (d.low - prev).abs()], axis=1).max(axis=1)
    f["atr"] = tr.rolling(14).mean()
    f["ema20"] = c.ewm(span=20, adjust=False).mean()
    f["ema50"] = c.ewm(span=50, adjust=False).mean()
    f["slope"] = f.ema20 - f.ema20.shift(3)
    f["er"] = (c - c.shift(10)).abs() / c.diff().abs().rolling(10).sum().replace(0, np.nan)
    f["high12"] = d.high.shift().rolling(12).max()
    f["low12"] = d.low.shift().rolling(12).min()
    f["high24"] = d.high.shift().rolling(24).max()
    f["low24"] = d.low.shift().rolling(24).min()
    f["high20"] = d.high.shift().rolling(20).max()
    f["low20"] = d.low.shift().rolling(20).min()
    f["low3"] = d.low.rolling(3).min()
    f["high3"] = d.high.rolling(3).max()
    f["low6"] = d.low.rolling(6).min()
    f["high6"] = d.high.rolling(6).max()
    f["close_time"] = d.time + seconds
    return f


def closed_context(frame: pd.DataFrame, times: np.ndarray, columns: list[str], prefix: str):
    """As-of align only bars whose close_time <= decision time; never back-fill."""
    left = pd.DataFrame({"close_time": times})
    right = frame[["close_time"] + columns].rename(columns={c: prefix + c for c in columns})
    right[prefix + "closed_at"] = right.close_time
    return pd.merge_asof(left, right, on="close_time", direction="backward")


def make_signal(r, name, side, rr, hold, stop_low=None, stop_high=None, trail=False, session_key=""):
    atr = float(r.atr)
    sp = float(r.spread_abs)
    buffer = max(0.15 * atr, 0.20)
    sl = (float(r.low3 if stop_low is None else stop_low) - buffer if side == 1 else
          float(r.high3 if stop_high is None else stop_high) + buffer + sp)
    sl = outward(side, sl)
    reference = float(r.close) + (sp if side == 1 else 0.0)
    risk = side * (reference - sl)
    if not np.isfinite(risk) or risk <= 0:
        return None
    tp = round(reference + side * rr * risk, 2)
    return Signal(int(r.close_time), name, side, sl, tp, atr, hold, reference, trail, session_key)


def generate_signals(data: dict[str, pd.DataFrame]):
    f5 = features(data["M5"], 300)
    f15 = features(data["M15"], 900)
    fh = features(data["H1"], 3600)
    hc = closed_context(fh, f5.close_time.to_numpy(), ["close", "ema20", "ema50", "slope", "er"], "h_")
    f5 = f5.join(hc.drop(columns="close_time"))
    f5["trend"] = np.where((f5.h_ema20 > f5.h_ema50) & (f5.h_slope > 0) &
                            (f5.h_close > f5.h_ema20), 1,
                    np.where((f5.h_ema20 < f5.h_ema50) & (f5.h_slope < 0) &
                             (f5.h_close < f5.h_ema20), -1, 0))
    f5["fresh_h"] = f5.close_time - f5.h_closed_at < 3900
    signals = {name: [] for name in NAMES}
    for r in f5.itertuples(index=False):
        if not np.isfinite(r.atr) or not r.fresh_h or not np.isfinite(r.low24):
            continue
        width = r.high - r.low
        body = abs(r.close - r.open)
        if width <= 0:
            continue
        for side in [1, -1]:
            close_location = (r.close - r.low) / width if side == 1 else (r.high - r.close) / width
            breakout = r.close > r.high12 + 0.05 * r.atr if side == 1 else r.close < r.low12 - 0.05 * r.atr
            prior_inside = r.open <= r.high12 if side == 1 else r.open >= r.low12
            if r.trend == side and breakout and prior_inside and body >= 0.5 * r.atr and close_location >= 0.70:
                s = make_signal(r, "continuation", side, 3.0, 180)
                if s:
                    signals[s.strategy].append(s)
            # Failed breakout requires an actual sweep and close back inside; never merely high RSI.
            sweep = (r.low < r.low24 - 0.05 * r.atr and r.close > r.low24 + 0.05 * r.atr) if side == 1 else (
                    r.high > r.high24 + 0.05 * r.atr and r.close < r.high24 - 0.05 * r.atr)
            wick = (min(r.open, r.close) - r.low) / width if side == 1 else (r.high - max(r.open, r.close)) / width
            not_strong_opposite = r.trend != -side or r.h_er <= 0.35
            if sweep and wick >= 0.45 and close_location >= 0.60 and not_strong_opposite:
                s = make_signal(r, "sweep", side, 2.0, 90)
                if s:
                    signals[s.strategy].append(s)
    hc15 = closed_context(fh, f15.close_time.to_numpy(), ["ema20", "ema50", "slope"], "h_")
    f15 = f15.join(hc15.drop(columns="close_time"))
    m5c = closed_context(f5, f15.close_time.to_numpy(), ["low3", "high3"], "m5_")
    f15 = f15.join(m5c.drop(columns="close_time"))
    for r in f15.itertuples(index=False):
        if (not np.isfinite(r.atr) or not np.isfinite(r.high12) or
                r.close_time - r.h_closed_at >= 3900 or r.close_time - r.m5_closed_at >= 300):
            continue
        compressed = 0.8 * r.atr <= r.high12 - r.low12 <= 3.0 * r.atr
        for side in [1, -1]:
            trend = r.h_ema20 > r.h_ema50 and r.h_slope > 0 if side == 1 else r.h_ema20 < r.h_ema50 and r.h_slope < 0
            brk = r.close > r.high12 + 0.10 * r.atr if side == 1 else r.close < r.low12 - 0.10 * r.atr
            if compressed and trend and brk:
                s = make_signal(r, "compression", side, 3.0, 240, r.m5_low3, r.m5_high3)
                if s:
                    signals[s.strategy].append(s)
    for r in fh.itertuples(index=False):
        if not np.isfinite(r.atr) or not np.isfinite(r.high20):
            continue
        for side in [1, -1]:
            brk = r.close > r.high20 and r.ema20 > r.ema50 if side == 1 else r.close < r.low20 and r.ema20 < r.ema50
            if brk:
                s = make_signal(r, "swing", side, 4.0, 2880, r.low6, r.high6, trail=True)
                if s:
                    signals[s.strategy].append(s)
    # Custom liquidity windows, not a claim that OTC gold has an exchange opening auction.
    # Grouping uses bar OPEN local time; incomplete opening ranges are rejected.
    for zone, label in [("Europe/London", "London"), ("America/New_York", "NewYork")]:
        local = pd.to_datetime(f5.time, unit="s", utc=True).dt.tz_convert(zone)
        work = f5.copy()
        work["date_key"] = local.dt.strftime("%Y-%m-%d")
        work["minute"] = local.dt.hour * 60 + local.dt.minute
        for day, group in work.groupby("date_key", sort=True):
            opening = group[(group.minute >= 480) & (group.minute < 510)]
            if len(opening) != 6 or not np.all(np.diff(opening.time) == 300):
                continue
            hi, lo = float(opening.high.max()), float(opening.low.min())
            a = float(opening.atr.iloc[-1])
            if not np.isfinite(a) or not 1.0 * a <= hi - lo <= 6.0 * a:
                continue
            # First confirmed breakout per session only; failed execution does not chase later.
            candidates = list(group[(group.minute >= 510) & (group.minute < 660)].itertuples(index=False))
            for j, r in enumerate(candidates):
                if not r.fresh_h:
                    continue
                side = 1 if r.close > hi + 0.05 * r.atr else -1 if r.close < lo - 0.05 * r.atr else 0
                if side:
                    s = make_signal(r, "session", side, 3.0, 180, session_key=label + "-" + day)
                    if s:
                        signals[s.strategy].append(s)
                    # Exploratory follow-up after round 1 showed latency sensitivity.
                    # Separate rule: first pullback in the next six M5 bars, then close outside.
                    boundary = hi if side == 1 else lo
                    for rr in candidates[j + 1:j + 7]:
                        if rr.close_time - r.close_time > 1800 or not rr.fresh_h:
                            break
                        invalid = (rr.close < boundary - .30 * rr.atr if side == 1 else
                                   rr.close > boundary + .30 * rr.atr)
                        if invalid:
                            break
                        touch = (rr.low <= boundary + .25 * rr.atr if side == 1 else
                                 rr.high >= boundary - .25 * rr.atr)
                        defended = (rr.close > boundary + .05 * rr.atr if side == 1 else
                                    rr.close < boundary - .05 * rr.atr)
                        if touch and defended:
                            retest = make_signal(rr, "session_retest", side, 3.0, 180,
                                                 session_key=label + "-" + day)
                            if retest:
                                signals[retest.strategy].append(retest)
                            break
                    break
    return {name: sorted(values, key=lambda s: s.time) for name, values in signals.items()}


def exit_on_bar(position, row, spread, slip):
    """Bid OHLC for buys; ask approximated with bar spread for sells.

    Gap stops fill at the adverse open. A bar touching SL and TP chooses SL.
    TP is a limit: no favorable gap/slippage is assumed.
    """
    side, sl, tp = position["signal"].side, position["stop"], position["target"]
    o, h, l = float(row.open), float(row.high), float(row.low)
    if side == -1:
        o, h, l = o + spread, h + spread, l + spread
    if side == 1:
        if o <= sl:
            return o - slip, "gap_sl"
        if o >= tp:
            return tp, "tp"
        if l <= sl:
            return sl - slip, "sl"
        if h >= tp:
            return tp, "tp"
    else:
        if o >= sl:
            return o + slip, "gap_sl"
        if o <= tp:
            return tp, "tp"
        if h >= sl:
            return sl + slip, "sl"
        if l <= tp:
            return tp, "tp"
    return None


def validate_fill(s: Signal, bid: float, spread: float, costs: Costs, cap: float):
    entry = bid + spread + costs.slippage if s.side == 1 else bid - costs.slippage
    risk, reward = s.side * (entry - s.stop), s.side * (s.target - entry)
    trigger = bid if s.side == 1 else bid + spread
    margin = s.side * (trigger - s.stop)
    if risk <= 0 or reward <= 0:
        return None, "crossed_plan"
    if risk > cap + 1e-9:
        return None, "fill_stop_cap"
    if margin < max(2.0 * spread, 0.15 * s.atr, 0.20) or risk < 3 * spread:
        return None, "stop_too_close"
    if reward / risk < 1.5:
        return None, "fill_rr"
    return {"signal": s, "entry": entry, "stop": s.stop, "target": s.target,
            "risk": risk, "rollovers": 0, "best": trigger}, None


def metrics(trades, balance, min_equity, max_dd, ruined, rejects, initial_balance=100.0):
    pnl = [t["pnl"] for t in trades]
    win = sum(x for x in pnl if x > 0)
    loss = -sum(x for x in pnl if x < 0)
    streak = current = 0
    for x in pnl:
        current = current + 1 if x < 0 else 0
        streak = max(streak, current)
    return {"trades": len(pnl), "net_pnl": round(balance - initial_balance, 4),
            "net_return_pct": round((balance / initial_balance - 1) * 100, 4),
            "win_rate": round(sum(x > 0 for x in pnl) / len(pnl), 4) if pnl else None,
            "profit_factor": round(win / loss, 4) if loss else None,
            "mean_r": round(float(np.mean([t["net_r"] for t in trades])), 4) if pnl else None,
            "max_drawdown_dollars": round(max_dd, 4), "min_equity": round(min_equity, 4),
            "max_loss_streak": streak, "ruined": ruined,
            "max_initial_risk_dollars": round(max((t["risk_dollars"] for t in trades), default=0), 4),
            "mean_hold_minutes": round(float(np.mean([t["hold_minutes"] for t in trades])), 2) if pnl else None,
            "rejections": dict(rejects)}


def simulate(m1, signals, costs: Costs, cap=8.0, lot_value=1.0, initial_balance=100.0):
    """One fixed 0.01-lot position per independently reset $100 virtual account.

    No margin/stop-out model: cash exhaustion is enforced, not broker executability.
    Every entry uses the next M1 OPEN after the closed signal (+explicit delay).
    Signals without an exact cached fill minute are discarded, never carried over gaps.
    """
    if min(cap, lot_value, initial_balance) <= 0 or not np.isfinite([cap, lot_value, initial_balance]).all():
        raise ValueError("invalid cap/lot value")
    times = m1.time.to_numpy(dtype=np.int64)
    scheduled = {}
    rejects = Counter()
    for s in signals:
        fill_time = s.time + costs.delay_seconds
        i = int(np.searchsorted(times, fill_time))
        if i >= len(times) or times[i] != fill_time:
            rejects["missing_fill_minute"] += 1
            continue
        scheduled.setdefault(i, []).append(s)
    ny = pd.to_datetime(m1.time, unit="s", utc=True).dt.tz_convert("America/New_York")
    roll_key = (ny - pd.Timedelta(hours=17)).dt.strftime("%Y-%m-%d").to_numpy()
    position = None
    balance = peak = min_equity = float(initial_balance)
    max_dd = 0.0
    trades = []
    ruined = False
    cooldown_until = 0
    rows = list(m1.itertuples(index=False))

    def adverse_equity(row, spread, outcome=None, at_open=False):
        """Conservative M1 drawdown bound, excluding prices after a triggered stop.

        For a TP-only bar, the low/high ordering is unknown: take the adverse
        excursion before TP. A planned open-time exit never sees later OHLC.
        """
        nonlocal min_equity, max_dd
        p, s = position, position["signal"]
        trigger = float(row.open) + (spread if s.side == -1 else 0)
        if at_open:
            worst = trigger
        elif outcome and outcome[1] in ("sl", "gap_sl"):
            worst = outcome[0] + s.side * costs.slippage
        else:
            worst = float(row.low) if s.side == 1 else float(row.high) + spread
        equity = balance + s.side * (worst - p["entry"]) * lot_value
        equity -= costs.round_trip_fee + p["rollovers"] * costs.rollover_fee + costs.slippage * lot_value
        min_equity = min(min_equity, equity)
        max_dd = max(max_dd, peak - equity)

    def close(price, i, reason):
        nonlocal position, balance, peak, min_equity, max_dd, cooldown_until, ruined
        p = position
        s = p["signal"]
        pnl = s.side * (price - p["entry"]) * lot_value - costs.round_trip_fee - p["rollovers"] * costs.rollover_fee
        balance += pnl
        peak = max(peak, balance)
        min_equity = min(min_equity, balance)
        max_dd = max(max_dd, peak - balance)
        trades.append({"strategy": s.strategy, "side": "BUY" if s.side == 1 else "SELL",
                       "signal_utc": utc_text(s.time), "open_utc": utc_text(p["open_time"]),
                       "close_utc": utc_text(int(times[i])), "entry": p["entry"], "initial_sl": s.stop,
                       "target": s.target, "exit": price, "reason": reason, "pnl": pnl,
                       "risk_dollars": p["risk"] * lot_value, "net_r": pnl / (p["risk"] * lot_value),
                       "hold_minutes": (int(times[i]) - p["open_time"]) / 60, "rollovers": p["rollovers"]})
        cooldown_until = int(times[i]) + 600
        position = None
        if balance <= 0:
            ruined = True

    for i, row in enumerate(rows):
        ts = int(row.time)
        spread = float(row.spread_abs) * costs.spread_multiplier
        if ruined:
            break
        if position and roll_key[i] != position["roll_key"]:
            # Includes elapsed calendar rollovers over missing weekend bars.
            days = (pd.Timestamp(roll_key[i]) - pd.Timestamp(position["roll_key"])).days
            position["rollovers"] += max(days, 0)
            position["roll_key"] = roll_key[i]
        if position:
            # A time exit is scheduled in advance; market-open SL remains first.
            outcome = exit_on_bar(position, row, spread, costs.slippage)
            elapsed = (ts - position["open_time"]) / 60
            if elapsed >= position["signal"].max_hold_minutes:
                side = position["signal"].side
                trigger = float(row.open) + (spread if side == -1 else 0)
                gap_stop = trigger <= position["stop"] if side == 1 else trigger >= position["stop"]
                adverse_equity(row, spread, at_open=True)
                if gap_stop:
                    close(trigger - side * costs.slippage, i, "gap_sl")
                else:
                    close(trigger - side * costs.slippage, i, "time_exit")
            elif outcome:
                adverse_equity(row, spread, outcome)
                close(outcome[0], i, outcome[1])
        if not position and not ruined and ts >= cooldown_until:
            for s in scheduled.get(i, []):
                p, reason = validate_fill(s, float(row.open), spread, costs, cap)
                if reason:
                    rejects[reason] += 1
                    continue
                if p["risk"] * lot_value + costs.round_trip_fee >= balance:
                    rejects["insufficient_cash_for_risk"] += 1
                    continue
                p.update(open_time=ts, roll_key=roll_key[i])
                position = p
                # Entry bar is included. A signal may stop out in its first minute.
                outcome = exit_on_bar(p, row, spread, costs.slippage)
                if outcome:
                    adverse_equity(row, spread, outcome)
                    close(outcome[0], i, outcome[1])
                break
        elif i in scheduled:
            rejects["occupied_or_cooldown"] += len(scheduled[i])
        if position:
            s = position["signal"]
            unfavorable = float(row.low) if s.side == 1 else float(row.high) + spread
            favorable = float(row.high) if s.side == 1 else float(row.low) + spread
            equity = balance + s.side * (unfavorable - position["entry"]) * lot_value
            equity -= costs.round_trip_fee + position["rollovers"] * costs.rollover_fee + costs.slippage * lot_value
            min_equity = min(min_equity, equity)
            # Use the prior peak for this bar's adverse excursion, then update with CLOSE.
            max_dd = max(max_dd, peak - equity)
            mark = float(row.close) + (spread if s.side == -1 else 0)
            peak = max(peak, balance + s.side * (mark - position["entry"]) * lot_value -
                       costs.round_trip_fee - position["rollovers"] * costs.rollover_fee)
            if equity <= 0:
                close(unfavorable - s.side * costs.slippage, i, "cash_exhaustion")
                ruined = True
            elif s.trail:
                position["best"] = max(position["best"], favorable) if s.side == 1 else min(position["best"], favorable)
                if s.side * (position["best"] - position["entry"]) >= 2 * position["risk"]:
                    candidate = outward(s.side, position["best"] - s.side * 3 * s.atr)
                    # Updated at closed M1; only applies to the NEXT M1, never retrospectively.
                    position["stop"] = max(position["stop"], candidate) if s.side == 1 else min(position["stop"], candidate)
    if position:
        row = rows[-1]
        spread = float(row.spread_abs) * costs.spread_multiplier
        side = position["signal"].side
        price = float(row.close) + (spread if side == -1 else 0) - side * costs.slippage
        close(price, len(rows) - 1, "end_of_sample")
        trades[-1]["close_utc"] = utc_text(int(times[-1] + 60))
        trades[-1]["hold_minutes"] += 1
    return metrics(trades, balance, min_equity, max_dd, ruined, rejects, initial_balance), trades


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "xau_fib_pa_logs/backtest")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "xau_fib_pa_logs/strategy_lab/2026-10-09-final")
    parser.add_argument("--start", default="2026-07-20T00:00:00+08:00")
    parser.add_argument("--split", default="2026-09-15T00:00:00+08:00")
    parser.add_argument("--end", default="2026-10-07T00:00:00+08:00")
    args = parser.parse_args()
    data, meta, audit = load_cache(args.cache_dir)
    signals = generate_signals(data)
    start, split, end = map(epoch, [args.start, args.split, args.end])
    if not start < split < end:
        raise ValueError("require start < split < end")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = {"version": VERSION, "rules_frozen": "original 5 strategies before round 1",
               "exploratory_followups": "retest and $1000/SL48 swing after round 1; combined cost/delay stress after round 2; no parameter grid",
               "selection_warning": "chronological validation is not pristine unseen data; no parameter search",
               "cash": 100, "lot": 0.01, "lot_value": float(meta["contract_size"]) * 0.01,
               "metadata": meta, "data_audit": audit, "costs": [asdict(c) for c in COSTS], "runs": []}
    for part, a, b in [("discovery", start, split), ("validation", split, end)]:
        m1 = data["M1"][(data["M1"].time >= a) & (data["M1"].time + 60 <= b)].reset_index(drop=True)
        if m1.empty:
            raise ValueError("empty sample " + part)
        for name in NAMES:
            chosen = [s for s in signals[name] if a <= s.time < b]
            profiles = [(100.0, 8.0), (100.0, 16.0)]
            if name == "swing":
                profiles.append((1000.0, 48.0))
            for cash, cap in profiles:
                for costs in COSTS:
                    result, trades = simulate(m1, chosen, costs, cap, summary["lot_value"], cash)
                    key = f"{part}_{name}_sl{int(cap)}_{costs.name}"
                    row = dict(part=part, strategy=name, label=NAMES[name], cash=cash, max_sl=cap, cost=costs.name,
                               signals=len(chosen), sample_start=utc_text(int(m1.time.iloc[0])),
                               sample_end=utc_text(int(m1.time.iloc[-1] + 60)), **result)
                    summary["runs"].append(row)
                    pd.DataFrame(trades).to_csv(args.output_dir / (key + "_trades.csv"), index=False)
                    print(f"{key}: n={result['trades']} pnl={result['net_pnl']:.2f} "
                          f"dd={result['max_drawdown_dollars']:.2f} ruin={result['ruined']}")
    pd.DataFrame(summary["runs"]).to_csv(args.output_dir / "summary.csv", index=False)
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                                            allow_nan=False), encoding="utf-8")
    print("Results:", args.output_dir)


if __name__ == "__main__":
    main()
