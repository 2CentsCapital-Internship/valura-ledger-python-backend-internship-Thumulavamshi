"""The class of bug practice run 1 exposed: a handler that exists but is not
reachable, or a no-leg event that scores CORRECT while its state effect is
silently missing.

Run 1 reported order_placed (68), order_cancelled (3), stock_split (20) and
symbol_change (11) as "not implemented" -- and the reference marked all 102 of
them CORRECT, because an empty list was the right answer for the LEGS. Nothing
in the score told us the holds, lot rescaling and re-keying were absent. These
tests make that failure mode loud.
"""
import pytest

from conftest import buy_fill, deposit, event
from ledger.engine import DEFERRED, EVENT_TYPES, LedgerEngine, build_handlers
from ledger.money import D
from ledger.snapshot import snapshot


class TestDispatchTable:
    def test_every_event_type_has_an_entry(self):
        assert set(build_handlers()) == set(EVENT_TYPES)

    def test_placeholders_match_the_deferred_list_exactly(self):
        """build_handlers() raises if they diverge, so this documents intent:
        a handler added without removing its DEFERRED entry, or an entry
        removed without adding the handler, is a hard error at import."""
        handlers = build_handlers()
        placeholders = {t for t, h in handlers.items()
                        if h.__name__.startswith("unimplemented_")}
        assert placeholders == set(DEFERRED)

    def test_an_unwired_event_type_fails_loudly(self, monkeypatch):
        """The exact run-1 bug: a type that reaches only the placeholder while
        nothing declares it as deferred.

        Simulated by declaring a new event type without a handler, since every
        real type is now wired."""
        import ledger.engine as engine_module
        monkeypatch.setattr(engine_module, "EVENT_TYPES",
                            EVENT_TYPES + ("teleportation",))
        with pytest.raises(RuntimeError, match="Not wired in"):
            build_handlers()

    def test_a_stale_deferred_entry_fails_loudly(self, monkeypatch):
        """The other direction: a type listed as deferred that is in fact
        implemented. That is how order_placed hid for a whole run."""
        import ledger.engine as engine_module
        padded = dict(DEFERRED, order_placed="not really")
        monkeypatch.setattr(engine_module, "DEFERRED", padded)
        with pytest.raises(RuntimeError, match="Listed as deferred"):
            build_handlers()

    @pytest.mark.parametrize("event_type", [
        "order_placed", "order_cancelled", "order_rejected",
        "stock_split", "symbol_change",
    ])
    def test_no_leg_handlers_reach_real_code_not_the_placeholder(
            self, engine, event_type):
        """A no-leg event returning [] proves nothing on its own -- the
        placeholder returns [] too. What distinguishes them is that a real
        handler does NOT increment the unimplemented counter."""
        handler = engine.handlers[event_type]
        assert not handler.__name__.startswith("unimplemented_")

    def test_nothing_is_deferred_any_more(self):
        """All 24 event types have a real handler."""
        assert set(DEFERRED) == set()

    def test_every_single_event_type_reaches_a_real_handler(self):
        handlers = build_handlers()
        placeholders = [t for t, h in handlers.items()
                        if h.__name__.startswith("unimplemented_")]
        assert placeholders == []
        assert len(handlers) == 24

    def test_all_cash_and_fx_events_are_now_wired(self):
        """Build step 6. These were the largest remaining gap after run 1:
        311 events across eight types."""
        handlers = build_handlers()
        for event_type in ("deposit", "fee_charged", "fee_refund",
                           "withdrawal_requested", "withdrawal_settled",
                           "withdrawal_rejected", "interest_credited",
                           "transfer_between_customers", "fx_deposit"):
            assert not handlers[event_type].__name__.startswith("unimplemented_")
            assert event_type not in DEFERRED


class TestNoHandlerCrashes:
    """The engine's "never stop the run" guard swallows handler exceptions --
    which is right in production and dangerous in tests, because a handler can
    mutate state, then crash, and still look like it returned [] correctly.

    That is exactly how a missing LedgerState.split_history attribute survived
    the split unit tests: apply_split ran first, so the position assertions
    passed while every stock_split in the real capture crashed.

    Any test exercising a handler should assert this counter stays at zero.
    """

    def test_a_full_mixed_stream_produces_no_crashes(self, engine):
        events = [
            deposit(amount="10000.00"),
            event("order_placed", {
                "order_id": "ord_1", "customer_id": "CUST-1001", "side": "buy",
                "symbol": "ACME", "quantity": "100", "limit_price": "50.00",
                "asset_class": "equity", "est_charges": "12.00"}),
            buy_fill(order_id="ord_1", quantity="40", principal="2000.00",
                     final=False),
            buy_fill(order_id="ord_1", quantity="60", principal="3000.00",
                     final=True, trade_id="trd_second"),
            event("stock_split", {"customer_id": "CUST-1001", "symbol": "ACME",
                                  "ratio_from": "1", "ratio_to": "2"}),
            event("symbol_change", {"customer_id": "CUST-1001",
                                    "old_symbol": "ACME",
                                    "new_symbol": "ACME2"}),
            event("order_placed", {
                "order_id": "ord_2", "customer_id": "CUST-1001", "side": "sell",
                "symbol": "ACME2", "quantity": "10", "limit_price": "60.00",
                "asset_class": "equity", "est_charges": "5.00"}),
            event("order_cancelled", {"order_id": "ord_2"}),
        ]
        for ev in events:
            engine.apply(ev)

        assert engine.stats["crashed"] == 0, engine.crashes_by_type
        assert dict(engine.crashes_by_type) == {}

    def test_split_records_its_pre_split_quantities(self, engine):
        """The attribute whose absence caused the crash. Asserted directly so
        it cannot silently disappear again."""
        engine.apply(buy_fill(principal="1000.00", quantity="10"))
        split = event("stock_split", {
            "customer_id": "CUST-1001", "symbol": "ACME",
            "ratio_from": "1", "ratio_to": "2"})
        engine.apply(split)

        assert engine.stats["crashed"] == 0
        recorded = engine.state.split_history[split["event_id"]]
        assert recorded["symbol"] == "ACME"
        assert recorded["quantities"] == [(0, 10)]


class TestNoLegEventsHaveStateEffects:
    """Each of these returns [] and ALSO changes state. Asserting only the
    empty list is what let run 1 pass while doing nothing."""

    def _place(self, order_id="ord_1", side="buy", quantity="100",
               limit_price="50.00", est_charges="12.00", symbol="ACME",
               asset_class="equity", customer_id="CUST-1001", **kw):
        return event("order_placed", {
            "order_id": order_id, "customer_id": customer_id, "side": side,
            "symbol": symbol, "quantity": quantity, "limit_price": limit_price,
            "asset_class": asset_class, "est_charges": est_charges,
        }, **kw)

    def test_order_placed_returns_no_legs_but_creates_a_hold(self, engine):
        assert engine.apply(self._place()) == []
        assert engine.stats["unimplemented"] == 0

        order = engine.state.orders["ord_1"]
        # quantity x limit_price + est_charges, used AS GIVEN
        assert str(order.remaining_hold) == "5012.00"
        assert engine.state.cash_hold("CUST-1001") == order.remaining_hold

    def test_order_placed_computes_and_stores_the_route(self, engine):
        """Notional 5000 on equity routes to BRK-B; 1200 routes to BRK-A."""
        engine.apply(self._place(order_id="big", quantity="100",
                                 limit_price="50.00"))
        engine.apply(self._place(order_id="small", quantity="100",
                                 limit_price="12.00"))
        assert engine.state.orders["big"].route == "BRK-B"
        assert engine.state.orders["small"].route == "BRK-A"
        # and it reaches the checkpoint, which is where the 8% is scored
        assert snapshot(engine.state)["open_order_routes"] == {
            "big": "BRK-B", "small": "BRK-A"}

    def test_sell_placement_holds_shares_not_cash(self, engine):
        """A-6: the checkpoint has no share-hold field, so a customer whose
        only open orders are sells reports cash_hold 0.00."""
        engine.apply(self._place(side="sell"))
        assert engine.state.cash_hold("CUST-1001") == 0
        assert engine.state.orders["ord_1"].is_open

    def test_order_cancelled_releases_the_hold_to_exactly_zero(self, engine):
        engine.apply(self._place())
        assert engine.state.cash_hold("CUST-1001") > 0

        assert engine.apply(event("order_cancelled", {"order_id": "ord_1"})) == []

        assert engine.state.orders["ord_1"].remaining_hold == 0
        assert engine.state.cash_hold("CUST-1001") == 0
        assert engine.state.open_orders() == []

    def test_order_rejected_behaves_identically(self, engine):
        engine.apply(self._place())
        engine.apply(event("order_rejected", {"order_id": "ord_1"}))
        assert engine.state.cash_hold("CUST-1001") == 0

    def test_partial_fill_releases_a_proportional_share(self, engine):
        """A-4: release round(initial_hold x fill_qty / order_qty)."""
        engine.apply(self._place(quantity="100", limit_price="50.00",
                                 est_charges="12.00"))          # hold 5012.00
        engine.apply(buy_fill(order_id="ord_1", quantity="25",
                              principal="1250.00", final=False))

        # 25 of 100 filled -> release 1253.00, leaving 3759.00
        assert str(engine.state.orders["ord_1"].remaining_hold) == "3759.00"

    def test_each_fill_is_rounded_INDEPENDENTLY(self, engine):
        """A-4, confirmed by run 3's checkpoint diff. The real numbers:
        order qty 48, limit 191.78, est_charges 26.00 -> hold 9231.44, then
        two fills of 10.

            per fill    round(9231.44 x 10/48) = 1923.22 twice -> 5385.00
            cumulative  round(9231.44 x 20/48) = 3846.43       -> 5385.01

        The reference wants 5385.00. That one cent was the last defect in the
        book -- every other checkpoint part scored 1.0 while cash_hold sat at
        0.9167.
        """
        engine.apply(self._place(quantity="48", limit_price="191.78",
                                 est_charges="26.00"))
        order = engine.state.orders["ord_1"]
        assert str(order.initial_hold) == "9231.44"

        engine.apply(buy_fill(order_id="ord_1", quantity="10",
                              principal="1917.80", final=False,
                              trade_id="t1"))
        engine.apply(buy_fill(order_id="ord_1", quantity="10",
                              principal="1917.80", final=False,
                              trade_id="t2"))

        assert str(order.remaining_hold) == "5385.00"
        assert order.fill_quantities == [D("10"), D("10")]

    def test_the_release_is_recomputed_not_accumulated(self, engine):
        """Recomputing from the recorded fill quantities keeps a replay exact.
        Applying the same fills again (the chaos rewind) must not double."""
        engine.apply(self._place(quantity="48", limit_price="191.78",
                                 est_charges="26.00"))
        f1 = buy_fill(order_id="ord_1", quantity="10", principal="1917.80",
                      final=False, trade_id="t1")
        engine.apply(f1)
        engine.apply(f1)                       # re-delivered

        assert str(engine.state.orders["ord_1"].remaining_hold) == "7308.22"
        assert engine.state.orders["ord_1"].fill_quantities == [D("10")]

    def test_final_fill_zeroes_the_hold(self, engine):
        engine.apply(self._place(quantity="100"))
        engine.apply(buy_fill(order_id="ord_1", quantity="60",
                              principal="3000.00", final=False))
        assert engine.state.orders["ord_1"].remaining_hold > 0

        engine.apply(buy_fill(order_id="ord_1", quantity="40",
                              principal="2000.00", final=True))
        assert engine.state.orders["ord_1"].remaining_hold == 0
        assert engine.state.cash_hold("CUST-1001") == 0

    def test_stock_split_returns_no_legs_but_rescales_lots(self, engine):
        engine.apply(buy_fill(principal="1000.00", quantity="10"))

        assert engine.apply(event("stock_split", {
            "customer_id": "CUST-1001", "symbol": "ACME",
            "ratio_from": "1", "ratio_to": "2"})) == []

        assert engine.state.position_quantity("CUST-1001", "ACME") == 20
        # "The total cost of each lot is unchanged, so cost per share moves."
        assert str(engine.state.position_cost("CUST-1001", "ACME")) == "1000.00"

    def test_stock_split_scales_each_lot_separately(self, engine):
        """Per lot, not on an aggregate: the FIFO queue must survive with its
        boundaries and per-lot costs intact."""
        engine.apply(buy_fill(principal="1000.00", quantity="10"))
        engine.apply(buy_fill(principal="600.00", quantity="4",
                              order_id="ord_2"))

        engine.apply(event("stock_split", {
            "customer_id": "CUST-1001", "symbol": "ACME",
            "ratio_from": "2", "ratio_to": "3"}))

        lots = engine.state.lots_for("CUST-1001", "ACME")
        assert [str(l.quantity) for l in lots] == ["15.000000", "6.000000"]
        assert [str(l.total_cost) for l in lots] == ["1000.00", "600.00"]

    def test_reverse_split_works_too(self, engine):
        engine.apply(buy_fill(principal="1000.00", quantity="10"))
        engine.apply(event("stock_split", {
            "customer_id": "CUST-1001", "symbol": "ACME",
            "ratio_from": "5", "ratio_to": "1"}))
        assert engine.state.position_quantity("CUST-1001", "ACME") == 2

    def test_split_on_an_unheld_symbol_is_a_clean_no_op(self, engine):
        assert engine.apply(event("stock_split", {
            "customer_id": "CUST-9999", "symbol": "NOPE",
            "ratio_from": "1", "ratio_to": "2"})) == []

    def test_split_does_not_touch_other_holders(self, engine):
        """"A split for one customer says nothing about anyone else's position
        in that symbol." Never propagate."""
        engine.apply(buy_fill(customer_id="CUST-1001", principal="1000.00",
                              quantity="10"))
        engine.apply(buy_fill(customer_id="CUST-2002", principal="1000.00",
                              quantity="10", order_id="ord_2"))

        engine.apply(event("stock_split", {
            "customer_id": "CUST-1001", "symbol": "ACME",
            "ratio_from": "1", "ratio_to": "2"}))

        assert engine.state.position_quantity("CUST-1001", "ACME") == 20
        assert engine.state.position_quantity("CUST-2002", "ACME") == 10

    def test_symbol_change_returns_no_legs_but_rekeys_the_position(self, engine):
        engine.apply(buy_fill(symbol="OLDCO", principal="1000.00",
                              quantity="10"))

        assert engine.apply(event("symbol_change", {
            "customer_id": "CUST-1001", "old_symbol": "OLDCO",
            "new_symbol": "NEWCO"})) == []

        assert engine.state.position_quantity("CUST-1001", "OLDCO") == 0
        assert engine.state.position_quantity("CUST-1001", "NEWCO") == 10
        assert str(engine.state.position_cost("CUST-1001", "NEWCO")) == "1000.00"

    def test_symbol_change_leaves_no_phantom_position(self, engine):
        """"Reporting a position that should not exist counts against you.\""""
        engine.apply(buy_fill(symbol="OLDCO", principal="1000.00", quantity="10"))
        engine.apply(event("symbol_change", {
            "customer_id": "CUST-1001", "old_symbol": "OLDCO",
            "new_symbol": "NEWCO"}))
        assert "OLDCO" not in engine.state.symbols_held("CUST-1001")

    def test_symbol_change_into_a_held_symbol_merges_by_delivery_order(self, engine):
        """A-11: merge by seq, not by appending."""
        engine.apply(buy_fill(symbol="NEWCO", principal="500.00", quantity="5"))
        engine.apply(buy_fill(symbol="OLDCO", principal="1000.00", quantity="10",
                              order_id="ord_2"))
        engine.apply(buy_fill(symbol="NEWCO", principal="700.00", quantity="7",
                              order_id="ord_3"))

        engine.apply(event("symbol_change", {
            "customer_id": "CUST-1001", "old_symbol": "OLDCO",
            "new_symbol": "NEWCO"}))

        lots = engine.state.lots_for("CUST-1001", "NEWCO")
        assert [l.seq for l in lots] == sorted(l.seq for l in lots)
        assert [str(l.total_cost) for l in lots] == ["500.00", "1000.00", "700.00"]
        assert engine.state.position_quantity("CUST-1001", "NEWCO") == 22

    def test_symbol_change_of_an_unheld_symbol_is_a_clean_no_op(self, engine):
        assert engine.apply(event("symbol_change", {
            "customer_id": "CUST-1", "old_symbol": "NOPE",
            "new_symbol": "ALSONOPE"})) == []
        assert engine.state.lots == {}
