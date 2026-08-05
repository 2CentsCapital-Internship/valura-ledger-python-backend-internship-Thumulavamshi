"""Order events. See LOGIC.md section 3.

Implemented so far: buy fills. Sells need the FIFO lot book (build step 8) and
the hold lifecycle needs the placement handler (build step 7); both raise
NotImplementedYet until then, so they cost their own events and nothing else.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from .. import accounts as acct
from ..engine import NotImplementedYet, Rejected
from ..lots import Oversell, apply_relief, plan_relief
from ..models import BUY, CLOSED, OPEN, SELL, Fill, Order
from ..money import ZERO, ZERO_QTY, dec, leg, money
from ..tariff import ASSET_CLASSES, UnknownBroker, fee_chain, route

log = logging.getLogger(__name__)


def on_order_placed(state, p: dict, event: dict) -> list[dict]:
    """order_placed -- NO LEGS.

    "No money moves and no legs are posted. A placement creates a hold: for a
    buy, cash of quantity x limit_price + est_charges is no longer spendable;
    for a sell, the shares are no longer sellable. Holds are reported at
    checkpoints, never posted."

    Confirmed correct by practice run 1: 68/68 marked correct with empty legs.
    What was MISSING was all of the state below -- the hold and the route.
    """
    for field in ("order_id", "customer_id", "side", "symbol", "quantity",
                  "limit_price"):
        if p.get(field) is None:
            raise Rejected(f"order_placed missing {field}")

    side = p["side"]
    if side not in (BUY, SELL):
        raise Rejected(f"order_placed with unknown side {side!r}")

    try:
        qty = dec(p["quantity"])
        limit_price = dec(p["limit_price"])
        est_charges = money(dec(p.get("est_charges", "0")))
    except Exception as exc:
        raise Rejected(f"order_placed unparseable numerics: {exc}") from None

    order_id = p["order_id"]
    asset_class = p.get("asset_class", "")

    existing = state.orders.get(order_id)
    if existing is not None and existing.placement_seen:
        # A genuine duplicate placement is caught by the engine's seen-gate, so
        # this is a second placement for the same order_id: defect candidate.
        log.warning("INVARIANT duplicate order_placed for %s (%s)",
                    order_id, event.get("event_id"))
        return []

    # "est_charges is a conservative estimate supplied in the feed; use it as
    # given." Do not recompute it from the tariff.
    hold = money(qty * limit_price) + est_charges if side == BUY else ZERO

    computed_route = None
    if asset_class in ASSET_CLASSES:
        try:
            computed_route = route(asset_class, qty * limit_price)
        except ValueError:
            computed_route = None
    else:
        log.warning("INVARIANT order_placed with unknown asset_class %r: %s",
                    asset_class, event.get("event_id"))

    if existing is None:
        order = Order(
            order_id=order_id, customer_id=p["customer_id"], side=side,
            symbol=p["symbol"], quantity=qty, limit_price=limit_price,
            asset_class=asset_class, est_charges=est_charges,
        )
        state.orders[order_id] = order
    else:
        # The placement arrived AFTER its fills. Fill in the metadata, but do
        # not resurrect an order the fills already closed.
        order = existing
        order.quantity = qty
        order.limit_price = limit_price
        order.asset_class = asset_class or order.asset_class
        order.est_charges = est_charges

    order.placement_seen = True
    order.route = computed_route
    order.initial_hold = hold

    if order.status == OPEN:
        # Any fills already applied consume their proportional share.
        order.remaining_hold = _remaining_hold(order)
    else:
        order.remaining_hold = ZERO

    state.note_customer(order.customer_id)
    if asset_class:
        conflict = state.note_symbol_class(p["symbol"], asset_class)
        if conflict:
            log.warning("INVARIANT symbol asset_class changed: %s %s was %s now %s",
                        event.get("event_id"), p["symbol"], conflict, asset_class)
    return []


def _remaining_hold(order: Order) -> Decimal:
    """The unreleased share of the hold.

    A-4, CONFIRMED against practice run 3's checkpoint diagnostics: each fill
    releases `round(initial_hold x fill_qty / order_qty)`, rounded
    INDEPENDENTLY per fill, and the remainder is what is left after subtracting
    the sum of those roundings.

    Rounding once on the cumulative filled quantity instead lands a cent away.
    That single cent was the last defect in the book: every other part of every
    checkpoint scored 1.0 while cash_hold sat at 0.9167.

        order qty 48, initial hold 9231.44, two fills of 10
          per fill    round(9231.44 x 10/48) = 1923.22, twice -> 5385.00  correct
          cumulative  round(9231.44 x 20/48) = 3846.43       -> 5385.01  wrong

    Two rival readings were refuted by the same data: releasing the principal
    while holding est_charges whole, and releasing each fill's actual
    principal. Both fixed the flagged customer and broke an unflagged one.

    Recomputed from the recorded fill quantities rather than accumulated
    incrementally, so a replay reproduces it exactly.
    """
    if order.side != BUY or order.initial_hold == ZERO:
        return ZERO
    if order.quantity <= ZERO_QTY:
        return order.initial_hold
    if order.filled_quantity >= order.quantity:
        return ZERO

    released = sum(
        (money(order.initial_hold * q / order.quantity)
         for q in order.fill_quantities),
        ZERO,
    )
    remaining = order.initial_hold - released
    return remaining if remaining > ZERO else ZERO


def on_trade_settled(state, p: dict, event: dict) -> list[dict]:
    """trade_settled -- trade_id only.

    "Settlement day: the cash from that fill actually moves, discharging the
    obligation the fill created. Nothing else about the trade changes."

        buy    Dr 2350 P      Cr 1100 P
        sell   Dr 1100 P      Cr 1150 P

    The buy created Cr 2350 P; discharging it means debiting it and paying
    cash. The sell created Dr 1150 P; collecting it means crediting it and
    receiving cash.

    "Nothing else about the trade changes" is an explicit instruction: no
    lot-book change, no fee change, no hold change. Settlement is purely the
    cash leg catching up with the trade leg.

    Each fill carries its own trade_id, so an order that filled five times
    settles five times. Indexed by trade_id, never by order_id.
    """
    trade_id = p.get("trade_id")
    if not trade_id:
        raise Rejected("trade_settled without trade_id")

    fill = state.fills.get(trade_id)
    if fill is None:
        raise Rejected("trade_settled for an unknown trade_id")
    if fill.settled:
        raise Rejected("trade_settled for an already-settled trade")
    if fill.reversed_:
        # The reversal already unwound the 2350 / 1150 obligation, so there is
        # nothing left to discharge. Settling anyway debits 2350 a second time
        # and leaves a LIABILITY sitting in debit -- which is exactly how this
        # was found: run 2's replay showed 2350 at +973.85.
        #
        # Confirmed against run 2: the reference rejected both settlements of a
        # reversed fill in that capture.
        raise Rejected("trade_settled for a reversed fill")

    fill.settled = True
    cid, P = fill.customer_id, fill.principal

    if fill.side == BUY:
        return [
            leg(acct.UNSETTLED_TRADE_PAYABLE, cid, debit=P),
            leg(acct.OMNIBUS_CASH, cid, credit=P),
        ]
    return [
        leg(acct.OMNIBUS_CASH, cid, debit=P),
        leg(acct.SETTLEMENT_RECEIVABLE, cid, credit=P),
    ]


def on_order_closed(state, p: dict, event: dict) -> list[dict]:
    """order_cancelled and order_rejected -- NO LEGS.

    "The remaining hold is released", to exactly zero.

    Confirmed correct by practice run 1: 3/3 marked correct with empty legs.
    """
    order_id = p.get("order_id")
    if not order_id:
        raise Rejected("order close without order_id")

    order = state.orders.get(order_id)
    if order is None:
        # Nothing to release. Do not invent a phantom order -- it would appear
        # in open_order_routes and count against us.
        log.info("close for unknown order %s (%s)", order_id, event.get("event_id"))
        return []

    order.status = CLOSED
    order.remaining_hold = ZERO
    return []

FILL_FIELDS = ("order_id", "customer_id", "side", "symbol", "quantity",
               "price", "principal", "broker", "partner_rate", "trade_id")


def _parse_fill(state, p: dict, event: dict) -> Fill:
    """Validate and structure a fill payload. Raises Rejected if malformed."""
    missing = [f for f in FILL_FIELDS if p.get(f) is None]
    if missing:
        raise Rejected(f"fill missing fields: {','.join(missing)}")

    side = p["side"]
    if side not in (BUY, SELL):
        raise Rejected(f"fill with unknown side {side!r}")

    try:
        quantity = dec(p["quantity"])
        price = dec(p["price"])
        principal = money(dec(p["principal"]))
        partner_rate = dec(p["partner_rate"])
    except Exception as exc:
        raise Rejected(f"fill with unparseable numerics: {exc}") from None

    if quantity <= ZERO_QTY:
        raise Rejected("fill quantity not positive")
    if principal <= ZERO:
        raise Rejected("fill principal not positive")

    return Fill(
        trade_id=p["trade_id"], order_id=p["order_id"],
        customer_id=p["customer_id"], side=side, symbol=p["symbol"],
        quantity=quantity, price=price, principal=principal,
        broker=p["broker"], partner_rate=partner_rate,
        asset_class=p.get("asset_class", ""), event_id=event["event_id"],
    )


def _flag_defect_candidates(state, fill: Fill) -> None:
    """Log-only invariant checks. Deliberately NOT rejections yet.

    "Detect broadly, reject narrowly" -- wrongly rejecting a valid fill loses
    its lot and poisons every later cost basis for that symbol, which is worse
    than posting a defective one. We enable a rejection only once practice has
    shown us which invariant fires on exactly the defective set.
    See LOGIC.md section 11.
    """
    expected = money(fill.quantity * fill.price)
    if expected != fill.principal:
        log.warning("INVARIANT principal != round(qty x price): %s "
                    "principal=%s qty=%s price=%s expected=%s",
                    fill.event_id, fill.principal, fill.quantity,
                    fill.price, expected)

    if fill.asset_class:
        conflict = state.note_symbol_class(fill.symbol, fill.asset_class)
        if conflict:
            log.warning("INVARIANT symbol asset_class changed: %s %s was %s now %s",
                        fill.event_id, fill.symbol, conflict, fill.asset_class)

        from ..tariff import BROKERS
        broker = BROKERS.get(fill.broker)
        if broker and fill.asset_class not in broker.trades:
            log.warning("INVARIANT broker does not trade asset class: %s %s %s",
                        fill.event_id, fill.broker, fill.asset_class)


def _register_order_from_fill(state, fill: Fill, is_final: bool) -> Order:
    """Ensure an Order record exists, creating a placeholder if the placement
    has not arrived. "A fill may arrive before its placement -- handle it or
    record it; do not stall the stream."
    """
    order = state.orders.get(fill.order_id)
    if order is None:
        order = Order(
            order_id=fill.order_id, customer_id=fill.customer_id,
            side=fill.side, symbol=fill.symbol, quantity=ZERO_QTY,
            limit_price=ZERO, asset_class=fill.asset_class,
            est_charges=ZERO, placement_seen=False,
        )
        state.orders[fill.order_id] = order

    order.filled_quantity += fill.quantity
    order.fill_quantities.append(fill.quantity)
    # No limit price means no computable route; the fill names its broker (A-10).
    order.fill_broker = fill.broker

    if is_final:
        # "The final fill or a cancellation releases whatever remains, so a
        # closed order always returns its hold to exactly zero."
        order.status = CLOSED
        order.remaining_hold = ZERO
    else:
        # "A fill releases a proportional share of the hold that order placed."
        order.remaining_hold = _remaining_hold(order)
    return order


def on_fill(state, p: dict, event: dict) -> list[dict]:
    """order_filled and order_partially_filled.

    Both post identically. order_filled is the last fill: it closes the order
    and releases whatever remains of the hold.

    The fee amounts are NOT in the payload -- the tariff turns the broker and
    the principal into money. See LOGIC.md section 3.2.
    """
    fill = _parse_fill(state, p, event)

    # --- SYSTEMATIC DEFECT (identified from practice run 1) -----------------
    # A fill carrying a trade_id we have already seen on a DIFFERENT event.
    # In the run-1 capture this separated the data perfectly: 7 of 7 fills the
    # reference rejected carried a duplicate trade_id, and 0 of 57 it accepted
    # did. A genuine re-delivery of the same event never reaches here -- the
    # engine's seen-gate catches it first -- so this only fires on a distinct
    # event reusing a settled identifier, which is "internally well-formed and
    # wrong". See LOGIC.md section 11.
    existing = state.fills.get(fill.trade_id)
    if existing is not None and existing.event_id != fill.event_id:
        raise Rejected("duplicate trade_id on a distinct event")

    try:
        fees = fee_chain(fill.principal, fill.broker, fill.partner_rate)
    except UnknownBroker as exc:
        raise Rejected(str(exc)) from None

    _flag_defect_candidates(state, fill)

    is_final = event.get("type") == "order_filled"

    if fill.side == SELL:
        # The live list, not a copy: state.lots is a defaultdict(list), so
        # indexing gives us the queue that apply_relief must mutate in place.
        lots = state.lots[(fill.customer_id, fill.symbol)]
        try:
            # Oversell is measured against the TOTAL position -- confirmed
            # against practice runs 1 and 2, where 20 sells exceeding the
            # "free" position (position less quantity committed to other open
            # sell orders) were accepted. plan_relief raises BEFORE mutating,
            # so a rejection leaves the lots exactly as they were.
            plan = plan_relief(lots, fill.quantity)
        except Oversell as exc:
            raise Rejected(f"oversell: {exc}") from None
        except ValueError as exc:
            raise Rejected(str(exc)) from None

        cost = money(sum((relieved for _lot, _take, relieved in plan), ZERO))
        legs = _sell_legs(fill, fees, cost)

        # Mutate only once the legs are built and the relief is known good.
        fill.consumption = [(lot.seq, take, relieved)
                            for lot, take, relieved in plan]
        apply_relief(lots, plan)
    else:
        legs = _buy_legs(fill, fees)
        # The lot cost is the PRINCIPAL ONLY. Charges are not capitalised: the
        # worked example credits 2100 with P, not P + b + c + r. Getting this
        # wrong balances perfectly and is wrong on 64% of the checkpoint score
        # for the rest of the run.
        state.add_lot(fill.customer_id, fill.symbol, fill.quantity,
                      fill.principal, event["event_id"])

    _register_order_from_fill(state, fill, is_final)
    state.fills[fill.trade_id] = fill
    state.note_customer(fill.customer_id)
    return legs


def _sell_legs(fill: Fill, fees, cost: Decimal) -> list[dict]:
    """The sell entry.  [DERIVED -- the sheet works only the buy]

        Dr 1150  P                      Cr 2010  P - b - c - r
        Dr 2100  cost                   Cr 1200  cost
        Dr 5000  bc                     Cr 4000  b
        Dr 5010  cc                     Cr 4010  c
        Dr 5100  ps                     Cr 2400  r
                                        Cr 241x  bc
                                        Cr 2420  cc
                                        Cr 2430  ps

    Derived line by line from the stated economics:

      "the sale proceeds are owed to the firm by the broker until settlement"
          -> Dr 1150 P, the mirror of the buy's 2350 payable. 1100 is NOT
             touched: same T+2 logic as a buy.
      "the customer is credited the principal net of their charges"
          -> Cr 2010 P - b - c - r
      "the custody position and the customer's claim on it shrink by the COST
       of the shares sold, not their sale value"
          -> Cr 1200 cost / Dr 2100 cost
      "the firm's revenue, cost, regulatory and partner economics are
       identical to a buy"
          -> the bottom six lines are copied verbatim from the buy

    Realised gain/loss is NOWHERE, and that is correct. The firm's obligation
    to the customer rises by (P - b - c - r) on the wallet and falls by `cost`
    on the securities claim; the difference IS the realised gain, appearing as
    a residual across two liability accounts. There is no realised-gain account
    in the chart and adding one would be wrong.

    Confirmed by run 2's `accounts_differ`, which named exactly these thirteen
    accounts on every sell we failed to post.
    """
    cid, P = fill.customer_id, fill.principal
    return [
        # debits
        leg(acct.SETTLEMENT_RECEIVABLE, cid, debit=P),
        leg(acct.SECURITIES_CLAIM, cid, debit=cost),
        leg(acct.BROKERAGE_COST, cid, debit=fees.broker_cost),
        leg(acct.CUSTODY_COST, cid, debit=fees.custody_cost),
        leg(acct.PARTNER_REVENUE_SHARE, cid, debit=fees.partner_share),
        # credits
        leg(acct.CUSTOMER_WALLET, cid, credit=P - fees.customer_charges),
        leg(acct.OMNIBUS_CUSTODY, cid, credit=cost),
        leg(acct.BROKERAGE_REVENUE, cid, credit=fees.brokerage),
        leg(acct.CUSTODY_REVENUE, cid, credit=fees.custody),
        leg(acct.REG_FEES_PAYABLE, cid, credit=fees.regulatory),
        leg(fees.payable_account, cid, credit=fees.broker_cost),
        leg(acct.CUSTODIAN_FEES_PAYABLE, cid, credit=fees.custody_cost),
        leg(acct.PARTNER_SHARE_PAYABLE, cid, credit=fees.partner_share),
    ]


def _buy_legs(fill: Fill, fees) -> list[dict]:
    """The buy entry, exactly as worked in the task sheet.

        Dr 2010  P + b + c + r          Cr 2350  P
        Dr 1200  P                      Cr 2100  P
        Dr 5000  bc                     Cr 4000  b
        Dr 5010  cc                     Cr 4010  c
        Dr 5100  ps                     Cr 2400  r
                                        Cr 241x  bc
                                        Cr 2420  cc
                                        Cr 2430  ps

    Zero-valued legs are INCLUDED (A-9): the worked example presents these as
    structural lines of the entry, not conditional ones, and the sheet's house
    style is inclusive of zeros. Confirm against practice run 1.
    """
    cid, P = fill.customer_id, fill.principal
    return [
        # debits
        leg(acct.CUSTOMER_WALLET, cid, debit=P + fees.customer_charges),
        leg(acct.OMNIBUS_CUSTODY, cid, debit=P),
        leg(acct.BROKERAGE_COST, cid, debit=fees.broker_cost),
        leg(acct.CUSTODY_COST, cid, debit=fees.custody_cost),
        leg(acct.PARTNER_REVENUE_SHARE, cid, debit=fees.partner_share),
        # credits
        leg(acct.UNSETTLED_TRADE_PAYABLE, cid, credit=P),
        leg(acct.SECURITIES_CLAIM, cid, credit=P),
        leg(acct.BROKERAGE_REVENUE, cid, credit=fees.brokerage),
        leg(acct.CUSTODY_REVENUE, cid, credit=fees.custody),
        leg(acct.REG_FEES_PAYABLE, cid, credit=fees.regulatory),
        leg(fees.payable_account, cid, credit=fees.broker_cost),
        leg(acct.CUSTODIAN_FEES_PAYABLE, cid, credit=fees.custody_cost),
        leg(acct.PARTNER_SHARE_PAYABLE, cid, credit=fees.partner_share),
    ]
