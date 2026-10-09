"""Trusted local strategy definition. Edit this file to change this strategy."""
from strategies.framework import Strategy, Assessment, momentum_bonus
from strategies.evidence import _rounded_number, _bar_payload, _extreme_bar, bars_for_prompt, bar_times_for_prompt

P = dict(lookback=24, min_width_atr=2.0, max_width_atr=7.0, edge_fraction=.3,
         inner_edge=.18, inner_score=30, outer_score=22, rejection_score=12, unconfirmed_cap=50)

def describe(df_m15, atr, action, parameters=None):
    lookback = (P if parameters is None else parameters).get("lookback", 24)
    if df_m15 is None or len(df_m15) < lookback:
        return {"available": False, "reason": "insufficient_range_bars"}
    window = df_m15.tail(lookback)
    low = float(window["low"].min())
    high = float(window["high"].max())
    width = max(high - low, 1e-9)
    price = float(df_m15["close"].iloc[-1])
    location = (price - low) / width
    edge_location = location if action == "buy" else 1.0 - location
    return {
        "available": True,
        "direction": action,
        "low": _rounded_number(low),
        "high": _rounded_number(high),
        "width_atr": _rounded_number(width / float(atr)) if atr else None,
        "location": _rounded_number(location),
        "edge_location": _rounded_number(edge_location),
        "window_bar_times": bar_times_for_prompt(window, len(window)),
        "low_bar": _extreme_bar(window, "low", "min", "range_low"),
        "high_bar": _extreme_bar(window, "high", "max", "range_high"),
    }

def evaluate(c):
    action, df_m15, atr = c.action, c.df_m15, c.atr
    h1_trend, m15_trend = c.h1_trend, c.m15_trend
    fib_score_for_action = c.api.fib_score_for_action
    classify_volume_state = c.api.classify_volume_state
    classify_price_action = c.api.classify_price_action
    trend_score_for_action = c.api.trend_score_for_action
    setup_details = c.api.setup_details
    FibLevels = c.api.FibLevels
    window = df_m15.tail(P["lookback"])
    low = float(window["low"].min())
    high = float(window["high"].max())
    box = max(high - low, 1e-9)
    box_atr = box / atr
    price = float(df_m15["close"].iloc[-1])
    location = (price - low) / box
    structure = FibLevels("bull" if action == "buy" else "bear", low, high, {})
    volume = classify_volume_state(df_m15)
    pa_score, pa_text = classify_price_action(df_m15, action, atr)
    trend_score, trend_text = trend_score_for_action(action, h1_trend, m15_trend)
    edge_loc = location if action == "buy" else 1.0 - location
    box_text = "%s根M15区间 %.2f-%.2f (%.2fATR), location=%.2f" % (P["lookback"], low, high, box_atr, location)
    facts = describe(df_m15, atr, action)
    if box_atr < P["min_width_atr"]:
        return Assessment(0, setup_details(box_text + "，区间太窄", volume, pa_score, pa_text, trend_text, 0), structure, facts)
    if box_atr > P["max_width_atr"]:
        return Assessment(0, setup_details(box_text + "，区间过宽，不按震荡做", volume, pa_score, pa_text, trend_text, 0), structure, facts)
    if edge_loc > P["edge_fraction"]:
        return Assessment(0, setup_details(box_text + "，不在区间边缘", volume, pa_score, pa_text, trend_text, 0), structure, facts)
    edge_score = P["inner_score"] if edge_loc <= P["inner_edge"] else P["outer_score"]
    score = edge_score + pa_score + int(volume["score"]) + trend_score
    if pa_score < P["rejection_score"]:
        score = min(score, P["unconfirmed_cap"])
        pa_text += "，边缘缺少拒绝K线"
    return Assessment(max(0, min(100, score)), setup_details(box_text, volume, pa_score, pa_text, trend_text, edge_score), structure, facts)

STRATEGY = Strategy(
    enabled=False,  # Retained for compatibility; report candidate is the active demo strategy.
    id="range_edge", name="区间边缘", version="1", sessions={"asia": 1, "late_us": 1},
    parameters=P, evaluate=evaluate, describe=describe,
    momentum=lambda side, rsi, hist, prev: momentum_bonus(side, rsi, hist, prev, rsi_band=(-float("inf"),40), rsi_bonus=10),
    ai_rules={
        "entry": f"H1同向；{P['lookback']}根M15区间宽{P['min_width_atr']}到{P['max_width_atr']}ATR，边缘{P['edge_fraction']:.0%}内需拒绝K；缺少拒绝本地封顶{P['unconfirmed_cap']}。中轴不追，目标看另一侧及中间障碍。",
        "review": f"核对strategy_evidence的区间端点、宽度和边缘位置；范围由parameters声明；拒绝分至少{P['rejection_score']}，普通量不单独否决。",
        "guardian": "按入场保存的区间边缘和失效证据护盘；不要改用当前新策略解释旧仓，止损只能收紧。"})
