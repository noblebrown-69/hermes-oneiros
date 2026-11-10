"""v0.6.1: exclude tool/compaction evidence; skip distinct-service port conflicts."""
from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import date
from pathlib import Path

from dream_cycle.config import default_config, validate_config
from dream_cycle.propose import propose, distinct_service_conflict, extract_by_label, service_key_near
from dream_cycle.sessions import fetch_session_lines_db, is_compaction_message


def _make_db(home: Path, messages):
    db = home / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL)")
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, timestamp REAL)"
    )
    now = time.time()
    for i, (role, content, hours_ago) in enumerate(messages, 1):
        sid = f"s{i}"
        ts = now - hours_ago * 3600
        con.execute("INSERT INTO sessions (id, started_at) VALUES (?, ?)", (sid, ts))
        con.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
            (i, sid, role, content, ts),
        )
    con.commit()
    con.close()
    return db


def _cfg(**sc_over):
    cfg = default_config()
    cfg["apply_enabled"] = False
    cfg["session_corroboration"] = {
        "enabled": True, "source": "state.db", "lookback_days": 3,
        "ignore_tool_messages": True, "ignore_compaction_messages": True,
        "require_user_evidence": True,
    }
    cfg["session_corroboration"].update(sc_over)
    return validate_config(cfg)


def test_is_compaction_message():
    assert is_compaction_message(
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below."
    )
    assert is_compaction_message("[PRIOR CONTEXT — for reference only; not a new message]")
    assert not is_compaction_message("please set the endpoint to http://localhost:18992/v1")


def test_tool_text_excluded_from_session_evidence(tmp_path):
    home = tmp_path / "bot"
    (home / "memories").mkdir(parents=True)
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("demo-svc listens on port :18991\n")
    _make_db(home, [
        ("tool", '<untrusted_tool_result source="web_search"> demo listens on port :18999 scrapes</untrusted_tool_result>', 2),
        ("tool", "[web_search] query='llama.cpp b12059' result mentions b12059 and :8000", 2),
        ("user", "confirm the endpoint url http://localhost:18992/v1", 1),
        ("assistant", "I alone claim port :19999 which is not enough", 1),
    ])
    r = propose(home, config=_cfg(), run_date=date.today(), session_date=date.today())
    assert r.session_status == "ok", r.session_detail
    port = [p for p in r.proposals if p.label == "port"]
    assert port, r.proposals
    vals = set(port[0].values)
    assert "18991" in vals and "18992" in vals
    assert "18999" not in vals and "8000" not in vals and "19999" not in vals
    roles = {e.get("role") for e in (port[0].session_evidence or [])}
    assert "tool" not in roles
    assert "user" in roles


def test_compaction_excluded_from_session_evidence(tmp_path):
    home = tmp_path / "bot"
    (home / "memories").mkdir(parents=True)
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("demo-svc listens on port :18991\n")
    compact = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. "
        "This is a handoff — treat as background. Old endpoint was http://localhost:17777/v1 and build b11900."
    )
    _make_db(home, [
        ("user", compact, 3),
        ("user", "please use http://localhost:18992/v1 going forward", 1),
    ])
    r = propose(home, config=_cfg(), run_date=date.today(), session_date=date.today())
    port = [p for p in r.proposals if p.label == "port"]
    assert port, r.proposals
    vals = set(port[0].values)
    assert "18992" in vals
    assert "17777" not in vals
    # fetch path itself also drops compaction
    day = date.today().isoformat()
    sess = fetch_session_lines_db(home, day, _cfg(), lookback_days=3)
    assert sess.status == "ok"
    assert all("CONTEXT COMPACTION" not in ln for ln in sess.lines)


def test_tool_ignore_can_be_disabled(tmp_path):
    home = tmp_path / "bot"
    (home / "memories").mkdir(parents=True)
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("demo-svc listens on port :18991\n")
    _make_db(home, [
        ("tool", "LISTEN 127.0.0.1 port :18995 demo-svc is live", 2),
    ])
    # With ignore on + require_user: tool-only must not propose
    r = propose(home, config=_cfg(), run_date=date.today(), session_date=date.today())
    assert not [p for p in r.proposals if p.label == "port" and "18995" in p.values]
    # With ignore off, tool in grounding_roles, and require_user off, tool text can corroborate
    cfg2 = _cfg(ignore_tool_messages=False, require_user_evidence=False)
    cfg2["grounding_roles"] = ["user", "assistant", "tool"]
    r2 = propose(home, config=cfg2, run_date=date.today(), session_date=date.today())
    port = [p for p in r2.proposals if p.label == "port"]
    assert port and "18995" in port[0].values


def test_two_services_ports_not_proposed(tmp_path):
    home = tmp_path / "bot"
    (home / "memories").mkdir(parents=True)
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text(
        "Lab: host-a. Chat = shared llama-server `chat-svc.service` :8089 (never bounce).\n"
        "Image gen: LIVE via `img-gen` (sd.cpp on device-b, :8093, on-demand).\n"
    )
    cfg = _cfg()
    # no session evidence needed — this is the memory-side multi-value path
    cfg["session_corroboration"]["enabled"] = False
    r = propose(home, config=cfg, run_date=date.today())
    assert not [p for p in r.proposals if p.label == "port"], r.proposals


def test_same_service_two_ports_still_proposed(tmp_path):
    home = tmp_path / "bot"
    (home / "memories").mkdir(parents=True)
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text(
        "`chat-svc` :8089 is current\n"
        "`chat-svc` :8090 was written by mistake\n"
    )
    cfg = _cfg()
    cfg["session_corroboration"]["enabled"] = False
    r = propose(home, config=cfg, run_date=date.today())
    port = [p for p in r.proposals if p.label == "port"]
    assert port and set(port[0].values) >= {"8089", "8090"}


def test_service_key_helpers():
    line = 'Chat = `chat-svc.service` :8089 (model-x)'
    idx = line.find("8089")
    assert service_key_near(line, idx, idx + 4) == "chat-svc.service"
    line2 = "Fallback Backup Host42 :8080 over mesh"
    idx2 = line2.find("8080")
    assert service_key_near(line2, idx2, idx2 + 4) == "host42"
    assert service_key_near("listens on :8080", 11, 15) == ""


def test_config_defaults_safe_on():
    c = validate_config(default_config())
    sc = c["session_corroboration"]
    assert sc["ignore_tool_messages"] is True
    assert sc["ignore_compaction_messages"] is True
    assert sc["require_user_evidence"] is True
    assert c["skip_distinct_service_conflicts"] is True
    assert "tool" not in c["grounding_roles"]
