#!/usr/bin/env python3
"""Turn a practice run's diagnostics into a report.

Practice tells you, per event, whether you were right and which accounts you
disagree on, and for deposits and buy fills it returns the reference's own
legs. Checkpoints score every part and name what diverges. Reading that by eye
wastes it -- this reads it mechanically.

    python tools/diagnose.py captures/practice-20260805-135950.jsonl

Reads three files that share a stem:
    <stem>.jsonl              the event log
    <stem>.feedback.jsonl     per-event grading
    <stem>.checkpoints.jsonl  per-checkpoint grading

Every finding that moved the score from 61.5 to 99.75 came out of this data:

  * A-9 refuted -- 129 responses with expected_legs, none containing a
    0.00/0.00 leg, and ours listed as `unexpected`.
  * The systematic defect -- 7 of 7 rejected buy fills carried a reused
    trade_id, 0 of 57 accepted ones did.
  * A-4 fixed -- the checkpoint diff named cash_hold and the exact customers,
    which is what made a one-cent rounding difference findable at all.

The `--check` mode is the important one: it replays the capture through the
CURRENT code and re-grades it offline, so a change can be validated without
spending one of the 12 practice runs.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger.engine import LedgerEngine          # noqa: E402
from ledger.eventlog import EventLog            # noqa: E402
from ledger.invariants import check_all         # noqa: E402


def load(stem: Path):
    """Returns (events, order, sent, results, checkpoints)."""
    events, order = {}, []
    for line in stem.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        eid = ev.get("event_id")
        if eid and eid not in events:
            events[eid] = ev
            order.append(eid)

    sent, results = {}, {}
    fb = stem.with_suffix(".feedback.jsonl")
    if fb.exists():
        for line in fb.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            for p in row.get("sent") or []:
                sent.setdefault(p["event_id"], p)
            for r in (row.get("response") or {}).get("results") or []:
                results.setdefault(r["event_id"], r)

    checkpoints = []
    cp = stem.with_suffix(".checkpoints.jsonl")
    if cp.exists():
        checkpoints = [json.loads(l) for l in
                       cp.read_text(encoding="utf-8").splitlines() if l.strip()]
    return events, order, sent, results, checkpoints


def norm(legs):
    return sorted((l["account"], l["customer_id"], l["debit"], l["credit"])
                  for l in legs)


def _reference_wanted_legs(result: dict, sent_posting: dict | None) -> bool | None:
    """Did the reference want legs for this event?

    Must be inferred from what WE SENT AT THE TIME, recorded in the feedback
    file -- never from what the current code produces, which would be circular
    and would silently report agreement with ourselves.

    Returns None where the response cannot settle it.
    """
    expected = result.get("expected_legs")
    if expected is not None:
        return len(expected) > 0

    sent_legs = bool((sent_posting or {}).get("legs"))
    if result.get("correct"):
        return sent_legs                 # we matched, so it wanted what we sent
    if not sent_legs:
        return True                      # we sent nothing and were wrong
    return None                          # wrong legs, or unwanted legs: unknown


def report_posting(events, sent, results) -> None:
    print("=" * 76)
    print("PER-EVENT POSTING")
    print("=" * 76)
    if not results:
        print("  no feedback file (practice mode only)")
        return

    by_type = defaultdict(Counter)
    accounts = defaultdict(Counter)
    for eid, r in results.items():
        if r.get("duplicate"):
            continue
        t = events.get(eid, {}).get("type", "?")
        by_type[t]["n"] += 1
        by_type[t]["ok" if r.get("correct") else "bad"] += 1
        if not r.get("correct"):
            for a in r.get("accounts_differ") or []:
                accounts[t][a if isinstance(a, str) else str(a)] += 1

    total = sum(c["n"] for c in by_type.values())
    good = sum(c["ok"] for c in by_type.values())
    print(f"  graded {total}, correct {good} ({100 * good / max(total, 1):.2f}%)")
    print()
    for t in sorted(by_type):
        c = by_type[t]
        flag = f"   <-- {c['bad']} WRONG" if c["bad"] else ""
        print(f"  {t:<30} {c['ok']:>4} / {c['n']:<4}{flag}")
        if accounts[t]:
            print(f"       accounts_differ: {dict(accounts[t].most_common(10))}")

    dupes = sum(1 for r in results.values() if r.get("duplicate"))
    if dupes:
        print(f"\n  {dupes} resubmissions ignored by the server (chaos rewind)")


def report_defect_candidates(events, sent, results) -> None:
    """Events where the reference expected [] but we posted legs.

    This bucket is the systematic-defect detector: intersect it with whatever
    invariants fired and the one that matches exactly is the signature.
    """
    print()
    print("=" * 76)
    print("DEFECT CANDIDATES  (reference expected no legs, we posted some)")
    print("=" * 76)
    hits = []
    for eid, r in results.items():
        exp = r.get("expected_legs")
        ours = (sent.get(eid) or {}).get("legs") or []
        if exp is not None and len(exp) == 0 and ours:
            hits.append(eid)
    if not hits:
        print("  none -- we never post where the reference refuses")
        return
    for eid in hits:
        ev = events.get(eid, {})
        print(f"  {eid}  {ev.get('type')}  "
              f"{json.dumps(ev.get('payload'))[:180]}")


def report_zero_legs(results) -> None:
    print()
    print("=" * 76)
    print("ZERO-VALUED LEGS  (A-9)")
    print("=" * 76)
    with_exp = [r for r in results.values() if r.get("expected_legs") is not None]
    zero = [r for r in with_exp
            if any(l["debit"] == "0.00" and l["credit"] == "0.00"
                   for l in r["expected_legs"])]
    print(f"  responses carrying expected_legs : {len(with_exp)}")
    print(f"  ... containing a 0.00/0.00 leg   : {len(zero)}")
    if with_exp and not zero:
        print("  -> the reference OMITS zero-valued legs (confirmed)")
    counts = Counter(len(r["expected_legs"]) for r in with_exp)
    print(f"  leg-count distribution: {dict(sorted(counts.items()))}")


def report_checkpoints(checkpoints) -> None:
    print()
    print("=" * 76)
    print("CHECKPOINTS  (40 of the 100 points)")
    print("=" * 76)
    if not checkpoints:
        print("  no checkpoint file -- is the client recording responses?")
        return
    for row in checkpoints:
        resp = row.get("response") or {}
        diff = resp.get("diff") or {}
        parts = diff.get("parts") or {}
        imperfect = {k: v for k, v in parts.items() if str(v) not in ("1.0", "1")}
        print(f"  {row.get('checkpoint_id'):<14} score={resp.get('score')} "
              f"on_time={resp.get('on_time')} "
              f"as_of={'yes' if row.get('as_of_event_id') else 'no'}")
        if imperfect:
            print(f"      imperfect parts : {imperfect}")
            print(f"      customers wrong : {diff.get('customers')}")
        elif parts:
            print("      every part 1.0")


def report_offline_check(stem: Path, events, order, sent, results) -> None:
    """Replay through the CURRENT code and re-grade offline."""
    print()
    print("=" * 76)
    print("OFFLINE RE-GRADE  (current code vs this capture's feedback)")
    print("=" * 76)
    log = EventLog.load(stem.with_suffix(".jsonl"))
    engine = LedgerEngine(event_log=None)
    ours = {ev["event_id"]: engine.apply(ev) for ev in log}

    match = differ = 0
    diffs = []
    for eid, r in results.items():
        exp = r.get("expected_legs")
        if exp is None:
            continue
        if norm(ours.get(eid, [])) == norm(exp):
            match += 1
        else:
            differ += 1
            diffs.append(eid)
    print(f"  legs vs expected_legs : {match} match, {differ} differ")
    for eid in diffs[:5]:
        print(f"    {eid}  {events.get(eid, {}).get('type')}")
        print(f"      ours    : {norm(ours.get(eid, []))}")
        print(f"      expected: {norm(results[eid]['expected_legs'])}")

    agree = disagree = unknown = 0
    bad = []
    for eid, r in results.items():
        if r.get("duplicate"):
            continue
        ref_wants = _reference_wanted_legs(r, sent.get(eid))
        if ref_wants is None:
            # The capture's feedback cannot tell us. Happens when we sent legs
            # and were marked wrong without expected_legs: the reference either
            # wanted no legs, or wanted different ones, and this response does
            # not distinguish them.
            unknown += 1
            continue
        if bool(ours.get(eid)) == ref_wants:
            agree += 1
        else:
            disagree += 1
            bad.append(eid)
    print(f"  accept/reject agreement: {agree} / {agree + disagree}"
          + (f"   ({unknown} undeterminable from this capture)" if unknown else ""))
    for eid in bad[:8]:
        print(f"    {eid}  {events.get(eid, {}).get('type')}  "
              f"reason={engine.rejections.get(eid)}")

    summary = engine.summary()
    print(f"\n  engine: {summary['stats']}")
    if summary["rejections_by_reason"]:
        print("  rejections:")
        for reason, n in sorted(summary["rejections_by_reason"].items(),
                                key=lambda kv: -kv[1]):
            print(f"     {reason:<52} {n:>4}")
    violations = check_all(engine.state)
    print(f"  invariant violations: {len(violations)}")
    for v in violations[:5]:
        print(f"     {v}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capture", help="path to the capture .jsonl")
    ap.add_argument("--check", action="store_true",
                    help="also replay through the current code and re-grade")
    a = ap.parse_args()

    stem = Path(a.capture)
    if stem.suffix == ".jsonl":
        stem = stem.with_suffix("")
    if not stem.with_suffix(".jsonl").exists():
        print(f"no such capture: {stem}.jsonl", file=sys.stderr)
        return 1

    events, order, sent, results, checkpoints = load(stem)
    print(f"capture: {len(events)} events  ({stem.name})\n")

    report_posting(events, sent, results)
    report_defect_candidates(events, sent, results)
    report_zero_legs(results)
    report_checkpoints(checkpoints)
    if a.check:
        report_offline_check(stem, events, order, sent, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
