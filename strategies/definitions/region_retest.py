"""Disabled research region breakout/retest definition, not author replication."""
from strategies.framework import Strategy, Assessment
from strategies.regions import region_facts

P = dict(region_event_kind='retest', pivot_lookback=48, pivot_left=2, pivot_right=2, atr_period=14,
         halfwidth_atr=.10, region_valid_bars=16, break_atr=.05, retest_atr=.25,
         hold_atr=.05, invalidation_atr=.30, reset_atr=.50, max_retest_bars=6,
         max_retest_seconds=1800, target_r=2.5, max_hold_minutes=120, cooldown_seconds=600)


def evaluate(c):
    facts = region_facts('retest', c.df_m15, c.df_m5, c.now, c.server_utc_offset, c.action, P)
    blockers = list(facts['blockers'])
    permitted = c.h1_trend == ('bull' if c.action == 'buy' else 'bear')
    facts['h1_permitted'] = permitted
    if not permitted:
        blockers.append('研究策略仍要求H1同向')
    confirmed = facts['closed_trigger']['confirmed']
    return Assessment(85 if confirmed and not blockers else 0,
        {'structure': '确认M15区域首次突破回踩', 'price_action': '最新首次收盘触发' if confirmed else '等待首次触发'},
        evidence=facts, blockers=tuple(blockers))


STRATEGY = Strategy(id='region_retest', name='盘中区域突破回踩', version='1',
    enabled=False, sessions={'asia':0, 'london':0, 'overlap':0, 'late_us':0},
    parameters=P, evaluate=evaluate, target_r=P['target_r'], m5_boost=False,
    entry_reference='closed_m5', stop_reference='region', max_hold_minutes=P['max_hold_minutes'],
    cooldown_seconds=P['cooldown_seconds'], demo_only=True, one_entry_per_event=True,
    ai_rules={
        'entry': f"项目研究假设，不是作者复现。M15最近{P['pivot_lookback']}根严格左右2确认极值，ATR{P['atr_period']}在确认冻结，半宽{P['halfwidth_atr']}ATR，确认后{P['region_valid_bars']}根有效。当地伦敦08–17/纽约08–16的连续并集窗口内，M15收盘跨外沿{P['break_atr']}ATR；后{P['max_retest_bars']}根/{P['max_retest_seconds']}秒首次M5回踩{P['retest_atr']}ATR且守外{P['hold_atr']}ATR，回收{P['invalidation_atr']}ATR失效。H1同向，目标上限{P['target_r']}R，量能/颜色/吞没不硬否决。",
        'review': '核对原始pivot5、冻结ATR、首次breakout/touch/trigger及UTC偏移、DST窗口、独占TTL，不挑第二次。止损须保护risk_anchor与最近3根M5并加生产M15缓冲/下限，SL8/RR1.5/EMA1.5ATR及实时余量不能放宽；前方反向区域障碍由Python审核。仅模拟账户、一事件一次。',
        'guardian': f"绑定所选region/entry_event契约和原始证据，未知数据不猜；只持有/收紧/平仓，不放宽止损，不延长{P['max_hold_minutes']}分钟期限，平仓冷却{P['cooldown_seconds']}秒。"})
