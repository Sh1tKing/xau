# -*- coding: utf-8 -*-
"""Replay MT5 history through the local XAUUSD strategy.

Every closed M5 bar runs build_fib_volume_pa_decision() with the same context the live
script sees. Fills and the local guardian (breakeven, virtual trailing line, structure
failure, real SL/TP) are simulated on M1 bars. AI-1/2/3 are not called.
"""

import argparse
import bisect
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

import xauusd_fib_volume_pa_trader as base

mt5 = base.mt5
pd = base.pd

OUT_DIR = os.path.join(base.LOG_DIR, "backtest")
FRAMES = {
    "M1": (mt5.TIMEFRAME_M1, 60, 1440, 200),
    "M5": (mt5.TIMEFRAME_M5, 300, 288, 200),
    "M15": (mt5.TIMEFRAME_M15, 900, 96, 200),
    "H1": (mt5.TIMEFRAME_H1, 3600, 24, 150),
}


class Window:
    """Closed-bar lookups: upto(ts) returns bars whose close time is <= ts."""

    def __init__(self, df, seconds):
        self.df = df.reset_index(drop=True)
        self.close_times = (self.df["time"].astype(int) + seconds).tolist()

    def upto(self, ts, count):
        end = bisect.bisect_right(self.close_times, ts)
        return self.df.iloc[max(0, end - count):end].reset_index(drop=True)


def beijing(server_ts, offset_hours):
    return datetime.fromtimestamp(server_ts - offset_hours * 3600, tz=base.BEIJING_TZ)


def connect():
    if mt5.initialize() and mt5.account_info() is not None:
        return mt5.symbol_select(base.SYMBOL, True)
    return base.FibVolumePATrader().connect_mt5()


def detect_server_offset():
    tick = mt5.symbol_info_tick(base.SYMBOL)
    if tick is None:
        return None
    hours = (float(tick.time) - time.time()) / 3600.0
    if abs(hours) > 14:
        return None
    return float(round(hours))


def fetch_history(days):
    data = {}
    for name, (timeframe, _, per_day, warmup) in FRAMES.items():
        count = days * per_day + warmup
        rates = mt5.copy_rates_from_pos(base.SYMBOL, timeframe, 1, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError("%s 历史数据为空: %s" % (name, mt5.last_error()))
        df = base.normalize_rates(rates)
        if "spread" not in df.columns:
            df["spread"] = 0
        if len(df) < count:
            print("提示: %s 只拿到 %s/%s 根，MT5 的图表最大K线数可能不够" % (name, len(df), count))
        data[name] = df
    return data


def save_history(data, meta):
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, df in data.items():
        df.to_csv(os.path.join(OUT_DIR, "rates_%s.csv" % name), index=False)
    with open(os.path.join(OUT_DIR, "rates_meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)


def load_history():
    data = {}
    for name in FRAMES:
        path = os.path.join(OUT_DIR, "rates_%s.csv" % name)
        if not os.path.exists(path):
            raise RuntimeError("缺少离线数据 %s，先在线跑一次" % path)
        data[name] = pd.read_csv(path)
    with open(os.path.join(OUT_DIR, "rates_meta.json"), encoding="utf-8") as handle:
        meta = json.load(handle)
    return data, meta


def reject_key(reasoning):
    text = re.sub(r"^\[[^\]]*\]\s*", "", reasoning or "")
    if "拒绝:" in text:
        text = text.split("拒绝:", 1)[1]
    head = re.split(r"[。；，]", text.strip(), maxsplit=1)[0]
    return re.sub(r"\d+\.\d+", "#", head)[:40] or "未写原因"


class Backtest:
    def __init__(self, data, meta, threshold, spread_points=None):
        self.offset = float(meta["server_offset"])
        self.point = float(meta["point"])
        self.lot_value = float(meta["contract_size"]) * base.LOT_SIZE
        self.threshold = threshold
        self.windows = {name: Window(df, FRAMES[name][1]) for name, df in data.items()}
        m1 = data["M1"].reset_index(drop=True)
        self.t = m1["time"].astype(int).tolist()
        self.o = m1["open"].astype(float).tolist()
        self.h = m1["high"].astype(float).tolist()
        self.l = m1["low"].astype(float).tolist()
        self.c = m1["close"].astype(float).tolist()
        fallback = float(meta.get("spread_now") or 0)
        if spread_points is not None:
            self.spreads = [float(spread_points) * self.point] * len(m1)
        else:
            raw = m1["spread"].astype(float).tolist() if "spread" in m1.columns else [0.0] * len(m1)
            self.spreads = [(value or fallback) * self.point for value in raw]
        self.trades = []
        self.rejects = Counter()
        self.stats = Counter()
        self.position = None
        self.pending = None
        self._m5_end = None
        self._m5 = None
        self._atr = 0.0

    def guardian_context(self, ts):
        window = self.windows["M5"]
        end = bisect.bisect_right(window.close_times, ts)
        if end != self._m5_end:
            self._m5_end = end
            self._m5 = window.df.iloc[max(0, end - 120):end].reset_index(drop=True)
            self._atr = base.calculate_atr(self._m5) if len(self._m5) >= 20 else 0.0
        return self._m5, self._atr

    def evaluate(self, ts):
        df_m15 = self.windows["M15"].upto(ts, 160)
        df_h1 = self.windows["H1"].upto(ts, 120)
        df_m5 = self.windows["M5"].upto(ts, 40)
        if len(df_m15) < 80 or len(df_h1) < 60 or len(df_m5) < 10:
            self.stats["数据不足跳过"] += 1
            return None
        self.stats["评估次数"] += 1
        try:
            result = base.build_fib_volume_pa_decision(
                df_m15, df_h1, df_m5, self.threshold, now=beijing(ts, self.offset)
            )
        except Exception as exc:
            self.stats["评估异常"] += 1
            self.rejects["评估异常 %s" % type(exc).__name__] += 1
            return None
        if result.action in ("buy", "sell") and result.confidence >= self.threshold:
            self.stats["本地信号"] += 1
            return result
        self.rejects[reject_key(result.reasoning)] += 1
        return None

    def open_position(self, result, i, signal_ts):
        side = result.action
        bid = self.o[i]
        entry = bid + self.spreads[i] if side == "buy" else bid
        sl, tp = float(result.stop_loss), float(result.take_profit)
        if base.reward_risk_ratio(side, entry, sl, tp) < base.MIN_REWARD_RISK:
            self.stats["成交价盈亏比不足放弃"] += 1
            return
        risk = entry - sl if side == "buy" else sl - entry
        state = base.GuardianState(
            ticket=len(self.trades) + 1,
            direction=side.upper(),
            entry_price=entry,
            initial_sl=sl,
            initial_tp=tp,
            risk=max(risk, 0.01),
            highest_price=entry,
            lowest_price=entry,
        )
        details = result.details or {}
        self.position = {
            "side": side,
            "state": state,
            "sl": sl,
            "tp": tp,
            "open_ts": self.t[i],
            "signal_ts": signal_ts,
            "score": result.confidence,
            "session": details.get("session", ""),
            "setup": details.get("setup", ""),
        }
        self.stats["成交"] += 1

    def close(self, ts, price, reason):
        pos = self.position
        state = pos["state"]
        sign = 1 if pos["side"] == "buy" else -1
        pnl = sign * (price - state.entry_price) * self.lot_value
        self.trades.append({
            "open_bj": beijing(pos["open_ts"], self.offset).strftime("%Y-%m-%d %H:%M"),
            "close_bj": beijing(ts, self.offset).strftime("%Y-%m-%d %H:%M"),
            "session": base.SESSION_TEXT.get(pos["session"], pos["session"]),
            "setup": base.SETUP_TEXT.get(pos["setup"], pos["setup"]),
            "side": pos["side"].upper(),
            "score": pos["score"],
            "entry": round(state.entry_price, 3),
            "sl": round(state.initial_sl, 3),
            "tp": round(state.initial_tp, 3),
            "exit": round(price, 3),
            "exit_reason": reason,
            "pnl": round(pnl, 2),
            "r": round(pnl / (state.risk * self.lot_value), 2),
            "hold_min": int((ts - pos["open_ts"]) / 60),
        })
        self.position = None
        return True

    def check_exit(self, i):
        # Pessimistic intrabar order: a bar that reaches both the stop and the target counts as a stop.
        pos = self.position
        state = pos["state"]
        t = self.t[i]
        moved = abs(pos["sl"] - state.initial_sl) > 1e-6
        reason = "移动止损" if moved else "初始止损"
        if pos["side"] == "buy":
            stop = pos["sl"]
            if state.virtual_sl > stop:
                stop, reason = state.virtual_sl, "虚拟锁盈线"
            if self.o[i] <= stop:
                return self.close(t, self.o[i], reason + "(跳空)")
            if self.l[i] <= stop:
                return self.close(t, stop, reason)
            if pos["tp"] > 0 and self.h[i] >= pos["tp"]:
                return self.close(t, pos["tp"], "止盈")
            return False
        spread = self.spreads[i]
        stop = pos["sl"]
        if 0 < state.virtual_sl < stop:
            stop, reason = state.virtual_sl, "虚拟锁盈线"
        if self.o[i] + spread >= stop:
            return self.close(t, self.o[i] + spread, reason + "(跳空)")
        if self.h[i] + spread >= stop:
            return self.close(t, stop, reason)
        if pos["tp"] > 0 and self.l[i] + spread <= pos["tp"]:
            return self.close(t, pos["tp"], "止盈")
        return False

    def update_guardian(self, i):
        # Same rules as FibVolumePATrader.run_guardian(), evaluated once per closed M1 bar.
        pos = self.position
        state = pos["state"]
        _, atr = self.guardian_context(self.t[i] + 60)
        if atr <= 0:
            return
        disaster = max(base.REAL_SL_DISASTER_MIN, base.REAL_SL_DISASTER_ATR * atr)
        step = max(base.MIN_SL_MODIFY_ABS, base.MIN_SL_MODIFY_ATR * atr)
        if pos["side"] == "buy":
            price = self.c[i]
            state.highest_price = max(state.highest_price, self.h[i])
            max_profit = max(0.0, state.highest_price - state.entry_price)
            line = state.highest_price - base.calculate_sigmoid_buffer(max_profit / atr) * atr
            state.virtual_sl = max(state.virtual_sl, line) if state.virtual_sl > 0 else line
            if not state.breakeven_done and (
                max_profit / state.risk >= base.BREAKEVEN_TRIGGER_R or max_profit >= base.BREAKEVEN_TRIGGER_ATR * atr
            ):
                be_sl = round(state.entry_price + base.BREAKEVEN_BUFFER_ATR * atr, 2)
                if be_sl < price and be_sl > pos["sl"] + step:
                    pos["sl"] = be_sl
                    state.breakeven_done = True
            target = round(state.virtual_sl - disaster, 2)
            if 0 < target < price and target > pos["sl"] + step:
                pos["sl"] = target
            return
        spread = self.spreads[i]
        price = self.c[i] + spread
        state.lowest_price = min(state.lowest_price, self.l[i] + spread)
        max_profit = max(0.0, state.entry_price - state.lowest_price)
        line = state.lowest_price + base.calculate_sigmoid_buffer(max_profit / atr) * atr
        state.virtual_sl = min(state.virtual_sl, line) if state.virtual_sl > 0 else line
        if not state.breakeven_done and (
            max_profit / state.risk >= base.BREAKEVEN_TRIGGER_R or max_profit >= base.BREAKEVEN_TRIGGER_ATR * atr
        ):
            be_sl = round(state.entry_price - base.BREAKEVEN_BUFFER_ATR * atr, 2)
            if be_sl > price and be_sl < pos["sl"] - step:
                pos["sl"] = be_sl
                state.breakeven_done = True
        target = round(state.virtual_sl + disaster, 2)
        if target > price and target < pos["sl"] - step:
            pos["sl"] = target

    def check_structure(self, close_ts):
        m5, atr = self.guardian_context(close_ts)
        if atr <= 0 or m5 is None:
            return
        failed, _ = base.is_structure_failure(m5, self.position["side"], self.position["state"], atr)
        if failed:
            self.position["exit_next"] = "结构失效"

    def run(self):
        m5_closes = set(self.windows["M5"].close_times)
        total = len(self.t)
        mark = max(1, total // 10)
        started = time.time()
        for i in range(total):
            if i and i % mark == 0:
                print("  进度 %d%%，已成交 %d 笔，用时 %.0fs" % (100 * i // total, len(self.trades), time.time() - started))
            if self.pending is not None and self.position is None:
                result, signal_ts = self.pending
                self.pending = None
                self.open_position(result, i, signal_ts)
            if self.position is not None:
                reason = self.position.pop("exit_next", None)
                if reason:
                    bid = self.o[i]
                    price = bid if self.position["side"] == "buy" else bid + self.spreads[i]
                    self.close(self.t[i], price, reason)
                elif not self.check_exit(i):
                    self.update_guardian(i)
            close_ts = self.t[i] + 60
            if close_ts not in m5_closes:
                continue
            if self.position is not None:
                self.check_structure(close_ts)
            elif self.pending is None:
                result = self.evaluate(close_ts)
                if result is not None:
                    self.pending = (result, close_ts)
        if self.position is not None:
            last = total - 1
            bid = self.c[last]
            price = bid if self.position["side"] == "buy" else bid + self.spreads[last]
            self.close(self.t[last] + 60, price, "回测结束平仓")
        return self.trades


def summarize(trades, stats, rejects, meta, start_balance):
    lines = []
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    equity, peak, max_dd, streak, worst_streak = start_balance, start_balance, 0.0, 0, 0
    for trade in trades:
        equity += trade["pnl"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        streak = streak + 1 if trade["pnl"] <= 0 else 0
        worst_streak = max(worst_streak, streak)
    lines.append("区间(北京时间): %s 到 %s" % (meta["start_bj"], meta["end_bj"]))
    lines.append("服务器时差: UTC%+g，点差: %s" % (meta["server_offset"], meta["spread_note"]))
    lines.append("本地阈值: %s，手数: %s，起始资金: %.2f" % (meta["threshold"], base.LOT_SIZE, start_balance))
    lines.append("")
    lines.append("成交 %d 笔，盈利 %d，亏损 %d，胜率 %.1f%%" % (
        len(trades), len(wins), len(losses), 100.0 * len(wins) / len(trades) if trades else 0.0))
    lines.append("净盈亏 %.2f 美元，期末资金 %.2f，最大回撤 %.2f 美元" % (equity - start_balance, equity, max_dd))
    if trades:
        lines.append("平均盈利 %.2f，平均亏损 %.2f，盈亏因子 %s，最长连亏 %d 笔" % (
            gross_win / len(wins) if wins else 0.0,
            -gross_loss / len(losses) if losses else 0.0,
            "%.2f" % (gross_win / gross_loss) if gross_loss > 0 else "无亏损",
            worst_streak,
        ))
        lines.append("平均持仓 %.0f 分钟，平均 R %.2f" % (
            sum(t["hold_min"] for t in trades) / len(trades), sum(t["r"] for t in trades) / len(trades)))

    def group(title, key):
        buckets = defaultdict(list)
        for trade in trades:
            buckets[trade[key]].append(trade)
        if not buckets:
            return
        lines.append("")
        lines.append(title)
        for name, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            won = sum(1 for t in items if t["pnl"] > 0)
            lines.append("  %-14s %3d 笔  胜率 %5.1f%%  净盈亏 %8.2f" % (
                name, len(items), 100.0 * won / len(items), sum(t["pnl"] for t in items)))

    group("按时段:", "session")
    group("按进场方式:", "setup")
    group("按方向:", "side")
    group("按出场原因:", "exit_reason")
    lines.append("")
    lines.append("流程计数: " + "，".join("%s %d" % kv for kv in stats.most_common()))
    lines.append("")
    lines.append("没有开仓的主要原因(前 12):")
    for reason, count in rejects.most_common(12):
        lines.append("  %6d  %s" % (count, reason))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="用 MT5 历史K线回放本地策略，不调用 AI，不下单。")
    parser.add_argument("--days", type=int, default=30, help="回放最近多少天，默认 30")
    parser.add_argument("--threshold", type=int, default=base.CONFIDENCE_THRESHOLD, help="本地开仓阈值")
    parser.add_argument("--balance", type=float, default=50.0, help="起始资金，只用于算回撤")
    parser.add_argument("--spread", type=float, default=None, help="固定点差(points)，默认用每根K线自带的点差")
    parser.add_argument("--server-offset", type=float, default=None, help="MT5 服务器相对 UTC 的小时数，默认自动检测")
    parser.add_argument("--offline", action="store_true", help="不连 MT5，直接用上次保存的 CSV")
    args = parser.parse_args()

    if args.offline:
        data, meta = load_history()
    else:
        if not connect():
            print("MT5 连接失败。确认 MT5 终端已打开并登录，或加 --offline 用上次的数据。")
            return 1
        try:
            info = mt5.symbol_info(base.SYMBOL)
            offset = args.server_offset if args.server_offset is not None else detect_server_offset()
            if offset is None:
                print("无法自动判断服务器时差（可能休市），请加 --server-offset，例如 --server-offset 3")
                return 1
            print("拉取最近 %d 天的 M1/M5/M15/H1 ..." % args.days)
            data = fetch_history(args.days)
            meta = {
                "server_offset": offset,
                "point": float(info.point),
                "contract_size": float(info.trade_contract_size),
                "spread_now": int(info.spread),
            }
            save_history(data, meta)
        finally:
            mt5.shutdown()
    if args.server_offset is not None:
        meta["server_offset"] = args.server_offset

    m1 = data["M1"]
    meta["start_bj"] = beijing(int(m1["time"].iloc[0]), meta["server_offset"]).strftime("%Y-%m-%d %H:%M")
    meta["end_bj"] = beijing(int(m1["time"].iloc[-1]), meta["server_offset"]).strftime("%Y-%m-%d %H:%M")
    meta["threshold"] = args.threshold
    meta["spread_note"] = "固定 %s points" % args.spread if args.spread is not None else "K线自带点差"
    print("回放 %s 到 %s，M1 共 %d 根" % (meta["start_bj"], meta["end_bj"], len(m1)))

    test = Backtest(data, meta, args.threshold, args.spread)
    trades = test.run()
    report = summarize(trades, test.stats, test.rejects, meta, args.balance)
    print()
    print(report)

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trades_path = os.path.join(OUT_DIR, "trades_%s.csv" % stamp)
    with open(trades_path, "w", newline="", encoding="utf-8-sig") as handle:
        fields = list(trades[0].keys()) if trades else ["open_bj"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(trades)
    report_path = os.path.join(OUT_DIR, "report_%s.txt" % stamp)
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(report + "\n")
    print()
    print("逐笔明细: %s" % trades_path)
    print("汇总报告: %s" % report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
