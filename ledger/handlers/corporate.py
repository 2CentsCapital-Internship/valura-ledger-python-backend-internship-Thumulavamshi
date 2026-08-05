"""Corporate actions. See LOGIC.md section 5.

Implemented here: stock_split and symbol_change, both NO-LEG events with real
state effects. Practice run 1 marked both correct (20/20 and 11/11) on legs
alone, because we submitted the empty list -- but the state effects were not
happening at all, which silently corrupts every position and cost basis after
them. That is exactly why the sheet says skipping corporate actions costs three
times what skipping reversals does.

dividend_cash and dividend_reinvested are build step 12 and still raise.

"Every corporate action here is delivered PER CUSTOMER: it carries a
customer_id and affects only that customer's holding. The same underlying
action reaches each affected customer as its own event, so a rename or a split
for one customer says nothing about anyone else's position in that symbol."

So: never propagate a split or a rename to other holders of the symbol. Wait
for each customer's own event.
"""
from __future__ import annotations

import logging

from .. import accounts as acct
from ..engine import Rejected
from ..lots import apply_split, merge_in_delivery_order
from ..money import ZERO_QTY, dec, leg, money

log = logging.getLogger(__name__)


def _require(payload: dict, fields, label: str) -> None:
    missing = [f for f in fields if payload.get(f) is None]
    if missing:
        raise Rejected(f"{label} missing {','.join(missing)}")


def _amount(payload: dict, field: str, label: str):
    try:
        return money(dec(payload[field]))
    except Exception:
        raise Rejected(f"{label}: unparseable {field}") from None


def on_dividend_cash(state, p: dict, event: dict) -> list[dict]:
    """dividend_cash -- customer_id, symbol, gross_amount, withholding_tax,
    net_amount.

    "Tax was withheld AT SOURCE: only the net ever reaches the firm, and the
    firm owes the tax to nobody. The net is the customer's money."

        Dr 1100 net           Cr 2010 net

    gross_amount and withholding_tax are INFORMATIONAL. They appear in the
    payload precisely to tempt us into raising a tax payable; there is no tax
    account in the chart and "the firm owes the tax to nobody" forbids one.
    """
    _require(p, ("customer_id", "net_amount"), "dividend_cash")
    customer_id = p["customer_id"]
    net = _amount(p, "net_amount", "dividend_cash")
    if net <= ZERO_QTY:
        raise Rejected("dividend_cash net not positive")

    _flag_tax_arithmetic(p, event)
    state.note_customer(customer_id)
    return [
        leg(acct.OMNIBUS_CASH, customer_id, debit=net),
        leg(acct.CUSTOMER_WALLET, customer_id, credit=net),
    ]


def on_dividend_reinvested(state, p: dict, event: dict) -> list[dict]:
    """dividend_reinvested -- as dividend_cash plus reinvest_price,
    reinvest_quantity.

    "The broker reinvests the net directly. CASH IS NEVER INVOLVED: the
    customer's holding grows by a new lot of reinvest_quantity whose cost is
    the net amount."

        Dr 1200 net           Cr 2100 net      + a lot of reinvest_quantity
                                                 at cost = net_amount

    "Cash is never involved" rules out 1100 and 2010 completely -- that is the
    discriminator against the obvious-but-wrong "credit the wallet, then buy".

    A-20: the lot's cost is net_amount, full stop, even where
    reinvest_quantity x reinvest_price disagrees. (Run 2: 0 of 17 disagreed.)
    """
    _require(p, ("customer_id", "symbol", "net_amount", "reinvest_quantity"),
             "dividend_reinvested")
    customer_id, symbol = p["customer_id"], p["symbol"]
    net = _amount(p, "net_amount", "dividend_reinvested")

    try:
        quantity = dec(p["reinvest_quantity"])
    except Exception as exc:
        raise Rejected(f"dividend_reinvested bad quantity: {exc}") from None

    if net <= ZERO_QTY:
        raise Rejected("dividend_reinvested net not positive")
    if quantity <= ZERO_QTY:
        raise Rejected("dividend_reinvested quantity not positive")

    _flag_tax_arithmetic(p, event)
    _flag_reinvest_arithmetic(p, net, quantity, event)

    state.add_lot(customer_id, symbol, quantity, net, event["event_id"])
    state.note_customer(customer_id)
    return [
        leg(acct.OMNIBUS_CUSTODY, customer_id, debit=net),
        leg(acct.SECURITIES_CLAIM, customer_id, credit=net),
    ]


def _flag_tax_arithmetic(p: dict, event: dict) -> None:
    try:
        gross = dec(p.get("gross_amount", "0"))
        tax = dec(p.get("withholding_tax", "0"))
        net = dec(p["net_amount"])
    except Exception:
        return
    if gross and money(gross - tax) != money(net):
        log.warning("INVARIANT dividend gross - withholding != net: %s "
                    "(%s - %s != %s)", event.get("event_id"), gross, tax, net)


def _flag_reinvest_arithmetic(p: dict, net, quantity, event: dict) -> None:
    try:
        price = dec(p["reinvest_price"])
    except Exception:
        return
    if money(quantity * price) != money(net):
        log.warning("INVARIANT reinvest qty x price != net: %s "
                    "(%s x %s != %s)", event.get("event_id"), quantity,
                    price, net)


def on_stock_split(state, p: dict, event: dict) -> list[dict]:
    """stock_split -- customer_id, symbol, ratio_from, ratio_to. NO LEGS.

    "Quantity scales by ratio_to / ratio_from; the total cost of each lot is
    unchanged, so cost per share moves."

    No legs because 1200 and 2100 both carry the position AT COST, and total
    cost does not change. Nothing of value moves. This is the clearest case of
    "no legs" and "no effect" being different things: it changes the position
    and the cost-per-share of every future sale, and posts nothing.
    """
    for field in ("customer_id", "symbol", "ratio_from", "ratio_to"):
        if p.get(field) is None:
            raise Rejected(f"stock_split missing {field}")

    try:
        ratio_from = dec(p["ratio_from"])
        ratio_to = dec(p["ratio_to"])
    except Exception as exc:
        raise Rejected(f"stock_split unparseable ratio: {exc}") from None

    if ratio_from <= ZERO_QTY or ratio_to <= ZERO_QTY:
        raise Rejected(f"stock_split bad ratio {ratio_from} -> {ratio_to}")

    customer_id, symbol = p["customer_id"], p["symbol"]
    lots = state.lots_for(customer_id, symbol)
    state.note_customer(customer_id)

    if not lots:
        # Nothing held. Still a no-leg event, and still correct.
        return []

    # Recorded so a reversal can restore the exact pre-split quantities:
    # dividing back by ratio_to/ratio_from is lossy at 6dp (LOGIC.md 13.4).
    before = apply_split(lots, ratio_from, ratio_to)
    state.split_history[event["event_id"]] = {
        "customer_id": customer_id, "symbol": symbol, "quantities": before,
    }
    return []


def on_symbol_change(state, p: dict, event: dict) -> list[dict]:
    """symbol_change -- customer_id, old_symbol, new_symbol. NO LEGS.

    "Re-key the holding."
    """
    for field in ("customer_id", "old_symbol", "new_symbol"):
        if p.get(field) is None:
            raise Rejected(f"symbol_change missing {field}")

    customer_id = p["customer_id"]
    old_symbol, new_symbol = p["old_symbol"], p["new_symbol"]
    state.note_customer(customer_id)

    if old_symbol == new_symbol:
        return []

    old_key = (customer_id, old_symbol)
    incoming = state.lots.pop(old_key, None)
    if not incoming:
        return []

    new_key = (customer_id, new_symbol)
    existing = state.lots.get(new_key)
    if existing:
        # Merge by delivery sequence, not by appending: "FIFO means delivery
        # order", and seq is exactly that (A-11).
        state.lots[new_key] = merge_in_delivery_order(existing, incoming)
    else:
        state.lots[new_key] = incoming

    # Carry the asset class across, or the "one class per symbol" invariant
    # will false-positive on every later event for the new symbol.
    if old_symbol in state.symbol_class:
        state.symbol_class.setdefault(new_symbol, state.symbol_class[old_symbol])
    return []
