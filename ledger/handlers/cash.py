"""Cash and FX events. See LOGIC.md section 2 and section 6.

Payload shapes below are confirmed against the practice run 1 capture, not
inferred. Two things that capture settled:

  * interest_credited.customer_share is an AMOUNT, not a rate (A-3). 41
    samples, ratios to gross spanning 0.43-0.83, every value greater than 1,
    none exceeding its gross.

  * fx_deposit rates are quoted FOREIGN PER USD, so amount_foreign is DIVIDED
    by the rate: 8758.00 / 84.0267 = 104.23. A higher rate therefore means
    fewer dollars. This is why the rejection rule compares the two usd_at_*
    figures and never the raw rates -- on raw rates the comparison inverts.
"""
from __future__ import annotations

import logging

from .. import accounts as acct
from ..engine import Rejected
from ..models import REJECTED, REQUESTED, SETTLED, FeeCharge, Withdrawal
from ..money import ZERO, dec, leg, money

log = logging.getLogger(__name__)


def _amount(payload: dict, field: str, label: str):
    """Parse a money field, rejecting rather than crashing on junk.

    The stream deliberately sends unparseable payloads: all four fee_charged
    events the reference rejected in run 1 carried amount="not-a-number".
    """
    try:
        return money(dec(payload[field]))
    except Exception:
        raise Rejected(f"{label}: unparseable {field}") from None


def _require(payload: dict, fields, label: str) -> None:
    missing = [f for f in fields if payload.get(f) is None]
    if missing:
        raise Rejected(f"{label} missing {','.join(missing)}")


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------

def on_deposit(state, p: dict, event: dict) -> list[dict]:
    """deposit -- customer_id, amount.  [GIVEN by the task sheet]

    "Cash arrives at the broker; the firm owes the customer that much more."

        Dr 1100 amount        Cr 2010 amount
    """
    _require(p, ("customer_id", "amount"), "deposit")
    customer_id = p["customer_id"]
    amount = _amount(p, "amount", "deposit")
    if amount <= ZERO:
        raise Rejected("deposit amount not positive")

    return [
        leg(acct.OMNIBUS_CASH, customer_id, debit=amount),
        leg(acct.CUSTOMER_WALLET, customer_id, credit=amount),
    ]


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------

def on_fee_charged(state, p: dict, event: dict) -> list[dict]:
    """fee_charged -- customer_id, amount.

    "The customer pays the firm's fee out of their wallet; the cash leaves the
    omnibus account."

        Dr 2010 amount        Cr 1100 amount

    Note what does NOT move: no income account. Both halves are stated
    verbatim, and they are only consistent if this is the firm sweeping the fee
    OUT of the omnibus rather than recognising it as revenue inside this book.
    Booking it to 4000 is contradicted by "the cash leaves the omnibus account"
    -- retained revenue would leave the cash where it is.
    """
    _require(p, ("customer_id", "amount"), "fee_charged")
    customer_id = p["customer_id"]
    amount = _amount(p, "amount", "fee_charged")
    if amount <= ZERO:
        raise Rejected("fee_charged amount not positive")

    # Indexed so fee_refund can look the amount up: it is not in that payload.
    state.fees[event["event_id"]] = FeeCharge(
        event_id=event["event_id"], customer_id=customer_id, amount=amount)

    return [
        leg(acct.CUSTOMER_WALLET, customer_id, debit=amount),
        leg(acct.OMNIBUS_CASH, customer_id, credit=amount),
    ]


def on_fee_refund(state, p: dict, event: dict) -> list[dict]:
    """fee_refund -- refunds_source_id, customer_id. NO amount in the payload.

    "Undoes a fee charged earlier, in full. The amount is the amount of the
    fee_charged event being refunded. Refunding the same fee twice is an error."

        Dr 1100 amount        Cr 2010 amount

    The exact inverse of fee_charged.
    """
    _require(p, ("refunds_source_id",), "fee_refund")
    source_id = p["refunds_source_id"]

    original = state.fees.get(source_id)
    if original is None:
        # "A reversal of an event you never received -> record it as rejected,
        # carry on." The same rule for any unresolvable back-reference. Run 1
        # contained no forward references, so this path is untested by data.
        raise Rejected("fee_refund of an unknown fee_charged")

    if original.refunded:
        raise Rejected("fee_refund of an already-refunded fee")

    # The amount comes from the original, so post against the original's
    # customer. A disagreement is a defect signal, not a reason to split them.
    if p.get("customer_id") and p["customer_id"] != original.customer_id:
        log.warning("INVARIANT fee_refund customer %s != original %s (%s)",
                    p["customer_id"], original.customer_id, event["event_id"])

    original.refunded = True
    customer_id = original.customer_id
    return [
        leg(acct.OMNIBUS_CASH, customer_id, debit=original.amount),
        leg(acct.CUSTOMER_WALLET, customer_id, credit=original.amount),
    ]


# ---------------------------------------------------------------------------
# Withdrawals
# ---------------------------------------------------------------------------

def on_withdrawal_requested(state, p: dict, event: dict) -> list[dict]:
    """withdrawal_requested -- withdrawal_id, customer_id, amount.

    "The money has left the customer's wallet but has not yet left the broker.
    It is no longer owed to the customer as wallet money; it is owed to them as
    a withdrawal being processed. Those are different obligations."

        Dr 2010 amount        Cr 2300 amount

    One liability reclassified into another. NO asset moves -- 2300 exists
    precisely to hold this intermediate state.
    """
    _require(p, ("withdrawal_id", "customer_id", "amount"), "withdrawal_requested")
    withdrawal_id = p["withdrawal_id"]
    customer_id = p["customer_id"]
    amount = _amount(p, "amount", "withdrawal_requested")
    if amount <= ZERO:
        raise Rejected("withdrawal amount not positive")

    if withdrawal_id in state.withdrawals:
        raise Rejected("withdrawal_requested with a duplicate withdrawal_id")

    state.withdrawals[withdrawal_id] = Withdrawal(
        withdrawal_id=withdrawal_id, customer_id=customer_id, amount=amount)

    return [
        leg(acct.CUSTOMER_WALLET, customer_id, debit=amount),
        leg(acct.WITHDRAWALS_IN_TRANSIT, customer_id, credit=amount),
    ]


def _resolve_withdrawal(state, p: dict, label: str) -> Withdrawal:
    _require(p, ("withdrawal_id",), label)
    withdrawal = state.withdrawals.get(p["withdrawal_id"])
    if withdrawal is None:
        raise Rejected(f"{label} for an unknown withdrawal_id")
    if withdrawal.status != REQUESTED:
        raise Rejected(f"{label} for a withdrawal already {withdrawal.status}")
    return withdrawal


def on_withdrawal_settled(state, p: dict, event: dict) -> list[dict]:
    """withdrawal_settled -- withdrawal_id only.

    "The cash actually leaves. Look the amount up from the request."

        Dr 2300 amount        Cr 1100 amount
    """
    withdrawal = _resolve_withdrawal(state, p, "withdrawal_settled")
    withdrawal.status = SETTLED
    return [
        leg(acct.WITHDRAWALS_IN_TRANSIT, withdrawal.customer_id,
            debit=withdrawal.amount),
        leg(acct.OMNIBUS_CASH, withdrawal.customer_id, credit=withdrawal.amount),
    ]


def on_withdrawal_rejected(state, p: dict, event: dict) -> list[dict]:
    """withdrawal_rejected -- withdrawal_id only.

    "The withdrawal fails and the money is owed to the customer as wallet money
    again. No cash moved at any point."

        Dr 2300 amount        Cr 2010 amount

    "No cash moved at any point" is an explicit instruction not to touch 1100:
    the trap is posting a cash round-trip that never happened.

    NOTE the naming collision -- this is a BUSINESS outcome that PRODUCES LEGS,
    not one of our own rejections.
    """
    withdrawal = _resolve_withdrawal(state, p, "withdrawal_rejected")
    withdrawal.status = REJECTED
    return [
        leg(acct.WITHDRAWALS_IN_TRANSIT, withdrawal.customer_id,
            debit=withdrawal.amount),
        leg(acct.CUSTOMER_WALLET, withdrawal.customer_id,
            credit=withdrawal.amount),
    ]


# ---------------------------------------------------------------------------
# Interest and transfers
# ---------------------------------------------------------------------------

def on_interest_credited(state, p: dict, event: dict) -> list[dict]:
    """interest_credited -- customer_id, gross_amount, customer_share.

    "The broker pays interest on the omnibus balance. The customer is credited
    their share; the firm keeps the remainder as income. This is not a
    pass-through."

        Dr 1100 gross         Cr 2010 customer_share
                              Cr 4200 gross - customer_share

    A-3 CONFIRMED (run 1): customer_share is an AMOUNT, not a rate.

    The firm's share is computed as the RESIDUAL of the two given amounts, so
    it absorbs any cent and the transaction balances exactly. Computing it
    independently and hoping three rounded numbers agree does not.
    """
    _require(p, ("customer_id", "gross_amount", "customer_share"),
             "interest_credited")
    customer_id = p["customer_id"]
    gross = _amount(p, "gross_amount", "interest_credited")
    share = _amount(p, "customer_share", "interest_credited")

    if gross <= ZERO:
        raise Rejected("interest_credited gross not positive")
    if share > gross:
        # Defect candidate: the firm's share would be negative. Never occurred
        # in run 1. Flag, do not reject -- detect broadly, reject narrowly.
        log.warning("INVARIANT interest customer_share %s > gross %s (%s)",
                    share, gross, event["event_id"])

    firm_share = gross - share
    # A zero firm share drops out centrally via money.drop_zero_legs.
    return [
        leg(acct.OMNIBUS_CASH, customer_id, debit=gross),
        leg(acct.CUSTOMER_WALLET, customer_id, credit=share),
        leg(acct.INTEREST_INCOME, customer_id, credit=firm_share),
    ]


def on_transfer_between_customers(state, p: dict, event: dict) -> list[dict]:
    """transfer_between_customers -- from_customer_id, to_customer_id, amount.

    "One customer pays another. No external cash moves, and the firm's total
    obligation is unchanged: only whose money it is changes."

        Dr 2010 amount  (from_customer_id)
                              Cr 2010 amount  (to_customer_id)

    THE CANARY FOR PER-CUSTOMER KEYING. Both legs land on 2010, so the ACCOUNT
    nets to zero. A book keyed by account rather than by (customer, account)
    posts this, balances perfectly, shows no net change, and is silently wrong
    about both customers' wallets at every checkpoint afterwards.
    """
    _require(p, ("from_customer_id", "to_customer_id", "amount"),
             "transfer_between_customers")
    sender = p["from_customer_id"]
    recipient = p["to_customer_id"]
    amount = _amount(p, "amount", "transfer_between_customers")

    if amount <= ZERO:
        raise Rejected("transfer amount not positive")
    if sender == recipient:
        # Nets to nothing. Defect candidate; never occurred in run 1.
        log.warning("INVARIANT transfer from == to (%s)", event["event_id"])

    state.note_customer(sender)
    state.note_customer(recipient)
    return [
        leg(acct.CUSTOMER_WALLET, sender, debit=amount),
        leg(acct.CUSTOMER_WALLET, recipient, credit=amount),
    ]


# ---------------------------------------------------------------------------
# Foreign currency
# ---------------------------------------------------------------------------

def on_fx_deposit(state, p: dict, event: dict) -> list[dict]:
    """fx_deposit -- customer_id, amount_foreign, currency, market_rate,
    customer_rate, usd_at_market_rate, usd_at_customer_rate.

    "The omnibus account receives the market value. The customer is credited at
    their rate, which is worse; the gap is the firm's FX spread, earned now."

        Dr 1100 usd_at_market_rate    Cr 2010 usd_at_customer_rate
                                      Cr 4100 the difference

    REJECTION: "An fx_deposit whose customer rate is BETTER than the market
    rate is rejected. A negative spread is bad data, not a gift."

    Compare the two usd_at_* figures, never the raw rates. Run 1 confirmed the
    rates are quoted FOREIGN PER USD (8758.00 / 84.0267 = 104.23), so a higher
    customer_rate means FEWER dollars -- the raw-rate comparison is inverted
    from the intuitive reading. The customer is better off when they receive
    MORE dollars, and that is unambiguous.
    """
    _require(p, ("customer_id", "usd_at_market_rate", "usd_at_customer_rate"),
             "fx_deposit")
    customer_id = p["customer_id"]
    usd_market = _amount(p, "usd_at_market_rate", "fx_deposit")
    usd_customer = _amount(p, "usd_at_customer_rate", "fx_deposit")

    if usd_market <= ZERO or usd_customer <= ZERO:
        raise Rejected("fx_deposit non-positive USD amount")

    if usd_customer > usd_market:
        # A-14 TERRITORY. Never observed in ~3,100 events across four practice
        # runs, so this rejection has never been confirmed against the
        # reference. Deliberately loud so that a first occurrence during a
        # scored, feedback-free attempt is at least visible in the log.
        # Grep A14-FIRST.
        log.warning(
            "A14-FIRST fx_deposit with a NEGATIVE spread, rejecting: "
            "event=%s customer=%s usd_at_market=%s usd_at_customer=%s "
            "spread=%s -- the spec says to reject this ('a negative spread is "
            "bad data, not a gift') but no capture has ever contained one, so "
            "the rule is untested",
            event.get("event_id"), customer_id, usd_market, usd_customer,
            usd_market - usd_customer)
        raise Rejected("fx_deposit with a negative spread")

    if usd_customer == usd_market:
        # Also A-14: we ACCEPT a zero spread, because the rule says "better
        # than", not "not worse than". Equally untested.
        log.warning(
            "A14-FIRST fx_deposit with EXACTLY ZERO spread, accepting: "
            "event=%s customer=%s usd=%s -- the rule rejects only a customer "
            "rate BETTER than market, so this posts with the 4100 leg dropped "
            "as zero. Untested against the reference",
            event.get("event_id"), customer_id, usd_market)

    # Spread as the difference of the two GIVEN figures, so the three legs
    # balance exactly. Deriving it from amount_foreign x (rate delta) can
    # differ by a cent -- and where it does, that is a defect signal rather
    # than a number to post.
    spread = usd_market - usd_customer
    return [
        leg(acct.OMNIBUS_CASH, customer_id, debit=usd_market),
        leg(acct.CUSTOMER_WALLET, customer_id, credit=usd_customer),
        leg(acct.FX_SPREAD_REVENUE, customer_id, credit=spread),
    ]
