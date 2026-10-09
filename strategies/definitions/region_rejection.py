"""Disabled research first region touch rejection definition."""
from strategies.framework import Strategy, Assessment
from strategies.regions import region_facts

P = dict(region_event_kind='rejection', pivot_lookback=48, pivot_left=2, pivot_right=2, atr_period=14,
         halfwidth_atr=.10, region_valid_bars=16, break_atr=.05, retest_atr=.25,
         hold_atr=.05, invalidation_atr=.30, reset_atr=.50, max_retest_bars=6,
         max_retest_seconds=1800, target_r=2.5, max_hold_minutes=120, cooldown_seconds=600)


def evaluate(c):
    facts = region_facts('rejection', c.df_m15, c.df_m5, c.now, c.server_utc_offset, c.action, P)
    blockers = list(facts['blockers'])
    permitted = c.h1_trend == ('bull' if c.action == 'buy' else 'bear')
    facts['h1_permitted'] = permitted
    if not permitted:
        blockers.append('研究策略仍要求H1同向')
    confirmed = facts['closed_trigger']['confirmed']
    return Assessment(85 if confirmed and not blockers else 0,
        {'structure': '确认M15区域首次触碰拒绝', 'price_action': '最新首次收盘触发' if confirmed else '等待首次触发'},
        evidence=facts, blockers=tuple(blockers))


STRATEGY = Strategy(id='region_rejection', name='盘中区域触碰拒绝', version='1',
    enabled=False, sessions={'asia':0, 'london':0, 'overlap':0, 'late_us':0},
    parameters=P, evaluate=evaluate, target_r=P['target_r'], m5_boost=False,
    entry_reference='closed_m5', stop_reference='region', max_hold_minutes=P['max_hold_minutes'],
    cooldown_seconds=P['cooldown_seconds'], demo_only=True, one_entry_per_event=True,
    ai_rules={
        'entry': f"项目假设：M15最近{P['pivot_lookback']}根严格左右2确认区域，ATR{P['atr_period']}冻结，半宽{P['halfwidth_atr']}ATR、有效{P['region_valid_bars']}根；伦敦08–17或纽约08–16当地DST并集。BUY首次触碰support后low<=lower-{P['break_atr']}ATR且close>=center+{P['hold_atr']}ATR；SELL镜像resistance。不强制颜色/吞没/普通量。首次有效拒绝错过即消耗，整根离开{P['reset_atr']}ATR并M15确认后才重置；触发前反向M15收盘越外沿{P['break_atr']}ATR令周期失效。H1同向、目标上限{P['target_r']}R。",
        'review': '核对pivot5/冻结ATR、触碰周期原始前缀与重置、首次有效trigger/UTC和窗口，不用滚动窗口制造新事件。risk_anchor为BUY lower-.30ATR / SELL upper+.30ATR；止损同时保护最近3M5加生产M15缓冲/下限；保留SL8/RR1.5/EMA1.5ATR和实时余量。障碍由Python审核，模拟账户与一事件一次。',
        'guardian': f"绑定原始region/entry_event及失效证据，未知不猜。仅持有/收紧/平仓，不放宽止损或延长{P['max_hold_minutes']}分钟；平仓冷却{P['cooldown_seconds']}秒。"})
