"""The event log and as-of replay.

"Current state is not enough to answer these; how you make your history
answerable is a design decision this assignment deliberately forces, and it is
much easier to take before you start than after."
"""
from decimal import Decimal

from conftest import buy_fill, deposit
from ledger.eventlog import EventLog, dumps, loads, replay
from ledger.money import D, dec
from ledger.snapshot import snapshot


class TestJsonDiscipline:
    def test_numbers_parse_as_decimal_never_float(self):
        """Without parse_float, json.loads turns 1000.10 into a binary float
        before we ever see it -- the exact failure the sheet warns about."""
        parsed = loads('{"amount": 1000.10, "rate": 0.0005}')
        assert isinstance(parsed["amount"], Decimal)
        assert isinstance(parsed["rate"], Decimal)
        assert parsed["amount"] == D("1000.10")

    def test_decimals_serialize_as_strings(self):
        assert dumps({"x": D("1.25")}) == '{"x": "1.25"}'

    def test_round_trip_preserves_exactness(self):
        original = {"amount": D("0.1"), "other": D("0.2")}
        back = loads(dumps(original))
        assert D(back["amount"]) + D(back["other"]) == D("0.3")


class TestEventLog:
    def test_appends_in_delivery_order(self):
        log = EventLog(None)
        for ev in [deposit(event_id="a"), deposit(event_id="b")]:
            log.append(ev)
        assert [e["event_id"] for e in log] == ["a", "b"]

    def test_deduplicates_by_event_id(self):
        """The log records what we PROCESSED, in the order we processed it --
        which is exactly the sequence an as-of query cuts."""
        log = EventLog(None)
        assert log.append(deposit(event_id="a")) is True
        assert log.append(deposit(event_id="a")) is False
        assert len(log) == 1

    def test_upto_is_inclusive_of_the_named_event(self):
        log = EventLog(None)
        for eid in ["a", "b", "c"]:
            log.append(deposit(event_id=eid))
        assert [e["event_id"] for e in log.upto("b")] == ["a", "b"]

    def test_upto_unknown_id_returns_everything(self, caplog):
        log = EventLog(None)
        log.append(deposit(event_id="a"))
        assert len(log.upto("never-delivered")) == 1
        assert "never delivered" in caplog.text

    def test_persists_and_reloads(self, tmp_path):
        path = tmp_path / "capture.jsonl"
        log = EventLog(path)
        log.append(deposit(event_id="a", amount="12.34"))
        log.append(buy_fill(event_id="b"))
        log.close()

        reloaded = EventLog.load(path)
        assert [e["event_id"] for e in reloaded] == ["a", "b"]
        # Amounts arrive from the server as strings and round-trip as strings;
        # what matters is that they parse back to the same exact value. Bare
        # JSON numbers are covered by TestJsonDiscipline.
        assert dec(reloaded.events[0]["payload"]["amount"]) == D("12.34")

    def test_load_survives_a_corrupt_line(self, tmp_path, caplog):
        path = tmp_path / "capture.jsonl"
        path.write_text('{"event_id":"a","type":"deposit","payload":{}}\n'
                        'not json at all\n'
                        '{"event_id":"c","type":"deposit","payload":{}}\n',
                        encoding="utf-8")
        reloaded = EventLog.load(path)
        assert [e["event_id"] for e in reloaded] == ["a", "c"]


class TestAsOfReplay:
    def test_replay_reproduces_live_state_exactly(self):
        """Purity check: if the fold is deterministic, replaying the whole log
        gives byte-identical state. If it does not, as-of answers will drift."""
        from ledger.engine import LedgerEngine

        log = EventLog(None)
        live = LedgerEngine(event_log=log)
        for ev in [deposit(amount="500.00"), buy_fill(), deposit(amount="7.77")]:
            live.apply(ev)

        rebuilt = replay(log.events)

        assert snapshot(rebuilt.state) == snapshot(live.state)

    def test_as_of_excludes_everything_after_the_cut(self):
        from ledger.engine import LedgerEngine

        log = EventLog(None)
        live = LedgerEngine(event_log=log)
        live.apply(deposit(event_id="d1", amount="100.00"))
        live.apply(deposit(event_id="d2", amount="900.00"))

        as_of = replay(log.upto("d1"))

        assert as_of.state.credit_balance("CUST-1001", "2010") == D("100.00")
        assert live.state.credit_balance("CUST-1001", "2010") == D("1000.00")

    def test_as_of_is_delivery_order_not_business_date(self):
        """"If a backdated event arrived later, the as-of answer does not
        include it." The cut is on OUR arrival sequence."""
        from ledger.engine import LedgerEngine

        log = EventLog(None)
        live = LedgerEngine(event_log=log)
        live.apply(deposit(event_id="d1", amount="100.00"))
        # Arrives after d1 but is dated before it. Still excluded from as-of d1.
        live.apply(deposit(event_id="d2", amount="50.00", backdated_days=30))

        as_of = replay(log.upto("d1"))
        assert as_of.state.credit_balance("CUST-1001", "2010") == D("100.00")

    def test_book_snapshot_as_of_matches_manual_replay(self):
        from book import Book

        book = Book()
        book.apply(deposit(event_id="d1", amount="100.00"))
        book.apply(buy_fill(event_id="f1", principal="1000.00", quantity="10"))
        book.apply(deposit(event_id="d2", amount="900.00"))

        at_d1 = book.snapshot(as_of_event_id="d1")
        assert at_d1["customers"]["CUST-1001"]["wallet_cash"] == "100.00"
        assert at_d1["customers"]["CUST-1001"]["positions"] == {}

        at_f1 = book.snapshot(as_of_event_id="f1")
        assert at_f1["customers"]["CUST-1001"]["positions"]["ACME"] == {
            "quantity": "10", "cost_basis": "1000.00",
        }

    def test_replay_is_fast_enough_for_the_grace_window(self):
        """6,000 events maximum against a 60-second grace period. Measured, so
        that the "no snapshotting needed" decision stays justified."""
        import time

        events = [deposit(amount="1.00") for _ in range(3000)]
        events += [buy_fill(principal="100.00", quantity="1") for _ in range(3000)]

        started = time.perf_counter()
        replay(events)
        elapsed = time.perf_counter() - started

        assert elapsed < 10.0, f"6k-event replay took {elapsed:.2f}s"
