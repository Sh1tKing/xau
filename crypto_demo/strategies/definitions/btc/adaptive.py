"""DEMO-only BTC day-trading context for autonomous AI proposals."""
from crypto_demo.strategies.adaptive import evaluate_profile
from crypto_demo.strategies.framework import Strategy

P = dict(profile="btc_adaptive", target_r=3, max_hold_minutes=90, cooldown_seconds=60,
         soft_scoring=dict(range_edge_fraction=.3, small_trend_max_atr=1., reaction_range_atr=1.25,
             weights=dict(directional_change=5, same_direction_m5=5, range_edge=10, m5_reclaim=10,
                          small_trend=8, directional_expansion=2, prior_extreme_cross_hold=2, volatility_reaction=0)))


def evaluate(c):
    return evaluate_profile(c, P["profile"], P["soft_scoring"])


STRATEGY = Strategy(
    id="btc_adaptive", name="BTC日内自主分析", version="1", sessions={"asia": 0, "london": 0, "overlap": 0, "late_us": 0},
    parameters=P, evaluate=evaluate, enabled=True, analysis_mode=True, demo_only=True,
    entry_reference="closed_m15", stop_reference="m5_extreme", target_r=P["target_r"],
    max_hold_minutes=P["max_hold_minutes"], cooldown_seconds=P["cooldown_seconds"], one_entry_per_event=True,
    m5_boost=False, regime_filter="none",
    ai_rules={
        "entry": f"BTC以自身H1方向及M15压缩/扩张、M5回踩/回收观察趋势延续或失败；不把压缩后首个突破当自动买卖信号。自主选择趋势回撤、压缩后突破回踩、扫回确认或有真实收盘证据的打法；时段菜单仅参考，本地低分/no无固定形态不剥夺分析。H1方向许可，M15风险，近三M5极值保护。价格距离上限、tick缓冲、现金预算由Python给出，不能裁短SL。ETH不得只因BTC上涨开多，无同期BTC数据时联动未知。tick_volume仅经纪商代理；OI/资金费率/订单簿/清算/新闻无数据则未知。目标参考上限{P['target_r']}R，风险预算按USD净值和合约计算，禁止固定手数。仅DEMO，账户全部持仓单仓。",
        "review": "审核选定打法真实闭合K线证据及Python定稿固定SL/TP对新报价；H1、价格距离上限、近三M5保护、EMA1.5ATR、RR1.5、spread0.25ATR与触发侧2buffer。无法取得合约、USD现金损益或保证金则拒绝；本地低分、无Fib或时段不单独否决。",
        "guardian": f"只hold/tighten_sl/close；按原入场保存的打法、证据和冻结期限护盘，不用当前定义替换旧仓。禁止加仓/放宽SL；原期限{P['max_hold_minutes']}分钟和冷却{P['cooldown_seconds']}秒由Python执行，参考目标{P['target_r']}R不保证收益。"})
