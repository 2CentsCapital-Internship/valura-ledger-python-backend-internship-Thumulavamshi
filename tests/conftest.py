"""Shared fixtures and event builders."""
from __future__ import annotations

import itertools

import pytest

from ledger.engine import LedgerEngine
from ledger.eventlog import EventLog

_counter = itertools.count(1)


def event(event_type: str, payload: dict, event_id: str | None = None,
          offset: int | None = None, **extra) -> dict:
    """Build a stream event envelope."""
    n = next(_counter)
    return {
        "offset": offset if offset is not None else n,
        "event_id": event_id or f"evt_{n:06d}",
        "type": event_type,
        "payload": payload,
        **extra,
    }


def deposit(customer_id="CUST-1001", amount="1000.00", **kw) -> dict:
    return event("deposit", {"customer_id": customer_id, "amount": amount}, **kw)


def buy_fill(customer_id="CUST-1001", symbol="ACME", quantity="100",
             price="100.00", principal="10000.00", broker="BRK-A",
             partner_rate="0.50", asset_class="equity", order_id="ord_1",
             trade_id=None, final=True, **kw) -> dict:
    n = next(_counter)
    return event(
        "order_filled" if final else "order_partially_filled",
        {
            "order_id": order_id, "customer_id": customer_id, "side": "buy",
            "symbol": symbol, "quantity": quantity, "price": price,
            "principal": principal, "asset_class": asset_class,
            "broker": broker, "partner_rate": partner_rate,
            "trade_id": trade_id or f"trd_{n:06d}",
        },
        **kw,
    )


def legs_by_account(legs: list[dict]) -> dict[str, tuple[str, str]]:
    return {l["account"]: (l["debit"], l["credit"]) for l in legs}


@pytest.fixture
def engine() -> LedgerEngine:
    return LedgerEngine(event_log=EventLog(None))
