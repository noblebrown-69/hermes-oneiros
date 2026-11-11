"""Oneiros Dream Cycle dashboard backend — mounted at /api/plugins/dream-cycle/.

Wraps the dream_cycle library only (no shelling out). The dashboard host scopes
``?profile=`` via ``HERMES_HOME`` and gates every request behind dashboard auth.
Writes go through review.jsonl / apply / rollback with the same guards as the CLI.
"""
from __future__ import annotations

import getpass
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from dream_cycle.apply import apply_accepted, apply_proposal, list_receipts, rollback  # noqa: E402
from dream_cycle.config import load_config  # noqa: E402
from dream_cycle.paths import data_root, is_disabled  # noqa: E402
from dream_cycle.remind import review_snapshot, snooze as snooze_reminders  # noqa: E402
from dream_cycle.review import (  # noqa: E402
    append_review,
    accept_pending,
    carry_prior_decisions,
    latest_decisions,
    latest_decisions_across,
    proposal_id as make_proposal_id,
    read_reviews,
    revert_batch,
    undecided,
)
from dream_cycle.status import status as dc_status  # noqa: E402

_SAFE_USER = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_SAFE_PROFILE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SAFE_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_PID = re.compile(r"^[a-f0-9]{8,16}$")
_SAFE_RECEIPT = re.compile(r"^[A-Za-z0-9._:-]{8,96}$")
_SAFE_BATCH = re.compile(r"^b[a-f0-9]{12}$")


def _home(profile: Optional[str] = None) -> Path:
    """Active profile. A deep-link ``profile`` query selects that profile directory."""
    name = (profile or "").strip()
    if not name:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    if not _SAFE_PROFILE.match(name):
        raise HTTPException(status_code=400, detail="invalid profile")
    root = _profiles_root().resolve()
    home = (root / name).resolve()
    if home.parent != root or not home.is_dir():
        raise HTTPException(status_code=404, detail=f"unknown profile {name}")
    return home


def _profiles_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root()) / "profiles"


def _valid_profile_names() -> set:
    root = _profiles_root()
    if not root.is_dir():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}


def _profile_name(home: Path) -> str:
    if home.parent.name == "profiles":
        return home.name
    return "default"


def _installed(home: Path) -> bool:
    return (home / "plugin-data" / "dream-cycle").is_dir() or (
        home / "plugins" / "dream-cycle" / "plugin.yaml"
    ).is_file()


def _cfg(home: Path) -> Dict[str, Any]:
    return load_config(home / "plugin-data" / "dream-cycle" / "config.yaml")


def _actor(user: Optional[str] = None) -> str:
    raw = (user or "").strip() or getpass.getuser()
    if not _SAFE_USER.match(raw):
        raise HTTPException(status_code=400, detail="actor user must be 1-32 chars [A-Za-z0-9._-]")
    return f"dashboard:{raw}"


def _version() -> str:
    try:
        from dream_cycle.yaml_load import safe_load
        data = safe_load((_PLUGIN_ROOT / "plugin.yaml").read_text()) or {}
        return str(data.get("version") or "0.5.0")
    except Exception:
        return "0.5.0"


def _latest_stage(home: Path, cfg: Dict[str, Any]) -> Optional[Path]:
    base = data_root(home) / cfg.get("stage_subdir", "dream-proposals")
    if not base.is_dir():
        return None
    days = sorted([p for p in base.iterdir() if p.is_dir()], reverse=True)
    return days[0] if days else None


def _summary(stage: Path) -> Dict[str, Any]:
    p = stage / "summary.json"
    if not p.is_file():
        return {"proposals": [], "undecided": [], "carried_decisions": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {"proposals": [], "error": "unreadable summary.json"}


def _hygiene_markdown(text: str) -> Optional[tuple]:
    """Cron output that looks like a hygiene run. None when it is some other job."""
    if "Memory Hygiene" not in text and "memory-hygiene" not in text and "hit_count" not in text:
        return None
    hit_count, scanned = None, []
    brace = text.find("{")
    if brace >= 0:
        try:
            blob = json.loads(text[brace:text.rfind("}") + 1])
        except ValueError:
            blob = None
        if isinstance(blob, dict):
            hit_count = blob.get("hit_count")
            scanned = blob.get("scanned") or []
            if not isinstance(scanned, list):
                scanned = []
    return hit_count, scanned


def _hygiene_json(text: str) -> tuple:
    try:
        blob = json.loads(text)
    except ValueError:
        return None, []
    if not isinstance(blob, dict):
        return None, []
    hit = blob.get("hit_count", blob.get("hits", blob.get("count")))
    scanned = blob.get("scanned") or []
    if not isinstance(scanned, list):
        scanned = []
    return hit, scanned


def _hygiene_report_files(pdata: Path) -> List[Path]:
    """Output files written by memory-hygiene. Never its source tree."""
    if not pdata.is_dir():
        return []
    found: List[Path] = []
    for name in ("last-report.json", "waking-check.json", "status.json"):
        cand = pdata / name
        if cand.is_file():
            found.append(cand)
    named = {p.resolve() for p in found}
    reports = [p for p in pdata.glob("**/report*.json") if p.is_file() and p.resolve() not in named]
    if reports:
        reports.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        found.append(reports[0])
    return found


def _hygiene_card(home: Path) -> Dict[str, Any]:
    """Read memory-hygiene output files only.

    The other plugin's modules stay unloaded, including when that plugin is
    disabled. Putting its directory on ``sys.path`` imported ``hygiene.*``
    into this process. Same idea as the cron-output read: open the files it
    already wrote.
    """
    out: Dict[str, Any] = {"installed": False, "hit_count": None, "scanned": [], "last_run": None, "source": None}
    plug = home / "plugins" / "memory-hygiene"
    pdata = home / "plugin-data" / "memory-hygiene"
    out["installed"] = plug.is_dir() or pdata.is_dir()
    if not out["installed"]:
        return out
    best = None  # (mtime, path, hit_count, scanned)
    cron_out = home / "cron" / "output"
    if cron_out.is_dir():
        for job_dir in cron_out.iterdir():
            if not job_dir.is_dir():
                continue
            for f in sorted(job_dir.glob("*.md"), reverse=True)[:3]:
                try:
                    text = f.read_text(errors="ignore")
                    parsed = _hygiene_markdown(text)
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                if parsed is None:
                    continue
                if best is None or mtime > best[0]:
                    best = (mtime, f, parsed[0], parsed[1])
    for report in _hygiene_report_files(pdata):
        try:
            text = report.read_text(errors="ignore")
            mtime = report.stat().st_mtime
        except OSError:
            continue
        hit, scanned = _hygiene_json(text)
        if best is None or mtime > best[0]:
            best = (mtime, report, hit, scanned)
    if best:
        mtime, src, hit, scanned = best
        out["last_run"] = datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
        out["source"] = str(src)
        out["hit_count"] = hit
        out["scanned"] = scanned
    return out


def _waking_card(home: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {"installed": False, "recaps": [], "last_recap": None}
    plug = home / "plugins" / "waking-self"
    recap_dir = home / "plugin-data" / "waking-self" / "recaps"
    out["installed"] = plug.is_dir() or recap_dir.is_dir()
    if not recap_dir.is_dir():
        return out
    files = sorted(recap_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    for f in files:
        st = f.stat()
        entry = {
            "name": f.name,
            "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
            "size": st.st_size,
        }
        try:
            # first non-empty content line after title (metadata only)
            lines = [ln.strip() for ln in f.read_text(errors="ignore").splitlines() if ln.strip()]
            entry["preview"] = lines[1] if len(lines) > 1 else (lines[0] if lines else "")
        except OSError:
            entry["preview"] = ""
        out["recaps"].append(entry)
    if out["recaps"]:
        out["last_recap"] = out["recaps"][0]["mtime"]
    return out


def _profile_allowed(pname: str, cfg: Optional[Dict[str, Any]]) -> bool:
    """Empty or missing allowlist: every installed profile is in scope."""
    if not cfg:
        return True
    names = cfg.get("profile_allowlist") or []
    if not names:
        return True
    return pname in {str(n) for n in names}


def _gate_flags(home: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    root = data_root(home)
    disabled = is_disabled(root, cfg.get("disabled_filename", "DISABLED"))
    apply_on = bool(cfg.get("apply_enabled"))
    reasons = []
    if disabled:
        reasons.append("DISABLED kill switch is present under plugin-data/dream-cycle/")
    if not apply_on:
        reasons.append("apply_enabled is false in plugin-data/dream-cycle/config.yaml")
    return {
        "apply_enabled": apply_on,
        "kill_switch": disabled,
        "can_apply": apply_on and not disabled,
        "can_review": not disabled,  # review ledger is always appendable unless kill switch
        "block_reasons": reasons,
    }


@router.get("/overview")
def overview(profile: Optional[str] = None) -> Dict[str, Any]:
    home = _home(profile)
    pname = _profile_name(home)
    valid = _valid_profile_names()
    if home.parent.name == "profiles" and pname not in valid:
        raise HTTPException(status_code=400, detail=f"unknown profile {pname!r}")
    installed = _installed(home)
    cfg = _cfg(home) if installed else None
    allowed = _profile_allowed(pname, cfg)
    base = {
        "ok": True,
        "profile": pname,
        "installed": installed,
        "oneiros_target_profile": allowed,
        "version": _version(),
        "suite": "Oneiros Dream Cycle",
        "tagline": "Your agent sleeps on it.",
    }
    if not installed:
        base["message"] = (
            f"Oneiros is not installed on profile '{pname}'. "
            "Install dream-cycle on this profile."
        )
        return base
    if not allowed:
        base["message"] = (
            f"Profile '{pname}' is not in profile_allowlist. "
            "Add it in plugin-data/dream-cycle/config.yaml, or leave the list empty."
        )
    cfg = cfg or _cfg(home)
    st = dc_status(home, cfg)
    gates = _gate_flags(home, cfg)
    stage = _latest_stage(home, cfg)
    summ = _summary(stage) if stage else {}
    props = summ.get("proposals") or []
    und = [p.get("proposal_id") for p in undecided(stage, props)] if stage else []
    base.update({
        "status": st,
        "status_text": st.get("text"),
        "gates": gates,
        "latest_day": stage.name if stage else None,
        "proposal_count": len(props),
        "undecided": und,
        "carried": summ.get("carried_decisions") or [],
        "hygiene": _hygiene_card(home),
        "waking": _waking_card(home),
    })
    return base


@router.get("/proposals")
def proposals(day: Optional[str] = None, profile: Optional[str] = None) -> Dict[str, Any]:
    home = _home(profile)
    if not _installed(home):
        return {"ok": True, "installed": False, "proposals": []}
    if day and not _SAFE_DAY.match(day):
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    cfg = _cfg(home)
    stage = (data_root(home) / cfg.get("stage_subdir", "dream-proposals") / day) if day else _latest_stage(home, cfg)
    if stage is None or not stage.is_dir():
        return {"ok": True, "installed": True, "day": day, "proposals": [], "reviews": []}
    summ = _summary(stage)
    props = summ.get("proposals") or []
    # ensure carry is reflected for display even if nightly hasn't rewritten summary
    try:
        carry_prior_decisions(stage, props)
    except Exception:
        pass
    across = latest_decisions_across(stage.parent)
    today = latest_decisions(stage)
    rows = []
    for p in props:
        pid = p.get("proposal_id") or make_proposal_id(p)
        last = today.get(pid) or across.get(pid) or {}
        rows.append({
            **p,
            "proposal_id": pid,
            "last_decision": last.get("decision"),
            "last_actor": last.get("actor"),
            "last_ts": last.get("ts"),
            "carried_from": last.get("carried_from") or (last.get("stage_day") if last.get("stage_day") != stage.name else None),
            "keep": last.get("keep"),
            "evidence": last.get("evidence"),
            "note": last.get("note") if last.get("note") is not None else p.get("note"),
            # defer counts as decided for the dashboard (buttons hide until Change decision)
            "is_undecided": last.get("decision") not in ("accept", "reject", "defer", "applied", "rolled-back"),
        })
    return {
        "ok": True,
        "installed": True,
        "day": stage.name,
        "gates": _gate_flags(home, cfg),
        "proposals": rows,
        "reviews": read_reviews(stage),
        "undecided": [r["proposal_id"] for r in rows if r["is_undecided"]],
    }


class ReviewBody(BaseModel):
    proposal_id: str
    decision: str
    evidence: str = ""
    note: str = ""
    keep: str | None = None
    retire: list[str] | None = None
    day: str | None = None
    actor_user: str | None = None
    profile: str | None = None


@router.post("/review")
def review(body: ReviewBody) -> Dict[str, Any]:
    home = _home(body.profile)
    if not _installed(home):
        raise HTTPException(status_code=404, detail="Oneiros not installed on this profile")
    cfg = _cfg(home)
    gates = _gate_flags(home, cfg)
    if gates["kill_switch"]:
        raise HTTPException(status_code=403, detail="DISABLED kill switch is on — review blocked")
    if body.decision not in ("accept", "reject", "defer"):
        raise HTTPException(status_code=400, detail="decision must be accept|reject|defer")
    if not _SAFE_PID.match(body.proposal_id):
        raise HTTPException(status_code=400, detail="invalid proposal_id")
    if body.day and not _SAFE_DAY.match(body.day):
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    evidence = " ".join((body.evidence or "").split())
    # Evidence is optional on accept (v0.8.1) and on apply (v0.8.2).
    if body.decision == "accept" and not body.keep:
        raise HTTPException(status_code=400, detail="keep is required for accept")
    stage = (data_root(home) / cfg.get("stage_subdir", "dream-proposals") / body.day) if body.day else _latest_stage(home, cfg)
    if stage is None:
        raise HTTPException(status_code=404, detail="no proposal stage yet — run propose first")
    summ = _summary(stage)
    prop = next((p for p in summ.get("proposals", []) if p.get("proposal_id") == body.proposal_id), None)
    if prop is None:
        raise HTTPException(status_code=404, detail=f"proposal {body.proposal_id} not in {stage.name}")
    if body.keep is not None and str(body.keep) not in [str(v) for v in prop.get("values") or []]:
        raise HTTPException(status_code=400, detail=f"keep must be one of {prop.get('values')}")
    actor = _actor(body.actor_user)
    row = append_review(
        stage, body.proposal_id, body.decision, actor,
        evidence=evidence, note=body.note or "",
        keep=str(body.keep) if body.keep is not None else None,
        retire=body.retire,
    )
    return {"ok": True, "row": row, "day": stage.name, "actor": actor}



class ReviewBatchBody(BaseModel):
    proposal_ids: list[str] | None = None  # if omitted, every pending on the day
    evidence: str = ""
    note: str = ""
    day: str | None = None
    actor_user: str | None = None
    profile: str | None = None


@router.post("/review/accept-all")
def review_accept_all(body: ReviewBatchBody) -> Dict[str, Any]:
    """Accept every pending proposal shown (optional id filter). review.jsonl only."""
    home = _home(body.profile)
    if not _installed(home):
        raise HTTPException(status_code=404, detail="Oneiros not installed on this profile")
    cfg = _cfg(home)
    gates = _gate_flags(home, cfg)
    if gates["kill_switch"]:
        raise HTTPException(status_code=403, detail="DISABLED kill switch is on — review blocked")
    if body.day and not _SAFE_DAY.match(body.day):
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    if body.proposal_ids:
        for pid in body.proposal_ids:
            if not _SAFE_PID.match(pid):
                raise HTTPException(status_code=400, detail="invalid proposal_id")
    stage = (data_root(home) / cfg.get("stage_subdir", "dream-proposals") / body.day) if body.day else _latest_stage(home, cfg)
    if stage is None:
        raise HTTPException(status_code=404, detail="no proposal stage yet — run propose first")
    summ = _summary(stage)
    props = summ.get("proposals") or []
    try:
        carry_prior_decisions(stage, props)
    except Exception:
        pass
    actor = _actor(body.actor_user)
    evidence = " ".join((body.evidence or "").split())
    out = accept_pending(
        stage, props, actor,
        evidence=evidence,
        note=body.note or "accept-all",
        proposal_ids=body.proposal_ids,
    )
    return {"ok": True, "day": stage.name, "actor": actor, **out}


class UndoBatchBody(BaseModel):
    batch_id: str
    day: str | None = None
    actor_user: str | None = None
    note: str = ""
    profile: str | None = None


@router.post("/review/undo-batch")
def review_undo_batch(body: UndoBatchBody) -> Dict[str, Any]:
    """Append reverted rows for an Accept-all batch. Never deletes ledger lines."""
    home = _home(body.profile)
    if not _installed(home):
        raise HTTPException(status_code=404, detail="Oneiros not installed on this profile")
    cfg = _cfg(home)
    gates = _gate_flags(home, cfg)
    if gates["kill_switch"]:
        raise HTTPException(status_code=403, detail="DISABLED kill switch is on — review blocked")
    if not _SAFE_BATCH.match(body.batch_id or ""):
        raise HTTPException(status_code=400, detail="invalid batch_id")
    if body.day and not _SAFE_DAY.match(body.day):
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    stage = (data_root(home) / cfg.get("stage_subdir", "dream-proposals") / body.day) if body.day else _latest_stage(home, cfg)
    if stage is None:
        raise HTTPException(status_code=404, detail="no proposal stage yet")
    actor = _actor(body.actor_user)
    out = revert_batch(stage, body.batch_id, actor, note=body.note)
    if out.get("unknown"):
        raise HTTPException(status_code=404, detail=f"batch {body.batch_id} not in {stage.name}")
    return {"ok": True, "day": stage.name, "actor": actor, **out}


class ApplyBody(BaseModel):

    proposal_id: str | None = None
    keep: str | None = None
    evidence: str | None = None
    retire: list[str] | None = None
    day: str | None = None
    actor_user: str | None = None
    dry_run: bool = False
    accepted: bool = False  # apply all today's accepts
    profile: str | None = None


@router.post("/apply")
def apply_route(body: ApplyBody) -> Dict[str, Any]:
    home = _home(body.profile)
    if not _installed(home):
        raise HTTPException(status_code=404, detail="Oneiros not installed on this profile")
    cfg = _cfg(home)
    gates = _gate_flags(home, cfg)
    if not body.dry_run and not gates["can_apply"]:
        raise HTTPException(status_code=403, detail="; ".join(gates["block_reasons"]) or "apply blocked")
    actor = _actor(body.actor_user)
    if body.accepted:
        if body.dry_run:
            # dry-run each today's accept
            stage = _latest_stage(home, cfg)
            if stage is None:
                return {"ok": True, "results": [], "message": "no stage"}
            results = []
            for pid, row in latest_decisions(stage).items():
                if row.get("carried_from"):
                    continue
                if row.get("decision") == "accept" and row.get("keep"):
                    # Pass the stored evidence through, even when it is blank.
                    # Do not invent a dry-run source.
                    r = apply_proposal(
                        home, pid, row["keep"], row.get("evidence") or "",
                        f"{row.get('actor')} via {actor}", config=cfg, day=stage.name,
                        dry_run=True, retire=row.get("retire"),
                    )
                    results.append({"proposal_id": pid, **r.to_dict()})
            return {"ok": True, "dry_run": True, "results": results, "actor": actor}
        out = apply_accepted(home, config=cfg, day=body.day, actor=actor)
        return {"ok": True, "dry_run": False, "results": out, "actor": actor}
    # single proposal
    if not body.proposal_id or not _SAFE_PID.match(body.proposal_id):
        raise HTTPException(status_code=400, detail="proposal_id required")
    if not body.keep:
        raise HTTPException(status_code=400, detail="keep required")
    evidence = " ".join((body.evidence or "").split())
    if body.day and not _SAFE_DAY.match(body.day):
        raise HTTPException(status_code=400, detail="invalid day")
    r = apply_proposal(
        home, body.proposal_id, body.keep, evidence, actor,
        config=cfg, day=body.day, dry_run=body.dry_run, retire=body.retire,
    )
    return {"ok": r.ok, "result": r.to_dict(), "actor": actor}


@router.get("/receipts")
def receipts(profile: Optional[str] = None) -> Dict[str, Any]:
    home = _home(profile)
    if not _installed(home):
        return {"ok": True, "installed": False, "receipts": []}
    cfg = _cfg(home)
    recs = list_receipts(home)
    # newest first
    recs = sorted(recs, key=lambda r: r.get("applied_at") or "", reverse=True)
    return {"ok": True, "installed": True, "gates": _gate_flags(home, cfg), "receipts": recs}


class RollbackBody(BaseModel):
    receipt_id: str
    force: bool = False
    actor_user: str | None = None
    dry_run: bool = False
    profile: str | None = None


@router.post("/rollback")
def rollback_route(body: RollbackBody) -> Dict[str, Any]:
    home = _home(body.profile)
    if not _installed(home):
        raise HTTPException(status_code=404, detail="Oneiros not installed on this profile")
    if not _SAFE_RECEIPT.match(body.receipt_id):
        raise HTTPException(status_code=400, detail="invalid receipt_id")
    cfg = _cfg(home)
    gates = _gate_flags(home, cfg)
    if gates["kill_switch"]:
        raise HTTPException(status_code=403, detail="DISABLED kill switch is on — rollback blocked")
    actor = _actor(body.actor_user)
    # dry-run: show snapshot vs current
    root = data_root(home)
    rec_path = root / "receipts" / f"{body.receipt_id}.json"
    if not rec_path.is_file():
        raise HTTPException(status_code=404, detail="receipt not found")
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    if rec.get("rolled_back_at"):
        raise HTTPException(status_code=400, detail="receipt already rolled back")
    target = Path(rec["file"])
    snap = Path(rec["snapshot"])
    if not snap.is_file():
        raise HTTPException(status_code=404, detail="snapshot missing")
    current = target.read_text(encoding="utf-8") if target.is_file() else ""
    snap_text = snap.read_text(encoding="utf-8")
    diff = {
        "file": str(target),
        "receipt_id": body.receipt_id,
        "sha_current": __import__("hashlib").sha256(current.encode()).hexdigest(),
        "sha_snapshot": __import__("hashlib").sha256(snap_text.encode()).hexdigest(),
        "current_preview": current[:2000],
        "snapshot_preview": snap_text[:2000],
        "would_restore_bytes": len(snap_text),
        "file_changed_since_apply": current != "" and __import__("hashlib").sha256(current.encode()).hexdigest() != rec.get("sha_after"),
    }
    if body.dry_run:
        return {"ok": True, "dry_run": True, "diff": diff, "actor": actor}
    r = rollback(home, receipt_id=body.receipt_id, force=body.force, actor=actor, config=cfg)
    return {"ok": r.ok, "result": r.to_dict(), "diff": diff, "actor": actor}


@router.get("/reminder")
def reminder(profile: Optional[str] = None) -> Dict[str, Any]:
    """Live review counts. Does not send a chat message and does not snooze."""
    from datetime import datetime
    home = _home(profile)
    if not _installed(home):
        return {"ok": True, "installed": False, "pending_review": 0, "accepted_awaiting_apply": 0}
    snap = review_snapshot(home, _cfg(home), datetime.now().astimezone())
    return {"ok": True, "installed": True, **snap}


class SnoozeBody(BaseModel):
    profile: str | None = None


@router.post("/reminder/snooze")
def reminder_snooze(body: SnoozeBody | None = None, profile: Optional[str] = None) -> Dict[str, Any]:
    """Snooze proactive reminders for this profile. Not a GET; opening a link must not snooze."""
    from datetime import datetime
    chosen = profile or (body.profile if body else None)
    home = _home(chosen)
    if not _installed(home):
        raise HTTPException(status_code=404, detail="Oneiros not installed on this profile")
    snap = snooze_reminders(home, _cfg(home), datetime.now().astimezone())
    return {"ok": True, **snap}


@router.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "plugin": "dream-cycle", "label": "Oneiros", "version": _version()}
