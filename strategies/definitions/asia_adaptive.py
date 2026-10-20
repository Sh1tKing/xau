"""DEMO-only Asian-session broad context for autonomous AI proposals."""
from strategies.adaptive import evaluate_profile
from strategies.direction import DEFAULT_POLICY
from strategies.framework import Strategy
from strategies.chop import DEFAULT_POLICY as CHOP_POLICY

P = dict(chop_policy=dict(CHOP_POLICY), profile="asia_adaptive", target_r=3., max_hold_minutes=90, cooldown_seconds=60,
         soft_scoring=dict(range_edge_fraction=.3, small_trend_max_atr=1., reaction_range_atr=1.25,
             weights=dict(directional_change=5, same_direction_m5=5, range_edge=10, m5_reclaim=10,
                          small_trend=8, directional_expansion=2, prior_extreme_cross_hold=2, volatility_reaction=0)))


def evaluate(c):
    return evaluate_profile(c, P["profile"], P["soft_scoring"], direction_policy=STRATEGY.direction_policy)


STRATEGY = Strategy(
    id="asia_adaptive", name="亚盘自主分析", version="4", sessions={"asia": 0},
    parameters=P, evaluate=evaluate, enabled=True, analysis_mode=True, demo_only=True,
    direction_policy=dict(DEFAULT_POLICY),
    entry_reference="closed_m15", stop_reference="m5_extreme", target_r=P["target_r"],
    max_hold_minutes=P["max_hold_minutes"], cooldown_seconds=P["cooldown_seconds"], one_entry_per_event=True,
    m5_boost=False, regime_filter="none",
    ai_rules={
        "entry": f"反复震荡按entry_chop机器确认审核，全天120分钟窗，凌晨01–07额外60分钟窗；命中后中部小形态/旧破位不得入场，只接受原箱外两M5收盘、边缘拒绝下一M5确认或机器已确认的近期同向推进；旧大箱不封死新方向机会，放行仍非交易信号。亚盘自主分析：区间边缘回收、流动性扫回、小趋势整理可优先考虑；明显趋势也可回撤或续势，不假定亚盘永远震荡。AI-1自主选择打法、真实触发、SL/TP或观望，菜单不是固定路由。H1只是真实背景；h1-or-m15-structure-v1的机器direction_permission决定本候选许可，BUY/SELL完全镜像：H1偏空时有效M15上涨可许可BUY，H1偏多时有效M15下跌可许可SELL。反向结构未失败时，不仅凭H1或小M5影线反复试原方向；原H1方向恢复须机器确认连续两根M5收盘失败证据，不能自行猜失败。许可不是自动触发；未知/缺证明不得交易。analysis_checkpoint限定分析与去重，本地低分、无Fib38–62%、无首次开盘回踩和反向单根M5均不自动否决。引用真实OHLC、ATR/EMA、极值及tick_volume代理，说明最新支持事实与最强反证；新闻无来源则未知。采用机器规范保护锚点并合并近三M5极值加原缓冲，真实结构SL放不进SL8就等待，禁止向内裁；RR1.5、EMA21追价1.5ATR及触发侧2倍buffer保持。目标须解释反向障碍和空间，不把每根影线都变硬门；买Ask/卖Bid参考，目标上限{P['target_r']:g}R。近期已结算赚亏及双向交易同看，不因亏损自动反手。仅DEMO/.01手/单仓，严格JSON/质量门及单请求预算保持，不声称算法复现或收益。",
        "review": f"核entry_chop原路线与最新报价，震荡原箱/确认失效则拒绝，不凭H1许可或旧M15破位解除。亚盘AI-2审核原自主打法、位置、区间边缘/扫回/整理与真实触发，不强制固定形态。H1为背景，BUY/SELL按原direction_permission机器许可镜像审核，合法M15短线方案可逆H1；无原证明的逆H1仍拒绝。原H1方向恢复须连续两根M5收盘失败证明，不凭小影线重试。每POST核原来源、事件、规范保护锚点、方向、UTC偏移、检查点/期限/指纹和新Bid/Ask；Python固定SL/TP须覆盖规范锚点及近三M5极值加原缓冲，保留SL8、RR1.5、EMA1.5ATR和触发侧2倍buffer。原事件/支点失效或新检查点到来即拒绝旧提案，不换方向、迁移支点或重写SL/TP。未知新闻不臆造，量能仅代理。仅DEMO、单仓、同检查点一次尝试和冷却{P['cooldown_seconds']}秒保持；no/REJECT/截断不重审。",
        "guardian": f"亚盘AI-3按入场保存的自主打法、direction_permission原路线、结构事件、规范保护锚点和失效证据护盘，BUY/SELL镜像判断。H1真实背景在入场时已知，不能仅因当前H1逆向自动平仓；不以当前策略重建旧仓。原区间回收或趋势续势失效必须引用真实闭合周期及本票新报价证据，未知新闻不推断。可持有、收紧SL、按冻结tp_review复核TP或平仓，不能扩SL/加仓；目标{P['target_r']:g}R不保证收益或强迫持有。原契约冻结，入场许可/检查点过期不等于持仓理由失效；{P['max_hold_minutes']}分钟为持仓复核节点，不能仅因时间到直接平仓。原始SLTP/风险记录保留；新仓profit-floor-v3按持续净浮盈现金阶梯保小利并留回撤空间，普通跟随保留完整M5空间。AI-3按闭合M5/M15/H1及原理由复核SL和TP，动态TP只在冻结tp_review授权时执行，不能撤SL或扩风险；已有旧仓沿用原冻结规则。"})
