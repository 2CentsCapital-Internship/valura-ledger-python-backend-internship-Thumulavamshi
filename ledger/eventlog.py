"""The append-only event log: delivery order, deduped, persisted.

This is the highest-leverage hundred lines in the project. It is required by:

  * as-of checkpoints  -- "current state is not enough to answer these"
  * crash recovery     -- the stream resumes by offset, but our book does not
                          rebuild itself; that is our job
  * the offline replay loop -- iterate on a captured run without spending one
                          of the 12 practice runs
  * the idempotency property tests
  * the A-5 recompute option, if practice says surgical undo is wrong

"How you make your history answerable is a design decision this assignment
deliberately forces, and it is much easier to take before you start than after."

See LOGIC.md section 12 and TECHNICAL_PLAN.md section 4.1.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

log = logging.getLogger(__name__)


def loads(text: str) -> dict:
    """Parse JSON with every number as a Decimal.

    Without parse_float, json.loads turns 1000.10 into a binary float before we
    ever see it -- the exact failure the task sheet warns about, and one that
    would be invisible until a cent went missing somewhere downstream.
    """
    return json.loads(text, parse_float=Decimal)


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


def dumps(obj) -> str:
    return json.dumps(obj, cls=_DecimalEncoder)


class EventLog:
    """Events in delivery order, first-delivery-wins.

    Only novel events are appended: the log records what we processed, in the
    order we processed it, which is exactly the sequence an as-of query cuts.
    Re-deliveries are no-ops at the engine's seen-gate and never reach here.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.events: list[dict] = []
        self._seen: set[str] = set()
        self.path = Path(path) if path else None
        self._fh = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")

    def append(self, event: dict) -> bool:
        """Record an event. Returns False if it was already logged."""
        event_id = event.get("event_id")
        if not event_id or event_id in self._seen:
            return False
        self._seen.add(event_id)
        self.events.append(event)
        if self._fh:
            try:
                self._fh.write(dumps(event) + "\n")
                self._fh.flush()
            except Exception:
                log.exception("failed to persist event %s", event_id)
        return True

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self):
        return iter(self.events)

    def upto(self, event_id: str) -> list[dict]:
        """Events up to and including the named one, in delivery order.

        "Describing your book as it stood once you had processed that event, in
        delivery order, and nothing after it."

        Returns the whole log if the id was never delivered -- we have no cut
        point, so current state is the best available answer.
        """
        for index, event in enumerate(self.events):
            if event.get("event_id") == event_id:
                return self.events[:index + 1]
        log.warning("as-of event %s never delivered; answering with full state",
                    event_id)
        return list(self.events)

    @classmethod
    def load(cls, path: str | Path) -> "EventLog":
        """Read a captured log back, for offline replay. Does not reopen it
        for appending."""
        log_ = cls(None)
        source = Path(path)
        if not source.exists():
            return log_
        with source.open("r", encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    log_.append(loads(line))
                except Exception:
                    log.error("could not parse %s line %d", source, line_number)
        return log_


def replay(events, upto_event_id: str | None = None):
    """Fold a sequence of events into a fresh engine.

    Uses the identical apply() path as the live run -- that is the point. If
    replay used a different code path it would not prove anything about the
    live state, and the as-of answers would drift from the current ones.
    """
    from .engine import LedgerEngine

    engine = LedgerEngine(event_log=None)
    for event in events:
        engine.apply(event)
        if upto_event_id is not None and event.get("event_id") == upto_event_id:
            break
    return engine
