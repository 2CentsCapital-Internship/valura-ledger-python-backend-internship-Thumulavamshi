"""The state records. Plain mutable dataclasses -- no behaviour beyond
bookkeeping helpers, so that state.py stays the only place state changes.

See TECHNICAL_PLAN.md section 4.2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .money import ZERO, ZERO_QTY

OPEN, CLOSED = "OPEN", "CLOSED"
BUY, SELL = "buy", "sell"

REQUESTED, SETTLED, REJECTED = "REQUESTED", "SETTLED", "REJECTED"


@dataclass
class Lot:
    """One parcel of shares at a total cost.

    Deliberately carries a TOTAL, not a rate. There is no cost_per_share field
    and there must never be one: keeping a cost per share and multiplying it
    out is also FIFO, and it disagrees with the graded convention by a cent.
    See LOGIC.md section 8.4.
    """
    seq: int                    # monotonic delivery sequence -- the FIFO order
    quantity: Decimal
    total_cost: Decimal
    source_event_id: str        # so a reversal can find the lot it created
    # The quantity this lot was created with. Never mutated. Its only purpose
    # is to let a reversal tell "untouched" from "partially consumed", which is
    # the A-5 divergence case -- the one place our handling is a guess.
    original_quantity: Decimal = ZERO_QTY


@dataclass
class Order:
    order_id: str
    customer_id: str
    side: str
    symbol: str
    quantity: Decimal
    limit_price: Decimal
    asset_class: str
    est_charges: Decimal
    route: str | None = None            # computed at placement, never changes
    status: str = OPEN
    filled_quantity: Decimal = ZERO_QTY
    initial_hold: Decimal = ZERO
    remaining_hold: Decimal = ZERO
    placement_seen: bool = False        # False when known only from a fill
    fill_broker: str | None = None      # fallback route source (A-10)
    # Each accepted fill's quantity, in arrival order. The hold release is
    # computed per fill and rounded independently (A-4), so the individual
    # quantities are needed -- a running total rounds once and lands a cent
    # away. Kept as a list rather than a running release so the whole thing is
    # recomputable from recorded state, which keeps replay exact.
    fill_quantities: list[Decimal] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status == OPEN

    @property
    def reported_route(self) -> str | None:
        """The route to publish in open_order_routes.

        Prefer the route computed from the placement. If a fill arrived before
        its placement we have no limit price and cannot compute one, so fall
        back to the broker the fill named (A-10).
        """
        return self.route or self.fill_broker


@dataclass
class Fill:
    """One fill, keyed by trade_id. Partial fills each have their own, so
    settlement is per fill and not per order."""
    trade_id: str
    order_id: str
    customer_id: str
    side: str
    symbol: str
    quantity: Decimal
    price: Decimal
    principal: Decimal
    broker: str
    partner_rate: Decimal
    asset_class: str
    event_id: str
    settled: bool = False
    reversed_: bool = False
    # Sells only: which lots this fill drew from, so a reversal can put them
    # back. (lot_seq, quantity_taken, cost_relieved)
    consumption: list[tuple[int, Decimal, Decimal]] = field(default_factory=list)


@dataclass
class Withdrawal:
    withdrawal_id: str
    customer_id: str
    amount: Decimal
    status: str = REQUESTED


@dataclass
class FeeCharge:
    """A fee_charged event, indexed so fee_refund can look its amount up."""
    event_id: str
    customer_id: str
    amount: Decimal
    refunded: bool = False
