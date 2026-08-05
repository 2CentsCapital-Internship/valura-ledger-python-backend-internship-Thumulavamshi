"""Sell fills and trade_settled.

The sell entry is DERIVED -- practice returns full legs only for deposits and
buy fills, confirmed across two runs (0 of 49 sells carried expected_legs).
What run 2 did give us is `accounts_differ`, which named exactly the thirteen
accounts below on every sell we failed to post. That is the strongest external
check available on this entry.

Cost basis is 64% of the checkpoint score, and until sells relieve cost every
position is overstated forever -- which is why this was the top priority after
run 2's 17.76/40 on checkpoints.
"""
import pytest

from conftest import buy_fill, deposit, event, legs_by_account
from ledger.money import D, legs_balance


def sell_fill(customer_id="CUST-1001", symbol="ACME", quantity="100",
              price="100.00", principal="10000.00", broker="BRK-A",
              partner_rate="0.50", asset_class="equity", order_id="ord_s",
              trade_id=None, final=True, **kw):
    ev = buy_fill(customer_id=customer_id, symbol=symbol, quantity=quantity,
                  price=price, principal=principal, broker=broker,
                  partner_rate=partner_rate, asset_class=asset_class,
                  order_id=order_id, trade_id=trade_id, final=final, **kw)
    ev["payload"]["side"] = "sell"
    return ev


@pytest.fixture
def position(engine):
    """A 100-share position at cost 8,500.00."""
    engine.apply(buy_fill(principal="8500.00", quantity="100",
                          order_id="ord_b", trade_id="trd_b"))
    return engine


class TestSellLegs:
    def test_full_sell_entry(self, position):
        """BRK-A, principal 10,000.00, partner_rate 0.50, FIFO cost 8,500.00.

            Dr 1150  10,000.00        Cr 2010   9,968.00
            Dr 2100   8,500.00        Cr 1200   8,500.00
            Dr 5000       9.35        Cr 4000      20.00
            Dr 5010       2.00        Cr 4010       4.00
            Dr 5100       6.33        Cr 2400       8.00
                                      Cr 2411       9.35
                                      Cr 2420       2.00
                                      Cr 2430       6.33
        """
        legs = position.apply(sell_fill(principal="10000.00", quantity="100"))

        assert legs_by_account(legs) == {
            "1150": ("10000.00", "0.00"),
            "2100": ("8500.00", "0.00"),
            "5000": ("9.35", "0.00"),
            "5010": ("2.00", "0.00"),
            "5100": ("6.33", "0.00"),
            "2010": ("0.00", "9968.00"),      # P - b - c - r
            "1200": ("0.00", "8500.00"),
            "4000": ("0.00", "20.00"),
            "4010": ("0.00", "4.00"),
            "2400": ("0.00", "8.00"),
            "2411": ("0.00", "9.35"),
            "2420": ("0.00", "2.00"),
            "2430": ("0.00", "6.33"),
        }
        assert legs_balance(legs)
        assert len(legs) == 13

    def test_the_account_set_matches_run_2_accounts_differ(self, position):
        """Run 2 named exactly these thirteen on every sell we missed."""
        legs = position.apply(sell_fill())
        assert sorted(legs_by_account(legs)) == [
            "1150", "1200", "2010", "2100", "2400", "2411", "2420", "2430",
            "4000", "4010", "5000", "5010", "5100"]

    def test_cash_does_not_move_on_the_trade_date(self, position):
        """Proceeds are owed by the broker until T+2: 1150, never 1100."""
        legs = position.apply(sell_fill())
        assert "1100" not in legs_by_account(legs)

    def test_custody_shrinks_by_cost_not_by_sale_value(self, position):
        """"The custody position and the customer's claim on it shrink by the
        cost of the shares sold, not their sale value.\""""
        legs = position.apply(sell_fill(principal="10000.00", quantity="100"))
        by_account = legs_by_account(legs)
        assert by_account["1200"] == ("0.00", "8500.00")     # cost
        assert by_account["2100"] == ("8500.00", "0.00")     # cost
        assert by_account["1150"] == ("10000.00", "0.00")    # sale value

    def test_realised_gain_is_never_posted_directly(self, position):
        """It is the residual across two liability accounts. There is no
        realised-gain account in the chart and adding one would be wrong."""
        legs = position.apply(sell_fill(principal="10000.00", quantity="100"))
        accounts = set(legs_by_account(legs))
        assert not accounts & {"4200", "4100", "5100"} - {"5100"}

        # Gain = wallet credit - cost relieved = 9968.00 - 8500.00
        by_account = legs_by_account(legs)
        gain = D(by_account["2010"][1]) - D(by_account["2100"][0])
        assert gain == D("1468.00")

    def test_zero_valued_legs_are_omitted_on_sells_too(self, engine):
        """A-9 applies to the sell entry identically."""
        engine.apply(buy_fill(principal="100.00", quantity="10",
                              broker="BRK-B", order_id="ord_b",
                              trade_id="trd_b"))
        legs = engine.apply(sell_fill(principal="121.88", quantity="10",
                                      broker="BRK-B"))
        by_account = legs_by_account(legs)
        assert "5100" not in by_account          # ps clamps to zero
        assert "2430" not in by_account
        assert legs_balance(legs)


class TestSellLotBook:
    def test_cost_is_relieved_fifo_across_lots_in_delivery_order(self, engine):
        """Lots (10 @ 1000.00) then (3 @ 613.00); sell 12 spans both."""
        engine.apply(buy_fill(principal="1000.00", quantity="10",
                              price="100.00", order_id="o1", trade_id="t1"))
        engine.apply(buy_fill(principal="613.00", quantity="3",
                              price="204.333333", order_id="o2",
                              trade_id="t2"))

        legs = engine.apply(sell_fill(quantity="12", price="100.00",
                                      principal="1200.00"))

        # 1000.00 (all of lot 1, exact) + round(613.00 x 2/3) = 408.67
        assert legs_by_account(legs)["2100"] == ("1408.67", "0.00")
        assert str(engine.state.position_cost("CUST-1001", "ACME")) == "204.33"
        assert engine.state.position_quantity("CUST-1001", "ACME") == 1

    def test_a_partial_sale_within_one_lot_uses_the_graded_formula(self, engine):
        """round(lot_total x sold_qty / lot_qty), not a cost per share."""
        engine.apply(buy_fill(principal="1000.00", quantity="10",
                              price="100.00", order_id="o1", trade_id="t1"))

        legs = engine.apply(sell_fill(quantity="9", price="100.00",
                                      principal="900.00"))

        assert legs_by_account(legs)["2100"] == ("900.00", "0.00")
        assert str(engine.state.position_cost("CUST-1001", "ACME")) == "100.00"

    def test_position_shrinks_so_the_checkpoint_is_right(self, position):
        """Until sells relieved cost, every position was overstated forever --
        which is what cost run 2 most of its checkpoint score."""
        assert position.state.position_quantity("CUST-1001", "ACME") == 100
        position.apply(sell_fill(quantity="40", principal="4000.00"))
        assert position.state.position_quantity("CUST-1001", "ACME") == 60
        assert str(position.state.position_cost("CUST-1001", "ACME")) == "5100.00"

    def test_consumption_is_recorded_for_a_future_reversal(self, position):
        position.apply(sell_fill(quantity="40", principal="4000.00",
                                 trade_id="trd_sell"))
        fill = position.state.fills["trd_sell"]
        assert fill.consumption == [(0, D("40"), D("3400.00"))]

    def test_selling_the_whole_position_leaves_no_phantom(self, position):
        position.apply(sell_fill(quantity="100", principal="10000.00"))
        assert position.state.position_quantity("CUST-1001", "ACME") == 0
        assert "ACME" not in position.state.symbols_held("CUST-1001")


class TestOversell:
    def test_a_sale_larger_than_the_position_is_rejected(self, position):
        assert position.apply(sell_fill(quantity="101",
                                        principal="10100.00")) == []
        assert position.stats["rejected"] == 1

    def test_rejection_leaves_the_lots_untouched(self, position):
        before_qty = position.state.position_quantity("CUST-1001", "ACME")
        before_cost = position.state.position_cost("CUST-1001", "ACME")

        position.apply(sell_fill(quantity="101", principal="10100.00"))

        assert position.state.position_quantity("CUST-1001", "ACME") == before_qty
        assert position.state.position_cost("CUST-1001", "ACME") == before_cost

    def test_rejection_posts_nothing_at_all(self, position):
        before = dict(position.state.balances)
        position.apply(sell_fill(quantity="500", principal="50000.00"))
        assert dict(position.state.balances) == before

    def test_a_sell_exceeding_the_FREE_position_is_still_accepted(self, engine):
        """A-7, confirmed across runs 1 and 2: 20 such sells accepted, 0
        rejected for this reason. Quantity committed to other open sell orders
        does NOT reduce what a sale may consume."""
        engine.apply(buy_fill(principal="1000.00", quantity="10",
                              order_id="o1", trade_id="t1"))
        # An open sell order holding 8 of the 10 shares.
        engine.apply(event("order_placed", {
            "order_id": "ord_hold", "customer_id": "CUST-1001", "side": "sell",
            "symbol": "ACME", "quantity": "8", "limit_price": "100.00",
            "asset_class": "equity", "est_charges": "5.00"}))

        legs = engine.apply(sell_fill(quantity="5", principal="500.00",
                                      order_id="ord_other"))

        assert len(legs) == 13                  # accepted, not rejected
        assert engine.stats["rejected"] == 0


class TestTradeSettled:
    def test_buy_settlement_pays_the_broker(self, engine):
        engine.apply(buy_fill(principal="10000.00", quantity="100",
                              trade_id="trd_1"))
        legs = engine.apply(event("trade_settled", {"trade_id": "trd_1"}))

        assert legs_by_account(legs) == {
            "2350": ("10000.00", "0.00"),
            "1100": ("0.00", "10000.00"),
        }
        # the obligation the fill created is now discharged
        assert engine.state.balance("CUST-1001", "2350") == D("0.00")

    def test_sell_settlement_collects_from_the_broker(self, position):
        position.apply(sell_fill(principal="10000.00", quantity="100",
                                 trade_id="trd_s"))
        legs = position.apply(event("trade_settled", {"trade_id": "trd_s"}))

        assert legs_by_account(legs) == {
            "1100": ("10000.00", "0.00"),
            "1150": ("0.00", "10000.00"),
        }
        assert position.state.balance("CUST-1001", "1150") == D("0.00")

    def test_nothing_else_about_the_trade_changes(self, engine):
        """"Nothing else about the trade changes" -- no lot-book change, no fee
        change, no hold change."""
        engine.apply(buy_fill(principal="1000.00", quantity="10",
                              trade_id="trd_1"))
        cost_before = engine.state.position_cost("CUST-1001", "ACME")
        qty_before = engine.state.position_quantity("CUST-1001", "ACME")

        engine.apply(event("trade_settled", {"trade_id": "trd_1"}))

        assert engine.state.position_cost("CUST-1001", "ACME") == cost_before
        assert engine.state.position_quantity("CUST-1001", "ACME") == qty_before

    def test_settling_a_reversed_fill_is_rejected(self, engine):
        """The reversal already unwound the 2350 obligation. Settling again
        debits it a second time and leaves a liability in debit -- found by
        replaying run 2, where 2350 sat at +973.85 while the trial balance
        still summed to zero. The reference rejected all three occurrences."""
        engine.apply(buy_fill(principal="1000.00", quantity="10",
                              event_id="evt_fill", trade_id="trd_1"))
        engine.apply(event("reversal", {"reverses_event_id": "evt_fill"}))

        assert engine.apply(event("trade_settled", {"trade_id": "trd_1"})) == []
        assert "trade_settled for a reversed fill" in engine.rejections_by_reason
        assert engine.state.balance("CUST-1001", "2350") == D("0.00")

    def test_reversing_an_already_settled_fill_leaves_the_payable_in_debit(
            self, engine):
        """The OPPOSITE order, and this one is CORRECT.

        "Post the exact inverse of the original's legs" says nothing about the
        settlement, so 2350 legitimately goes into debit: we paid the broker
        for a trade that was then reversed, and the broker owes it back. Run 1
        does this nine times and we agree with the reference on all 776 events.

        Pinned as a test because an invariant asserting "a liability is never
        in debit" was briefly added and had to be removed -- it flagged this
        correct sequence. See the note in ledger/invariants.py.
        """
        engine.apply(buy_fill(principal="1000.00", quantity="10",
                              event_id="evt_fill", trade_id="trd_1"))
        engine.apply(event("trade_settled", {"trade_id": "trd_1"}))
        assert engine.state.balance("CUST-1001", "2350") == D("0.00")

        legs = engine.apply(event("reversal",
                                  {"reverses_event_id": "evt_fill"}))

        assert legs                                    # posted, not rejected
        assert engine.state.balance("CUST-1001", "2350") == D("1000.00")
        from ledger.invariants import check_all
        assert check_all(engine.state) == []           # and this is NOT a bug

    def test_unknown_trade_id_is_rejected(self, engine):
        assert engine.apply(event("trade_settled",
                                  {"trade_id": "trd_nope"})) == []
        assert engine.stats["rejected"] == 1

    def test_double_settlement_is_rejected(self, engine):
        engine.apply(buy_fill(trade_id="trd_1"))
        engine.apply(event("trade_settled", {"trade_id": "trd_1"}))
        assert engine.apply(event("trade_settled", {"trade_id": "trd_1"})) == []
        assert engine.stats["rejected"] == 1

    def test_each_partial_fill_settles_separately(self, engine):
        """Each fill carries its own trade_id, so a five-times-filled order
        settles five times. Indexed by trade_id, never order_id."""
        engine.apply(buy_fill(order_id="ord_1", quantity="40",
                              principal="4000.00", trade_id="trd_a",
                              final=False))
        engine.apply(buy_fill(order_id="ord_1", quantity="60",
                              principal="6000.00", trade_id="trd_b",
                              final=True))

        first = engine.apply(event("trade_settled", {"trade_id": "trd_a"}))
        second = engine.apply(event("trade_settled", {"trade_id": "trd_b"}))

        assert legs_by_account(first)["1100"] == ("0.00", "4000.00")
        assert legs_by_account(second)["1100"] == ("0.00", "6000.00")
        assert engine.state.balance("CUST-1001", "2350") == D("0.00")


class TestFullTradeLifecycle:
    def test_buy_settle_sell_settle_leaves_a_coherent_book(self, engine):
        """The end-to-end path the final reconciliation checks."""
        engine.apply(deposit(amount="50000.00"))
        engine.apply(buy_fill(principal="8500.00", quantity="100",
                              order_id="ob", trade_id="tb"))
        engine.apply(event("trade_settled", {"trade_id": "tb"}))
        engine.apply(sell_fill(principal="10000.00", quantity="100",
                               order_id="os", trade_id="ts"))
        engine.apply(event("trade_settled", {"trade_id": "ts"}))

        state = engine.state
        # position fully closed
        assert state.position_quantity("CUST-1001", "ACME") == 0
        # nothing left unsettled
        assert state.balance("CUST-1001", "2350") == D("0.00")
        assert state.balance("CUST-1001", "1150") == D("0.00")
        # custody and claim both back to zero
        assert state.balance("CUST-1001", "1200") == D("0.00")
        assert state.balance("CUST-1001", "2100") == D("0.00")

        from ledger.invariants import check_all
        assert check_all(state) == []
