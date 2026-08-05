"""Book-level self-checks. These catch OUR bugs, not the feed's."""
import pytest

from conftest import buy_fill, deposit
from ledger.invariants import (check_all, custody_matches_lot_book,
                               holds_are_sane, lots_are_sane,
                               per_customer_claim_matches_lots,
                               trial_balance_sums_to_zero)
from ledger.models import CLOSED
from ledger.money import D, leg


class TestCleanBook:
    def test_a_correct_book_violates_nothing(self, engine):
        engine.apply(deposit(amount="1000.00"))
        engine.apply(buy_fill(principal="500.00", quantity="5"))
        engine.apply(buy_fill(principal="250.00", quantity="2",
                              customer_id="CUST-2002", order_id="ord_2"))
        assert check_all(engine.state) == []


class TestTrialBalance:
    def test_detects_an_out_of_balance_book(self, engine):
        engine.apply(deposit(amount="100.00"))
        engine.state.balances[("CUST-1001", "1100")] += D("0.01")
        assert trial_balance_sums_to_zero(engine.state)


class TestCustodyMirrorsLotBook:
    def test_clean_after_buys(self, engine):
        engine.apply(buy_fill(principal="1000.00", quantity="10"))
        engine.apply(buy_fill(principal="2500.00", quantity="20",
                              order_id="ord_2"))
        assert custody_matches_lot_book(engine.state) == []

    def test_detects_a_lot_left_behind_by_a_bad_reversal(self, engine):
        """The exact failure mode the sheet warns about: "a reversed buy whose
        lot you leave in place will balance perfectly and quietly corrupt every
        subsequent cost basis". The accounts unwind, the lot does not."""
        engine.apply(buy_fill(principal="1000.00", quantity="10"))
        # Unwind only the accounting half, as a naive reversal would.
        engine.state.post([
            leg("1200", "CUST-1001", credit="1000.00"),
            leg("2100", "CUST-1001", debit="1000.00"),
        ])
        violations = custody_matches_lot_book(engine.state)
        assert {v.check for v in violations} == {"custody_vs_lots", "claim_vs_lots"}

    def test_per_customer_check_catches_what_the_global_one_misses(self, engine):
        """Cost relieved against the wrong customer nets out globally."""
        engine.apply(buy_fill(customer_id="CUST-1001", principal="1000.00",
                              quantity="10"))
        engine.apply(buy_fill(customer_id="CUST-2002", principal="1000.00",
                              quantity="10", order_id="ord_2"))
        # Move a claim from one customer to the other without touching lots.
        engine.state.balances[("CUST-1001", "2100")] += D("100.00")
        engine.state.balances[("CUST-2002", "2100")] -= D("100.00")

        assert custody_matches_lot_book(engine.state) == []      # nets out
        assert len(per_customer_claim_matches_lots(engine.state)) == 2


class TestLotsAndHolds:
    def test_detects_a_negative_lot(self, engine):
        engine.apply(buy_fill(principal="1000.00", quantity="10"))
        engine.state.lots_for("CUST-1001", "ACME")[0].quantity = D("-1")
        assert lots_are_sane(engine.state)

    def test_detects_a_closed_order_still_holding_cash(self, engine):
        """"A closed order always returns its hold to exactly zero.\""""
        engine.apply(buy_fill(order_id="ord_9"))
        order = engine.state.orders["ord_9"]
        assert order.status == CLOSED
        order.remaining_hold = D("50.00")
        assert holds_are_sane(engine.state)


class TestDeliberatelyAbsentChecks:
    def test_wallets_are_not_required_to_equal_omnibus_cash(self, engine):
        """"The sum of customer wallets does not equal omnibus cash, and any
        check you write assuming it does will be wrong on a correct book."

        A buy fill makes them diverge by the firm's retained revenue. That is
        correct, and nothing here may flag it."""
        engine.apply(deposit(amount="100000.00"))
        engine.apply(buy_fill(principal="10000.00", quantity="100"))

        wallets = engine.state.credit_balance("CUST-1001", "2010")
        cash = engine.state.balance("CUST-1001", "1100")
        assert wallets != cash
        assert check_all(engine.state) == []

    def test_a_negative_wallet_is_not_flagged(self, engine):
        """Plausible as an invariant, never promised by the sheet. Enforcing it
        would reject good data."""
        engine.apply(buy_fill(principal="10000.00", quantity="100"))
        assert engine.state.credit_balance("CUST-1001", "2010") < D("0")
        assert check_all(engine.state) == []
