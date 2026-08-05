"""The chart of accounts (LOGIC.md section 1.4, PROJECT.md section 3).

Account codes are strings everywhere, including on the wire. Never integers:
'2411' and 2411 are different keys and only one of them serializes correctly.
"""
from __future__ import annotations

# -- assets (debit-positive) -------------------------------------------------
OMNIBUS_CASH = "1100"           # Omnibus Cash at Broker
SETTLEMENT_RECEIVABLE = "1150"  # Settlement Receivable (sell proceeds until T+2)
OMNIBUS_CUSTODY = "1200"        # Omnibus Custody, carried AT COST

# -- liabilities (credit-positive) -------------------------------------------
CUSTOMER_WALLET = "2010"        # Customer Wallet
SECURITIES_CLAIM = "2100"       # Customer Securities Claim, AT COST
WITHDRAWALS_IN_TRANSIT = "2300"
UNSETTLED_TRADE_PAYABLE = "2350"  # owed to broker for a buy until T+2
REG_FEES_PAYABLE = "2400"       # collected on the venue's behalf, owed onward
BROKER_FEES_PAYABLE_A = "2411"
BROKER_FEES_PAYABLE_B = "2412"
BROKER_FEES_PAYABLE_C = "2413"
CUSTODIAN_FEES_PAYABLE = "2420"
PARTNER_SHARE_PAYABLE = "2430"

# -- income (credit-positive) ------------------------------------------------
BROKERAGE_REVENUE = "4000"      # renamed from "Commission Revenue" in the stale spec
CUSTODY_REVENUE = "4010"
FX_SPREAD_REVENUE = "4100"
INTEREST_INCOME = "4200"

# -- expense (debit-positive) ------------------------------------------------
BROKERAGE_COST = "5000"
CUSTODY_COST = "5010"
PARTNER_REVENUE_SHARE = "5100"


ASSET, LIABILITY, INCOME, EXPENSE = "asset", "liability", "income", "expense"

ACCOUNT_TYPE: dict[str, str] = {
    OMNIBUS_CASH: ASSET,
    SETTLEMENT_RECEIVABLE: ASSET,
    OMNIBUS_CUSTODY: ASSET,
    CUSTOMER_WALLET: LIABILITY,
    SECURITIES_CLAIM: LIABILITY,
    WITHDRAWALS_IN_TRANSIT: LIABILITY,
    UNSETTLED_TRADE_PAYABLE: LIABILITY,
    REG_FEES_PAYABLE: LIABILITY,
    BROKER_FEES_PAYABLE_A: LIABILITY,
    BROKER_FEES_PAYABLE_B: LIABILITY,
    BROKER_FEES_PAYABLE_C: LIABILITY,
    CUSTODIAN_FEES_PAYABLE: LIABILITY,
    PARTNER_SHARE_PAYABLE: LIABILITY,
    BROKERAGE_REVENUE: INCOME,
    CUSTODY_REVENUE: INCOME,
    FX_SPREAD_REVENUE: INCOME,
    INTEREST_INCOME: INCOME,
    BROKERAGE_COST: EXPENSE,
    CUSTODY_COST: EXPENSE,
    PARTNER_REVENUE_SHARE: EXPENSE,
}

ALL_ACCOUNTS = frozenset(ACCOUNT_TYPE)

# Graded as ONE all-or-nothing block: "either your statement of what the firm
# earned and owes is right, or it is not." Worth 11% of the checkpoint score.
FIRM_ACCOUNTS = frozenset({
    REG_FEES_PAYABLE,
    BROKER_FEES_PAYABLE_A, BROKER_FEES_PAYABLE_B, BROKER_FEES_PAYABLE_C,
    CUSTODIAN_FEES_PAYABLE, PARTNER_SHARE_PAYABLE,
    BROKERAGE_REVENUE, CUSTODY_REVENUE, FX_SPREAD_REVENUE, INTEREST_INCOME,
    BROKERAGE_COST, CUSTODY_COST, PARTNER_REVENUE_SHARE,
})

BROKER_PAYABLE: dict[str, str] = {
    "BRK-A": BROKER_FEES_PAYABLE_A,
    "BRK-B": BROKER_FEES_PAYABLE_B,
    "BRK-C": BROKER_FEES_PAYABLE_C,
}


def broker_payable(broker: str) -> str:
    """The 241x account for a broker. Raises on an unknown broker id."""
    try:
        return BROKER_PAYABLE[broker]
    except KeyError:
        raise KeyError(f"unknown broker {broker!r}") from None
