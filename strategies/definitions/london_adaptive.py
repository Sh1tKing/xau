"""DEMO-only London-session broad context for autonomous AI proposals."""
from strategies.adaptive import evaluate_profile
from strategies.framework import Strategy

P = dict(profile="london_adaptive", target_r=3., max_hold_minutes=90, cooldown_seconds=60,
         soft_scoring=dict(range_edge_fraction=.3, small_trend_max_atr=1., reaction_range_atr=1.25,
             weights=dict(directional_change=5, same_direction_m5=5, range_edge=2, m5_reclaim=3,
                          small_trend=2, directional_expansion=12, prior_extreme_cross_hold=8, volatility_reaction=3)))


def evaluate(c):
    return evaluate_profile(c, P["profile"], P["soft_scoring"])


STRATEGY = Strategy(
    id="london_adaptive", name="欧盘自主分析", version="1", sessions={"london": 0},
    parameters=P, evaluate=evaluate, enabled=True, analysis_mode=True, demo_only=True,
    entry_reference="closed_m15", stop_reference="m5_extreme", target_r=P["target_r"],
    max_hold_minutes=P["max_hold_minutes"], cooldown_seconds=P["cooldown_seconds"], one_entry_per_event=True,
    m5_boost=False, regime_filter="none",
    ai_rules={
        "entry": f"欧盘自主分析：可参考隔夜区间扩张、支撑阻力角色转换、突破后整理回踩，也可判断失败突破回收。AI-1自主选择打法、真实证据及SL/TP或观望，不必先完成本地固定突破/回踩或首次开盘事件；没有代码匹配形态也可引用真实收盘K线证明。analysis_checkpoint仅为分析去重，不是确认交易触发。本地低分、Fib位置及反向单根M5作为参考风险，不直接取消自主提案。OHLC/ATR/EMA、位置、近期极值和tick_volume代理必须来自给定来源，未覆盖隔夜全段就标覆盖未知，不能编造箱体/新闻。CME区域、角色转换、整理延续及有限B站资料仅为知识参考，不是完整算法/胜率。保留H1同向、SL8/RR1.5、近三M5极值及缓冲、EMA21追价1.5ATR、实时触发侧2倍buffer。成功AI请求买Ask/卖Bid为参考，{P['target_r']:g}R目标上限，不裁短结构SL。仅DEMO/.01手/单仓，严格JSON和质量门。",
        "review": f"欧盘AI-2审核原自主打法的扩张/角色转换/整理或失败突破事实，不强制再命中本地箱体、首次突破、回踩或固定时窗。检查点不是触发，真实闭合K线/原始区域及覆盖应能支持提案；缺失隔夜覆盖不当成已知完整区间。核对方向、最新来源、UTC偏移、期限和策略指纹；固定Python SL/TP用当次实时Bid/Ask验证SL8、RR1.5、近三M5保护、EMA1.5ATR、触发侧2倍buffer，不允许移动SL追价。宏观事件无来源则未知，量能仅代理。仅DEMO、单仓、单检查点一次尝试和{P['cooldown_seconds']}秒冷却仍生效。",
        "guardian": f"欧盘AI-3保留入场自主打法、角色转换价区或整理失效理由，以真实闭合周期证据判断扩张失败/区域重新失守；自主失败突破打法按原契约护盘，不强迫继续突破。只持有、收紧或平仓，不放宽止损/加仓；没有最新事实就标未知，不制造宏观理由。原始策略契约冻结，{P['target_r']:g}R参考目标不要求一直持有；{P['max_hold_minutes']}分钟触发持仓复核而非强平，原理由有效可继续，原SLTP和单向锁盈保持。"})
