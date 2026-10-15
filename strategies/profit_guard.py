"""Frozen, monotonic profit protection. No terminal, AI or environment access."""
import copy
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
import math
import os
from pathlib import Path
import tempfile

VERSION = "profit-floor-v3"
LOSS_REDUCTION_VERSION = "profit-floor-v2"
LEGACY_VERSION = "profit-floor-v1"
# The container schema is unchanged; policies inside it keep their entry version.
STORE_VERSION = LEGACY_VERSION


def finite(value, low=0.0, *, positive=False):
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > low if positive else value >= low))


def policy(**overrides):
    result = dict(version=VERSION, min_net_lock=0.20, fee_reserve=0.10,
                  room_atr=1.0, room_spread=3.0, room_abs=0.50,
                  advance_abs=0.20, advance_atr=0.10, confirm_seconds=1.0,
                  stages=[[0.0, 0.30], [1.0, 0.40], [2.0, 0.50]])
    result.update(overrides)
    if result["version"] == VERSION:
        for key, value in dict(cash_floors=[[2., .20], [4., 1.], [6., 2.], [10., 4.], [15., 6.]],
                               floor_room_r=.25, floor_room_abs=1.,
                               tp_review=dict(version="guardian-target-review-v1", max_r=6.,
                                   max_age_seconds=90., max_move_r=.25,
                                   min_change_abs=.20, min_change_atr=.10)).items():
            result.setdefault(key, value)
    validate_policy(result)
    return result


def validate_policy(value):
    keys = {"version", "min_net_lock", "fee_reserve", "room_atr", "room_spread",
            "room_abs", "advance_abs", "advance_atr", "confirm_seconds", "stages"}
    if isinstance(value, dict) and value.get("version") == VERSION:
        keys |= {"cash_floors", "floor_room_r", "floor_room_abs", "tp_review"}
    if (not isinstance(value, dict) or set(value) != keys
            or value.get("version") not in (LEGACY_VERSION, LOSS_REDUCTION_VERSION, VERSION)):
        raise ValueError("unknown profit guardian policy")
    for key in keys - {"version", "stages", "cash_floors", "tp_review"}:
        if not finite(value[key], positive=key != "fee_reserve"):
            raise ValueError("invalid profit guardian parameter: " + key)
    stages = value["stages"]
    if not isinstance(stages, list) or not stages:
        raise ValueError("invalid profit stages")
    previous_r, previous_fraction = -1., 0.
    for pair in stages:
        if (not isinstance(pair, list) or len(pair) != 2 or not finite(pair[0])
                or not finite(pair[1], positive=True) or pair[1] >= 1
                or pair[0] <= previous_r or pair[1] < previous_fraction):
            raise ValueError("profit stages must increase in R and never reduce retention")
        previous_r, previous_fraction = pair
    if stages[0][0] != 0:
        raise ValueError("profit protection cannot require an initial R threshold")
    if value["version"] == VERSION:
        previous_trigger, previous_floor = 0., 0.
        floors = value["cash_floors"]
        if not isinstance(floors, list) or not floors:
            raise ValueError("invalid cash floor steps")
        for pair in floors:
            if (not isinstance(pair, list) or len(pair) != 2
                    or not all(finite(x, positive=True) for x in pair)
                    or pair[0] <= previous_trigger or pair[1] <= previous_floor
                    or pair[1] < value["min_net_lock"] or pair[1] >= pair[0]):
                raise ValueError("cash floor steps must increase and leave pullback room")
            previous_trigger, previous_floor = pair
        review = value["tp_review"]
        if (not isinstance(review, dict) or set(review) != {"version", "max_r", "max_age_seconds",
                "max_move_r", "min_change_abs", "min_change_atr"}
                or review["version"] != "guardian-target-review-v1"
                or not all(finite(review[k], positive=True) for k in review if k != "version")
                or review["max_r"] > 6 or review["max_age_seconds"] > 90 or review["max_move_r"] > .25):
            raise ValueError("invalid frozen guardian target review policy")


def tighter(direction, old, new):
    return (direction in ("BUY", "SELL") and finite(old) and finite(new, positive=True)
            and (old == 0 or (new > old if direction == "BUY" else new < old)))


def validate_identity(identity):
    if (not isinstance(identity, dict)
            or type(identity.get("ticket")) is not int or identity["ticket"] <= 0
            or type(identity.get("identifier")) is not int or identity["identifier"] <= 0
            or type(identity.get("magic")) is not int or identity["magic"] < 0
            or type(identity.get("time")) is not int or identity["time"] <= 0
            or identity.get("direction") not in ("BUY", "SELL")
            or not isinstance(identity.get("symbol"), str) or not identity["symbol"]
            or not finite(identity.get("entry"), positive=True)
            or not finite(identity.get("volume"), positive=True)):
        raise ValueError("invalid guardian identity fields")


@dataclass
class ProfitFloor:
    rules: dict
    protected_sl: float
    confirmed_price: float
    desired_sl: float = 0.0
    atr_floor: float = 0.0
    peak_net: float = 0.0
    cost_cash: float = 0.0
    cash_per_price: float = 0.0
    known_entry_fees: float = 0.0
    fees_known: bool = False
    server_offset: float = None
    pending_price: float = field(default=0.0, repr=False)
    pending_since: float = field(default=0.0, repr=False)
    pending_last_at: float = field(default=0.0, repr=False)

    def __post_init__(self):
        validate_policy(self.rules)
        self.rules = copy.deepcopy(self.rules)
        for name in ("protected_sl", "confirmed_price", "desired_sl", "atr_floor",
                     "peak_net", "cost_cash", "cash_per_price", "known_entry_fees"):
            if not finite(getattr(self, name), positive=name == "confirmed_price"):
                raise ValueError("invalid saved profit guardian: " + name)
        if type(self.fees_known) is not bool:
            raise ValueError("invalid saved fee evidence")
        if self.server_offset is not None and (type(self.server_offset) not in (int,float)
                or not math.isfinite(self.server_offset) or not -12 <= self.server_offset <= 14):
            raise ValueError("invalid saved quote clock")

    def export(self):
        data = asdict(self)
        data.pop("pending_price")
        data.pop("pending_since")  # A restart must observe a new confirmation interval.
        data.pop("pending_last_at")
        return data

    def room(self, atr, spread, broker_gap):
        return max(self.rules["room_atr"] * max(self.atr_floor, atr),
                   self.rules["room_spread"] * spread, self.rules["room_abs"], broker_gap)

    def floor_room(self, risk, spread, broker_gap):
        """The explicit v3 cash-floor exception, never a replacement M5 ATR."""
        if self.rules["version"] != VERSION:
            return None
        return max(self.rules["floor_room_r"] * risk, self.rules["floor_room_abs"],
                   self.rules["room_spread"] * spread, broker_gap)

    @staticmethod
    def cash_floor_stop(direction, entry, amount, cash_per_price, costs, tick_size):
        sign = 1 if direction == "BUY" else -1
        raw = Decimal(str(entry)) + sign * ((Decimal(str(amount)) + Decimal(str(costs)))
                                           / Decimal(str(cash_per_price)))
        if tick_size is not None:
            if not finite(tick_size, positive=True):
                raise ValueError("invalid cash floor price grid")
            grid = Decimal(str(tick_size))
            raw = (raw / grid).to_integral_value(
                rounding=ROUND_CEILING if sign > 0 else ROUND_FLOOR) * grid
        return float(raw)

    def stepped_floor(self, *, direction, entry, risk, price, spread, broker_gap,
                      cash_per_price, costs, tick_size=None):
        """Only a sustained confirmed tier may authorize this narrower stop."""
        if self.rules["version"] != VERSION:
            return None
        if (direction not in ("BUY", "SELL") or not all(finite(x, positive=True)
                for x in (entry, risk, price, cash_per_price))
                or not all(finite(x) for x in (spread, broker_gap, costs))):
            raise ValueError("invalid cash floor facts")
        sign = 1 if direction == "BUY" else -1
        confirmed_net = sign * (self.confirmed_price - entry) * cash_per_price - costs
        step = None
        for threshold, amount in self.rules["cash_floors"]:
            if confirmed_net + 1e-10 >= threshold:
                step = (threshold, amount)
        if step is None:
            return None
        threshold, amount = step
        stop = self.cash_floor_stop(direction,entry,amount,cash_per_price,costs,tick_size)
        room = self.floor_room(risk, spread, broker_gap)
        if min(sign*(price-stop), sign*(self.confirmed_price-stop)) + 1e-10 < room:
            return None  # No clamping to manufacture a feasible floor after a pullback.
        return dict(stop=stop, trigger_net=threshold, floor_net=amount, minimum_room=room)

    def is_saved_stepped_stop(self, stop, **facts):
        step = self.stepped_floor(**facts)
        sign = 1 if facts["direction"] == "BUY" else -1
        return (step is not None and self.desired_sl > 0 and finite(stop, positive=True)
                and abs(step["stop"]-stop) < 1e-8
                and sign*(stop-self.desired_sl) <= 1e-8)

    def net_at_stop(self, direction, entry, stop):
        if (direction not in ("BUY", "SELL") or not finite(entry, positive=True)
                or not finite(stop, positive=True) or self.cash_per_price <= 0):
            return None
        sign = 1 if direction == "BUY" else -1
        return sign * (stop - entry) * self.cash_per_price - self.cost_cash

    def protection_details(self, direction, entry, initial_sl):
        """Estimated stages, never a broker execution receipt or realized P/L."""
        result = {}
        for name, stop in (("protected", self.protected_sl), ("desired", self.desired_sl)):
            net = self.net_at_stop(direction, entry, stop)
            if not finite(stop, positive=True) or net is None:
                phase = "unknown"
            elif not tighter(direction, initial_sl, stop):
                phase = "initial_risk"
            elif net + 1e-8 >= self.rules["min_net_lock"]:
                phase = "profit_lock"
            elif net >= -1e-8:
                phase = "breakeven"
            else:
                phase = "loss_reduction"
            result[name + "_phase"] = phase
            result[name + "_estimated_net"] = net
        return result

    def early_reduction_target(self, *, direction, entry, price, atr, spread,
                               broker_gap, cash_per_price, costs):
        """Bound early reduction by confirmed AND current quotes, up to net breakeven."""
        if self.rules["version"] not in (LOSS_REDUCTION_VERSION, VERSION):
            return None
        if (direction not in ("BUY", "SELL") or not all(finite(x, positive=True)
                for x in (entry, price, atr, cash_per_price))
                or not all(finite(x) for x in (spread, broker_gap, costs))):
            raise ValueError("invalid loss reduction quote or cash model")
        sign = 1 if direction == "BUY" else -1
        confirmed_net = sign * (self.confirmed_price - entry) * cash_per_price - costs
        live_net = sign * (price - entry) * cash_per_price - costs
        if min(confirmed_net, live_net) <= 1e-10:
            return None
        available = min(confirmed_net, live_net) - self.room(atr, spread, broker_gap) * cash_per_price
        return entry + sign * ((min(available, 0.) + costs) / cash_per_price)

    def observe(self, *, direction, entry, risk, price, atr, spread, broker_gap,
                cash_per_price, costs, now, closed_bar_proxy=False, tick_size=None):
        """Propose only from sustained new favorable quotes, never from falling ATR.

        Prices are executable close-side quotes. Costs exclude spread (already in
        entry/close prices). A desired stop is intent, not an acknowledged lock.
        """
        values = (entry, risk, price, atr, cash_per_price, now)
        if (direction not in ("BUY", "SELL") or not all(finite(x, positive=True) for x in values)
                or not all(finite(x) for x in (spread, broker_gap, costs))):
            raise ValueError("invalid profit quote or cash model")
        sign = 1 if direction == "BUY" else -1
        self.atr_floor = max(self.atr_floor, atr)
        self.cash_per_price, self.cost_cash = cash_per_price, costs
        # A previously sustained tier can become executable after spread or
        # broker distance normalizes. It never borrows an unconfirmed price.
        ready = self.stepped_floor(direction=direction,entry=entry,risk=risk,price=price,
            spread=spread,broker_gap=broker_gap,cash_per_price=cash_per_price,costs=costs,tick_size=tick_size)
        if (ready and tighter(direction,self.protected_sl,ready["stop"])
                and tighter(direction,self.desired_sl,ready["stop"])):
            self.desired_sl = ready["stop"]
        step = max(self.rules["advance_abs"], self.rules["advance_atr"] * self.atr_floor)
        confirmed_net = sign * (self.confirmed_price-entry) * cash_per_price-costs
        live_net = sign * (price-entry) * cash_per_price-costs
        crossing_floor = False
        if self.rules["version"] == VERSION:
            for threshold, amount in self.rules["cash_floors"]:
                stop = self.cash_floor_stop(direction,entry,amount,cash_per_price,costs,tick_size)
                executable_net = sign*(stop-entry)*cash_per_price-costs
                required_net = max(threshold,executable_net+self.floor_room(risk,spread,broker_gap)*cash_per_price)
                crossing_floor |= confirmed_net + 1e-10 < required_net <= live_net + 1e-10
        if sign * (price - self.confirmed_price) + 1e-10 < step and not crossing_floor:
            self.pending_price = self.pending_since = self.pending_last_at = 0.0
            return self.desired_sl or None
        if not closed_bar_proxy and (not self.pending_since or now < self.pending_since
                                     or now - self.pending_last_at > 2.0):
            self.pending_price, self.pending_since = price, now
            self.pending_last_at = now
            return self.desired_sl or None
        if closed_bar_proxy:
            self.pending_price = price
        # Use the least favorable quote of the sustained interval, not its spike.
        self.pending_price = min(self.pending_price, price) if sign > 0 else max(self.pending_price, price)
        self.pending_last_at = now
        if not closed_bar_proxy and now - self.pending_since < self.rules["confirm_seconds"]:
            return self.desired_sl or None
        self.confirmed_price = self.pending_price
        self.pending_price = self.pending_since = self.pending_last_at = 0.0
        net = sign * (self.confirmed_price - entry) * cash_per_price - costs
        self.peak_net = max(self.peak_net, net)
        if net <= 1e-10:
            return self.desired_sl or None
        r = net / (risk * cash_per_price)
        fraction = self.rules["stages"][0][1]
        for threshold, retained in self.rules["stages"]:
            if r >= threshold:
                fraction = retained
        available = sign * (price - entry) * cash_per_price - costs
        available -= self.room(atr, spread, broker_gap) * cash_per_price
        if self.rules["version"] in (LOSS_REDUCTION_VERSION, VERSION):
            # Even positive locks cannot borrow room from an unconfirmed spike.
            available = min(available, net - self.room(atr, spread, broker_gap) * cash_per_price)
        lock_cash = min(net * fraction, available)
        candidate = None
        if lock_cash + 1e-10 < self.rules["min_net_lock"]:
            candidate = self.early_reduction_target(
                direction=direction, entry=entry, price=price, atr=atr, spread=spread,
                broker_gap=broker_gap, cash_per_price=cash_per_price, costs=costs)
        else:
            candidate = entry + sign * ((lock_cash + costs) / cash_per_price)
        step = self.stepped_floor(direction=direction, entry=entry, risk=risk, price=price,
            spread=spread, broker_gap=broker_gap, cash_per_price=cash_per_price,
            costs=costs, tick_size=tick_size)
        if step and (candidate is None or tighter(direction, candidate, step["stop"])):
            candidate = step["stop"]
        if (candidate is not None and tighter(direction, self.protected_sl, candidate)
                and tighter(direction, self.desired_sl, candidate)):
            self.desired_sl = candidate
        return self.desired_sl or None

    def acknowledge(self, direction, stop):
        if tighter(direction, self.protected_sl, stop):
            self.protected_sl = stop


class GuardStore:
    """Atomic per-position snapshots. Caller serializes mutations with its MT5 lock."""
    def __init__(self, path):
        self.path = Path(path)
        self.records = {}
        self.error = ""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != STORE_VERSION
                    or not isinstance(data.get("positions"), dict)):
                raise ValueError("invalid guardian store")
            for ticket, record in data["positions"].items():
                if (not isinstance(record, dict) or not isinstance(record.get("identity"), dict)
                        or str(record["identity"].get("ticket")) != ticket):
                    raise ValueError("invalid guardian identity")
                identity = record["identity"]
                validate_identity(identity)
                ProfitFloor(**record["profit_floor"])
                if not all(finite(record.get(key), positive=True)
                           for key in ("entry_price", "risk", "initial_sl", "initial_tp", "highest_price", "lowest_price")):
                    raise ValueError("invalid guardian entry snapshot")
                sign = 1 if identity["direction"] == "BUY" else -1
                if (record["entry_price"] != identity["entry"]
                        or abs(sign*(record["entry_price"]-record["initial_sl"])-record["risk"]) > 1e-8
                        or not record["lowest_price"] <= record["entry_price"] <= record["highest_price"]):
                    raise ValueError("saved original risk or extrema are inconsistent")
            self.records = data["positions"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.error = str(exc)

    def flush(self):
        return self.put(None,None,force=True)

    def put(self, ticket, record, *, force=False):
        if self.error:
            return False  # Never overwrite an unreadable original contract.
        updated = copy.deepcopy(self.records)
        if record is None:
            updated.pop(str(ticket), None)
        else:
            updated[str(ticket)] = copy.deepcopy(record)
        if updated == self.records and not force:
            return True
        temp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", delete=False) as handle:
                temp = handle.name
                json.dump({"version": STORE_VERSION, "positions": updated}, handle, allow_nan=False,
                          ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            self.records = updated
            return True
        except (OSError, ValueError, TypeError):
            return False
        finally:
            if temp and os.path.exists(temp):
                try:
                    os.unlink(temp)
                except OSError:
                    pass
