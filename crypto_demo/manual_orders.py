"""Local one-shot order requests. The existing account owner alone talks to MT5.

No credentials, MT5 imports, AI calls or background guardian in this module.
Claimed requests survive crashes and are never automatically submitted again.
"""
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

VERSION = "crypto-manual-entry-v2"
HEARTBEAT_AGE = 30


class ManualOrderError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManualOrderError("duplicate request field: " + key)
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_object)


def write_json(path, value):
    path = Path(path)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".manual-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def valid_id(value):
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ManualOrderError("price/volume/time must be finite and positive")
    return value


def validate_request(value, binding, session, now):
    fields = {"version", "request_id", "session", "account", "asset", "symbol", "magic", "mode",
              "created_at", "expires_at", "side", "sl", "tp", "volume", "reason", "invalidation", "target_basis"}
    if not isinstance(value, dict) or set(value) != fields or value.get("version") != VERSION:
        raise ManualOrderError("invalid order request format")
    if not valid_id(value["request_id"]) or value["session"] != session:
        raise ManualOrderError("request belongs to an earlier controller; submit a new decision")
    if any(type(value[k]) is not type(binding[k]) or value[k] != binding[k] for k in binding):
        raise ManualOrderError("request account/asset/symbol/magic/mode mismatch")
    if value["mode"] != "demo" or value["side"] not in ("buy", "sell"):
        raise ManualOrderError("manual entry requires demo and buy/sell")
    created, expires = number(value["created_at"]), number(value["expires_at"])
    if not created <= now < expires or not 0 < expires-created <= 60:
        raise ManualOrderError("request expired or its clock is invalid")
    for key in ("sl", "tp"):
        number(value[key])
    if value["sl"] == value["tp"]:
        raise ManualOrderError("SL and TP must differ")
    if value["volume"] is not None:
        number(value["volume"])
    for key in ("reason", "invalidation", "target_basis"):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 500:
            raise ManualOrderError("nonempty reason/invalidation/target_basis required (max 500 characters)")
    return value


class ManualQueue:
    def __init__(self, directory, account, asset, symbol, magic, mode="demo"):
        self.directory = Path(directory)
        self.binding = dict(account=account, asset=asset, symbol=symbol, magic=magic, mode=mode)

    def publish(self, session, now=None):
        if not valid_id(session):
            raise ManualOrderError("invalid controller session")
        write_json(self.directory/"controller.json", dict(version=VERSION, session=session,
                   updated_at=time.time() if now is None else now, **self.binding))

    def submit(self, side, sl, tp, volume=None, reason="按指定方向及SL/TP进场，形态依据未提供",
               invalidation="原SL触发；其他结构失效条件未知", target_basis="固定TP，目标结构依据未提供", now=None):
        now = time.time() if now is None else now
        try:
            owner = read_json(self.directory/"controller.json")
            if (owner.get("version") != VERSION or not valid_id(owner.get("session"))
                    or not 0 <= now-number(owner.get("updated_at")) <= HEARTBEAT_AGE
                    or any(owner.get(k) != v for k, v in self.binding.items())):
                raise ManualOrderError("no matching demo controller with a fresh heartbeat")
        except (OSError, ValueError, AttributeError) as exc:
            raise ManualOrderError("启动同目录对应币种的新版 run.py --mode demo 后再下单") from exc
        value = dict(version=VERSION, request_id=uuid.uuid4().hex, session=owner["session"],
                     created_at=now, expires_at=now+60, side=side, sl=sl, tp=tp, volume=volume,
                     reason=reason, invalidation=invalidation, target_basis=target_basis, **self.binding)
        validate_request(value, self.binding, owner["session"], now)
        write_json(self.directory/(value["request_id"]+".request.json"), value)
        return value

    def pending(self):
        return sorted(path for path in self.directory.glob("*.request.json")
                      if valid_id(path.name.removesuffix(".request.json")))

    def claim(self):
        for path in self.pending():
            identifier = path.name.removesuffix(".request.json")
            if not valid_id(identifier):
                continue
            claimed = self.directory/(identifier+".claimed.json")
            if claimed.exists() or self.result(identifier) is not None:
                # An earlier claim may already have reached the broker.
                path.unlink(missing_ok=True)
                continue
            try:
                os.rename(path, claimed)
            except FileNotFoundError:
                continue
            return identifier, claimed
        return None

    def reject_invalid(self, session, now):
        """Expire waiting commands even while an earlier AI request is in flight."""
        for path in self.pending():
            identifier = path.name.removesuffix(".request.json")
            try:
                value = read_json(path)
                validate_request(value, self.binding, session, now)
                if value["request_id"] != identifier:
                    raise ManualOrderError("请求文件与订单编号不一致")
            except ValueError as exc:
                claimed = self.directory/(identifier+".claimed.json")
                if claimed.exists() or self.result(identifier) is not None:
                    path.unlink(missing_ok=True)
                    continue
                try:
                    os.rename(path, claimed)
                except FileNotFoundError:
                    continue
                self.finish(identifier, "rejected", str(exc))

    def finish(self, identifier, status, message, ticket=0):
        if not valid_id(identifier):
            raise ManualOrderError("invalid request identifier")
        value = dict(version=VERSION, request_id=identifier, status=status, message=message,
                     ticket=ticket, completed_at=time.time())
        write_json(self.directory/(identifier+".result.json"), value)
        return value

    def result(self, identifier):
        if not valid_id(identifier):
            raise ManualOrderError("invalid request identifier")
        try:
            return read_json(self.directory/(identifier+".result.json"))
        except FileNotFoundError:
            return None


def manual_contract(asset, profile):
    """Human intent is a separate source; never impersonate an audited AI proposal."""
    return dict(id="manual_"+asset.lower(), version="2", demo_only=True, manual_entry=True,
                analysis_mode=False, one_entry_per_event=True, stop_reference="human_fixed",
                max_hold_minutes=profile["max_hold_minutes"], cooldown_seconds=profile["cooldown_seconds"],
                ai_rules={"guardian": (
                    "原入场是用户决策，不是AI1/AI2已审核结论。理由、失效条件及目标依据保留用户原文，未知则明确未知。"
                    "只依据对应周期已收盘K线判断原理由失效；仍可按公共M1/M5恶化规则护盘。"
                    "只持有、收紧或平仓，不放宽SL、不补仓、不开新仓；Python执行原冻结持仓期限。"
                    "返回reasoning只描述行情及护盘依据，不标记入场渠道。")})


def prepare_result(base, trader, request):
    """Build factual, frozen context from the controller's current closed sources."""
    from crypto_demo.strategies.adaptive import _rows
    now = time.time()
    validate_request(request, trader.manual_queue.binding, trader._manual_session, now)
    trader.crypto_account_ready(empty=True)
    frames = trader.get_context()
    tick = base.mt5.symbol_info_tick(base.SYMBOL)
    bid, ask = base.crypto_quote_prices(tick)
    offset = base.tick_clock_offset(tick, now)
    if offset is None:
        raise ManualOrderError("报价或经纪商时钟无效")
    m15, h1, m5 = frames
    for frame, name in zip(frames, ("M15", "H1", "M5")):
        _rows(frame, name, now, offset)
    side, sl, tp = request["side"], request["sl"], request["tp"]
    atr = base.calculate_atr(m15)
    ema = base.ema_last(m15["close"], 21)
    entry = ask if side == "buy" else bid
    if not trader.validate_order_prices(side, entry, sl, tp):
        raise ManualOrderError("SL/TP方向非法")
    extreme = base.recent_m5_extreme(m5, side)
    snapshot = base.STRATEGIES.snapshot()
    strategy = snapshot.get(base.ASSET.lower()+"_adaptive")
    if snapshot.error or strategy is None or not strategy.enabled:
        raise ManualOrderError("当前策略规则不可用，不能冻结护盘期限")
    profile = strategy.manifest()
    contract = manual_contract(base.ASSET, profile)
    event_id = "manual/"+request["request_id"]
    evidence = dict(event_id=event_id, server_offset_hours=offset,
                    manual_request=copy.deepcopy(request),
                    m15_bar_time=int(m15.time.iloc[-1]), m5_bar_time=int(m5.time.iloc[-1]),
                    h1_bar_time=int(h1.time.iloc[-1]), h1_trend=base.trend_from_ema(h1, 50),
                    m5_protection_extreme=extreme,
                    source="human_command_with_current_closed_market_data")
    return base.StrategyResult(side, 0, False, request["reason"], sl, tp, 0, 0, dict(
        source="manual", manual_request=copy.deepcopy(request), manual_volume=request["volume"],
        strategy_contract=contract, strategy_event_id=event_id, event_id=event_id,
        strategy_evidence={side: {"evidence": evidence}},
        entry_price=entry, reference_entry_price=entry, entry_guard_atr=atr, entry_guard_ema21=ema,
        setup=contract["id"], trigger_evidence=request["reason"], invalidation=request["invalidation"],
        target_basis=request["target_basis"],
        unknowns=["用户原文未经过AI-1/AI-2审核"],
        entry_price_source="controller_live_ask" if side == "buy" else "controller_live_bid"))


def entry_error(base, trader, result):
    try:
        request = (result.details or {}).get("manual_request")
        if not request or request != getattr(trader, "_active_manual_request", None):
            raise ManualOrderError("下单请求没有正在处理的原始用户指令")
        current = prepare_result(base, trader, request)
        # Re-read sources and permission without editing the user's fixed plan.
        for key in ("source", "strategy_contract", "strategy_evidence", "strategy_event_id",
                    "manual_volume", "entry_guard_atr", "entry_guard_ema21"):
            if result.details.get(key) != current.details[key]:
                raise ManualOrderError("原始闭合来源或冻结持仓规则已改变，停止本次下单")
        if result.action != request["side"] or result.stop_loss != request["sl"] or result.take_profit != request["tp"]:
            raise ManualOrderError("订单与原始用户指令不一致")
        return trader.strategy_ledger.entry_error(current.details["event_id"], time.time())
    except (ValueError, RuntimeError, KeyError, TypeError, AttributeError, IndexError, OSError) as exc:
        return str(exc)
