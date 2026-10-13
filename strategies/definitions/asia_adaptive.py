"""DEMO-only Asian-session broad context for autonomous AI proposals."""
from strategies.adaptive import evaluate_profile
from strategies.framework import Strategy

P = dict(profile="asia_adaptive", target_r=3., max_hold_minutes=90, cooldown_seconds=60,
         soft_scoring=dict(range_edge_fraction=.3, small_trend_max_atr=1., reaction_range_atr=1.25,
             weights=dict(directional_change=5, same_direction_m5=5, range_edge=10, m5_reclaim=10,
                          small_trend=8, directional_expansion=2, prior_extreme_cross_hold=2, volatility_reaction=0)))


def evaluate(c):
    return evaluate_profile(c, P["profile"], P["soft_scoring"])


STRATEGY = Strategy(
    id="asia_adaptive", name="亚盘自主分析", version="1", sessions={"asia": 0},
    parameters=P, evaluate=evaluate, enabled=True, analysis_mode=True, demo_only=True,
    entry_reference="closed_m15", stop_reference="m5_extreme", target_r=P["target_r"],
    max_hold_minutes=P["max_hold_minutes"], cooldown_seconds=P["cooldown_seconds"], one_entry_per_event=True,
    m5_boost=False, regime_filter="none",
    ai_rules={
        "entry": f"亚盘自主分析：区间边缘回收、流动性扫回、小趋势整理可优先考虑；明显趋势也可回撤或续势，不假定亚盘永远震荡。AI-1自主选择打法、真实证据及SL/TP或观望，菜单不是固定路由，未匹配本地形态也可由原始收盘K线证明。analysis_checkpoint只限定本次分析/去重，不是已确认交易触发；本地低分、无Fib38–62%、无首次开盘突破回踩和反向单根M5均不自动否决。引用真实OHLC、位置、极值、ATR/EMA与量能代理，并说明反向M5等风险及未知；宏观/新闻无实际来源则未知。知识仅参考位置、支撑阻力区域与回收，不声称完整复现B站算法/胜率。H1同向、SL8/RR1.5、近三M5极值加缓冲、EMA21追价1.5ATR及实时触发侧2倍buffer仍为Python硬门。成功请求实时买Ask/卖Bid为AI参考，目标上限{P['target_r']:g}R，不硬拉SL。仅DEMO/.01手/单仓，有效JSON/质量门保持。",
        "review": f"亚盘AI-2审核已选打法与原始位置、区间边缘/扫回/整理证据是否一致，不再次强迫固定回收或拒绝形态。检查点不是触发，提案仍须真实闭合K线证明；区间中部、追价及单根反向M5可构成事实风险，不能凭时段直接否决。核对绑定来源、方向、UTC偏移/期限/指纹及当次新Bid/Ask；审核Python固定SL/TP的SL8、RR1.5、M5保护、EMA1.5ATR及止损触发侧2倍buffer。未知新闻不臆造，量能只是代理。仅模拟账户、单仓、同检查点一次、冷却{P['cooldown_seconds']}秒由Python执行；不给no/REJECT换模型重审。",
        "guardian": f"亚盘AI-3按入场保存的自主打法、回收区域或小趋势整理理由及失效证据护盘；若当时自主选趋势续势，按原理由判断而非强制区间规则。引用真实周期收盘及新持仓/报价，区间再次回收失败或原趋势失效必须说明证据，未知新闻不可推断。只持有、收紧或平仓，不能扩大SL/加仓；目标{P['target_r']:g}R不保证收益或强迫持有。原契约冻结，{P['max_hold_minutes']}分钟为持仓复核节点，不能仅因时间到直接平仓；原理由仍有效则继续护仓，原SLTP和单向锁盈保持。"})
