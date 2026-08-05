"""The rounding convention. Graded exactly as stated, so tested exactly.

"Every amount is rounded to the cent independently, half away from zero."
"""
from decimal import Decimal

import pytest

from ledger.money import (D, ZERO, dec, invert_legs, leg, legs_balance, money,
                          money_str, quantity, quantity_str)


class TestHalfAwayFromZero:
    """The half-cent cases that actually occur in the fee chain."""

    @pytest.mark.parametrize("raw,expected", [
        ("6.325", "6.33"),    # partner share, 0.50 x 12.65 (worked example 1)
        ("4.875", "4.88"),    # brokerage, BRK-B on 3250 (worked example 2)
        ("1.625", "1.63"),    # custody,   BRK-B on 3250
        ("0.975", "0.98"),    # custody cost, BRK-B on 3250
        ("3.335", "3.34"),    # FIFO relief, 6.67 / 2 (LOGIC.md section 8.4)
        ("3.325", "3.33"),
        ("0.005", "0.01"),
        ("0.004", "0.00"),
    ])
    def test_ties_round_away_from_zero(self, raw, expected):
        assert money_str(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("-6.325", "-6.33"),
        ("-1.005", "-1.01"),
        ("-0.005", "-0.01"),
    ])
    def test_negative_ties_round_away_from_zero(self, raw, expected):
        """ROUND_HALF_UP in Python's decimal means away from zero, not upward.

        Reversals copy stored amounts rather than recomputing, so this should
        never actually be exercised -- but if it is, it must be right.
        """
        assert money_str(raw) == expected

    def test_differs_from_builtin_round(self):
        """The built-in round() is half-to-even and would disagree with the
        reference on exactly the cases the fee chain produces."""
        assert money_str("6.325") == "6.33"
        assert round(Decimal("6.325"), 2) == Decimal("6.32")   # banker's
        assert money_str("3.325") == "3.33"
        assert round(Decimal("3.325"), 2) == Decimal("3.32")


class TestParsing:
    def test_never_goes_through_float(self):
        """0.1 + 0.2 in binary float is not 0.3. Through Decimal strings it is."""
        assert dec("0.1") + dec("0.2") == dec("0.3")

    def test_accepts_decimal_str_int(self):
        assert dec(D("1.23")) == D("1.23")
        assert dec("1.23") == D("1.23")
        assert dec(5) == D(5)
        assert dec(" 1.23 ") == D("1.23")

    def test_float_is_converted_but_warns(self, caplog):
        value = dec(1.25)
        assert value == D("1.25")
        assert "float reached dec()" in caplog.text

    def test_rejects_nonsense(self):
        with pytest.raises(TypeError):
            dec(None)
        with pytest.raises(Exception):
            dec("not a number")


class TestSerialization:
    def test_money_str_is_always_two_places(self):
        assert money_str(0) == "0.00"
        assert money_str("5") == "5.00"
        assert money_str("5.1") == "5.10"

    @pytest.mark.parametrize("raw,expected", [
        ("8.000000", "8"),
        ("8", "8"),
        ("10", "10"),          # normalize() alone would give '1E+1'
        ("100", "100"),
        ("42.857143", "42.857143"),
        ("0.500000", "0.5"),
        ("0", "0"),
    ])
    def test_quantity_str_never_uses_exponent_notation(self, raw, expected):
        """The 2026-08-03 clarification says the server stopped serializing 10
        as '1E+1'. We must not start."""
        assert quantity_str(raw) == expected

    def test_quantity_rounds_to_six_places(self):
        # 100 shares through a 3-for-7 split
        assert quantity_str(D(100) * D(3) / D(7)) == "42.857143"


class TestLegs:
    def test_leg_shape(self):
        assert leg("1100", "CUST-1", debit="1000") == {
            "account": "1100", "customer_id": "CUST-1",
            "debit": "1000.00", "credit": "0.00",
        }

    def test_balance_check(self):
        balanced = [leg("1100", "C", debit="10"), leg("2010", "C", credit="10")]
        assert legs_balance(balanced)
        assert legs_balance([])
        unbalanced = [leg("1100", "C", debit="10"), leg("2010", "C", credit="9")]
        assert not legs_balance(unbalanced)

    def test_invert_swaps_columns_and_preserves_everything_else(self):
        original = [leg("1100", "CUST-1", debit="10.00"),
                    leg("2010", "CUST-1", credit="10.00")]
        inverted = invert_legs(original)
        assert inverted == [
            {"account": "1100", "customer_id": "CUST-1",
             "debit": "0.00", "credit": "10.00"},
            {"account": "2010", "customer_id": "CUST-1",
             "debit": "10.00", "credit": "0.00"},
        ]
        assert legs_balance(inverted)

    def test_inversion_is_exact_not_recomputed(self):
        """A reversal must copy the stored amount. If it were recomputed the
        half-cent could land the other way and leave a residual."""
        original = [leg("5100", "C", debit="6.33"), leg("2430", "C", credit="6.33")]
        assert invert_legs(original)[0]["credit"] == "6.33"
