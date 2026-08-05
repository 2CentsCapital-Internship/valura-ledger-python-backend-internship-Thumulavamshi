"""The fee chain and routing -- the worked examples from LOGIC.md section 3.

"Get the rounding right before you move on; everything downstream inherits it."
"""
import pytest

from ledger.money import D, ZERO, money_str
from ledger.tariff import (BROKERS, Fees, broker_cost, brokerage, candidates,
                           custody, custody_cost, customer_charge, fee_chain,
                           regulatory, route)


def fees_as_strings(f: Fees) -> dict:
    return {
        "b": money_str(f.brokerage), "c": money_str(f.custody),
        "r": money_str(f.regulatory), "bc": money_str(f.broker_cost),
        "cc": money_str(f.custody_cost), "ps": money_str(f.partner_share),
    }


class TestFeeChainWorkedExamples:
    def test_example_1_profitable_buy_with_half_cent_partner_share(self):
        """BRK-A, principal 10,000.00, partner_rate 0.50 (LOGIC.md section 3.2).

        The partner share lands on 6.325 -- exactly a half cent. Banker's
        rounding gives 6.32; float gives whichever way it is represented.
        """
        f = fee_chain(D("10000.00"), "BRK-A", D("0.50"))
        assert fees_as_strings(f) == {
            "b": "20.00", "c": "4.00", "r": "8.00",
            "bc": "9.35", "cc": "2.00", "ps": "6.33",
        }
        assert money_str(f.margin) == "12.65"
        assert money_str(f.customer_charges) == "32.00"
        assert f.payable_account == "2411"

    def test_example_2_loss_making_fill_clamps_partner_share(self):
        """BRK-B, principal 3,250.00. Three independent half-cent roundings
        (4.875, 1.625, 0.975) and a clamped partner share."""
        f = fee_chain(D("3250.00"), "BRK-B", D("0.50"))
        assert fees_as_strings(f) == {
            "b": "4.88", "c": "1.63", "r": "2.60",
            "bc": "5.60", "cc": "0.98", "ps": "0.00",
        }
        assert money_str(f.margin) == "-0.07"
        assert f.payable_account == "2412"

    def test_example_3_minimum_fee_binding(self):
        """BRK-B, principal 100.00. The floor multiplies the customer's
        brokerage charge by roughly 17x."""
        f = fee_chain(D("100.00"), "BRK-B", D("0.50"))
        assert fees_as_strings(f) == {
            "b": "2.50", "c": "0.05", "r": "0.08",
            "bc": "3.08", "cc": "0.03", "ps": "0.00",
        }


class TestPartnerShare:
    def test_no_clawback_when_cost_exceeds_revenue(self):
        f = fee_chain(D("1000.00"), "BRK-B", D("0.50"))
        assert f.margin < ZERO
        assert f.partner_share == ZERO

    def test_zero_margin_gives_zero_share(self):
        f = fee_chain(D("3333.00"), "BRK-B", D("0.50"))
        assert money_str(f.margin) == "0.00"
        assert money_str(f.partner_share) == "0.00"

    def test_ticket_is_inside_cost(self):
        """A-2, provable from the sheet: without the ticket inside cost, no
        broker's fills are ever loss-making, contradicting "the ticket fee
        makes roughly a quarter of all fills loss-making"."""
        f = fee_chain(D("3250.00"), "BRK-B", D("0.50"))
        assert f.broker_cost == D("5.60")                        # 2.60 + 3.00 ticket
        assert f.cost == f.broker_cost + f.custody_cost
        # Strip the ticket and the same fill becomes profitable:
        without_ticket = f.revenue - (f.cost - BROKERS["BRK-B"].ticket)
        assert without_ticket > ZERO
        assert f.margin < ZERO

    @pytest.mark.parametrize("broker_id", ["BRK-A", "BRK-C"])
    @pytest.mark.parametrize("principal", ["1", "10", "50", "100", "500",
                                           "1000", "5000", "50000"])
    def test_brk_a_and_c_are_never_loss_making(self, broker_id, principal):
        """Derived in LOGIC.md section 3.2: only BRK-B fills can lose money.

        A self-check with teeth -- a clamped partner share on BRK-A or BRK-C
        means the fee chain has a bug."""
        f = fee_chain(D(principal), broker_id, D("0.50"))
        assert f.margin > ZERO, f"{broker_id} @ {principal} margin {f.margin}"

    @pytest.mark.parametrize("principal", ["1", "100", "1000", "2000", "3000"])
    def test_brk_b_is_loss_making_below_3333(self, principal):
        f = fee_chain(D(principal), "BRK-B", D("0.50"))
        assert f.margin <= ZERO
        assert f.partner_share == ZERO

    def test_brk_b_is_profitable_above_3333(self):
        f = fee_chain(D("3400.00"), "BRK-B", D("0.50"))
        assert f.margin > ZERO
        assert money_str(f.partner_share) == "0.03"


class TestZeroAmountThresholds:
    """A-9b: which amounts can round to 0.00, and at what principal.

    Zero when principal x rate < 0.005, so a HIGHER rate makes zero HARDER to
    reach. The full table is in LOGIC.md section 3.5.3.
    """

    def test_custody_cost_brk_c_is_zero_below_fifty(self):
        """1 bp -- the widest zero-rounding exposure in the chain."""
        c = BROKERS["BRK-C"]
        assert money_str(custody_cost(D("49.00"), c)) == "0.00"
        assert money_str(custody_cost(D("50.00"), c)) == "0.01"

    def test_custody_revenue_brk_c_is_zero_below_sixteen_sixty_seven(self):
        c = BROKERS["BRK-C"]
        assert money_str(custody(D("16.00"), c)) == "0.00"
        assert money_str(custody(D("17.00"), c)) == "0.01"

    def test_regulatory_is_zero_below_six_twenty_five(self):
        assert money_str(regulatory(D("6.00"))) == "0.00"
        assert money_str(regulatory(D("7.00"))) == "0.01"

    @pytest.mark.parametrize("broker_id", ["BRK-A", "BRK-B", "BRK-C"])
    def test_brokerage_is_never_zero(self, broker_id):
        """Floored at the minimum fee, which is at least 0.50."""
        b = BROKERS[broker_id]
        assert brokerage(D("0.01"), b) == b.min_fee
        assert brokerage(D("0.01"), b) > ZERO

    @pytest.mark.parametrize("broker_id", ["BRK-A", "BRK-B", "BRK-C"])
    def test_broker_cost_is_never_zero(self, broker_id):
        """The ticket fee (at least 0.20) is always added."""
        b = BROKERS[broker_id]
        assert broker_cost(D("0.01"), b) == b.ticket

    def test_tiny_fill_zeroes_r_c_and_cc_at_once(self):
        """A fill under 6.25 principal is the single most efficient A-9b probe:
        three zero legs in one event."""
        f = fee_chain(D("5.00"), "BRK-C", D("0.50"))
        assert money_str(f.regulatory) == "0.00"
        assert money_str(f.custody) == "0.00"
        assert money_str(f.custody_cost) == "0.00"
        # The two structurally immune amounts stay non-zero even here.
        assert money_str(f.brokerage) == "0.50"      # floored at min_fee
        # 5.00 x 12bps = 0.006, which rounds UP to 0.01, plus the 0.20 ticket.
        assert money_str(f.broker_cost) == "0.21"


class TestAssetClassCoverage:
    def test_exactly_two_brokers_per_class(self):
        assert [b.broker_id for b in candidates("equity")] == ["BRK-A", "BRK-B"]
        assert [b.broker_id for b in candidates("etf")] == ["BRK-A", "BRK-C"]
        assert [b.broker_id for b in candidates("bond")] == ["BRK-B", "BRK-C"]

    def test_no_broker_covers_all_three(self):
        for b in BROKERS.values():
            assert len(b.trades) == 2


class TestRouting:
    @pytest.mark.parametrize("asset_class,notional,expected", [
        # LOGIC.md section 3.3 worked table
        ("equity", "1200.00", "BRK-A"),
        ("equity", "5000.00", "BRK-B"),
        ("etf",     "200.00", "BRK-C"),
        ("etf",    "1000.00", "BRK-A"),
        ("bond",   "1000.00", "BRK-C"),
        ("bond",  "10000.00", "BRK-B"),
    ])
    def test_worked_cases(self, asset_class, notional, expected):
        assert route(asset_class, D(notional)) == expected

    @pytest.mark.parametrize("asset_class,below,above,cheap_below,cheap_above", [
        ("equity", "1200.00", "1400.00", "BRK-A", "BRK-B"),   # crossover ~1315.79
        ("etf",     "400.00",  "450.00", "BRK-C", "BRK-A"),   # crossover ~416.67
        ("bond",   "1000.00", "1200.00", "BRK-C", "BRK-B"),   # crossover ~1086.96
    ])
    def test_crossovers_from_both_sides(self, asset_class, below, above,
                                        cheap_below, cheap_above):
        """Every asset class has a real crossover driven by the minimum fee.
        A bps-only comparison gets the small side wrong every time."""
        assert route(asset_class, D(below)) == cheap_below
        assert route(asset_class, D(above)) == cheap_above

    def test_exact_tie_breaks_on_broker_id_ascending(self):
        """Equity at notional 1315.00 is a genuine tie: BRK-A charges
        2.63 + 0.53 and BRK-B charges 2.50 + 0.66, both 3.16."""
        a, b = BROKERS["BRK-A"], BROKERS["BRK-B"]
        assert customer_charge(D("1315.00"), a) == customer_charge(D("1315.00"), b)
        assert money_str(customer_charge(D("1315.00"), a)) == "3.16"
        assert route("equity", D("1315.00")) == "BRK-A"

    def test_minimum_fee_flips_the_answer_at_small_notionals(self):
        """Without the floor, BRK-B (15bps) would always beat BRK-A (20bps)
        on equity. With it, BRK-A wins everything below ~1315."""
        assert route("equity", D("100.00")) == "BRK-A"
        assert route("equity", D("50000.00")) == "BRK-B"

    def test_unknown_asset_class_raises(self):
        with pytest.raises(ValueError):
            route("crypto", D("1000.00"))
