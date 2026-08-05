"""Event handlers, one module per family.

Every handler has the signature ``(state, payload, event) -> list[dict]`` and
either returns legs, returns ``[]``, or raises ``Rejected`` / ``NotImplementedYet``.
None of them may mutate state and then raise: apply the state change last, or
apply it atomically, so a rejection leaves the book exactly as it was.
"""
