"""Read-only session corroboration for Dream Cycle.

Dream Cycle can compare memory against what the day's sessions actually said.
The session source is any read-only command that prints JSON. Two shapes are
accepted for the day payload:

    {"daily_log": {"work_items": [{"kind": "...", "text": "..."}]}}
    {"lines": ["...", "..."]}

Quiet days are normal. Many day-log APIs answer 404 ("day not found") when a
profile simply had no sessions on that date, so Dream Cycle checks the day
index first (``days_command``) and treats a missing date — or a "not found"
error from ``day_command`` — as status ``no-sessions``, not as a failure.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

NOT_FOUND_MARKERS = ("not found", "404")


@dataclass
class SessionResult:
    status: str  # disabled | ok | no-sessions | unavailable
    lines: List[str] = field(default_factory=list)
    detail: str = ""
    # Per-line evidence for propose (state.db). Never required for command source.
    evidence: List[Dict[str, Any]] = field(default_factory=list)


def _fill(argv: List[str], day: str) -> List[str]:
    return [a.replace("{date}", day) for a in argv]


def _run(argv: List[str], timeout: int):
    return subprocess.run(argv, capture_output=True, timeout=timeout)


def parse_days(payload: Any) -> set:
    """Accept {"daily_logs":[{"date":..}]}, {"dates":[..]} or a bare list."""
    if isinstance(payload, dict):
        if isinstance(payload.get("dates"), list):
            return {str(d) for d in payload["dates"]}
        payload = payload.get("daily_logs", [])
    out = set()
    for item in payload or []:
        if isinstance(item, str):
            out.add(item)
        elif isinstance(item, dict) and item.get("date"):
            out.add(str(item["date"]))
    return out


def parse_day(payload: Any) -> List[str]:
    if isinstance(payload, dict) and isinstance(payload.get("lines"), list):
        return [str(x) for x in payload["lines"] if str(x).strip()]
    dl = payload.get("daily_log", {}) if isinstance(payload, dict) else {}
    lines = []
    for wi in dl.get("work_items", []) or []:
        if isinstance(wi, dict) and wi.get("kind") != "tool" and wi.get("text"):
            lines.append(str(wi["text"]))
    return lines




def _snippet(content: str, limit: int = 120) -> str:
    s = " ".join((content or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


# Compaction / context-summary handoffs Hermes injects as role=user. Not live conversation.
_COMPACTION_MARKERS = (
    "[CONTEXT COMPACTION",
    "[PRIOR CONTEXT",
    "[END OF PRIOR CONTEXT",
    "COMPACTION SUMMARY",
    "demoted at compaction",
    "Earlier turns were compacted",
)


def is_compaction_message(content: str) -> bool:
    """True when message text is a context-compaction / summary handoff block."""
    head = (content or "")[:800]
    return any(m in head for m in _COMPACTION_MARKERS)


def _evidence_roles(cfg) -> list:
    """Roles scanned for corroboration after applying ignore_tool_messages."""
    sc = (cfg or {}).get("session_corroboration") or {}
    roles = list((cfg or {}).get("grounding_roles") or ["user", "assistant"])
    if sc.get("ignore_tool_messages", True):
        roles = [r for r in roles if r != "tool"]
    return [r for r in roles if r]


def _keep_message(role: str, content: str, cfg) -> bool:
    """Whether a state.db message may count as corroboration evidence."""
    sc = (cfg or {}).get("session_corroboration") or {}
    if sc.get("ignore_tool_messages", True) and role == "tool":
        return False
    if sc.get("ignore_compaction_messages", True) and is_compaction_message(content or ""):
        return False
    return True


def fetch_session_lines_db(

    hermes_home,
    day: str,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    lookback_days: int = 1,
) -> SessionResult:
    """Read conversation message text for ``day`` (and optional lookback) from state.db.

    By default skips ``role=tool`` and compaction/summary handoffs (v0.6.1).
    Opened ``mode=ro`` + ``PRAGMA query_only`` — never writes, never migrates.
    """
    import hashlib
    from pathlib import Path as _P
    from datetime import datetime as _dt, timedelta as _td

    sc = (cfg or {}).get("session_corroboration") or {}
    costs = (cfg or {}).get("session_costs") or {}
    db = _P(costs.get("db") or (_P(hermes_home) / "state.db"))
    roles = _evidence_roles(cfg)
    if not roles:
        return SessionResult("unavailable", detail="no grounding roles after filters")
    if not db.is_file():
        return SessionResult("unavailable", detail="state.db not found")
    try:
        end = _dt.strptime(day, "%Y-%m-%d").astimezone() + _td(days=1)
        start = end - _td(days=max(1, int(lookback_days)))
        lo, hi = start.timestamp(), end.timestamp()
    except ValueError:
        return SessionResult("unavailable", detail=f"bad day {day!r}")
    try:
        con = _ro_connect(db)
        try:
            q = (
                "SELECT m.id, m.session_id, m.role, m.content, m.timestamp FROM messages m "
                "WHERE m.timestamp >= ? AND m.timestamp < ? "
                f"AND m.role IN ({','.join('?' * len(roles))}) "
                "AND m.content IS NOT NULL AND TRIM(m.content) != '' "
                "ORDER BY m.timestamp ASC LIMIT ?"
            )
            max_rows = int(sc.get("max_rows") or 8000)
            rows = con.execute(q, [lo, hi, *roles, max_rows]).fetchall()
        finally:
            con.close()
    except Exception as e:
        return SessionResult("unavailable", detail=f"state.db {e.__class__.__name__}")
    lines, evidence = [], []
    skipped = 0
    for mid, sid, role, content, _ts in rows:
        content = content or ""
        if not _keep_message(role, content, cfg):
            skipped += 1
            continue
        lines.append(content)
        evidence.append({
            "message_id": mid,
            "session_id": sid,
            "role": role,
            "line_sha256": hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest(),
            "snippet": _snippet(content),
        })
    if not lines:
        detail = f"no conversation messages on {day} (lookback {lookback_days}"
        if skipped:
            detail += f"; skipped {skipped} tool/compaction"
        detail += ")"
        return SessionResult("no-sessions", detail=detail)
    detail = f"{len(lines)} state.db message(s) over {lookback_days}d"
    if skipped:
        detail += f" (skipped {skipped} tool/compaction)"
    return SessionResult("ok", lines=lines, detail=detail, evidence=evidence)


def fetch_session_lines(day: str, cfg: Optional[Dict[str, Any]] = None, hermes_home=None) -> SessionResult:
    """Session text for propose. Prefer read-only state.db; optional command fallback.

    ``session_corroboration.source``:
      - ``state.db`` (default when enabled): query messages in the profile's state.db
      - ``command``: legacy days_command / day_command argv path (memory-wiki-read etc.)
    """
    sc = (cfg or {}).get("session_corroboration") or {}
    if not sc.get("enabled"):
        return SessionResult("disabled", detail="session_corroboration.enabled is false")
    source = (sc.get("source") or "state.db").strip()
    lookback = int(sc.get("lookback_days") or 1)
    if source == "state.db":
        if hermes_home is None:
            return SessionResult("unavailable", detail="state.db source needs hermes_home")
        return fetch_session_lines_db(hermes_home, day, cfg, lookback_days=lookback)
    # --- legacy command source ---
    timeout = int(sc.get("timeout_s") or 30)
    try:
        if sc.get("days_command"):
            r = _run(_fill(sc["days_command"], day), timeout)
            if r.returncode != 0:
                return SessionResult("unavailable", detail=f"days_command exit {r.returncode}")
            if day not in parse_days(json.loads(r.stdout or b"{}")):
                return SessionResult("no-sessions", detail=f"no sessions recorded on {day}")
        if not sc.get("day_command"):
            return SessionResult("unavailable", detail="command source needs day_command")
        r = _run(_fill(sc["day_command"], day), timeout)
        if r.returncode != 0:
            err = (r.stderr or b"").decode(errors="replace").lower()
            if any(m in err for m in NOT_FOUND_MARKERS):
                return SessionResult("no-sessions", detail=f"day log for {day} not found (quiet day)")
            return SessionResult("unavailable", detail=f"day_command exit {r.returncode}")
        lines = parse_day(json.loads(r.stdout or b"{}"))
        return SessionResult("ok", lines=lines, detail=f"{len(lines)} session lines")
    except subprocess.TimeoutExpired:
        return SessionResult("unavailable", detail=f"session command timed out after {timeout}s")
    except (OSError, ValueError) as e:
        return SessionResult("unavailable", detail=e.__class__.__name__)




# --- Session cost evidence (Exo-derived, 2026-11-06) -----------------------
# Read-only view of the profile's Hermes state.db so Dream Cycle can see what
# a day cost: tokens, cache reuse, tool/API calls. Never writes, never invents
# USD (local models report 0 -> omitted), fails soft when the DB is missing.

_TOK = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


def _ro_connect(db_path):
    import sqlite3
    from pathlib import Path as _P
    uri = _P(db_path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2)
    con.execute("PRAGMA query_only = ON")
    return con


def _day_bounds(day: str):
    from datetime import datetime as _dt, timedelta as _td
    start = _dt.strptime(day, "%Y-%m-%d").astimezone()  # local midnight
    return start.timestamp(), (start + _td(days=1)).timestamp()


def session_costs(hermes_home, day: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from pathlib import Path as _P
    sc = (cfg or {}).get("session_costs") or {}
    if sc.get("enabled") is False:
        return {"status": "disabled"}
    db = _P(sc.get("db") or (_P(hermes_home) / "state.db"))
    if not db.is_file():
        return {"status": "unavailable", "detail": "state.db not found"}
    lo, hi = _day_bounds(day)
    try:
        con = _ro_connect(db)
        try:
            cols = ", ".join(f"COALESCE(SUM({c}),0)" for c in _TOK)
            row = con.execute(
                f"SELECT COUNT(*), {cols}, COALESCE(SUM(tool_call_count),0), COALESCE(SUM(api_call_count),0),"
                " COALESCE(SUM(estimated_cost_usd),0), COALESCE(SUM(actual_cost_usd),0),"
                " COALESCE(SUM(message_count),0)"
                " FROM sessions WHERE started_at >= ? AND started_at < ?", (lo, hi)).fetchone()
            by_model = []
            try:
                for r in con.execute(
                        f"SELECT u.model, COUNT(DISTINCT u.session_id), {', '.join(f'COALESCE(SUM(u.{c}),0)' for c in _TOK)},"
                        " COALESCE(SUM(u.api_call_count),0), COALESCE(SUM(u.estimated_cost_usd),0),"
                        " COALESCE(SUM(u.actual_cost_usd),0)"
                        " FROM session_model_usage u JOIN sessions s ON s.id = u.session_id"
                        " WHERE s.started_at >= ? AND s.started_at < ? GROUP BY u.model ORDER BY 3 DESC",
                        (lo, hi)):
                    m = {"model": r[0], "sessions": r[1], **dict(zip(_TOK, r[2:7])), "api_call_count": r[7]}
                    _usd(m, r[8], r[9])
                    by_model.append(m)
            except Exception:  # older DBs without session_model_usage
                pass
        finally:
            con.close()
    except Exception as e:
        return {"status": "unavailable", "detail": e.__class__.__name__}
    n = row[0]
    if not n:
        return {"status": "no-sessions", "sessions": 0}
    out: Dict[str, Any] = {"status": "ok", "sessions": n, **dict(zip(_TOK, row[1:6])),
                           "tool_call_count": row[6], "api_call_count": row[7], "message_count": row[10]}
    denom = out["input_tokens"] + out["cache_read_tokens"]
    if out["input_tokens"] > 0:
        out["cache_read_share"] = round(out["cache_read_tokens"] / denom, 4)
    if out["api_call_count"]:
        out["tool_calls_per_api_call"] = round(out["tool_call_count"] / out["api_call_count"], 3)
    _usd(out, row[8], row[9])
    out["by_model"] = by_model
    return out


def _usd(d: Dict[str, Any], est, act) -> None:
    if est:
        d["estimated_cost_usd"] = round(float(est), 6)
    if act:
        d["actual_cost_usd"] = round(float(act), 6)


def format_costs(c: Dict[str, Any]) -> str:
    if c.get("status") != "ok":
        return f"session costs: {c.get('status')}" + (f" — {c['detail']}" if c.get("detail") else "")
    s = (f"session costs: {c['sessions']} session(s); in {c['input_tokens']:,} / out {c['output_tokens']:,} / "
         f"cache_read {c['cache_read_tokens']:,} / cache_write {c['cache_write_tokens']:,} / "
         f"reasoning {c['reasoning_tokens']:,} tok; tools {c['tool_call_count']} / api {c['api_call_count']}")
    if "cache_read_share" in c:
        s += f"; cache_read share {c['cache_read_share']:.1%}"
    usd = [f"{k.split('_')[0]} ${c[k]:.4f}" for k in ("estimated_cost_usd", "actual_cost_usd") if k in c]
    s += ("; " + ", ".join(usd)) if usd else "; USD: not reported (local model)"
    return s


# --- Evidence grounding (quote-grounding idea, 2026-11-08) ----------
# At apply time: does the value we are about to keep actually appear in the
# day's real sessions? Read-only state.db. We record only hashes of matching
# lines, never their text, so a receipt can never carry a secret.

def ground_value(hermes_home, label: Dict[str, Any], keep: str, days: List[str],
                 cfg: Optional[Dict[str, Any]] = None, session_ids: Optional[List[str]] = None,
                 max_matches: int = 5, max_rows: int = 5000) -> Dict[str, Any]:
    import hashlib
    from pathlib import Path as _P
    from .propose import line_value_spans  # local import: propose imports this module
    sc = (cfg or {}).get("session_costs") or {}
    db = _P(sc.get("db") or (_P(hermes_home) / "state.db"))
    roles = _evidence_roles(cfg)
    out: Dict[str, Any] = {"evidence_status": "unverified", "days": sorted(set(days)), "roles": roles,
                           "session_ids_cited": list(session_ids or []), "matches": [], "rows_scanned": 0}
    if not db.is_file():
        out["detail"] = "state.db not found"
        return out
    clauses, params = [], []
    for d in sorted(set(days)):
        try:
            lo, hi = _day_bounds(d)
        except ValueError:
            continue
        clauses.append("(m.timestamp >= ? AND m.timestamp < ?)"); params += [lo, hi]
    if session_ids:
        clauses.append(f"m.session_id IN ({','.join('?' * len(session_ids))})"); params += list(session_ids)
    if not clauses:
        out["detail"] = "no valid days or session ids"
        return out
    q = (f"SELECT m.id, m.session_id, m.role, m.content, m.timestamp FROM messages m "
         f"WHERE ({' OR '.join(clauses)}) AND m.role IN ({','.join('?' * len(roles))}) "
         f"ORDER BY m.timestamp LIMIT {int(max_rows)}")
    try:
        con = _ro_connect(db)
        try:
            rows = con.execute(q, params + roles).fetchall()
        finally:
            con.close()
    except Exception as e:
        out["detail"] = e.__class__.__name__
        return out
    out["rows_scanned"] = len(rows)
    for mid, sid, role, content, ts in rows:
        content = str(content or "")
        if not _keep_message(role, content, cfg):
            continue
        sc = (cfg or {}).get("session_corroboration") or {}
        require_user = sc.get("require_user_evidence", True)
        if require_user and role not in (None, "user"):
            continue  # assistant/tool matches are weaker / already filtered; need user
        for ln in content.splitlines():
            if any(v[0] == keep for v in line_value_spans(ln, label)):
                out["matches"].append({"session_id": sid, "message_id": mid, "role": role, "ts": ts,
                                       "line_sha256": hashlib.sha256(ln.encode("utf-8")).hexdigest()})
                break
        if len(out["matches"]) >= max_matches:
            break
    if out["matches"]:
        out["evidence_status"] = "grounded"
        out["detail"] = f"{len(out['matches'])} message(s) carry {keep!r} as a {label.get('name')} value"
    else:
        out["detail"] = f"{keep!r} not found as a {label.get('name')} value in {len(rows)} message(s)"
    return out
