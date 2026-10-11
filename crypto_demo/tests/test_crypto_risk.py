import math
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from crypto_demo import risk
from crypto_demo.instance_lock import AccountLock
from crypto_demo.pending import PendingOrder


def specification(**changes):
    values = dict(trade_tick_size=.5, volume_min=.001, volume_max=10., volume_step=.001,
                  trade_contract_size=1., point=.1, trade_stops_level=0, trade_freeze_level=0,
                  filling_mode=1, trade_exemode=2, currency_profit="USD")
    return NS(**(values | changes))


def account(**changes):
    return NS(**(dict(currency="USD", trade_mode=0, equity=100., margin_free=100., login=1, server="offline-test") | changes))


def broker():
    value = NS(ACCOUNT_TRADE_MODE_DEMO=0, ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1)
    value.positions_get = Mock(return_value=())
    value.order_calc_profit = Mock(side_effect=lambda kind, symbol, volume, entry, end: (end-entry)*volume*(1 if kind == 0 else -1))
    value.order_calc_margin = Mock(side_effect=lambda kind, symbol, volume, entry: volume*entry*.01)
    return value


class CashRiskTests(unittest.TestCase):
    def sizing(self, asset="BTC", action="buy", **kwargs):
        entry, atr = (100000., 200.) if asset == "BTC" else (3000., 6.)
        sign = 1 if action == "buy" else -1
        mt5 = kwargs.pop("mt5", broker())
        acct = kwargs.pop("account", account())
        spec = kwargs.pop("spec", specification())
        tick = NS(bid=entry if sign == -1 else entry-1, ask=entry if sign == 1 else entry+1, time=time.time())
        return risk.size_order(mt5, acct, spec, asset, asset+"USD.mock", action, atr, entry,
                               entry-sign*atr, entry+sign*3*atr, tick, **kwargs)

    def test_different_assets_directions_tick_and_cash(self):
        for asset in ("BTC", "ETH"):
            for action in ("buy", "sell"):
                with self.subTest(asset=asset, action=action):
                    result = self.sizing(asset, action)
                    self.assertLessEqual(result["estimated_loss_usd"], 8)
                    self.assertGreaterEqual(result["net_rr"], 1.5)

    def test_budget_bounded_by_actual_small_equity(self):
        self.assertEqual(risk.budget(50), 4)
        self.assertEqual(risk.budget(1000), 8)
        self.assertLessEqual(self.sizing(account=account(equity=50))["estimated_loss_usd"], 4)

    def test_cash_step_floor_never_up(self):
        result = self.sizing(spec=specification(volume_step=.003, volume_min=.003))
        self.assertAlmostEqual(result["volume"], .039)
        self.assertLessEqual(result["estimated_loss_usd"], 8)

    def test_minimum_lot_infeasible(self):
        with self.assertRaises(risk.RiskError):
            self.sizing(spec=specification(volume_min=.1, volume_step=.1))

    def test_margin_sizes_down_before_rejecting(self):
        mt5 = broker()
        mt5.order_calc_margin.side_effect = lambda kind, symbol, volume, entry: volume*5000
        result = self.sizing(mt5=mt5)
        self.assertEqual(result["volume"], .018)
        self.assertLessEqual(result["margin_usd"], 90)
        with self.assertRaises(risk.RiskError):
            self.sizing(mt5=mt5, account=account(margin_free=1))

    def test_costs_reserve_budget_and_net_rr(self):
        result = self.sizing(commission_per_lot=10., slippage_ticks=1.)
        self.assertGreater(result["configured_cost_usd"], 0)
        self.assertLessEqual(result["estimated_loss_usd"], 8)
        with self.assertRaises(risk.RiskError):
            self.sizing(commission_bps=100.)

    def test_unknown_non_demo_non_usd_nonfinite(self):
        for acct in (None, account(trade_mode=2), account(currency="EUR"), account(equity=float("nan"))):
            with self.subTest(acct=acct), self.assertRaises(risk.RiskError):
                self.sizing(account=acct)
        for key in ("trade_tick_size", "volume_step", "trade_contract_size"):
            with self.subTest(key=key), self.assertRaises(risk.RiskError):
                self.sizing(spec=specification(**{key: float("nan")}))

    def test_account_all_positions_unknown(self):
        for positions in (None, (NS(symbol="XAUUSD", magic=42),)):
            mt5 = broker()
            mt5.positions_get.return_value = positions
            with self.assertRaises(risk.RiskError):
                self.sizing(mt5=mt5)

    def test_missing_profit_margin_check(self):
        mt5 = broker()
        mt5.order_calc_profit.side_effect = None
        mt5.order_calc_profit.return_value = None
        with self.assertRaises(risk.RiskError):
            self.sizing(mt5=mt5)
        mt5 = broker()
        mt5.order_calc_margin.side_effect = None
        mt5.order_calc_margin.return_value = math.inf
        with self.assertRaises(risk.RiskError):
            self.sizing(mt5=mt5)
        mt5.order_check = Mock(return_value=None)
        with self.assertRaises(risk.RiskError):
            risk.order_check(mt5, {})

    def test_tick_lattice_and_dynamic_price_caps(self):
        self.assertEqual(risk.quantize(100.12, .25), 100)
        self.assertEqual(risk.quantize(100.12, .25, True), 100.25)
        self.assertEqual(risk.price_cap("BTC", 1000, 100000), 1500)
        self.assertEqual(risk.price_cap("ETH", 100, 3000), 60)
        with self.assertRaises(risk.RiskError):
            self.sizing(spec=specification(trade_tick_size=7))


class QuoteClockTests(unittest.TestCase):
    def test_explicit_offset_and_future_boundary_are_not_relaxed(self):
        tick = NS(bid=2498.6, ask=2500.6, time=1791735000, time_msc=1791735000123)
        with self.assertRaises(risk.QuoteError) as caught:
            risk.quote_prices(tick, 1791724199.9191933, 3)
        self.assertEqual(caught.exception.reason, "future")
        self.assertAlmostEqual(caught.exception.facts["age_sec"], -.0808067, places=5)
        self.assertEqual(caught.exception.facts["raw_time_msc"], tick.time_msc)
        self.assertIn("offset_hours=3", str(caught.exception))
        self.assertEqual(risk.quote_prices(tick, 1791724200.5, 3), (2498.6, 2500.6))
        with self.assertRaises(risk.QuoteError) as stale:
            risk.quote_prices(tick, 1791724230.001, 3)
        self.assertEqual(stale.exception.reason, "stale")

    def test_quote_diagnostics_distinguish_crossed_and_invalid_clock(self):
        tick = NS(bid=2500, ask=2499, time=100)
        with self.assertRaises(risk.QuoteError) as caught:
            risk.quote_prices(tick, 101)
        self.assertEqual(caught.exception.reason, "crossed")
        tick.ask = 2502
        for now in (True, float("nan"), float("inf"), 0):
            with self.subTest(now=now), self.assertRaises(risk.QuoteError) as caught:
                risk.quote_prices(tick, now)
            self.assertEqual(caught.exception.reason, "invalid_observed_clock")


class LatchTests(unittest.TestCase):
    def test_account_lock_conflicts_and_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            with AccountLock(1, "server", folder):
                with self.assertRaises(RuntimeError):
                    AccountLock(1, "server", folder).acquire()
                with AccountLock(2, "server", folder):
                    pass
            with AccountLock(1, "server", folder):
                pass

    def test_pending_cross_asset_requires_exact_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"pending.json"
            pending = PendingOrder(path, "account")
            pending.reserve(dict(comment="token", symbol="BTCUSD", magic=1, type=0))
            restarted_eth = PendingOrder(path, "account")
            self.assertTrue(restarted_eth.unresolved(()))
            self.assertTrue(restarted_eth.unresolved((NS(comment="token", symbol="ETHUSD", magic=1, type=0),)))
            with self.assertRaises(RuntimeError):
                restarted_eth.reserve(dict(comment="other", symbol="ETHUSD", magic=1, type=0))
            self.assertFalse(restarted_eth.unresolved((NS(comment="token", symbol="BTCUSD", magic=1, type=0),)))
            self.assertIsNone(pending.read())


if __name__ == "__main__":
    unittest.main()
