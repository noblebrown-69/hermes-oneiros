"""Don't-save detector (v0.8.0) — deterministic, user-role only, no model."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from dream_cycle.config import default_config, load_config, validate_config
from dream_cycle.errors import ConfigError
from dream_cycle.nosave import (
    apply_to_session,
    detect_nosave,
    fetch_conversation_messages,
    format_nosave_status,
    scan_messages,
)
from dream_cycle.propose import propose
from dream_cycle.review import read_reviews
from dream_cycle.sessions import SessionResult
from dream_cycle.status import status
from conftest import write_mem


REF = date(2026, 11, 9)
RUN = date(2026, 11, 10)


def _ts(hour=15, minute=0, day=9):
    return datetime(2026, 11, day, hour, minute, 0).timestamp()


def _db(home: Path, rows):
    """rows: (session_id, role, content, timestamp[, message_id])"""
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL)"
    )
    sids = set()
    for i, row in enumerate(rows, 1):
        sid, role, content, ts = row[0], row[1], row[2], row[3]
        mid = row[4] if len(row) > 4 else i
        if sid not in sids:
            con.execute("INSERT INTO sessions (id, started_at) VALUES (?, ?)", (sid, ts))
            sids.add(sid)
        con.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) "
            "VALUES (?,?,?,?,?)",
            (mid, sid, role, content, ts),
        )
    con.commit()
    con.close()


def _cfg(**kw):
    c = default_config()
    c["session_corroboration"]["enabled"] = False
    c["user_facts"]["enabled"] = True
    c["user_facts"]["lookback_days"] = 30
    c.update(kw)
    return validate_config(c)


def _facts(home, cfg=None):
    cfg = cfg or _cfg()
    r = propose(home, config=cfg, run_date=RUN, session_date=REF)
    s = json.loads(Path(r.stage_path, "summary.json").read_text())
    uf = [p for p in s["proposals"] if p.get("kind") == "user_fact"]
    return r, s, uf


# ---------------------------------------------------------------------------
# Phrase positives / negatives / quote guards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Don't save this. Alex is my sibling.",
    "Please do not save this message.",
    "Don't remember this.",
    "Do not remember that I said anything.",
    "Off the record, I'm taking a trip to Harbor City on Nov 19.",
    "Forget I said any of that.",
    "Don't log this.",
    "Keep this between us: I prefer dark roast.",
    "This is not for memory.",
    "Don't save this whole conversation.",
    "Please don't remember.",
    "do not store any of this",
])
def test_detect_positives(text):
    assert detect_nosave(text) is not None, text


@pytest.mark.parametrize("text", [
    "I don't remember where I put my keys.",
    "I don't save receipts anymore.",
    'What does "don\'t save" mean?',
    "If I say don't save this, what happens?",
    "Here's the log:\n```\ndon't save this\n```\nWhat do you think?",
    "Does the `don't save` flag work?",
    "What about “off the record” as a phrase?",
    "INFO 2026-11-09 12:00:01 don't save this was written to the log",
    "They don't remember the password.",
    "How does off the record work when I type it?",
    "The phrase don't save — does the bot honor it?",
])
def test_detect_negatives_and_quote_guards(text):
    assert detect_nosave(text) is None, text


def test_whole_conversation_flag():
    h = detect_nosave("Off the record for this entire session.")
    assert h and h.reason == "off_the_record" and h.whole_conversation is True
    h2 = detect_nosave("Don't save this.")
    assert h2 and h2.whole_conversation is False


# ---------------------------------------------------------------------------
# Scope: session vs exchange
# ---------------------------------------------------------------------------

def test_first_user_message_skips_whole_session():
    msgs = [
        {"session_id": "s1", "message_id": 1, "role": "user",
         "content": "Don't save this. Alex is my sibling.", "timestamp": 1,
         "line_sha256": "a"},
        {"session_id": "s1", "message_id": 2, "role": "assistant",
         "content": "Got it.", "timestamp": 2, "line_sha256": "b"},
        {"session_id": "s1", "message_id": 3, "role": "user",
         "content": "I'm taking a trip to Harbor City on Nov 19.", "timestamp": 3,
         "line_sha256": "c"},
    ]
    idx = scan_messages(msgs)
    assert "s1" in idx.session_ids
    assert idx.counts()["sessions_skipped"] == 1
    assert idx.counts()["exchanges_skipped"] == 0
    assert all(idx.is_skipped("s1", m["message_id"], m["line_sha256"]) for m in msgs)
    assert idx.skips[0].reason == "dont_save"
    assert "first_user_message" in idx.skips[0].tags


def test_whole_conversation_mid_session_skips_earlier_facts():
    msgs = [
        {"session_id": "s1", "message_id": 1, "role": "user",
         "content": "Alex is my sibling.", "timestamp": 1, "line_sha256": "a"},
        {"session_id": "s1", "message_id": 2, "role": "assistant",
         "content": "Nice.", "timestamp": 2, "line_sha256": "b"},
        {"session_id": "s1", "message_id": 3, "role": "user",
         "content": "Don't save this whole conversation.", "timestamp": 3,
         "line_sha256": "c"},
    ]
    idx = scan_messages(msgs)
    assert "s1" in idx.session_ids
    assert idx.is_skipped("s1", 1, "a")
    assert "whole_conversation" in idx.skips[0].tags


def test_mid_session_phrase_skips_only_the_exchange():
    msgs = [
        {"session_id": "s1", "message_id": 1, "role": "user",
         "content": "Alex is my sibling.", "timestamp": 1, "line_sha256": "a"},
        {"session_id": "s1", "message_id": 2, "role": "assistant",
         "content": "Nice.", "timestamp": 2, "line_sha256": "b"},
        {"session_id": "s1", "message_id": 3, "role": "user",
         "content": "Don't save this. I'm taking a trip to Harbor City on Nov 19.",
         "timestamp": 3, "line_sha256": "c"},
        {"session_id": "s1", "message_id": 4, "role": "assistant",
         "content": "Have fun, also the demo-svc endpoint is http://localhost:18992/v1",
         "timestamp": 4, "line_sha256": "d"},
        {"session_id": "s1", "message_id": 5, "role": "user",
         "content": "I prefer dark roast.", "timestamp": 5, "line_sha256": "e"},
    ]
    idx = scan_messages(msgs)
    assert "s1" not in idx.session_ids
    assert idx.counts()["exchanges_skipped"] == 1
    assert idx.counts()["sessions_skipped"] == 0
    assert not idx.is_skipped("s1", 1, "a")
    assert not idx.is_skipped("s1", 2, "b")
    assert idx.is_skipped("s1", 3, "c")
    assert idx.is_skipped("s1", 4, "d")  # following assistant turn
    assert not idx.is_skipped("s1", 5, "e")


def test_apply_to_session_drops_skipped_evidence():
    ev = [
        {"session_id": "s1", "message_id": 0, "line_sha256": "z", "role": "user"},
        {"session_id": "s1", "message_id": 1, "line_sha256": "a", "role": "user"},
        {"session_id": "s1", "message_id": 2, "line_sha256": "d", "role": "assistant"},
        {"session_id": "s1", "message_id": 3, "line_sha256": "e", "role": "user"},
    ]
    lines = [
        "Alex is my sibling.",
        "Don't save this. confirm the endpoint url http://localhost:18992/v1",
        "demo-svc listens on port :18992",
        "I prefer dark roast.",
    ]
    msgs = [
        {"session_id": "s1", "message_id": 0, "role": "user", "content": lines[0],
         "timestamp": 0, "line_sha256": "z"},
        {"session_id": "s1", "message_id": 1, "role": "user", "content": lines[1],
         "timestamp": 1, "line_sha256": "a"},
        {"session_id": "s1", "message_id": 2, "role": "assistant", "content": lines[2],
         "timestamp": 2, "line_sha256": "d"},
        {"session_id": "s1", "message_id": 3, "role": "user", "content": lines[3],
         "timestamp": 3, "line_sha256": "e"},
    ]
    idx = scan_messages(msgs)
    out = apply_to_session(idx, SessionResult("ok", lines=lines, evidence=ev, detail="3 lines"))
    assert out.lines == ["Alex is my sibling.", "I prefer dark roast."]
    assert [e["message_id"] for e in out.evidence] == [0, 3]
    assert "nosave skipped 2" in out.detail


def test_ledger_rows_are_hashes_only():
    msgs = [{
        "session_id": "secret-session-id", "message_id": 7, "role": "user",
        "content": "Don't save this secret fact about Alex.",
        "timestamp": 1,
        "line_sha256": hashlib.sha256(b"Don't save this secret fact about Alex.").hexdigest(),
    }]
    rows = scan_messages(msgs).ledger_rows(ts="2026-11-09T15:00:00-07:00")
    assert len(rows) == 1
    row = rows[0]
    assert row["decision"] == "nosave-skip"
    blob = json.dumps(row)
    assert "Alex" not in blob
    assert "secret fact" not in blob
    assert "Don't save" not in blob
    assert "secret-session-id" not in blob
    assert row["message_sha256"] == msgs[0]["line_sha256"]
    assert row["session_id_sha256"] == hashlib.sha256(b"secret-session-id").hexdigest()
    assert row["proposal_id"].startswith("nosave-")


# ---------------------------------------------------------------------------
# Propose integration: user facts + corroboration honor skips
# ---------------------------------------------------------------------------

def test_propose_skips_first_message_session_facts(home):
    write_mem(home, "demo-svc listens on port :18991\n")
    _db(home, [
        ("s1", "user", "Don't save this. Alex is my sibling.", _ts(15, 0)),
        ("s1", "assistant", "Understood.", _ts(15, 1)),
        ("s1", "user", "I'm taking a trip to Harbor City on Nov 19.", _ts(15, 2)),
    ])
    r, s, uf = _facts(home)
    assert uf == []
    assert s["nosave"]["honored"] is True
    assert s["nosave"]["sessions_skipped"] == 1
    assert s["nosave"]["exchanges_skipped"] == 0
    # Ledger has a nosave-skip row, hashes only
    rows = [x for x in read_reviews(Path(r.stage_path)) if x.get("decision") == "nosave-skip"]
    assert len(rows) == 1
    assert "Alex" not in json.dumps(rows)
    assert "Harbor City" not in json.dumps(rows)


def test_propose_exchange_skip_keeps_other_facts_and_drops_port(home):
    write_mem(home, "demo-svc listens on port :18991\n")
    _db(home, [
        ("s1", "user", "Alex is my sibling.", _ts(15, 0)),
        ("s1", "assistant", "Nice to hear.", _ts(15, 1)),
        ("s1", "user", "Don't save this. I'm taking a trip to Harbor City on Nov 19.", _ts(15, 2)),
        ("s1", "assistant", "Have fun. confirm the endpoint url http://localhost:18992/v1", _ts(15, 3)),
        ("s1", "user", "I prefer dark roast.", _ts(15, 4)),
    ])
    cfg = _cfg()
    cfg["session_corroboration"] = {
        "enabled": True, "source": "state.db", "lookback_days": 3,
    }
    cfg = validate_config(cfg)
    r, s, uf = _facts(home, cfg)
    cats = {p["category"]: p["values"][0] for p in uf}
    assert any("Alex" in v for v in cats.values())
    assert any("dark roast" in v.lower() for v in cats.values())
    assert not any("Harbor City" in v or "trip" in v.lower() for v in cats.values())
    # The :18992 mention lived only in the skipped assistant turn
    port = [p for p in s["proposals"] if p.get("label") == "port"]
    for p in port:
        assert "18992" not in p.get("values", [])
    assert s["nosave"]["exchanges_skipped"] == 1
    assert s["nosave"]["sessions_skipped"] == 0


def test_honor_nosave_false_does_not_skip(home):
    write_mem(home, "x\n")
    _db(home, [
        ("s1", "user", "Don't save this. Alex is my sibling.", _ts(15, 0)),
    ])
    cfg = _cfg(honor_nosave=False)
    r, s, uf = _facts(home, cfg)
    assert s["nosave"]["honored"] is False
    assert any(p["category"] == "PERSON" and "Alex" in p["values"][0] for p in uf)
    assert not any(x.get("decision") == "nosave-skip" for x in read_reviews(Path(r.stage_path)))


def test_quoted_question_does_not_drop_the_fact(home):
    write_mem(home, "x\n")
    _db(home, [
        ("s1", "user", 'What does "don\'t save" mean? Also, Alex is my sibling.', _ts(15, 0)),
    ])
    _, s, uf = _facts(home)
    assert s["nosave"]["sessions_skipped"] == 0
    assert s["nosave"]["exchanges_skipped"] == 0
    assert any("Alex" in p["values"][0] for p in uf)


def test_rerun_does_not_duplicate_ledger_rows(home):
    write_mem(home, "x\n")
    _db(home, [
        ("s1", "user", "Off the record, I prefer dark roast.", _ts(15, 0)),
    ])
    r1, _, _ = _facts(home)
    n1 = len([x for x in read_reviews(Path(r1.stage_path)) if x.get("decision") == "nosave-skip"])
    r2, s, _ = _facts(home)
    n2 = len([x for x in read_reviews(Path(r2.stage_path)) if x.get("decision") == "nosave-skip"])
    assert n1 == 1 and n2 == 1
    assert s["nosave"]["ledger_rows_written"] == 0


# ---------------------------------------------------------------------------
# Status screen
# ---------------------------------------------------------------------------

def test_status_shows_skip_counts_no_content(home):
    write_mem(home, "x\n")
    _db(home, [
        ("s1", "user", "Don't save this. Alex is my sibling.", _ts(15, 0)),
    ])
    _facts(home)
    st = status(home, _cfg())
    text = st["text"]
    assert "1 session(s)" in text
    assert "don't-save" in text or "dont_save" in text
    assert "Alex" not in text
    assert "sibling" not in text
    assert st["nosave"]["sessions_skipped"] == 1


def test_format_nosave_status_disabled():
    assert "honor_nosave=false" in format_nosave_status({"honored": False})
    assert format_nosave_status(None) == "nosave: n/a"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_honor_nosave_default_true():
    assert default_config()["honor_nosave"] is True
    assert validate_config(default_config())["honor_nosave"] is True


def test_honor_nosave_yaml_override(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("honor_nosave: false\n")
    assert load_config(p)["honor_nosave"] is False


def test_honor_nosave_must_be_bool():
    c = default_config()
    c["honor_nosave"] = "yes"
    with pytest.raises(ConfigError, match="honor_nosave"):
        validate_config(c)


def test_fetch_conversation_is_read_only(home):
    _db(home, [("s1", "user", "hello", _ts(15, 0))])
    rows = fetch_conversation_messages(home, lookback_days=30, end_day=REF)
    assert rows and rows[0]["role"] == "user"
    # file mtime / size unchanged; and a follow-up ro fetch still works
    before = (home / "state.db").read_bytes()
    fetch_conversation_messages(home, lookback_days=30, end_day=REF)
    assert (home / "state.db").read_bytes() == before
