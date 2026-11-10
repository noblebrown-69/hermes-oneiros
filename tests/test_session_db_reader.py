"""Read-only state.db session corroboration for propose (v0.6.0)."""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

from dream_cycle.config import load_config, validate_config, default_config
from dream_cycle.propose import propose, extract_by_label
from dream_cycle.sessions import fetch_session_lines, fetch_session_lines_db, SessionResult


def _make_db(home: Path, messages):
    db = home / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL)"
    )
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, timestamp REAL)"
    )
    now = time.time()
    # put messages "yesterday" relative to a fixed day by using absolute timestamps
    for i, (role, content, day_offset_hours) in enumerate(messages, 1):
        sid = f"s{i}"
        ts = now - day_offset_hours * 3600
        con.execute("INSERT INTO sessions (id, started_at) VALUES (?, ?)", (sid, ts))
        con.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
            (i, sid, role, content, ts),
        )
    con.commit()
    con.close()
    return db


def test_ro_open_rejects_writes(tmp_path):
    home = tmp_path / "p"
    home.mkdir()
    _make_db(home, [("user", "demo listens on port :18991", 1)])
    # fetch works
    from datetime import date, timedelta
    day = (date.today() - timedelta(days=0)).isoformat()  # messages 1h ago -> today
    r = fetch_session_lines_db(home, day, {"grounding_roles": ["user", "tool"]}, lookback_days=2)
    assert r.status == "ok"
    assert r.lines
    assert r.evidence[0]["line_sha256"] == hashlib.sha256(r.lines[0].encode()).hexdigest()
    # ensure URI is ro: opening via our helper then attempting write should fail
    from dream_cycle.sessions import _ro_connect
    con = _ro_connect(home / "state.db")
    try:
        try:
            con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('x','user','y',1)")
            con.commit()
            raised = False
        except Exception:
            raised = True
        assert raised, "read-only connection must refuse writes"
    finally:
        con.close()


def test_propose_session_vs_memory_cites_evidence(tmp_path):
    """Fixed clock: messages on 2026-11-08 afternoon, propose run_date=2026-11-09
    so session_date is 2026-11-08. Independent of the local time of day.
    (The previous now-minus-20h setup failed after ~8 PM local, when those
    messages rolled past today's midnight and out of the yesterday window.)
    """
    from datetime import date, datetime
    import sqlite3

    home = tmp_path / "profiles" / "bot"
    (home / "memories").mkdir(parents=True)
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    # Memory has old port; sessions say new port
    (home / "memories" / "MEMORY.md").write_text(
        "demo-svc listens on port :18991\n"
    )
    sess_day = date(2026, 11, 8)
    def _ts(hour):
        return datetime(sess_day.year, sess_day.month, sess_day.day, hour, 0, 0).timestamp()
    db = home / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL)")
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, timestamp REAL)"
    )
    rows = [
        ("s1", "tool", "LISTEN 127.0.0.1 port :18992 demo-svc is live", _ts(14)),
        ("s2", "user", "confirm the endpoint url http://localhost:18992/v1", _ts(15)),
        ("s3", "assistant", "I claim port :19999 but assistants are not evidence", _ts(16)),
    ]
    for i, (sid, role, content, ts) in enumerate(rows, 1):
        con.execute("INSERT INTO sessions (id, started_at) VALUES (?, ?)", (sid, ts))
        con.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
            (i, sid, role, content, ts),
        )
    con.commit()
    con.close()
    cfg = default_config()
    cfg["apply_enabled"] = False
    cfg["session_corroboration"] = {
        "enabled": True, "source": "state.db", "lookback_days": 3,
    }
    cfg = validate_config(cfg)
    r = propose(home, config=cfg, run_date=date(2026, 11, 9), session_date=sess_day)
    assert r.skipped_reason is None
    assert r.session_status == "ok"
    assert r.proposals, r
    # Find port proposal with both values
    port = [p for p in r.proposals if p.label == "port"]
    assert port, r.proposals
    vals = set(port[0].values)
    assert "18991" in vals and "18992" in vals
    assert port[0].session_evidence, "must cite session evidence"
    ev = port[0].session_evidence[0]
    assert ev.get("line_sha256") and ev.get("session_id")
    assert "18992" in (ev.get("snippet") or "") or ev.get("value") == "18992"
    # assistant-only value must not create a lone 19999 keep without user/tool
    assert "19999" not in vals


def test_config_state_db_source_needs_no_day_command():
    cfg = default_config()
    cfg["session_corroboration"] = {"enabled": True, "source": "state.db", "lookback_days": 2}
    out = validate_config(cfg)
    assert out["session_corroboration"]["source"] == "state.db"


def test_fetch_disabled():
    r = fetch_session_lines("2026-11-08", {"session_corroboration": {"enabled": False}})
    assert r.status == "disabled"
