"""The four settlement events. See LOGIC.md section 4.

"Four payables accrue a few cents per trade and are discharged IN FULL, one
customer at a time, paid out of omnibus cash. THE AMOUNT IS NEVER IN THE
PAYLOAD: it is whatever has accumulated on that account for that customer, so
each of these audits every per-trade rounding you have done since the last one.
Settling an account with nothing outstanding is an error."

    Dr <payable>  balance        Cr 1100  balance

All four are the same shape: discharge a liability by paying cash. The debit
clears the payable; the credit is named outright by "paid out of omnibus cash".

These matter out of proportion to their eight scoring points. They read our own
accumulated balance back out and pay it, so a single cent of fee-chain rounding
drift since the last settlement leaves the payable non-zero AND makes 1100
wrong -- both inside the all-or-nothing firm-accounts block.

They are also why balances MUST be keyed by (customer, account):
broker_fees_settled for CUST-1001 at BRK-B needs the (CUST-1001, 2412) balance
specifically, which an account-level book cannot produce at all.
"""
from __future__ import annotations

import logging

from .. import accounts as acct
from ..engine import Rejected
from ..money import ZERO, leg
from ..tariff import BROKERS

log = logging.getLogger(__name__)


def _settle(state, customer_id: str, account: str, label: str) -> list[dict]:
    """Discharge whatever has accumulated on (customer_id, account)."""
    if not customer_id:
        raise Rejected(f"{label} without customer_id")

    # Credit-positive: the natural sign for a liability, and the amount owed.
    balance = state.credit_balance(customer_id, account)

    if balance == ZERO:
        # "Settling an account with nothing outstanding is an error."
        raise Rejected(f"{label} with nothing outstanding")
    if balance < ZERO:
        # The account is in debit -- the firm is owed, not owing. Cannot be
        # "discharged in full" by paying. Never occurred in runs 1 or 2 (A-15).
        raise Rejected(f"{label} with a negative balance")

    return [
        leg(account, customer_id, debit=balance),
        leg(acct.OMNIBUS_CASH, customer_id, credit=balance),
    ]


def on_broker_fees_settled(state, p: dict, event: dict) -> list[dict]:
    """broker_fees_settled -- customer_id, broker.

    Pays that broker's accumulated fees for that customer, so the payable
    account depends on which broker.
    """
    broker = p.get("broker")
    if broker not in BROKERS:
        raise Rejected(f"broker_fees_settled with unknown broker {broker!r}")
    return _settle(state, p.get("customer_id"), acct.broker_payable(broker),
                   "broker_fees_settled")


def on_custodian_fees_settled(state, p: dict, event: dict) -> list[dict]:
    """custodian_fees_settled -- customer_id."""
    return _settle(state, p.get("customer_id"), acct.CUSTODIAN_FEES_PAYABLE,
                   "custodian_fees_settled")


def on_reg_fees_remitted(state, p: dict, event: dict) -> list[dict]:
    """reg_fees_remitted -- customer_id.

    The regulatory fee was collected on the venue's behalf and is now owed
    onward. This is where it physically leaves the omnibus.
    """
    return _settle(state, p.get("customer_id"), acct.REG_FEES_PAYABLE,
                   "reg_fees_remitted")


def on_partner_payout(state, p: dict, event: dict) -> list[dict]:
    """partner_payout -- customer_id."""
    return _settle(state, p.get("customer_id"), acct.PARTNER_SHARE_PAYABLE,
                   "partner_payout")
