"""Cash and FX events. Payload shapes and rejection rules are taken from the
practice run 1 capture, not invented.
"""
import pytest

from conftest import deposit, event, legs_by_account
from ledger.money import D, legs_balance


def fee(customer_id="CUST-1001", amount="39.90", **kw):
    return event("fee_charged", {"customer_id": customer_id,
                                 "amount": amount}, **kw)


def withdraw(withdrawal_id="wdr_1", customer_id="CUST-1001",
             amount="2422.97", **kw):
    return event("withdrawal_requested", {
        "withdrawal_id": withdrawal_id, "customer_id": customer_id,
        "amount": amount}, **kw)


class TestFeeCharged:
    def test_wallet_down_cash_down(self, engine):
        """"The customer pays the firm's fee out of their wallet; the cash
        leaves the omnibus account." No income account moves."""
        legs = engine.apply(fee(amount="39.90"))
        assert legs_by_account(legs) == {
            "2010": ("39.90", "0.00"),
            "1100": ("0.00", "39.90"),
        }
        assert legs_balance(legs)

    def test_no_revenue_is_booked(self, engine):
        """Booking to 4000 is contradicted by "the cash leaves the omnibus":
        retained revenue would leave the cash where it is."""
        legs = engine.apply(fee())
        assert "4000" not in legs_by_account(legs)

    def test_unparseable_amount_is_rejected(self, engine):
        """The task sheet's "malformed: a payload that will not parse -> reject
        it, carry on", and the feed means it literally.

        Audited across all five practice captures: **7 rejections, every one
        carrying the exact string "not-a-number"** -- the only distinct bad
        value ever seen. Unparseable under every normalisation worth trying
        (stripped, thousands separators removed, currency symbol removed), so
        it is not a parser gap. The reference agreed with our empty submission
        **7 / 7**.

        Control against over-rejection: 389 fee_charged events across those
        captures, 382 posted.
        """
        assert engine.apply(fee(amount="not-a-number")) == []
        assert engine.stats["rejected"] == 1
        assert engine.state.balances == {}

    @pytest.mark.parametrize("amount,should_post", [
        ("39.90", True),        # ordinary
        ("0.01", True),         # smallest posting amount
        ("  12.34  ", True),    # dec() strips whitespace
        ("1E+2", True),         # exponent notation still parses, if it appears
        ("not-a-number", False),
        ("", False),
        ("12.34.56", False),
    ])
    def test_parser_accepts_everything_decimal_can_read(self, engine, amount,
                                                        should_post):
        """Guards the boundary from the other side: we must reject ONLY what is
        genuinely unreadable. Rejecting a legitimate format would silently drop
        a real fee, and over-rejection is the expensive failure mode."""
        legs = engine.apply(fee(amount=amount))
        assert bool(legs) is should_post

    def test_non_positive_amount_is_rejected(self, engine):
        assert engine.apply(fee(amount="0.00")) == []
        assert engine.apply(fee(amount="-5.00")) == []
        assert engine.stats["rejected"] == 2


class TestFeeRefund:
    def test_exact_inverse_of_the_original(self, engine):
        """The amount is NOT in the payload -- it comes from the fee_charged
        event named by refunds_source_id."""
        engine.apply(fee(amount="39.90", event_id="evt_fee"))
        legs = engine.apply(event("fee_refund", {
            "customer_id": "CUST-1001", "refunds_source_id": "evt_fee"}))

        assert legs_by_account(legs) == {
            "1100": ("39.90", "0.00"),
            "2010": ("0.00", "39.90"),
        }
        # the pair nets the wallet and the omnibus back to zero
        assert engine.state.balance("CUST-1001", "1100") == D("0.00")
        assert engine.state.balance("CUST-1001", "2010") == D("0.00")

    def test_unknown_source_is_rejected(self, engine):
        assert engine.apply(event("fee_refund", {
            "customer_id": "CUST-1001",
            "refunds_source_id": "evt_never_seen"})) == []
        assert engine.stats["rejected"] == 1

    def test_refunding_the_same_fee_twice_is_an_error(self, engine):
        engine.apply(fee(amount="10.00", event_id="evt_fee"))
        first = engine.apply(event("fee_refund", {
            "customer_id": "CUST-1001", "refunds_source_id": "evt_fee"}))
        second = engine.apply(event("fee_refund", {
            "customer_id": "CUST-1001", "refunds_source_id": "evt_fee"}))

        assert len(first) == 2
        assert second == []
        assert engine.stats["rejected"] == 1

    def test_posts_against_the_originals_customer_on_disagreement(
            self, engine, caplog):
        engine.apply(fee(customer_id="CUST-1001", amount="10.00",
                         event_id="evt_fee"))
        legs = engine.apply(event("fee_refund", {
            "customer_id": "CUST-9999", "refunds_source_id": "evt_fee"}))

        assert {l["customer_id"] for l in legs} == {"CUST-1001"}
        assert "fee_refund customer" in caplog.text


class TestWithdrawals:
    def test_request_reclassifies_one_liability_into_another(self, engine):
        """"The money has left the customer's wallet but has not yet left the
        broker." NO asset moves."""
        legs = engine.apply(withdraw(amount="2422.97"))
        assert legs_by_account(legs) == {
            "2010": ("2422.97", "0.00"),
            "2300": ("0.00", "2422.97"),
        }
        assert "1100" not in legs_by_account(legs)

    def test_settlement_looks_the_amount_up_and_moves_cash(self, engine):
        engine.apply(withdraw(withdrawal_id="wdr_1", amount="2422.97"))
        legs = engine.apply(event("withdrawal_settled",
                                  {"withdrawal_id": "wdr_1"}))
        assert legs_by_account(legs) == {
            "2300": ("2422.97", "0.00"),
            "1100": ("0.00", "2422.97"),
        }

    def test_rejection_returns_it_to_the_wallet_with_no_cash_movement(self, engine):
        """"No cash moved at any point" -- the trap is posting a cash
        round-trip that never happened."""
        engine.apply(withdraw(withdrawal_id="wdr_1", amount="500.00"))
        legs = engine.apply(event("withdrawal_rejected",
                                  {"withdrawal_id": "wdr_1"}))
        assert legs_by_account(legs) == {
            "2300": ("500.00", "0.00"),
            "2010": ("0.00", "500.00"),
        }
        assert "1100" not in legs_by_account(legs)
        assert engine.state.balance("CUST-1001", "1100") == D("0.00")

    def test_request_then_rejection_leaves_the_wallet_whole(self, engine):
        engine.apply(deposit(amount="1000.00"))
        engine.apply(withdraw(withdrawal_id="wdr_1", amount="400.00"))
        engine.apply(event("withdrawal_rejected", {"withdrawal_id": "wdr_1"}))

        assert engine.state.credit_balance("CUST-1001", "2010") == D("1000.00")
        assert engine.state.balance("CUST-1001", "2300") == D("0.00")

    @pytest.mark.parametrize("event_type", ["withdrawal_settled",
                                            "withdrawal_rejected"])
    def test_unknown_withdrawal_id_is_rejected(self, engine, event_type):
        assert engine.apply(event(event_type, {"withdrawal_id": "wdr_nope"})) == []
        assert engine.stats["rejected"] == 1

    @pytest.mark.parametrize("second", ["withdrawal_settled",
                                        "withdrawal_rejected"])
    def test_a_withdrawal_can_only_resolve_once(self, engine, second):
        engine.apply(withdraw(withdrawal_id="wdr_1"))
        engine.apply(event("withdrawal_settled", {"withdrawal_id": "wdr_1"}))

        assert engine.apply(event(second, {"withdrawal_id": "wdr_1"})) == []
        assert engine.stats["rejected"] == 1

    def test_duplicate_withdrawal_id_on_a_new_request_is_rejected(self, engine):
        engine.apply(withdraw(withdrawal_id="wdr_1", amount="100.00"))
        assert engine.apply(withdraw(withdrawal_id="wdr_1",
                                     amount="999.00")) == []
        assert engine.stats["rejected"] == 1


class TestInterestCredited:
    def test_firm_keeps_the_remainder_as_income(self, engine):
        """Real payload from the capture: gross 332.92, share 259.68."""
        legs = engine.apply(event("interest_credited", {
            "customer_id": "CUST-1010", "gross_amount": "332.92",
            "customer_share": "259.68"}))

        assert legs_by_account(legs) == {
            "1100": ("332.92", "0.00"),
            "2010": ("0.00", "259.68"),
            "4200": ("0.00", "73.24"),
        }
        assert legs_balance(legs)

    def test_customer_share_is_an_amount_not_a_rate(self, engine):
        """A-3 CONFIRMED by run 1: 41 samples, ratios 0.43-0.83, every value
        greater than 1, none exceeding gross. A rate reading would have
        credited the customer 0.78 instead of 259.68."""
        legs = engine.apply(event("interest_credited", {
            "customer_id": "C", "gross_amount": "332.92",
            "customer_share": "259.68"}))
        assert legs_by_account(legs)["2010"] == ("0.00", "259.68")

    def test_this_is_not_a_pass_through(self, engine):
        legs = engine.apply(event("interest_credited", {
            "customer_id": "C", "gross_amount": "100.00",
            "customer_share": "75.00"}))
        assert legs_by_account(legs)["4200"] == ("0.00", "25.00")

    def test_the_firm_share_is_the_residual_so_it_always_balances(self, engine):
        """Half-cent inputs: the residual absorbs the cent."""
        legs = engine.apply(event("interest_credited", {
            "customer_id": "C", "gross_amount": "12.21",
            "customer_share": "9.16"}))
        assert legs_by_account(legs)["4200"] == ("0.00", "3.05")
        assert legs_balance(legs)

    def test_a_full_pass_through_drops_the_zero_income_leg(self, engine):
        """A-9: zero-valued legs are omitted."""
        legs = engine.apply(event("interest_credited", {
            "customer_id": "C", "gross_amount": "50.00",
            "customer_share": "50.00"}))
        assert "4200" not in legs_by_account(legs)
        assert len(legs) == 2

    def test_share_exceeding_gross_is_flagged_not_rejected(self, engine, caplog):
        legs = engine.apply(event("interest_credited", {
            "customer_id": "C", "gross_amount": "10.00",
            "customer_share": "12.00"}))
        assert len(legs) == 3
        assert "customer_share" in caplog.text


class TestTransferBetweenCustomers:
    def test_both_legs_land_on_2010_and_the_account_nets_to_zero(self, engine):
        """The canary for per-customer keying."""
        legs = engine.apply(event("transfer_between_customers", {
            "from_customer_id": "CUST-1000", "to_customer_id": "CUST-1009",
            "amount": "2309.67"}))

        assert [(l["account"], l["customer_id"], l["debit"], l["credit"])
                for l in legs] == [
            ("2010", "CUST-1000", "2309.67", "0.00"),
            ("2010", "CUST-1009", "0.00", "2309.67"),
        ]
        assert engine.state.account_total("2010") == D("0.00")

    def test_the_per_customer_split_actually_changes(self, engine):
        """An account-keyed book shows nothing happening at all here."""
        engine.apply(deposit(customer_id="CUST-1000", amount="5000.00"))
        engine.apply(event("transfer_between_customers", {
            "from_customer_id": "CUST-1000", "to_customer_id": "CUST-1009",
            "amount": "2000.00"}))

        assert engine.state.credit_balance("CUST-1000", "2010") == D("3000.00")
        assert engine.state.credit_balance("CUST-1009", "2010") == D("2000.00")

    def test_no_external_cash_moves(self, engine):
        legs = engine.apply(event("transfer_between_customers", {
            "from_customer_id": "A", "to_customer_id": "B", "amount": "10.00"}))
        assert "1100" not in legs_by_account(legs)

    def test_self_transfer_is_flagged_not_rejected(self, engine, caplog):
        legs = engine.apply(event("transfer_between_customers", {
            "from_customer_id": "A", "to_customer_id": "A", "amount": "10.00"}))
        assert len(legs) == 2
        assert "transfer from == to" in caplog.text


class TestFxDeposit:
    def test_omnibus_gets_market_value_customer_gets_their_rate(self, engine):
        """Real payload from the capture: GBP 8758.00, market 84.0267,
        customer 84.40, giving 104.23 and 103.77."""
        legs = engine.apply(event("fx_deposit", {
            "customer_id": "CUST-1009", "currency": "GBP",
            "amount_foreign": "8758.00", "market_rate": "84.0267",
            "customer_rate": "84.40", "usd_at_market_rate": "104.23",
            "usd_at_customer_rate": "103.77"}))

        assert legs_by_account(legs) == {
            "1100": ("104.23", "0.00"),
            "2010": ("0.00", "103.77"),
            "4100": ("0.00", "0.46"),
        }
        assert legs_balance(legs)

    def test_negative_spread_is_rejected(self, engine):
        """"An fx_deposit whose customer rate is better than the market rate is
        rejected. A negative spread is bad data, not a gift.\""""
        assert engine.apply(event("fx_deposit", {
            "customer_id": "C", "currency": "EUR", "amount_foreign": "100.00",
            "market_rate": "1.00", "customer_rate": "0.90",
            "usd_at_market_rate": "100.00",
            "usd_at_customer_rate": "111.11"})) == []
        assert engine.stats["rejected"] == 1
        assert engine.state.balances == {}

    def test_the_comparison_uses_usd_figures_not_raw_rates(self, engine):
        """Run 1 confirmed rates are quoted FOREIGN PER USD (8758 / 84.0267 =
        104.23), so a HIGHER customer_rate means FEWER dollars. Here
        customer_rate is numerically higher yet the customer is worse off --
        a raw-rate comparison would wrongly reject this valid deposit."""
        legs = engine.apply(event("fx_deposit", {
            "customer_id": "C", "currency": "GBP", "amount_foreign": "8758.00",
            "market_rate": "84.0267", "customer_rate": "84.40",
            "usd_at_market_rate": "104.23",
            "usd_at_customer_rate": "103.77"}))
        assert len(legs) == 3
        assert engine.stats["rejected"] == 0

    def test_zero_spread_is_accepted_and_drops_the_revenue_leg(self, engine):
        """A-14: the rule says "better than", not "not worse than"."""
        legs = engine.apply(event("fx_deposit", {
            "customer_id": "C", "currency": "EUR", "amount_foreign": "100.00",
            "market_rate": "1.00", "customer_rate": "1.00",
            "usd_at_market_rate": "100.00",
            "usd_at_customer_rate": "100.00"}))
        assert "4100" not in legs_by_account(legs)
        assert len(legs) == 2
        assert legs_balance(legs)

    def test_spread_is_the_difference_of_the_given_figures(self, engine):
        """Not derived from amount_foreign x rate delta, which can differ by a
        cent -- and where it does, that is a defect signal, not a value."""
        legs = engine.apply(event("fx_deposit", {
            "customer_id": "C", "currency": "JPY", "amount_foreign": "1000000",
            "market_rate": "150.1234", "customer_rate": "151.9999",
            "usd_at_market_rate": "6661.63",
            "usd_at_customer_rate": "6578.95"}))
        assert legs_by_account(legs)["4100"] == ("0.00", "82.68")
        assert legs_balance(legs)
