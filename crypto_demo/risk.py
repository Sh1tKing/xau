"""CFD DEMO execution risk. No terminal initialization or environment access."""
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math
import time


class RiskError(ValueError):
    pass


class QuoteError(RiskError):
    """Recoverable quote rejection with clock evidence, never credentials."""
    def __init__(self, reason, facts):
        self.reason = reason
        self.facts = facts
        fields = " ".join(f"{key}={value}" for key, value in facts.items() if value is not None)
        super().__init__(f"quote rejected: {reason} {fields}")


def number(value, positive=True):
    if isinstance(value, bool):
        raise RiskError("boolean numeric value")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise RiskError("missing numeric value") from exc
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise RiskError("invalid numeric value")
    return result


def quantize(value, step, upward=False):
    value, step = number(value), number(step)
    units = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(
        rounding=ROUND_CEILING if upward else ROUND_FLOOR)
    return float(units * Decimal(str(step)))


def price_cap(asset, atr, reference):
    if asset not in ("BTC", "ETH"):
        raise RiskError("unknown asset")
    return min(2.5 * number(atr), number(reference) * (.015 if asset == "BTC" else .02))


def buffer(atr, tick_size):
    return max(.15 * number(atr), 2 * number(tick_size))


def budget(equity):
    return min(8., .08 * min(number(equity), 100.))


def account_ready(mt5, account, empty=False):
    if account is None or getattr(account, "trade_mode", None) != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise RiskError("DEMO account required")
    if getattr(account, "currency", None) != "USD":
        raise RiskError("USD account required")
    number(getattr(account, "equity", None))
    if empty:
        positions = mt5.positions_get()
        if positions is None or len(positions):
            raise RiskError("account positions unknown or nonempty")


def spec_values(spec):
    if spec is None:
        raise RiskError("missing contract specification")
    result = {key: number(getattr(spec, key, None)) for key in
              ("trade_tick_size", "volume_min", "volume_max", "volume_step", "trade_contract_size", "point")}
    if result["volume_min"] > result["volume_max"]:
        raise RiskError("invalid volume range")
    result["trade_stops_level"] = number(getattr(spec, "trade_stops_level", None), False)
    result["trade_freeze_level"] = number(getattr(spec, "trade_freeze_level", None), False)
    for key in ("volume_min",):
        units = Decimal(str(result[key])) / Decimal(str(result["volume_step"]))
        if abs(units-units.to_integral_value()) > Decimal("0.00000001"):
            raise RiskError("broker minimum volume not on step lattice")
    return result


def quote_prices(tick, now=None, server_offset_hours=0):
    def fact(value):
        try:
            parsed = float(value)
            return parsed if not isinstance(value, bool) and math.isfinite(parsed) else None
        except (ValueError, TypeError, OverflowError):
            return None
    observed = time.time() if now is None else now
    facts = {"bid": fact(getattr(tick, "bid", None)), "ask": fact(getattr(tick, "ask", None)),
             "raw_time_sec": fact(getattr(tick, "time", None)),
             "raw_time_msc": fact(getattr(tick, "time_msc", None)),
             "offset_hours": fact(server_offset_hours), "observed_utc": fact(observed),
             "tick_utc": None, "age_sec": None}
    if tick is None:
        raise QuoteError("missing_quote", facts)
    try:
        bid, ask = number(getattr(tick, "bid", None)), number(getattr(tick, "ask", None))
    except RiskError as exc:
        raise QuoteError("invalid_bid_ask", facts) from exc
    try:
        stamp = number(getattr(tick, "time", None))
    except RiskError as exc:
        raise QuoteError("invalid_tick_time", facts) from exc
    try:
        offset = float(server_offset_hours)
    except (TypeError, ValueError, OverflowError) as exc:
        raise QuoteError("invalid_configured_offset", facts) from exc
    if isinstance(server_offset_hours, bool) or not math.isfinite(offset) or not -12 <= offset <= 14:
        raise QuoteError("invalid_configured_offset", facts)
    try:
        observed = number(observed)
    except RiskError as exc:
        raise QuoteError("invalid_observed_clock", facts) from exc
    facts["tick_utc"] = stamp-offset*3600
    age = observed-facts["tick_utc"]
    facts["age_sec"] = round(age, 6)
    if ask < bid:
        raise QuoteError("crossed", facts)
    if age < 0:
        raise QuoteError("future", facts)
    if age > 30:
        raise QuoteError("stale", facts)
    return bid, ask


def check_manual_order_prices(action, bid, ask, sl, tp):
    """Human prices stay fixed; broker stop direction uses the closing quote side."""
    failures = []
    if action == "sell":
        if sl <= ask:
            failures.append(f"SELL 止损SL必须高于当前Ask({ask:g})")
        if tp >= bid:
            failures.append(f"SELL 止盈TP必须低于当前Bid({bid:g})")
    elif action == "buy":
        if sl >= bid:
            failures.append(f"BUY 止损SL必须低于当前Bid({bid:g})")
        if tp <= ask:
            failures.append(f"BUY 止盈TP必须高于当前Ask({ask:g})")
    else:
        raise RiskError("invalid direction")
    if failures:
        raise RiskError(f"{action.upper()} SL/TP方向非法: Bid={bid:g} Ask={ask:g} SL={sl:g} TP={tp:g}；"
                        + "；".join(failures))


def execution_prices(asset, action, atr, ema, sl, tp, tick, spec, server_offset_hours=0, *, manual=False):
    if action not in ("buy", "sell"):
        raise RiskError("invalid direction")
    values = spec_values(spec)
    bid, ask = quote_prices(tick, server_offset_hours=server_offset_hours)
    sl, tp = map(number, (sl, tp))
    sign = 1 if action == "buy" else -1
    entry, trigger = (ask, bid) if sign == 1 else (bid, ask)
    distance, reward = sign*(entry-sl), sign*(tp-entry)
    if manual:
        check_manual_order_prices(action, bid, ask, sl, tp)
        minimum = values["trade_stops_level"]*values["point"]
    else:
        atr, ema = map(number, (atr, ema))
        cap, buf = price_cap(asset, atr, entry), buffer(atr, values["trade_tick_size"])
        if not 0 < distance <= cap or reward <= 0 or reward/distance < 1.5:
            raise RiskError("price cap / direction / gross RR rejected")
        if abs(entry-ema) > 1.5*atr or ask-bid > .25*atr:
            raise RiskError("EMA chase or spread rejected")
        minimum = max(2*buf, values["trade_stops_level"]*values["point"])
    if (sign*(trigger-sl) <= 0 or sign*(trigger-sl) < minimum
            or sign*(tp-trigger) < values["trade_stops_level"]*values["point"]):
        raise RiskError("trigger clearance rejected")
    for value in (sl, tp):
        ratio = Decimal(str(value))/Decimal(str(values["trade_tick_size"]))
        if abs(ratio-ratio.to_integral_value()) > Decimal("0.00000001"):
            raise RiskError("fixed SL/TP not on broker tick lattice")
    return entry


def size_order(mt5, account, spec, asset, symbol, action, atr, ema, sl, tp, tick,
               commission_per_lot=0., slippage_ticks=0., commission_bps=0., server_offset_hours=0,
               requested_volume=None, *, manual=False):
    account_ready(mt5, account, empty=True)
    values = spec_values(spec)
    entry = execution_prices(asset, action, atr, ema, sl, tp, tick, spec, server_offset_hours, manual=manual)
    fee_lot, slip, bps = map(lambda x: number(x, False),
                            (commission_per_lot, slippage_ticks, commission_bps))
    if bps and getattr(spec, "currency_profit", None) != "USD":
        raise RiskError("bps cash estimate requires USD-priced profit contract; use USD per-lot fee otherwise")
    sign = 1 if action == "buy" else -1
    order_type = mt5.ORDER_TYPE_BUY if sign == 1 else mt5.ORDER_TYPE_SELL
    adverse = entry + sign*slip*values["trade_tick_size"]
    adverse_sl = sl - sign*slip*values["trade_tick_size"]
    adverse_tp = tp - sign*slip*values["trade_tick_size"]
    # Explicit human volume is checked against broker volume and margin, not an AI strategy cash cap.
    # An omitted volume still uses the existing bounded risk sizing.
    limit = None if manual and requested_volume is not None else budget(account.equity)
    # Broker estimates constrain both risk and margin; never shrink the structural SL.
    probe = values["volume_min"]
    loss = mt5.order_calc_profit(order_type, symbol, probe, adverse, adverse_sl)
    win = mt5.order_calc_profit(order_type, symbol, probe, adverse, adverse_tp)
    if loss is None or not math.isfinite(loss) or loss >= 0 or win is None or not math.isfinite(win) or (not manual and win <= 0):
        raise RiskError("broker cash profit calculation unavailable")
    fee = probe*(fee_lot + bps/10000*entry*values["trade_contract_size"])
    unit_loss = (-loss+fee)/probe
    free = .9 * number(getattr(account, "margin_free", None), False)
    probe_margin = number(mt5.order_calc_margin(order_type, symbol, probe, entry))
    if probe_margin > free or (limit is not None and -loss+fee > limit):
        raise RiskError("minimum volume exceeds cash or margin budget")
    cash_volume = values["volume_max"] if limit is None else limit/unit_loss
    volume = quantize(min(values["volume_max"], cash_volume, free*probe/probe_margin), values["volume_step"])
    if volume < probe:
        raise RiskError("minimum broker volume exceeds cash budget")
    # Establish monotonic positive margin estimates, then reduce when exact margin exceeds budget.
    margin = number(mt5.order_calc_margin(order_type, symbol, volume, entry))
    if volume > probe and margin < probe_margin:
        raise RiskError("non-monotonic broker margin estimates")
    if margin > free:
        low, high = int(round(probe/values["volume_step"])), int(round(volume/values["volume_step"]))
        previous_margin = margin
        for _ in range(64):
            if low >= high:
                break
            middle = (low+high+1)//2
            candidate = float(Decimal(middle)*Decimal(str(values["volume_step"])))
            estimate = number(mt5.order_calc_margin(order_type, symbol, candidate, entry))
            if not probe_margin <= estimate <= previous_margin:
                raise RiskError("non-monotonic broker margin estimates")
            if estimate <= free:
                low = middle
            else:
                high = middle-1
        volume = float(Decimal(low)*Decimal(str(values["volume_step"])))
    if volume < probe:
        raise RiskError("minimum volume cannot satisfy margin budget")
    if requested_volume is not None:
        requested = number(requested_volume)
        lattice = Decimal(str(requested))/Decimal(str(values["volume_step"]))
        if (abs(lattice-lattice.to_integral_value()) > Decimal("0.00000001")
                or requested < probe or requested > values["volume_max"]
                or requested > volume + 1e-12):
            raise RiskError("requested volume exceeds cash/margin budget or broker volume lattice; unchanged")
        volume = requested
    final_loss = mt5.order_calc_profit(order_type, symbol, volume, adverse, adverse_sl)
    final_win = mt5.order_calc_profit(order_type, symbol, volume, adverse, adverse_tp)
    if (final_loss is None or not math.isfinite(final_loss) or final_loss >= 0
            or final_win is None or not math.isfinite(final_win) or (not manual and final_win <= 0)):
        raise RiskError("final broker cash calculation unavailable")
    costs = volume*(fee_lot + bps/10000*entry*values["trade_contract_size"])
    cash_loss, cash_reward = -final_loss+costs, final_win-costs
    if (limit is not None and cash_loss > limit + 1e-9) or (not manual and cash_reward/cash_loss < 1.5):
        raise RiskError("cash budget or cost adjusted RR rejected")
    margin = mt5.order_calc_margin(order_type, symbol, volume, entry)
    margin = number(margin)
    if margin > free:
        raise RiskError("insufficient free margin")
    return dict(volume=volume, price=entry, budget_usd=limit, estimated_loss_usd=cash_loss,
                estimated_reward_usd=cash_reward, net_rr=cash_reward/cash_loss,
                configured_cost_usd=costs, margin_usd=margin,
                fee_status="configured estimate, not measured broker fees")


def order_check(mt5, request):
    result = mt5.order_check(request)
    if result is None or getattr(result, "retcode", None) != 0:
        raise RiskError("broker order_check rejected or unavailable")
    for field in ("margin", "margin_free", "equity"):
        number(getattr(result, field, None), field == "equity")
    return result
