"""Offline evidence regressions; no terminal connection, model call or orders."""
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai

base = ai.base


def bars(rows, step=900, start=1800000000):
    return base.pd.DataFrame([
        dict(time=start + i * step, open=o, high=h, low=l, close=c, tick_volume=v)
        for i, (o, h, l, c, v) in enumerate(rows)
    ])


def mirror(frame):
    result = frame.copy()
    result['open'] = 200 - frame['open']
    result['close'] = 200 - frame['close']
    result['high'] = 200 - frame['low']
    result['low'] = 200 - frame['high']
    return result


class EntryEvidenceTests(unittest.TestCase):
    def test_m15_false_break_can_coexist_with_m5_no_trigger(self):
        m15 = bars([(4118, 4121.39, 4116, 4119, 100)] * 7 +
                   [(4116.99, 4121.77, 4116.28, 4121.02, 100)])
        m5 = bars([(4119.5, 4122, 4118, 4120, 100)] * 7 +
                  [(4119.97, 4121.77, 4119.62, 4121.02, 100)], step=300)
        for action, a, b in [('sell', m15, m5), ('buy', mirror(m15), mirror(m5))]:
            first = ai._price_action_evidence(a, 'M15', action, 5.77)
            second = ai._price_action_evidence(b, 'M5', action, 2.0)
            self.assertTrue(first['checks']['false_break'])
            self.assertEqual(first['score'], 20)
            self.assertEqual(second['score'], 0)
            self.assertEqual(first['prior_bar_times'], list(a.time.iloc[-7:-1]))
            self.assertEqual((first['score'], first['description']), base.classify_price_action(a, action, 5.77))

    def test_price_action_insufficient_data_is_unknown(self):
        facts = ai._price_action_evidence(None, 'M5', 'sell', 0)
        self.assertFalse(facts['available'])
        self.assertIsNone(facts['bar_time'])
        self.assertNotIn('checks', facts)

    def breakout(self, touch):
        return bars([(100, 101, 99, 100, 100)] * 40 + [
            (100, 106, 99.5, 103, 90),
            (103, 104, 101.3 if touch else 102, 103, 90),
            (103, 104, 102, 103, 90),
            (103, 104, 102, 103.5, 90)])

    def test_breakout_requires_later_touch_buy_and_sell(self):
        for action in ('buy', 'sell'):
            for touch in (False, True):
                with self.subTest(action=action, touch=touch):
                    frame = self.breakout(touch)
                    if action == 'sell':
                        frame = mirror(frame)
                    facts = ai._breakout_evidence(frame, 1, action)
                    self.assertTrue(facts['breakout_closed'])
                    self.assertEqual(facts['retest_holds'], touch)
                    self.assertEqual(bool(facts['retest_bars']), touch)
                    self.assertEqual(facts['breakout_volume_ratio'], .9)
                    score, details, _ = base.score_breakout(action, frame, 1,
                                                           'bull' if action == 'buy' else 'bear', 'neutral')
                    self.assertEqual(int(details['fib_score']), 30 if touch else 0)
                    if not touch:
                        self.assertLessEqual(score, 45)

    def test_edge_lost_after_touch_is_not_held(self):
        frame = self.breakout(True)
        frame.loc[frame.index[-1], 'close'] = 100.5
        for action, sample in [('buy', frame), ('sell', mirror(frame))]:
            facts = ai._breakout_evidence(sample, 1, action)
            self.assertTrue(facts['retested'])
            self.assertFalse(facts['retest_holds'])

    def test_fib_impulse_not_independent_and_m5_maps_to_same_event(self):
        for action in ('buy', 'sell'):
            frame = self.breakout(True)
            m5 = bars([(103, 106, 102, 104, 90)], step=300, start=int(frame.iloc[-4]['time']) + 300)
            if action == 'sell':
                frame, m5 = mirror(frame), mirror(m5)
            fib = base.build_fib_levels('bull' if action == 'buy' else 'bear',
                                        float(frame.low.min()), float(frame.high.max()))
            facts = ai._breakout_evidence(frame, 1, action)
            candidates = ai._obstacle_candidates(frame, m5, fib, facts, float(frame.iloc[-1]['close']), action)
            target = 106 if action == 'buy' else 94
            same = [c for c in candidates if c['price'] == target]
            self.assertEqual(len(same), 1)
            self.assertEqual(same[0]['classification'], 'process_extreme')
            self.assertTrue(any(o['timeframe'] == 'M5' for o in same[0]['observed']))
            self.assertIn('candle_parts', same[0]['origin_bar'])

    def test_repeated_same_price_is_separate_event(self):
        frame = bars([(100, 106, 99, 100, 100), (100, 105, 99, 100, 100), (100, 106, 99, 100, 100)])
        fib = base.build_fib_levels('bull', 99, 106)
        candidates = ai._obstacle_candidates(frame, None, fib, {}, 100, 'buy')
        same = [c for c in candidates if c['price'] == 106]
        self.assertEqual(len(same), 2)
        self.assertEqual(len({c['event_group'] for c in same}), 2)
        self.assertIsNone(ai._closest_bar(frame, 'high', 106))
        self.assertIsNone(ai._closest_bar(frame, 'high', 107))

    def test_missing_fib_origin_is_unknown(self):
        frame = self.breakout(True)
        fib = base.build_fib_levels('bull', 99, 110)
        candidates = ai._obstacle_candidates(frame, None, fib, {}, 103, 'buy')
        missing = next(c for c in candidates if c['price'] == 110)
        self.assertIsNone(missing['bar_time'])
        self.assertIsNone(missing['event_group'])
        self.assertEqual(missing['closed_breakthrough']['status'], 'unknown')

    def test_secondary_reversal_pivot_is_not_erased_by_higher_impulse(self):
        frame = bars([(100, h, 99, 100, 100) for h in (101, 102, 110, 102, 101, 106, 102, 101)])
        for action, sample in [('buy', frame), ('sell', mirror(frame))]:
            candidates = ai._obstacle_candidates(sample, None, None, {}, 100, action)
            secondary = next(c for c in candidates if c['price'] == (106 if action == 'buy' else 94))
            self.assertEqual(secondary['source'], 'confirmed_m15_pivot')
            self.assertNotEqual(secondary['classification'], 'process_extreme')

    def test_old_obstacle_break_and_opposing_evidence_are_exposed(self):
        frame = bars([(100, 101, 99, 100, 100)] * 8 + [
            (100, 106, 99, 101, 100), (104, 108, 103, 107, 100), (104, 105, 102, 103, 100)])
        for action, sample in [('buy', frame), ('sell', mirror(frame))]:
            fib = base.build_fib_levels('bull' if action == 'buy' else 'bear', 94 if action == 'sell' else 99,
                                        101 if action == 'sell' else 106)
            candidates = ai._obstacle_candidates(sample, None, fib, {}, float(sample.iloc[-1]['close']), action)
            item = next(c for c in candidates if c['price'] == (106 if action == 'buy' else 94))
            self.assertEqual(item['closed_breakthrough']['status'], 'observed')
            self.assertEqual(item['opposing_price_action']['status'], 'evaluated')
            self.assertGreater(item['opposing_price_action']['score'], 0)
            parts = item['origin_bar']['candle_parts']
            self.assertEqual(parts['body'], 1)
            self.assertEqual(parts['upper'] if action == 'buy' else parts['lower'], 5)

    def test_ai3_payload_has_matching_closed_timeframes_and_handles_missing(self):
        frame = self.breakout(True)
        with tempfile.TemporaryDirectory() as folder, patch.object(ai, 'AI3_STATE_FILE', str(Path(folder) / 'state.json')):
            trader = ai.ThreeAITrader()
            position = SimpleNamespace(ticket=123, type=base.mt5.POSITION_TYPE_BUY, price_open=100,
                                       time=1800000000, volume=.01, sl=98, tp=104, profit=0)
            tick = SimpleNamespace(bid=100, ask=100.1, time=1800003600)
            for missing in (False, True):
                captured = {}
                def rates(timeframe, count, shift):
                    self.assertEqual(shift, 1)
                    return base.pd.DataFrame() if missing and timeframe == base.mt5.TIMEFRAME_M15 else frame
                def prompt(_, payload):
                    captured.update(payload)
                    return json.dumps(payload)
                with patch.object(trader, 'get_positions', return_value=[position]), \
                     patch.object(trader, 'get_rates', side_effect=rates) as get_rates, \
                     patch.object(base.mt5, 'symbol_info_tick', return_value=tick), \
                     patch.object(ai, 'prompt_json', side_effect=prompt), \
                     patch.object(ai, 'call_chat', return_value='{"action":"hold","ticket":123}') as chat, \
                     patch.object(trader, 'ai3_is_current', return_value=True), \
                     patch.object(trader, 'release_task'), patch.object(trader, 'execute_ai3'):
                    trader.ai3_task(1, 1)
                self.assertEqual(chat.call_count, 1)
                self.assertEqual(get_rates.call_count, 4)
                self.assertEqual(bool(captured['m15_bars']), not missing)
                self.assertTrue(captured['h1_bars'])
                self.assertEqual(captured['bar_closure']['source_shift'], 1)


if __name__ == '__main__':
    unittest.main()
