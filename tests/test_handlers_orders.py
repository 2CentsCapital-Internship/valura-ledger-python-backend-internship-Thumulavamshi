"""Buy fills -- the one posting the task sheet works for us in full.

Practice returns the reference's legs for these, so this is the handler that
validates the fee chain and the A-1/A-2/A-18 assumptions underneath it.
"""
import pytest

from conftest import buy_fill, legs_by_account
from ledger.money import D, legs_balance, money_str


class TestBuyFillLegs:
    def test_worked_example_full_entry(self, engine):
        """BRK-A, principal 10,000.00, partner_rate 0.50.

            Dr 2010  10,032.00        Cr 2350  10,000.00
            Dr 1200  10,000.00        Cr 2100  10,000.00
            Dr 5000       9.35        Cr 4000      20.00
            Dr 5010       2.00        Cr 4010       4.00
            Dr 5100       6.33        Cr 2400       8.00
                                      Cr 2411       9.35
                                      Cr 2420       2.00
                                      Cr 2430       6.33
        """
        legs = engine.apply(buy_fill(principal="10000.00", broker="BRK-A"))
        by_account = legs_by_account(legs)

        assert by_account == {
            "2010": ("10032.00", "0.00"),     # P + b + c + r
            "1200": ("10000.00", "0.00"),
            "5000": ("9.35", "0.00"),
            "5010": ("2.00", "0.00"),
            "5100": ("6.33", "0.00"),
            "2350": ("0.00", "10000.00"),
            "2100": ("0.00", "10000.00"),
            "4000": ("0.00", "20.00"),
            "4010": ("0.00", "4.00"),
            "2400": ("0.00", "8.00"),
            "2411": ("0.00", "9.35"),
            "2420": ("0.00", "2.00"),
            "2430": ("0.00", "6.33"),
        }
        assert legs_balance(legs)
        assert len(legs) == 13

    def test_every_leg_carries_the_customer_id(self, engine):
        legs = engine.apply(buy_fill(customer_id="CUST-2002"))
        assert {l["customer_id"] for l in legs} == {"CUST-2002"}

    def test_broker_payable_account_follows_the_fill_broker(self, engine):
        for broker, account in [("BRK-A", "2411"), ("BRK-B", "2412"),
                                ("BRK-C", "2413")]:
            eng = type(engine)(None)
            legs = eng.apply(buy_fill(broker=broker, asset_class="equity"
                                      if broker != "BRK-C" else "etf"))
            assert account in legs_by_account(legs)

    def test_cash_does_not_move_on_the_trade_date(self, engine):
        """"Cash does not move on the trade date: the firm owes the broker the
        principal until settlement, two days later." A book that touches 1100
        here disagrees with the broker for as long as anything is unsettled."""
        legs = engine.apply(buy_fill())
        assert "1100" not in legs_by_account(legs)
        assert engine.state.balance("CUST-1001", "1100") == D("0.00")

    def test_revenue_and_cost_are_booked_gross(self, engine):
        """Never netted to margin: 5000 and 4000 both move at full size."""
        by_account = legs_by_account(engine.apply(buy_fill(broker="BRK-A")))
        assert by_account["4000"] == ("0.00", "20.00")     # gross revenue
        assert by_account["5000"] == ("9.35", "0.00")      # gross cost
        # A margin-only book would post 10.65 to one account and nothing here.

    def test_zero_partner_share_legs_are_OMITTED(self, engine):
        """A-9a, CONFIRMED WRONG BY PRACTICE RUN 1 (2026-08-04).

        We assumed the reference emits 5100/2430 as structural zero lines. It
        does not -- it returned 11 legs and listed ours as `unexpected`.

        Real fixture: evt_82fc4a16bcb6c3a5, BRK-B, principal 2280.50,
        partner_rate 0.50. Margin is 4.56 - 5.50 = -0.94, so ps clamps to zero.
        """
        legs = engine.apply(buy_fill(principal="2280.50", broker="BRK-B",
                                     partner_rate="0.50", quantity="100",
                                     price="22.805"))
        by_account = legs_by_account(legs)

        assert "5100" not in by_account
        assert "2430" not in by_account
        assert sorted(by_account) == ["1200", "2010", "2100", "2350", "2400",
                                      "2412", "2420", "4000", "4010", "5000",
                                      "5010"]
        assert len(legs) == 11
        assert legs_balance(legs)

    def test_zero_custody_and_regulatory_legs_are_OMITTED(self, engine):
        """A-9b, same verdict, independent trigger.

        Real fixture: evt_79539ad5fce3da1b, BRK-A, principal 8.03,
        partner_rate 0.20. Custody (4bps -> 0.003) and custody cost (2bps ->
        0.002) both round to zero, so THREE legs drop and the reference
        returned 10. Note ps is 0.13 here -- non-zero -- which is why A-9a and
        A-9b are genuinely independent.
        """
        legs = engine.apply(buy_fill(principal="8.03", broker="BRK-A",
                                     partner_rate="0.20", quantity="0.625",
                                     price="12.85"))
        by_account = legs_by_account(legs)

        assert "4010" not in by_account          # custody revenue, 0.00
        assert "2420" not in by_account          # custodian payable, 0.00
        assert "5010" not in by_account          # custody cost, 0.00
        assert sorted(by_account) == ["1200", "2010", "2100", "2350", "2400",
                                      "2411", "2430", "4000", "5000", "5100"]
        assert len(legs) == 10
        assert by_account["4000"] == ("0.00", "1.00")    # floored at min fee
        assert by_account["5100"] == ("0.13", "0.00")    # partner share NOT zero
        assert legs_balance(legs)

    def test_fee_amounts_match_the_reference_exactly(self, engine):
        """The reference's `missing` list was empty across all 776 results:
        whenever we posted a leg it wanted, the amount agreed. This pins the
        exact non-zero values it returned for evt_79539ad5fce3da1b."""
        legs = engine.apply(buy_fill(principal="8.03", broker="BRK-A",
                                     partner_rate="0.20", quantity="0.625",
                                     price="12.85"))
        by_account = legs_by_account(legs)
        assert by_account["1200"] == ("8.03", "0.00")
        assert by_account["2010"] == ("9.04", "0.00")    # P + b + c + r
        assert by_account["2100"] == ("0.00", "8.03")
        assert by_account["2350"] == ("0.00", "8.03")
        assert by_account["2400"] == ("0.00", "0.01")
        assert by_account["2411"] == ("0.00", "0.36")    # 0.01 + 0.35 ticket
        assert by_account["2430"] == ("0.00", "0.13")
        assert by_account["5000"] == ("0.36", "0.00")


class TestBuyFillState:
    def test_lot_cost_is_principal_only_not_principal_plus_charges(self, engine):
        """The worked example credits 2100 with P, not P + b + c + r.

        Capitalising the charges balances perfectly and is wrong on 64% of the
        checkpoint score for the rest of the run."""
        engine.apply(buy_fill(principal="10000.00", quantity="100"))

        assert engine.state.position_cost("CUST-1001", "ACME") == D("10000.00")
        assert engine.state.position_quantity("CUST-1001", "ACME") == D("100")

    def test_lots_append_in_delivery_order(self, engine):
        engine.apply(buy_fill(principal="1000.00", quantity="10"))
        engine.apply(buy_fill(principal="2000.00", quantity="10"))

        lots = engine.state.lots_for("CUST-1001", "ACME")
        assert [str(l.total_cost) for l in lots] == ["1000.00", "2000.00"]
        assert [l.seq for l in lots] == [0, 1]

    def test_fill_is_indexed_by_trade_id(self, engine):
        engine.apply(buy_fill(trade_id="trd_abc"))
        assert "trd_abc" in engine.state.fills
        assert engine.state.fills["trd_abc"].settled is False

    def test_final_fill_closes_the_order(self, engine):
        engine.apply(buy_fill(order_id="ord_9", final=True))
        assert engine.state.orders["ord_9"].status == "CLOSED"
        assert engine.state.open_orders() == []

    def test_partial_fill_leaves_the_order_open(self, engine):
        engine.apply(buy_fill(order_id="ord_9", final=False))
        assert engine.state.orders["ord_9"].is_open

    def test_fill_before_placement_creates_a_placeholder(self, engine):
        """"A fill may arrive before its placement -- handle it or record it;
        do not stall the stream." The fill's payload is self-sufficient."""
        legs = engine.apply(buy_fill(order_id="ord_orphan", final=False))

        assert len(legs) == 13
        order = engine.state.orders["ord_orphan"]
        assert order.placement_seen is False
        # No limit price, so no computable route: fall back to the fill's
        # broker for open_order_routes (A-10).
        assert order.reported_route == "BRK-A"


class TestBuyFillRejections:
    @pytest.mark.parametrize("field", ["order_id", "customer_id", "side",
                                       "symbol", "quantity", "price",
                                       "principal", "broker", "partner_rate",
                                       "trade_id"])
    def test_missing_required_field_is_rejected(self, engine, field):
        ev = buy_fill()
        del ev["payload"][field]
        assert engine.apply(ev) == []
        assert engine.stats["rejected"] == 1

    def test_unknown_broker_is_rejected(self, engine):
        assert engine.apply(buy_fill(broker="BRK-Z")) == []
        assert engine.stats["rejected"] == 1

    def test_unparseable_numeric_is_rejected(self, engine):
        assert engine.apply(buy_fill(principal="not-a-number")) == []
        assert engine.stats["rejected"] == 1

    def test_sell_with_no_position_is_an_oversell(self, engine):
        ev = buy_fill()
        ev["payload"]["side"] = "sell"
        assert engine.apply(ev) == []
        assert engine.stats["rejected"] == 1
        assert any("oversell" in r for r in engine.rejections_by_reason)


class TestDuplicateTradeIdRejection:
    """The systematic defect, identified from practice run 1.

    In the run-1 capture this separated the fills perfectly: 7 of 7 buy fills
    the reference rejected carried a trade_id already seen on an earlier,
    DIFFERENT event, and 0 of 57 it accepted did.
    """

    def test_a_reused_trade_id_on_a_distinct_event_is_rejected(self, engine):
        first = engine.apply(buy_fill(trade_id="trd_dup", order_id="ord_1"))
        assert len(first) == 13

        second = engine.apply(buy_fill(trade_id="trd_dup", order_id="ord_2"))

        assert second == []
        assert engine.rejections_by_reason[
            "duplicate trade_id on a distinct event"] == 1

    def test_rejection_leaves_no_lot_and_no_second_fill(self, engine):
        engine.apply(buy_fill(trade_id="trd_dup", principal="1000.00",
                              quantity="10"))
        engine.apply(buy_fill(trade_id="trd_dup", principal="9999.00",
                              quantity="99", order_id="ord_2"))

        assert engine.state.position_quantity("CUST-1001", "ACME") == 10
        assert str(engine.state.position_cost("CUST-1001", "ACME")) == "1000.00"
        assert len(engine.state.fills) == 1

    def test_a_genuine_re_delivery_is_NOT_a_defect(self, engine):
        """The engine's seen-gate catches a re-delivered event_id before the
        handler runs, so this rule can only ever fire on a distinct event
        reusing a settled identifier. Confusing the two would reject every
        replayed fill during the chaos rewind -- 300 to 400 events."""
        ev = buy_fill(trade_id="trd_same")
        first = engine.apply(ev)
        second = engine.apply(ev)               # same event_id, re-delivered

        assert second == first                  # not rejected, not re-posted
        assert engine.stats["duplicates"] == 1
        assert engine.rejections_by_reason.get(
            "duplicate trade_id on a distinct event") is None

    def test_distinct_trade_ids_are_unaffected(self, engine):
        engine.apply(buy_fill(trade_id="trd_1", order_id="ord_1"))
        engine.apply(buy_fill(trade_id="trd_2", order_id="ord_2"))
        assert engine.stats["rejected"] == 0
        assert len(engine.state.fills) == 2


class TestDefectCandidateLogging:
    def test_principal_mismatch_is_logged_not_rejected(self, engine, caplog):
        """Detect broadly, reject narrowly. Wrongly rejecting a valid fill
        loses its lot and poisons every later cost basis for that symbol."""
        legs = engine.apply(buy_fill(quantity="100", price="100.00",
                                     principal="9999.00"))
        assert len(legs) == 13                     # still posted
        assert "principal != round(qty x price)" in caplog.text

    def test_asset_class_conflict_is_logged(self, engine, caplog):
        """"Every symbol belongs to one asset class for the whole run" is
        handed to us as an invariant -- one of the strongest defect candidates."""
        engine.apply(buy_fill(symbol="ACME", asset_class="equity"))
        engine.apply(buy_fill(symbol="ACME", asset_class="bond",
                              broker="BRK-B"))
        assert "symbol asset_class changed" in caplog.text

    def test_broker_not_trading_asset_class_is_logged(self, engine, caplog):
        engine.apply(buy_fill(broker="BRK-A", asset_class="bond"))
        assert "broker does not trade asset class" in caplog.text
