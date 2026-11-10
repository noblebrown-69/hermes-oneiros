"""Decision-row UI rules for the Oneiros dashboard (mirrored in dist/index.js).

review.jsonl is append-only; the latest entry wins. Once a proposal has a
guardian/apply decision, the dashboard hides Accept/Reject/Defer and shows the
badge + actor/time, with a small "Change decision" control to re-reveal them.
"""
from __future__ import annotations

DECIDED = frozenset({"accept", "reject", "defer", "applied", "rolled-back"})


def is_decided(last_decision: str | None) -> bool:
    return bool(last_decision) and last_decision in DECIDED


def show_decision_actions(last_decision: str | None, editing: bool) -> bool:
    """True when Accept/Reject/Defer should be visible."""
    return (not is_decided(last_decision)) or bool(editing)
