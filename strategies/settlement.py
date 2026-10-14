"""Pure broker-history accounting; never infer a realized result from a quote."""
from dataclasses import dataclass
import math

VERSION = "close-settlement-v1"


class SettlementError(ValueError):
    pass


@dataclass(frozen=True)
class PositionSettlement:
    position_id: int
    deal_ids: tuple
    profit: float
    commission: float
    swap: float
    fee: float
    volume: float
    price_open: float
    price_close: float
    reason: int
    time: int

    @property
    def net_profit(self):
        return math.fsum((self.profit, self.commission, self.swap, self.fee))


def settle_position_deals(deals, position_id, symbol, direction):
    """Require a complete single-direction lifetime, including partial fills/exits.

    DEAL_ENTRY_IN/OUT/INOUT/OUT_BY are 0/1/2/3; DEAL_TYPE_BUY/SELL are 0/1.
    Reversals cannot safely be attributed to a single original entry snapshot.
    """
    if position_id <= 0 or direction not in ("BUY", "SELL"):
        raise SettlementError("invalid position identity")
    rows = {}
    for deal in deals or ():
        if int(getattr(deal, "position_id", 0) or 0) != position_id or getattr(deal, "symbol", None) != symbol:
            continue
        if int(getattr(deal, "type", -1)) not in (0, 1):
            continue
        ticket = int(deal.ticket)
        if ticket <= 0:
            raise SettlementError("invalid deal ticket")
        fields = {name: float(getattr(deal, name)) for name in
                  ("volume", "price", "profit", "commission", "swap", "fee")}
        if (not all(math.isfinite(value) for value in fields.values())
                or fields["volume"] <= 0 or fields["price"] <= 0):
            raise SettlementError("invalid monetary or fill data")
        fields.update(ticket=ticket, entry=int(deal.entry), type=int(deal.type),
                      reason=int(deal.reason), time=int(deal.time),
                      time_msc=int(getattr(deal, "time_msc", 0) or 0))
        if ticket in rows and fields != rows[ticket]:
            raise SettlementError("conflicting duplicate deal")
        rows[ticket] = fields
    values = list(rows.values())
    if any(row["entry"] not in (0, 1, 3) for row in values):
        raise SettlementError("netting reversal or unsupported entry")
    entries = [row for row in values if row["entry"] == 0]
    exits = [row for row in values if row["entry"] in (1, 3)]
    if not entries or not exits:
        raise SettlementError("opening or closing history not ready")
    side = 0 if direction == "BUY" else 1
    if any(row["type"] != side for row in entries) or any(row["type"] == side for row in exits):
        raise SettlementError("deal direction differs from original position")
    opened = math.fsum(row["volume"] for row in entries)
    closed = math.fsum(row["volume"] for row in exits)
    if not math.isclose(opened, closed, rel_tol=0., abs_tol=1e-8):
        raise SettlementError("position history is not fully settled")
    latest = max(exits, key=lambda row: (row["time_msc"], row["time"], row["ticket"]))
    return PositionSettlement(
        position_id, tuple(sorted(rows)),
        *(math.fsum(row[field] for row in values) for field in ("profit", "commission", "swap", "fee")),
        opened, math.fsum(row["price"]*row["volume"] for row in entries)/opened,
        math.fsum(row["price"]*row["volume"] for row in exits)/closed,
        latest["reason"], latest["time"])
