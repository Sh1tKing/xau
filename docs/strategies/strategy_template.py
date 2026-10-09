"""Copy into strategies/definitions/, rename the id, implement and test, then enable."""
from strategies.framework import Strategy, Assessment

P = {"ema_period": 20}


def evaluate(c):
    frame = c.df_m15
    period = P["ema_period"]
    if len(frame) < period + 2:
        return Assessment(0, {}, evidence={"closed_trigger": {"confirmed": False}},
                          blockers=("EMA样本不足",))
    line = frame.close.ewm(span=period, adjust=False).mean()
    sign = 1 if c.action == "buy" else -1
    crossed = bool(sign * (frame.close.iloc[-2] - line.iloc[-2]) <= 0
                   and sign * (frame.close.iloc[-1] - line.iloc[-1]) > 0)
    facts = {"ema": float(line.iloc[-1]), "close": float(frame.close.iloc[-1]),
             "closed_trigger": {"confirmed": crossed, "timeframe": "M15",
                                "bar_time": int(frame.time.iloc[-1])}}
    return Assessment(70 if crossed else 0, {"price_action": "M15收盘穿过EMA" if crossed else "未穿过EMA"},
                      evidence=facts, blockers=() if crossed else ("尚无收盘穿越触发",))


STRATEGY = Strategy(
    id="my_setup", name="自定义策略示例", version="1", enabled=False,
    sessions={"asia": 0, "london": 0, "overlap": 0, "late_us": 0},
    evaluate=evaluate, parameters=P, target_r=3.0, m5_boost=False,
    ai_rules={
        "entry": "只在M15已收盘穿过parameters.ema_period的EMA时提出方案；核对strategy_evidence，H1许可和Python风控仍必须通过。",
        "review": "独立核对输入收盘穿越证据及最终实时报价，只拒绝策略硬伤或Python硬限制。",
        "guardian": "按入场保存的EMA触发与AI失效条件护盘；未知历史不得补造，止损只能收紧。"})
