"""Trusted local strategy definition. Edit this file to change this strategy."""
from strategies.framework import Strategy, Assessment, momentum_bonus
from strategies.evidence import _rounded_number, _bar_payload, _extreme_bar, bars_for_prompt, bar_times_for_prompt

P = dict(box_bars=16, recent_bars=4, warmup_extra=26, volume_bars=20,
         max_box_atr=3.5, break_atr=.1, retest_atr=.35, strong_volume=1.3,
         normal_volume=1.1, strong_volume_score=25, normal_volume_score=15,
         weak_volume_score=5, retest_score=30, hold_score=12, no_retest_cap=45)

def retest_facts(recent, broke, edge, atr, action, retest_atr=.35):
    """Shared closed-bar retest facts for local scoring and AI evidence."""
    sign = 1 if action == "buy" else -1
    after = recent.loc[broke.index[0] + 1:] if not broke.empty else recent.iloc[:0]
    field = "low" if action == "buy" else "high"
    touches = after[sign * (after[field] - edge) <= retest_atr * atr]
    probe = None if after.empty else float(after[field].min() if action == "buy" else after[field].max())
    holds = bool(not broke.empty and sign * (float(recent.iloc[-1]["close"]) - edge) > 0)
    return {"after": after, "touches": touches, "probe": probe,
            "retested": bool(not touches.empty), "close_holds_edge": holds,
            "retest_holds": bool(not touches.empty and holds)}

def describe(df_m15, atr, action, parameters=None):
    """Expose the same breakout window used by base.score_breakout without rescoring it."""
    p = P if parameters is None else parameters
    box_bars, recent_bars = p.get("box_bars", 16), p.get("recent_bars", 4)
    if df_m15 is None or len(df_m15) < box_bars + p.get("warmup_extra", 26):
        return {"available": False, "reason": "insufficient_breakout_bars"}
    work = df_m15.reset_index(drop=True)
    box = work.iloc[-(box_bars + recent_bars):-recent_bars]
    recent = work.iloc[-recent_bars:]
    low = float(box["low"].min())
    high = float(box["high"].max())
    edge = high if action == "buy" else low
    sign = 1 if action == "buy" else -1
    pushes = recent.iloc[:-1]
    broke = pushes[sign * (pushes["close"] - edge) > p.get("break_atr", .1) * float(atr)]
    volume_base = float(work["tick_volume"].iloc[-(p.get("volume_bars", 20) + recent_bars):-recent_bars].mean())
    result = {
        "available": True,
        "direction": action,
        "box_low": _rounded_number(low),
        "box_high": _rounded_number(high),
        "box_atr": _rounded_number((high - low) / float(atr)) if atr else None,
        "box_edge": _rounded_number(edge),
        "box_bar_times": bar_times_for_prompt(box, len(box)),
        "breakout_bar": None,
        "retest_bars": [],
        "retest_probe": None,
        "retest_distance_to_edge": None,
        "retest_holds": False,
        "retested": False,
        "close_holds_edge": False,
        "post_breakout_bars": [],
        "breakout_volume_baseline": _rounded_number(volume_base),
        "breakout_volume_ratio": None,
        "breakout_closed": bool(not broke.empty),
        "same_event_group": "breakout_sequence",
    }
    if broke.empty:
        return result
    first_index = int(broke.index[0])
    breakout_row = recent.loc[first_index]
    result["breakout_bar"] = _bar_payload(breakout_row, "breakout_close")
    if volume_base > 0:
        result["breakout_volume_ratio"] = _rounded_number(float(broke["tick_volume"].max()) / volume_base)
    facts = retest_facts(recent, broke, edge, atr, action, p.get("retest_atr", .35))
    result["same_event_group"] = "breakout_%s" % result["breakout_bar"]["time"]
    result["post_breakout_bars"] = bars_for_prompt(facts["after"], len(facts["after"]))
    result["retest_bars"] = [_bar_payload(row, "edge_retest") for row in facts["touches"].itertuples(index=False)]
    result.update({key: facts[key] for key in ("retested", "close_holds_edge", "retest_holds")})
    if facts["probe"] is not None:
        probe = facts["probe"]
        result["retest_probe"] = _rounded_number(probe)
        result["retest_distance_to_edge"] = _rounded_number(abs(probe - edge))
    return result

def evaluate(c):
    action, df_m15, atr = c.action, c.df_m15, c.atr
    h1_trend, m15_trend = c.h1_trend, c.m15_trend
    fib_score_for_action = c.api.fib_score_for_action
    classify_volume_state = c.api.classify_volume_state
    classify_price_action = c.api.classify_price_action
    trend_score_for_action = c.api.trend_score_for_action
    setup_details = c.api.setup_details
    FibLevels = c.api.FibLevels
    work = df_m15.reset_index(drop=True)
    direction = "bull" if action == "buy" else "bear"
    empty_volume = {"score": 0, "summary": "突破样本不足"}
    facts = describe(df_m15, atr, action)
    if len(work) < P["box_bars"] + P["warmup_extra"]:
        return Assessment(0, setup_details("突破样本不足", empty_volume, 0, "", "", 0), None, facts)
    box = work.iloc[-(P["box_bars"] + P["recent_bars"]):-P["recent_bars"]]
    recent = work.iloc[-P["recent_bars"]:]
    low = float(box["low"].min())
    high = float(box["high"].max())
    box_atr = (high - low) / atr
    structure = FibLevels(direction, low, high, {})
    pa_score, pa_text = classify_price_action(df_m15, action, atr)
    trend_score, trend_text = trend_score_for_action(action, h1_trend, m15_trend)
    box_text = "突破前%s根箱体 %.2f-%.2f (%.2fATR)" % (P["box_bars"], low, high, box_atr)
    vol_ma = float(work["tick_volume"].iloc[-(P["volume_bars"] + P["recent_bars"]):-P["recent_bars"]].mean())
    if box_atr > P["max_box_atr"] or vol_ma <= 0:
        return Assessment(0, setup_details(box_text + "，突破前没有收敛", empty_volume, pa_score, pa_text, trend_text, 0), structure, facts)
    edge = high if action == "buy" else low
    sign = 1 if action == "buy" else -1
    pushes = recent.iloc[:-1]
    broke = pushes[sign * (pushes["close"] - edge) > P["break_atr"] * atr]
    facts["breakout_closed"] = bool(not broke.empty)
    last = recent.iloc[-1]
    if broke.empty:
        return Assessment(0, setup_details(box_text + "，没有收盘突破", empty_volume, pa_score, pa_text, trend_text, 0), structure, facts)
    if sign * (float(last["close"]) - edge) <= 0:
        return Assessment(0, setup_details(box_text + "，回踩收回箱体，突破失败", empty_volume, pa_score, pa_text, trend_text, 0), structure, facts)
    # The retest must happen after the first breakout close; the breakout bar's own wick is still inside the box.
    retested = retest_facts(recent, broke, edge, atr, action, P["retest_atr"])["retested"]
    facts["retested"] = retested
    facts["close_holds_edge"] = True
    break_ratio = float(broke["tick_volume"].max()) / vol_ma
    break_score = P["strong_volume_score"] if break_ratio >= P["strong_volume"] else P["normal_volume_score"] if break_ratio >= P["normal_volume"] else P["weak_volume_score"]
    volume = {"score": break_score, "summary": "突破K量能 %.2fx" % break_ratio}
    retest_score = P["retest_score"] if retested else 0
    holds = (float(last["close"]) > float(last["open"])) if action == "buy" else (float(last["close"]) < float(last["open"]))
    if holds and pa_score < P["hold_score"]:
        pa_score = P["hold_score"]
        pa_text = (pa_text + "，回踩后收" + ("阳" if action == "buy" else "阴")).strip("，")
    text = box_text + ("，回踩到箱体边缘后守住" if retested else "，没有回踩，属于追突破")
    score = break_score + retest_score + pa_score + trend_score
    if not retested:
        score = min(score, P["no_retest_cap"])
    return Assessment(max(0, min(100, score)), setup_details(text, volume, pa_score, pa_text, trend_text, retest_score), structure, facts)

STRATEGY = Strategy(
    id="breakout", name="突破回踩", version="1", sessions={"london": 0, "overlap": 0},
    parameters=P, evaluate=evaluate, describe=describe,
    momentum=lambda side, rsi, hist, prev: momentum_bonus(side, rsi, hist, prev, missing_macd=8),
    ai_rules={
        "entry": f"H1同向；突破前{P['box_bars']}根M15箱体≤{P['max_box_atr']}ATR，收盘越过边缘{P['break_atr']}ATR，突破后的K线回踩{P['retest_atr']}ATR内并守住；无回踩不追，本地封顶{P['no_retest_cap']}。",
        "review": "按parameters和strategy_evidence核对收盘突破、回踩先后及守住箱体；不能把突破K自己的影线当作之后回踩。量能仅辅助。",
        "guardian": "按入场保存的箱体边缘及突破回踩失效条件护盘；只有指定周期已收盘证据才可确认失效，止损只能收紧。"})
