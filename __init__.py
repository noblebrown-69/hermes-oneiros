"""Oneiros Dream Cycle Hermes plugin."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent

if __package__:
    from . import plugin_schemas as schemas
    from .plugin_tools import (tool_dream_cycle_propose, tool_dream_cycle_status, tool_dream_cycle_apply,
                               tool_dream_cycle_rollback, tool_dream_cycle_receipts,
                               tool_dream_cycle_review_status, tool_dream_cycle_snooze)
else:
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    import plugin_schemas as schemas  # type: ignore
    from plugin_tools import (tool_dream_cycle_propose, tool_dream_cycle_status, tool_dream_cycle_apply,  # type: ignore
                              tool_dream_cycle_rollback, tool_dream_cycle_receipts,
                              tool_dream_cycle_review_status, tool_dream_cycle_snooze)


def handle_slash(args: str = "") -> str:
    parts = (args or "").split()
    if parts and parts[0] == "propose":
        return tool_dream_cycle_propose(date=parts[1] if len(parts) > 1 else None)
    if parts and parts[0] == "receipts":
        return tool_dream_cycle_receipts()
    if parts and parts[0] == "rollback":
        return tool_dream_cycle_rollback(receipt_id=parts[1] if len(parts) > 1 else None, last=len(parts) == 1,
                                         actor="slash")
    if parts and parts[0] in ("review-status", "review"):
        return tool_dream_cycle_review_status()
    if parts and parts[0] == "snooze":
        return tool_dream_cycle_snooze()
    if parts and parts[0] not in ("status",):
        return f"Unknown subcommand {parts[0]!r}. Usage: /dream-cycle [propose [YYYY-MM-DD]|status|review-status|snooze|receipts|rollback [ID]]"
    out = tool_dream_cycle_status()
    try:
        return json.loads(out).get("text") or out
    except ValueError:
        return out


def _setup_cli(parser) -> None:
    sub = parser.add_subparsers(dest="dc_cmd")
    p = sub.add_parser("propose", help="Stage proposal-only consolidations (no model)")
    p.add_argument("--date", help="Stage date YYYY-MM-DD (default today)")
    st = sub.add_parser("status", help="Freshness, files scanned, undecided, last receipt, apply gate")
    st.add_argument("--text", action="store_true", help="human-readable block instead of JSON")
    a = sub.add_parser("apply", help="Apply one reviewed proposal (gated by apply_enabled)")
    a.add_argument("--id", dest="proposal_id"); a.add_argument("--keep")
    a.add_argument("--evidence", default="", help="optional source note; an explicit accept is enough")
    a.add_argument("--actor", default=""); a.add_argument("--date"); a.add_argument("--dry-run", action="store_true")
    a.add_argument("--retire", action="append", help="stale value to retire (repeatable; needed for 3+ values)")
    a.add_argument("--all-lines", action="store_true", help="edit every live line with the retired value (default: proposal lines only)")
    a.add_argument("--accepted", action="store_true", help="apply every review.jsonl accept row with keep")
    r = sub.add_parser("rollback", help="Restore memory from an apply's pre-apply snapshot")
    r.add_argument("--receipt"); r.add_argument("--last", action="store_true"); r.add_argument("--force", action="store_true")
    r.add_argument("--actor", default="cli")
    sub.add_parser("receipts", help="List apply receipts")
    sub.add_parser("review-status", help="Live pending-review counts and dashboard link (no send)")
    sub.add_parser("snooze", help="Snooze proactive review reminders for this profile")
    sub.add_parser("remind", help="Emit one overdue reminder for cron stdout, or nothing")


def _cli_handler(args) -> None:
    cmd = getattr(args, "dc_cmd", None)
    if cmd == "propose":
        print(tool_dream_cycle_propose(date=getattr(args, "date", None)))
    elif cmd == "apply":
        print(tool_dream_cycle_apply(proposal_id=args.proposal_id, keep=args.keep, evidence=args.evidence,
                                     actor=args.actor, date=args.date, dry_run=args.dry_run, accepted=args.accepted,
                                     retire=args.retire, all_lines=args.all_lines))
    elif cmd == "rollback":
        print(tool_dream_cycle_rollback(receipt_id=args.receipt, last=args.last, force=args.force, actor=args.actor))
    elif cmd == "receipts":
        print(tool_dream_cycle_receipts())
    elif cmd == "review-status":
        print(tool_dream_cycle_review_status())
    elif cmd == "snooze":
        print(tool_dream_cycle_snooze())
    elif cmd == "remind":
        from dream_cycle.remind import emit_for_scheduler
        from plugin_tools import _cfg, _home
        text = emit_for_scheduler(_home(), _cfg())
        if text:
            print(text)
    elif getattr(args, "text", False):
        print(json.loads(tool_dream_cycle_status()).get("text") or tool_dream_cycle_status())
    else:
        print(tool_dream_cycle_status())



def _hermes_handler(fn):
    """Hermes' tool registry calls ``handler(args_dict, **kwargs)``; our tools take keyword
    arguments. Unpack the dict so chat tool calls work (2026-11-08 fix: zero-arg tools raised
    TypeError and arg tools got the dict as their first parameter)."""
    import functools

    @functools.wraps(fn)
    def _call(args=None, **kwargs):
        merged = dict(args) if isinstance(args, dict) else {}
        merged.update(kwargs)
        return fn(**merged)
    return _call



_REVIEW_PROMPT = (
    "Oneiros: if the user asks whether there are memories or dreams to review, "
    "call dream_cycle_review_status and answer only from that JSON. "
    "pending_review is waiting for a decision. accepted_awaiting_apply was already accepted "
    "and is not unreviewed. If both are 0, say there is nothing waiting. "
    "Include dashboard_url when present; never invent a host or a count. "
    "Snooze suppresses proactive reminders only; an explicit question still gets this status. "
    "To snooze proactive reminders, call dream_cycle_snooze or /dream-cycle snooze."
)


def _register_review_prompt(ctx) -> None:
    """Supported Hermes prompt section, when this host exposes it. Never raises."""
    register = getattr(ctx, "register_system_prompt_section", None)
    if not callable(register):
        return
    try:
        register("dream-cycle.review", _REVIEW_PROMPT, position="after_memory")
    except Exception:
        pass


def _make_pre_llm_call():
    """Inject a due reminder into this private turn. Never raises, never pushes."""

    def pre_llm_call(**kwargs):
        try:
            from dream_cycle.config import load_config
            from dream_cycle.paths import data_root
            from dream_cycle.remind import turn_reminder_context
            from plugin_tools import _home
            home = _home()
            cfg_path = data_root(home) / "config.yaml"
            cfg = load_config(cfg_path if cfg_path.is_file() else None)
            text = turn_reminder_context(home, cfg, **kwargs)
            if not text:
                return None
            return {"context": text}
        except Exception:
            return None

    return pre_llm_call


def _make_on_session_open():
    """Build the callback. Signature matches docs: session_id, platform, reason, **kwargs."""
    def on_session_open(session_id: str = "", platform: str = "", reason: str = "", **kwargs):
        # Never raise into the host. Keep well under the ~5s turn-wait budget.
        try:
            from dream_cycle.session_warm import warm_session_caches
            warm_session_caches(
                session_id=session_id or "",
                platform=platform or "",
                reason=reason or "",
            )
        except Exception:
            pass
    return on_session_open


def register(ctx) -> None:
    toolset = "dream-cycle"
    ctx.register_tool(name="dream_cycle_propose", toolset=toolset, schema=schemas.DREAM_CYCLE_PROPOSE,
                      handler=_hermes_handler(tool_dream_cycle_propose), emoji="🌙",
                      description=schemas.DREAM_CYCLE_PROPOSE["description"])
    ctx.register_tool(name="dream_cycle_status", toolset=toolset, schema=schemas.DREAM_CYCLE_STATUS,
                      handler=_hermes_handler(tool_dream_cycle_status), emoji="🌙",
                      description=schemas.DREAM_CYCLE_STATUS["description"])
    ctx.register_tool(name="dream_cycle_review_status", toolset=toolset, schema=schemas.DREAM_CYCLE_REVIEW_STATUS,
                      handler=_hermes_handler(tool_dream_cycle_review_status), emoji="🌙",
                      description=schemas.DREAM_CYCLE_REVIEW_STATUS["description"])
    ctx.register_tool(name="dream_cycle_snooze", toolset=toolset, schema=schemas.DREAM_CYCLE_SNOOZE,
                      handler=_hermes_handler(tool_dream_cycle_snooze), emoji="🌙",
                      description=schemas.DREAM_CYCLE_SNOOZE["description"])
    for name, sch, fn in (("dream_cycle_apply", schemas.DREAM_CYCLE_APPLY, tool_dream_cycle_apply),
                          ("dream_cycle_rollback", schemas.DREAM_CYCLE_ROLLBACK, tool_dream_cycle_rollback),
                          ("dream_cycle_receipts", schemas.DREAM_CYCLE_RECEIPTS, tool_dream_cycle_receipts)):
        ctx.register_tool(name=name, toolset=toolset, schema=sch, handler=_hermes_handler(fn), emoji="🌙",
                          description=sch["description"])
    ctx.register_command("dream-cycle", handler=handle_slash,
                         description="Oneiros Dream Cycle: propose | status | review-status | snooze | receipts | rollback",
                         args_hint="[propose [YYYY-MM-DD]|status|review-status|snooze|receipts|rollback [ID]]")
    try:
        ctx.register_cli_command(name="dream-cycle", help="Oneiros Dream Cycle (proposal-only, no model)",
                                 setup_fn=_setup_cli, handler_fn=_cli_handler,
                                 description="Stage nightly memory consolidation proposals.")
    except Exception:  # pragma: no cover - older Hermes without CLI plugins
        pass
    # Warm status/Waking Check caches before the first turn when the host
    # runs this hook. Messaging platforms do not fire it. An unknown hook
    # name only logs a warning in Hermes, so register unconditionally.
    try:
        ctx.register_hook("on_session_open", _make_on_session_open())
    except Exception:
        pass
    # Next private turn (CLI, Desktop, dashboard, API, or an explicit DM).
    # Not a background push. Cron origin delivery does not reach api_server
    # / Desktop chats. Unknown hook names log a warning and do not abort.
    try:
        ctx.register_hook("pre_llm_call", _make_pre_llm_call())
    except Exception:
        pass
    _register_review_prompt(ctx)
    skill = _PLUGIN_DIR / "skills" / "dream-cycle" / "SKILL.md"
    if skill.is_file():
        try:
            ctx.register_skill("dream-cycle", skill, description="Oneiros Dream Cycle morning review")
        except Exception:  # pragma: no cover
            pass
