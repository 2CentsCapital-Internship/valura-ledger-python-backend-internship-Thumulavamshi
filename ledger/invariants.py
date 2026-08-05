"""Book-level self-checks on our own arithmetic.

Deliberately NOT the systematic-defect hunt (build step 16). These are the
checks LOGIC.md section 11.3 tier 3 flags as "the ones to wire in permanently":
they catch our bugs, not the feed's, and they cost nothing.

Note what is absent, and why:

  * sum(2010) == 1100 is NOT here. "The sum of customer wallets does not equal
    omnibus cash, and any check you write assuming it does will be wrong on a
    correct book." The firm has money of its own.

  * wallet >= 0 and omnibus cash >= 0 are NOT here. Plausible, but the sheet
    never promises them, and a customer with a negative wallet is a business
    reality it does not forbid. Enforcing either would reject good data.
"""
from __future__ import annotations

from decimal import Decimal

from . import accounts as acct
from .money import ZERO, money
from .state import LedgerState


class Violation:
    def __init__(self, check: str, detail: str) -> None:
        self.check = check
        self.detail = detail

    def __repr__(self) -> str:
        return f"{self.check}: {self.detail}"


def trial_balance_sums_to_zero(state: LedgerState) -> list[Violation]:
    """True by construction if every posting balances, so this detects OUR
    bugs rather than the feed's. Worth asserting anyway -- it is 1% of the
    checkpoint score and free."""
    total = sum(state.balances.values(), ZERO)
    if money(total) != ZERO:
        return [Violation("trial_balance", f"sums to {money(total)}, not zero")]
    return []


def custody_matches_lot_book(state: LedgerState) -> list[Violation]:
    """1200 and 2100 carry the position AT COST, so both must mirror the lot
    book exactly. Divergence means a corporate action or a reversal failed to
    keep the accounts and the lots in step -- which is precisely the class of
    bug that "balances perfectly and quietly corrupts every subsequent cost
    basis"."""
    lot_total = money(sum(
        (lot.total_cost for lots in state.lots.values() for lot in lots), ZERO))

    violations = []
    custody = money(state.account_total(acct.OMNIBUS_CUSTODY))
    if custody != lot_total:
        violations.append(Violation(
            "custody_vs_lots",
            f"1200 is {custody} but lots total {lot_total}"))

    claim = money(-state.account_total(acct.SECURITIES_CLAIM))
    if claim != lot_total:
        violations.append(Violation(
            "claim_vs_lots",
            f"2100 is {claim} (credit-positive) but lots total {lot_total}"))
    return violations


def per_customer_claim_matches_lots(state: LedgerState) -> list[Violation]:
    """The per-customer version of the above, and more sensitive: it catches a
    cost relieved against the wrong customer, which nets out globally."""
    violations = []
    for customer_id in sorted(state.customers):
        lot_total = money(sum(
            (lot.total_cost for (cid, _sym), lots in state.lots.items()
             if cid == customer_id for lot in lots), ZERO))
        claim = money(state.credit_balance(customer_id, acct.SECURITIES_CLAIM))
        if claim != lot_total:
            violations.append(Violation(
                "customer_claim_vs_lots",
                f"{customer_id}: 2100 is {claim} but lots total {lot_total}"))
    return violations


def lots_are_sane(state: LedgerState) -> list[Violation]:
    """No negative quantity or cost. Catches FIFO relief walking past the end
    of a lot, and an oversell that mutated before rejecting."""
    violations = []
    for (customer_id, symbol), lots in sorted(state.lots.items()):
        for lot in lots:
            if lot.quantity < ZERO:
                violations.append(Violation(
                    "negative_lot_quantity",
                    f"{customer_id}/{symbol} lot {lot.seq} qty {lot.quantity}"))
            if lot.total_cost < ZERO:
                violations.append(Violation(
                    "negative_lot_cost",
                    f"{customer_id}/{symbol} lot {lot.seq} cost {lot.total_cost}"))
    return violations


def holds_are_sane(state: LedgerState) -> list[Violation]:
    """"A closed order always returns its hold to exactly zero", and a hold
    never goes negative."""
    violations = []
    for order in sorted(state.orders.values(), key=lambda o: o.order_id):
        if order.remaining_hold < ZERO:
            violations.append(Violation(
                "negative_hold",
                f"{order.order_id} hold {order.remaining_hold}"))
        if not order.is_open and order.remaining_hold != ZERO:
            violations.append(Violation(
                "closed_order_with_hold",
                f"{order.order_id} is {order.status} but holds "
                f"{order.remaining_hold}"))
    return violations


# ---------------------------------------------------------------------------
# TRIED AND REJECTED: "a liability must never carry a debit balance"
#
# Added after run 2's replay showed 2350 at +973.85, which turned out to be a
# real bug (settling a fill that had already been reversed, double-debiting the
# payable). The check found it.
#
# Then run 1's capture disproved the check itself. There, 2350 ends at +21,941
# through an entirely CORRECT sequence:
#
#     buy fill        Cr 2350 P     (we owe the broker)
#     trade_settled   Dr 2350 P     (paid; 2350 back to zero)
#     reversal        Dr 2350 P     (the exact inverse of the fill's legs)
#
# The spec says "post the exact inverse of the ORIGINAL's legs" and says
# nothing about the settlement, so the payable legitimately goes into debit:
# we paid the broker for a trade that was then reversed, and the broker owes it
# back. We agree with the reference on all 776 of run 1's events while this
# holds, so the balance is not a defect.
#
# Every payable can reach debit the same way (a reversal inverts Cr 2400,
# Cr 241x, Cr 2420, Cr 2430 too), so the check is unsound in general, not just
# for 2350. Shipping it would flag correct books and invite someone to "fix"
# real correctness to silence it.
#
# The bug it originally caught is now prevented directly and precisely, by
# rejecting trade_settled on a reversed fill in handlers/orders.py, with a unit
# test on that exact case. This note stays so the check is not re-added.
# ---------------------------------------------------------------------------


ALL_CHECKS = (
    trial_balance_sums_to_zero,
    custody_matches_lot_book,
    per_customer_claim_matches_lots,
    lots_are_sane,
    holds_are_sane,
)


def check_all(state: LedgerState) -> list[Violation]:
    violations: list[Violation] = []
    for check in ALL_CHECKS:
        violations.extend(check(state))
    return violations
