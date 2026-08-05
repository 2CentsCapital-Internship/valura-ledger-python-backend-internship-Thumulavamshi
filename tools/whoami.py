#!/usr/bin/env python3
"""Find your leaderboard pseudonym and standing.

The leaderboard shows "best run per candidate, under a stable pseudonym"
(names like `amber-shrike-50`). Your own pseudonym is not printed by the run
summary, so this asks the API for it and then locates you in the standings.

    python tools/whoami.py --key ak_your_key_here
    python tools/whoami.py --key ak_... --mode submission

It dumps the full /v1/me payload rather than guessing at field names, then
tries to match you against /v1/leaderboard by pseudonym or by score.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

PSEUDONYM_HINTS = ("pseudonym", "handle", "alias", "display_name",
                   "candidate", "name", "nickname", "codename")


def get(url: str, key: str, path: str, params: dict) -> dict | list | None:
    try:
        r = httpx.get(f"{url.rstrip('/')}{path}",
                      headers={"Authorization": f"Bearer {key}"},
                      params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(f"  ! {path} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def find_pseudonym(me: dict) -> str | None:
    """Look for a pseudonym-shaped value anywhere in the payload."""
    found = []

    def walk(node, trail=""):
        if isinstance(node, dict):
            for k, v in node.items():
                where = f"{trail}.{k}" if trail else k
                if isinstance(v, str) and any(h in k.lower()
                                              for h in PSEUDONYM_HINTS):
                    found.append((where, v))
                walk(v, where)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{trail}[{i}]")

    walk(me)
    if found:
        print("\n  candidate pseudonym fields:")
        for where, value in found:
            print(f"    {where:<28} = {value}")
        # Prefer a hyphenated three-part name, the observed leaderboard shape.
        for _where, value in found:
            if value.count("-") == 2:
                return value
        return found[0][1]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="https://hiring-arena.twocc.in")
    ap.add_argument("--key", required=True)
    ap.add_argument("--mode", default="practice",
                    choices=["practice", "submission", "final"])
    a = ap.parse_args()

    print(f"== GET /v1/me?mode={a.mode} ==")
    me = get(a.url, a.key, "/v1/me", {"mode": a.mode})
    if me is None:
        return 1
    print(json.dumps(me, indent=2))

    pseudonym = find_pseudonym(me)
    if pseudonym:
        print(f"\n  >> your pseudonym looks like: {pseudonym}")
    else:
        print("\n  no obvious pseudonym field -- check the dump above by eye")

    print(f"\n== GET /v1/leaderboard?mode={a.mode} ==")
    board = get(a.url, a.key, "/v1/leaderboard", {"mode": a.mode})
    if board is None:
        return 0

    rows = board.get("leaderboard") or board.get("entries") \
        or board.get("standings") if isinstance(board, dict) else board
    if not isinstance(rows, list):
        print(json.dumps(board, indent=2)[:3000])
        return 0

    my_score = me.get("score")
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            print(f"  {i:>3}. {row}")
            continue
        name = next((row[k] for k in row
                     if isinstance(row.get(k), str)
                     and any(h in k.lower() for h in PSEUDONYM_HINTS)), "?")
        score = row.get("score")
        mine = ""
        if pseudonym and name == pseudonym:
            mine = "   <<< YOU"
        elif my_score is not None and score == my_score:
            mine = "   <<< matches your score"
        rank = row.get("rank", i)
        print(f"  {rank:>3}. {str(name):<24} {str(score):>8}{mine}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
