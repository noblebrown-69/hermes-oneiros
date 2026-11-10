"""Dream Cycle status with freshness (autopilot-heartbeat idea, 2026-11-08).

Presence is not health: status grades how *fresh* the last run is, warns loudly
when it scanned zero memory files, counts undecided proposals against the
review ledger now, and shows the last apply/rollback receipt.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import default_config
from .paths import data_root, is_disabled
from .review import read_reviews, undecided
from .nosave import format_nosave_status

FRESH_HOURS = 26  # nightly job + slack


def _age(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts)
        now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
        return round((now - t).total_seconds() / 3600, 2)
    except ValueError:
        return None


def _human(hours: Optional[float]) -> str:
    if hours is None:
        return "unknown"
    if hours < 1:
        return f"{int(hours * 60)} min ago"
    if hours < 48:
        return f"{hours:.1f} h ago"
    return f"{hours / 24:.1f} days ago"


def _last_receipt(root: Path) -> Optional[Dict[str, Any]]:
    d = root / "receipts"
    best = None
    for p in d.glob("*.json") if d.exists() else []:
        try:
            r = json.loads(p.read_text())
        except ValueError:
            continue
        action, when = ("rolled-back", r["rolled_back_at"]) if r.get("rolled_back_at") else ("applied", r.get("applied_at"))
        cand = {"receipt_id": r.get("receipt_id"), "action": action, "time": when, "proposal_id": r.get("proposal_id"),
                "evidence_status": r.get("evidence_status", "n/a"), "file": r.get("file")}
        if best is None or str(when) > str(best["time"]):
            best = cand
    return best



def _plugin_version(home: Path) -> str:
    """Version from the profile's installed plugin.yaml (falls back to package)."""
    for cand in (home / "plugins" / "dream-cycle" / "plugin.yaml",
                 Path(__file__).resolve().parents[1] / "plugin.yaml"):
        if not cand.is_file():
            continue
        try:
            for line in cand.read_text(encoding="utf-8").splitlines():
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return "unknown"

def status(hermes_home: Path, config=None) -> Dict[str, Any]:
    cfg = config or default_config()
    root = data_root(hermes_home)
    stage = root / cfg.get("stage_subdir", "dream-proposals")
    latest, last, warnings = None, {}, []
    if stage.exists():
        days = sorted([p for p in stage.iterdir() if p.is_dir()], reverse=True)
        latest = str(days[0]) if days else None
        if days and (days[0] / "summary.json").exists():
            try:
                s = json.loads((days[0] / "summary.json").read_text())
                props = s.get("proposals", [])
                rows = read_reviews(days[0])
                last = {"count": len(props), "session_status": s.get("session_status"),
                        "generated_at": s.get("generated_at"), "scanned_files": s.get("scanned_files"),
                        "undecided": len(undecided(days[0], props)), "ledger_rows": len(rows),
                        "stale_format": any("file_sha256" not in p for p in props),
                        "nosave": s.get("nosave")}
            except ValueError:
                last = {"error": "latest summary.json is unreadable"}
    age = _age(last.get("generated_at"))
    if latest is None:
        freshness = "never-run"; warnings.append("Dream Cycle has never run on this profile")
    elif age is None:
        freshness = "unknown"
    else:
        freshness = "fresh" if age <= FRESH_HOURS else "stale"
        if freshness == "stale":
            warnings.append(f"last run {_human(age)} — nightly may not be running")
    if last.get("scanned_files") == 0:
        warnings.append("⚠ LAST RUN SCANNED 0 MEMORY FILES — proposals are meaningless; check memory_globs/roots")
    if last.get("stale_format"):
        warnings.append("latest stage predates freshness fields; apply will refuse it as stale — re-run propose")
    disabled = is_disabled(root, cfg.get("disabled_filename", "DISABLED"))
    if disabled:
        warnings.append("DISABLED kill switch is ON")
    out = {
        "disabled": disabled,
        "data_root": str(root),
        "latest_stage": latest,
        "latest": last,
        "last_run_at": last.get("generated_at"),
        "last_run_age_hours": age,
        "freshness": freshness,
        "last_receipt": _last_receipt(root),
        "apply_enabled": bool(cfg.get("apply_enabled")),
        "require_grounded_evidence": bool(cfg.get("require_grounded_evidence")),
        "session_corroboration": bool((cfg.get("session_corroboration") or {}).get("enabled")),
        "nosave": last.get("nosave"),
        "warnings": warnings,
        "model_used": False,
        "version": _plugin_version(hermes_home),
        "suite": "Oneiros Dream Cycle",
    }
    out["text"] = format_status(out)
    return out


def format_status(s: Dict[str, Any]) -> str:
    L = s.get("latest") or {}
    rc = s.get("last_receipt")
    lines = [
        "🌙 Oneiros Dream Cycle status  v" + str(s.get("version") or "unknown"),
        f"  apply: {'ON' if s['apply_enabled'] else 'off'}   kill switch: {'ON (DISABLED)' if s['disabled'] else 'off'}"
        f"   require grounded evidence: {'yes' if s.get('require_grounded_evidence') else 'no'}",
        f"  last run: {s.get('last_run_at') or 'never'} ({_human(s.get('last_run_age_hours'))}) — {s['freshness']}",
        f"  files scanned: {L.get('scanned_files', 'n/a')}   proposals: {L.get('count', 0)}"
        f"   undecided: {L.get('undecided', 0)}   sessions: {L.get('session_status') or 'n/a'}",
        "  " + format_nosave_status(L.get("nosave") or s.get("nosave")),
        "  last receipt: " + (f"{rc['receipt_id']} — {rc['action']} at {rc['time']} (evidence {rc['evidence_status']})"
                              if rc else "none"),
    ]
    for w in s.get("warnings") or []:
        lines.append(f"  WARN: {w}")
    return "\n".join(lines)
