"""LedgerState: every piece of mutable state, and the only place it changes.

Two properties this file exists to guarantee:

  * Balances are keyed by (customer_id, account), NEVER by account alone.
    transfer_between_customers puts both legs on 2010, so an account-keyed
    book shows nothing happening and is wrong at every checkpoint afterwards.
    The four settlement events cannot be computed at all without per-customer
    balances -- their amount is "whatever has accumulated on that account for
    that customer".

  * The state is a pure fold over the event log. Nothing here reads a clock,
    a random source, or anything outside (state, event). That is what makes
    as-of replay and the chaos-replay idempotency test work.

See LOGIC.md section 1.3 and TECHNICAL_PLAN.md section 4.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from . import accounts
from .models import Fill, FeeCharge, Lot, Order, Withdrawal
from .money import ZERO, ZERO_QTY, dec, money


class LedgerState:
    def __init__(self) -> None:
        # (customer_id, account) -> debit-positive balance
        self.balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        # "Report every account you have ever posted to, including any that
        # have netted back to zero."
        self.accounts_seen: set[str] = set()
        self.customers: set[str] = set()

        # (customer_id, symbol) -> lots in delivery order
        self.lots: dict[tuple[str, str], list[Lot]] = defaultdict(list)
        self.next_lot_seq: int = 0

        self.orders: dict[str, Order] = {}
        self.fills: dict[str, Fill] = {}            # keyed by trade_id
        self.withdrawals: dict[str, Withdrawal] = {}
        self.fees: dict[str, FeeCharge] = {}        # keyed by fee_charged event_id

        # "Every symbol belongs to one asset class for the whole run" -- handed
        # to us as an invariant, and one of the strongest defect candidates.
        self.symbol_class: dict[str, str] = {}

        # Every leg we ever posted, so a reversal can invert the exact amounts
        # rather than recomputing them.
        self.postings: dict[str, list[dict]] = {}
        self.reversed_event_ids: set[str] = set()

        # split event_id -> the pre-split lot quantities, so a reversal can
        # restore them verbatim. Dividing back by ratio_to/ratio_from is lossy
        # at 6dp and not exactly invertible (LOGIC.md 13.4).
        self.split_history: dict[str, dict] = {}

    # -- posting ------------------------------------------------------------
    def post(self, legs: list[dict]) -> None:
        """Apply balanced legs to the balances. Caller has already checked
        that debits equal credits."""
        for l in legs:
            cid, acct = l["customer_id"], l["account"]
            self.balances[(cid, acct)] += dec(l["debit"]) - dec(l["credit"])
            self.accounts_seen.add(acct)
            self.customers.add(cid)

    def balance(self, customer_id: str, account: str) -> Decimal:
        """Debit-positive balance for one (customer, account)."""
        return self.balances.get((customer_id, account), ZERO)

    def credit_balance(self, customer_id: str, account: str) -> Decimal:
        """Credit-positive balance -- the natural sign for a liability.

        This is what the four settlement events pay out, and what wallet_cash
        reports.
        """
        return -self.balance(customer_id, account)

    def account_total(self, account: str) -> Decimal:
        """Sum across customers, for the trial balance."""
        return sum((bal for (_cid, acct), bal in self.balances.items()
                    if acct == account), ZERO)

    # -- lot book -----------------------------------------------------------
    def add_lot(self, customer_id: str, symbol: str, quantity: Decimal,
                total_cost: Decimal, source_event_id: str) -> Lot:
        """Append a lot at the TAIL of the delivery-order queue.

        Created by buy fills (cost = principal) and dividend_reinvested
        (cost = net amount). Nothing else.
        """
        lot = Lot(seq=self.next_lot_seq, quantity=dec(quantity),
                  total_cost=money(total_cost), source_event_id=source_event_id,
                  original_quantity=dec(quantity))
        self.next_lot_seq += 1
        self.lots[(customer_id, symbol)].append(lot)
        return lot

    def lots_for(self, customer_id: str, symbol: str) -> list[Lot]:
        return self.lots.get((customer_id, symbol), [])

    def position_quantity(self, customer_id: str, symbol: str) -> Decimal:
        return sum((l.quantity for l in self.lots_for(customer_id, symbol)),
                   ZERO_QTY)

    def position_cost(self, customer_id: str, symbol: str) -> Decimal:
        """The reported cost_basis: the sum of lot TOTALS. 64% of the
        checkpoint score."""
        return money(sum((l.total_cost for l in self.lots_for(customer_id, symbol)),
                         ZERO))

    def symbols_held(self, customer_id: str) -> list[str]:
        """Symbols with a non-zero position.

        Zero-quantity positions are omitted from the checkpoint: "reporting a
        position that should not exist counts against you as well" (A-12).
        """
        return sorted(
            sym for (cid, sym), lots in self.lots.items()
            if cid == customer_id and sum((l.quantity for l in lots), ZERO_QTY) != ZERO_QTY
        )

    # -- orders -------------------------------------------------------------
    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.is_open]

    def cash_hold(self, customer_id: str) -> Decimal:
        """Total unreleased cash hold across this customer's open BUY orders.

        Sell placements hold shares, not cash, and the checkpoint has no field
        for a share hold (A-6).
        """
        from .models import BUY
        return money(sum(
            (o.remaining_hold for o in self.orders.values()
             if o.customer_id == customer_id and o.is_open and o.side == BUY),
            ZERO,
        ))

    # -- reference registration --------------------------------------------
    def note_customer(self, customer_id: str) -> None:
        """Register a customer we have seen even if nothing posted for them."""
        if customer_id:
            self.customers.add(customer_id)

    def note_symbol_class(self, symbol: str, asset_class: str) -> str | None:
        """Record a symbol's asset class; return the previous one if it
        conflicts. A conflict is a strong systematic-defect signal."""
        if not symbol or not asset_class:
            return None
        previous = self.symbol_class.get(symbol)
        if previous is None:
            self.symbol_class[symbol] = asset_class
            return None
        return previous if previous != asset_class else None
