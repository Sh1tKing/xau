"""Trusted local strategy definition. Edit this file to change this strategy."""
from crypto_demo.strategies.framework import Strategy, Assessment, momentum_bonus

P = dict(fib_core=25, volume_core=15, pa_core=18, confluence_bonus=5,
         weak_penalty=12, adx_floor=14, adx_penalty=8)

def evaluate(c):
    action, df_m15, fib, atr = c.action, c.df_m15, c.fib, c.atr
    h1_trend, m15_trend, adx = c.h1_trend, c.m15_trend, c.adx
    fib_score_for_action = c.api.fib_score_for_action
    classify_volume_state = c.api.classify_volume_state
    classify_price_action = c.api.classify_price_action
    trend_score_for_action = c.api.trend_score_for_action
    setup_details = c.api.setup_details
    FibLevels = c.api.FibLevels
    price = float(df_m15["close"].iloc[-1])
    fib_score, fib_text = fib_score_for_action(action, fib, price, atr, df_m15)
    volume = classify_volume_state(df_m15)
    pa_score, pa_text = classify_price_action(df_m15, action, atr)
    trend_score, trend_text = trend_score_for_action(action, h1_trend, m15_trend)
    if adx >= 25:
        trend_text = f"{trend_text}；ADX={adx:.1f}趋势清楚"
    elif adx > 0:
        trend_text = f"{trend_text}；ADX={adx:.1f}"

    core_count = int(fib_score >= P["fib_core"]) + int(volume["score"] >= P["volume_core"]) + int(pa_score >= P["pa_core"])
    score = fib_score + int(volume["score"]) + pa_score + trend_score
    if core_count == 3:
        score += P["confluence_bonus"]
    elif core_count <= 1:
        score -= P["weak_penalty"]
    if adx < P["adx_floor"] and core_count < 3:
        score -= P["adx_penalty"]

    score = max(0, min(100, score))
    return Assessment(score, {
        "fib": fib_text,
        "volume": str(volume["summary"]),
        "price_action": pa_text,
        "trend": trend_text,
        "core_count": str(core_count),
        "pa_score": str(pa_score),
        "fib_score": str(fib_score),
    }, fib, {"fib_score": fib_score, "volume_score": int(volume["score"]), "pa_score": pa_score, "adx": float(adx)})

STRATEGY = Strategy(
    enabled=False,  # Retained for compatibility; report candidate is the active demo strategy.
    id="pullback", name="趋势回撤", version="1",
    sessions={"asia": 0, "late_us": 0, "london": 1, "overlap": 1},
    parameters=P, evaluate=evaluate, regime_filter="pullback",
    momentum=lambda side, rsi, hist, prev: momentum_bonus(side, rsi, hist, prev, rsi_band=(35,55), rsi_bonus=5),
    ai_rules={
        "entry": "H1同向趋势回撤；用输入Fib容差/探入核实结构。tight_range必须观望；wide_range仅区间边缘且有拒绝K。旧波段被收盘破坏须有新结构和确认，影线收回不算收盘破坏。M15反向可以是回撤。普通量及浅回撤仅轻微意见。",
        "review": "核对H1许可、Fib回撤结构和已收盘触发；只审核明确硬伤，不把M15暂时反向当作H1失效。窄区间或宽区间中轴拒绝。",
        "guardian": "按入场保存的回撤结构及失效条件护盘；核对指定周期已收盘证据，数据不足标未知，不能靠单根反向K推断结构失效。"})
