#!/usr/bin/env python3
"""The arena client: transport only. The ledger is in `book.py` / `ledger/`.

Subscribing to the stream, surviving the replay, resuming from an offset,
batching postings, answering checkpoints on time. None of it is what is being
assessed, but several of the gaps below WOULD have cost real score, so this is
no longer the version the starter kit shipped. See TECHNICAL_PLAN.md section 2
for the full gap list; the two that matter most:

  G-1  --seconds defaulted to 1500 (25 min). Submission runs 3,600s and final
       runs 4,500s, so a default invocation stopped two thirds of the way into
       a SCORED attempt and never saw stream_end. Now derived from the mode,
       preferring the live /v1/rules figure.

  G-2  &new=true was never sent. On submission/final, connecting after a
       previous run finished returns 409 and the old client retried it until
       the deadline. Now an explicit --new flag, sent only on the FIRST
       connect: automatic would be dangerous, because a mid-run reconnect with
       new=true spends an attempt.

    pip install -r requirements.txt
    python client.py --key ak_... --mode practice
"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

import httpx

from book import Book
from ledger.eventlog import dumps, loads

log = logging.getLogger("client")

# Nominal run lengths from /v1/rules. Used only if the endpoint is unreachable.
MODE_DURATION = {"practice": 1200, "submission": 3600, "final": 4500}

# "The lengths are nominal: your stream is staggered against everyone else's
# and drains its tail after the nominal duration, so budget a few extra minutes
# and let stream_end tell you the run is over, not your own clock."
TAIL_MARGIN_SECONDS = 900

# The first event can be up to 90 seconds after connecting (staggered starts),
# so the read timeout has to clear that comfortably. Without one, a silently
# dead TCP connection hangs until the deadline (G-6).
STREAM_READ_TIMEOUT = 120.0

MAX_POSTINGS_PER_REQUEST = 500


class ArenaClient:
    def __init__(self, url: str, key: str, mode: str, start_new: bool = False,
                 batch: int = 100, flush_ms: int = 400,
                 log_path: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.mode = mode
        self.start_new = start_new
        self.batch = batch
        self.flush_ms = flush_ms
        self.book = Book(log_path)
        self.pending: list[dict] = []
        self.cursor = 0
        self.first_connect = True
        self.finished_notice = False
        self.stats = {"events": 0, "posted": 0, "checkpoints": 0,
                      "checkpoints_failed": 0, "reconnects": 0, "resets": 0,
                      "errors": 0}
        self.done = False
        self.received_at: dict[str, float] = {}     # event_id -> time (G-9)
        self.latencies: list[float] = []

    # -- submitting ---------------------------------------------------------
    def flush(self, http: httpx.Client) -> None:
        """Postings go up in batches. One request per event works and is slow;
        at the burst rate it will put you behind the stream."""
        while self.pending:
            chunk = self.pending[:MAX_POSTINGS_PER_REQUEST]
            rest = self.pending[MAX_POSTINGS_PER_REQUEST:]
            body = {"postings": chunk}
            try:
                r = http.post(f"{self.url}/v1/postings", params={"mode": self.mode},
                              content=dumps(body),
                              headers={"Content-Type": "application/json"},
                              timeout=30)
                if r.status_code == 429:
                    time.sleep(float(r.headers.get("Retry-After", 5)))
                    return                      # keep pending intact, retry later
                r.raise_for_status()
            except httpx.HTTPError as exc:
                self.stats["errors"] += 1
                log.warning("posting batch failed (%s), will retry",
                            type(exc).__name__)
                time.sleep(1)
                return                          # keep pending intact

            self.pending = rest
            self.stats["posted"] += len(chunk)
            self._record_latencies(chunk)
            self._record_feedback(r, chunk)

    def _record_latencies(self, chunk: list[dict]) -> None:
        now = time.time()
        for posting in chunk:
            started = self.received_at.pop(posting["event_id"], None)
            if started is not None:
                self.latencies.append(now - started)

    def _record_feedback(self, response: httpx.Response, chunk: list[dict]) -> None:
        """Practice returns per-event diagnostics, including the reference's own
        legs for deposits and buy fills. Persist the raw response next to the
        event log so tools/scan.py can diff it offline -- reading it by eye
        after the run wastes the window."""
        if self.mode != "practice" or not self.book.event_log.path:
            return
        try:
            payload = loads(response.text)
        except Exception:
            return
        path = self.book.event_log.path.with_suffix(".feedback.jsonl")
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(dumps({"sent": chunk, "response": payload}) + "\n")
        except Exception:
            log.exception("could not persist feedback")

    def _record_checkpoint_feedback(self, checkpoint_id, as_of, snap,
                                    response: httpx.Response) -> None:
        """Persist the checkpoint diagnostics next to the event log.

        Practice "checkpoints score every part and name what diverges" -- and
        checkpoint correctness is 40 of the 100 points, the largest single
        component. Run 2 discarded every one of these responses, which meant
        the weakest part of the score was also the only part we had no
        diagnostics for. Written to its own file so tools/ can diff it.
        """
        if self.mode != "practice" or not self.book.event_log.path:
            return
        path = self.book.event_log.path.with_suffix(".checkpoints.jsonl")
        try:
            body = loads(response.text)
        except Exception:
            body = {"raw": response.text[:4000]}
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(dumps({
                    "checkpoint_id": checkpoint_id,
                    "as_of_event_id": as_of,
                    "sent": snap,
                    "response": body,
                }) + "\n")
        except Exception:
            log.exception("could not persist checkpoint feedback")

    def checkpoint(self, http: httpx.Client, payload: dict) -> None:
        """Snapshot FIRST, send second.

        The reply must describe the book as at the checkpoint's place in the
        stream. Taking the snapshot after the network round trip, or from
        another thread while the stream keeps running, reports a later state
        than the one being asked about.
        """
        checkpoint_id = payload.get("checkpoint_id")
        as_of = payload.get("as_of_event_id")           # G-3
        grace = float(payload.get("respond_within_seconds", 60))
        deadline = time.time() + grace

        try:
            snap = self.book.snapshot(as_of_event_id=as_of)
        except Exception:
            log.exception("snapshot failed for %s; sending what we can",
                          checkpoint_id)
            snap = {"trial_balance": {}, "customers": {}, "open_order_routes": {}}

        if as_of:
            log.info("checkpoint %s is AS-OF %s", checkpoint_id, as_of)

        self.flush(http)

        body = dumps({"checkpoint_id": checkpoint_id, **snap})
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                r = http.post(f"{self.url}/v1/checkpoint",
                              params={"mode": self.mode}, content=body,
                              headers={"Content-Type": "application/json"},
                              timeout=20)
                r.raise_for_status()
                self.stats["checkpoints"] += 1
                self._record_checkpoint_feedback(checkpoint_id, as_of, snap, r)
                return
            except httpx.HTTPError as exc:
                self.stats["errors"] += 1
                log.warning("checkpoint %s attempt %d failed (%s)",
                            checkpoint_id, attempt, type(exc).__name__)
                time.sleep(min(2 ** attempt, 10))
        self.stats["checkpoints_failed"] += 1
        log.error("checkpoint %s not delivered within %.0fs", checkpoint_id, grace)

    # -- consuming ----------------------------------------------------------
    def handle(self, ev: dict) -> None:
        """One ledger event. Never raises: an event we cannot handle costs that
        event, never the run (G-4)."""
        event_id = ev.get("event_id")
        try:
            self.received_at[event_id] = time.time()
            legs = self.book.apply(ev)
        except Exception:
            log.exception("book.apply blew up on %s", event_id)
            legs = []
        # An event you correctly reject still needs a submission, with no legs.
        # Not submitting at all scores zero for that event.
        if event_id:
            self.pending.append({"event_id": event_id, "legs": legs or []})
        self.stats["events"] += 1

    def _stream_params(self) -> dict:
        params = {"mode": self.mode, "from": self.cursor}
        # Only ever on the FIRST connect, and only when asked for explicitly:
        # a reconnect carrying new=true would spend an attempt (G-2).
        if self.start_new and self.first_connect:
            params["new"] = "true"
        return params

    def consume(self, http: httpx.Client, deadline: float) -> None:
        params = self._stream_params()
        last_flush = time.time()
        with http.stream("GET", f"{self.url}/v1/stream", params=params,
                         timeout=httpx.Timeout(STREAM_READ_TIMEOUT,
                                               connect=20)) as r:
            if r.status_code == 409:
                r.read()
                log.error("409 from /v1/stream: the last run on %s has finished. "
                          "Pass --new to deliberately start a new attempt.",
                          self.mode)
                self.finished_notice = True
                self.done = True
                return
            r.raise_for_status()
            self.first_connect = False

            etype = data = None
            for line in r.iter_lines():
                if time.time() > deadline:
                    log.warning("wall-clock ceiling reached; stopping")
                    return
                if line.startswith("event:"):
                    etype = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                elif line == "" and data is not None:
                    try:
                        ev = loads(data)
                    except Exception:
                        log.error("unparseable SSE frame, skipping: %.200s", data)
                        etype = data = None
                        continue

                    try:
                        if self._dispatch_frame(http, etype, ev):
                            return
                    except Exception:
                        log.exception("frame dispatch failed; carrying on")

                    if (len(self.pending) >= self.batch
                            or (time.time() - last_flush) * 1000 > self.flush_ms):
                        self.flush(http)
                        last_flush = time.time()
                    etype = data = None

    def _dispatch_frame(self, http: httpx.Client, etype: str, ev: dict) -> bool:
        """Returns True when the caller should stop consuming this connection."""
        if etype == "stream_open":
            log.info("connected: run %s, resumed at %s, next event in %ss",
                     ev.get("run_id"), ev.get("resumed_from"),
                     ev.get("next_event_in_seconds"))
            return False

        if etype == "stream_reset":
            # The server deliberately rewinds us and re-sends events we have
            # already seen. If the book is idempotent this costs nothing.
            self.cursor = ev.get("resume_from", self.cursor)
            self.stats["resets"] += 1
            log.info("stream_reset: resuming from %s", self.cursor)
            self.flush(http)
            return True

        if etype == "stream_end":
            log.info("stream_end")
            self.flush(http)
            self.done = True
            return True

        self.cursor = max(self.cursor, ev.get("offset", 0) + 1)
        if ev.get("type") == "checkpoint_request":
            # Not a ledger event and it produces no legs: no posting for it.
            self.checkpoint(http, ev.get("payload") or {})
        else:
            self.handle(ev)
        return False

    # -- run ----------------------------------------------------------------
    def run(self, max_seconds: float) -> dict:
        deadline = time.time() + max_seconds
        headers = {"Authorization": f"Bearer {self.key}"}
        with httpx.Client(headers=headers) as http:
            while time.time() < deadline and not self.done:
                try:
                    self.consume(http, deadline)
                except httpx.HTTPError as exc:
                    self.stats["reconnects"] += 1
                    log.warning("reconnecting after %s", type(exc).__name__)
                    time.sleep(1)
                except Exception:
                    # Never let anything end the run (G-4).
                    self.stats["reconnects"] += 1
                    log.exception("unexpected error in consume; reconnecting")
                    time.sleep(1)
            self.flush(http)
            try:
                me = http.get(f"{self.url}/v1/me", params={"mode": self.mode},
                              timeout=20).json()
            except httpx.HTTPError:
                me = {}
        self.book.close()
        return {"stats": self.stats, "me": me}

    def latency_report(self) -> dict:
        if not self.latencies:
            return {}
        ordered = sorted(self.latencies)
        p95_index = max(0, int(len(ordered) * 0.95) - 1)
        return {
            "n": len(ordered),
            "p50": round(statistics.median(ordered), 3),
            "p95": round(ordered[p95_index], 3),
            "max": round(ordered[-1], 3),
        }


def fetch_rules(url: str, key: str) -> dict:
    """/v1/rules is live and authoritative. "If this table and that endpoint
    ever disagree, the endpoint wins.\""""
    try:
        r = httpx.get(f"{url.rstrip('/')}/v1/rules",
                      headers={"Authorization": f"Bearer {key}"}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        log.warning("could not fetch /v1/rules; falling back to the local table")
        return {}


def run_ceiling(mode: str, rules: dict) -> float:
    """The wall-clock ceiling, derived from the mode (G-1).

    This is a backstop, not the intended stopping condition: stream_end ends
    the run. The margin is deliberately generous because the stream drains a
    tail past its nominal duration.
    """
    duration = MODE_DURATION[mode]
    modes = (rules or {}).get("modes") or {}
    live = (modes.get(mode) or {}).get("duration_seconds")
    if isinstance(live, (int, float)) and live > 0:
        duration = float(live)
    return float(duration) + TAIL_MARGIN_SECONDS


def normalise_confirmation(raw: str) -> str:
    """Whitespace and a UTF-8 BOM stripped.

    Some shells prepend a BOM when input is piped, which would reject a CORRECT
    answer -- baffling at the exact moment you least want to be baffled.
    """
    return raw.strip().lstrip("﻿").strip()


def confirm_scored_run(mode: str, seconds: float, start_new: bool,
                       read_line=input) -> bool:
    """Guard on a scarce resource: submission has 3 attempts, final has 1.

    The prompt is a plain print(), NOT input()'s prompt argument. That argument
    goes through the C-level readline path and gets swallowed when stderr is
    redirected -- which leaves a bare cursor and no instruction on screen. Being
    unclear here is how an attempt gets wasted.
    """
    print(f"\n  You are about to start a {mode.upper()} run.")
    print(f"  Attempts are limited and this one WILL count.")
    print(f"  Wall-clock ceiling: {seconds:.0f}s. Start a new run: {start_new}.")
    print(f"\n  Type exactly:  {mode}")
    print("  (anything else cancels; nothing has been sent to the server yet)")
    print("  > ", end="", flush=True)
    try:
        typed = normalise_confirmation(read_line())
    except EOFError:
        typed = ""
    if typed != mode:
        print(f"\n  Cancelled. You typed {typed!r}; expected {mode!r}.")
        print("  NO ATTEMPT WAS CONSUMED -- the run was never started.")
        return False
    print()
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://hiring-arena.twocc.in")
    ap.add_argument("--key", required=True, help="your API key from the portal")
    ap.add_argument("--mode", default="practice",
                    choices=["practice", "submission", "final"])
    ap.add_argument("--seconds", type=float, default=None,
                    help="wall-clock ceiling; defaults to the mode's duration "
                         "plus a tail margin. stream_end normally ends the run.")
    ap.add_argument("--new", action="store_true",
                    help="deliberately start a NEW run. Required on submission "
                         "and final after a previous run has finished.")
    ap.add_argument("--capture", default=None,
                    help="path for the JSONL event log (enables crash recovery "
                         "and offline replay). Recommended always.")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stderr,
    )

    rules = fetch_rules(a.url, a.key)
    seconds = a.seconds if a.seconds is not None else run_ceiling(a.mode, rules)

    if a.mode != "practice" and not confirm_scored_run(a.mode, seconds, a.new):
        return 1

    capture = a.capture
    if capture is None:
        capture = f"captures/{a.mode}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    Path(capture).parent.mkdir(parents=True, exist_ok=True)

    c = ArenaClient(a.url, a.key, a.mode, start_new=a.new, log_path=capture)
    log.info("connecting to %s as %s (ceiling %.0fs, capture %s)",
             a.url, a.mode, seconds, capture)
    out = c.run(seconds)

    print("\nstats:", dumps(out["stats"]))
    print("ledger:", dumps(c.book.summary()["stats"]))
    latency = c.latency_report()
    if latency:
        print("latency:", dumps(latency),
              "(full marks under 5s at p95)")

    summary = c.book.summary()
    if summary["unknown_types"]:
        print("\n!! UNKNOWN EVENT TYPES (scoring zero on all of these):")
        for t, n in summary["unknown_types"].items():
            print(f"  {t:<30} {n:>5}")

    todo = c.book.todo
    if todo:
        print(f"\nnot implemented yet ({sum(todo.values())} events skipped):")
        for t, n in sorted(todo.items(), key=lambda kv: -kv[1]):
            print(f"  {t:<30} {n:>5} events")

    if summary["rejections_by_reason"]:
        print("\nrejections (over-rejection is the expensive failure mode):")
        for reason, n in sorted(summary["rejections_by_reason"].items(),
                                key=lambda kv: -kv[1]):
            print(f"  {reason:<50} {n:>5}")

    if summary["crashes_by_type"]:
        print("\nhandler crashes (bugs to fix offline):")
        for t, n in summary["crashes_by_type"].items():
            print(f"  {t:<30} {n:>5}")

    print(f"\ncapture written to {capture}")

    me = out.get("me") or {}
    # /v1/me returns the BEST run; latest_run is the attempt we are actually
    # iterating on, and is what to read while developing.
    latest = me.get("latest_run") or {}
    for label, block in (("best", me), ("latest", latest)):
        if block.get("score") is not None:
            print(f"\nscore ({label}): {block['score']}")
            for k, v in (block.get("breakdown") or {}).items():
                print(f"  {k:<26} {v.get('points'):>6} / {v.get('max')}")
    if me.get("score") is None:
        print("\nscore: withheld on this tier")
    return 0


if __name__ == "__main__":
    sys.exit(main())
