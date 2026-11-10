"""Deterministic user-fact extractor for Dream Cycle (v0.7.3).

Reads ONLY role=user conversation text. Never mines assistant or tool output.
No model calls. No network. Proposals are staged locally like stack proposals.

Categories:
  PERSON   — name + relationship or role (→ USER.md)
  EVENT    — dated trip / show / interview (→ MEMORY.md as STATUS: with expiry)
  PREFERENCE — durable preference (→ USER.md)
  STATUS   — job / location / job-search change (→ USER.md)

Assertion gate: first-person, explicit ``X is my/our …``, or ``X is a <role>``
when X is a capitalized proper name. Bare name mentions, questions
(``who is X?``), hypotheticals, quoted third-party text, compaction handoffs,
discussion/cron system prompts are skipped.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .markers import is_superseded_line, line_tier
from .sessions import _snippet, is_compaction_message

CATEGORIES = ("PERSON", "EVENT", "PREFERENCE", "STATUS")

# System / injected user-role wrappers that are not the human speaking.
_SKIP_PREFIX = re.compile(
    r"(?is)^\s*(?:"
    r"\[CONTEXT COMPACTION\b|"
    r"\[Discussion:\s*|"
    r"\[IMPORTANT:\s*You are running as a scheduled cron|"
    r"###\s*Task:\s*|"
    r"You are [A-Z@]|"
    r"Message from\s|"
    r"@file:"
    r")"
)

_QUESTION_LEAD = re.compile(
    r"(?is)^\s*(?:do not save[^.]*\.\s*)?"
    r"(?:who|what|when|where|how|why|do you|did you|can you|could you|would you|"
    r"tell me|give me|any ideas|ideas for)\b"
)

_HYPOTHETICAL = re.compile(
    r"(?i)\b(?:if I (?:were|was)|suppose I|imagine I|hypothetically|what if I)\b"
)

# ---- date helpers ----------------------------------------------------------

_MONTH = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MDY = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\b")
_MON_DAY = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s+(\d{1,2})(?:(?:st|nd|rd|th))?(?:,?\s*(20\d{2}))?\b",
    re.I,
)
_RELATIVE = re.compile(
    r"(?i)\b(?:in\s+a\s+week(?:\s+or\s+so)?|next\s+week|in\s+(\d+)\s+days?|"
    r"tomorrow|today|in\s+two\s+weeks)\b"
)

# Nov 19–22, 2026  /  Nov 19-22  /  November 19 to 22, 2026
_RANGE_MON = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s+(\d{1,2})(?:(?:st|nd|rd|th))?"
    r"\s*(?:[–—-]|to)\s*(\d{1,2})(?:(?:st|nd|rd|th))?"
    r"(?:,?\s*(20\d{2}))?\b",
    re.I,
)
# Thu 11/19 to Sun 11/22   /  11/19-11/22  /  11/19 through 11/22
_RANGE_MDY = re.compile(
    r"\b(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+)?"
    r"(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\s*"
    r"(?:[–—-]|to|through)\s*"
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+)?"
    r"(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\b",
    re.I,
)
_THROUGH_MDY = re.compile(
    r"(?i)\bthrough\s+(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\b"
)
_THROUGH_MON = re.compile(
    r"(?i)\bthrough\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2})(?:(?:st|nd|rd|th))?"
    r"(?:,?\s*(20\d{2}))?\b"
)

# Dates that are document metadata, not the event itself.
_META_BEFORE = re.compile(
    r"(?i)(?:prepared|updated|edited|last\s+edited|data\s+through|as\s+of|"
    r"sources:|logged|photographed|refreshed)\s+$"
)

_WEEKDAY = {
    "mon", "monday", "tue", "tues", "tuesday", "wed", "wednesday",
    "thu", "thur", "thurs", "thursday", "fri", "friday",
    "sat", "saturday", "sun", "sunday",
}


def _safe_date(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _month_num(token: str) -> Optional[int]:
    key = re.sub(r"[^a-z]", "", token.lower())
    for k, v in sorted(_MONTH.items(), key=lambda kv: -len(kv[0])):
        if key == k or key.startswith(k):
            return v
    return None


def _apply_year(dt: Optional[date], year_explicit: bool, ref: date) -> Optional[date]:
    if dt is None:
        return None
    if dt < ref - timedelta(days=180) and not year_explicit:
        return _safe_date(dt.year + 1, dt.month, dt.day)
    return dt


def normalize_date(text: str, ref: date) -> Optional[date]:
    """Pick the strongest date mention in ``text``, relative to ``ref``."""
    m = _ISO.search(text)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _MON_DAY.search(text)
    if m:
        mon = _month_num(m.group(1))
        if mon is None:
            return None
        day_n = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else ref.year
        return _apply_year(_safe_date(year, mon, day_n), bool(m.group(3)), ref)
    m = _MDY.search(text)
    if m:
        return _parse_mdy_parts(
            int(m.group(1)), int(m.group(2)),
            int(m.group(3)) if m.group(3) else ref.year,
            bool(m.group(3)), ref,
        )
    m = _RELATIVE.search(text)
    if m:
        raw = m.group(0).lower()
        if "tomorrow" in raw:
            return ref + timedelta(days=1)
        if "today" in raw:
            return ref
        if "two weeks" in raw:
            return ref + timedelta(days=14)
        if "next week" in raw or "a week" in raw:
            return ref + timedelta(days=7)
        if m.group(1):
            return ref + timedelta(days=int(m.group(1)))
    return None


def _parse_mdy_parts(a: int, b: int, year: int, year_explicit: bool, ref: date) -> Optional[date]:
    # US month/day only. Do not treat ratios like 24/7 as July 24.
    if 1 <= a <= 12 and 1 <= b <= 31:
        return _apply_year(_safe_date(year, a, b), year_explicit, ref)
    return None


def _is_meta_date(text: str, start: int) -> bool:
    prefix = text[max(0, start - 48):start]
    return bool(_META_BEFORE.search(prefix))


def iter_date_ranges(text: str, ref: date) -> List[Tuple[date, date, int, int]]:
    """Return (start_date, end_date, span_start, span_end) for stated stays/ranges."""
    out: List[Tuple[date, date, int, int]] = []
    for m in _RANGE_MON.finditer(text):
        if _is_meta_date(text, m.start()):
            continue
        mon = _month_num(m.group(1))
        if mon is None:
            continue
        year = int(m.group(4)) if m.group(4) else ref.year
        start = _apply_year(_safe_date(year, mon, int(m.group(2))), bool(m.group(4)), ref)
        end = _apply_year(_safe_date(year, mon, int(m.group(3))), bool(m.group(4)), ref)
        if start and end and end >= start:
            out.append((start, end, m.start(), m.end()))
    for m in _RANGE_MDY.finditer(text):
        if _is_meta_date(text, m.start()):
            continue
        y1 = int(m.group(3)) if m.group(3) else ref.year
        y2 = int(m.group(6)) if m.group(6) else y1
        start = _parse_mdy_parts(int(m.group(1)), int(m.group(2)), y1, bool(m.group(3)), ref)
        end = _parse_mdy_parts(int(m.group(4)), int(m.group(5)), y2, bool(m.group(6)), ref)
        if start and end and end >= start:
            out.append((start, end, m.start(), m.end()))
    return out


def parse_date_range(text: str, ref: date) -> Optional[Tuple[date, date]]:
    """Best stay range in ``text`` (explicit end date), or None."""
    ranges = iter_date_ranges(text, ref)
    if ranges:
        # prefer the longest stay, then the later end
        ranges.sort(key=lambda r: ((r[1] - r[0]).days, r[1]), reverse=True)
        return ranges[0][0], ranges[0][1]
    # "through 11/22" / "through Nov 22" with a separate start date
    end: Optional[date] = None
    m = _THROUGH_MDY.search(text)
    if m and not _is_meta_date(text, m.start()):
        end = _parse_mdy_parts(
            int(m.group(1)), int(m.group(2)),
            int(m.group(3)) if m.group(3) else ref.year,
            bool(m.group(3)), ref,
        )
    if end is None:
        m = _THROUGH_MON.search(text)
        if m and not _is_meta_date(text, m.start()):
            mon = _month_num(m.group(1))
            if mon:
                year = int(m.group(3)) if m.group(3) else ref.year
                end = _apply_year(_safe_date(year, mon, int(m.group(2))), bool(m.group(3)), ref)
    if end is None:
        return None
    start = None
    # pick an explicit start that is not the through-date itself and precedes end
    for hit in iter_explicit_dates(text, ref):
        if hit[0] <= end and (hit[1], hit[2]) != (m.start(), m.end()):
            start = hit[0]
            break
    if start is None:
        return None
    return start, end


def iter_explicit_dates(text: str, ref: date) -> List[Tuple[date, int, int]]:
    """All absolute dates in appearance order, excluding range interiors."""
    covered = []
    for _s, _e, a, b in iter_date_ranges(text, ref):
        covered.append((a, b))
        # still expose the start of the range as an explicit date
    hits: List[Tuple[date, int, int]] = []

    def _overlaps_interior(start: int, end: int) -> bool:
        for a, b in covered:
            if start >= a and end <= b:
                return True
            if start > a and start < b:
                return True
        return False

    for m in _ISO.finditer(text):
        dt = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if dt and not _overlaps_interior(m.start(), m.end()):
            hits.append((dt, m.start(), m.end()))
    for m in _MON_DAY.finditer(text):
        mon = _month_num(m.group(1))
        if mon is None:
            continue
        year = int(m.group(3)) if m.group(3) else ref.year
        dt = _apply_year(_safe_date(year, mon, int(m.group(2))), bool(m.group(3)), ref)
        if dt and not _overlaps_interior(m.start(), m.end()):
            hits.append((dt, m.start(), m.end()))
    for m in _MDY.finditer(text):
        dt = _parse_mdy_parts(
            int(m.group(1)), int(m.group(2)),
            int(m.group(3)) if m.group(3) else ref.year,
            bool(m.group(3)), ref,
        )
        if dt and not _overlaps_interior(m.start(), m.end()):
            hits.append((dt, m.start(), m.end()))
    hits.sort(key=lambda h: h[1])
    # de-dupe identical spans
    uniq: List[Tuple[date, int, int]] = []
    seen = set()
    for dt, s, e in hits:
        key = (dt, s)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((dt, s, e))
    return uniq


# ---- patterns --------------------------------------------------------------

# Case-sensitive on purpose: a proper name must actually be capitalized.
# (re.I on the old pattern made "My dad makes" yield PERSON 'makes — dad'.)
_NAME = r"([A-Z][a-z]{1,24}(?:-[A-Z][a-z]{1,24})?)"
_REL = (
    r"(wife|husband|spouse|partner|daughter|son|kid|child|sister|brother|sibling|"
    r"mother|father|mom|dad|girlfriend|boyfriend|consort)"
)

_NOT_A_NAME = {
    "the", "a", "an", "this", "that", "there", "here", "what", "who", "will",
    "your", "our", "their", "his", "her", "its", "my", "me", "we", "you",
    "i", "she", "he", "it", "they", "them", "us", "when", "where", "why",
    "how", "which", "whose", "whom", "and", "but", "for", "nor", "or",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "today", "tomorrow", "yesterday", "january", "february", "march", "april",
    "may", "june", "july", "august", "september", "october", "november", "december",
    "christmas", "python", "america", "english", "update", "status", "brief",
    "primary", "fallback", "option", "target",
}

_ROLES = {
    # kinship / household
    "wife", "husband", "spouse", "partner", "daughter", "son", "kid", "child",
    "sister", "brother", "sibling", "mother", "father", "mom", "dad",
    "girlfriend", "boyfriend", "consort", "fiancée", "fiancee", "fiance",
    # common occupations / titles (lowercase)
    "nurse", "teacher", "doctor", "physician", "engineer", "attorney", "lawyer",
    "pilot", "student", "model", "photographer", "writer", "chef", "musician",
    "artist", "accountant", "consultant", "manager", "director", "architect",
    "designer", "developer", "programmer", "researcher", "scientist",
    "professor", "coach", "trainer", "therapist", "dentist", "veterinarian",
    "electrician", "plumber", "carpenter", "mechanic", "bartender",
    "waiter", "waitress", "flight attendant", "steward", "paralegal",
    "realtor", "broker", "banker", "analyst", "intern", "founder",
    "entrepreneur", "contractor", "freelancer", "paramedic", "firefighter",
    "officer", "soldier", "sailor", "marine", "veterinarian", "pharmacist",
    "librarian", "journalist", "editor", "producer", "director", "actor",
    "actress", "dancer", "singer", "composer", "pastor", "priest", "rabbi",
    "imam", "monk", "nun", "judge", "clerk", "secretary", "assistant",
    "receptionist", "cashier", "salesperson", "executive", "president",
    "ceo", "cto", "cfo", "coo", "vp", "vice president",
}

_PERSON_IS_MY = re.compile(
    rf"\b{_NAME}\s+is\s+(?i:my|our)\s+(?i:{_REL})\b"
)
_PERSON_MY_NAME = re.compile(
    rf"\b(?i:my|our)\s+(?i:{_REL})\s+{_NAME}\b"
)
# "Maya is a teacher", "Maya is currently a teacher",
# "Maya is a teacher on my team"
_PERSON_IS_A = re.compile(
    rf"\b{_NAME}\s+is\s+(?:currently\s+)?(?:an?\s+)([A-Za-z][A-Za-z][A-Za-z '-]{{1,40}}?)(?=\s*(?:,|\.|$|which\b|in\s+))"
)
_PERSON_IS_OUR_ROLE = re.compile(
    rf"\b{_NAME}\s+is\s+(?i:my|our)\s+(?:primary\s+)?([A-Za-z][A-Za-z][A-Za-z '-]{{1,40}}?)(?=\s*[,.]|$)"
)
_HOUSEHOLD_CTX = re.compile(
    r"(?i)\bin\s+(?:my|our)\s+(?:household|family|home|circle)\b"
)

_EVENT_TRIP = re.compile(
    r"(?i)\b(?:I(?:'m|\s+am)|we(?:'re|\s+are))\s+"
    r"(?:taking\s+a\s+trip\s+to|going\s+to|headed\s+to|flying\s+to|traveling\s+to|"
    r"visiting|leaving\s+for|leave\s+for)\s+"
    r"([A-Za-z][A-Za-z .'-]{1,40}?)(?=\s+(?:on|for|in|next|tomorrow|today|,|\.|$))"
)

_EVENT_TRIP_BARE = re.compile(
    r"(?i)\b(?:I(?:'m|\s+am)|we(?:'re|\s+are))\s+"
    r"(?:taking\s+a\s+trip\s+to|going\s+to|headed\s+to|flying\s+to|traveling\s+to|"
    r"visiting|leaving\s+for|leave\s+for)\s+"
    r"([A-Za-z][A-Za-z .'-]{1,40})"
)

_SHOW_LABEL = re.compile(
    r"(?i)(?:primary\s+target|fallback|newest\s+option|target\s+show|show(?:\s+date)?)\s*:\s*"
    r"([^\n,]{3,80}?),\s*"
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+)?"
)
_SHOW_KW = re.compile(
    r"(?i)\b(?:npc|competitions?|contests?|championships?|physique|"
    r"(?<!to )(?<!fashion )(?<!trade )show)\b"
)
_BAD_SHOW_NAME = re.compile(
    r"(?i)^(?:published|article|source|here'?s|outlook|ties|report|pid|spec|"
    r"deepens|fuel|strong|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)
_INTERVIEW_KW = re.compile(
    r"(?i)\b(?:interview|technical\s+round|recruiter\s+screen|panel\s+interview)\b"
)
_INTERVIEW_SKIP = {
    "technical", "round", "interview", "recruiter", "screen", "panel",
    "principal", "senior", "sr", "architect", "security", "director",
    "engineer", "manager", "vice", "president", "prep", "brief", "job",
    "search", "ai", "architecture", "vp", "tomorrow", "today", "teams",
    "phoenix", "time", "pm", "am", "with", "the", "and", "for", "from",
} | _WEEKDAY | set(_MONTH)

_PREF = re.compile(
    r"(?i)\bI\s+(?:prefer|always)\s+(.{5,80}?)(?:\.|$)"
)

_STATUS_LIVE = re.compile(
    r"(?i)\bI\s+(?:live\s+in|moved\s+to|work\s+at|work\s+for|joined)\s+"
    r"(.{2,60}?)(?:\.|$)"
)
_EMPLOYED = re.compile(r"(?i)\bI(?:'m|\s+am)\s+(?:gainfully\s+)?employed\b")
_JOB_SEARCHING = re.compile(
    r"(?i)\b(?:"
    r"looking for a new (?:job|role|one)|"
    r"actively searching\b|"
    r"I(?:'m|\s+am)\s+(?:currently\s+)?(?:job[- ]hunting|on the (?:job )?market)|"
    r"my (?:current )?job hunting status"
    r")"
)
_JOB_CTX = re.compile(
    r"(?i)\b(?:job|jobs|role|roles|career|recruiter|recruiters|hiring|"
    r"employed|employment|job[- ]hunt)\b"
)


@dataclass
class UserFact:
    category: str  # PERSON | EVENT | PREFERENCE | STATUS
    key: str  # dedupe key
    summary: str  # short human value (also proposal.values[0])
    proposed_line: str
    target: str  # USER.md | MEMORY.md
    confidence: float
    expires: Optional[str] = None  # ISO date for EVENT
    session_id: Optional[str] = None
    message_id: Optional[int] = None
    line_sha256: Optional[str] = None
    snippet: str = ""
    quote: str = ""
    precision: Optional[str] = None  # "explicit" | "relative" (EVENT)
    sort_ts: float = 0.0

    def evidence_row(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "message_id": self.message_id,
            "line_sha256": self.line_sha256,
            "snippet": self.snippet or _snippet(self.quote),
            "role": "user",
            "value": self.summary,
            "category": self.category,
        }


def _msg_date(ts: Any) -> date:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts).date()
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return date.today()


def _ts_to_float(ts: Any) -> float:
    if isinstance(ts, (int, float)) and ts > 0:
        return float(ts)
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def _is_skippable_user_text(content: str) -> bool:
    if not content or not content.strip():
        return True
    if is_compaction_message(content):
        return True
    if _SKIP_PREFIX.search(content):
        return True
    if re.search(r"(?i)\b@file:attachments/", content):
        return True
    return False


def _is_proper_name(token: str) -> bool:
    if not token or token.lower() in _NOT_A_NAME:
        return False
    return bool(re.fullmatch(r"[A-Z][a-z]{1,24}(?:-[A-Z][a-z]{1,24})?", token))


def _looks_like_role(raw: str, *, household: bool) -> bool:
    role = re.sub(r"\s+", " ", raw).strip(" .,;:")
    if not role or len(role) > 40:
        return False
    low = role.lower()
    if low in _NOT_A_NAME or low in {"language", "country", "city", "company",
                                      "idea", "brief", "plan", "test", "problem",
                                      "thing", "way", "type", "kind", "form",
                                      "part", "lot", "bit", "shame", "fact",
                                      "holiday", "update", "unique", "simple"}:
        return False
    if low in _ROLES:
        return True
    if household and re.fullmatch(r"[A-Za-z][A-Za-z '-]{1,40}", role):
        return True
    # Capitalized title word (Teacher, Director) — not a common noun
    if re.fullmatch(r"[A-Z][a-z]{2,24}(?:[ -][A-Z][a-z]{2,24})?", role) and low not in _WEEKDAY:
        return True
    return False


def _clean_place(raw: str) -> str:
    place = re.sub(r"\s+", " ", raw).strip(" .,;:!?")
    # drop trailing relative clauses accidentally captured
    place = re.split(r"\b(?:for the|to see|with my|and then)\b", place, maxsplit=1, flags=re.I)[0].strip()
    # common short forms
    if re.fullmatch(r"(?i)vegas", place):
        place = "Las Vegas"
    return place[:60]


def _place_key(place: str) -> str:
    p = place.lower().strip()
    if "vegas" in p:
        return "vegas"
    return p


def _entity_before(text: str, pos: int) -> str:
    window = text[max(0, pos - 200):pos]
    tokens = list(re.finditer(r"[A-Za-z][A-Za-z.'-]*", window))
    collected: List[str] = []
    for m in reversed(tokens):
        tok = m.group(0)
        low = tok.lower().rstrip(".")
        if low in _INTERVIEW_SKIP or low in _NOT_A_NAME:
            if collected:
                break
            continue
        if re.fullmatch(r"[A-Z]{2,4}", tok):
            if collected:
                break
            continue
        if not re.fullmatch(r"[A-Z][a-z]+(?:[.'-][A-Z]?[a-z]+)?", tok):
            if collected:
                break
            continue
        collected.append(tok)
        if len(collected) >= 3:
            break
    collected.reverse()
    return " ".join(collected)


def _trip_when_and_expiry(body: str, ref: date) -> Optional[Tuple[date, date, str]]:
    """Return (start, end, precision) for a trip mentioned in ``body``."""
    rng = parse_date_range(body, ref)
    if rng:
        start, end = rng
        return start, end, "explicit"
    # Prefer an explicit date that is not document metadata.
    for dt, s, _e in iter_explicit_dates(body, ref):
        if _is_meta_date(body, s):
            continue
        return dt, dt + timedelta(days=1), "explicit"
    rel = normalize_date(" ".join(
        m.group(0) for m in _RELATIVE.finditer(body)
    ) or "", ref)
    # normalize_date on relative-only text
    if rel is None:
        # fall back to the historical single-date helper (may hit a relative)
        # but skip if the only hits were meta dates already considered
        guess = None
        m = _RELATIVE.search(body)
        if m:
            guess = normalize_date(m.group(0), ref)
        if guess is None:
            return None
        return guess, guess + timedelta(days=1), "relative"
    return rel, rel + timedelta(days=1), "relative"


def extract_from_text(content: str, *, ref_date: date, session_id: Optional[str] = None,
                      message_id: Optional[int] = None, timestamp: Any = None) -> List[UserFact]:
    """Extract user facts from a single user message body."""
    if _is_skippable_user_text(content):
        return []
    # Strip a leading "Do not save…" directive; keep the rest for assertion mining.
    body = re.sub(r"(?is)^\s*Do not save or update anything in memory while answering\.\s*", "", content).strip()
    if not body:
        return []
    if _HYPOTHETICAL.search(body):
        return []
    out: List[UserFact] = []
    ref = _msg_date(timestamp) if timestamp is not None else ref_date
    sha = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    snip = _snippet(body)
    ts = _ts_to_float(timestamp)

    def _add(fact: UserFact) -> None:
        fact.sort_ts = ts
        out.append(fact)

    # PERSON
    def _person(name: str, rel: str, quote: str, conf: float = 0.9) -> None:
        if not _is_proper_name(name):
            return
        role = re.sub(r"\s+", " ", rel).strip(" .,;:")
        if not role:
            return
        if _QUESTION_LEAD.search(body) and body.strip().endswith("?") and "is my" not in body.lower() and "is our" not in body.lower():
            return
        key = f"person:{name.lower()}:{role.lower()}"
        line = f"- PERSON: {name} — {role}"
        _add(UserFact(
            category="PERSON", key=key, summary=f"{name} ({role})", proposed_line=line,
            target="USER.md", confidence=conf, session_id=session_id, message_id=message_id,
            line_sha256=sha, snippet=snip, quote=quote,
        ))

    for m in _PERSON_IS_MY.finditer(body):
        _person(m.group(1), m.group(2).lower(), m.group(0))
    for m in _PERSON_MY_NAME.finditer(body):
        _person(m.group(2), m.group(1).lower(), m.group(0))
    household = bool(_HOUSEHOLD_CTX.search(body))
    for m in _PERSON_IS_A.finditer(body):
        name, role = m.group(1), m.group(2)
        clause = body[m.start(): min(len(body), m.end() + 80)]
        local_hh = household or bool(_HOUSEHOLD_CTX.search(clause))
        if _looks_like_role(role, household=local_hh):
            _person(name, re.sub(r"\s+", " ", role).strip(" .,;:"), m.group(0), 0.86)
    for m in _PERSON_IS_OUR_ROLE.finditer(body):
        name, role = m.group(1), m.group(2)
        # already covered kinship forms; this catches "X is my primary consort"
        if _looks_like_role(role, household=True):
            _person(name, re.sub(r"\s+", " ", role).strip(" .,;:"), m.group(0), 0.88)

    # EVENT — first-person trip / travel
    for pat in (_EVENT_TRIP, _EVENT_TRIP_BARE):
        m = pat.search(body)
        if not m:
            continue
        place = _clean_place(m.group(1))
        if len(place) < 2 or place.lower() in {"the", "a", "an", "my", "our"}:
            continue
        dated = _trip_when_and_expiry(body, ref)
        if dated is None:
            # still try a short window after the verb, for back-compat
            when = normalize_date(m.group(0) + " " + body[m.end(): m.end() + 40], ref)
            if when is None:
                continue
            dated = (when, when + timedelta(days=1), "relative" if _RELATIVE.search(body) else "explicit")
        when, end, prec = dated
        expiry = end.isoformat()
        key = f"event:trip:{place.lower()}:{when.isoformat()}"
        line = (f"STATUS: trip to {place} on {when.isoformat()} "
                f"(revisit after {expiry})")
        _add(UserFact(
            category="EVENT", key=key, summary=f"trip to {place} on {when.isoformat()}",
            proposed_line=line, target="MEMORY.md",
            confidence=0.85 if prec == "relative" else 0.92,
            expires=expiry, session_id=session_id, message_id=message_id,
            line_sha256=sha, snippet=snip, quote=m.group(0)[:120],
            precision=prec,
        ))
        break  # one trip fact per message is enough

    # EVENT — dated shows / competitions (may be several in one brief)
    seen_show: set = set()
    for m in _SHOW_LABEL.finditer(body):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" .,;:")
        rest = body[m.end(): m.end() + 48]
        when = normalize_date(rest, ref)
        if when is None or not name or len(name) < 3:
            continue
        sk = (name.lower(), when.isoformat())
        if sk in seen_show:
            continue
        seen_show.add(sk)
        expiry = (when + timedelta(days=1)).isoformat()
        key = f"event:show:{name.lower()}:{when.isoformat()}"
        line = f"STATUS: {name} on {when.isoformat()} (revisit after {expiry})"
        _add(UserFact(
            category="EVENT", key=key, summary=f"{name} on {when.isoformat()}",
            proposed_line=line, target="MEMORY.md", confidence=0.9,
            expires=expiry, session_id=session_id, message_id=message_id,
            line_sha256=sha, snippet=snip, quote=(name + " " + rest[:40]).strip()[:120],
            precision="explicit",
        ))
    # unlabeled "NPC … show June 12 2028" / "show June 12, 2028"
    for dt, s, e in iter_explicit_dates(body, ref):
        if _is_meta_date(body, s):
            continue
        line_start = body.rfind("\n", 0, s) + 1
        line_end = body.find("\n", e)
        if line_end < 0:
            line_end = len(body)
        window = body[line_start:line_end]
        if not _SHOW_KW.search(window):
            continue
        if any(dt.isoformat() == k[1] for k in seen_show):
            continue
        # name: prefer an NPC title on this line, else words just before the date
        nm = None
        nmm = re.search(
            r"(?i)\b(NPC\s+[A-Za-z][A-Za-z0-9 .&'-]{1,40}?)(?=\s*\(|\s*,|\s*$)",
            window,
        )
        if nmm:
            nm = re.sub(r"\s+", " ", nmm.group(1)).strip(" .,;:")
        if not nm:
            pre = body[max(0, s - 60):s]
            nmm = re.search(
                r"([A-Z][A-Za-z0-9'&-]+(?:\s+[A-Z][A-Za-z0-9'&-]+){0,4})\s*$",
                pre.strip(" ,;:"),
            )
            if nmm and nmm.group(1).split()[0].lower() not in _WEEKDAY:
                nm = nmm.group(1).strip()
        if not nm:
            nm = "NPC show" if re.search(r"(?i)\bnpc\b", window) else None
        if not nm or _BAD_SHOW_NAME.search(nm):
            continue
        sk = (nm.lower(), dt.isoformat())
        if sk in seen_show:
            continue
        seen_show.add(sk)
        expiry = (dt + timedelta(days=1)).isoformat()
        key = f"event:show:{nm.lower()}:{dt.isoformat()}"
        line = f"STATUS: {nm} on {dt.isoformat()} (revisit after {expiry})"
        _add(UserFact(
            category="EVENT", key=key, summary=f"{nm} on {dt.isoformat()}",
            proposed_line=line, target="MEMORY.md", confidence=0.86,
            expires=expiry, session_id=session_id, message_id=message_id,
            line_sha256=sha, snippet=snip, quote=window.strip()[:120],
            precision="explicit",
        ))

    # EVENT — interviews / technical rounds
    seen_iv: set = set()
    for m in _INTERVIEW_KW.finditer(body):
        # date in a window around the keyword
        win = body[max(0, m.start() - 20): m.end() + 80]
        when = normalize_date(win, ref)
        if when is None:
            # "interview Tue 11/10" may sit just after the keyword
            when = normalize_date(body[m.start(): m.end() + 40], ref)
        if when is None:
            continue
        if when.isoformat() in seen_iv:
            continue
        # locate the date span to extract a company name
        date_pos = m.start()
        for dt, s, e in iter_explicit_dates(body, ref):
            if dt == when and s >= m.start() - 20:
                date_pos = s
                break
        who = _entity_before(body, date_pos) or _entity_before(body, m.start())
        label = f"{who} interview" if who else "interview"
        seen_iv.add(when.isoformat())
        expiry = (when + timedelta(days=1)).isoformat()
        key = f"event:interview:{(who or 'interview').lower()}:{when.isoformat()}"
        line = (f"STATUS: {label} on {when.isoformat()} "
                f"(revisit after {expiry})")
        _add(UserFact(
            category="EVENT", key=key, summary=f"{label} on {when.isoformat()}",
            proposed_line=line, target="MEMORY.md", confidence=0.9,
            expires=expiry, session_id=session_id, message_id=message_id,
            line_sha256=sha, snippet=snip, quote=win.strip()[:120],
            precision="explicit",
        ))

    # PREFERENCE
    if not _QUESTION_LEAD.search(body):
        m = _PREF.search(body)
        if m:
            pref = re.sub(r"\s+", " ", m.group(1)).strip(" .,;:")
            if 4 <= len(pref) <= 80:
                key = f"pref:{pref.lower()[:48]}"
                line = f"- PREFERENCE: {pref}"
                _add(UserFact(
                    category="PREFERENCE", key=key, summary=pref, proposed_line=line,
                    target="USER.md", confidence=0.7, session_id=session_id, message_id=message_id,
                    line_sha256=sha, snippet=snip, quote=m.group(0)[:120],
                ))

    # STATUS (job / location)
    if not _QUESTION_LEAD.search(body):
        m = _STATUS_LIVE.search(body)
        if m:
            val = re.sub(r"\s+", " ", m.group(1)).strip(" .,;:")
            if 2 <= len(val) <= 60:
                key = f"status:{val.lower()[:48]}"
                _add(UserFact(
                    category="STATUS", key=key, summary=val, proposed_line=f"- STATUS: {val}",
                    target="USER.md", confidence=0.75, session_id=session_id, message_id=message_id,
                    line_sha256=sha, snippet=snip, quote=m.group(0)[:120],
                ))
        # job-search status (first person)
        searching = False
        for jm in _JOB_SEARCHING.finditer(body):
            # first-person only: the sentence up to the match must have an "I" subject
            sent_start = max(body.rfind(".", 0, jm.start()), body.rfind("\n", 0, jm.start())) + 1
            prefix = body[sent_start:jm.start()]
            if jm.group(0).lower().startswith(("i'm", "i am", "my ")):
                searching = True
                break
            if re.search(r"\bI(?:'m|\s+am|\s+[a-z]+)\b", prefix) and not re.search(
                    r"(?i)\b(?:my\s+\w+|he|she|they|we|you)\s+(?:is|are|was|were)\b", prefix):
                searching = True
                break
        searching = searching and bool(_JOB_CTX.search(body))
        employed = bool(_EMPLOYED.search(body))
        if searching or (employed and searching):
            if employed and searching:
                val = "employed, actively searching"
            else:
                val = "actively searching"
            key = "status:job-search"
            _add(UserFact(
                category="STATUS", key=key, summary=val, proposed_line=f"- STATUS: {val}",
                target="USER.md", confidence=0.88, session_id=session_id, message_id=message_id,
                line_sha256=sha, snippet=snip,
                quote=(_EMPLOYED.search(body) or _JOB_SEARCHING.search(body)).group(0)[:120],
            ))
        elif employed and not searching:
            # bare "I'm employed" without a search assertion is too thin to stage
            pass

    # Do not stage events whose revisit date is already in the past
    # relative to the dream day (ref_date), not the source-message date.
    kept: List[UserFact] = []
    for fact in out:
        if fact.category == "EVENT" and fact.expires and fact.expires < ref_date.isoformat():
            continue
        kept.append(fact)
    return _dedupe_facts(kept)


def _trip_start(fact: UserFact) -> Optional[date]:
    m = re.search(r"\bon (\d{4}-\d{2}-\d{2})\b", fact.summary)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _supersede_trip_events(facts: Sequence[UserFact]) -> List[UserFact]:
    """Same place/trip, different dates: keep the explicit (or later) one."""
    trips: List[UserFact] = []
    other: List[UserFact] = []
    for f in facts:
        if f.category == "EVENT" and f.key.startswith("event:trip:"):
            trips.append(f)
        else:
            other.append(f)
    if len(trips) <= 1:
        return list(facts)

    def _grp(f: UserFact) -> str:
        m = re.search(r"trip to (.+) on ", f.summary, re.I)
        return _place_key(m.group(1) if m else f.key)

    groups: Dict[str, List[UserFact]] = {}
    for f in trips:
        groups.setdefault(_grp(f), []).append(f)

    kept: List[UserFact] = []
    used = set()
    for _place, items in groups.items():
        # cluster by start-date proximity (same trip being refined)
        remaining = list(items)
        clusters: List[List[UserFact]] = []
        while remaining:
            seed = remaining.pop(0)
            sd = _trip_start(seed)
            cluster = [seed]
            rest = []
            for other_f in remaining:
                od = _trip_start(other_f)
                if sd and od and abs((od - sd).days) <= 30:
                    cluster.append(other_f)
                else:
                    rest.append(other_f)
            remaining = rest
            clusters.append(cluster)
        for cluster in clusters:
            explicit = [f for f in cluster if f.precision == "explicit"]
            pool = explicit or cluster

            def _rank(f: UserFact) -> Tuple:
                exp = date.fromisoformat(f.expires) if f.expires else date.min
                return (exp, f.sort_ts, f.message_id or 0)

            winner = max(pool, key=_rank)
            kept.append(winner)
            used.add(id(winner))

    # preserve original order: non-trips in place, winning trips in first occurrence slot
    out: List[UserFact] = []
    emitted_trip_keys = set()
    winners_by_id = {id(f): f for f in kept}
    for f in facts:
        if f.category == "EVENT" and f.key.startswith("event:trip:"):
            # emit the cluster winner the first time we see any member
            # find winner for this fact's cluster
            place = _grp(f)
            start = _trip_start(f)
            match = None
            for w in kept:
                if _grp(w) != place:
                    continue
                wd = _trip_start(w)
                if start and wd and abs((wd - start).days) <= 30:
                    match = w
                    break
            if match is None:
                continue
            if match.key in emitted_trip_keys:
                continue
            emitted_trip_keys.add(match.key)
            out.append(match)
        else:
            out.append(f)
    return out


def _merge_nearby_named_events(facts: Sequence[UserFact], prefix: str) -> List[UserFact]:
    """Collapse the same named show/interview on consecutive days into one span."""
    chosen: List[UserFact] = []
    other: List[UserFact] = []
    groups: Dict[str, List[UserFact]] = {}
    for f in facts:
        if f.category == "EVENT" and f.key.startswith(prefix):
            name = f.key[len(prefix):].rsplit(":", 1)[0]
            groups.setdefault(name, []).append(f)
        else:
            other.append(f)
    if not groups:
        return list(facts)
    keep_ids = set()
    for name, items in groups.items():
        items = sorted(items, key=lambda x: _trip_start(x) or date.min)
        cluster: List[UserFact] = []

        def _flush(cluster: List[UserFact]) -> None:
            if not cluster:
                return
            first, last = cluster[0], cluster[-1]
            start = _trip_start(first)
            end_d = None
            if last.expires:
                try:
                    end_d = date.fromisoformat(last.expires)
                except ValueError:
                    end_d = None
            # expires is already day-after-event; if we merged, use the latest expiry
            if len(cluster) == 1:
                keep_ids.add(id(first))
                return
            if start and end_d:
                first.summary = re.sub(r"\bon \d{4}-\d{2}-\d{2}\b", f"on {start.isoformat()}", first.summary, count=1)
                first.expires = end_d.isoformat()
                first.proposed_line = (
                    f"STATUS: {name} on {start.isoformat()} (revisit after {first.expires})"
                    if not first.proposed_line.startswith("STATUS: trip")
                    else first.proposed_line
                )
                # name may be lowercased from the key; prefer original summary stem
                stem = re.sub(r"\s+on \d{4}-\d{2}-\d{2}$", "", cluster[0].summary)
                first.summary = f"{stem} on {start.isoformat()}"
                first.proposed_line = f"STATUS: {stem} on {start.isoformat()} (revisit after {first.expires})"
                first.key = f"{prefix}{name}:{start.isoformat()}"
            keep_ids.add(id(first))

        for f in items:
            if not cluster:
                cluster = [f]
                continue
            prev = _trip_start(cluster[-1])
            cur = _trip_start(f)
            if prev and cur and 0 <= (cur - prev).days <= 2:
                cluster.append(f)
            else:
                _flush(cluster)
                cluster = [f]
        _flush(cluster)
    out: List[UserFact] = []
    for f in facts:
        if f.category == "EVENT" and f.key.startswith(prefix):
            if id(f) in keep_ids:
                out.append(f)
        else:
            out.append(f)
    return out


def _dedupe_facts(facts: Sequence[UserFact]) -> List[UserFact]:
    seen = set()
    out = []
    for f in facts:
        if f.key in seen:
            continue
        seen.add(f.key)
        out.append(f)
    out = _merge_nearby_named_events(out, "event:show:")
    return _supersede_trip_events(out)


def already_in_text(fact: UserFact, text: str) -> bool:
    """True when MEMORY/USER already carries this fact (dedupe)."""
    if not text:
        return False
    low = text.lower()
    if fact.category == "PERSON":
        # name + relationship both present nearby-ish
        parts = fact.summary.lower().replace("(", " ").replace(")", " ").split()
        if parts and parts[0] in low and (len(parts) < 2 or parts[-1] in low):
            # also accept proposed_line substring
            if fact.proposed_line.lower().lstrip("- ").lower() in low or parts[0] in low:
                # stricter: require relationship word too when present
                if len(parts) >= 2 and parts[-1] not in low:
                    return False
                return parts[0] in low and (len(parts) < 2 or parts[-1] in low)
        return False
    if fact.category == "EVENT":
        # place + date
        m = re.search(r"trip to (.+) on (\d{4}-\d{2}-\d{2})", fact.summary, re.I)
        if m:
            place, day = m.group(1).lower(), m.group(2)
            if place in low and day in low:
                return True
            # Las Vegas / Vegas equivalence
            if "vegas" in place and "vegas" in low and day in low:
                return True
        return fact.summary.lower() in low
    return fact.summary.lower() in low or fact.proposed_line.lower().lstrip("- ") in low


def would_exceed_limit(text: str, new_line: str, limit: Optional[int]) -> bool:
    if limit is None:
        return False
    sep = "" if text.endswith("\n") or not text else "\n"
    return len(text) + len(sep) + len(new_line) + 1 > int(limit)


def fetch_user_messages(hermes_home: Path, *, lookback_days: int = 30,
                        max_rows: int = 8000, end_day: Optional[date] = None) -> List[Dict[str, Any]]:
    """Read-only user-role messages from state.db (no tool / compaction)."""
    import sqlite3
    from datetime import time as _time

    db = Path(hermes_home) / "state.db"
    if not db.is_file():
        return []
    end = end_day or date.today()
    # inclusive window: end_day 23:59:59 back lookback_days
    hi = datetime.combine(end, _time(23, 59, 59)).timestamp()
    lo = datetime.combine(end - timedelta(days=max(0, lookback_days - 1)), _time(0, 0)).timestamp()
    uri = f"file:{db}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
        except sqlite3.Error:
            pass
        rows = con.execute(
            "SELECT m.id, m.session_id, m.content, m.timestamp FROM messages m "
            "WHERE m.role = 'user' AND m.timestamp >= ? AND m.timestamp <= ? "
            "ORDER BY m.timestamp DESC LIMIT ?",
            (lo, hi, int(max_rows)),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass
    out = []
    for mid, sid, content, ts in rows:
        if not content or _is_skippable_user_text(content):
            continue
        out.append({
            "message_id": mid,
            "session_id": sid,
            "content": content,
            "timestamp": ts,
            "role": "user",
            "line_sha256": hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest(),
            "snippet": _snippet(content),
        })
    return out


def extract_user_facts(messages: Iterable[Dict[str, Any]], *, ref_date: Optional[date] = None) -> List[UserFact]:
    ref = ref_date or date.today()
    facts: List[UserFact] = []
    for msg in messages:
        if (msg.get("role") or "user") != "user":
            continue
        facts.extend(extract_from_text(
            msg.get("content") or "",
            ref_date=ref,
            session_id=msg.get("session_id"),
            message_id=msg.get("message_id"),
            timestamp=msg.get("timestamp"),
        ))
    return _dedupe_facts(facts)


def resolve_target(hermes_home: Path, target_name: str) -> Path:
    """Prefer memories/<name>, else HERMES_HOME/<name>."""
    home = Path(hermes_home)
    for cand in (home / "memories" / target_name, home / target_name):
        if cand.is_file():
            return cand
    # default create-path for apply (file may be created only on apply; propose needs a path)
    mem = home / "memories"
    return (mem / target_name) if mem.is_dir() else home / target_name


def filter_new_facts(facts: Sequence[UserFact], hermes_home: Path,
                     limits: Optional[Dict[str, Optional[int]]] = None) -> List[UserFact]:
    """Drop facts already present or that would blow the char limit (propose still stages
    size-overflow as a note-only skip — we simply omit them)."""
    limits = limits or {}
    cache: Dict[str, str] = {}
    out = []
    for f in facts:
        path = resolve_target(hermes_home, f.target)
        key = str(path)
        if key not in cache:
            cache[key] = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        text = cache[key]
        if already_in_text(f, text):
            continue
        lim = limits.get(f.target)
        if would_exceed_limit(text, f.proposed_line, lim):
            continue
        out.append(f)
    return out


def char_limits_from_home(hermes_home: Path, cfg: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """MEMORY.md 8000 default; USER.md from hermes config when present."""
    mem_lim: Optional[int] = cfg.get("apply_max_chars")
    user_lim: Optional[int] = cfg.get("apply_max_chars")
    hc = Path(hermes_home) / "config.yaml"
    if hc.is_file():
        try:
            import yaml
            data = yaml.safe_load(hc.read_text()) or {}
            mem = (data.get("memory") or {}) if isinstance(data, dict) else {}
            if mem_lim is None and mem.get("memory_char_limit") is not None:
                mem_lim = int(mem["memory_char_limit"])
            if user_lim is None and mem.get("user_char_limit") is not None:
                user_lim = int(mem["user_char_limit"])
        except Exception:
            pass
    if mem_lim is None:
        mem_lim = 8000
    return {"MEMORY.md": mem_lim, "USER.md": user_lim}
