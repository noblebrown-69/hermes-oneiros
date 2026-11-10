---
name: dream-cycle
description: Oneiros Dream Cycle — review nightly memory proposals. If the user asks whether there are memories or dreams to review, call dream_cycle_review_status and answer from that live result, including dashboard_url. Do not guess.
---

# Oneiros Dream Cycle

> Dream the day. Wake as yourself.

**When:** at the start of a day, or when `dream_cycle_status` shows a new stage.

1. Call `dream_cycle_status`. If `latest.count` is 0, there is nothing to review.
2. Read `<latest_stage>/diff.md`. Each line names a label, the competing values,
   and where memory says it.
3. For each proposal, confirm the current value from a real source (config,
   service, the user) before changing memory. Never pick a value just because
   it is newer.
4. Retire the old value with a `SUPERSEDED` marker (Waking Check's
   `memory_supersede`) rather than silently deleting it.
5. `session_status: no-sessions` means a quiet day — not an error.
   `unavailable` means the session source failed; mention it, don't guess.

Dream Cycle never rewrites memory and never calls a model.

## Asked "anything to review?" (v0.8.4)

Call `dream_cycle_review_status`. Answer only from that JSON.

- `pending_review` — still needs a human decision (including a reverted dream). This is the unreviewed queue.
- `accepted_awaiting_apply` — already accepted. Not unreviewed. Mention it separately.
- Both zero — say there is nothing waiting. Do not invent a dream or a link.
- `dashboard_url` — include it when present. Never invent a hostname.
- `snoozed_until` — proactive reminders are quiet until then. Still answer the question.
- Snooze proactive reminders with `dream_cycle_snooze` or `/dream-cycle snooze` (seven days, this profile only).


## Apply (v0.3, gated by `apply_enabled`)
1. Confirm which value is current. A cited source is welcome; it is not required when the proposal already has an explicit accept.
2. `dream_cycle_apply` with `proposal_id`, `keep`, `actor`, and `evidence` when you have a source (`dry_run: true` first). Blank evidence applies only after an accept whose `keep` matches. `accepted: true` applies today's accept rows, including ones stored with empty evidence.
3. Same call without `dry_run`. Report the receipt id and `sha_before` → `sha_after`.
4. Undo: `dream_cycle_rollback` with `last: true` (or `hermes dream-cycle rollback --last`).
Never apply a value you did not verify. Never invent evidence, and never put credentials in evidence; the secret scan refuses.

## Status and evidence (v0.4)
- `/dream-cycle status` shows freshness, files scanned (WARN on 0), undecided count and the last receipt. Check it before any apply.
- Every apply records `evidence_status`: **grounded** means the kept value appeared in that day's user/tool messages in state.db; **unverified** means it did not. Say which one in your report. With `require_grounded_evidence: true`, unverified applies are refused.

## User facts (v0.7)
- Proposals with `kind: user_fact` come from role=user assertions only.
- Review `diff.md`; confirm the user actually said it before accept/apply.
- Apply appends a line (USER.md or MEMORY.md STATUS:) with a provenance note; rollback restores the snapshot.
- Never put user-fact content into web searches.

## Don't save (v0.8)
- A user "don't save" / "off the record" / "this whole conversation" directive
  is honored: that exchange, or the whole session, is not mined for user facts
  or session corroboration.
- Status reports skip counts and reasons only. The review ledger stores
  `nosave-skip` rows with hashes, never message text. Do not quote skipped
  content back to the user unless they ask.
