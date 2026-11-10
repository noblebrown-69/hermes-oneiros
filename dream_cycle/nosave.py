"""Deterministic "do not save" detector for Dream Cycle (v0.8.0).

Scans role=user messages for an explicit don't-save / off-the-record directive
and returns skipped ranges. No model calls. No network. Never stores message
text in the status screen or the review ledger — hashes and reason codes only.

Scope:
  * phrase in the first real user message of a session, or a phrase that
    names "this whole conversation" (or equivalent) → skip the whole session
  * otherwise → skip the exchange (that user message plus following non-user
    turns until the next user message)

Quote / negation guards: quoted or fenced text, questions about the feature,
first-person declaratives ("I don't remember"), and log-looking lines do not
trigger.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .sessions import SessionResult, is_compaction_message

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_APOS = str.maketrans({
    "\u2019": "'", "\u2018": "'", "\u201B": "'",
    "\u2032": "'", "\u00B4": "'",
})

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`]*`")
_DQUOT = re.compile(r'"[^"]*"')
_TYPO_QUOT = re.compile(r"“[^”]*”")
_GUILLEMET = re.compile(r"«[^»]*»")
_DO_NOT = re.compile(r"(?i)\bdo not\b")

_LOG_LINE = re.compile(
    r"(?i)^\s*(?:"
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}"
    r"|\[?(?:DEBUG|INFO|WARN(?:ING)?|ERROR|TRACE|LOG|CRIT(?:ICAL)?)\]?"
    r"|\[\s*\d+\s*\]"
    r")\b"
)

_I_SUBJECT = re.compile(r"(?i)\bI(?:'ve|'d)?\s+$")

# Talking *about* the feature, not issuing a directive.
_META_BEFORE = re.compile(
    r"(?is)\b(?:"
    r"if I (?:say|said|type|typed|write|wrote|use|used)|"
    r"when I (?:say|said|type|write)|"
    r"what (?:does|do|if|happens|would happen)|"
    r"how (?:does|do|would|is|should)|"
    r"does (?:(?:the )?(?:bot|agent|plugin|detector|flag) )?"
    r"(?:honor|treat|skip|ignore|handle)|"
    r"the (?:phrase|words?|sentence|feature|detector|flag)|"
    r"meaning of|quoted|"
    r"you (?:mentioned|said|wrote)|"
    r"asking about|"
    r"(?:for )?example(?: of)?|"
    r"e\.g\.|"
    r"say(?:ing)? the words"
    r")\b.{0,120}$"
)

_WHOLE = re.compile(
    r"(?i)\b(?:this|the|our)\s+(?:whole|entire|full)\s+"
    r"(?:conversation|session|chat|thread|discussion|talk)\b"
    r"|\b(?:this|the)\s+conversation\s+as\s+a\s+whole\b"
    r"|\b(?:none|any)\s+of\s+this\s+conversation\b"
)

# reason -> pattern, applied to normalized + quote/code-stripped text
_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("dont_save", re.compile(r"(?i)\b(?:please\s+)?don't\s+(?:save|store)\b")),
    ("dont_log", re.compile(r"(?i)\b(?:please\s+)?don't\s+(?:log|record)\b")),
    ("dont_remember", re.compile(r"(?i)\b(?:please\s+)?don't\s+remember\b")),
    ("off_the_record", re.compile(r"(?i)\boff\s+the\s+record\b")),
    ("forget_i_said", re.compile(
        r"(?i)\bforget\s+(?:that\s+)?I\s+(?:said|told|mentioned|wrote)\b"
    )),
    ("keep_between_us", re.compile(r"(?i)\bkeep\s+this\s+between\s+us\b")),
    ("not_for_memory", re.compile(
        r"(?i)\bnot\s+for\s+(?:(?:your|the|bot|agent)\s+)?memory\b"
    )),
]

# Human labels for status (no message content).
REASON_LABEL = {
    "dont_save": "don't-save",
    "dont_log": "don't-log",
    "dont_remember": "don't-remember",
    "off_the_record": "off-the-record",
    "forget_i_said": "forget-I-said",
    "keep_between_us": "keep-between-us",
    "not_for_memory": "not-for-memory",
    "whole_conversation": "whole-conversation",
    "first_user_message": "first-user-message",
}


def normalize_text(text: str) -> str:
    t = (text or "").translate(_APOS)
    return _DO_NOT.sub("don't", t)


def strip_quoted_and_code(text: str) -> str:
    """Remove fenced/inline code and double/typographic quotes (not apostrophes)."""
    t = normalize_text(text)
    t = _FENCE.sub(" ", t)
    t = _INLINE.sub(" ", t)
    t = _DQUOT.sub(" ", t)
    t = _TYPO_QUOT.sub(" ", t)
    t = _GUILLEMET.sub(" ", t)
    return t


def _line_at(text: str, idx: int) -> str:
    lo = text.rfind("\n", 0, idx) + 1
    hi = text.find("\n", idx)
    return text[lo: hi if hi != -1 else len(text)]


def _sentence_at(text: str, start: int, end: int) -> str:
    lo = start
    while lo > 0 and text[lo - 1] not in ".\n!?":
        lo -= 1
    hi = end
    while hi < len(text) and text[hi] not in ".\n!?":
        hi += 1
    return text[lo:hi].strip()


def _remember_has_object_or_please(bare: str, start: int, end: int) -> bool:
    after = bare[end: end + 48]
    if re.match(r"(?i)\s*(?:this|that|it|any|what I)\b", after):
        return True
    before = bare[max(0, start - 16):start]
    if re.search(r"(?i)\bplease\s+$", before):
        return True
    sent = _sentence_at(bare, start, end)
    return bool(re.fullmatch(r"(?i)(?:please\s+)?don't remember[.!]?", sent))


def _is_meta(bare: str, start: int) -> bool:
    prefix = bare[max(0, start - 160):start]
    if _META_BEFORE.search(prefix):
        return True
    # Phrase used inside a short question about the feature.
    sent = _sentence_at(bare, start, start + 1)
    if sent.endswith("?") or (start < len(bare) and "?" in bare[start: start + 80]):
        if re.search(r"(?i)\b(?:what|how|does|do|if|when|should|would|meaning|phrase)\b", sent):
            return True
    return False


@dataclass(frozen=True)
class NoSaveHit:
    reason: str
    whole_conversation: bool = False


def detect_nosave(text: str) -> Optional[NoSaveHit]:
    """Return a hit when ``text`` issues a real don't-save directive."""
    if not text or not str(text).strip():
        return None
    bare = strip_quoted_and_code(text)
    if not bare.strip():
        return None
    whole = bool(_WHOLE.search(bare))
    best: Optional[NoSaveHit] = None
    for reason, pat in _PATTERNS:
        for m in pat.finditer(bare):
            if _LOG_LINE.search(_line_at(bare, m.start())):
                continue
            if _I_SUBJECT.search(bare[max(0, m.start() - 20):m.start()]):
                continue
            if _is_meta(bare, m.start()):
                continue
            if reason == "dont_remember" and not _remember_has_object_or_please(
                bare, m.start(), m.end()
            ):
                continue
            best = NoSaveHit(reason=reason, whole_conversation=whole)
            break
        if best:
            break
    return best


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

@dataclass
class SkipRecord:
    scope: str  # "session" | "exchange"
    reason: str
    tags: List[str]
    session_id_sha256: str
    message_sha256: str


@dataclass
class NoSaveIndex:
    session_ids: Set[str] = field(default_factory=set)
    message_keys: Set[Tuple[str, Any]] = field(default_factory=set)
    message_hashes: Set[str] = field(default_factory=set)
    skips: List[SkipRecord] = field(default_factory=list)

    def is_skipped(
        self,
        session_id: Optional[str] = None,
        message_id: Optional[Any] = None,
        line_sha256: Optional[str] = None,
    ) -> bool:
        if session_id and session_id in self.session_ids:
            return True
        if session_id is not None and message_id is not None:
            if (str(session_id), message_id) in self.message_keys:
                return True
        if line_sha256 and line_sha256 in self.message_hashes:
            return True
        return False

    def filter_messages(self, messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for m in messages:
            if self.is_skipped(
                m.get("session_id"),
                m.get("message_id"),
                m.get("line_sha256"),
            ):
                continue
            out.append(m)
        return out

    def counts(self) -> Dict[str, Any]:
        sess = [s for s in self.skips if s.scope == "session"]
        exch = [s for s in self.skips if s.scope == "exchange"]
        reasons: Dict[str, int] = {}
        for s in self.skips:
            reasons[s.reason] = reasons.get(s.reason, 0) + 1
            for t in s.tags:
                if t != s.reason:
                    reasons[t] = reasons.get(t, 0) + 1
        return {
            "sessions_skipped": len(sess),
            "exchanges_skipped": len(exch),
            "reasons": reasons,
        }

    def ledger_rows(self, *, ts: Optional[str] = None) -> List[Dict[str, Any]]:
        """Append-only review rows: hashes and reason codes, no message text."""
        rows = []
        when = ts or datetime.now().astimezone().isoformat(timespec="seconds")
        for s in self.skips:
            key = f"nosave|{s.scope}|{s.session_id_sha256}|{s.message_sha256}"
            pid = "nosave-" + hashlib.sha1(key.encode()).hexdigest()[:12]
            rows.append({
                "decision": "nosave-skip",
                "proposal_id": pid,
                "actor": "dream-cycle",
                "ts": when,
                "scope": s.scope,
                "reason": s.reason,
                "tags": list(s.tags),
                "session_id_sha256": s.session_id_sha256,
                "message_sha256": s.message_sha256,
                "note": "",
            })
        return rows


def empty_index() -> NoSaveIndex:
    return NoSaveIndex()


def _sid_hash(session_id: Optional[str]) -> str:
    return hashlib.sha256(str(session_id or "").encode("utf-8")).hexdigest()


def _msg_hash(msg: Dict[str, Any]) -> str:
    if msg.get("line_sha256"):
        return str(msg["line_sha256"])
    content = msg.get("content") or ""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def _sort_key(m: Dict[str, Any]) -> Tuple:
    ts = m.get("timestamp") or 0
    try:
        ts_f = float(ts)
    except (TypeError, ValueError):
        ts_f = 0.0
    mid = m.get("message_id") or 0
    try:
        mid_k: Any = int(mid)
    except (TypeError, ValueError):
        mid_k = str(mid)
    return (ts_f, mid_k)


def _is_real_user(msg: Dict[str, Any]) -> bool:
    if (msg.get("role") or "") != "user":
        return False
    content = msg.get("content") or ""
    if not content.strip():
        return False
    if is_compaction_message(content):
        return False
    try:
        from .user_facts import _is_skippable_user_text
        if _is_skippable_user_text(content):
            return False
    except Exception:
        pass
    return True


def _mark_message(idx: NoSaveIndex, msg: Dict[str, Any]) -> None:
    sid = msg.get("session_id")
    mid = msg.get("message_id")
    if sid is not None and mid is not None:
        idx.message_keys.add((str(sid), mid))
    idx.message_hashes.add(_msg_hash(msg))


def scan_messages(messages: Sequence[Dict[str, Any]]) -> NoSaveIndex:
    """Build a skip index from a conversation (any roles; only user text triggers)."""
    idx = NoSaveIndex()
    by_sid: Dict[str, List[Dict[str, Any]]] = {}
    for m in messages:
        sid = str(m.get("session_id") or "")
        by_sid.setdefault(sid, []).append(m)
    for sid, group in by_sid.items():
        ordered = sorted(group, key=_sort_key)
        real_users = [m for m in ordered if _is_real_user(m)]
        first_user = real_users[0] if real_users else None
        # Collect session-level vs exchange-level hits first so a later
        # "this whole conversation" still suppresses earlier facts.
        session_hit: Optional[Tuple[Dict[str, Any], NoSaveHit, List[str]]] = None
        exchange_hits: List[Tuple[Dict[str, Any], NoSaveHit]] = []
        for um in real_users:
            hit = detect_nosave(um.get("content") or "")
            if not hit:
                continue
            tags = [hit.reason]
            is_first = um is first_user
            if hit.whole_conversation:
                tags.append("whole_conversation")
            if is_first:
                tags.append("first_user_message")
            if hit.whole_conversation or is_first:
                session_hit = (um, hit, tags)
                break
            exchange_hits.append((um, hit))
        if session_hit:
            um, hit, tags = session_hit
            idx.session_ids.add(sid)
            for m in ordered:
                _mark_message(idx, m)
            idx.skips.append(SkipRecord(
                scope="session",
                reason=hit.reason,
                tags=tags,
                session_id_sha256=_sid_hash(sid),
                message_sha256=_msg_hash(um),
            ))
            continue
        for um, hit in exchange_hits:
            # user message + following non-user turns
            try:
                pos = ordered.index(um)
            except ValueError:
                pos = -1
            _mark_message(idx, um)
            if pos >= 0:
                for nxt in ordered[pos + 1:]:
                    if (nxt.get("role") or "") == "user":
                        break
                    _mark_message(idx, nxt)
            idx.skips.append(SkipRecord(
                scope="exchange",
                reason=hit.reason,
                tags=[hit.reason],
                session_id_sha256=_sid_hash(sid),
                message_sha256=_msg_hash(um),
            ))
    return idx


def apply_to_session(idx: NoSaveIndex, sess: SessionResult) -> SessionResult:
    """Drop skipped lines/evidence from a SessionResult (roles honored when present)."""
    if not sess.lines and not sess.evidence:
        return sess
    ev = list(sess.evidence or [])
    if ev and len(ev) == len(sess.lines):
        keep_l, keep_e = [], []
        for line, row in zip(sess.lines, ev):
            if idx.is_skipped(row.get("session_id"), row.get("message_id"), row.get("line_sha256")):
                continue
            # Command-less fallback: also honor a directive in the line itself
            # when the index has no id (shouldn't happen for state.db).
            keep_l.append(line)
            keep_e.append(row)
        dropped = len(sess.lines) - len(keep_l)
        if not dropped:
            return sess
        detail = sess.detail or ""
        extra = f"nosave skipped {dropped} line(s)"
        detail = f"{detail}; {extra}" if detail else extra
        status = sess.status if keep_l else "no-sessions"
        return SessionResult(status, lines=keep_l, detail=detail, evidence=keep_e)
    # No parallel evidence (legacy command source): drop a triggering line + the next.
    if not sess.lines:
        return sess
    keep: List[str] = []
    skip_next = False
    dropped = 0
    for line in sess.lines:
        if skip_next:
            skip_next = False
            dropped += 1
            continue
        if detect_nosave(line):
            skip_next = True
            dropped += 1
            continue
        keep.append(line)
    if not dropped:
        return sess
    detail = sess.detail or ""
    extra = f"nosave skipped {dropped} line(s)"
    detail = f"{detail}; {extra}" if detail else extra
    status = sess.status if keep else "no-sessions"
    return SessionResult(status, lines=keep, detail=detail, evidence=list(ev))


def fetch_conversation_messages(
    hermes_home: Path,
    *,
    lookback_days: int = 30,
    end_day: Optional[date] = None,
    max_rows: int = 16000,
) -> List[Dict[str, Any]]:
    """Read-only user/assistant/tool messages from state.db (fail-soft)."""
    import sqlite3

    db = Path(hermes_home) / "state.db"
    if not db.is_file():
        return []
    end = end_day or date.today()
    hi = datetime.combine(end, time(23, 59, 59)).timestamp()
    lo = datetime.combine(end - timedelta(days=max(0, lookback_days - 1)), time(0, 0)).timestamp()
    uri = f"file:{db}?mode=ro"
    con = None
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
        except sqlite3.Error:
            pass
        rows = con.execute(
            "SELECT m.id, m.session_id, m.role, m.content, m.timestamp FROM messages m "
            "WHERE m.timestamp >= ? AND m.timestamp <= ? "
            "AND m.role IN ('user','assistant','tool') "
            "AND m.content IS NOT NULL AND TRIM(m.content) != '' "
            "ORDER BY m.timestamp ASC, m.id ASC LIMIT ?",
            (lo, hi, int(max_rows)),
        ).fetchall()
    except (sqlite3.Error, OSError):
        return []
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    out: List[Dict[str, Any]] = []
    for mid, sid, role, content, ts in rows:
        content = content or ""
        out.append({
            "message_id": mid,
            "session_id": sid,
            "role": role,
            "content": content,
            "timestamp": ts,
            "line_sha256": hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest(),
        })
    return out


def format_nosave_status(info: Optional[Dict[str, Any]]) -> str:
    """One status line: counts and reasons, never message text."""
    if not info:
        return "nosave: n/a"
    if info.get("honored") is False:
        return "nosave: off (honor_nosave=false)"
    n_s = int(info.get("sessions_skipped") or 0)
    n_e = int(info.get("exchanges_skipped") or 0)
    reasons = info.get("reasons") or {}
    labels = []
    for key, n in reasons.items():
        lab = REASON_LABEL.get(key, key)
        labels.append(f"{lab}×{n}" if n > 1 else lab)
    reason_bit = f" ({', '.join(labels)})" if labels else ""
    return (
        f"nosave: {n_s} session(s), {n_e} exchange(s) skipped{reason_bit}"
    )


def lookback_union(cfg: Dict[str, Any]) -> int:
    """Wider of user-fact and session-corroboration lookbacks (at least 1)."""
    lb = 1
    uf = cfg.get("user_facts") or {}
    if uf.get("enabled", True):
        try:
            lb = max(lb, int(uf.get("lookback_days") or 30))
        except (TypeError, ValueError):
            lb = max(lb, 30)
    sc = cfg.get("session_corroboration") or {}
    if sc.get("enabled"):
        try:
            lb = max(lb, int(sc.get("lookback_days") or 3))
        except (TypeError, ValueError):
            pass
    return lb
