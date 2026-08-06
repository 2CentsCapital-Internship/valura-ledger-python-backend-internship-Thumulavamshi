# Ledger Arena — double-entry book of record

A client that consumes a broker's event feed over SSE, posts the journal legs
each event produces, and answers state checkpoints — including historical
"as-of" ones asking what the book looked like earlier in the stream.

Built for the Valura.AI Ledger Arena take-home.

| | |
| --- | --- |
| Best submission score | **99.85** / 100 — posting 29.98/30, checkpoint **40.0/40**, resilience 14.87/15, liveness 10/10, reconciliation 5/5 |
| Final run | completed; score withheld by that tier's design |
| Event types implemented | **24 / 24** |
| Tests | **340** (`python -m pytest tests/ -q`) |
| Runtime dependencies | `httpx`, and nothing else |

> ### ⚠️ `PROTOCOL.md` in this repo is a stale spec snapshot — do not use it
>
> It ships with the starter kit and disagrees with the real specification on the
> substance of the assignment: 11 accounts instead of 17, no broker tariff, no
> order routing, no as-of checkpoints, commission handed to you in the payload,
> and different scoring weights. Its posting hints for fills are **wrong**.
>
> **`task_description.txt` is canonical.** The full diff is in
> [PROJECT.md §0](PROJECT.md#-0-read-this-before-anything-else-the-repos-spec-is-stale).
> Catching this was the first real finding of the project.

## Where to start

| Read | For |
| --- | --- |
| **[NOTES.md](NOTES.md)** | What is built, what is not, and **how the findings were actually made**. Start here |
| [PROJECT.md](PROJECT.md) | Problem statement, full API, scoring breakdown, run history |
| [LOGIC.md](LOGIC.md) | The business-rules spec: every event type's legs, derived from the economics. §16 is the assumption register, §17 a dated findings log |
| [TECHNICAL_PLAN.md](TECHNICAL_PLAN.md) | Structure, data models, state strategy |
| [HANDOFF.md](HANDOFF.md) | Cold-start guide: conventions, gotchas, what was tried and rejected |

## Architecture

**Event sourcing — the log is the source of truth, state is a pure fold over it.**

As-of checkpoints ask what the book looked like at an earlier event, and **the
lot book cannot be walked backwards**: a consumed lot's quantity and cost are
unrecoverable from what remains, because the relief rounded. So history is
answered by replay. A 6,000-event replay takes ~50 ms against a 60-second grace
period, so there is no snapshotting machinery and none is needed.

Three properties the design depends on:

- **`apply(state, event)` is deterministic** — no clocks, no randomness, no
  dependence on iteration order. This is what makes replay and as-of work.
- **The seen-gate is the first statement of `apply()`**, so idempotency is
  structural rather than something each handler must remember. That is what
  survives the deliberate mid-run rewind of 80–400 events.
- **Nothing ever ends the run.** Three nested guards: a rejection is a normal
  outcome, a handler crash costs one event, a loop error reconnects.

```
ledger/
  money.py       the rounding convention (2dp, half away from zero)
  tariff.py      broker table, six-part fee chain, order routing
  lots.py        FIFO relief, split rescaling, symbol re-keying
  state.py       balances keyed by (customer, account); lots; orders
  engine.py      dispatch, seen-gate, "every type is wired" contract
  handlers/      cash · orders · corporate · payables · corrections
  snapshot.py    checkpoint serialization, current and as-of
  eventlog.py    append-only log, JSONL persistence, replay
  invariants.py  book-level self-checks
tools/           diagnose · scan · replay · whoami
```

## Running it

```bash
pip install -r requirements.txt
python -m pytest tests/ -q

python client.py --key ak_... --mode practice
```

Redirect stderr to a file — all logging goes there, so the console is quiet for
the whole run:

```bash
python client.py --key ak_... --mode submission --new 2>submission.log
```

### Offline tooling

Every run writes a JSONL capture. The captures are committed, so all of this
works with no network and no attempt spent:

```bash
python tools/diagnose.py captures/<run>.jsonl --check   # re-grade against the reference
python tools/scan.py     captures/<run>.jsonl           # open-question scans
python tools/replay.py   captures/<run>.jsonl --check-idempotency
```

`diagnose --check` replays a capture through the current code and re-grades it
against that run's own feedback. It doubles as the regression suite: every
captured run still re-grades with 100% agreement and zero invariant violations.

## How the hard parts were settled

The spec deliberately withholds the journal entries — only `deposit` and the buy
fill are worked. Everything else is derived, and several derivations were wrong
before they were right. The method that worked:

**Practice mode is a graded oracle**, and its checkpoint diff names *which
customers* diverge. So a candidate rule must change **exactly those and nobody
else** — a negative control. Applied to one checkpoint, six rival hold formulas
all looked plausible; applied across every checkpoint of every run, exactly one
survived.

The corollary, learned the hard way: *"fits every run so far"* is much weaker
than it feels. The cash-hold formula was wrong **twice**, and the second version
scored a perfect checkpoint on three consecutive runs before a sharper dataset
exposed it. What finally settled it was an independent argument, not more data.

The undisclosed systematic defect turned out to be **a fill reusing an
already-settled `trade_id`** — found by intersecting "the reference expected no
legs but we posted some" with a battery of candidate invariants. Two of the
invariants that looked strongest had to be discarded: one fires on fills the
reference accepts, and a limit-price check would have destroyed 32 valid fills'
worth of lots.

## Known gaps

Documented rather than hidden — see [NOTES.md](NOTES.md).

**A-5, reversing a partially consumed lot.** Two readings are defensible, the
spec does not say which, and across the whole assessment the case never occurred:

```
25,381 events · 101 reversals of buy fills · 0 against a consumed lot
```

It ships as an argued guess with a runtime warning wired to make a first
occurrence visible in a feedback-free scored run. The warning never fired.
