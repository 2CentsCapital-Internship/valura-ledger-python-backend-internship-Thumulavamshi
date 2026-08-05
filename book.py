"""Your ledger. This is the whole assignment.

`client.py` handles the network and hands you one event at a time; this returns
the journal legs it produced. Some events correctly produce none: an empty list,
not None-as-an-accident.

This file is a thin adapter. The ledger itself lives in the `ledger/` package:

    ledger/money.py      the rounding convention (half away from zero)
    ledger/tariff.py     the six-part fee chain and order routing
    ledger/state.py      balances keyed by (customer, account), the lot book
    ledger/engine.py     dispatch, the seen-gate, "never stop the run"
    ledger/handlers/     one module per event family
    ledger/snapshot.py   checkpoint serialization
    ledger/eventlog.py   the append-only log that makes as-of answerable

Read PROJECT.md, then LOGIC.md. NOTE that the PROTOCOL.md bundled in this repo
is a STALE snapshot of the spec -- it predates the broker tariff, six of the
seventeen accounts, order routing and the as-of checkpoints, and its posting
hints for fills are wrong. task_description.txt is the canonical version.
"""
from __future__ import annotations

from ledger.engine import LedgerEngine, NotImplementedYet, Rejected
from ledger.eventlog import EventLog, replay
from ledger.snapshot import snapshot as build_snapshot

__all__ = ["Book", "Rejected", "NotImplementedYet"]


class Book:
    """The interface client.py expects: apply() and snapshot()."""

    def __init__(self, log_path: str | None = None) -> None:
        self.event_log = EventLog(log_path)
        self.engine = LedgerEngine(event_log=self.event_log)

    # -- consuming ----------------------------------------------------------
    def apply(self, event: dict) -> list[dict]:
        """Post one event and return its legs. Never raises."""
        return self.engine.apply(event)

    # -- reporting ----------------------------------------------------------
    def snapshot(self, as_of_event_id: str | None = None) -> dict:
        """Full state for a checkpoint.

        With `as_of_event_id`, describes the book as it stood once that event
        had been processed, in delivery order, and nothing after it -- answered
        by replaying the log through the same apply() path the live run uses.

        At 6,000 events a full replay is milliseconds against a 60-second grace
        period, so there is no snapshotting machinery here and does not need to
        be any. See LOGIC.md section 12.3.
        """
        if as_of_event_id is None:
            return build_snapshot(self.engine.state)
        historical = replay(self.event_log.upto(as_of_event_id))
        return build_snapshot(historical.state)

    @property
    def todo(self) -> dict[str, int]:
        """Event types we have not implemented yet, for the end-of-run report."""
        return dict(self.engine.unimplemented_by_type)

    def summary(self) -> dict:
        return self.engine.summary()

    def close(self) -> None:
        self.event_log.close()
