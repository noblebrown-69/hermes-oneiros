"""Deterministic Dream Cycle proposer — no model calls."""
from __future__ import annotations
import json, re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import default_config, validate_config
from .sessions import SessionResult, fetch_session_lines, format_costs, session_costs
from .markers import in_iso_date, in_paper_id, is_superseded_line, line_tier, parse_attr
from .review import (proposal_id as _pid, undecided as _undecided, carry_prior_decisions,
                    append_event, read_reviews)
from .paths import acquire_lock, data_root, is_disabled, release_lock, resolve_memory_files
from .user_facts import (char_limits_from_home, extract_user_facts, fetch_user_messages,
                        filter_new_facts, resolve_target)
from .nosave import (apply_to_session, empty_index, fetch_conversation_messages,
                     format_nosave_status, lookback_union, scan_messages)

@dataclass
class Proposal:
    label: str
    values: List[str]
    file: str
    line_nos: List[int]
    note: str = ""
    speaker: Optional[str] = None  # optional ATTR provenance; backward compatible
    session_ids: Optional[List[str]] = None
    session_evidence: Optional[List[Dict[str, Any]]] = None  # cite-by-hash + short snippet
    # v0.7.0 user-fact fields (optional; stack proposals leave these None/default)
    kind: Optional[str] = None  # "user_fact" | None
    category: Optional[str] = None  # PERSON|EVENT|PREFERENCE|STATUS
    action: Optional[str] = None  # "append" for new user-fact lines
    proposed_line: Optional[str] = None
    confidence: Optional[float] = None
    expires: Optional[str] = None

@dataclass
class ProposeResult:
    proposals: List[Proposal] = field(default_factory=list)
    stage_path: Optional[str] = None
    skipped_reason: Optional[str] = None
    scanned_files: int = 0
    session_status: str = "disabled"
    session_detail: str = ""

_WORD = re.compile(r"\S+")

def _nearby_ok(line: str, start: int, end: int, words: int, label_re: Optional[str]) -> bool:
    # Colon-form ports (:9119) are self-identifying — no nearby keyword required.
    if start > 0 and line[start - 1] == ":":
        return True
    if start < len(line) and line[start] == ":":
        return True
    if not label_re:
        return True
    tokens = list(_WORD.finditer(line))
    if not tokens:
        return False
    idx = 0
    for i, t in enumerate(tokens):
        if t.start() <= start < t.end() or start <= t.start() < end:
            idx = i
            break
    lo, hi = max(0, idx - words), min(len(tokens), idx + words + 1)
    window = " ".join(t.group(0) for t in tokens[lo:hi])
    return bool(re.search(label_re, window))


def _normalize(v: str) -> str:
    return re.sub(r"\s+", " ", v.lstrip("~").strip())


_SERVICE_STOP = {
    "port", "listen", "bind", "endpoint", "url", "http", "https", "on", "the", "a", "an",
    "via", "for", "to", "at", "and", "or", "with", "from", "over", "only", "shared",
    "live", "lab", "chat", "image", "gen", "never", "bounce", "it", "fallback",
}


def service_key_near(line: str, start: int, end: int) -> str:
    """High-confidence service/host name left of a labeled value. Empty if unknown.

    Only backtick names or the token immediately before ``:PORT`` count — bare
    nearby words like "listen"/"also" are too fuzzy and must not suppress a
    real conflict proposal.
    """
    left = line[max(0, start - 96):start]
    ticks = re.findall(r"`([^`]+)`", left)
    if ticks:
        # First token inside the tick (ignore args like `img-gen "<prompt>"`).
        tok = ticks[-1].strip().split()[0].strip("\"'" )
        if tok:
            return tok.lower()
    # Immediate name before :PORT (… name:8089 or … name :8089)
    if start > 0 and line[start - 1] == ":":
        prefix = line[: start - 1].rstrip()
        m = re.search(r"`([^`]+)`$", prefix)
        if m:
            return m.group(1).strip().lower()
        m = re.search(r"([A-Za-z][\w.-]{1,48})$", prefix)
        if m:
            low = m.group(1).lower()
            if low not in _SERVICE_STOP:
                return low
    return ""


def distinct_service_conflict(hits: list, text_lines: list) -> bool:
    """True when every hit has a non-empty service key and all keys differ.

    ``hits`` are (norm, line_no, raw) from extract_by_label. When True, the
    multi-value set is different services sharing a label — not a stale fact.
    """
    keys = []
    for norm, line_no, raw in hits:
        if not (0 < line_no <= len(text_lines)):
            return False
        line = text_lines[line_no - 1]
        # Locate raw span for proximity; fall back to first digit/token match.
        idx = line.find(raw) if raw else -1
        if idx < 0:
            # try :port form
            idx = line.find(":" + norm) if norm else -1
            if idx >= 0:
                start, end = idx + 1, idx + 1 + len(norm)
            else:
                return False
        else:
            start, end = idx, idx + len(raw)
        key = service_key_near(line, start, end)
        if not key:
            return False
        keys.append(key)
    return len(keys) >= 2 and len(set(keys)) == len(keys)


def extract_by_label(text: str, label: Dict[str, Any]) -> List[tuple]:

    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for norm, raw, _s, _e in line_value_spans(line, label):
            hits.append((norm, i, raw))
    return hits


def line_value_spans(line: str, label: Dict[str, Any]) -> List[tuple]:
    """(normalized, raw, start, end) for each live labeled value on one line.

    Skips MECH:/STATUS: lines (recognized, not mined) and column-0 SUPERSEDED
    markers (retired values are history, not live facts).
    """
    if line_tier(line) or is_superseded_line(line):
        return []
    pat = re.compile(label["pattern"])
    words = int(label.get("proximity_words") or 3)
    label_re = label.get("nearby_label_re")
    drop_pid = bool(label.get("drop_if_looks_like_pid"))
    out = []
    for m in pat.finditer(line):
        raw = m.group(1) if m.lastindex else m.group(0)
        if not _nearby_ok(line, m.start(), m.end(), words, label_re):
            continue
        if drop_pid and re.fullmatch(r"\d{4,5}", raw) and re.search(r"(?i)\b(pid|process)\b", line):
            continue
        g = m.lastindex or 0
        if in_iso_date(line, m.start(g), m.end(g)):  # 2026 from 2026-11-05 is not a value
            continue
        if in_paper_id(line, m.start(g), m.end(g)):  # 2611 from arXiv:2611.00463 is not a port
            continue
        out.append((_normalize(raw), raw, m.start(g), m.end(g)))
    return out

def _with_freshness(p: "Proposal") -> Dict[str, Any]:
    """Proposal dict + what apply needs to detect staleness: the file's sha256 and
    the exact text of every cited line at propose time (2026-11-08 safe-write guard)."""
    import hashlib
    d = dict(asdict(p), proposal_id=_pid(asdict(p)))
    try:
        text = Path(p.file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return d
    lines = text.splitlines()
    d["file_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    d["lines"] = {str(n): lines[n - 1] for n in sorted(set(p.line_nos)) if 0 < n <= len(lines)}
    return d


def propose(
    hermes_home: Path,
    memory_roots: Optional[Sequence[Path]] = None,
    config: Optional[Dict[str, Any]] = None,
    run_date: Optional[date] = None,
    session_date: Optional[date] = None,
    session_result: Optional[SessionResult] = None,
    cost_result: Optional[Dict[str, Any]] = None,
) -> ProposeResult:
    """Stage proposal-only consolidations.

    run_date names the stage folder (default today). session_date is the day
    whose sessions are used for corroboration (default: run_date - 1, i.e. the
    day being dreamed). session_result lets callers/tests inject sessions.
    """
    cfg = validate_config(config or default_config())
    root = data_root(hermes_home)
    root.mkdir(parents=True, exist_ok=True)
    if is_disabled(root, cfg.get("disabled_filename", "DISABLED")):
        return ProposeResult(skipped_reason="DISABLED")
    lock_path = root / cfg.get("lock_filename", "dream-cycle.lock")
    fd = acquire_lock(lock_path)
    try:
        roots = list(memory_roots) if memory_roots else [hermes_home / "memories", hermes_home]
        files = resolve_memory_files(roots, cfg.get("memory_globs") or ["MEMORY.md"],
                                     shallow_roots=[hermes_home])
        proposals: List[Proposal] = []
        tiers: Dict[str, List[str]] = {"MECH": [], "STATUS": []}
        for f in files:
            text = f.read_text(errors="replace")
            tlines = text.splitlines()
            for n, ln in enumerate(tlines, 1):
                t = line_tier(ln)
                if t:
                    tiers[t].append(f"{f}:{n}")
            for label in cfg.get("labels") or []:
                hits = extract_by_label(text, label)
                vals = sorted({h[0] for h in hits})
                if len(vals) >= 2:
                    if cfg.get("skip_distinct_service_conflicts", True) and distinct_service_conflict(hits, tlines):
                        continue  # different named services/hosts — not a conflict
                    spk = sorted({s for s in (parse_attr(tlines[h[1] - 1])[0] for h in hits) if s})
                    proposals.append(Proposal(
                        label=label["name"], values=vals, file=str(f),
                        line_nos=[h[1] for h in hits],
                        note="same label, multiple values in one file — morning review",
                        speaker=",".join(spk) or None,
                    ))
        rd = run_date or date.today()
        sd = session_date or (rd - timedelta(days=1))
        sess = session_result or fetch_session_lines(sd.isoformat(), cfg, hermes_home=hermes_home)
        honor_nosave = bool(cfg.get("honor_nosave", True))
        nosave_idx = empty_index()
        nosave_error = ""
        if honor_nosave:
            try:
                conv = fetch_conversation_messages(
                    hermes_home,
                    lookback_days=lookback_union(cfg),
                    end_day=sd,
                    max_rows=int((cfg.get("user_facts") or {}).get("max_rows") or 8000) * 2,
                )
                nosave_idx = scan_messages(conv)
                sess = apply_to_session(nosave_idx, sess)
            except Exception as e:  # fail-soft: never abort propose
                nosave_error = f"{type(e).__name__}: {e}"
                nosave_idx = empty_index()
        if sess.lines:
            mem_vals: Dict[str, Dict[str, tuple]] = {}
            for f in files:
                text = f.read_text(errors="replace")
                for label in cfg.get("labels") or []:
                    for v, ln, _ in extract_by_label(text, label):
                        mem_vals.setdefault(label["name"], {}).setdefault(v, (str(f), ln))
            # Map each session line to its evidence row (state.db) when present.
            ev_rows = list(sess.evidence or [])
            stext = "\n".join(sess.lines)
            for label in cfg.get("labels") or []:
                mv = mem_vals.get(label["name"]) or {}
                # value -> evidence citations from sessions
                val_ev: Dict[str, List[Dict[str, Any]]] = {}
                for idx, line in enumerate(sess.lines):
                    for norm, _raw, _s, _e in line_value_spans(line, label):
                        cite = {"value": norm}
                        if idx < len(ev_rows):
                            er = ev_rows[idx]
                            cite.update({
                                "session_id": er.get("session_id"),
                                "message_id": er.get("message_id"),
                                "line_sha256": er.get("line_sha256"),
                                "snippet": er.get("snippet"),
                                "role": er.get("role"),
                            })
                        val_ev.setdefault(norm, []).append(cite)
                scfg = cfg.get("session_corroboration") or {}
                require_user = scfg.get("require_user_evidence", True)
                sv = sorted(val_ev)
                fresh = []
                for v in sv:
                    if v in mv:
                        continue
                    cites = val_ev.get(v) or []
                    # Legacy command source has no roles — treat as user-strength.
                    roles_present = [c.get("role") for c in cites if c.get("role")]
                    if require_user and roles_present and "user" not in roles_present:
                        continue  # assistant-only (weaker) is not enough alone
                    fresh.append(v)
                if mv and fresh:
                    first_file, first_line = next(iter(mv.values()))
                    cites = []
                    for v in fresh:
                        cites.extend(val_ev.get(v) or [])
                    # Dedup by (session_id, message_id, value)
                    seen, uniq = set(), []
                    for c in cites:
                        key = (c.get("session_id"), c.get("message_id"), c.get("value"))
                        if key in seen:
                            continue
                        seen.add(key)
                        uniq.append(c)
                    uniq = uniq[:8]
                    sids = sorted({c["session_id"] for c in uniq if c.get("session_id")})
                    snip = uniq[0].get("snippet") if uniq else ""
                    proposals.append(Proposal(
                        label=label["name"], values=sorted(mv) + fresh, file=first_file,
                        line_nos=[first_line],
                        note=(f"memory says {', '.join(sorted(mv))}; recent sessions said "
                              f"{', '.join(fresh)} — confirm which is current"
                              + (f" (e.g. «{snip}»)" if snip else "")),
                        session_ids=sids or None,
                        session_evidence=uniq or None,
                    ))
        # v0.7.0 — user-fact proposals from role=user messages only (no model / no network).
        uf_cfg = cfg.get("user_facts") or {}
        user_fact_status = "disabled"
        user_fact_detail = ""
        if uf_cfg.get("enabled", True):
            try:
                msgs = fetch_user_messages(
                    hermes_home,
                    lookback_days=int(uf_cfg.get("lookback_days") or 30),
                    max_rows=int(uf_cfg.get("max_rows") or 8000),
                    end_day=sd,
                )
                if honor_nosave and nosave_idx.skips:
                    msgs = nosave_idx.filter_messages(msgs)
                facts = extract_user_facts(msgs, ref_date=sd)
                allow = set(uf_cfg.get("categories") or ["PERSON", "EVENT", "PREFERENCE", "STATUS"])
                facts = [f for f in facts if f.category in allow]
                facts = filter_new_facts(facts, hermes_home, char_limits_from_home(hermes_home, cfg))
                for f in facts:
                    target = resolve_target(hermes_home, f.target)
                    # Ensure parent exists for path stability; do not create the memory file here.
                    proposals.append(Proposal(
                        label=f.category.lower(),  # person|event|preference|status
                        values=[f.summary],
                        file=str(target),
                        line_nos=[],  # append — no existing line
                        note=(f"user asserted {f.category}: {f.summary}"
                              + (f" (expires {f.expires})" if f.expires else "")
                              + (f" — «{f.quote}»" if f.quote else "")),
                        session_ids=[f.session_id] if f.session_id else None,
                        session_evidence=[f.evidence_row()],
                        kind="user_fact",
                        category=f.category,
                        action="append",
                        proposed_line=f.proposed_line,
                        confidence=f.confidence,
                        expires=f.expires,
                    ))
                user_fact_status = "ok"
                user_fact_detail = f"{len(msgs)} user msg(s), {len(facts)} new fact proposal(s)"
            except Exception as e:  # fail-soft: never abort the stack pass
                user_fact_status = "unavailable"
                user_fact_detail = f"{type(e).__name__}: {e}"
        # stash for summary
        _uf_meta = {"user_fact_status": user_fact_status, "user_fact_detail": user_fact_detail}

        overflow = max(0, len(proposals) - int(cfg["max_proposals_per_run"]))
        proposals = proposals[: int(cfg["max_proposals_per_run"])]
        costs = cost_result if cost_result is not None else session_costs(hermes_home, sd.isoformat(), cfg)
        day = rd.isoformat()
        stage = root / cfg.get("stage_subdir", "dream-proposals") / day
        stage.mkdir(parents=True, exist_ok=True)
        summary = {
            "date": day,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "model_used": False,
            "proposals": [_with_freshness(p) for p in proposals],
            "apply_enabled": bool(cfg.get("apply_enabled", False)),
            "session_costs": costs,
            "tier_markers": tiers,
            "review_ledger": "review.jsonl (append-only; decisions by a named actor only)",
            "scanned_files": len(files),
            "overflow": overflow,
            "session_date": sd.isoformat(),
            "session_status": sess.status,
            "session_detail": sess.detail,
            "user_fact_status": _uf_meta.get("user_fact_status", "disabled"),
            "user_fact_detail": _uf_meta.get("user_fact_detail", ""),
            "nosave": {
                "honored": honor_nosave,
                **nosave_idx.counts(),
                **({"error": nosave_error} if nosave_error else {}),
            },
            "suite": "Oneiros Dream Cycle",
            "tagline": "Your agent sleeps on it.",
        }
        # v0.4.1: copy prior reject/defer into today's ledger (never accept).
        carried = carry_prior_decisions(stage, summary["proposals"])
        summary["carried_decisions"] = [c["proposal_id"] for c in carried]
        summary["undecided"] = [p["proposal_id"] for p in _undecided(stage, summary["proposals"])]
        if honor_nosave:
            existing = {
                (r.get("message_sha256"), r.get("scope"))
                for r in read_reviews(stage)
                if r.get("decision") == "nosave-skip"
            }
            written = 0
            for row in nosave_idx.ledger_rows():
                key = (row.get("message_sha256"), row.get("scope"))
                if key in existing:
                    continue
                append_event(stage, row)
                existing.add(key)
                written += 1
            summary["nosave"]["ledger_rows_written"] = written
        (stage / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        lines = [f"# Dream Cycle proposals — {day}", "", "model_used: false", f"count: {len(proposals)}",
                 f"scanned_files: {len(files)}" + ("  ⚠ no memory files found — check memory_globs/roots" if not files else ""),
                 f"sessions ({sd.isoformat()}): {sess.status} — {sess.detail}",
                 f"user facts: {summary.get('user_fact_status')} — {summary.get('user_fact_detail')}",
                 format_nosave_status(summary.get("nosave")),
                 f"{format_costs(costs)}",
                 f"tier markers: MECH {len(tiers['MECH'])}, STATUS {len(tiers['STATUS'])}",
                 f"undecided: {len(summary['undecided'])} (review.jsonl)",
                 f"carried: {len(summary.get('carried_decisions') or [])} prior reject/defer", ""]
        for p in proposals:
            lines.append(
                f"- `{_pid(asdict(p))}` **{p.label}**: {', '.join(p.values)} ({p.file}:{','.join(map(str, p.line_nos))}) — {p.note}"
            )
        if not proposals:
            lines.append("(no candidates)")
        (stage / "diff.md").write_text("\n".join(lines) + "\n")
        return ProposeResult(proposals=proposals, stage_path=str(stage), scanned_files=len(files),
                             session_status=sess.status, session_detail=sess.detail)
    finally:
        release_lock(fd)
