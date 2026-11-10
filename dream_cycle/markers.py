import re
# --- Exo-steal 2026-11-06: date-fragment skip, ATTR speaker, MECH/STATUS tiers ---

_ISO_SPAN_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")
_ATTR_RE = re.compile(r"^\s*(?:[-*]\s+)?ATTR\s+([^\s@:]+)(?:@([^\s:]+))?")
_TIER_RE = re.compile(r"^\s*(?:[-*]\s+)?(MECH|STATUS):")



def in_iso_date(line: str, start: int, end: int) -> bool:
    """True when [start, end) falls inside an ISO date/datetime token.

    Stops ``2026`` from ``2026-11-05`` being proposed as a port/value.
    """
    for m in _ISO_SPAN_RE.finditer(line):
        if m.start() <= start and end <= m.end():
            return True
    return False


# Paper / preprint / DOI ids — not ports or builds (v0.6.2).
# Covers: arXiv:2611.00463, 2611.00463v2, doi:10.1234/foo, 10.1000/xyz123
_PAPER_ID_RE = re.compile(
    r"(?i)\b(?:"
    r"arXiv:\d{4}\.\d{4,5}(?:v\d+)?|"          # arXiv:2611.00463 / arXiv:2611.00463v2
    r"\d{4}\.\d{4,5}(?:v\d+)?|"                 # bare 2611.00463v2
    r"doi:\s*10\.\d{4,9}/\S+|"                  # doi:10.1234/..."
    r"10\.\d{4,9}/[\w.\-;()/]+"                 # bare DOI 10.1000/xyz
    r")\b"
)


def in_paper_id(line: str, start: int, end: int) -> bool:
    """True when [start, end) falls inside an arXiv / DOI / year.number paper id.

    Stops ``2611`` from ``arXiv:2611.00463`` (or bare ``2611.00463v2``) being
    proposed as a port.
    """
    for m in _PAPER_ID_RE.finditer(line):
        if m.start() <= start and end <= m.end():
            return True
    # Also: digit run immediately followed by .digits (arxiv-style), even if the
    # broader token matcher missed an edge case.
    if end < len(line) and line[end] == "." and end + 1 < len(line) and line[end + 1].isdigit():
        # left side looks like a 4-digit year/code
        if end - start == 4 and (line[start:end]).isdigit():
            return True
    return False



def parse_attr(line: str) -> tuple:
    """Return (speaker, attr) from a leading ``ATTR Name@Venue ...`` tag, else (None, None)."""
    m = _ATTR_RE.match(line)
    if not m:
        return None, None
    attr = line.strip().lstrip("-* ").split(":", 1)[0][len("ATTR "):].strip()
    return m.group(1), attr or None


def line_tier(line: str):
    """Return ``"MECH"`` / ``"STATUS"`` when that is the line's first token, else None.

    Recognition only — not a Proposal field. Value extraction skips these lines
    so the scanner never guesses free text on them.
    """
    m = _TIER_RE.match(line)
    return m.group(1) if m else None


_SUPERSEDED_LINE_RE = re.compile(r"^SUPERSEDED\s+\d{4}-\d{2}-\d{2}\s*:")


def is_superseded_line(line: str) -> bool:
    """Column-0 ``SUPERSEDED YYYY-MM-DD:`` marker. Retired values there are not live facts."""
    return bool(_SUPERSEDED_LINE_RE.match(line))
