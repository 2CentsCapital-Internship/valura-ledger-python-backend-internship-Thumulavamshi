"""The broker tariff: the six-part fee chain, and order routing.

Pure functions over Decimal. No state, no I/O -- which is deliberate, because
this is where a large share of the score is won or lost and it needs to be
exhaustively unit-testable.

See LOGIC.md section 3.1 (the tariff), 3.2 (the fee chain) and 3.3 (routing).

Two derived facts worth keeping in view while reading:

  * The flat ticket fee is INSIDE broker cost, and therefore inside the `cost`
    term of the partner share. This is provable from the sheet's own claim that
    "the ticket fee makes roughly a quarter of all fills loss-making" -- remove
    the ticket from cost and no broker's fills are ever loss-making at any
    principal, so the claim would be false. (LOGIC.md section 3.2, A-1/A-2.)

  * The minimum fee is load-bearing in ROUTING, not just in charging. All three
    asset classes have a real crossover notional driven by the floor, so a
    bps-only comparison is wrong on every small order. (LOGIC.md section 3.3.)
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .accounts import broker_payable
from .money import D, ZERO, money

BPS = D(10000)

# Every fill, every broker, charged to the customer and owed onward to the
# venue. Not the firm's income, and the firm does not add to it.
REG_FEE_BPS = D(8)
REG_FEE_RATE = REG_FEE_BPS / BPS

EQUITY, ETF, BOND = "equity", "etf", "bond"
ASSET_CLASSES = frozenset({EQUITY, ETF, BOND})


@dataclass(frozen=True)
class Broker:
    broker_id: str
    trades: frozenset[str]
    brokerage_bps: Decimal      # charged to the customer -> firm revenue
    custody_bps: Decimal        # charged to the customer -> firm revenue
    broker_cost_bps: Decimal    # charged to the firm by the executing broker
    custody_cost_bps: Decimal   # charged to the firm by the custodian
    min_fee: Decimal            # floors the BROKERAGE charge only
    ticket: Decimal             # flat, per fill, whatever its size

    @property
    def brokerage_rate(self) -> Decimal:
        return self.brokerage_bps / BPS

    @property
    def custody_rate(self) -> Decimal:
        return self.custody_bps / BPS

    @property
    def broker_cost_rate(self) -> Decimal:
        return self.broker_cost_bps / BPS

    @property
    def custody_cost_rate(self) -> Decimal:
        return self.custody_cost_bps / BPS


BROKERS: dict[str, Broker] = {
    "BRK-A": Broker("BRK-A", frozenset({EQUITY, ETF}),
                    D(20), D(4), D(9), D(2), D("1.00"), D("0.35")),
    "BRK-B": Broker("BRK-B", frozenset({EQUITY, BOND}),
                    D(15), D(5), D(8), D(3), D("2.50"), D("3.00")),
    "BRK-C": Broker("BRK-C", frozenset({ETF, BOND}),
                    D(25), D(3), D(12), D(1), D("0.50"), D("0.20")),
}


class UnknownBroker(KeyError):
    pass


def get_broker(broker_id: str) -> Broker:
    try:
        return BROKERS[broker_id]
    except KeyError:
        raise UnknownBroker(f"unknown broker {broker_id!r}") from None


# ---------------------------------------------------------------------------
# The individual charges. Each is rounded to the cent independently, half away
# from zero, BEFORE it is used in anything downstream -- including before it is
# summed into the wallet debit and before it feeds the partner-share margin.
# ---------------------------------------------------------------------------

def brokerage(principal: Decimal, broker: Broker) -> Decimal:
    """Charged to the customer. Firm revenue. FLOORED at the broker minimum.

    max(round(x), min_fee) and round(max(x, min_fee)) agree in every case here,
    because min_fee is already exactly 2dp. We round first, consistent with
    "each derived amount is rounded to the cent independently before use".
    """
    return max(money(principal * broker.brokerage_rate), broker.min_fee)


def custody(principal: Decimal, broker: Broker) -> Decimal:
    """Charged to the customer. Firm revenue. NOT floored -- the minimum fee
    applies to the brokerage charge only.

    Can round to 0.00 on a small enough principal: at 3bps (BRK-C) anything
    under 16.67. See LOGIC.md A-9b.
    """
    return money(principal * broker.custody_rate)


def regulatory(principal: Decimal) -> Decimal:
    """8 bps, every fill, every broker. Charged to the customer, owed onward.

    Zero below 6.25 principal. See LOGIC.md A-9b.
    """
    return money(principal * REG_FEE_RATE)


def broker_cost(principal: Decimal, broker: Broker) -> Decimal:
    """Charged to the FIRM by the executing broker. Includes the flat ticket.

    Never zero, because the ticket (>= 0.20) is always added.
    """
    return money(principal * broker.broker_cost_rate) + broker.ticket


def custody_cost(principal: Decimal, broker: Broker) -> Decimal:
    """Charged to the FIRM by the custodian.

    The widest zero-rounding exposure in the whole fee chain: at 1bp (BRK-C)
    anything under 50.00 principal rounds to 0.00. See LOGIC.md A-9b.
    """
    return money(principal * broker.custody_cost_rate)


def partner_share(revenue: Decimal, cost: Decimal, partner_rate: Decimal) -> Decimal:
    """partner_rate x (revenue - cost), clamped at zero.

    "Where cost exceeds revenue the share is zero; there is no clawback."

    revenue = brokerage + custody (the regulatory fee is excluded: it is not
    the firm's income). cost = broker cost + custody cost, ticket included.

    Both terms are already exact at 2dp, so the margin is exact and only the
    multiplication by partner_rate needs rounding -- which is precisely where
    a rate of 0.50 on an odd-cent margin lands on a half cent.
    """
    margin = revenue - cost
    if margin <= ZERO:
        return ZERO
    return money(partner_rate * margin)


@dataclass(frozen=True)
class Fees:
    """The six derived amounts for one fill, each already rounded."""
    brokerage: Decimal          # b  -> Cr 4000, charged to customer
    custody: Decimal            # c  -> Cr 4010, charged to customer
    regulatory: Decimal         # r  -> Cr 2400, charged to customer
    broker_cost: Decimal        # bc -> Dr 5000 / Cr 241x
    custody_cost: Decimal       # cc -> Dr 5010 / Cr 2420
    partner_share: Decimal      # ps -> Dr 5100 / Cr 2430
    broker_id: str

    @property
    def customer_charges(self) -> Decimal:
        """b + c + r -- what the customer pays on top of (or out of) principal."""
        return self.brokerage + self.custody + self.regulatory

    @property
    def revenue(self) -> Decimal:
        """What the firm earned. Excludes the regulatory fee."""
        return self.brokerage + self.custody

    @property
    def cost(self) -> Decimal:
        """What the broker and custodian charged the firm. Ticket included."""
        return self.broker_cost + self.custody_cost

    @property
    def margin(self) -> Decimal:
        return self.revenue - self.cost

    @property
    def payable_account(self) -> str:
        return broker_payable(self.broker_id)


def fee_chain(principal: Decimal, broker_id: str, partner_rate: Decimal) -> Fees:
    """The whole chain for one fill. Identical for buys and sells.

    "The firm's revenue, cost, regulatory and partner economics are identical
    to a buy."
    """
    broker = get_broker(broker_id)
    b = brokerage(principal, broker)
    c = custody(principal, broker)
    r = regulatory(principal)
    bc = broker_cost(principal, broker)
    cc = custody_cost(principal, broker)
    ps = partner_share(b + c, bc + cc, partner_rate)
    return Fees(b, c, r, bc, cc, ps, broker_id)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def customer_charge(notional: Decimal, broker: Broker) -> Decimal:
    """Total customer charge used for the routing comparison: brokerage +
    custody, each rounded to the cent independently first (A-8).

    The minimum fee is included, and that is what makes routing non-trivial.
    """
    return brokerage(notional, broker) + custody(notional, broker)


def candidates(asset_class: str) -> list[Broker]:
    """Brokers that trade this asset class, in broker-id ascending order.

    No broker covers all three classes, so this is always exactly two brokers
    for a valid asset class.
    """
    return [BROKERS[k] for k in sorted(BROKERS)
            if asset_class in BROKERS[k].trades]


def route(asset_class: str, notional: Decimal) -> str:
    """The broker the routing rule sends an order to.

    "Route to the broker with the lowest total customer charge (brokerage +
    custody) for quantity x limit_price, among brokers that trade that asset
    class. Ties break on broker id ascending, so there is always exactly one
    right answer."

    Reported at every checkpoint via open_order_routes (8% of the checkpoint
    score). Never used for the fee chain -- fills name their own broker.
    """
    options = candidates(asset_class)
    if not options:
        raise ValueError(f"no broker trades asset class {asset_class!r}")
    # sorted() is stable and `options` is already broker-id ascending, so a
    # tie resolves to the lowest broker id without a secondary sort key.
    return min(options, key=lambda b: (customer_charge(notional, b), b.broker_id)).broker_id
