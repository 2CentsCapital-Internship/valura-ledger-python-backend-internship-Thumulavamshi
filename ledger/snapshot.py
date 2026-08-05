"""Checkpoint serialization. See LOGIC.md section 14.

"A checkpoint reports your state as at the checkpoint's offset, not as at the
moment you reply. The grace period is for the network, not for processing
further events first."

So the caller must snapshot BEFORE any network round trip. client.py does.
"""
from __future__ import annotations

from . import accounts as acct
from .money import money_str, quantity_str
from .state import LedgerState


def snapshot(state: LedgerState) -> dict:
    """The full checkpoint body, minus checkpoint_id which the caller adds."""
    return {
        "trial_balance": _trial_balance(state),
        "customers": _customers(state),
        "open_order_routes": _open_order_routes(state),
    }


def _trial_balance(state: LedgerState) -> dict[str, str]:
    """Per account, summed across customers, DEBIT-POSITIVE.

    "Report every account you have ever posted to, including any that have
    netted back to zero" -- so we key off accounts_seen, not off which
    balances happen to be non-zero.
    """
    totals: dict[str, str] = {}
    for account in sorted(state.accounts_seen):
        totals[account] = money_str(state.account_total(account))
    return totals


def _customers(state: LedgerState) -> dict[str, dict]:
    """Every customer we have ever seen, even if their state is all zero (A-13).

    Note the deliberate asymmetry with positions: zero-value customers are
    included, zero-quantity positions are not. The sheet says to report
    accounts that netted to zero, but also that "reporting a position that
    should not exist counts against you as well".
    """
    out: dict[str, dict] = {}
    for customer_id in sorted(state.customers):
        out[customer_id] = {
            # 2010 is a liability, so wallet_cash is reported credit-positive:
            # the sign is flipped relative to the trial balance.
            "wallet_cash": money_str(state.credit_balance(customer_id,
                                                          acct.CUSTOMER_WALLET)),
            "cash_hold": money_str(state.cash_hold(customer_id)),
            "positions": _positions(state, customer_id),
        }
    return out


def _positions(state: LedgerState, customer_id: str) -> dict[str, dict]:
    """Per symbol: quantity and total cost basis. Zero-quantity symbols are
    omitted entirely (A-12)."""
    positions: dict[str, dict] = {}
    for symbol in state.symbols_held(customer_id):
        positions[symbol] = {
            "quantity": quantity_str(state.position_quantity(customer_id, symbol)),
            "cost_basis": money_str(state.position_cost(customer_id, symbol)),
        }
    return positions


def _open_order_routes(state: LedgerState) -> dict[str, str]:
    """order_id -> broker, for orders we believe are still open.

    "Orders you have seen filled or cancelled do not belong here."
    """
    routes: dict[str, str] = {}
    for order in sorted(state.open_orders(), key=lambda o: o.order_id):
        route = order.reported_route
        if route:
            routes[order.order_id] = route
    return routes
