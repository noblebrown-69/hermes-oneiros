"""Gated Dream Cycle apply (2026-11-08): turn one reviewed proposal into a memory edit.

Every apply, in order (any failure restores the snapshot and logs nothing as applied):

1. Gate: ``apply_enabled: true``, no DISABLED kill switch, dream-cycle lock held,
   proposal found in the stage's summary.json, not already applied, a named
   actor, ``keep`` is one of the proposal's values. Evidence may be empty when
   today's latest review row is an explicit ``accept`` for this proposal (and
   ``keep`` matches that accept). A non-empty evidence string still permits a
   direct apply. Empty evidence is stored as empty — never replaced with a
   placeholder source. Non-empty evidence is length-capped.
2. Secret scan: evidence, actor, keep and every line that would be written are
   scanned; any hit refuses the apply (nothing written).
3. Pre-apply snapshot: the target file is copied to
   ``plugin-data/dream-cycle/snapshots/<receipt_id>/`` and its sha256 verified.
4. Edit: on live lines (not SUPERSEDED / MECH / STATUS) only the retired value
   token is replaced by ``keep``. Nothing is deleted: a column-0
   ``SUPERSEDED <date>: <old> → <keep>, because <evidence> [dream-cycle <id>]``
   marker is inserted above the first changed line. When evidence is empty the
   ``because`` clause is omitted (no invented source). Full before/after line
   text goes into the receipt.
5. Size guard: refuses if the file would grow past the Hermes memory char limit.
6. Verify: re-read; sha matches the planned text; no retired value remains live;
   ``keep`` present. Otherwise restore the snapshot.
7. Receipt: ``receipts/<receipt_id>.json`` plus an append-only ``applied`` row in
   the stage's ``review.jsonl`` (proposal, evidence, speaker, before/after sha).

``rollback(home, receipt_id | last=True)`` restores the snapshot only if the file
still matches ``sha_after`` (else refuses unless ``force``), verifies sha_before,
and logs a ``rolled-back`` row.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import default_config, validate_config
from .errors import DreamCycleError
from .markers import is_superseded_line
from .paths import LockBusy, acquire_lock, data_root, is_disabled, memory_file_lock, release_lock
from .propose import line_value_spans, _normalize
from .sessions import ground_value
from .review import append_event, latest_decisions, read_reviews, latest_decisions_across

# Test seam: called with the target path after the CAS read is planned, just before the
# final re-check + atomic write (lets tests simulate a writer that ignores the lock).
_pre_write_hook = None

SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                    r"gho_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[bpas]-[A-Za-z0-9-]{10,}|"
                    r"-----BEGIN [A-Z ]*PRIVATE KEY|(?i:(?:api[_-]?key|password|passwd|secret|token)\s*[:=]\s*\S{8,}))")


class ApplyRefused(DreamCycleError):
    pass


@dataclass
class ApplyResult:
    ok: bool
    status: str  # applied | refused | rolled-back | dry-run
    message: str
    receipt_id: Optional[str] = None
    file: Optional[str] = None
    sha_before: Optional[str] = None
    sha_after: Optional[str] = None
    snapshot: Optional[str] = None
    changes: List[Dict[str, Any]] = field(default_factory=list)
    markers: List[str] = field(default_factory=list)
    evidence_status: Optional[str] = None
    grounding: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def secret_hit(*texts: str) -> Optional[str]:
    for t in texts:
        m = SECRET.search(t or "")
        if m:
            return m.group(0)[:6] + "…"  # never echo the whole thing
    return None


def _stage_dir(home: Path, cfg: Dict[str, Any], day: Optional[str]) -> Path:
    base = data_root(home) / cfg.get("stage_subdir", "dream-proposals")
    if day:
        return base / day
    days = sorted([p for p in base.iterdir() if p.is_dir()], reverse=True) if base.exists() else []
    if not days:
        raise ApplyRefused("no Dream Cycle stage yet — run `dream-cycle propose` first")
    return days[0]


def _char_limit(home: Path, cfg: Dict[str, Any], target: Path) -> Optional[int]:
    if cfg.get("apply_max_chars"):
        return int(cfg["apply_max_chars"])
    if target.name.lower() not in ("memory.md", "user.md"):
        return None
    try:
        from .yaml_load import safe_load
        mem = (safe_load((home / "config.yaml").read_text()) or {}).get("memory") or {}
    except Exception:
        return None
    key = "memory_char_limit" if target.name.lower() == "memory.md" else "user_char_limit"
    v = mem.get(key)
    return int(v) if v else None


def _label(cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    for lab in cfg.get("labels") or []:
        if lab["name"] == name:
            return lab
    raise ApplyRefused(f"label {name!r} is not in this profile's config")


def _evidence_text(evidence: str) -> str:
    """Collapse evidence whitespace. Empty stays empty — never a made-up source."""
    return " ".join((evidence or "").split())


def _evidence_clause(evidence: str) -> str:
    """`, because <source>` when a source was given, else empty."""
    text = _evidence_text(evidence)
    if not text:
        return ""
    return ", because " + text


def plan_edit(text: str, label: Dict[str, Any], retired: List[str], keep: str, evidence: str,
              pid: str, day: str, line_nos: Optional[List[int]] = None, all_lines: bool = False) -> tuple:
    """Pure: return (new_text, changes, markers).

    Edits only the proposal's own ``line_nos`` unless ``all_lines``. Refuses
    (ApplyRefused) when nothing live would change, or when a line to be edited
    carries both ``keep`` and a retired value — that is usually two different
    facts sharing a label (e.g. two services on :8080 and :8089), not a stale one.
    """
    keep_newline = text.endswith("\n")
    lines = text.splitlines()
    scope = None if all_lines else {int(n) for n in (line_nos or [])}
    if scope is not None and not scope:
        raise ApplyRefused("proposal has no line_nos; re-run propose (or pass all_lines)")
    changes, first_idx = [], None
    for i, line in enumerate(lines):
        if scope is not None and (i + 1) not in scope:
            continue
        spans_all = line_value_spans(line, label)
        spans = [s for s in spans_all if s[0] in retired]
        if not spans:
            continue
        if any(s[0] == keep for s in spans_all):
            raise ApplyRefused(f"line {i + 1} carries both {keep!r} and retired {sorted({s[0] for s in spans})} — "
                               "likely two different facts sharing a label; edit by hand or reject")
        new = line
        for _n, _raw, st, en in sorted(spans, key=lambda s: s[2], reverse=True):
            new = new[:st] + keep + new[en:]
        lines[i] = new
        changes.append({"line_no": i + 1, "before": line, "after": new})
        first_idx = i if first_idx is None else first_idx
    if not changes:
        raise ApplyRefused("no live line in the proposal's scope carries a retired value — nothing to apply")
    clause = _evidence_clause(evidence)
    markers = [f"SUPERSEDED {day}: {old} → {keep}{clause} [dream-cycle {pid}]" for old in retired]
    lines[first_idx:first_idx] = markers
    return "\n".join(lines) + ("\n" if keep_newline else ""), changes, markers


def plan_append(text: str, line: str, evidence: str, pid: str, day: str) -> tuple:
    """Append a new user-fact line with a short provenance note above it."""
    new_line = (line or "").rstrip("\n")
    if not new_line.strip():
        raise ApplyRefused("empty proposed_line for append")
    body = text
    if body and not body.endswith("\n"):
        body += "\n"
    text_ev = _evidence_text(evidence)
    if text_ev:
        note = f"# dream-cycle append {day}: {text_ev} [{pid}]"
    else:
        note = f"# dream-cycle append {day} [{pid}]"
    addition = note + "\n" + new_line + "\n"
    new_text = body + addition
    # line_no of the new fact line
    line_no = body.count("\n") + 2
    changes = [{"line_no": line_no, "before": "", "after": new_line}]
    return new_text, changes, [note]


def stale_reason(prop: Dict[str, Any], text: str) -> Optional[str]:
    """None if the proposal still describes the file; else why it is stale.

    Proposals carry ``file_sha256`` and ``lines`` ({line_no: text}) from propose
    time. A changed file is fine as long as every cited line is byte-identical.
    Append-style user-fact proposals cite no lines: they are fresh when the file
    sha still matches, otherwise refused (re-propose).
    """
    lines_then = prop.get("lines")
    if not prop.get("file_sha256") or not isinstance(lines_then, dict):
        return "proposal predates freshness fields (no file_sha256/lines); re-run propose"
    if sha256(text) == prop["file_sha256"]:
        return None
    if prop.get("action") == "append" or prop.get("kind") == "user_fact":
        return "target file changed since propose; re-run propose before append"
    now = text.splitlines()
    for n, then in lines_then.items():
        i = int(n) - 1
        if i >= len(now) or now[i] != then:
            return f"line {n} changed since propose ({prop.get('generated_at') or 'stage time'}); re-run propose"
    return None


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".dc-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def apply_proposal(home: Path, proposal_id: str, keep: str, evidence: str, actor: str, *,
                   config: Optional[Dict[str, Any]] = None, day: Optional[str] = None,
                   dry_run: bool = False, today: Optional[date] = None,
                   retire: Optional[List[str]] = None, all_lines: bool = False,
                   overlay: Optional[Dict[str, Any]] = None) -> ApplyResult:
    home = Path(home)
    cfg = validate_config(config or default_config())
    root = data_root(home)
    if not dry_run and not cfg.get("apply_enabled"):
        return ApplyResult(False, "refused", "apply_enabled is false in plugin-data/dream-cycle/config.yaml")
    if is_disabled(root, cfg.get("disabled_filename", "DISABLED")):
        return ApplyResult(False, "refused", "DISABLED kill switch present")
    actor, evidence, keep = (actor or "").strip(), _evidence_text(evidence), _normalize(keep or "")
    if not actor:
        return ApplyResult(False, "refused", "actor is required (who decided)")
    # Evidence is optional after an explicit accept (v0.8.2). A non-empty
    # source still authorizes a direct apply. Never invent a placeholder.
    if len(evidence) > int(cfg.get("apply_evidence_max") or 200):
        return ApplyResult(False, "refused", f"evidence longer than {cfg.get('apply_evidence_max')} chars; keep it to the source")
    hit = secret_hit(evidence, actor, keep)
    if hit:
        return ApplyResult(False, "refused", f"secret scan failed on apply inputs ({hit}); nothing written")
    fd = acquire_lock(root / cfg.get("lock_filename", "dream-cycle.lock"))
    try:
        stage = _stage_dir(home, cfg, day)
        summ = json.loads((stage / "summary.json").read_text())
        prop = next((p for p in summ.get("proposals", []) if p.get("proposal_id") == proposal_id), None)
        if prop is None:
            return ApplyResult(False, "refused", f"proposal {proposal_id} not in {stage.name}/summary.json")
        if overlay:
            prop = dict(prop)
            for key in ("file_sha256", "lines", "line_nos"):
                if overlay.get(key) is not None:
                    prop[key] = overlay[key]
        last = latest_decisions(stage).get(proposal_id, {})
        if last.get("decision") == "applied":
            return ApplyResult(False, "refused", f"already applied (receipt {last.get('receipt_id')}); roll back first")
        # Today first, then any prior day (v0.4.1) — a reject stays a reject.
        across = latest_decisions_across(stage.parent).get(proposal_id, {})
        if last.get("decision") == "reject" or across.get("decision") == "reject":
            return ApplyResult(False, "refused", "proposal was rejected in review.jsonl")
        values = [_normalize(v) for v in prop.get("values", [])]
        if keep not in values:
            return ApplyResult(False, "refused", f"keep {keep!r} is not one of the proposal values {values}")
        if not evidence:
            if last.get("decision") != "accept":
                return ApplyResult(
                    False, "refused",
                    "proposal is not accepted; evidence is optional only after an explicit accept",
                )
            accepted_keep = _normalize(str(last.get("keep") or ""))
            if accepted_keep and accepted_keep != keep:
                return ApplyResult(
                    False, "refused",
                    f"keep {keep!r} does not match the accepted keep {accepted_keep!r}",
                )
        others = [v for v in values if v != keep]
        if retire:
            retired = [_normalize(v) for v in retire]
            bad = [v for v in retired if v not in others]
            if bad:
                return ApplyResult(False, "refused", f"retire {bad} not among the proposal's other values {others}")
        elif len(others) > 1:
            return ApplyResult(False, "refused", f"proposal has {len(values)} values; name the stale one(s) with "
                                                 f"retire (candidates {others}) — different facts may share a label")
        else:
            retired = others
        target = Path(prop["file"])
        try:
            target.resolve().relative_to(home.resolve())
        except ValueError:
            return ApplyResult(False, "refused", f"{target} is outside this profile")
        if not target.is_file() or target.is_symlink():
            return ApplyResult(False, "refused", f"{target} is missing or a symlink")
        g = _grounding(home, cfg, stage, summ, prop, keep)
        if cfg.get("require_grounded_evidence") and g["evidence_status"] != "grounded":
            r = _refuse_logged(stage, proposal_id, actor, f"evidence unverified: {g.get('detail')} "
                               "(require_grounded_evidence is on)", dry_run, file=str(target),
                               evidence_status=g["evidence_status"], grounding=g)
            r.evidence_status, r.grounding = g["evidence_status"], g
            return r
        try:
            with memory_file_lock(target, float(cfg.get("memory_lock_timeout_s") or 10)):
                return _apply_locked(home, cfg, root, stage, prop, proposal_id, target, keep, retired,
                                     evidence, actor, today, dry_run, all_lines, g)
        except LockBusy as e:
            return ApplyResult(False, "refused", f"MEMORY lock busy ({e}); try again", file=str(target))
    finally:
        release_lock(fd)


def _refuse_logged(stage: Path, proposal_id: str, actor: str, msg: str, dry_run: bool, **extra) -> ApplyResult:
    reason = (msg or "").strip() or "apply refused"
    if not dry_run:
        append_event(stage, {"proposal_id": proposal_id, "decision": "apply-refused", "actor": actor,
                             "evidence": reason, "reason": reason, **extra})
    return ApplyResult(False, "refused", reason, file=extra.get("file"))


def _grounding(home, cfg, stage, summ, prop, keep) -> Dict[str, Any]:
    """Check ``keep`` against the dreamed day's + stage day's sessions (and any cited session ids)."""
    if prop.get("kind") == "user_fact" or prop.get("action") == "append":
        ev = prop.get("session_evidence") or []
        if ev:
            return {
                "evidence_status": "grounded",
                "detail": f"user-fact cited {len(ev)} user message(s)",
                "matches": [
                    {"session_id": e.get("session_id"), "message_id": e.get("message_id"),
                     "line_sha256": e.get("line_sha256"),
                     "role": e.get("role") or "user", "snippet": e.get("snippet")}
                    for e in ev[:8]
                ],
            }
        return {"evidence_status": "unverified", "detail": "user-fact missing session_evidence", "matches": []}
    days = [d for d in (summ.get("session_date"), summ.get("date") or stage.name) if d]
    try:
        return ground_value(home, _label(cfg, prop["label"]), keep, days, cfg,
                            session_ids=prop.get("session_ids"))
    except Exception as e:  # grounding is evidence, never a crash
        return {"evidence_status": "unverified", "detail": f"grounding error {e.__class__.__name__}", "matches": []}


def _apply_locked(home, cfg, root, stage, prop, proposal_id, target, keep, retired, evidence, actor,
                  today, dry_run, all_lines, g=None) -> ApplyResult:
    g = g or {"evidence_status": "unverified", "matches": []}
    text = target.read_text(encoding="utf-8")
    why = stale_reason(prop, text)
    if why:
        return _refuse_logged(stage, proposal_id, actor, f"stale proposal: {why}", dry_run, file=str(target),
                              sha_now=sha256(text), sha_at_propose=prop.get("file_sha256"))
    d = (today or date.today()).isoformat()
    is_append = prop.get("action") == "append" or prop.get("kind") == "user_fact"
    try:
        if is_append:
            line = prop.get("proposed_line") or keep
            new_text, changes, markers = plan_append(text, line, evidence, proposal_id, d)
        else:
            new_text, changes, markers = plan_edit(text, _label(cfg, prop["label"]), retired, keep, evidence,
                                                   proposal_id, d, line_nos=prop.get("line_nos"),
                                                   all_lines=all_lines)
    except ApplyRefused as e:
        return ApplyResult(False, "refused", str(e), file=str(target))
    hit = secret_hit(*markers, *[c["after"] for c in changes])
    if hit:
        return ApplyResult(False, "refused", f"secret scan failed on lines to be written ({hit}); nothing written")
    limit = _char_limit(home, cfg, target)
    if limit and len(new_text) > limit and len(new_text) > len(text):
        return ApplyResult(False, "refused", f"{target.name} would be {len(new_text)} chars (limit {limit}); "
                                             "trim memory first or set apply_max_chars")
    res = ApplyResult(True, "dry-run", f"{len(changes)} line(s) + {len(markers)} marker(s) would change",
                      file=str(target), sha_before=sha256(text), sha_after=sha256(new_text),
                      changes=changes, markers=markers, evidence_status=g["evidence_status"], grounding=g)
    if dry_run:
        return res
    rid = datetime.now().strftime("%Y%m%dT%H%M%S%f") + "-" + proposal_id
    snap_dir = root / "snapshots" / rid
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap = snap_dir / target.name
    snap.write_text(text, encoding="utf-8")
    if sha256(snap.read_text(encoding="utf-8")) != res.sha_before:
        return ApplyResult(False, "refused", "snapshot verification failed; nothing written")
    if _pre_write_hook:
        _pre_write_hook(target)
    # Compare-and-swap: the file must still be exactly what we planned against.
    if sha256(target.read_text(encoding="utf-8")) != res.sha_before:
        return _refuse_logged(stage, proposal_id, actor, "file changed since read (concurrent write); "
                              "nothing written — re-run propose", False, file=str(target))
    try:
        _write_atomic(target, new_text)
        after = target.read_text(encoding="utf-8")
        if is_append:
            still = []
            if sha256(after) != res.sha_after or (prop.get("proposed_line") or keep) not in after:
                raise ApplyRefused("post-write verify failed")
        else:
            lab = _label(cfg, prop["label"])
            al = after.splitlines()
            changed_now = [al[c["line_no"] - 1 + len(markers)] for c in changes]
            still = [ln for ln in changed_now for s in line_value_spans(ln, lab) if s[0] in retired]
            if sha256(after) != res.sha_after or still or keep not in after:
                raise ApplyRefused("post-write verify failed")
    except Exception as e:
        _write_atomic(target, text) if target.exists() else target.write_text(text, encoding="utf-8")
        return ApplyResult(False, "refused", f"{e}; snapshot restored")
    res.status, res.message, res.receipt_id, res.snapshot = "applied", \
        f"applied {len(changes)} line(s), {len(markers)} SUPERSEDED marker(s)", rid, str(snap)
    receipt = {"receipt_id": rid, "proposal_id": proposal_id, "stage": str(stage), "label": prop["label"],
               "retired": retired, "keep": keep, "evidence": evidence, "actor": actor,
               "speaker": prop.get("speaker"), "file": str(target), "snapshot": str(snap),
               "sha_before": res.sha_before, "sha_after": res.sha_after, "changes": changes,
               "markers": markers, "scope": "all-lines" if all_lines else "proposal-lines",
               "evidence_status": g["evidence_status"], "grounding": g,
               "file_changed_since_propose": res.sha_before != prop.get("file_sha256"),
               "applied_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "rollback": f"hermes dream-cycle rollback --receipt {rid}"}
    (root / "receipts").mkdir(exist_ok=True)
    (root / "receipts" / f"{rid}.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    append_event(stage, {"proposal_id": proposal_id, "decision": "applied", "actor": actor,
                         "evidence": evidence, "speaker": prop.get("speaker"), "keep": keep,
                         "retired": retired, "file": str(target), "sha_before": res.sha_before,
                         "sha_after": res.sha_after, "receipt_id": rid, "snapshot": str(snap),
                         "evidence_status": g["evidence_status"],
                         "grounding_matches": [{k: m[k] for k in ("session_id", "message_id", "line_sha256")}
                                               for m in g.get("matches", [])]})
    return res


def _shift_after_sibling(prop: Dict[str, Any], first_line: int, n_markers: int,
                         new_text: str) -> Optional[Dict[str, Any]]:
    """Retarget a not-yet-applied sibling after this batch wrote ``new_text``.

    Marker inserts shift later line numbers. Cited text that is still present
    is remapped. A cited line whose text changed, or an external edit that
    does not match this write, returns None so the normal stale check refuses.
    """
    new_sha = sha256(new_text)
    if prop.get("action") == "append" or prop.get("kind") == "user_fact":
        out = dict(prop)
        out["file_sha256"] = new_sha
        return out
    now = new_text.splitlines()
    new_lines: Dict[str, str] = {}
    for n_str, then in (prop.get("lines") or {}).items():
        n = int(n_str)
        n2 = n + n_markers if n >= first_line else n
        if n2 < 1 or n2 > len(now) or now[n2 - 1] != then:
            return None
        new_lines[str(n2)] = then
    out = dict(prop)
    out["lines"] = new_lines
    out["line_nos"] = [int(n) + n_markers if int(n) >= first_line else int(n)
                       for n in (prop.get("line_nos") or [])]
    out["file_sha256"] = new_sha
    return out


def apply_accepted(home: Path, *, config: Optional[Dict[str, Any]] = None, day: Optional[str] = None,
                   actor: str = "dream-cycle-nightly") -> List[Dict[str, Any]]:
    """Apply every proposal whose *today's* review row is ``accept`` with a ``keep`` value.

    Prior-day accepts are never auto-applied (v0.4.1). A guardian must accept
    again in the current stage. Blank evidence on the accept row is preserved.
    Several accepts that target one file are applied in one batch: a sibling
    write's marker or append is not treated as an outside edit. A cited line
    that actually changed, or a file touched by something other than this
    batch, is still refused.
    """
    cfg = validate_config(config or default_config())
    try:
        stage = _stage_dir(Path(home), cfg, day)
    except ApplyRefused as e:
        return [{"ok": False, "status": "refused", "message": str(e)}]
    try:
        summ = json.loads((stage / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        summ = {}
    working = {p.get("proposal_id"): dict(p) for p in (summ.get("proposals") or []) if p.get("proposal_id")}
    batch_sha: Dict[str, str] = {}
    out = []
    # Today-only: never pull an accept from a prior stage_day.
    for pid, row in latest_decisions(stage).items():
        if row.get("carried_from"):
            continue  # carried rows are reject/defer only; belt-and-suspenders
        if not (row.get("decision") == "accept" and row.get("keep")):
            continue
        prop = working.get(pid) or {}
        target = prop.get("file") or ""
        overlay = None
        if target and target in batch_sha:
            try:
                current = sha256(Path(target).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                current = ""
            if current == batch_sha[target] and prop.get("file_sha256") == current:
                overlay = {k: prop.get(k) for k in ("file_sha256", "lines", "line_nos")}
        r = apply_proposal(home, pid, row["keep"], row.get("evidence", ""), f"{row['actor']} via {actor}",
                           config=cfg, day=stage.name, retire=row.get("retire"), overlay=overlay)
        out.append({"proposal_id": pid, **r.to_dict()})
        if not (r.ok and r.status == "applied" and target):
            continue
        try:
            new_text = Path(target).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        first = min((c.get("line_no") or 0) for c in (r.changes or [])) if r.changes else 10 ** 9
        n_markers = len(r.markers or [])
        for oid, other in list(working.items()):
            if oid == pid or other.get("file") != target:
                continue
            shifted = _shift_after_sibling(other, first, n_markers, new_text)
            if shifted is not None:
                working[oid] = shifted
        batch_sha[target] = sha256(new_text)
    return out


def list_receipts(home: Path) -> List[Dict[str, Any]]:
    d = data_root(Path(home)) / "receipts"
    if not d.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]


def rollback(home: Path, receipt_id: Optional[str] = None, *, last: bool = False, force: bool = False,
             actor: str = "rollback", config: Optional[Dict[str, Any]] = None) -> ApplyResult:
    home = Path(home)
    cfg = validate_config(config or default_config())
    root = data_root(home)
    recs = [r for r in list_receipts(home) if not r.get("rolled_back_at")]
    if receipt_id:
        rec = next((r for r in list_receipts(home) if r["receipt_id"] == receipt_id), None)
        if rec is None:
            return ApplyResult(False, "refused", f"no receipt {receipt_id}")
        if rec.get("rolled_back_at"):
            return ApplyResult(False, "refused", f"receipt {receipt_id} already rolled back")
    elif last and recs:
        rec = recs[-1]
    else:
        return ApplyResult(False, "refused", "nothing to roll back (give --receipt ID or --last)")
    fd = acquire_lock(root / cfg.get("lock_filename", "dream-cycle.lock"))
    try:
        target, snap = Path(rec["file"]), Path(rec["snapshot"])
        try:
            with memory_file_lock(target, float(cfg.get("memory_lock_timeout_s") or 10)):
                return _rollback_locked(root, rec, target, snap, force, actor)
        except LockBusy as e:
            return ApplyResult(False, "refused", f"MEMORY lock busy ({e}); try again", receipt_id=rec["receipt_id"])
    finally:
        release_lock(fd)


def _rollback_locked(root, rec, target, snap, force, actor) -> ApplyResult:
    cur = target.read_text(encoding="utf-8") if target.is_file() else ""
    if sha256(cur) != rec["sha_after"] and not force:
        return ApplyResult(False, "refused", f"{target.name} changed since apply (sha differs); "
                                             "re-run with --force to overwrite with the snapshot",
                           receipt_id=rec["receipt_id"])
    snap_text = snap.read_text(encoding="utf-8")
    if sha256(snap_text) != rec["sha_before"]:
        return ApplyResult(False, "refused", "snapshot no longer matches sha_before; not restoring")
    safety = snap.with_name(snap.name + ".pre-rollback")
    if target.is_file():
        safety.write_text(cur, encoding="utf-8")
    if _pre_write_hook:
        _pre_write_hook(target)
    if (target.read_text(encoding="utf-8") if target.is_file() else "") != cur:  # CAS
        return ApplyResult(False, "refused", "file changed during rollback (concurrent write); nothing restored",
                           receipt_id=rec["receipt_id"])
    _write_atomic(target, snap_text) if target.is_file() else target.write_text(snap_text, encoding="utf-8")
    if sha256(target.read_text(encoding="utf-8")) != rec["sha_before"]:
        _write_atomic(target, cur)
        return ApplyResult(False, "refused", "restore verify failed; left file as it was")
    rec["rolled_back_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    (root / "receipts" / f"{rec['receipt_id']}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    append_event(Path(rec["stage"]), {"proposal_id": rec["proposal_id"], "decision": "rolled-back",
                                      "actor": actor, "receipt_id": rec["receipt_id"], "file": str(target),
                                      "sha_before": rec["sha_after"], "sha_after": rec["sha_before"],
                                      "evidence": "rollback of " + rec["receipt_id"]})
    return ApplyResult(True, "rolled-back", f"restored {target.name} from snapshot", rec["receipt_id"],
                       str(target), rec["sha_after"], rec["sha_before"], str(snap))
