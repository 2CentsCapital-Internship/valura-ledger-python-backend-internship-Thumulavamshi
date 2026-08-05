"""Dispatch, idempotency, and the guarantee that nothing ever stops the run.

"The single most expensive mistake is stopping. A server that rejects one event
and keeps consuming beats one that crashes and misses a thousand."

The seen-gate is the FIRST statement of apply(), above every mutation. That is
what makes idempotency structural rather than something each handler has to
remember, and it is what survives the deliberate mid-run replay.

See LOGIC.md section 10 and TECHNICAL_PLAN.md section 4.3.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from .money import drop_zero_legs, legs_balance
from .state import LedgerState

log = logging.getLogger(__name__)


class Rejected(Exception):
    """An event we refuse to post on its own merits.

    An oversell, a reversal of something we never received, a payload that will
    not parse, an fx_deposit with a negative spread. Produces no legs and must
    leave the book exactly as it was.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotImplementedYet(Exception):
    """A handler we have not written yet.

    Deliberately distinct from Rejected: an unwritten handler is a gap in our
    coverage, a rejection is a judgement we made. Conflating them would hide
    the former in the stats for the latter.
    """


# Every ledger event type in the canonical task sheet, section 4. Enumerated
# explicitly so an unknown type is LOUD rather than silently counted (G-10).
#
# Note: this list has 24 entries; the portal blurb says "23 event types". The
# discrepancy is unresolved and is one of the run-1 capture questions.
EVENT_TYPES = (
    # cash
    "deposit", "fee_charged", "fee_refund",
    "withdrawal_requested", "withdrawal_settled", "withdrawal_rejected",
    "interest_credited", "transfer_between_customers",
    # orders
    "order_placed", "order_partially_filled", "order_filled",
    "trade_settled", "order_cancelled", "order_rejected",
    # payables
    "broker_fees_settled", "custodian_fees_settled",
    "reg_fees_remitted", "partner_payout",
    # corporate actions
    "dividend_cash", "dividend_reinvested", "stock_split", "symbol_change",
    # fx
    "fx_deposit",
    # corrections
    "reversal",
)

# Not ledger events. checkpoint_request is routed by the client; the three
# control frames never reach the book at all.
NON_LEDGER_TYPES = frozenset({
    "checkpoint_request", "stream_open", "stream_reset", "stream_end",
})


def _unimplemented(event_type: str):
    def handler(state, payload, event):
        raise NotImplementedYet(event_type)
    handler.__name__ = f"unimplemented_{event_type}"
    return handler


# Event types we have deliberately NOT built yet, with the build step that
# owns each. This list is the contract: `build_handlers` asserts that the set
# of types without a real handler is exactly this set, so a handler that exists
# but is not wired in fails at import rather than silently scoring zero.
#
# Removing a name from here without adding its handler is a hard error, and so
# is adding a handler without removing its name.
DEFERRED: dict[str, str] = {}
"""Nothing is deferred: all 24 event types have a real handler."""


def build_handlers() -> dict:
    """The dispatch table.

    Every type in EVENT_TYPES has an entry, and the entries that are still
    placeholders must match DEFERRED exactly. Practice run 1 showed why that
    check earns its keep: four no-leg handlers (order_placed, order_cancelled,
    stock_split, symbol_change) were reported as "not implemented" and scored
    CORRECT on legs anyway, because an empty list happened to be the right
    answer -- while their state effects silently went missing.
    """
    from .handlers import cash, corporate, corrections, orders, payables

    handlers = {t: _unimplemented(t) for t in EVENT_TYPES}
    handlers.update({
        # cash
        "deposit": cash.on_deposit,
        "fee_charged": cash.on_fee_charged,
        "fee_refund": cash.on_fee_refund,
        "withdrawal_requested": cash.on_withdrawal_requested,
        "withdrawal_settled": cash.on_withdrawal_settled,
        "withdrawal_rejected": cash.on_withdrawal_rejected,
        "interest_credited": cash.on_interest_credited,
        "transfer_between_customers": cash.on_transfer_between_customers,
        "fx_deposit": cash.on_fx_deposit,
        # orders
        "order_placed": orders.on_order_placed,
        "order_cancelled": orders.on_order_closed,
        "order_rejected": orders.on_order_closed,
        "order_filled": orders.on_fill,
        "order_partially_filled": orders.on_fill,
        "trade_settled": orders.on_trade_settled,
        # corporate actions
        "dividend_cash": corporate.on_dividend_cash,
        "dividend_reinvested": corporate.on_dividend_reinvested,
        "stock_split": corporate.on_stock_split,
        "symbol_change": corporate.on_symbol_change,
        # paying it all onward
        "broker_fees_settled": payables.on_broker_fees_settled,
        "custodian_fees_settled": payables.on_custodian_fees_settled,
        "reg_fees_remitted": payables.on_reg_fees_remitted,
        "partner_payout": payables.on_partner_payout,
        # corrections
        "reversal": corrections.on_reversal,
    })

    placeholders = {t for t, h in handlers.items()
                    if getattr(h, "__name__", "").startswith("unimplemented_")}
    if placeholders != set(DEFERRED):
        unwired = placeholders - set(DEFERRED)
        stale = set(DEFERRED) - placeholders
        raise RuntimeError(
            "dispatch table disagrees with DEFERRED. "
            f"Not wired in (handler may exist but is unreachable): {sorted(unwired)}. "
            f"Listed as deferred but actually implemented: {sorted(stale)}.")
    return handlers


class LedgerEngine:
    """Applies events to a LedgerState and returns the legs each produced."""

    def __init__(self, event_log=None) -> None:
        self.state = LedgerState()
        self.event_log = event_log
        self.handlers = build_handlers()

        # event_id -> the legs we returned. Doubles as the seen-set, so a
        # re-delivery returns the same answer with no state change.
        self.seen: dict[str, list[dict]] = {}
        self.rejections: dict[str, str] = {}          # event_id -> reason

        self.stats = {
            "events": 0, "posted": 0, "no_legs": 0, "rejected": 0,
            "unimplemented": 0, "crashed": 0, "duplicates": 0, "unknown_type": 0,
        }
        self.by_type: dict[str, int] = defaultdict(int)
        self.unimplemented_by_type: dict[str, int] = defaultdict(int)
        self.rejections_by_reason: dict[str, int] = defaultdict(int)
        self.crashes_by_type: dict[str, int] = defaultdict(int)
        self.unknown_types: dict[str, int] = defaultdict(int)

    # -- the one entry point into state mutation ---------------------------
    def apply(self, event: dict) -> list[dict]:
        """Post one event and return its legs.

        Never raises. An event we cannot handle costs that event, never the run.
        """
        event_id = event.get("event_id")
        if not event_id:
            log.error("event with no event_id: %r", event)
            self.stats["crashed"] += 1
            return []

        # --- the seen-gate. Above every mutation, deliberately. -------------
        if event_id in self.seen:
            self.stats["duplicates"] += 1
            # Same answer, no state change. A re-delivered event we rejected
            # stays one rejection, not two: "an id you have seen is an id you
            # have seen, whatever you did with it."
            return self.seen[event_id]

        event_type = event.get("type", "<missing>")
        self.stats["events"] += 1
        self.by_type[event_type] += 1

        if self.event_log is not None:
            self.event_log.append(event)

        legs = self._dispatch(event_id, event_type, event)

        # The reference omits zero-valued legs entirely (A-9, confirmed by
        # practice run 1). Applied centrally so no handler has to remember, and
        # safe because dropping a 0.00/0.00 leg cannot unbalance a transaction.
        legs = drop_zero_legs(legs)

        if legs and not legs_balance(legs):
            # Reject rather than post garbage. The balance assertion belongs in
            # tests, where raising is free; here it must not end the run (G-5).
            log.error("unbalanced legs for %s (%s), rejecting: %r",
                      event_id, event_type, legs)
            self.rejections_by_reason["unbalanced legs"] += 1
            self.rejections[event_id] = "unbalanced legs"
            self.stats["rejected"] += 1
            legs = []
        elif legs:
            self.state.post(legs)
            self.state.postings[event_id] = legs
            self.stats["posted"] += 1

        self.seen[event_id] = legs
        return legs

    def _dispatch(self, event_id: str, event_type: str, event: dict) -> list[dict]:
        handler = self.handlers.get(event_type)
        if handler is None:
            if event_type not in NON_LEDGER_TYPES:
                # Loud: an unenumerated type means we are scoring zero on all
                # of them and would otherwise never notice.
                log.error("UNKNOWN EVENT TYPE %r (event %s) -- not in EVENT_TYPES",
                          event_type, event_id)
                self.unknown_types[event_type] += 1
                self.stats["unknown_type"] += 1
            return []

        payload = event.get("payload") or {}
        try:
            legs = handler(self.state, payload, event) or []
        except Rejected as exc:
            self.rejections[event_id] = exc.reason
            self.rejections_by_reason[exc.reason] += 1
            self.stats["rejected"] += 1
            return []
        except NotImplementedYet:
            self.unimplemented_by_type[event_type] += 1
            self.stats["unimplemented"] += 1
            return []
        except Exception:
            # Never propagate. One bad event, not the whole run.
            log.exception("handler crashed on %s (%s)", event_id, event_type)
            self.crashes_by_type[event_type] += 1
            self.stats["crashed"] += 1
            return []

        if not legs:
            self.stats["no_legs"] += 1
        return legs

    # -- reporting ----------------------------------------------------------
    def summary(self) -> dict:
        return {
            "stats": dict(self.stats),
            "by_type": dict(sorted(self.by_type.items())),
            "unimplemented_by_type": dict(sorted(self.unimplemented_by_type.items())),
            "rejections_by_reason": dict(sorted(self.rejections_by_reason.items())),
            "crashes_by_type": dict(sorted(self.crashes_by_type.items())),
            "unknown_types": dict(sorted(self.unknown_types.items())),
        }
