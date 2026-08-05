"""Decimal arithmetic, the rounding convention, and leg construction.

The task sheet states two conventions that are "graded exactly as stated":

  1. Every amount is rounded to the cent independently, half away from zero.
  2. Debits equal credits on every transaction, and every leg carries the
     customer_id it concerns.

This module owns (1). There is exactly one rounding helper for money and one
for quantities, and no handler is allowed to call ``quantize`` itself -- a
second rounding path is a second convention, and the whole point is that there
is only one.

See LOGIC.md section 1 for the reasoning.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP, getcontext

log = logging.getLogger(__name__)

# 28 significant digits is the default, but set it explicitly so behaviour does
# not depend on whatever ambient context we happen to be imported into.
getcontext().prec = 28

D = Decimal

CENT = D("0.01")
MICRO = D("0.000001")          # share quantities carry up to 6 decimal places

ZERO = D("0.00")
ZERO_QTY = D("0")


def dec(value) -> Decimal:
    """Parse a payload value into a Decimal without ever touching binary float.

    JSON should be parsed with ``parse_float=Decimal`` (see eventlog.py) so a
    float never reaches us in the first place. If one does, we convert via
    ``str`` -- which uses the shortest round-tripping repr and so is the least
    wrong option available -- and log loudly, because it means the parsing
    discipline has a hole in it somewhere.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        log.warning("float reached dec(): %r -- JSON parsing discipline broken", value)
        return D(str(value))
    if isinstance(value, int):
        return D(value)
    if isinstance(value, str):
        return D(value.strip())
    raise TypeError(f"cannot parse {value!r} ({type(value).__name__}) as Decimal")


def money(x) -> Decimal:
    """Round to the cent, half AWAY FROM ZERO.

    Not the built-in ``round()``, which is half-to-even (banker's rounding) and
    would give 6.32 where the reference gives 6.33.

    Python's ``ROUND_HALF_UP`` is a misleading name for the right behaviour: it
    rounds away from zero on a tie, so -1.005 -> -1.01, not -1.00.
    """
    return dec(x).quantize(CENT, rounding=ROUND_HALF_UP)


def quantity(x) -> Decimal:
    """Round a share quantity to 6 decimal places, half away from zero.

    Used after a stock split scales a lot; fill quantities arrive already
    within precision and pass through unchanged.
    """
    return dec(x).quantize(MICRO, rounding=ROUND_HALF_UP)


def money_str(x) -> str:
    """Serialize an amount as a fixed 2dp string: '0.00', never '0' or '0E-2'."""
    return format(money(x), "f")


def quantity_str(x) -> str:
    """Serialize a quantity as a plain decimal string, no exponent notation.

    ``Decimal.normalize()`` turns Decimal('10') into Decimal('1E+1'), which is
    exactly the serialization the 2026-08-03 clarification says the server
    stopped emitting. Formatting with 'f' forces positional notation, so
    8.000000 -> '8' and 42.857143 -> '42.857143'.
    """
    return format(quantity(x).normalize(), "f")


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    """One journal leg, in the wire format /v1/postings expects.

    Both columns are always present; the unused one is '0.00'.
    """
    return {
        "account": account,
        "customer_id": customer_id,
        "debit": money_str(debit),
        "credit": money_str(credit),
    }


def drop_zero_legs(legs: list[dict]) -> list[dict]:
    """Remove legs whose debit AND credit are both zero.

    CONFIRMED BY PRACTICE RUN 1 (2026-08-04). Our A-9 assumption was that the
    reference emits these as structural lines of the entry. It does not: across
    129 responses carrying expected_legs, not one contained a 0.00/0.00 leg,
    and the reference explicitly listed ours as `unexpected`. A fill with a
    clamped partner share returns 11 legs, not 13; one that also zeroes custody
    returns 10.

    Dropping a zero leg cannot unbalance a transaction, so this is safe to
    apply centrally after every handler. See LOGIC.md A-9.
    """
    return [l for l in legs
            if not (dec(l["debit"]) == ZERO and dec(l["credit"]) == ZERO)]


def legs_balance(legs: list[dict]) -> bool:
    """Debits equal credits. True for an empty list."""
    debits = sum((dec(l["debit"]) for l in legs), ZERO)
    credits = sum((dec(l["credit"]) for l in legs), ZERO)
    return money(debits) == money(credits)


def invert_legs(legs: list[dict]) -> list[dict]:
    """Swap the debit and credit columns of every leg.

    Used by ``reversal``, which posts the exact inverse of the original. The
    amounts are COPIED, never recomputed: re-running the fee chain could land a
    half-cent differently and leave a residual that never clears.
    """
    return [
        {
            "account": l["account"],
            "customer_id": l["customer_id"],
            "debit": l["credit"],
            "credit": l["debit"],
        }
        for l in legs
    ]
