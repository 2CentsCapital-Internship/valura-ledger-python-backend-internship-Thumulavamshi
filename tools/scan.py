#!/usr/bin/env python3
"""Capture queries: extract run 1's answers mechanically, not by eye.

We get one 800-event practice window and then it is spent. Four of the five
open questions are queries over the captured log and cost no further runs, so
they are written and tested BEFORE connecting -- reading logs under time
pressure in the minutes after a 20-minute run ends is how findings get missed.

    python tools/scan.py captures/practice-20260804-120000.jsonl

The queries, and what each settles (LOGIC.md section 16):

    types   the 24-vs-23 event type count
    a3      interest_credited.customer_share: amount or rate?
    a5      does a reversal-of-a-partially-consumed-buy occur at all?
    a7      is there a sell that divides the two oversell readings?
    a9a     a loss-making BRK-B fill (zero partner share)
    a9b     a tiny-principal fill (zero custody / regulatory legs)

IMPORTANT: an empty result is NOT a confirmation. Every query reports "absent"
distinctly from "answered", because "the stream did not contain the case" and
"the case behaves as we assumed" are very different positions to ship from.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from decimal import (Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_EVEN,
                     ROUND_HALF_UP)
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger.engine import EVENT_TYPES                      # noqa: E402
from ledger.eventlog import EventLog                       # noqa: E402
from ledger.models import BUY, SELL                        # noqa: E402
from ledger.money import D, ZERO, ZERO_QTY, dec, money     # noqa: E402
from ledger.tariff import BROKERS, fee_chain               # noqa: E402

FILL_TYPES = ("order_filled", "order_partially_filled")

# Thresholds derived in LOGIC.md section 3.5.3. A higher bps rate makes a zero
# HARDER to reach, so these are the widest triggers, not the narrowest.
A9B_PRINCIPAL_CEILING = D("50.00")     # cc at BRK-C (1bp) rounds to 0.00 below this
A9B_IDEAL_CEILING = D("6.25")          # r, c and cc all zero below this


def _dec(value, default=ZERO) -> Decimal:
    try:
        return dec(value)
    except (TypeError, InvalidOperation, ArithmeticError):
        return default


def _payload(event) -> dict:
    return event.get("payload") or {}


# ---------------------------------------------------------------------------
# 1. Event type histogram -- settles the 24 vs 23 question
# ---------------------------------------------------------------------------
def scan_types(events) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        counts[event.get("type", "<missing>")] += 1

    seen = set(counts) - {"checkpoint_request"}
    return {
        "counts": dict(sorted(counts.items())),
        "distinct_ledger_types": len(seen),
        "declared_types": len(EVENT_TYPES),
        "unknown_to_us": sorted(seen - set(EVENT_TYPES)),
        "declared_but_absent": sorted(set(EVENT_TYPES) - seen),
    }


# ---------------------------------------------------------------------------
# 2. A-3: is interest_credited.customer_share an amount or a rate?
# ---------------------------------------------------------------------------
def scan_a3_interest(events) -> dict:
    samples = []
    for event in events:
        if event.get("type") != "interest_credited":
            continue
        p = _payload(event)
        gross = _dec(p.get("gross_amount"))
        share = _dec(p.get("customer_share"))
        samples.append({
            "event_id": event.get("event_id"),
            "gross_amount": str(p.get("gross_amount")),
            "customer_share": str(p.get("customer_share")),
            "share_over_gross": str(share / gross) if gross else None,
        })

    verdict = "ABSENT -- no interest_credited in this capture"
    if samples:
        # A rate lives in [0, 1] and is almost never larger than its gross.
        # An amount is bounded by the gross and usually a similar magnitude.
        looks_like_rate = all(
            ZERO <= _dec(s["customer_share"]) <= D("1")
            and _dec(s["gross_amount"]) > D("1")
            for s in samples
        )
        verdict = ("LIKELY A RATE -- share is in [0,1] while gross is larger"
                   if looks_like_rate else
                   "LIKELY AN AMOUNT -- share scales with gross")
    return {"verdict": verdict, "samples": samples[:10], "n": len(samples)}


# ---------------------------------------------------------------------------
# 3. A-5: does a reversal of a partially-consumed buy lot occur at all?
# ---------------------------------------------------------------------------
def scan_a5_reversal_of_consumed_buy(events) -> dict:
    """A reversal whose target is a buy fill, where a sell for the same
    (customer, symbol) landed in between.

    A deliberately loose approximation -- it does not simulate the lot book, so
    it over-reports. That is the right bias: it produces a short list of
    candidates to inspect, and a genuinely empty list is meaningful.
    """
    buys: dict[str, tuple[int, str, str]] = {}          # event_id -> (idx, cid, symbol)
    sells: list[tuple[int, str, str]] = []              # (idx, cid, symbol)

    for index, event in enumerate(events):
        etype = event.get("type")
        if etype not in FILL_TYPES:
            continue
        p = _payload(event)
        key = (p.get("customer_id"), p.get("symbol"))
        if p.get("side") == BUY:
            buys[event.get("event_id")] = (index, *key)
        elif p.get("side") == SELL:
            sells.append((index, *key))

    hits, reversals_of_buys = [], 0
    for index, event in enumerate(events):
        if event.get("type") != "reversal":
            continue
        target = _payload(event).get("reverses_event_id")
        if target not in buys:
            continue
        reversals_of_buys += 1
        buy_index, cid, symbol = buys[target]
        intervening = [s for s in sells
                       if buy_index < s[0] < index and s[1] == cid and s[2] == symbol]
        if intervening:
            hits.append({
                "reversal_event_id": event.get("event_id"),
                "reverses": target,
                "customer_id": cid, "symbol": symbol,
                "intervening_sells": len(intervening),
            })

    if not hits:
        verdict = ("ABSENT -- no reversal of a consumed buy in this capture. "
                   "NOT a resolution: we would ship the surgical-undo guess "
                   "untested. Re-run this scan on every later capture.")
    else:
        verdict = f"PRESENT -- {len(hits)} candidate(s). Build both A-5 implementations."
    return {"verdict": verdict, "reversals_of_buy_fills": reversals_of_buys,
            "candidates": hits}


# ---------------------------------------------------------------------------
# 4. A-7: a sell that divides the two oversell readings
# ---------------------------------------------------------------------------
def scan_a7_oversell_boundary(events) -> dict:
    """Sells exceeding (position - shares held by open sell orders) but not
    exceeding the position itself.

    Under our reading (oversell measured against the total position) these are
    accepted; under the alternative (measured against the un-held position)
    they are rejected. Either way the loser corrupts the lot book permanently.
    """
    position: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO_QTY)
    held: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO_QTY)
    open_sell_orders: dict[str, tuple[str, str, Decimal]] = {}
    divergent = []

    for event in events:
        etype, p = event.get("type"), _payload(event)

        if etype == "order_placed" and p.get("side") == SELL:
            key = (p.get("customer_id"), p.get("symbol"))
            qty = _dec(p.get("quantity"), ZERO_QTY)
            open_sell_orders[p.get("order_id")] = (*key, qty)
            held[key] += qty

        elif etype in ("order_cancelled", "order_rejected"):
            entry = open_sell_orders.pop(p.get("order_id"), None)
            if entry:
                cid, symbol, qty = entry
                held[(cid, symbol)] -= qty

        elif etype in FILL_TYPES:
            key = (p.get("customer_id"), p.get("symbol"))
            qty = _dec(p.get("quantity"), ZERO_QTY)
            if p.get("side") == BUY:
                position[key] += qty
            else:
                free = position[key] - held[key]
                if qty > free and qty <= position[key]:
                    divergent.append({
                        "event_id": event.get("event_id"),
                        "customer_id": key[0], "symbol": key[1],
                        "sell_quantity": str(qty),
                        "position": str(position[key]),
                        "held_by_open_sells": str(held[key]),
                        "free": str(free),
                    })
                position[key] -= qty
                entry = open_sell_orders.pop(p.get("order_id"), None)
                if entry and event.get("type") == "order_filled":
                    held[(entry[0], entry[1])] -= entry[2]

    verdict = ("ABSENT -- no sell distinguishes the two readings in this "
               "capture. A-7 stays unconfirmed; keep scanning."
               if not divergent else
               f"PRESENT -- {len(divergent)} divergent sell(s). Check what the "
               f"reference did with each in the feedback file.")
    return {"verdict": verdict, "divergent_sells": divergent}


# ---------------------------------------------------------------------------
# 5. A-9a / A-9b: fills that actually exercise a zero-valued leg
# ---------------------------------------------------------------------------
def scan_a9_zero_legs(events) -> dict:
    loss_making, tiny, ideal = [], [], []

    for event in events:
        if event.get("type") not in FILL_TYPES:
            continue
        p = _payload(event)
        broker = p.get("broker")
        if broker not in BROKERS:
            continue
        principal = _dec(p.get("principal"))
        if principal <= ZERO:
            continue
        try:
            fees = fee_chain(principal, broker, _dec(p.get("partner_rate")))
        except Exception:
            continue

        record = {
            "event_id": event.get("event_id"), "side": p.get("side"),
            "broker": broker, "principal": str(principal),
            "b": str(fees.brokerage), "c": str(fees.custody),
            "r": str(fees.regulatory), "cc": str(fees.custody_cost),
            "ps": str(fees.partner_share),
        }
        if fees.partner_share == ZERO:
            loss_making.append(record)
        if fees.custody == ZERO or fees.custody_cost == ZERO or fees.regulatory == ZERO:
            tiny.append(record)
        if principal < A9B_IDEAL_CEILING:
            ideal.append(record)

    def verdict(hits, label):
        if not hits:
            return (f"ABSENT -- no {label} in this capture. A-9 stays OPEN for "
                    f"this trigger; keep zero legs included and re-check on run 2.")
        return (f"PRESENT -- {len(hits)} fill(s). Read the reference legs for "
                f"one of these in the .feedback.jsonl file.")

    return {
        "a9a_zero_partner_share": {
            "verdict": verdict(loss_making, "loss-making fill"),
            "note": "buy fills only -- practice returns full legs for those",
            "samples": [h for h in loss_making if h["side"] == BUY][:5],
            "n_total": len(loss_making),
            "n_buys": sum(1 for h in loss_making if h["side"] == BUY),
        },
        "a9b_zero_custody_or_regulatory": {
            "verdict": verdict(tiny, "tiny-principal fill"),
            "note": f"widest trigger is cc at BRK-C below {A9B_PRINCIPAL_CEILING}",
            "samples": [h for h in tiny if h["side"] == BUY][:5],
            "n_total": len(tiny),
            "n_buys": sum(1 for h in tiny if h["side"] == BUY),
            "ideal_probes_under_6_25": ideal[:5],
        },
    }


# ---------------------------------------------------------------------------
def scan_a14_fx_spread(events) -> dict:
    """A-14: has an fx_deposit with a zero or NEGATIVE spread ever arrived?

    The rejection rule is implemented per spec but has never fired, so it is
    untested. This keeps watching -- and unlike the per-event feedback, it works
    on a submission or final capture, where no diagnostics are returned.
    """
    negative, zero, inconsistent = [], [], 0
    total = 0
    for event in events:
        if event.get("type") != "fx_deposit":
            continue
        p = _payload(event)
        total += 1
        market = _dec(p.get("usd_at_market_rate"))
        customer = _dec(p.get("usd_at_customer_rate"))
        record = {
            "event_id": event.get("event_id"),
            "currency": p.get("currency"),
            "usd_at_market_rate": str(market),
            "usd_at_customer_rate": str(customer),
            "spread": str(market - customer),
        }
        if customer > market:
            negative.append(record)
        elif customer == market:
            zero.append(record)
        # The quote convention is FOREIGN PER USD, so the amount is DIVIDED.
        rate = _dec(p.get("market_rate"))
        foreign = _dec(p.get("amount_foreign"))
        if rate and money(foreign / rate) != money(market):
            inconsistent += 1

    if negative or zero:
        verdict = (f"PRESENT -- {len(negative)} negative-spread and {len(zero)} "
                   f"zero-spread deposit(s). A-14 can finally be checked: "
                   f"compare our decision against the reference.")
    else:
        verdict = ("ABSENT -- no zero- or negative-spread fx_deposit. A-14 "
                   "stays UNTESTED; the rejection rule has still never fired. "
                   "Not a confirmation.")
    return {
        "verdict": verdict, "n_fx_deposits": total,
        "negative_spread": negative[:5], "zero_spread": zero[:5],
        "rate_inconsistent": inconsistent,
    }


MICRO = D("0.000001")


def scan_a16_split_rounding(events) -> dict:
    """A-16: does a stock_split ever land a lot quantity on a rounding boundary?

    The sheet fixes "half away from zero" for MONEY. It says only that share
    quantities have "up to 6" decimal places, with no rounding rule -- so our
    choice of half-away-from-zero for quantities is an assumption, not a
    reading.

    It only matters when `quantity x ratio_to / ratio_from` lands exactly on a
    half at the 7th decimal, where half-up, half-even and truncation disagree.
    Practice runs 3 and 5 produced ZERO such cases in 73 scaled lots; submission
    run 1 produced 21 in 379. That is why a perfect practice checkpoint did not
    transfer.

    Also reports TRIGGER DENSITY, so a run can be judged on whether it had a
    realistic chance of reproducing the case at all -- an absent result from a
    low-density run says much less than one from a high-density run.
    """
    from ledger.engine import LedgerEngine     # local: avoids an import cycle

    engine = LedgerEngine(event_log=None)
    scaled_lots = 0
    boundary = []
    fractional_lots_at_split = 0
    first_hit_index = None

    for index, event in enumerate(events):
        if event.get("type") == "stock_split":
            p = _payload(event)
            cid, sym = p.get("customer_id"), p.get("symbol")
            ratio_from = _dec(p.get("ratio_from"), ZERO)
            ratio_to = _dec(p.get("ratio_to"), ZERO)
            if ratio_from > ZERO and ratio_to > ZERO:
                for lot in engine.state.lots.get((cid, sym), []):
                    if lot.quantity <= ZERO_QTY:
                        continue
                    scaled_lots += 1
                    if lot.quantity != lot.quantity.to_integral_value():
                        fractional_lots_at_split += 1
                    exact = lot.quantity * ratio_to / ratio_from
                    if exact != exact.quantize(MICRO, rounding=ROUND_HALF_UP):
                        if first_hit_index is None:
                            first_hit_index = index
                        half_up = exact.quantize(MICRO, rounding=ROUND_HALF_UP)
                        half_even = exact.quantize(MICRO, rounding=ROUND_HALF_EVEN)
                        trunc = exact.quantize(MICRO, rounding=ROUND_DOWN)
                        boundary.append({
                            "event_id": event.get("event_id"),
                            "stream_index": index,
                            "customer_id": cid, "symbol": sym,
                            "ratio": f"{ratio_from}->{ratio_to}",
                            "lot_seq": lot.seq,
                            "quantity_before": str(lot.quantity),
                            "exact": str(exact),
                            "half_up": str(half_up),
                            "half_even": str(half_even),
                            "truncated": str(trunc),
                            # Which open question can this case actually settle?
                            # Truncation was ruled out on 2026-08-05; the live
                            # question is half-up vs half-even, and only a case
                            # where those two DIFFER can settle it. They differ
                            # exactly when the 6th decimal is even.
                            "discriminates_half_up_vs_half_even": half_up != half_even,
                            "discriminates_rounding_vs_truncation": half_up != trunc,
                        })
        engine.apply(event)

    # Did any affected lot go on to feed a FIFO cost relief? That is the
    # difference between 0.8 points at risk (quantity) and 25.6 (cost basis).
    consumed_seqs = {seq for fill in engine.state.fills.values()
                     for seq, _q, _c in fill.consumption}
    fed_cost_relief = [b for b in boundary if b["lot_seq"] in consumed_seqs]

    n = max(len(events), 1)
    density = {
        "events": len(events),
        "dividend_reinvested": sum(1 for e in events
                                   if e.get("type") == "dividend_reinvested"),
        "stock_split": sum(1 for e in events if e.get("type") == "stock_split"),
        "lots_scaled_by_splits": scaled_lots,
        "fractional_lots_at_split_time": fractional_lots_at_split,
        "scaled_lots_per_1000_events": round(1000 * scaled_lots / n, 1),
        "boundary_hits": len(boundary),
        "boundary_hit_rate_of_scaled_lots":
            f"{100 * len(boundary) / scaled_lots:.1f}%" if scaled_lots else "n/a",
        "first_hit_at_stream_index": first_hit_index,
    }

    discriminating = [b for b in boundary
                      if b["discriminates_half_up_vs_half_even"]]

    if boundary and discriminating:
        verdict = (f"PRESENT and DECISIVE -- {len(boundary)} boundary case(s), "
                   f"{len(discriminating)} of which separate half-up from "
                   f"half-even. This run CAN settle the open half of A-16: "
                   f"check the checkpoint diff for the named customer/symbol.")
    elif boundary:
        verdict = (f"PRESENT but NOT decisive -- {len(boundary)} boundary "
                   f"case(s), none of which separate half-up from half-even "
                   f"(they differ only when the 6th decimal is even). Confirms "
                   f"rounding-not-truncation again; leaves the open half of "
                   f"A-16 open.")
    elif scaled_lots == 0:
        verdict = ("ABSENT and UNINFORMATIVE -- no lots were scaled by a split "
                   "at all, so this run could not have tested A-16.")
    else:
        verdict = (f"ABSENT -- {scaled_lots} lots scaled, none on a boundary. "
                   f"Weak evidence at best: see the density figures for whether "
                   f"this run had a realistic chance of hitting the case.")

    return {
        "verdict": verdict,
        "density": density,
        "boundary_cases": boundary[:10],
        "fed_cost_relief": fed_cost_relief[:5],
        "n_fed_cost_relief": len(fed_cost_relief),
    }


def scan_all(events) -> dict:
    return {
        "n_events": len(events),
        "types": scan_types(events),
        "a3_interest_share": scan_a3_interest(events),
        "a5_reversal_of_consumed_buy": scan_a5_reversal_of_consumed_buy(events),
        "a7_oversell_boundary": scan_a7_oversell_boundary(events),
        "a9_zero_legs": scan_a9_zero_legs(events),
        "a14_fx_spread": scan_a14_fx_spread(events),
        "a16_split_rounding": scan_a16_split_rounding(events),
    }


def _render(report: dict) -> str:
    from ledger.eventlog import dumps

    out = [f"capture: {report['n_events']} events", ""]

    types = report["types"]
    out.append(f"-- event types: {types['distinct_ledger_types']} distinct "
               f"(we declare {types['declared_types']})")
    for t, n in types["counts"].items():
        out.append(f"     {t:<32} {n:>5}")
    if types["unknown_to_us"]:
        out.append(f"  !! UNKNOWN TO US: {types['unknown_to_us']}")
    if types["declared_but_absent"]:
        out.append(f"     declared but absent: {types['declared_but_absent']}")
    out.append("")

    for key, title in [
        ("a3_interest_share", "A-3  interest_credited.customer_share"),
        ("a5_reversal_of_consumed_buy", "A-5  reversal of a consumed buy lot"),
        ("a7_oversell_boundary", "A-7  oversell boundary"),
        ("a14_fx_spread", "A-14 fx_deposit zero / negative spread"),
        ("a16_split_rounding", "A-16 split quantity rounding boundary"),
    ]:
        out.append(f"-- {title}")
        out.append(f"     {report[key]['verdict']}")
        out.append("")

    a9 = report["a9_zero_legs"]
    out.append("-- A-9a zero partner share (loss-making BRK-B fill)")
    out.append(f"     {a9['a9a_zero_partner_share']['verdict']}")
    out.append(f"     buys: {a9['a9a_zero_partner_share']['n_buys']}, "
               f"all sides: {a9['a9a_zero_partner_share']['n_total']}")
    out.append("")
    out.append("-- A-9b zero custody / regulatory (tiny-principal fill)")
    out.append(f"     {a9['a9b_zero_custody_or_regulatory']['verdict']}")
    out.append(f"     buys: {a9['a9b_zero_custody_or_regulatory']['n_buys']}, "
               f"all sides: {a9['a9b_zero_custody_or_regulatory']['n_total']}")
    a16 = report["a16_split_rounding"]
    d = a16["density"]
    out.append("     TRIGGER DENSITY (can this run test A-16 at all?)")
    out.append(f"       dividend_reinvested        {d['dividend_reinvested']:>6}")
    out.append(f"       stock_split                {d['stock_split']:>6}")
    out.append(f"       lots scaled by splits      {d['lots_scaled_by_splits']:>6}"
               f"   ({d['scaled_lots_per_1000_events']} per 1000 events)")
    out.append(f"       fractional at split time   {d['fractional_lots_at_split_time']:>6}")
    out.append(f"       boundary hits              {d['boundary_hits']:>6}"
               f"   ({d['boundary_hit_rate_of_scaled_lots']} of scaled lots)")
    if a16["n_fed_cost_relief"]:
        out.append(f"       ... that fed a cost relief {a16['n_fed_cost_relief']:>6}"
                   f"   <-- these touch the 64% slice")
    out.append("")

    out.append("full report:")
    out.append(dumps(report))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capture", help="path to a captured JSONL event log")
    ap.add_argument("--json", action="store_true", help="raw JSON only")
    a = ap.parse_args()

    events = EventLog.load(a.capture).events
    if not events:
        print(f"no events in {a.capture}", file=sys.stderr)
        return 1

    report = scan_all(events)
    if a.json:
        from ledger.eventlog import dumps
        print(dumps(report))
    else:
        print(_render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
