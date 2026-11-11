# 🌙 Oneiros Dream Cycle

<p align="center">
  <img src="assets/banner.png" alt="Oneiros — Your agent sleeps on it." width="100%">
</p>

> **Your agent sleeps on it.**
>
> While Hermes sleeps, Oneiros sifts the day and lets the stale memories fade like old dreams.
>
> DREAM AT NIGHT · WAKE TO PROPOSALS · KEEP WHAT'S TRUE · A RECEIPT FOR EVERY CHANGE

Agents forget in a specific, embarrassing way: they keep quoting yesterday's
facts as today's. Dream Cycle is the nightly pass that catches it. While the
agent "sleeps", it replays the day's labeled facts (builds, throughput, ports —
or any label you define) against `MEMORY.md`, and stages **proposals** for a
morning review.

It never rewrites memory. It never calls a model. It's a deterministic regex
pass with a lock, a kill switch, and a dated paper trail.

This repository is the Oneiros Dream Cycle plugin only (catalog name `oneiros`,
plugin id `dream-cycle`, version 0.8.8). Distinct from the community plugin
`hermes-dreaming`.

The official catalog entry is `plugin-catalog/oneiros.yaml` in
NousResearch/hermes-agent (name `oneiros`, version 0.8.8).
`catalog/dream-cycle.yaml` in this repo is a stale draft, not that entry.

## Before / after

**Before** — memory says one thing, the day said another:

```text
MEMORY.md:   - llama build b12000 is live
yesterday:   "upgraded llama build to b12500 this afternoon"
```

**After a Dream Cycle night** — memory is untouched; a proposal waits:

```text
dream-proposals/2030-01-02/diff.md
- **build**: b12000, b12500 (MEMORY.md:3) — memory says b12000;
  2030-01-01 sessions said b12500 — confirm which is current
```

Run it yourself (synthetic data, ~1 second): `./scripts/demo.sh`

## Session warm (optional)

Dream Cycle registers an `on_session_open` callback that warms the status
cache before the first CLI, TUI, or Desktop turn (well under 5 seconds, never
raises; messaging platforms do not fire the hook). A Hermes build that does
not implement the hook logs a warning and still loads the plugin. Do not
upgrade Hermes just to get this hook.

## Install

```bash
hermes plugins install https://github.com/noblebrown-69/hermes-oneiros --enable
cp config.example.yaml "$HERMES_HOME/plugin-data/dream-cycle/config.yaml"   # optional
```

## Surfaces

| Surface | Name | What it does |
|---|---|---|
| Tool | `dream_cycle_propose` | Stage proposals for a date (default today) |
| Tool | `dream_cycle_status` | Kill switch, latest stage + count, session status, apply gate |
| Tool | `dream_cycle_apply` | Gated apply of one reviewed proposal (or all ledger `accept` rows with `keep`) |
| Tool | `dream_cycle_rollback` | Restore from the pre-apply snapshot (`--last` or `--receipt ID`) |
| Tool | `dream_cycle_receipts` | List apply receipts with before/after sha256 |
| Tool | `dream_cycle_review_status` | Live pending-review and accepted-not-applied counts |
| Tool | `dream_cycle_snooze` | Pause review reminders for `snooze_days` (default 7) |
| Slash | `/dream-cycle propose [YYYY-MM-DD] \| status \| snooze` | Propose, status, or pause reminders, in-session |
| CLI | `hermes dream-cycle propose --date … \| status` | Same, for cron / shell |
| Skill | `dream-cycle` | How to run the morning review |

## What it finds

### Learn about the person (v0.7.3)

Besides stack labels, Dream Cycle mines **only the user's own messages** for
first-person and explicit name+role assertions and stages proposals:

| Category | Example user text | Target |
|---|---|---|
| PERSON | "Alex is my sibling." / "Maya is a teacher." | `USER.md` |
| EVENT | "I'm taking a trip to Harbor City on Nov 19." / show or interview dates | `MEMORY.md` as `STATUS:` with expiry |
| PREFERENCE | "I prefer dark roast." | `USER.md` |
| STATUS | "I live in Portland." / "I'm employed, looking for a new one." | `USER.md` |

Questions (`Who is Alex?`), bare name drops, hypotheticals, compaction
handoffs, and assistant/tool text never become user-fact proposals. A
capitalized proper name is required (so "My dad makes …" is not a PERSON).
Same-place trip dates collapse: an explicit itinerary supersedes a relative
"week or so", and expiry is the end of the stay when a range is stated. Demo:
`./scripts/demo-userfacts.sh`.

### Do not save (v0.8.0)

If a user says "don't save this", "off the record", "forget I said this", or a
similar directive, Dream Cycle skips that exchange (the user message and the
following bot turn). If the phrase is in the first user message of a session,
or names "this whole conversation", the whole session is skipped. User-fact
extraction and session corroboration both honor the skip. The status screen
and the review ledger record skip counts and message hashes only — never the
message text. Quoted questions about the feature, fenced code, and
first-person declaratives like "I don't remember" do not trigger. Toggle with
`honor_nosave` (default `true`). Demo: `./scripts/demo-nosave.sh`.

1. **Inside memory**: one label, two different values in the same file
   (`port :9119` and `port :8080`).
2. **Memory vs. the day** (optional): a value from yesterday's sessions that
   memory doesn't know yet. Enable `session_corroboration` and point it at any
   read-only command that prints the day's log as JSON (`{"lines": [...]}` or
   `{"daily_log": {"work_items": [...]}}`).

From v0.6.1, session corroboration ignores `role=tool` dumps and compaction summaries by default (`ignore_tool_messages` / `ignore_compaction_messages`), and skips memory-side multi-value proposals when each value names a different service (`skip_distinct_service_conflicts`).

Quiet days are normal. Many day-log APIs answer **404 "day not found"** when an
agent simply had no sessions that day. Dream Cycle checks the day index first,
and records `session_status: no-sessions` instead of failing. Real failures
(auth, timeouts, bad JSON) are recorded as `unavailable`, and the memory-only
pass still runs.

## Safety

- **Proposal-first.** `apply_enabled: false` by default. v0.3 adds a gated apply: every apply needs a named actor, writes a pre-apply snapshot, secret-scans inputs and written lines (refuses on a hit), retires the old value with a column-0 `SUPERSEDED` marker instead of deleting anything, refuses to grow MEMORY.md past the Hermes char limit, verifies the write, and logs an `applied` receipt (proposal, evidence, speaker, sha_before/sha_after) to `review.jsonl`. Evidence is optional once a human has accepted that proposal (v0.8.2); a non-empty source still authorizes a direct apply, and blank evidence is stored blank. `hermes dream-cycle rollback --last` restores the snapshot (refuses if the file changed since, unless `--force`). v0.3.1 safe-write guard: apply/rollback hold Hermes' `MEMORY.md.lock`, re-check the sha right before the atomic write (compare-and-swap), edit only the proposal's own lines (`--all-lines` to widen), refuse a line that carries both the kept and a retired value, and refuse stale proposals whose cited lines changed since propose.
- **Honors "don't save".** `honor_nosave: true` by default. Skipped ranges are
  hashed in the ledger, never written out as text.
- **No model calls.** `model_used: false` is written into every summary.
- **Kill switch.** `touch $HERMES_HOME/plugin-data/dream-cycle/DISABLED`.
- **Lock.** Overlapping runs fail fast with a clear message.
- **Readable errors.** Bad regexes, bad dates and bad YAML come back as
  `{"ok": false, "error": "labels[0] (port): bad regex in 'pattern': …"}`.

## Output

`$HERMES_HOME/plugin-data/dream-cycle/dream-proposals/<date>/summary.json` and
`diff.md` (proposals, overflow count, session date/status, `model_used: false`).

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install pytest pyyaml
.venv/bin/pytest -q          # synthetic fixtures only; PyYAML stands in for hermes_yaml
./scripts/scrub-check.sh     # generic secret patterns; optional scripts/scrub-local.txt
./scripts/demo.sh
```

MIT licensed.

## Exo-derived evidence + guardian ledger (2026-11-06)

### MECH: / STATUS: tier markers (recognition only)

Two tiers, marked by the **first token** of a memory line (an optional leading `- ` or `* ` is allowed):

- `MECH:` — mechanism tier. How something works (a rule, a wiring fact, a procedure). Durable; change it only with evidence.
- `STATUS:` — status tier. What is true right now (up/down, current value, in-progress). Expected to go stale.

**Job-relative placement:** write the line under the heading of the job, routine, or service it describes (for example under `## Dream Cycle 06:30` or `## llama-server`), not in a global catch-all section. The tier tells a reader how much to trust it; the placement tells them what it belongs to.

Dream Cycle propose **recognizes** these lines: it lists their locations under `tier_markers` in the stage manifest/summary and **does not mine them for values** (no guessing free text on tier lines). They are **not** Proposal or receipt fields yet — that promotion waits on a two-profile proof week.

### Optional speaker on proposals

Lines that start with `ATTR Name@Venue ...` produce proposals with optional `speaker`. Old manifests without these keys still load.

### ISO dates are not values

A number inside an ISO date/datetime (e.g. `2026` in `2026-11-05`) is never proposed as a port/build/value. Neither is a number inside an arXiv/DOI id (e.g. `2611` in `arXiv:2611.00463`).

### Guardian review ledger

Each dated stage folder may hold an append-only `review.jsonl`:

```json
{"proposal_id": "…", "evidence": "…", "decision": "accept|reject|defer", "actor": "reviewer", "ts": "…", "note": "…"}
```

Every proposal in the manifest carries a stable `proposal_id`. Rows are only ever appended; the latest row per id wins. Defer stays open. Nothing decides automatically, and a decision only applies when `apply_enabled: true` and `dream-cycle apply --accepted` runs (an `accept` row must carry `--keep VALUE`). Append a row:

```bash
python -m dream_cycle.review append <stage-dir> <proposal_id> defer reviewer --evidence "unverified" --note "recheck Friday"
python -m dream_cycle.review list <stage-dir>
```

### Dashboard (v0.8.3)

The Oneiros tab on the Hermes dashboard reviews staged proposals. **Accept**
(and **Accept all**) write `review.jsonl` only — they do not change bot
memory. Evidence is optional on accept and on **Apply accepted…**; an explicit
accept is enough, and older blank-evidence accepts apply without being
rewritten. **Undo** on an Accept-all batch appends a `reverted` row (the
ledger is never rewritten). Apply stays a separate step behind `apply_enabled`.
The confirm dialog keeps its title and Apply / Cancel on screen; each dry-run
is one row, and the line diff opens only when you ask.

Open a profile's night directly with
`/oneiros?profile=<name>&day=YYYY-MM-DD`. The page loads that profile and that
day. **Snooze for a week** is a button (POST); opening the link does not snooze.

### Review reminders (v0.8.4)

If dreams stay unreviewed for more than `overdue_days` (default 7), a proactive
reminder may go out at most once per `cooldown_hours` (default 24) for that
profile. Accepted dreams are already reviewed. An empty queue stays quiet.
`/dream-cycle snooze` or the dashboard button pauses nags for `snooze_days`
(default 7); asking in chat still gets a live answer from
`dream_cycle_review_status`.

Set `review_reminder.dashboard_base_url` to the dashboard origin (no
credentials). The link is `{base}/oneiros?profile=<name>&day=<YYYY-MM-DD>`.
Leave the origin empty and the count is still real, but no host is invented.

Desktop, dashboard, and API chats do not get an idle push. On a host that
lists `pre_llm_call`, the next private owner turn receives the reminder as
turn context (the model is told to mention the count, the link, and snooze).
A group, a channel, a thread, or a messaging chat with no DM type gets
nothing. CLI and TUI are the operator's own session. This is not active
until the plugin host reloads.

`scripts/review-reminder.py` remains the optional cron path for a transport
that really has an origin: `deliver: origin` only works when the job was
created in a live session that set platform and chat id. `api_server` origin
is not a push target. Do not schedule that script against Desktop or API
chats. Empty stdout stays silent, and the script records an emit time rather
than a delivery receipt.

### Manifest tools (v0.8.5)

`provides_tools` lists `dream_cycle_review_status` and `dream_cycle_snooze`
with the five older tools, and `provides_hooks` lists `pre_llm_call`. Hosts
that catalog or validate from the manifest were only advertising the older
five.

The reminder is the next private owner turn via `pre_llm_call`, not a
background push. Nothing is sent while the user is idle. Groups, channels,
threads, and messaging chats with no DM type are skipped, and a skip does not
consume the 24 hour cap. `scripts/review-reminder.py` stays the optional cron
path only when that transport really has an origin chat. It is not the path
for Desktop or API.

### Apply a whole file (v0.8.6)

**Apply accepted** can apply a whole file in one click. A night of accepts
that share `MEMORY.md` or `USER.md` used to land one receipt and refuse the
rest: the first write changed the hash and shifted line numbers, so the
sibling lines looked stale. Sibling lines in the same click are remapped. A cited
line whose text actually changed, and any edit from outside this batch, is
still refused. `apply-refused` ledger rows include a non-empty `reason` (the
same text as `evidence`). Roll receipts back from newest to oldest to restore
a file that took several writes. `apply_enabled`, the kill switch, the sha
re-check, same-line conflicts, secret scan, snapshots, and rollback are
unchanged.

### Configurable profiles and public scrub (v0.8.7)

`profile_allowlist` replaces the built-in list of profile names. Leave it
empty (the default) and every profile with the plugin installed is in scope.
A non-empty list limits the tab to those directory names. A profile without
the plugin gets a generic install message.

`scripts/scrub-check.sh` checks generic secret and home-path patterns only.
Extra name patterns belong in gitignored `scripts/scrub-local.txt` (one
extended regex per line). The published script does not list them.

### Host YAML, locks, and hooks (v0.8.8)

Config, Hermes char limits, and the dashboard version read go through
`hermes_yaml.safe_load`. Hermes does not ship PyYAML, so copying in
`config.yaml` no longer raises ImportError on the next tool call.
`python_dependencies` stays empty. This plugin does not import other plugins.
File locks use `fcntl.flock` where that module exists. Where it does not,
they use `msvcrt.locking` on one byte at the start of the same lock file.
`pre_llm_call` and `on_session_open` register unconditionally. A host that
does not know a hook logs a warning and still loads the plugin.

### Session cost evidence

`summary.json` → `session_costs` is a read-only view (SQLite `mode=ro`, `query_only`) of `$HERMES_HOME/state.db` for the dreamed day (local midnight to midnight): sessions, input/output/cache_read/cache_write/reasoning tokens, `cache_read_share` = cache_read / (input + cache_read) when input > 0, tool and API call counts, and `by_model` from `session_model_usage`. USD shows up only when Hermes recorded a non-zero value; local models show "not reported". If the DB is missing or unreadable the status is `unavailable` and the run goes on. Turn it off with `session_costs: {enabled: false}`.
