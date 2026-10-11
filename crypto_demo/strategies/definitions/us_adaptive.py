"""DEMO-only US overlap/late-session context for autonomous AI proposals."""
from crypto_demo.strategies.adaptive import evaluate_profile
from crypto_demo.strategies.framework import Strategy

P = dict(profile="us_adaptive", target_r=3., max_hold_minutes=90, cooldown_seconds=60,
         soft_scoring=dict(range_edge_fraction=.3, small_trend_max_atr=1., reaction_range_atr=1.25,
             weights=dict(directional_change=10, same_direction_m5=8, range_edge=3, m5_reclaim=5,
                          small_trend=2, directional_expansion=5, prior_extreme_cross_hold=3, volatility_reaction=10)))


def evaluate(c):
    return evaluate_profile(c, P["profile"], P["soft_scoring"])


STRATEGY = Strategy(
    id="us_adaptive", name="美盘自主分析", version="1", sessions={"overlap": 0, "late_us": 0},
    parameters=P, evaluate=evaluate, enabled=True, analysis_mode=True, demo_only=True,
    entry_reference="closed_m15", stop_reference="m5_extreme", target_r=P["target_r"],
    max_hold_minutes=P["max_hold_minutes"], cooldown_seconds=P["cooldown_seconds"], one_entry_per_event=True,
    m5_boost=False, regime_filter="none",
    ai_rules={
        "entry": f"美盘自主分析覆盖欧美重叠及尾盘：可参考趋势续势、双向扫动后的确认、区间反转及回撤；AI-1自主选择打法、真实证据、SL/TP或观望，菜单不固定方向。H1同向仍是硬限制，不能借反转主题逆H1。analysis_checkpoint只是分析/去重范围，不是已确认交易触发；不要求本地高分、固定形态、Fib38–62%或首次开盘回踩，反向单根M5交AI判断风险。真实OHLC、位置、极值、ATR/EMA、tick_volume代理支持提案；没有实际宏观信息、日历或波动原因则标未知，不凭波动编新闻。CME波动/支撑阻力与WGC分时段资料只供背景，有限B站信息不等于完整算法或胜率。保留SL8/RR1.5、近三M5极值及缓冲、EMA21追价1.5ATR及止损触发侧2倍buffer。成功请求买Ask/卖Bid参考、{P['target_r']:g}R上限，不缩短真实结构SL。仅DEMO/.01手/最多1仓，JSON/质量门保持。",
        "review": f"美盘AI-2审核原续势、扫动确认、回撤或区间反转打法的真实闭合K线与风险事实，不要求固定本地形态/首次开盘事件。检查点只为去重，不能替代触发证据；反转也不得逆H1，双向扫动未出现不能编造。核对原来源、方向、UTC偏移、期限和指纹；Python固定SL/TP用本次Bid/Ask审核SL8、RR1.5、近三M5保护、EMA1.5ATR及触发侧2倍buffer。波动突变原因与宏观日历无来源则未知。仅DEMO、单仓、同检查点一次、{P['cooldown_seconds']}秒冷却保留，不放宽价格/质量门。",
        "guardian": f"美盘AI-3保存入场自主续势/扫动/反转打法和原失效证据，以最新真实收盘周期与本票报价判断持有、收紧或平仓；双向波动不是无来源宏观结论，不自动按时段反手。不能扩SL、加仓或接管别的票；原契约冻结，{P['target_r']:g}R目标只是参考上限而非收益承诺，Python{P['max_hold_minutes']}分钟期限不可延长。"})
