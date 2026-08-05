#!/usr/bin/env python3
"""Re-run a captured event log offline. The inner development loop.

Change a handler, replay, diff. No network, no practice run consumed,
sub-second turnaround. This is what makes 12 practice runs enough.

    python tools/replay.py captures/practice-20260804-120000.jsonl
    python tools/replay.py capture.jsonl --as-of evt_9f2c11a04b3e7712
    python tools/replay.py capture.jsonl --check-idempotency

Uses the identical apply() path as the live run. If it used a different one it
would not prove anything about the live state.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger.engine import LedgerEngine           # noqa: E402
from ledger.eventlog import EventLog, dumps      # noqa: E402
from ledger.invariants import check_all          # noqa: E402
from ledger.snapshot import snapshot             # noqa: E402


def fold(events) -> LedgerEngine:
    engine = LedgerEngine(event_log=None)
    for event in events:
        engine.apply(event)
    return engine


def check_idempotency(events, window: int = 300, seed: int = 0) -> bool:
    """Simulate the deliberate mid-run rewind (chaos_replay is 80/300/400).

    "An idempotent consumer notices nothing." If this fails, resilience (15
    points) is at risk and so is every balance after the replay point.
    """
    rng = random.Random(seed)
    clean = snapshot(fold(events).state)

    if len(events) > window:
        start = rng.randrange(0, len(events) - window)
        replayed = events[:start + window] + events[start:start + window] \
            + events[start + window:]
    else:
        replayed = events + events

    dirty = snapshot(fold(replayed).state)

    if clean == dirty:
        print(f"idempotent: a {window}-event rewind changed nothing")
        return True

    print("NOT IDEMPOTENT -- the rewind changed state:")
    for key in ("trial_balance", "customers", "open_order_routes"):
        if clean.get(key) != dirty.get(key):
            print(f"  {key} diverged")
            for field in sorted(set(clean.get(key, {})) | set(dirty.get(key, {}))):
                before, after = clean.get(key, {}).get(field), dirty.get(key, {}).get(field)
                if before != after:
                    print(f"    {field}: clean={before} replayed={after}")
    return False


def check_determinism(events) -> bool:
    """Two folds of the same log must be byte-identical. If they are not, the
    as-of answers will drift from the current ones."""
    if snapshot(fold(events).state) == snapshot(fold(events).state):
        print("deterministic: two folds agree")
        return True
    print("NOT DETERMINISTIC -- two folds of the same log disagree")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capture")
    ap.add_argument("--as-of", default=None, metavar="EVENT_ID")
    ap.add_argument("--check-idempotency", action="store_true")
    ap.add_argument("--check-determinism", action="store_true")
    ap.add_argument("--summary-only", action="store_true")
    a = ap.parse_args()

    log = EventLog.load(a.capture)
    if not len(log):
        print(f"no events in {a.capture}", file=sys.stderr)
        return 1

    events = log.upto(a.as_of) if a.as_of else log.events

    started = time.perf_counter()
    engine = fold(events)
    elapsed = time.perf_counter() - started
    print(f"replayed {len(events)} events in {elapsed * 1000:.0f}ms "
          f"(60s checkpoint grace -- as-of by full replay is comfortable)")

    summary = engine.summary()
    print("\nstats:", dumps(summary["stats"]))
    print("\nby type:")
    for event_type, n in summary["by_type"].items():
        gap = summary["unimplemented_by_type"].get(event_type, 0)
        marker = f"   ({gap} unimplemented)" if gap else ""
        print(f"  {event_type:<32} {n:>5}{marker}")

    if summary["unknown_types"]:
        print("\n!! UNKNOWN EVENT TYPES:", dumps(summary["unknown_types"]))
    if summary["rejections_by_reason"]:
        print("\nrejections:", dumps(summary["rejections_by_reason"]))
    if summary["crashes_by_type"]:
        print("\ncrashes:", dumps(summary["crashes_by_type"]))

    if not a.summary_only:
        print("\nsnapshot:")
        print(dumps(snapshot(engine.state)))

    violations = check_all(engine.state)
    print()
    if violations:
        print(f"!! {len(violations)} INVARIANT VIOLATION(S) -- our own arithmetic:")
        for v in violations:
            print(f"   {v}")
    else:
        print("invariants: trial balance zero, custody mirrors the lot book, "
              "holds sane")

    ok = not violations
    if a.check_determinism:
        print()
        ok &= check_determinism(events)
    if a.check_idempotency:
        print()
        ok &= check_idempotency(events)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
