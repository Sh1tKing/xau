"""Report candidate: first London/New York opening-range breakout retest, demo only."""
from datetime import timezone
from strategies.framework import Strategy, Assessment
from strategies.session_retest import session_retest_facts

P = dict(open_minute=480, range_minutes=30, entry_end_minute=660,
         min_width_atr=1., max_width_atr=6., break_atr=.05, retest_atr=.25,
         hold_atr=.05, invalidation_atr=.30, max_retest_bars=6, max_retest_seconds=1800,
         target_r=3., max_hold_minutes=180, cooldown_seconds=600)


def evaluate(c):
    clock = c.now.astimezone(timezone.utc) if c.now is not None and c.now.tzinfo is not None else None
    facts = session_retest_facts(c.df_m5, clock, c.server_utc_offset, c.action, P)
    blockers = list(facts.get("blockers", []))
    permitted = c.h1_trend == ("bull" if c.action == "buy" else "bear")
    if not permitted:
        blockers.append("研究策略仍要求H1同向")
    facts["h1_permitted"] = permitted
    confirmed = bool(facts.get("closed_trigger", {}).get("confirmed"))
    score = 85 if confirmed and not blockers else 0
    return Assessment(score, {"fib": "欧美时段建箱与首次突破回踩",
                              "volume": "量能仅供AI参考，不是本策略硬条件",
                              "price_action": "首次M5回踩收盘确认" if confirmed else "等待合法窗口及首次回踩确认",
                              "trend": "H1=%s" % c.h1_trend, "fib_score": "0", "pa_score": "0"},
                      evidence=facts, blockers=tuple(blockers))


STRATEGY = Strategy(
    id="session_retest", name="欧美时段突破回踩", version="1", enabled=False,
    sessions={"asia": 0, "london": 0, "overlap": 0, "late_us": 0},
    parameters=P, evaluate=evaluate, target_r=P["target_r"], m5_boost=False,
    entry_reference="closed_m5", stop_reference="m5_extreme", max_hold_minutes=P["max_hold_minutes"],
    cooldown_seconds=P["cooldown_seconds"], one_entry_per_event=True, demo_only=True,
    ai_rules={
        "entry": f"按strategy_evidence的伦敦/纽约当地日期窗口判断（含夏令时），窗口起止由parameters和证据windows声明。前{P['range_minutes']//5}根连续M5建箱，宽{P['min_width_atr']:g}–{P['max_width_atr']:g}个末根M5 ATR；第一次收盘突破{P['break_atr']:g}ATR，随后{P['max_retest_bars']}根/{P['max_retest_seconds']//60}分钟内第一次回踩{P['retest_atr']:g}ATR并收盘守在外{P['hold_atr']:g}ATR。回收箱内{P['invalidation_atr']:g}ATR失效。只能最新已收盘M5确认，不能追错过的首次事件。保留H1同向、EMA21追价及M5保护硬门；参考价为确认M5收盘，目标上限{P['target_r']:g}R。AI可自主精筛该合法事件，不受旧Fib38–62%或M15箱体规则约束。",
        "review": f"核对原始建箱{P['range_minutes']//5}根、第一次突破的方向与时间、其后的第一次回踩、时间窗口与UTC偏移，不能把突破自身影线当回踩或挑第二次机会。核对Python最终SL/TP和实时Bid/Ask执行区间、SL8/RR1.5/保护余量；模拟账户、一窗口一次和冷却由Python再检查。",
        "guardian": f"使用入场保存的箱体边缘、确认M5及失效条件；须指定周期收盘证据，未知不得臆断。参考目标{P['target_r']:g}R并不要求一直持有，AI仍可合法收紧或平仓，不能放宽止损或延长Python{P['max_hold_minutes']}分钟期限。"})
