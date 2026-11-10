DREAM_CYCLE_PROPOSE = {
    "name": "dream_cycle_propose",
    "description": ("Oneiros Dream Cycle: stage proposal-only memory consolidations for morning review. "
                    "Finds labeled values (build/throughput/port, configurable) that disagree within memory "
                    "or against yesterday's sessions. Never rewrites memory. No model calls."),
    "parameters": {"type": "object",
                   "properties": {"date": {"type": "string", "description": "Stage date YYYY-MM-DD (default today)"}},
                   "additionalProperties": False},
}
DREAM_CYCLE_STATUS = {
    "name": "dream_cycle_status",
    "description": "Oneiros Dream Cycle status: kill switch, latest stage, session corroboration, apply gate.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
DREAM_CYCLE_APPLY = {
    "name": "dream_cycle_apply",
    "description": ("Oneiros Dream Cycle apply (gated by apply_enabled): retire the stale value(s) of one staged "
                    "proposal in favour of `keep`. Requires a named actor. Evidence is optional when the proposal "
                    "was explicitly accepted (or pass a real source for a direct apply). Snapshots first, "
                    "secret-scans, inserts a SUPERSEDED marker (never deletes), logs a receipt to review.jsonl, "
                    "and is reversible with dream_cycle_rollback. Use dry_run first."),
    "parameters": {"type": "object", "properties": {
        "proposal_id": {"type": "string"}, "keep": {"type": "string", "description": "the current value"},
        "evidence": {"type": "string", "description": "optional source note; omit when an explicit accept is enough"},
        "actor": {"type": "string", "description": "who decided"},
        "date": {"type": "string", "description": "stage date YYYY-MM-DD (default latest)"},
        "retire": {"type": "array", "items": {"type": "string"},
                   "description": "stale value(s) to retire; required when the proposal has 3+ values"},
        "all_lines": {"type": "boolean", "description": "edit every live line with the retired value, not only the proposal's lines (default false)"},
        "dry_run": {"type": "boolean"}, "accepted": {"type": "boolean",
                    "description": "apply all review.jsonl accept rows that carry keep"}},
        "additionalProperties": False},
}
DREAM_CYCLE_ROLLBACK = {
    "name": "dream_cycle_rollback",
    "description": "Undo a Dream Cycle apply from its pre-apply snapshot (refuses if the file changed since, unless force).",
    "parameters": {"type": "object", "properties": {"receipt_id": {"type": "string"}, "last": {"type": "boolean"},
                   "force": {"type": "boolean"}, "actor": {"type": "string"}}, "additionalProperties": False},
}
DREAM_CYCLE_REVIEW_STATUS = {
    "name": "dream_cycle_review_status",
    "description": (
        "Live Oneiros review status for this profile. Call this when the user asks whether there are "
        "memories or dreams to review. Reads the local review ledger only (no model, no network). "
        "pending_review is still waiting for a human decision. accepted_awaiting_apply was accepted and "
        "is not unreviewed. Both zero means nothing is waiting — say so, do not invent a queue. "
        "Include dashboard_url when it is present; never invent a host. Snooze does not hide this answer."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
DREAM_CYCLE_SNOOZE = {
    "name": "dream_cycle_snooze",
    "description": (
        "Snooze proactive Oneiros review reminders for this profile (default seven days). "
        "Does not change review status. An explicit question still uses dream_cycle_review_status."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
DREAM_CYCLE_RECEIPTS = {
    "name": "dream_cycle_receipts",
    "description": "List Dream Cycle apply receipts (hashes, rollback command).",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
