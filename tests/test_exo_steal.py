import json, sqlite3
from datetime import date, datetime
from dream_cycle.config import default_config
from dream_cycle.propose import propose, extract_by_label
from dream_cycle.sessions import SessionResult, session_costs, format_costs
from dream_cycle.review import append_review, read_reviews
from conftest import write_mem

PORT = default_config()["labels"][2]
NOSESS = SessionResult("disabled")


def _db(path, rows, with_usage=True):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions (id TEXT, started_at REAL, message_count INT, tool_call_count INT,"
                " api_call_count INT, input_tokens INT, output_tokens INT, cache_read_tokens INT,"
                " cache_write_tokens INT, reasoning_tokens INT, estimated_cost_usd REAL, actual_cost_usd REAL)")
    if with_usage:
        con.execute("CREATE TABLE session_model_usage (session_id TEXT, model TEXT, api_call_count INT,"
                    " input_tokens INT, output_tokens INT, cache_read_tokens INT, cache_write_tokens INT,"
                    " reasoning_tokens INT, estimated_cost_usd REAL, actual_cost_usd REAL)")
    for r in rows:
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", r)
        if with_usage:
            con.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (r[0], "local-27b", r[4], r[5], r[6], r[7], r[8], r[9], 0, 0))
    con.commit(); con.close()


def test_year_not_port():
    t = "ATTR Alice@Forum 2026-11-05: endpoint listens on port :18991\n"
    assert [h[0] for h in extract_by_label(t, PORT)] == ["18991"]


def test_cost_query(home):
    ts = datetime(2026, 11, 5, 12).astimezone().timestamp()
    _db(home / "state.db", [("a", ts, 4, 3, 5, 1000, 50, 3000, 0, 7, 0.0, 0.0),
                            ("b", ts + 60, 2, 1, 2, 1000, 50, 1000, 0, 0, 0.0, 0.0),
                            ("old", ts - 86400 * 3, 1, 1, 1, 9, 9, 9, 9, 9, 0.0, 0.0)])
    c = session_costs(home, "2026-11-05", default_config())
    assert c["status"] == "ok" and c["sessions"] == 2 and c["input_tokens"] == 2000
    assert c["cache_read_share"] == round(4000 / 6000, 4) and c["tool_call_count"] == 4
    assert "estimated_cost_usd" not in c and "actual_cost_usd" not in c  # no invented USD
    assert c["by_model"][0]["model"] == "local-27b"
    assert "not reported" in format_costs(c)


def test_cost_fail_soft(home):
    assert session_costs(home, "2026-11-05", default_config())["status"] == "unavailable"
    (home / "state.db").write_text("not a db")
    assert session_costs(home, "2026-11-05", default_config())["status"] == "unavailable"


def test_summary_has_costs_speaker_tiers_undecided(home):
    write_mem(home, "ATTR Bob@Forum 2026-11-05: listens on port :18992\nlisten port :18991\n"
                    "MECH: bind port :1111 rule\n")
    r = propose(home, config=default_config(), run_date=date(2026, 11, 6), session_result=NOSESS,
                cost_result={"status": "ok", "sessions": 1, "input_tokens": 1, "output_tokens": 1,
                             "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
                             "tool_call_count": 0, "api_call_count": 1, "cache_read_share": 0.0})
    s = json.loads(open(r.stage_path + "/summary.json").read())
    assert s["apply_enabled"] is False and s["session_costs"]["sessions"] == 1
    assert s["tier_markers"]["MECH"] and not any("1111" in p["values"] for p in s["proposals"])
    p = s["proposals"][0]
    assert p["speaker"] == "Bob" and s["undecided"] == [p["proposal_id"]]
    append_review(r.stage_path, p["proposal_id"], "defer", "reviewer", evidence="unverified")
    assert read_reviews(r.stage_path)[0]["decision"] == "defer"
