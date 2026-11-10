"""Guardian review ledger (Dream Cycle copy) (Exo-derived, 2026-11-06).

Append-only ``review.jsonl`` that sits next to a dated proposals folder.
Each row records a *human* (or named guardian) decision about one proposal:

    {"proposal_id", "evidence", "decision": "accept|reject|defer",
     "actor", "ts", "note"}  # evidence may be empty on accept

Nothing here decides anything automatically. apply.py appends 'applied' /
'rolled-back' receipt rows here (append_event); it never rewrites the ledger.
Rejections and defers are logged too — the evidence trail is the point.
The latest row per proposal_id wins when computing status; older rows stay.

v0.4.1: decisions carry across days. ``undecided`` / status consult the latest
row for a proposal_id across all ``dream-proposals/*/review.jsonl``. Propose
copies a prior reject/defer into today's ledger with ``carried_from`` so the
trail stays auditable. Accepts are NEVER carried — apply --accepted only acts
on an accept recorded in today's stage (and still re-checks file sha).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

LEDGER = "review.jsonl"
DECISIONS = ("accept", "reject", "defer")
CARRYABLE = ("reject", "defer")


def proposal_id(p: Dict[str, Any]) -> str:
    """Stable short id from label + value(s) + file + line(s)."""
    vals = p.get("values") or [p.get("value")]
    lines = p.get("line_nos") or [p.get("line_no")]
    key = "|".join([str(p.get("label")), ",".join(map(str, vals)), str(p.get("file")),
                    ",".join(map(str, lines))])
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def new_batch_id() -> str:
    """Short id shared by every accept row in one Accept-all click."""
    return "b" + hashlib.sha1(os.urandom(16)).hexdigest()[:12]


def append_review(stage: Path, proposal_id: str, decision: str, actor: str,
                  evidence: str = "", note: str = "", ts: Optional[str] = None,
                  keep: Optional[str] = None, retire: Optional[List[str]] = None,
                  carried_from: Optional[str] = None,
                  batch_id: Optional[str] = None) -> Dict[str, Any]:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if not actor or not str(actor).strip():
        raise ValueError("actor is required (who decided)")
    if not proposal_id:
        raise ValueError("proposal_id is required")
    row = {"proposal_id": proposal_id, "evidence": evidence, "decision": decision,
           "actor": str(actor).strip(), "ts": ts or datetime.now().astimezone().isoformat(timespec="seconds"),
           "note": note}
    if keep is not None:
        row["keep"] = str(keep)  # the value the guardian says is current (used by apply --accepted)
    if retire:
        row["retire"] = [str(v) for v in retire]
    if carried_from:
        row["carried_from"] = str(carried_from)
    if batch_id:
        row["batch_id"] = str(batch_id)
    stage = Path(stage)
    stage.mkdir(parents=True, exist_ok=True)
    with open(stage / LEDGER, "a", encoding="utf-8") as f:  # append-only
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())
    return row


EVENTS = ("applied", "rolled-back", "apply-refused", "nosave-skip", "reverted")


def append_event(stage: Path, row: Dict[str, Any]) -> Dict[str, Any]:
    """Append an apply/rollback/revert receipt row (append-only, fsync'd)."""
    if row.get("decision") not in EVENTS:
        raise ValueError(f"event decision must be one of {EVENTS}")
    if not row.get("proposal_id") or not str(row.get("actor") or "").strip():
        raise ValueError("receipt rows need proposal_id and actor")
    row = {**row, "ts": row.get("ts") or datetime.now().astimezone().isoformat(timespec="seconds")}
    stage = Path(stage)
    stage.mkdir(parents=True, exist_ok=True)
    with open(stage / LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())
    return row


def read_reviews(stage: Path) -> List[Dict[str, Any]]:
    path = Path(stage) / LEDGER
    if not path.is_file():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue  # never crash on a torn line; never rewrite the ledger
    return out


def iter_stage_dirs(proposals_root: Path) -> List[Path]:
    """Dated stage folders under dream-proposals/, chronological by name."""
    root = Path(proposals_root)
    if not root.is_dir():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)


def latest_decisions(stage: Path) -> Dict[str, Dict[str, Any]]:
    """Latest row per proposal_id in a single day's ledger (today-only)."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in read_reviews(stage):
        out[r.get("proposal_id")] = r
    return out


def latest_decisions_across(proposals_root: Path) -> Dict[str, Dict[str, Any]]:
    """Latest row per proposal_id across all day folders (chronological overwrite).

    Each returned row includes ``stage_day`` (folder name) so callers know which
    day's ledger held the winning decision.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for stage in iter_stage_dirs(proposals_root):
        for r in read_reviews(stage):
            pid = r.get("proposal_id")
            if not pid:
                continue
            out[pid] = {**r, "stage_day": stage.name}
    return out


def undecided(stage: Path, proposals: Iterable[Dict[str, Any]], *,
              across: bool = True) -> List[Dict[str, Any]]:
    """Proposals with no accept/reject yet (defer counts as still open).

    By default consults every prior day's ledger for the same proposal_id
    (v0.4.1). Pass across=False to read only ``stage``'s own review.jsonl.
    """
    stage = Path(stage)
    dec = latest_decisions_across(stage.parent) if across else latest_decisions(stage)
    res = []
    for p in proposals:
        pid = p.get("proposal_id") or proposal_id(p)
        d = dec.get(pid, {}).get("decision")
        if d not in ("accept", "reject", "applied"):
            res.append({**p, "proposal_id": pid, "last_decision": d})
    return res


def carry_prior_decisions(stage: Path, proposals: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copy a prior reject/defer into today's ledger for matching proposal_ids.

    Never carries accept/applied — apply --accepted must see a same-day accept
    whose proposal still matches the live file sha. Marks each copy with
    ``carried_from`` = the prior stage day. Skips ids that already have a row
    in today's ledger. Returns the rows appended.
    """
    stage = Path(stage)
    today = latest_decisions(stage)
    across = latest_decisions_across(stage.parent)
    carried: List[Dict[str, Any]] = []
    for p in proposals:
        pid = p.get("proposal_id") or proposal_id(p)
        if pid in today:
            continue
        prior = across.get(pid)
        if not prior:
            continue
        prior_day = prior.get("stage_day")
        if not prior_day or prior_day == stage.name:
            continue
        decision = prior.get("decision")
        if decision not in CARRYABLE:
            continue
        note = (prior.get("note") or "").strip()
        tag = f"[carried_from {prior_day}]"
        if tag not in note:
            note = f"{note} {tag}".strip() if note else tag
        row = append_review(
            stage, pid, decision, prior.get("actor") or "carried",
            evidence=prior.get("evidence") or "",
            note=note,
            keep=prior.get("keep"),
            retire=prior.get("retire"),
            carried_from=prior_day,
        )
        carried.append(row)
    return carried



# Dashboard treats these as decided (Accept/Reject/Defer hidden until Change).
DECIDED_FOR_UI = ("accept", "reject", "defer", "applied", "rolled-back")


def accept_pending(stage: Path, proposals: Iterable[Dict[str, Any]], actor: str,
                   *, evidence: str = "", note: str = "",
                   proposal_ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Accept currently pending proposals with default keep (first value).

    Writes one append-only ``accept`` row each, tagged with a shared
    ``batch_id`` so the batch can be undone later. Does not apply to memory
    files. If ``proposal_ids`` is given, only those ids are considered
    (already-decided ids are skipped). Evidence may be empty.
    """
    stage = Path(stage)
    today = latest_decisions(stage)
    wanted = None if proposal_ids is None else set(proposal_ids)
    to_accept: List[tuple] = []
    for p in proposals:
        pid = p.get("proposal_id") or proposal_id(p)
        if wanted is not None and pid not in wanted:
            continue
        last = today.get(pid) or {}
        if last.get("decision") in DECIDED_FOR_UI:
            continue
        values = [str(v) for v in (p.get("values") or []) if str(v)]
        if not values:
            continue
        to_accept.append((pid, values[0]))
    if not to_accept:
        return {"batch_id": None, "count": 0, "rows": []}
    bid = new_batch_id()
    rows: List[Dict[str, Any]] = []
    for pid, keep in to_accept:
        rows.append(append_review(
            stage, pid, "accept", actor,
            evidence=evidence, note=note, keep=keep, batch_id=bid,
        ))
    return {"batch_id": bid, "count": len(rows), "rows": rows}


def revert_batch(stage: Path, batch_id: str, actor: str, *,
                 note: str = "") -> Dict[str, Any]:
    """Append ``reverted`` rows for this batch's accepts that are still latest.

    Never deletes or rewrites ``review.jsonl``. After a revert, the proposal
    is pending again (latest row wins). Already-changed decisions are left
    alone. Unknown ``batch_id`` returns ``unknown=True`` and count 0.
    """
    if not batch_id:
        raise ValueError("batch_id is required")
    stage = Path(stage)
    all_rows = read_reviews(stage)
    members = [r for r in all_rows
               if r.get("batch_id") == batch_id and r.get("decision") == "accept"]
    known = any(r.get("batch_id") == batch_id for r in all_rows)
    if not known:
        return {"batch_id": batch_id, "count": 0, "rows": [], "unknown": True}
    latest = latest_decisions(stage)
    rows: List[Dict[str, Any]] = []
    for acc in members:
        pid = acc.get("proposal_id")
        cur = latest.get(pid) or {}
        if cur.get("decision") != "accept" or cur.get("batch_id") != batch_id:
            continue
        row = append_event(stage, {
            "proposal_id": pid,
            "decision": "reverted",
            "actor": actor,
            "evidence": "",
            "note": note or "undo accept-all",
            "batch_id": batch_id,
            "reverts_ts": acc.get("ts"),
            "keep": acc.get("keep"),
        })
        rows.append(row)
        latest[pid] = row
    return {"batch_id": batch_id, "count": len(rows), "rows": rows, "unknown": False}


def _main(argv=None) -> int:
    """CLI: append <stage> <proposal_id> <accept|reject|defer> <actor> [--evidence E] [--note N]
           list <stage>   (print ledger rows)"""
    import argparse
    ap = argparse.ArgumentParser(prog="review")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("append"); a.add_argument("stage"); a.add_argument("proposal_id")
    a.add_argument("decision", choices=DECISIONS); a.add_argument("actor")
    a.add_argument("--evidence", default=""); a.add_argument("--note", default="")
    a.add_argument("--keep", default=None, help="value that is current (accept only)")
    a.add_argument("--retire", action="append", default=None, help="stale value(s) to retire")
    l = sub.add_parser("list"); l.add_argument("stage")
    ns = ap.parse_args(argv)
    if ns.cmd == "append":
        print(json.dumps(append_review(Path(ns.stage), ns.proposal_id, ns.decision, ns.actor,
                                       ns.evidence, ns.note, keep=ns.keep, retire=ns.retire)))
    else:
        for r in read_reviews(Path(ns.stage)):
            print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
