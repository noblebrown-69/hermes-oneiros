"""Evidence grounding + freshness status (2026-11-08). Synthetic data only."""
import json, sqlite3
from datetime import date, datetime

from dream_cycle.apply import apply_proposal, rollback
from dream_cycle.config import default_config
from dream_cycle.propose import propose
from dream_cycle.review import read_reviews, append_review
from dream_cycle.sessions import SessionResult, ground_value
from dream_cycle.status import status, format_status
from conftest import write_mem

NOSESS = SessionResult("disabled")
COST = {"status": "unavailable", "detail": "test"}
STAGE = "plugin-data/dream-cycle/dream-proposals/2026-11-08"
MEM = "demo-svc listens on port :18991\n§\ndemo-svc endpoint url http://localhost:18992/v1\n"
PORT = default_config()["labels"][2]
NOON = datetime(2026, 11, 8, 12).astimezone().timestamp()


def _cfg(**kw):
    c = default_config(); c["apply_enabled"] = True; c["memory_lock_timeout_s"] = 0.5; c.update(kw); return c


def _db(home, msgs):
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    for sid, role, content, ts in msgs:
        con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (sid, role, content, ts))
    con.commit(); con.close()


def _stage(home):
    write_mem(home, MEM)
    propose(home, config=_cfg(), run_date=date(2026, 11, 8), session_result=NOSESS, cost_result=COST)
    return json.loads((home / STAGE / "summary.json").read_text())["proposals"][0]["proposal_id"]


def _apply(home, pid, **cfg):
    return apply_proposal(home, pid, "18992", "synthetic: ss shows :18992", "test", config=_cfg(**cfg),
                          today=date(2026, 11, 8))


def test_ground_value_user_output_grounds_and_hashes(home):
    _db(home, [("s1", "tool", "LISTEN 127.0.0.1 port :18992 users:demo-svc", NOON),
               ("s2", "user", "confirm endpoint port :18992 for demo-svc", NOON),
               ("s3", "assistant", "I think the port is :18992", NOON)])
    g = ground_value(home, PORT, "18992", ["2026-11-08"], default_config())
    # v0.6.1: tool/assistant ignored by default; only user cites ground
    assert g["evidence_status"] == "grounded" and [m["session_id"] for m in g["matches"]] == ["s2"]
    assert "content" not in json.dumps(g) and "LISTEN" not in json.dumps(g)  # hashes only, no text
    assert len(g["matches"][0]["line_sha256"]) == 64


def test_ground_value_tool_ignored_by_default(home):
    _db(home, [("s1", "tool", "LISTEN 127.0.0.1 port :18992 users:demo-svc", NOON)])
    g = ground_value(home, PORT, "18992", ["2026-11-08"], default_config())
    assert g["evidence_status"] == "unverified"


def test_assistant_claim_alone_is_unverified(home):
    _db(home, [("s2", "assistant", "the port is :18992", NOON)])
    assert ground_value(home, PORT, "18992", ["2026-11-08"], default_config())["evidence_status"] == "unverified"


def test_other_day_does_not_ground(home):
    _db(home, [("s1", "user", "port :18992", NOON - 86400 * 5)])
    assert ground_value(home, PORT, "18992", ["2026-11-08"], default_config())["evidence_status"] == "unverified"


def test_cited_session_ids_ground(home):
    _db(home, [("old", "user", "endpoint port :18992", NOON - 86400 * 5)])
    g = ground_value(home, PORT, "18992", ["2026-11-08"], default_config(), session_ids=["old"])
    assert g["evidence_status"] == "grounded"


def test_no_db_is_unverified_not_crash(home):
    assert ground_value(home, PORT, "18992", ["2026-11-08"], default_config())["detail"] == "state.db not found"


def test_receipt_and_ledger_record_grounded(home):
    _db(home, [("s1", "user", "bind port :18992 ok", NOON)])
    pid = _stage(home)
    r = _apply(home, pid)
    assert r.status == "applied" and r.evidence_status == "grounded"
    rec = json.loads(next((home / "plugin-data/dream-cycle/receipts").glob("*.json")).read_text())
    assert rec["evidence_status"] == "grounded" and rec["grounding"]["matches"][0]["session_id"] == "s1"
    row = read_reviews(home / STAGE)[-1]
    assert row["evidence_status"] == "grounded" and row["grounding_matches"][0]["message_id"] == 1


def test_unverified_allowed_by_default_but_recorded(home):
    pid = _stage(home)
    r = _apply(home, pid)
    assert r.status == "applied" and r.evidence_status == "unverified"
    assert read_reviews(home / STAGE)[-1]["evidence_status"] == "unverified"


def test_require_grounded_refuses_unverified(home):
    _db(home, [("s2", "assistant", "port :18992", NOON)])
    pid = _stage(home)
    p = home / "memories/MEMORY.md"
    r = _apply(home, pid, require_grounded_evidence=True)
    assert r.status == "refused" and "unverified" in r.message and p.read_text() == MEM
    row = read_reviews(home / STAGE)[-1]
    assert row["decision"] == "apply-refused" and row["evidence_status"] == "unverified"


def test_require_grounded_allows_grounded(home):
    _db(home, [("s1", "user", "the endpoint port :18992 is live", NOON)])
    pid = _stage(home)
    assert _apply(home, pid, require_grounded_evidence=True).status == "applied"


def test_status_freshness_receipt_and_warnings(home):
    s = status(home, _cfg())
    assert s["freshness"] == "never-run" and s["warnings"]
    pid = _stage(home)
    s = status(home, _cfg())
    assert s["freshness"] == "fresh" and s["latest"]["scanned_files"] == 1 and s["latest"]["undecided"] == 1
    assert s["last_receipt"] is None and "apply: ON" in s["text"]
    _apply(home, pid)
    s = status(home, _cfg())
    assert s["last_receipt"]["action"] == "applied" and s["latest"]["undecided"] == 0
    rollback(home, last=True, config=_cfg())
    s = status(home, _cfg())
    assert s["last_receipt"]["action"] == "rolled-back" and s["latest"]["undecided"] == 1
    assert "rolled-back" in s["text"]


def test_status_warns_loudly_on_zero_files_and_kill_switch(home):
    propose(home, config=_cfg(), run_date=date(2026, 11, 8), session_result=NOSESS, cost_result=COST)  # no MEMORY.md
    (home / "plugin-data/dream-cycle/DISABLED").touch()
    s = status(home, _cfg())
    assert s["latest"]["scanned_files"] == 0
    assert any("0 MEMORY FILES" in w for w in s["warnings"]) and "WARN: ⚠ LAST RUN SCANNED 0" in s["text"]
    assert s["disabled"] and "kill switch: ON" in s["text"]


def test_status_stale_run(home):
    pid = _stage(home)
    sp = home / STAGE / "summary.json"; d = json.loads(sp.read_text())
    d["generated_at"] = "2026-11-01T06:45:00"; sp.write_text(json.dumps(d))
    s = status(home, _cfg())
    assert s["freshness"] == "stale" and any("nightly" in w for w in s["warnings"])
