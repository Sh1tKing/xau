# -*- coding: utf-8 -*-
"""Replay MT5 history through the local XAUUSD strategy.

Every closed M5 bar runs build_fib_volume_pa_decision() with the same context the live
script sees. Fills and the selected local guardian are simulated on M1 bars;
profit-floor stops take effect on the next bar. AI-1/2/3 are not called.
"""

import argparse
import copy
import math
import uuid
from dataclasses import replace
from strategies.events import validate_entry_event
import bisect
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from types import SimpleNamespace

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


HISTORY_PAGE = 20000


def copy_rates_page(timeframe, start_pos, count):
    """Ask for one page. Shrink it if the terminal rejects a large count."""
    ask = min(int(count), HISTORY_PAGE)
    last_error = None
    while True:
        rates = mt5.copy_rates_from_pos(base.SYMBOL, timeframe, int(start_pos), int(ask))
        last_error = mt5.last_error()
        if rates is not None and len(rates) > 0:
            return rates, ask, last_error
        code = last_error[0] if last_error else None
        if code != -2 or ask <= 500:
            return None, ask, last_error
        nxt = max(500, ask // 2)
        if nxt >= ask:
            return None, ask, last_error
        ask = nxt


def fetch_history(days):
    data = {}
    for name, (timeframe, _, per_day, warmup) in FRAMES.items():
        wanted = int(days) * per_day + warmup
        frames = []
        start_pos = 1
        got = 0
        while got < wanted:
            rates, asked, last_error = copy_rates_page(timeframe, start_pos, wanted - got)
            if rates is None or len(rates) == 0:
                if not frames:
                    raise RuntimeError("%s 历史数据为空: %s" % (name, last_error))
                break
            frames.append(base.normalize_rates(rates))
            got += len(rates)
            start_pos += len(rates)
            if len(rates) < asked:
                break
        df = pd.concat(frames, ignore_index=True)
        df = df.drop_duplicates(subset=["time"], keep="last").sort_values("time").reset_index(drop=True)
        if "spread" not in df.columns:
            df["spread"] = 0
        if len(df) < wanted:
            hint = ""
            if len(df) >= 100000:
                hint = "。请在 MT5 工具-选项-图表里调大图表最大柱数和历史最大柱数，重开终端后再拉"
            print("提示: %s 只拿到 %s/%s 根，MT5 的图表最大K线数可能不够%s" % (name, len(df), wanted, hint))
        else:
            print("  %s 拿到 %s 根" % (name, len(df)))
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
    def __init__(self, data, meta, threshold, spread_points=None, *, strategies=None,
                 entry_delay_seconds=0, spread_multiplier=1.0, guardian_policy=None):
        if guardian_policy not in (None, "legacy", "profit_floor_v1", "profit_floor_v2"):
            raise ValueError("unknown guardian policy")
        self.guardian_policy = (None if guardian_policy == "legacy" else base.frozen_guardian_policy())
        if guardian_policy in ("profit_floor_v1", "profit_floor_v2"):
            values = copy.deepcopy(self.guardian_policy) if self.guardian_policy else {}
            values["version"] = guardian_policy.replace("_", "-")
            self.guardian_policy = base.profit_policy(**values)
        self.guard_fee = float(meta.get("guardian_fee_cash", self.guardian_policy["fee_reserve"] if self.guardian_policy else 0.))
        if not math.isfinite(self.guard_fee) or self.guard_fee < 0:
            raise ValueError("invalid replay guardian costs")
        self.strategy_snapshot = base.STRATEGIES.snapshot()
        if self.strategy_snapshot.error:
            raise base.StrategyError(self.strategy_snapshot.error)
        if type(entry_delay_seconds) is not int or entry_delay_seconds < 0 or entry_delay_seconds % 60:
            raise ValueError("entry delay must be nonnegative whole minutes")
        if isinstance(spread_multiplier, bool) or not math.isfinite(spread_multiplier) or spread_multiplier <= 0:
            raise ValueError("spread multiplier must be finite and positive")
        self.entry_delay_seconds = entry_delay_seconds
        self.spread_multiplier = float(spread_multiplier)
        self.original_fingerprint = self.strategy_snapshot.fingerprint
        self.override = strategies
        if strategies is not None:
            ids = strategies.split(',') if isinstance(strategies, str) else list(strategies)
            if not ids or any(not value or value != value.strip() for value in ids) or len(set(ids)) != len(ids):
                raise ValueError("strategy override must contain unique IDs")
            unknown = set(ids) - {s.id for s in self.strategy_snapshot.strategies}
            if unknown:
                raise ValueError("unknown strategy IDs: " + ','.join(sorted(unknown)))
            self.strategy_snapshot = replace(self.strategy_snapshot, strategies=tuple(
                replace(copy.deepcopy(strategy), enabled=strategy.id in ids)
                for strategy in self.strategy_snapshot.strategies))
        self.market_days = set()
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
        self.spreads = [value * self.spread_multiplier for value in self.spreads]
        self.trades = []
        self.rejects = Counter()
        self.candidate_rejects = Counter()
        self.rejected_fills = []
        self.stats = Counter()
        self.position = None
        self.pending = None
        self._m5_end = None
        self._m5 = None
        self._atr = 0.0
        self.consumed_events = set()
        self.cooldown_until_utc = 0.0

    def utc_timestamp(self, server_ts):
        return float(server_ts) - self.offset * 3600

    def entry_gate(self, event_id, decision_utc):
        """Replay-only reservations; the builder's clock already represents UTC."""
        if not isinstance(event_id, str) or not event_id.strip():
            return "策略事件标识缺失"
        if event_id in self.consumed_events:
            return "策略事件已经尝试，不能重复入场"
        if float(decision_utc) < self.cooldown_until_utc:
            return "策略平仓后10分钟冷却中"
        return ""

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
        df_m5 = self.windows["M5"].upto(ts, 80)
        if len(df_m15) < 80 or len(df_h1) < 60 or len(df_m5) < 10:
            self.stats["数据不足跳过"] += 1
            return None
        self.market_days.add(beijing(ts, self.offset).date().isoformat())
        self.stats["评估次数"] += 1
        try:
            result = base.build_fib_volume_pa_decision(
                df_m15, df_h1, df_m5, self.threshold, now=beijing(ts, self.offset),
                strategy_snapshot=self.strategy_snapshot, server_utc_offset=self.offset,
                entry_gate=self.entry_gate,
            )
        except Exception as exc:
            self.stats["评估异常"] += 1
            self.rejects["评估异常 %s" % type(exc).__name__] += 1
            return None
        for opportunity in (result.details or {}).get("opportunities", []):
            self.stats["候选总数"] += 1
            for blocker in opportunity.get("structural_blockers", []):
                self.candidate_rejects[opportunity.get('strategy_contract', {}).get('id', 'unknown') + ': ' + blocker] += 1
            if not opportunity.get("structural_blockers"):
                self.stats["结构候选"] += 1
            if opportunity.get("local_plan_status", {}).get("valid"):
                self.stats["参考计划有效"] += 1
        if result.action in ("buy", "sell") and result.confidence >= self.threshold:
            self.stats["本地信号"] += 1
            return result
        self.rejects[reject_key(result.reasoning)] += 1
        return None

    def research_signal_error(self, result, i, signal_ts):
        """Validate the original event and source; never rebuild a delayed plan."""
        details = result.details or {}
        contract = details.get("strategy_contract", {})
        if (not self.entry_delay_seconds and not contract.get("demo_only")
                and not contract.get("one_entry_per_event")):
            return ""  # Preserve disabled legacy zero-delay replay semantics.
        fill_ts = self.t[i]
        if fill_ts != signal_ts + self.entry_delay_seconds:
            return "研究策略确认后的对应M1开盘缺失，不延迟成交"
        for name in ("M5", "M15", "H1"):
            original = self.windows[name].upto(signal_ts, 1)
            current = self.windows[name].upto(fill_ts, 1)
            if original.empty or current.empty or int(original['time'].iloc[-1]) != int(current['time'].iloc[-1]):
                return "研究策略原确认已过期：新闭合" + name
        if not contract.get("demo_only") and not contract.get("one_entry_per_event"):
            return ""
        assessments = details.get("strategy_evidence", details.get("strategy_assessments", {}))
        evidence = assessments.get(result.action, {}).get("evidence", {})
        event_id = details.get("strategy_event_id")
        if event_id != evidence.get("event_id"):
            return "研究策略原事件标识不一致"
        trigger = evidence.get('closed_trigger', {})
        latest = self.windows.get(trigger.get('timeframe'))
        latest = latest.upto(fill_ts, 1) if latest is not None else None
        if latest is None or latest.empty or trigger.get('bar_time') != int(latest['time'].iloc[-1]):
            return "研究策略原确认不再是最新闭合K线"
        event = evidence.get("entry_event")
        legacy = contract.get("id") == "session_retest"
        if legacy:
            try:
                own = [w for w in evidence['windows'] if w.get('active') is True]
                if len(own) == 1 and self.utc_timestamp(fill_ts) >= datetime.fromisoformat(own[0]['entry_end_utc']).timestamp():
                    return "研究策略原事件时段已过期"
            except (KeyError, TypeError, ValueError):
                pass
        if not legacy:
            bound = evidence.get("host_verified_entry_event", {})
            if (bound.get("entry_event") != event or bound.get("action") != result.action
                    or bound.get("event_id") != event_id or bound.get("server_offset_hours") != self.offset):
                return "研究策略首次事件缺少原始来源绑定"
        if evidence.get("server_offset_hours", self.offset) != self.offset:
            return "研究策略原来源UTC偏移不一致"
        if event and event.get("action") != result.action:
            return "研究策略原事件方向不一致"
        # Old archived session receipts have no complete source list. Only the
        # explicit session adapter may read them; new events prove actual frames.
        frame_args = {} if legacy and event is None else dict(
            df_m5=self.windows['M5'].upto(signal_ts, 80),
            df_m15=self.windows['M15'].upto(signal_ts, 160))
        error = validate_entry_event(evidence, self.utc_timestamp(fill_ts), self.offset,
                                     legacy_session=legacy, **frame_args)
        if error:
            return "研究策略原确认或时段已过期/无效: " + error
        if details.get("reference_bar_time") != evidence.get("closed_trigger", {}).get("bar_time"):
            return "研究策略原确认参考时间不一致"
        if contract.get("stop_reference") == "region":
            verified = evidence.get("host_verified_region", {})
            if (verified.get("entry_event") != event or verified.get("action") != result.action
                    or self.utc_timestamp(fill_ts) >= min(verified.get("active_window", {}).get("end_utc", 0),
                                                         verified.get("region", {}).get("expiry_utc", 0))):
                return "研究策略区域原来源或期限无效"
        return ""

    def open_position(self, result, i, signal_ts):
        if self.position is not None:
            self.stats["单仓阻止成交"] += 1
            return
        side = result.action
        bid = self.o[i]
        entry = bid + self.spreads[i] if side == "buy" else bid
        details = result.details or {}
        contract = details.get("strategy_contract", {})
        freshness_error = self.research_signal_error(result, i, signal_ts)
        if freshness_error:
            self.stats["研究策略信号过期丢弃"] += 1
            self.rejects[freshness_error] += 1
            self.rejected_fills.append(dict(signal_server_ts=signal_ts, fill_server_ts=self.t[i],
                scheduled_fill_server_ts=signal_ts+self.entry_delay_seconds,
                reason=freshness_error, event_id=details.get('strategy_event_id')))
            return
        event_id = details.get("strategy_event_id")
        if contract.get("one_entry_per_event"):
            event_id = event_id or details.get("strategy_assessments", {}).get(side, {}).get("evidence", {}).get("event_id")
            event_error = self.entry_gate(event_id, self.utc_timestamp(self.t[i]))
            if event_error:
                self.stats["事件或冷却阻止成交"] += 1
                self.rejects[event_error] += 1
                return
            # A rejected executable quote still consumes this window's sole attempt.
            self.consumed_events.add(event_id)
        execution_error = base.research_execution_error(
            result, SimpleNamespace(bid=bid, ask=bid + self.spreads[i]))
        if execution_error:
            self.stats["研究策略成交保护拒绝"] += 1
            self.rejects[execution_error] += 1
            self.rejected_fills.append(dict(signal_server_ts=signal_ts,fill_server_ts=self.t[i],
                scheduled_fill_server_ts=signal_ts+self.entry_delay_seconds,
                reason=execution_error,event_id=event_id))
            return
        # place_order applies this cap to every definition, including legacy
        # contracts that deliberately bypass the demo research helper above.
        live_risk = base.directional_stop_distance(side, entry, float(result.stop_loss))
        if not math.isfinite(live_risk) or live_risk <= 0 or live_risk > base.MAX_SL_ABS + .001:
            reason = "成交价止损距离非法或超出上限"
            self.stats["成交价止损距离非法或超限放弃"] += 1
            self.rejects[reason] += 1
            self.rejected_fills.append(dict(signal_server_ts=signal_ts, fill_server_ts=self.t[i],
                scheduled_fill_server_ts=signal_ts+self.entry_delay_seconds,
                reason=reason, event_id=event_id))
            return
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
            profit_floor=base.ProfitFloor(copy.deepcopy(self.guardian_policy), sl, entry) if self.guardian_policy else None,
        )
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
            "setup_name": contract.get("name"),
            "strategy_id": contract.get("id", details.get("setup", "")),
            "event_id": event_id,
            "max_hold_minutes": int(contract.get("max_hold_minutes", 0)),
            "holding_policy": base.holding_policy(contract),
            "cooldown_seconds": int(contract.get("cooldown_seconds", 0)),
        }
        self.stats["成交"] += 1

    def close(self, ts, price, reason):
        pos = self.position
        state = pos["state"]
        sign = 1 if pos["side"] == "buy" else -1
        pnl = sign * (price - state.entry_price) * self.lot_value
        pnl -= self.guard_fee
        self.trades.append({
            "open_bj": beijing(pos["open_ts"], self.offset).strftime("%Y-%m-%d %H:%M"),
            "close_bj": beijing(ts, self.offset).strftime("%Y-%m-%d %H:%M"),
            "session": base.SESSION_TEXT.get(pos["session"], pos["session"]),
            "setup": pos.get("setup_name") or base.SETUP_TEXT.get(pos["setup"], pos["setup"]),
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
            "event_id": pos.get("event_id") or "",
            "strategy_id": pos.get("strategy_id", ""),
            "signal_bj": beijing(pos["signal_ts"], self.offset).strftime("%Y-%m-%d %H:%M"),
            "signal_server_ts": pos["signal_ts"],
            "fill_server_ts": pos["open_ts"],
            "entry_delay_seconds": self.entry_delay_seconds,
        })
        self.cooldown_until_utc = max(
            self.cooldown_until_utc, self.utc_timestamp(ts) + pos.get("cooldown_seconds", 0))
        self.position = None
        return True

    def time_exit_due(self, i):
        hold = self.position.get("max_hold_minutes", 0)
        return (self.position.get("holding_policy", "hard_exit") == "hard_exit"
                and hold > 0 and self.t[i] >= self.position["open_ts"] + hold * 60)

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
            if self.time_exit_due(i):
                return self.close(t, self.o[i], "策略持仓时限")
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
        if self.time_exit_due(i):
            return self.close(t, self.o[i] + spread, "策略持仓时限")
        if self.h[i] + spread >= stop:
            return self.close(t, stop, reason)
        if pos["tp"] > 0 and self.l[i] + spread <= pos["tp"]:
            return self.close(t, pos["tp"], "止盈")
        return False

    def update_guardian(self, i):
        # Closed M1 proxy, not a reconstructed intrabar tick path.
        pos = self.position
        state = pos["state"]
        _, atr = self.guardian_context(self.t[i] + 60)
        if atr <= 0:
            return
        if state.profit_floor:
            guard = state.profit_floor
            price = self.c[i] + (self.spreads[i] if pos["side"] == "sell" else 0.)
            base.update_guardian_extremes(state, price)
            target = guard.observe(direction=state.direction, entry=state.entry_price, risk=state.risk,
                                   price=price, atr=atr, spread=self.spreads[i], broker_gap=self.point,
                                   cash_per_price=self.lot_value, costs=self.guard_fee, now=self.t[i] + 60,
                                   closed_bar_proxy=True)
            if target:
                target = base.round_protective_stop(pos["side"], target)
                sign = 1 if pos["side"] == "buy" else -1
                permitted = guard.early_reduction_target(
                    direction=state.direction, entry=state.entry_price, price=price, atr=atr,
                    spread=self.spreads[i], broker_gap=self.point,
                    cash_per_price=self.lot_value, costs=self.guard_fee)
                reduction_ok = (permitted is not None and sign * (target - permitted) <= 1e-8
                                and sign * (target - guard.desired_sl) <= 1e-8)
                if (base.sl_change_is_tighter(state.direction, pos["sl"], target)
                        and sign * (price - target) + 1e-8 >= guard.room(atr, self.spreads[i], self.point)
                        and (sign * (target - state.entry_price) * self.lot_value - self.guard_fee + 1e-8 >= guard.rules["min_net_lock"]
                             or reduction_ok)):
                    pos["sl"] = target
                    guard.acknowledge(state.direction, target)
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
        if self.position["state"].profit_floor:
            return  # This control has no AI3; do not quietly reinstate legacy exits.
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
                if self.t[i] >= signal_ts + self.entry_delay_seconds:
                    self.pending = None
                    self.open_position(result, i, signal_ts)
            if self.position is not None:
                reason = self.position.pop("exit_next", None)
                if reason and not self.time_exit_due(i):
                    bid = self.o[i]
                    price = bid if self.position["side"] == "buy" else bid + self.spreads[i]
                    self.close(self.t[i], price, reason)
                elif not self.check_exit(i):
                    self.update_guardian(i)
            close_ts = self.t[i] + 60
            if close_ts not in m5_closes:
                continue
            # Denominator is independent of whether a position or pending fill
            # suppresses evaluation. Do not pad absent weekends/market days.
            if all(bisect.bisect_right(self.windows[name].close_times, close_ts) >= minimum
                   for name, minimum in (("M15", 80), ("H1", 60), ("M5", 10))):
                self.market_days.add(beijing(close_ts, self.offset).date().isoformat())
            if self.position is not None:
                self.check_structure(close_ts)
            elif self.pending is None:
                result = self.evaluate(close_ts)
                if result is not None:
                    self.pending = (copy.deepcopy(result), close_ts)
        if self.pending is not None:
            self.stats["缓存结束待成交丢弃"] += 1
            self.rejects["缓存结束无对应延迟成交分钟"] += 1
            self.pending = None
        if self.position is not None:
            last = total - 1
            bid = self.c[last]
            price = bid if self.position["side"] == "buy" else bid + self.spreads[last]
            self.close(self.t[last] + 60, price, "回测结束平仓")
        return self.trades


def replay_metrics(trades, market_days, start_balance, split_date="2026-09-15"):
    """Market days have sufficient closed context; dates/counts use Asia/Shanghai.

    Profit days use close date; frequency uses entry date. The fixed temporal
    split reuses the research cache and is not independent out-of-sample data.
    Drawdown follows realized, rounded cash PnL, with no margin model.
    """
    datetime.strptime(split_date, "%Y-%m-%d")
    daily = {day: {"entries": 0, "net": 0.0} for day in sorted(market_days)}
    equity = peak = low = float(start_balance)
    dd = 0.0
    split = {key: {"trades": 0, "net": 0.0} for key in ("early", "later")}
    groups = {key: Counter() for key in ("session", "strategy_id", "side")}
    session_days = defaultdict(set)
    for trade in trades:
        day = trade['open_bj'][:10]
        daily.setdefault(day, {"entries": 0, "net": 0.0})['entries'] += 1
        close_day = trade['close_bj'][:10]
        daily.setdefault(close_day, {"entries": 0, "net": 0.0})['net'] += trade['pnl']
        equity += trade['pnl']; peak = max(peak, equity); low = min(low, equity)
        dd = max(dd, peak-equity)
        bucket = split['early' if day < split_date else 'later']
        bucket['trades'] += 1; bucket['net'] += trade['pnl']
        for key in groups:
            groups[key][trade.get(key, trade.get('setup','') if key == 'strategy_id' else '')] += 1
        session_days[trade.get('session','')].add(day)
    holds = sorted(float(t['hold_min']) for t in trades)
    def percentile(q):
        if not holds: return None
        point = (len(holds)-1)*q
        lower = int(point); upper = min(lower+1,len(holds)-1)
        return holds[lower] + (holds[upper]-holds[lower])*(point-lower)
    net = sum(t['pnl'] for t in trades)
    best = max((v['net'] for v in daily.values()), default=0.0)
    return dict(timezone='Asia/Shanghai', market_day_definition='cache M5 evaluation with sufficient H1/M15/M5 closed context; no missing-market/weekend padding',
                market_days=len(market_days), zero_trade_days=sum(daily[d]['entries']==0 for d in market_days),
                daily=daily, trades=len(trades), net=round(net,2), ending_balance=round(equity,2),
                closed_drawdown=round(dd,2), equity_low=round(low,2),
                best_day_removed_net=round(net-best,2),
                hold_minutes=dict(median=percentile(.5),p90=percentile(.9),max=max(holds) if holds else None),
                longest_trade=max(trades,key=lambda t:t['hold_min']) if trades else None,
                groups={k:dict(v) for k,v in groups.items()},
                active_session_days={k:len(v) for k,v in session_days.items()},
                split_date=split_date,split=split, split_limitation='reused research cache, not independent OOS',
                ai_quality=None,ai2_approvals=None,model='local Python fixed proposal, M1 Bid/Ask and guardian; no margin/forced-liquidation/tick path')


def summarize(trades, stats, rejects, meta, start_balance):
    lines = []
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    equity, peak, max_dd, streak, worst_streak = start_balance, start_balance, 0.0, 0, 0
    low_equity, broke_at = start_balance, ""
    for trade in trades:
        equity += trade["pnl"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        low_equity = min(low_equity, equity)
        if equity <= 0 and not broke_at:
            broke_at = trade["close_bj"]
        streak = streak + 1 if trade["pnl"] <= 0 else 0
        worst_streak = max(worst_streak, streak)
    lines.append("区间(北京时间): %s 到 %s" % (meta["start_bj"], meta["end_bj"]))
    lines.append("服务器时差: UTC%+g，点差: %s" % (meta["server_offset"], meta["spread_note"]))
    lines.append("本地阈值: %s，手数: %s，起始资金: %.2f" % (meta["threshold"], base.LOT_SIZE, start_balance))
    lines.append("止损上限: %.2f 美元，单笔最多亏起始资金的 %.1f%%" % (
        base.MAX_SL_ABS, 100.0 * base.MAX_SL_ABS / start_balance if start_balance > 0 else 0.0))
    lines.append("护盘规则: " + json.dumps(meta.get("guardian_policy", "legacy"), ensure_ascii=False))
    lines.append("护盘分辨率: " + str(meta.get("guardian_resolution", "legacy M1")))
    lines.append("每笔费用假设: " + str(meta.get("guardian_fee_cash_assumption", 0.0)))
    lines.append("")
    lines.append("成交 %d 笔，盈利 %d，亏损 %d，胜率 %.1f%%" % (
        len(trades), len(wins), len(losses), 100.0 * len(wins) / len(trades) if trades else 0.0))
    lines.append("净盈亏 %.2f 美元，期末资金 %.2f，最大回撤 %.2f 美元" % (equity - start_balance, equity, max_dd))
    lines.append("资金最低 %.2f，%s" % (low_equity, ("在 %s 归零" % broke_at) if broke_at else "没有归零"))
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
    parser.add_argument("--balance", type=float, default=100.0, help="起始资金，用于算回撤和是否归零，默认 100")
    parser.add_argument(
        "--max-sl", type=float, default=None,
        help="止损上限(美元)，默认用 .env 的 FIB_PA_MAX_SL_ABS（当前 %.2f）" % base.MAX_SL_ABS,
    )
    parser.add_argument("--spread", type=float, default=None, help="固定点差(points)，默认用每根K线自带的点差")
    parser.add_argument("--server-offset", type=float, default=None, help="MT5 服务器相对 UTC 的小时数，默认自动检测")
    parser.add_argument("--offline", action="store_true", help="不连 MT5，直接用上次保存的 CSV")
    parser.add_argument("--guardian-policy", choices=("profit_floor_v1", "profit_floor_v2", "legacy"), default=None,
                        help="冻结护盘对照；profit为M1收盘代理，无AI3/秒级路径")
    parser.add_argument("--strategies", default=None, help="离线启用ID列表，不修改定义或.env")
    parser.add_argument("--entry-delay-seconds", type=int, default=0, help="冻结方案延迟，非负整分钟秒数")
    parser.add_argument("--spread-multiplier", type=float, default=1.0, help="统一成交/护盘/出场点差倍数")
    parser.add_argument("--split-date", default="2026-09-15", help="重复研究缓存分段日期(北京时间)")
    parser.add_argument("--output-label", default=None, help="安全场景标识，仅字母数字下划线短横线")
    args = parser.parse_args()
    if args.strategies is not None and not args.offline:
        parser.error("--strategies requires --offline; broker history startup is forbidden for overrides")
    if args.entry_delay_seconds < 0 or args.entry_delay_seconds % 60:
        parser.error("--entry-delay-seconds must be nonnegative whole-minute seconds")
    if not math.isfinite(args.spread_multiplier) or args.spread_multiplier <= 0:
        parser.error("--spread-multiplier must be finite and positive")
    if args.output_label is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.output_label):
        parser.error("invalid --output-label")
    try:
        datetime.strptime(args.split_date, "%Y-%m-%d")
    except ValueError:
        parser.error("--split-date must be YYYY-MM-DD")
    if args.max_sl is not None:
        if not math.isfinite(args.max_sl) or args.max_sl <= 0:
            print("--max-sl 必须大于 0")
            return 1
        # Strategy helpers read the module global at call time, so this covers every stop check.
        base.MAX_SL_ABS = float(args.max_sl)
    if not math.isfinite(args.balance) or args.balance <= 0:
        print("--balance 必须大于 0")
        return 1

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

    test = Backtest(data, meta, args.threshold, args.spread, strategies=args.strategies,
                    entry_delay_seconds=args.entry_delay_seconds, spread_multiplier=args.spread_multiplier,
                    guardian_policy=args.guardian_policy)
    meta['guardian_policy'] = copy.deepcopy(test.guardian_policy) or 'legacy'
    meta['guardian_fee_cash_assumption'] = test.guard_fee
    meta['guardian_resolution'] = 'closed M1 proxy, next-bar stop; no tick confirmation or AI3' if test.guardian_policy else 'legacy M1'
    meta['selected_strategies'] = [s.id for s in test.strategy_snapshot.strategies if s.enabled]
    meta['offline_strategy_override'] = args.strategies
    meta['entry_delay_seconds'] = args.entry_delay_seconds
    meta['spread_multiplier'] = args.spread_multiplier
    meta['max_sl'] = base.MAX_SL_ABS
    meta['balance'] = args.balance
    meta['split_date'] = args.split_date
    print("策略框架 %s，定义指纹 %s" % (base.STRATEGY_FRAMEWORK_VERSION, test.strategy_snapshot.fingerprint))
    meta["strategy_fingerprint"] = test.strategy_snapshot.fingerprint
    meta["strategy_framework"] = base.STRATEGY_FRAMEWORK_VERSION
    trades = test.run()
    report = summarize(trades, test.stats, test.rejects, meta, args.balance)
    report += "\n\n策略框架: %s\n定义指纹: %s" % (meta["strategy_framework"], meta["strategy_fingerprint"])
    metrics = replay_metrics(trades, test.market_days, args.balance, args.split_date)
    metrics.update(metadata=meta, funnel=dict(test.stats), rejection_counts=dict(test.rejects),
                   candidate_blocker_counts=dict(test.candidate_rejects), rejected_fills=test.rejected_fills)
    report += "\n\n离线启用策略: " + ','.join(meta['selected_strategies'])
    report += "\n冻结方案延迟: %ds，统一点差倍数: %g" % (args.entry_delay_seconds, args.spread_multiplier)
    report += "\n市场日 %d，零成交日 %d，去最佳日净盈亏 %.2f" % (metrics['market_days'], metrics['zero_trade_days'], metrics['best_day_removed_net'])
    report += "\n持仓分钟(中位/p90/最大): " + str(metrics['hold_minutes'])
    report += "\n分段(重复研究缓存，非独立OOS): " + json.dumps(metrics['split'],ensure_ascii=False)
    print()
    print(report)

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = (args.output_label + '_' if args.output_label else '') + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + '_' + uuid.uuid4().hex[:8]
    metrics_path = os.path.join(OUT_DIR, "summary_%s.json" % stamp)
    with open(metrics_path, 'x', encoding='utf-8') as handle:
        json.dump(metrics, handle, ensure_ascii=False, indent=2, allow_nan=False)
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
    print("指标JSON: %s" % metrics_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
