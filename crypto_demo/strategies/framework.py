"""Discover trusted local strategy modules and publish atomic, versioned snapshots.

This is a Python plugin interface, not a sandbox or a natural-language compiler.
Only the host can supply market data, generate protected prices, or send orders.
"""
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from pathlib import Path
import re
import threading
from types import ModuleType
import copy
import os
from typing import Any, Callable

VERSION = "strategy-framework-v2"
SESSIONS = ("asia", "london", "overlap", "late_us")


class StrategyError(ValueError):
    pass


@dataclass(frozen=True)
class Context:
    action: str
    df_m15: Any
    df_h1: Any
    df_m5: Any
    fib: Any
    atr: float
    h1_trend: str
    m15_trend: str
    adx: float
    session: str
    api: Any  # Explicit host math helpers, no trader/MT5 object.
    now: Any = None  # Aware decision clock; plugins must not read the wall clock themselves.
    server_utc_offset: float | None = None


@dataclass(frozen=True)
class Assessment:
    score: int
    details: dict
    structure: Any = None
    evidence: dict = field(default_factory=dict)
    blockers: tuple = ()


@dataclass(frozen=True)
class Strategy:
    id: str
    name: str
    version: str
    sessions: dict  # Session -> deterministic tie priority; lower comes first.
    evaluate: Callable
    parameters: dict = field(default_factory=dict)
    ai_rules: dict = field(default_factory=dict)
    target_r: float = 1.8
    enabled: bool = True
    m5_boost: bool = True
    momentum: Callable | None = None
    regime_filter: str = "none"
    describe: Callable | None = None  # Optional detailed evidence compatibility API.
    entry_reference: str = "closed_m15"
    stop_reference: str = "structure"
    max_hold_minutes: int = 0
    cooldown_seconds: int = 0
    one_entry_per_event: bool = False
    demo_only: bool = False
    analysis_mode: bool = False  # Broad AI analysis checkpoint, not a confirmed entry pattern.

    def manifest(self):
        return copy.deepcopy({"id": self.id, "name": self.name, "version": self.version,
                "sessions": dict(self.sessions), "parameters": dict(self.parameters),
                "ai_rules": dict(self.ai_rules), "target_max_reference_r": self.target_r,
                "regime_filter": self.regime_filter, "entry_reference": self.entry_reference,
                "stop_reference": self.stop_reference, "max_hold_minutes": self.max_hold_minutes,
                "cooldown_seconds": self.cooldown_seconds, "one_entry_per_event": self.one_entry_per_event,
                "demo_only": self.demo_only, "analysis_mode": self.analysis_mode})

    def assess(self, context):
        contract = self.manifest()
        try:
            result = self.evaluate(context)
        except Exception as exc:
            raise StrategyError(f"{self.id} 评分失败: {type(exc).__name__}: {exc}") from exc
        if self.manifest() != contract:
            raise StrategyError(f"{self.id} 评分函数不得修改策略声明或参数")
        if not isinstance(result, Assessment) or type(result.score) is not int or not 0 <= result.score <= 100:
            raise StrategyError(f"{self.id} 必须返回Assessment，score必须为0–100整数")
        if not isinstance(result.details, dict) or not isinstance(result.evidence, dict):
            raise StrategyError(f"{self.id} details/evidence必须为对象")
        if not isinstance(result.blockers, tuple) or any(not isinstance(x, str) or not x.strip() for x in result.blockers):
            raise StrategyError(f"{self.id} blockers必须为字符串tuple")
        try:
            json.dumps([result.details, result.evidence], allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise StrategyError(f"{self.id} 返回值不能包含NaN或不可序列化数据") from exc
        trigger = result.evidence.get("closed_trigger")
        if "closed_trigger" in result.evidence:
            if not isinstance(trigger, dict) or type(trigger.get("confirmed")) is not bool:
                raise StrategyError(f"{self.id} closed_trigger需声明confirmed布尔值")
            if trigger["confirmed"]:
                frame = {"M15": context.df_m15, "M5": context.df_m5}.get(trigger.get("timeframe"))
                stamp = trigger.get("bar_time")
                if type(stamp) is not int or frame is None or "time" not in frame or stamp not in set(frame["time"]):
                    raise StrategyError(f"{self.id} 触发必须引用输入中的已收盘M15/M5时间")
        return replace(result, details=copy.deepcopy(result.details), evidence=copy.deepcopy(result.evidence))


@dataclass(frozen=True)
class Snapshot:
    fingerprint: str
    strategies: tuple
    error: str = ""

    def active(self, session):
        return tuple(sorted((s for s in self.strategies if s.enabled and session in s.sessions),
                            key=lambda s: (s.sessions[session], s.id)))

    def get(self, strategy_id):
        if self.error:
            raise StrategyError(self.error)
        for s in self.strategies:
            if s.id == strategy_id:
                return s
        raise StrategyError(f"未找到策略 {strategy_id}")


class Registry:
    def __init__(self, directory):
        self.directory = Path(directory)
        self._lock = threading.RLock()
        self._snapshot = None

    def _sources(self):
        if not self.directory.is_dir():
            raise StrategyError(f"策略目录缺失: {self.directory}")
        result = []
        for path in sorted(self.directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            raw = path.read_bytes()
            if len(raw) > 256 * 1024:
                raise StrategyError(f"策略文件过大: {path.name}")
            result.append((path.name, path, raw))
        if not result:
            raise StrategyError("策略目录没有可识别的.py文件")
        return result

    @staticmethod
    def digest(sources):
        h = hashlib.sha256()
        for name, _, raw in sources:
            h.update(name.encode("utf-8") + b"\0" + hashlib.sha256(raw).digest())
        return h.hexdigest()

    @staticmethod
    def validate(s):
        if not isinstance(s, Strategy):
            raise StrategyError("模块必须声明STRATEGY=Strategy(...)")
        if not isinstance(s.id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", s.id):
            raise StrategyError("策略id必须为小写字母/数字/下划线")
        if not s.name or not s.version or not isinstance(s.name, str) or not isinstance(s.version, str):
            raise StrategyError(f"{s.id} 缺少名称或版本")
        if type(s.enabled) is not bool or type(s.m5_boost) is not bool or not callable(s.evaluate):
            raise StrategyError(f"{s.id} enabled/m5_boost/evaluate类型错误")
        if not isinstance(s.sessions, dict) or not s.sessions or any(
                k not in SESSIONS or type(v) is not int or not 0 <= v <= 100 for k, v in s.sessions.items()):
            raise StrategyError(f"{s.id} sessions需指定合法时段及0–100优先级")
        if type(s.target_r) not in (int, float) or not math.isfinite(s.target_r) or not 1.5 <= s.target_r <= 6:
            raise StrategyError(f"{s.id} target_r必须在1.5–6之间")
        if s.entry_reference not in ("closed_m15", "closed_m5") or s.stop_reference not in ("structure", "m5_extreme", "region"):
            raise StrategyError(f"{s.id} 入场/止损来源非法")
        if (type(s.max_hold_minutes) is not int or not 0 <= s.max_hold_minutes <= 2880
                or type(s.cooldown_seconds) is not int or not 0 <= s.cooldown_seconds <= 86400
                or type(s.one_entry_per_event) is not bool or type(s.demo_only) is not bool
                or type(s.analysis_mode) is not bool):
            raise StrategyError(f"{s.id} 生命周期声明非法")
        if s.analysis_mode and (not s.demo_only or not s.one_entry_per_event):
            raise StrategyError(f"{s.id} 分析模式必须仅模拟账户且单检查点一次订单")
        if (s.regime_filter not in ("none", "pullback") or (s.momentum is not None and not callable(s.momentum))
                or s.describe is not None and not callable(s.describe)):
            raise StrategyError(f"{s.id} regime_filter/momentum错误")
        if not isinstance(s.parameters, dict) or not isinstance(s.ai_rules, dict) or set(s.ai_rules) != {"entry", "review", "guardian"}:
            raise StrategyError(f"{s.id} parameters及三角色ai_rules必须完整")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in s.ai_rules.values()):
            raise StrategyError(f"{s.id} AI规则需为非空短文本")
        json.dumps(s.manifest(), allow_nan=False)

    def snapshot(self):
        with self._lock:
            fingerprint = "unavailable"
            try:
                sources = self._sources()
                fingerprint = self.digest(sources)
                if self._snapshot is not None and self._snapshot.fingerprint == fingerprint:
                    return self._snapshot
                strategies = []
                for name, path, raw in sources:
                    module = ModuleType(f"_xau_strategy_{path.stem}_{fingerprint[:12]}")
                    module.__file__ = str(path)
                    # Compile the bytes we hashed; never reuse stale timestamp-based .pyc.
                    exec(compile(raw, str(path), "exec"), module.__dict__)
                    s = module.__dict__.get("STRATEGY")
                    self.validate(s)
                    if any(x.id == s.id for x in strategies):
                        raise StrategyError(f"重复策略id: {s.id}")
                    strategies.append(s)
                if self.digest(self._sources()) != fingerprint:
                    raise StrategyError("读取期间策略文件改变，本轮不开仓")
                self._snapshot = Snapshot(fingerprint, tuple(strategies))
            except Exception as exc:
                # A broken edit blocks NEW entries. Never quietly trade the last good definition.
                self._snapshot = Snapshot(fingerprint, (), f"策略加载失败: {type(exc).__name__}: {exc}")
            return self._snapshot

    def require_current(self, fingerprint):
        snapshot = self.snapshot()
        if snapshot.error:
            raise StrategyError(snapshot.error)
        if fingerprint != snapshot.fingerprint:
            raise StrategyError("策略版本已改变，旧提案停止审核/执行，等待重新分析")


DEFAULT_REGISTRY = Registry(Path(__file__).resolve().parent / "definitions" / os.environ.get("CRYPTO_ASSET", "BTC").lower())


def momentum_bonus(action, rsi, hist, prev_hist, *, rsi_band=None, rsi_bonus=0, missing_macd=0):
    score, notes = 0, []
    sign = 1 if action == "buy" else -1
    directed = rsi if action == "buy" else 100 - rsi
    if directed >= 75:
        score -= 12
        notes.append("RSI已到极端区，不追")
    elif rsi_band is not None and rsi_band[0] <= directed <= rsi_band[1]:
        score += rsi_bonus
        notes.append("RSI回撤降温" if rsi_bonus == 5 else "RSI在区间边缘偏离")
    if sign * hist > 0 or sign * (hist - prev_hist) > 0:
        score += 5
        notes.append("MACD动能同向")
    elif missing_macd:
        score -= missing_macd
        notes.append("MACD没跟上突破")
    text = "RSI=%.1f, MACD柱=%.2f" % (rsi, hist)
    if notes:
        text += "，" + "，".join(notes)
    return score, text
