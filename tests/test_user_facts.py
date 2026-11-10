"""User-fact extractor (v0.7.0) — deterministic, user-role only, no model."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from dream_cycle.apply import apply_proposal, rollback
from dream_cycle.config import default_config, load_config, validate_config
from dream_cycle.propose import propose
from dream_cycle.review import append_review
from dream_cycle.user_facts import (
    already_in_text,
    extract_from_text,
    extract_user_facts,
    fetch_user_messages,
    filter_new_facts,
    normalize_date,
    would_exceed_limit,
)


REF = date(2026, 11, 9)


def test_normalize_iso_and_month_and_relative():
    assert normalize_date("on 2026-11-21", REF) == date(2026, 11, 21)
    assert normalize_date("Nov 19", REF) == date(2026, 11, 19)
    assert normalize_date("November 19, 2026", REF) == date(2026, 11, 19)
    assert normalize_date("in a week or so", REF) == date(2026, 11, 16)
    assert normalize_date("next week", REF) == date(2026, 11, 16)
    assert normalize_date("tomorrow", REF) == date(2026, 11, 10)
    assert normalize_date("in 3 days", REF) == date(2026, 11, 12)


def test_person_positive_is_my_and_my_name():
    f = extract_from_text("Maya is my daughter.", ref_date=REF)
    assert len(f) == 1 and f[0].category == "PERSON" and "Maya" in f[0].summary
    assert f[0].target == "USER.md" and f[0].proposed_line.startswith("- PERSON:")
    f2 = extract_from_text("Elena is my wife.", ref_date=REF)
    assert f2[0].summary.startswith("Elena")
    f3 = extract_from_text("my wife Jordan came home.", ref_date=REF)
    assert f3 and f3[0].category == "PERSON" and "Jordan" in f3[0].summary


def test_event_vegas_trip_with_date():
    f = extract_from_text("I'm taking a trip to Las Vegas on Nov 19.", ref_date=REF)
    assert len(f) == 1
    assert f[0].category == "EVENT"
    assert f[0].target == "MEMORY.md"
    assert "2026-11-19" in f[0].summary
    assert f[0].proposed_line.startswith("STATUS:")
    assert f[0].expires == "2026-11-20"


def test_event_relative_week():
    f = extract_from_text(
        "I'm going to Vegas for the F1 race in a week or so. What weather?",
        ref_date=REF,
        timestamp="2026-11-09T12:00:00",
    )
    assert f and f[0].category == "EVENT"
    assert "Las Vegas" in f[0].summary
    assert "2026-11-16" in f[0].summary


def test_negative_question_who_is():
    assert extract_from_text("Who is Maya?", ref_date=REF) == []


def test_negative_gift_mention_no_relationship():
    assert extract_from_text(
        "Give me some reasonably priced Christmas gift ideas for Elena and Maya.",
        ref_date=REF,
    ) == []


def test_negative_assistant_and_compaction_and_hypothetical():
    assert extract_from_text(
        "[CONTEXT COMPACTION — REFERENCE ONLY] Maya is my daughter.", ref_date=REF
    ) == []
    # role filter at extract_user_facts level
    assert extract_user_facts([{"role": "assistant", "content": "Maya is my daughter."}]) == []
    assert extract_user_facts([{"role": "tool", "content": "Maya is my daughter."}]) == []
    assert extract_from_text("If I were going to Vegas on Nov 19, what then?", ref_date=REF) == []


def test_negative_quoted_third_party_discussion_prefix():
    assert extract_from_text(
        '[Discussion: "The Forum"] You are @bot. Maya is my daughter.',
        ref_date=REF,
    ) == []


def test_do_not_save_preamble_still_mines_assertion():
    body = (
        "Do not save or update anything in memory while answering.\n\n"
        "I'm going to Vegas for the F1 race in a week or so. What sort of weather?"
    )
    f = extract_from_text(body, ref_date=REF, timestamp="2026-11-09")
    assert f and f[0].category == "EVENT"


def test_preference_and_status():
    f = extract_from_text("I prefer dark roast coffee in the morning.", ref_date=REF)
    assert f and f[0].category == "PREFERENCE" and f[0].target == "USER.md"
    f2 = extract_from_text("I live in Portland.", ref_date=REF)
    assert f2 and f2[0].category == "STATUS"


def test_dedupe_already_in_text_and_size_limit():
    fact = extract_from_text("Maya is my daughter.", ref_date=REF)[0]
    assert already_in_text(fact, "# User\n- PERSON: Maya — daughter\n")
    assert not already_in_text(fact, "# User\nnope\n")
    assert would_exceed_limit("x" * 100, "hello", 104)  # 100 + sep + line + nl
    assert not would_exceed_limit("hi", "hello", 8000)


def test_expiry_field_set_for_events():
    f = extract_from_text("I'm taking a trip to Metro City on 2026-12-01.", ref_date=REF)[0]
    assert f.expires == "2026-12-02"


def _seed_db(home: Path, user_texts):
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, timestamp REAL)"
    )
    ts = time.mktime(datetime(2026, 11, 9, 12, 0, 0).timetuple())
    for i, content in enumerate(user_texts):
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
            (f"s{i}", "user", content, ts + i),
        )
    # poison: assistant should never yield user facts
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        ("poison", "assistant", "Alex is my brother.", ts),
    )
    con.commit()
    con.close()


def test_propose_stages_user_facts(tmp_path: Path):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("# mem\nport :9999\n")
    (home / "memories" / "USER.md").write_text("# user\n")
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    _seed_db(
        home,
        [
            "I'm taking a trip to Las Vegas on Nov 19.",
            "Jordan is my sister.",
            "Who is Jordan?",  # negative
        ],
    )
    cfg = default_config()
    cfg["session_corroboration"]["enabled"] = False
    cfg["user_facts"]["lookback_days"] = 30
    r = propose(home, config=cfg, run_date=date(2026, 11, 10), session_date=date(2026, 11, 9))
    kinds = {(p.kind, p.category, p.values[0]) for p in r.proposals if p.kind == "user_fact"}
    assert any(k[1] == "EVENT" and "Las Vegas" in k[2] for k in kinds)
    assert any(k[1] == "PERSON" and "Jordan" in k[2] for k in kinds)
    # no person fact from the question alone beyond the assertion message
    summ = json.loads(Path(r.stage_path, "summary.json").read_text())
    assert summ["model_used"] is False
    assert summ["user_fact_status"] == "ok"


def test_dedupe_skips_existing_user_fact(tmp_path: Path):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("# mem\n")
    (home / "memories" / "USER.md").write_text("# user\n- PERSON: Jordan — sister\n")
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    _seed_db(home, ["Jordan is my sister."])
    cfg = default_config()
    cfg["session_corroboration"]["enabled"] = False
    r = propose(home, config=cfg, run_date=date(2026, 11, 10), session_date=date(2026, 11, 9))
    persons = [p for p in r.proposals if p.category == "PERSON"]
    assert persons == []


def test_apply_append_and_rollback(tmp_path: Path):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    user = home / "memories" / "USER.md"
    user.write_text("# user\n")
    (home / "memories" / "MEMORY.md").write_text("# mem\n")
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    _seed_db(home, ["Riley is my brother."])
    cfg = default_config()
    cfg["apply_enabled"] = True
    cfg["session_corroboration"]["enabled"] = False
    r = propose(home, config=cfg, run_date=date(2026, 11, 10), session_date=date(2026, 11, 9))
    props = [p for p in r.proposals if p.kind == "user_fact"]
    assert props
    # load proposal id from summary
    summ = json.loads(Path(r.stage_path, "summary.json").read_text())
    prop = next(p for p in summ["proposals"] if p.get("kind") == "user_fact")
    pid = prop["proposal_id"]
    keep = prop["values"][0]
    before = user.read_text()
    dry = apply_proposal(home, pid, keep, "user message assertion", "tester", config=cfg, dry_run=True)
    assert dry.status == "dry-run"
    assert user.read_text() == before
    out = apply_proposal(home, pid, keep, "user message assertion", "tester", config=cfg)
    assert out.ok and out.status == "applied"
    after = user.read_text()
    assert "PERSON: Riley" in after
    assert "# dream-cycle append" in after
    rb = rollback(home, last=True, config=cfg, actor="tester")
    assert rb.ok
    assert user.read_text() == before


def test_config_user_facts_defaults():
    cfg = validate_config(default_config())
    assert cfg["user_facts"]["enabled"] is True
    assert cfg["user_facts"]["lookback_days"] == 30


def test_fetch_user_messages_skips_compaction(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    ts = time.mktime(datetime(2026, 11, 9, 12, 0, 0).timetuple())
    con.execute("INSERT INTO messages VALUES (1,'s','user',?,?)",
                ("[CONTEXT COMPACTION — REFERENCE ONLY] earlier", ts))
    con.execute("INSERT INTO messages VALUES (2,'s','user',?,?)",
                ("Riley is my brother.", ts))
    con.commit(); con.close()
    msgs = fetch_user_messages(home, lookback_days=5, end_day=date(2026, 11, 9))
    assert len(msgs) == 1
    assert "Riley" in msgs[0]["content"]


def test_skip_forwarded_bot_and_attachment():
    assert extract_from_text(
        "Message from bot: Maya is my daughter.", ref_date=REF
    ) == []
    assert extract_from_text(
        "@file:attachments/meeting-notes-2026-11-05.html OK, Maya is my daughter.",
        ref_date=REF,
    ) == []


def test_preference_ignores_love_noise():
    assert extract_from_text(
        "Good. Now, do some reading. I loved one character already.", ref_date=REF
    ) == []
    assert extract_from_text("I prefer dark roast.", ref_date=REF)
