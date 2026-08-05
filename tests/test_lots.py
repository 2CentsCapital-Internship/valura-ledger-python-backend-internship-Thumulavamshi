"""The FIFO lot book. Cost basis is 25.6 points of 100 -- the largest single
line item on the scoresheet.

The A-7 boundary here is CONFIRMED against practice run 1, not assumed: see
TestOversellBoundary for the real numbers.
"""
import pytest

from ledger.lots import (Oversell, apply_split, merge_in_delivery_order,
                         plan_relief, relieve, restore_quantities,
                         total_cost, total_quantity)
from ledger.models import Lot
from ledger.money import D


def lots(*pairs) -> list[Lot]:
    """lots((qty, cost), ...) in delivery order."""
    return [Lot(seq=i, quantity=D(str(q)), total_cost=D(str(c)),
                source_event_id=f"evt_{i}")
            for i, (q, c) in enumerate(pairs)]


class TestTheGradedFormula:
    def test_three_share_lot_sold_one_at_a_time(self):
        """LOGIC.md 8.4, the case that separates the graded formula from a
        cost-per-share implementation.

        Formula:  3.33, 3.34, 3.33   (lot totals 6.67 -> 3.33 -> 0)
        Per-share: 3.33, 3.33, 3.34

        Both total 10.00. They disagree at the middle step, and a checkpoint
        landing between the second and third sale reports a cost_basis one cent
        apart -- inside the 64% slice.
        """
        book = lots((3, "10.00"))

        relieved, _ = relieve(book, D(1))
        assert str(relieved) == "3.33"
        assert str(total_cost(book)) == "6.67"

        relieved, _ = relieve(book, D(1))
        assert str(relieved) == "3.34"        # round(6.67 / 2) = round(3.335)
        assert str(total_cost(book)) == "3.33"

        relieved, _ = relieve(book, D(1))
        assert str(relieved) == "3.33"
        assert total_quantity(book) == 0

    def test_a_fully_consumed_lot_relieves_its_exact_total(self):
        """Not round(total x qty/qty) -- no residual cent may be stranded."""
        book = lots((7, "1000.00"))
        relieved, _ = relieve(book, D(7))
        assert str(relieved) == "1000.00"
        # The emptied lot is KEPT at zero so a reversal can restore it by seq.
        assert total_quantity(book) == 0
        assert total_cost(book) == D("0.00")

    def test_emptied_lots_are_invisible_to_reporting(self):
        """Keeping them must not create a phantom position."""
        book = lots((5, "500.00"), (5, "700.00"))
        relieve(book, D(10))
        assert total_quantity(book) == 0
        assert total_cost(book) == D("0.00")

    def test_emptied_lots_are_skipped_on_a_later_walk(self):
        book = lots((5, "500.00"), (5, "700.00"))
        relieve(book, D(5))                      # empties lot 0
        relieved, record = relieve(book, D(2))
        assert record == [(1, D(2), D("280.00"))]
        assert str(relieved) == "280.00"

    def test_multi_lot_relief_rounds_each_lot_independently(self):
        """The total is the SUM OF ROUNDED per-lot amounts, not a rounding of
        the sum."""
        book = lots((7, "1000.00"), (3, "613.00"))
        relieved, record = relieve(book, D(9))

        assert str(relieved) == "1408.67"        # 1000.00 + round(613 x 2/3)
        assert record == [(0, D(7), D("1000.00")), (1, D(2), D("408.67"))]
        assert str(total_cost(book)) == "204.33"
        assert total_quantity(book) == 1

    def test_consumption_record_is_kept_for_reversal(self):
        """A reversal that cannot restore what a sell consumed will balance
        perfectly and corrupt every later cost basis."""
        book = lots((10, "1000.00"), (5, "560.00"))
        _, record = relieve(book, D(12))
        assert record == [(0, D(10), D("1000.00")), (1, D(2), D("224.00"))]


class TestOversellBoundary:
    """A-7, CONFIRMED BY PRACTICE RUN 1 (2026-08-04).

    Oversell is measured against the TOTAL position, not against the position
    less quantity committed to other open sell orders.

    Evidence: ten sells in the run-1 capture exceeded the "free" position while
    staying within the total, and the reference ACCEPTED all ten. Had the rule
    been "free position", all ten would have been rejected.
    """

    def test_a_sale_within_the_position_is_allowed(self):
        book = lots((10, "1000.00"))
        relieved, _ = relieve(book, D(10))
        assert str(relieved) == "1000.00"

    def test_a_sale_larger_than_the_position_is_rejected(self):
        book = lots((10, "1000.00"))
        with pytest.raises(Oversell):
            relieve(book, D("10.000001"))

    def test_rejection_leaves_the_lots_completely_untouched(self):
        """"Reject it. Do NOT leave lots half-consumed." Walking and mutating
        until you run out corrupts the book in a way nothing later repairs."""
        book = lots((5, "500.00"), (5, "700.00"))
        before = [(l.seq, l.quantity, l.total_cost) for l in book]

        with pytest.raises(Oversell):
            relieve(book, D(11))

        assert [(l.seq, l.quantity, l.total_cost) for l in book] == before
        assert str(total_cost(book)) == "1200.00"

    @pytest.mark.parametrize("position,held_by_other_orders,sell_qty", [
        # Real boundary cases from the run-1 capture: quantity exceeds the
        # free position but not the total, and the reference ACCEPTED each.
        ("2.5", "2", "1"),                       # evt_395d218b4d792fe4
        ("4", "4.80", "1.32"),                   # evt_e5788976584d9268
        ("2.68", "5.2220", "0.871"),             # evt_de7888584276386a
        ("1.809", "5.2220", "1.74"),             # evt_0d93b595d9413c14
        ("43", "38.27", "10"),                   # evt_2b8c5da71ca6b97e
        ("33", "38.27", "14.135"),               # evt_6f883fcfb3512be8
    ])
    def test_sells_exceeding_the_free_position_are_still_allowed(
            self, position, held_by_other_orders, sell_qty):
        """The decisive A-7 cases. `held_by_other_orders` is deliberately
        unused: it is exactly the quantity a "free position" rule would have
        subtracted, and the reference did not."""
        book = lots((position, "1000.00"))
        free = D(position) - D(held_by_other_orders)
        assert D(sell_qty) > free, "fixture must exceed the free position"

        relieved, _ = relieve(book, D(sell_qty))       # must not raise
        assert relieved > 0

    def test_the_oversell_check_cannot_consult_a_hold(self):
        """The confirmed rule, stated structurally: relief takes only the lot
        queue and a quantity. There is no parameter through which a share hold
        could influence the decision, so the "free position" reading is not
        merely unused -- it is unrepresentable."""
        import inspect

        assert list(inspect.signature(plan_relief).parameters) == ["lots", "sell_qty"]
        assert list(inspect.signature(relieve).parameters) == ["lots", "sell_qty"]


class TestSplitRescaling:
    def test_quantity_scales_and_total_cost_does_not(self):
        book = lots((10, "1000.00"))
        apply_split(book, D(1), D(2))
        assert total_quantity(book) == 20
        assert str(total_cost(book)) == "1000.00"

    def test_applied_per_lot_so_the_queue_survives(self):
        book = lots((10, "1000.00"), (4, "600.00"))
        apply_split(book, D(2), D(3))
        assert [str(l.quantity) for l in book] == ["15.000000", "6.000000"]
        assert [str(l.total_cost) for l in book] == ["1000.00", "600.00"]

    def test_quantities_quantize_to_six_places(self):
        book = lots((100, "1000.00"))
        apply_split(book, D(7), D(3))
        assert str(book[0].quantity) == "42.857143"

    def test_reverse_split(self):
        book = lots((100, "1000.00"))
        apply_split(book, D(5), D(1))
        assert total_quantity(book) == 20
        assert str(total_cost(book)) == "1000.00"

    def test_pre_split_quantities_restore_exactly(self):
        """Dividing back by ratio_to/ratio_from is lossy at 6dp, so a reversal
        restores the recorded quantities verbatim (LOGIC.md 13.4)."""
        book = lots((100, "1000.00"))
        before = apply_split(book, D(7), D(3))
        assert str(book[0].quantity) == "42.857143"

        restore_quantities(book, before)
        assert book[0].quantity == D(100)

    def test_a_bad_ratio_raises(self):
        for bad in ((D(0), D(2)), (D(2), D(0)), (D(-1), D(2))):
            with pytest.raises(ValueError):
                apply_split(lots((10, "100.00")), *bad)

    def test_relief_after_a_split_uses_the_new_quantities(self):
        """An ignored split corrupts every position and cost basis after it."""
        book = lots((10, "1000.00"))
        apply_split(book, D(1), D(2))
        relieved, _ = relieve(book, D(10))
        assert str(relieved) == "500.00"           # half the lot, half the cost
        assert str(total_cost(book)) == "500.00"


class TestMerge:
    def test_merges_by_delivery_sequence(self):
        target = [Lot(0, D(5), D("500.00"), "a"), Lot(4, D(7), D("700.00"), "c")]
        incoming = [Lot(2, D(10), D("1000.00"), "b")]
        merged = merge_in_delivery_order(target, incoming)
        assert [l.seq for l in merged] == [0, 2, 4]
        assert [str(l.total_cost) for l in merged] == ["500.00", "1000.00", "700.00"]
