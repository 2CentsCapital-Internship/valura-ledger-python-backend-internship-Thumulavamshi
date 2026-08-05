"""Monitors for the two rules that rest on judgement rather than evidence.

A-5 (reversal of a partially consumed lot) and A-14 (zero/negative-spread
fx_deposit) have never occurred in ~3,100 events across four practice runs.
That is an absence of evidence, not evidence of safety, so both carry a loud
runtime warning: if either fires for the first time during a scored,
feedback-free attempt, the log is the only way we will ever know.

These tests exist because a warning nobody has ever seen fire is a warning
nobody knows works.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from conftest import buy_fill, event                       # noqa: E402
from scan import scan_a14_fx_spread, scan_a5_reversal_of_consumed_buy  # noqa: E402
from ledger.money import D                                 # noqa: E402


def sell_fill(quantity="4", principal="400.00", symbol="ACME", **kw):
    ev = buy_fill(quantity=quantity, principal=principal, symbol=symbol,
                  order_id="ord_s", **kw)
    ev["payload"]["side"] = "sell"
    return ev


class TestA5DivergenceWarning:
    def test_fires_when_a_partially_consumed_lot_is_reversed(self, engine, caplog):
        """THE case we have never seen. Buy 10, sell 4, then reverse the buy:
        surgical undo removes the remaining 6, but the cost the sell already
        relieved stands. Full recompute would answer differently."""
        engine.apply(buy_fill(principal="1000.00", quantity="10",
                              event_id="evt_buy", trade_id="t1"))
        engine.apply(sell_fill(quantity="4", principal="500.00",
                               trade_id="t2"))

        engine.apply(event("reversal", {"reverses_event_id": "evt_buy"}))

        assert "A5-DIVERGENCE" in caplog.text
        assert "original_qty=10" in caplog.text
        assert "remaining_qty=6" in caplog.text
        assert "consumed=4" in caplog.text

    def test_silent_when_the_lot_is_untouched(self, engine, caplog):
        """The common case, where surgical undo and recompute agree exactly.
        A warning here would be noise and would train us to ignore it."""
        engine.apply(buy_fill(principal="1000.00", quantity="10",
                              event_id="evt_buy", trade_id="t1"))
        engine.apply(event("reversal", {"reverses_event_id": "evt_buy"}))

        assert "A5-DIVERGENCE" not in caplog.text

    def test_fires_for_a_fully_consumed_lot_too(self, engine, caplog):
        """Fully consumed is also a divergence: there is nothing left to
        remove, so the relief the sells computed stands unchallenged."""
        engine.apply(buy_fill(principal="1000.00", quantity="10",
                              event_id="evt_buy", trade_id="t1"))
        engine.apply(sell_fill(quantity="10", principal="1200.00",
                               trade_id="t2"))

        engine.apply(event("reversal", {"reverses_event_id": "evt_buy"}))

        assert "A5-DIVERGENCE" in caplog.text
        assert "remaining_qty=0" in caplog.text

    def test_original_quantity_is_never_mutated(self, engine):
        engine.apply(buy_fill(principal="1000.00", quantity="10",
                              trade_id="t1"))
        engine.apply(sell_fill(quantity="4", principal="500.00",
                               trade_id="t2"))
        lot = engine.state.lots_for("CUST-1001", "ACME")[0]
        assert lot.original_quantity == D("10")
        assert lot.quantity == D("6")


class TestA14FxWarning:
    def _fx(self, market, customer, **kw):
        return event("fx_deposit", {
            "customer_id": "CUST-1001", "currency": "EUR",
            "amount_foreign": "100.00", "market_rate": "1.00",
            "customer_rate": "1.00",
            "usd_at_market_rate": market,
            "usd_at_customer_rate": customer}, **kw)

    def test_fires_on_a_negative_spread(self, engine, caplog):
        assert engine.apply(self._fx("100.00", "111.11")) == []
        assert "A14-FIRST" in caplog.text
        assert "NEGATIVE spread" in caplog.text
        assert engine.stats["rejected"] == 1

    def test_fires_on_an_exactly_zero_spread(self, engine, caplog):
        """Accepted, not rejected -- the rule says "better than", not "not
        worse than" -- but equally untested, so equally loud."""
        legs = engine.apply(self._fx("100.00", "100.00"))
        assert len(legs) == 2                    # the 4100 leg drops as zero
        assert "A14-FIRST" in caplog.text
        assert "ZERO spread" in caplog.text
        assert engine.stats["rejected"] == 0

    def test_silent_on_a_normal_positive_spread(self, engine, caplog):
        legs = engine.apply(self._fx("104.23", "103.77"))
        assert len(legs) == 3
        assert "A14-FIRST" not in caplog.text


class TestScansKeepWatchingInScoredModes:
    """Both scans read only the event log, which the client writes in every
    mode. So submission and final give three more free chances to catch these,
    even though neither returns any per-event feedback."""

    def test_a5_scan_reports_absent_distinctly_from_confirmed(self):
        report = scan_a5_reversal_of_consumed_buy([buy_fill()])
        assert report["verdict"].startswith("ABSENT")
        assert "NOT a resolution" in report["verdict"]

    def test_a5_scan_detects_the_case(self):
        events = [buy_fill(event_id="b1", symbol="ACME"),
                  sell_fill(symbol="ACME"),
                  event("reversal", {"reverses_event_id": "b1"})]
        assert scan_a5_reversal_of_consumed_buy(events)["verdict"].startswith(
            "PRESENT")

    def test_a14_scan_reports_absent_distinctly(self):
        ok = event("fx_deposit", {
            "customer_id": "C", "currency": "GBP", "amount_foreign": "8758.00",
            "market_rate": "84.0267", "customer_rate": "84.40",
            "usd_at_market_rate": "104.23", "usd_at_customer_rate": "103.77"})
        report = scan_a14_fx_spread([ok])
        assert report["verdict"].startswith("ABSENT")
        assert "UNTESTED" in report["verdict"]
        assert report["n_fx_deposits"] == 1

    def test_a14_scan_detects_a_negative_spread(self):
        bad = event("fx_deposit", {
            "customer_id": "C", "currency": "EUR", "amount_foreign": "100.00",
            "market_rate": "1.00", "customer_rate": "0.90",
            "usd_at_market_rate": "100.00", "usd_at_customer_rate": "111.11"})
        report = scan_a14_fx_spread([bad])
        assert report["verdict"].startswith("PRESENT")
        assert len(report["negative_spread"]) == 1

    def test_a14_scan_detects_a_zero_spread(self):
        flat = event("fx_deposit", {
            "customer_id": "C", "currency": "EUR", "amount_foreign": "100.00",
            "market_rate": "1.00", "customer_rate": "1.00",
            "usd_at_market_rate": "100.00", "usd_at_customer_rate": "100.00"})
        report = scan_a14_fx_spread([flat])
        assert report["verdict"].startswith("PRESENT")
        assert len(report["zero_spread"]) == 1


class TestSettlementOfARejectedFill:
    """Item 2, resolved: 'trade_settled for an unknown trade_id' is correct
    behaviour and follows directly from an earlier rejection.

    Across runs 3 and 4 all seven occurrences settled a fill we had rejected as
    an OVERSELL, and the reference agreed with our empty submission 7/7.
    """

    def test_settling_a_fill_we_rejected_as_an_oversell_is_rejected(self, engine):
        engine.apply(buy_fill(principal="100.00", quantity="1",
                              order_id="ob", trade_id="tb"))
        # oversell: only 1 share held
        assert engine.apply(sell_fill(quantity="5", principal="500.00",
                                      trade_id="t_oversold")) == []

        assert engine.apply(event("trade_settled",
                                  {"trade_id": "t_oversold"})) == []
        assert engine.rejections_by_reason[
            "trade_settled for an unknown trade_id"] == 1

    def test_a_rejected_fill_never_enters_the_trade_index(self, engine):
        engine.apply(sell_fill(quantity="5", principal="500.00",
                               trade_id="t_oversold"))
        assert "t_oversold" not in engine.state.fills
