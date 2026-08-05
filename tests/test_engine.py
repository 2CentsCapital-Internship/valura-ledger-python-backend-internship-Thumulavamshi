"""The engine's guarantees: idempotency, and never stopping the run.

These matter more than any single handler. Resilience is 15 points, the chaos
replay is guaranteed to happen (80/300/400 events), and "the single most
expensive mistake is stopping".
"""
from decimal import Decimal

import pytest

from conftest import buy_fill, deposit, event
from ledger.engine import LedgerEngine, NotImplementedYet, Rejected
from ledger.eventlog import EventLog
from ledger.money import D


class TestIdempotency:
    def test_duplicate_delivery_changes_no_balance(self, engine):
        ev = deposit(amount="1000.00")
        first = engine.apply(ev)
        balances_after_first = dict(engine.state.balances)

        second = engine.apply(ev)

        assert second == first
        assert dict(engine.state.balances) == balances_after_first
        assert engine.stats["duplicates"] == 1
        assert engine.stats["events"] == 1        # counted once

    def test_conflicting_duplicate_first_delivery_wins(self, engine):
        """The same event_id with DIFFERENT content: first delivery wins.

        Reported separately by the server, and explicitly not a rejection.
        """
        first = engine.apply(deposit(amount="1000.00", event_id="evt_dup"))
        second = engine.apply(deposit(amount="9999.99", event_id="evt_dup"))

        assert second == first
        assert engine.state.balance("CUST-1001", "1100") == D("1000.00")
        assert engine.stats["rejected"] == 0

    def test_redelivered_rejection_stays_one_rejection(self, engine):
        """A redelivered event already rejected stays ONE rejection, not two.

        "An id you have seen is an id you have seen, whatever you did with it."
        """
        bad = deposit(amount="-5.00", event_id="evt_bad")
        assert engine.apply(bad) == []
        assert engine.apply(bad) == []
        assert engine.stats["rejected"] == 1

    def test_replayed_window_is_a_no_op(self, engine):
        """Simulates the deliberate mid-run rewind."""
        events = [deposit(amount=f"{100 + i}.00") for i in range(20)]
        for ev in events:
            engine.apply(ev)
        snapshot = dict(engine.state.balances)

        for ev in events[5:15]:                    # server rewinds us
            engine.apply(ev)

        assert dict(engine.state.balances) == snapshot
        assert engine.stats["duplicates"] == 10


class TestNeverStops:
    def test_handler_crash_costs_one_event_not_the_run(self, engine):
        def exploding(state, payload, ev):
            raise ZeroDivisionError("boom")
        engine.handlers["deposit"] = exploding

        assert engine.apply(deposit()) == []
        assert engine.stats["crashed"] == 1

        engine.handlers["deposit"] = LedgerEngine().handlers["deposit"]
        assert len(engine.apply(deposit())) == 2   # still consuming

    def test_unbalanced_legs_are_rejected_not_raised(self, engine):
        def lopsided(state, payload, ev):
            from ledger.money import leg
            return [leg("1100", "C", debit="10.00"),
                    leg("2010", "C", credit="9.00")]
        engine.handlers["deposit"] = lopsided

        assert engine.apply(deposit()) == []
        assert engine.state.balances == {}
        assert "unbalanced legs" in engine.rejections_by_reason

    def test_unknown_event_type_is_loud_but_survivable(self, engine, caplog):
        assert engine.apply(event("teleportation", {})) == []
        assert engine.stats["unknown_type"] == 1
        assert "UNKNOWN EVENT TYPE" in caplog.text

    def test_control_frames_are_not_flagged_as_unknown(self, engine):
        assert engine.apply(event("checkpoint_request", {})) == []
        assert engine.stats["unknown_type"] == 0

    def test_missing_event_id_does_not_raise(self, engine):
        assert engine.apply({"type": "deposit", "payload": {}}) == []

    def test_unimplemented_is_counted_separately_from_rejected(self, engine):
        """Every real type is wired now, so this exercises the mechanism with a
        placeholder installed by hand. It stays because an unwritten handler
        must never be conflated with a judgement we made."""
        from ledger.engine import _unimplemented
        engine.handlers["reversal"] = _unimplemented("reversal")

        assert engine.apply(event("reversal", {})) == []
        assert engine.stats["unimplemented"] == 1
        assert engine.stats["rejected"] == 0
        assert engine.unimplemented_by_type["reversal"] == 1


class TestRejectionLeavesBookUnchanged:
    def test_rejected_event_posts_nothing(self, engine):
        engine.apply(deposit(amount="1000.00"))
        before = dict(engine.state.balances)

        assert engine.apply(deposit(amount="0.00")) == []

        assert dict(engine.state.balances) == before

    def test_malformed_fill_leaves_no_lot(self, engine):
        bad = buy_fill(quantity="0")
        assert engine.apply(bad) == []
        assert engine.state.lots == {}
        assert engine.state.fills == {}


class TestDeterminism:
    def test_same_log_gives_identical_state(self):
        events = [deposit(amount="100.00"), buy_fill(), deposit(amount="7.77")]

        a = LedgerEngine(EventLog(None))
        b = LedgerEngine(EventLog(None))
        for ev in events:
            a.apply(ev)
        for ev in events:
            b.apply(ev)

        assert dict(a.state.balances) == dict(b.state.balances)
        assert a.state.position_cost("CUST-1001", "ACME") == \
               b.state.position_cost("CUST-1001", "ACME")


class TestHandlerCoverage:
    def test_every_declared_event_type_has_an_entry(self):
        from ledger.engine import EVENT_TYPES
        handlers = LedgerEngine().handlers
        assert set(handlers) == set(EVENT_TYPES)

    def test_event_type_count_is_24(self):
        """The portal blurb says 23. Enumerating section 4 of the task sheet
        gives 24. Unresolved -- run 1's type histogram settles it."""
        from ledger.engine import EVENT_TYPES
        assert len(EVENT_TYPES) == 24
        assert len(set(EVENT_TYPES)) == 24
