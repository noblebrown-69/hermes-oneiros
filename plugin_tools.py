from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from dream_cycle.config import load_config
from dream_cycle.errors import DreamCycleError
from dream_cycle.propose import propose
from dream_cycle.status import status as dc_status
from dream_cycle import apply as dc_apply


def _home() -> Path:
    """Active profile home. In the one-process gateway HERMES_HOME is a context-local
    override (hermes_constants), not os.environ — 2026-11-08 fix: chat tools read ~/.hermes."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _cfg():
    return load_config(_home() / "plugin-data" / "dream-cycle" / "config.yaml")


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg, "model_used": False})


def tool_dream_cycle_propose(date: str | None = None, date_str: str | None = None, **_):
    raw = date or date_str
    try:
        from datetime import date as _d
        d = _d.fromisoformat(raw) if raw else None
    except ValueError:
        return _err(f"date must be YYYY-MM-DD, got {raw!r}")
    try:
        res = propose(_home(), config=_cfg(), run_date=d)
    except DreamCycleError as e:
        return _err(str(e))
    return json.dumps({
        "ok": True, "skipped_reason": res.skipped_reason, "stage_path": res.stage_path,
        "count": len(res.proposals), "proposals": [p.__dict__ for p in res.proposals],
        "session_status": res.session_status, "session_detail": res.session_detail,
        "model_used": False,
    })


def tool_dream_cycle_review_status(**_):
    from datetime import datetime
    from dream_cycle.remind import review_snapshot
    try:
        snap = review_snapshot(_home(), _cfg(), datetime.now().astimezone())
    except Exception as e:
        return _err(str(e))
    return json.dumps({"ok": True, **snap})


def tool_dream_cycle_snooze(**_):
    from datetime import datetime
    from dream_cycle.remind import snooze
    try:
        snap = snooze(_home(), _cfg(), datetime.now().astimezone())
    except Exception as e:
        return _err(str(e))
    return json.dumps({"ok": True, **snap, "snoozed": True})


def tool_dream_cycle_status(**_):
    try:
        return json.dumps({"ok": True, **dc_status(_home(), _cfg())})
    except DreamCycleError as e:
        return _err(str(e))


def tool_dream_cycle_apply(proposal_id: str | None = None, keep: str | None = None, evidence: str | None = None,
                           actor: str | None = None, date: str | None = None, dry_run: bool = False,
                           accepted: bool = False, retire=None, all_lines: bool = False, **_):
    """Apply one reviewed proposal (or all ledger-accepted ones). Gated by apply_enabled."""
    try:
        if accepted:
            return json.dumps({"ok": True, "results": dc_apply.apply_accepted(_home(), config=_cfg(), day=date),
                               "model_used": False})
        if not proposal_id or not keep:
            return _err("proposal_id and keep are required (or accepted=true)")
        r = dc_apply.apply_proposal(_home(), proposal_id, keep, evidence or "", actor or "",
                                    config=_cfg(), day=date, dry_run=bool(dry_run),
                                    retire=[retire] if isinstance(retire, str) else retire,
                                    all_lines=bool(all_lines))
    except DreamCycleError as e:
        return _err(str(e))
    return json.dumps({**r.to_dict(), "model_used": False})


def tool_dream_cycle_rollback(receipt_id: str | None = None, last: bool = False, force: bool = False,
                              actor: str | None = None, **_):
    try:
        r = dc_apply.rollback(_home(), receipt_id, last=bool(last), force=bool(force),
                              actor=actor or "rollback", config=_cfg())
    except DreamCycleError as e:
        return _err(str(e))
    return json.dumps({**r.to_dict(), "model_used": False})


def tool_dream_cycle_receipts(**_):
    rs = dc_apply.list_receipts(_home())
    return json.dumps({"ok": True, "receipts": [{k: r.get(k) for k in ("receipt_id", "proposal_id", "keep", "retired",
                       "file", "sha_before", "sha_after", "applied_at", "rolled_back_at", "rollback")} for r in rs]})
