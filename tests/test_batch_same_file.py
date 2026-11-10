"""Apply accepted must land every eligible line in a file, not only the first.

The live failure was 9 accepts, 2 receipts (one MEMORY.md, one USER.md), and
7 apply-refused rows: the first write changed the file, and the rest were
called stale. A real outside edit is still refused, and the refusal says why.
"""
import json
from datetime import date
from pathlib import Path

from dream_cycle.apply import apply_accepted, list_receipts, rollback, sha256
from dream_cycle.config import default_config
from dream_cycle.propose import propose
from dream_cycle.review import append_review, read_reviews
from dream_cycle.sessions import SessionResult

NOSESS = SessionResult("disabled")
COST = {"status": "unavailable", "detail": "test"}
DAY = date(2026, 11, 9)


def _cfg():
    cfg = default_config()
    cfg["apply_enabled"] = True
    cfg["user_facts"]["enabled"] = False
    cfg["memory_globs"] = ["MEMORY.md"]
    cfg["labels"] = [
        {"name": f"tok{i}", "pattern": rf"\b(t{i}[ab])\b", "proximity_words": 2}
        for i in range(5)
    ]
    return cfg


def _memory() -> str:
    lines = []
    for i in range(5):
        lines.append(f"svc t{i}a is the kept value")
        lines.append(f"svc t{i}b is the retired value")
    return "\n".join(lines) + "\n"


def _stage(home: Path):
    mem = home / "memories" / "MEMORY.md"
    user = home / "memories" / "USER.md"
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text(_memory())
    user.write_text("# user\n")
    propose(home, config=_cfg(), run_date=DAY, session_result=NOSESS, cost_result=COST)
    stage = home / "plugin-data/dream-cycle/dream-proposals/2026-11-09"
    summary = json.loads((stage / "summary.json").read_text())
    assert len(summary["proposals"]) == 5, [p["label"] for p in summary["proposals"]]
    user_sha = sha256(user.read_text())
    extras = []
    for n in range(4):
        line = f"PERSON: Ada {n} is a pilot"
        pid = f"c{n}c{n}c{n}c{n}c{n}c{n}"[:12]
        extras.append({
            "proposal_id": pid,
            "label": "user",
            "values": [line],
            "file": str(user),
            "line_nos": [],
            "note": "user fact",
            "kind": "user_fact",
            "action": "append",
            "proposed_line": line,
            "file_sha256": user_sha,
            "lines": {},
        })
    summary["proposals"].extend(extras)
    (stage / "summary.json").write_text(json.dumps(summary))
    for prop in summary["proposals"]:
        append_review(stage, prop["proposal_id"], "accept", "guardian",
                      evidence="", keep=prop["values"][0])
    return stage, mem, user, summary


def test_nine_accepts_two_files_all_apply_and_roll_back(home):
    """Regression: 9 accepts, 5 in MEMORY.md and 4 appends in USER.md.

    The old path wrote one receipt per file and refused the other 7.
    """
    stage, mem, user, summary = _stage(home)
    before_mem, before_user = mem.read_text(), user.read_text()
    out = apply_accepted(home, config=_cfg(), day="2026-11-09", actor="dashboard:test")
    applied = [r for r in out if r["status"] == "applied"]
    refused = [r for r in out if r["status"] == "refused"]
    assert len(out) == 9
    assert len(applied) == 9, [(r["proposal_id"], r["status"], r["message"]) for r in out]
    assert refused == []
    text = mem.read_text()
    for i in range(5):
        assert f"t{i}a" in text
        assert f"svc t{i}b is the retired value" not in text
        assert f"t{i}b" in text  # kept in the SUPERSEDED marker, not as the live line
    for n in range(4):
        assert f"PERSON: Ada {n} is a pilot" in user.read_text()
    assert len(list_receipts(home)) == 9
    rows = [r for r in read_reviews(stage) if r["decision"] == "apply-refused"]
    assert rows == []
    for rec in reversed(list_receipts(home)):
        rb = rollback(home, receipt_id=rec["receipt_id"], config=_cfg(), actor="dashboard:test")
        assert rb.ok, rb.message
    assert mem.read_text() == before_mem
    assert user.read_text() == before_user
    assert summary["proposals"]  # stage summary was not rewritten into a fake


def test_outside_edit_still_refused_with_a_reason_siblings_apply(home):
    stage, mem, user, summary = _stage(home)
    # Spoil only the last MEMORY proposal's retired line. The other four
    # MEMORY proposals and the USER appends stay eligible.
    last_edit = [p for p in summary["proposals"] if p["label"] == "tok4"][0]
    spoiled = mem.read_text().replace("svc t4b is the retired value", "svc t4b was edited by hand")
    mem.write_text(spoiled)
    out = apply_accepted(home, config=_cfg(), day="2026-11-09", actor="dashboard:test")
    by_id = {r["proposal_id"]: r for r in out}
    bad = by_id[last_edit["proposal_id"]]
    assert bad["status"] == "refused"
    assert "stale" in bad["message"]
    refused_rows = [r for r in read_reviews(stage) if r["decision"] == "apply-refused"]
    assert len(refused_rows) == 1
    assert refused_rows[0]["reason"].strip()
    assert "stale" in refused_rows[0]["reason"]
    assert refused_rows[0]["evidence"].strip()
    assert refused_rows[0].get("sha_now") != refused_rows[0].get("sha_at_propose")
    others = [r for r in out if r["proposal_id"] != last_edit["proposal_id"]]
    assert all(r["status"] == "applied" for r in others), [(r["proposal_id"], r["message"]) for r in others]
    assert "svc t4b was edited by hand" in mem.read_text()
    for n in range(4):
        assert f"PERSON: Ada {n} is a pilot" in user.read_text()
