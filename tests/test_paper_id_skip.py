"""v0.6.2: arXiv / DOI / year.number ids are not ports."""
from __future__ import annotations

import sqlite3
import time
from datetime import date
from pathlib import Path

from dream_cycle.config import default_config, validate_config
from dream_cycle.markers import in_paper_id
from dream_cycle.propose import extract_by_label, line_value_spans, propose


def _port():
    return [l for l in default_config()["labels"] if l["name"] == "port"][0]


def test_in_paper_id_arxiv_and_doi():
    line = "Cite as: arXiv:2611.00463 [cs.AI] (or arXiv:2611.00463v1)"
    # 2611 spans inside arXiv tokens
    i = line.index("2611")
    assert in_paper_id(line, i, i + 4)
    bare = "see 2611.00463v2 for details"
    j = bare.index("2611")
    assert in_paper_id(bare, j, j + 4)
    doi = "doi:10.48550/arXiv.2611.00463"
    # 48550 is inside DOI — start at 10.
    k = doi.index("10.")
    assert in_paper_id(doi, k, k + 2) or in_paper_id(doi, k, k + 7)
    assert not in_paper_id("listens on :8089", 12, 16)


def test_extract_skips_arxiv_keeps_real_port():
    text = (
        "Attached: arXiv:2611.00463 and https://arxiv.org/abs/2611.00463\n"
        "demo-svc listens on port :18992\n"
        "bare id 2611.00463v2 should not be a port\n"
    )
    hits = extract_by_label(text, _port())
    vals = {h[0] for h in hits}
    assert vals == {"18992"}
    assert "2611" not in vals


def test_line_value_spans_arxiv_colon_form():
    line = "Computer Science > Artificial Intelligence  arXiv:2611.00463 (cs)"
    assert line_value_spans(line, _port()) == []


def test_propose_ignores_arxiv_in_sessions(tmp_path):
    home = tmp_path / "bot"
    (home / "memories").mkdir(parents=True)
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("demo-svc listens on port :18991\n")
    db = home / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL)")
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, timestamp REAL)"
    )
    now = time.time()
    msg = (
        "@url:`https://arxiv.org/abs/2611.00463` Read this\n"
        "Cite as: arXiv:2611.00463 [cs.AI]\n"
    )
    con.execute("INSERT INTO sessions VALUES ('s1', ?)", (now - 3600,))
    con.execute(
        "INSERT INTO messages VALUES (1, 's1', 'user', ?, ?)",
        (msg, now - 3600),
    )
    con.commit()
    con.close()
    cfg = default_config()
    cfg["apply_enabled"] = False
    cfg["session_corroboration"] = {
        "enabled": True, "source": "state.db", "lookback_days": 3,
        "ignore_tool_messages": True, "ignore_compaction_messages": True,
        "require_user_evidence": True,
    }
    cfg = validate_config(cfg)
    r = propose(home, config=cfg, run_date=date.today(), session_date=date.today())
    port = [p for p in r.proposals if p.label == "port"]
    assert not port, port  # arxiv must not create a session-vs-memory port proposal
