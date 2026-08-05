"""The capture queries, against hand-made fixtures.

These exist so that run 1's 800-event window is extracted mechanically. A query
that has never fired is a query we do not know works, and we get one shot.

Every query must distinguish ABSENT from ANSWERED: "the stream did not contain
the case" and "the case behaves as we assumed" are very different positions.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from conftest import buy_fill, deposit, event                       # noqa: E402
from scan import (scan_a3_interest, scan_a5_reversal_of_consumed_buy,  # noqa: E402
                  scan_a7_oversell_boundary, scan_a9_zero_legs,
                  scan_all, scan_types)


def sell_fill(customer_id="CUST-1001", symbol="ACME", quantity="10",
              principal="1000.00", broker="BRK-A", order_id="ord_s",
              final=True, **kw):
    ev = buy_fill(customer_id=customer_id, symbol=symbol, quantity=quantity,
                  principal=principal, broker=broker, order_id=order_id,
                  final=final, **kw)
    ev["payload"]["side"] = "sell"
    return ev


def placement(order_id="ord_p", customer_id="CUST-1001", symbol="ACME",
              side="sell", quantity="10", limit_price="100.00", **kw):
    return event("order_placed", {
        "order_id": order_id, "customer_id": customer_id, "side": side,
        "symbol": symbol, "quantity": quantity, "limit_price": limit_price,
        "asset_class": "equity", "est_charges": "5.00",
    }, **kw)


class TestTypeHistogram:
    def test_counts_and_flags_unknown_types(self):
        report = scan_types([deposit(), deposit(), buy_fill(),
                             event("teleportation", {})])
        assert report["counts"]["deposit"] == 2
        assert report["unknown_to_us"] == ["teleportation"]

    def test_reports_declared_types_we_never_saw(self):
        report = scan_types([deposit()])
        assert "stock_split" in report["declared_but_absent"]

    def test_checkpoint_request_is_not_counted_as_a_ledger_type(self):
        report = scan_types([deposit(), event("checkpoint_request", {})])
        assert report["distinct_ledger_types"] == 1


class TestA3InterestShare:
    def test_absent_is_reported_distinctly(self):
        assert scan_a3_interest([deposit()])["verdict"].startswith("ABSENT")

    def test_detects_an_amount(self):
        events = [event("interest_credited", {
            "customer_id": "C", "gross_amount": "12.50", "customer_share": "10.00",
        })]
        assert "AMOUNT" in scan_a3_interest(events)["verdict"]

    def test_detects_a_rate(self):
        events = [event("interest_credited", {
            "customer_id": "C", "gross_amount": "12.50", "customer_share": "0.80",
        })]
        assert "RATE" in scan_a3_interest(events)["verdict"]


class TestA5ReversalOfConsumedBuy:
    def test_absent_says_so_and_says_it_is_not_a_resolution(self):
        report = scan_a5_reversal_of_consumed_buy([buy_fill(), deposit()])
        assert report["verdict"].startswith("ABSENT")
        assert "NOT a resolution" in report["verdict"]

    def test_reversal_of_an_unconsumed_buy_is_not_a_candidate(self):
        buy = buy_fill(event_id="buy1")
        rev = event("reversal", {"reverses_event_id": "buy1"})
        report = scan_a5_reversal_of_consumed_buy([buy, rev])
        assert report["reversals_of_buy_fills"] == 1
        assert report["candidates"] == []

    def test_detects_a_sell_between_the_buy_and_its_reversal(self):
        events = [
            buy_fill(event_id="buy1", symbol="ACME"),
            sell_fill(symbol="ACME"),
            event("reversal", {"reverses_event_id": "buy1"}),
        ]
        report = scan_a5_reversal_of_consumed_buy(events)
        assert report["verdict"].startswith("PRESENT")
        assert report["candidates"][0]["reverses"] == "buy1"

    def test_a_sell_in_a_different_symbol_does_not_count(self):
        events = [
            buy_fill(event_id="buy1", symbol="ACME"),
            sell_fill(symbol="OTHER"),
            event("reversal", {"reverses_event_id": "buy1"}),
        ]
        assert scan_a5_reversal_of_consumed_buy(events)["candidates"] == []

    def test_a_sell_after_the_reversal_does_not_count(self):
        events = [
            buy_fill(event_id="buy1", symbol="ACME"),
            event("reversal", {"reverses_event_id": "buy1"}),
            sell_fill(symbol="ACME"),
        ]
        assert scan_a5_reversal_of_consumed_buy(events)["candidates"] == []


class TestA7OversellBoundary:
    def test_absent_is_reported_distinctly(self):
        report = scan_a7_oversell_boundary([buy_fill(quantity="100")])
        assert report["verdict"].startswith("ABSENT")

    def test_detects_a_sell_that_divides_the_two_readings(self):
        """Position 100, an open sell order holding 60, then a sell of 50:
        under 'total position' it is fine, under 'un-held position' it is an
        oversell. Exactly the case that decides A-7."""
        events = [
            buy_fill(quantity="100", symbol="ACME"),
            placement(order_id="ord_hold", quantity="60", symbol="ACME"),
            sell_fill(quantity="50", symbol="ACME", order_id="ord_other"),
        ]
        report = scan_a7_oversell_boundary(events)
        assert report["verdict"].startswith("PRESENT")
        hit = report["divergent_sells"][0]
        assert hit["position"] == "100" and hit["free"] == "40"

    def test_a_sell_within_the_free_balance_is_not_divergent(self):
        events = [
            buy_fill(quantity="100", symbol="ACME"),
            placement(order_id="ord_hold", quantity="60", symbol="ACME"),
            sell_fill(quantity="30", symbol="ACME", order_id="ord_other"),
        ]
        assert scan_a7_oversell_boundary(events)["divergent_sells"] == []

    def test_a_true_oversell_is_not_divergent_either(self):
        """Both readings reject a sell larger than the whole position, so it
        tells us nothing about A-7."""
        events = [
            buy_fill(quantity="100", symbol="ACME"),
            sell_fill(quantity="150", symbol="ACME"),
        ]
        assert scan_a7_oversell_boundary(events)["divergent_sells"] == []

    def test_a_cancelled_sell_order_releases_its_share_hold(self):
        events = [
            buy_fill(quantity="100", symbol="ACME"),
            placement(order_id="ord_hold", quantity="60", symbol="ACME"),
            event("order_cancelled", {"order_id": "ord_hold"}),
            sell_fill(quantity="50", symbol="ACME", order_id="ord_other"),
        ]
        assert scan_a7_oversell_boundary(events)["divergent_sells"] == []


class TestA9ZeroLegs:
    def test_absent_keeps_a9_open_rather_than_confirming_it(self):
        report = scan_a9_zero_legs([buy_fill(principal="10000.00",
                                             broker="BRK-A")])
        a9a = report["a9a_zero_partner_share"]
        assert a9a["verdict"].startswith("ABSENT")
        assert "stays OPEN" in a9a["verdict"]

    def test_detects_a_loss_making_brk_b_fill(self):
        """A-9a: BRK-B under ~3,333 principal is the only way ps hits 0.00."""
        report = scan_a9_zero_legs([buy_fill(principal="3250.00",
                                             broker="BRK-B")])
        a9a = report["a9a_zero_partner_share"]
        assert a9a["verdict"].startswith("PRESENT")
        assert a9a["n_buys"] == 1
        assert a9a["samples"][0]["ps"] == "0.00"

    def test_a_profitable_brk_a_fill_does_not_settle_a9a(self):
        """The trap: filtering on 'buy fill' alone gets a full-legs diagnostic
        back that contains no zero leg and settles nothing."""
        report = scan_a9_zero_legs([buy_fill(principal="10000.00",
                                             broker="BRK-A")])
        assert report["a9a_zero_partner_share"]["n_total"] == 0

    def test_detects_a_tiny_principal_fill_independently(self):
        """A-9b: different trigger entirely -- no loss required, any broker."""
        report = scan_a9_zero_legs([buy_fill(principal="30.00", broker="BRK-C",
                                             quantity="1", price="30.00")])
        a9b = report["a9b_zero_custody_or_regulatory"]
        assert a9b["verdict"].startswith("PRESENT")
        assert a9b["samples"][0]["cc"] == "0.00"

    def test_a9a_and_a9b_are_independent(self):
        """A capture can answer one and not the other. Confirming the partner
        share tells us nothing about a zero custody leg."""
        report = scan_a9_zero_legs([buy_fill(principal="3250.00",
                                             broker="BRK-B")])
        assert report["a9a_zero_partner_share"]["verdict"].startswith("PRESENT")
        assert report["a9b_zero_custody_or_regulatory"]["verdict"].startswith("ABSENT")

    def test_flags_the_ideal_sub_six_twenty_five_probe(self):
        """One fill under 6.25 zeroes r, c and cc at once."""
        report = scan_a9_zero_legs([buy_fill(principal="5.00", broker="BRK-C",
                                             quantity="1", price="5.00")])
        a9b = report["a9b_zero_custody_or_regulatory"]
        assert len(a9b["ideal_probes_under_6_25"]) == 1
        probe = a9b["ideal_probes_under_6_25"][0]
        assert probe["r"] == "0.00" and probe["c"] == "0.00" and probe["cc"] == "0.00"


class TestScanAll:
    def test_runs_end_to_end_on_a_mixed_capture(self):
        events = [deposit(), buy_fill(), sell_fill(),
                  event("interest_credited", {"customer_id": "C",
                                              "gross_amount": "10.00",
                                              "customer_share": "8.00"})]
        report = scan_all(events)
        assert report["n_events"] == 4
        assert set(report) >= {"types", "a3_interest_share",
                               "a5_reversal_of_consumed_buy",
                               "a7_oversell_boundary", "a9_zero_legs"}

    def test_survives_malformed_payloads(self):
        """The stream deliberately sends payloads that will not parse. A scan
        that dies on one is a scan that loses the whole capture."""
        events = [
            {"event_id": "x", "type": "order_filled",
             "payload": {"side": "buy", "principal": "not-a-number",
                         "broker": "BRK-A", "partner_rate": "0.5"}},
            {"event_id": "y", "type": "deposit"},           # no payload at all
            {"event_id": "z"},                              # no type either
        ]
        report = scan_all(events)
        assert report["n_events"] == 3
