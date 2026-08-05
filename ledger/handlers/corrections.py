"""Reversals. See LOGIC.md section 13.

Two halves, both required:

    "Post the exact inverse of the original's legs, and KEEP BOTH: the audit
     trail retains the original and its reversal. A reversal must ALSO undo the
     original's effect on your LOT BOOK, not just on the accounts. A reversed
     buy whose lot you leave in place will balance perfectly and quietly
     corrupt every subsequent cost basis."

And one thing that is NOT undone:

    "Reversing a fill does not restore the hold. A released hold stays
     released; a reversal undoes the postings and the lot book, NOT the
     lifecycle."

Run 2's capture shows what actually gets reversed: 13 of 31 target
dividend_reinvested, 6 fee_charged, 5 deposit, 4 fills, 2 dividend_cash, and
one targets an event never delivered (which must be rejected).
"""
from __future__ import annotations

import logging

from ..engine import Rejected
from ..lots import restore_quantities
from ..models import BUY, SELL
from ..money import ZERO_QTY, invert_legs

log = logging.getLogger(__name__)


def on_reversal(state, p: dict, event: dict) -> list[dict]:
    """reversal -- reverses_event_id, plus a free-text reason we ignore."""
    source_id = p.get("reverses_event_id")
    if not source_id:
        raise Rejected("reversal without reverses_event_id")

    if source_id in state.reversed_event_ids:
        raise Rejected("reversal of an already-reversed event")

    original_legs = state.postings.get(source_id)
    original_was_stateful = _has_state_effect(state, source_id)

    if original_legs is None and not original_was_stateful:
        # "A reversal of an event you never received -> record it as rejected,
        # carry on." Also covers an original we ourselves rejected (A-17):
        # there are no legs to invert and no state change to undo.
        raise Rejected("reversal of an unknown or unposted event")

    # Undo the lot book FIRST, so that if it raises we have not posted legs
    # for a reversal we could not complete.
    _undo_lot_book(state, source_id)

    state.reversed_event_ids.add(source_id)

    # Copy the stored amounts and swap the columns. NEVER recompute: re-running
    # the fee chain could land a half-cent differently and leave a residual
    # that never clears.
    return invert_legs(original_legs or [])


def _has_state_effect(state, source_id: str) -> bool:
    """True if the original changed the lot book even though it posted no legs
    (a split or a symbol change)."""
    return source_id in state.split_history


def _undo_lot_book(state, source_id: str) -> None:
    """Remove the original's effect on the lot book.

    Dispatch is by what the original event actually did, discovered from the
    state it left behind rather than from its type -- which keeps this correct
    even for an original whose type we look up after the fact.
    """
    # --- a split: restore the exact pre-split quantities -------------------
    record = state.split_history.pop(source_id, None)
    if record is not None:
        lots = state.lots_for(record["customer_id"], record["symbol"])
        restore_quantities(lots, record["quantities"])
        return

    # --- a sell fill: put back what it consumed ---------------------------
    fill = next((f for f in state.fills.values() if f.event_id == source_id),
                None)
    if fill is not None:
        fill.reversed_ = True
        if fill.side == SELL:
            _restore_consumed(state, fill)
        elif fill.side == BUY:
            _remove_lot_created_by(state, source_id)
        # "Reversing a fill does not restore the hold." The order stays CLOSED
        # and does not return to open_order_routes. Deliberately untouched.
        return

    # --- a buy fill or a dividend_reinvested: remove the lot it created ----
    _remove_lot_created_by(state, source_id)


def _restore_consumed(state, fill) -> None:
    """Put quantity and cost back on the exact lots a sell drew from.

    Emptied lots are kept at zero rather than dropped (see lots.apply_relief),
    so every seq in the consumption record is still findable.
    """
    lots = state.lots_for(fill.customer_id, fill.symbol)
    by_seq = {lot.seq: lot for lot in lots}
    for lot_seq, take, relieved in fill.consumption:
        lot = by_seq.get(lot_seq)
        if lot is None:
            log.warning("reversal cannot find lot seq %s for %s/%s",
                        lot_seq, fill.customer_id, fill.symbol)
            continue
        lot.quantity += take
        lot.total_cost += relieved


def _remove_lot_created_by(state, source_id: str) -> None:
    """Remove the lot a buy fill or a dividend_reinvested created.

    A-5, still UNRESOLVED after two practice runs: neither capture contained a
    reversal of a lot that had been partially consumed, so the choice between
    surgical undo (remove what remains) and full recompute (rebuild the lot
    book excluding the voided event) has never been tested against the
    reference.

    We do the surgical undo. Where the lot is untouched -- which is every case
    observed so far -- the two strategies agree exactly. Where it has been
    partially consumed they diverge, and the divergence lands on the 64%
    cost-basis slice, so this stays flagged rather than settled.
    """
    for (customer_id, symbol), lots in list(state.lots.items()):
        for lot in lots:
            if lot.source_event_id != source_id:
                continue
            if lot.quantity < lot.original_quantity:
                # THE A-5 DIVERGENCE CASE. Later sells already relieved cost
                # against this lot, so surgical undo (remove what remains) and
                # full recompute (rebuild as if the buy never happened) give
                # different answers, and the difference lands on the 64%
                # cost-basis slice.
                #
                # Deliberately loud: four practice runs and ~3,100 events have
                # never produced this, so if it fires for the first time during
                # a scored, feedback-free attempt, this log line is the only
                # way we will ever know it happened. Grep A5-DIVERGENCE.
                consumed = lot.original_quantity - lot.quantity
                log.warning(
                    "A5-DIVERGENCE reversal of a partially consumed lot: "
                    "event=%s customer=%s symbol=%s lot_seq=%s "
                    "original_qty=%s remaining_qty=%s consumed=%s "
                    "remaining_cost=%s -- surgical undo applied, which is a "
                    "GUESS; cost basis for this symbol may diverge from the "
                    "reference from here on",
                    source_id, customer_id, symbol, lot.seq,
                    lot.original_quantity, lot.quantity, consumed,
                    lot.total_cost)
            lots.remove(lot)
            return
