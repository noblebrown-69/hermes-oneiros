"""Session corroboration — quiet days (404) are normal, not failures."""
import json
import sys
from datetime import date

from dream_cycle.config import default_config
from dream_cycle.propose import propose
from dream_cycle.sessions import SessionResult, fetch_session_lines, parse_day, parse_days
from conftest import write_mem

PY = sys.executable


def _cfg(days_payload=None, day_payload=None, day_exit=0, day_err="", days_exit=0):
    cfg = default_config()
    days_cmd = [PY, "-c", f"import sys;print({json.dumps(json.dumps(days_payload))});sys.exit({days_exit})"] \
        if days_payload is not None else None
    day_cmd = [PY, "-c",
               f"import sys;sys.stderr.write({day_err!r});print({json.dumps(json.dumps(day_payload or {}))});"
               f"sys.exit({day_exit})"]
    cfg["session_corroboration"] = {"enabled": True, "source": "command", "days_command": days_cmd, "day_command": day_cmd, "timeout_s": 10}
    return cfg


def test_disabled_by_default():
    assert fetch_session_lines("2030-01-02", default_config()).status == "disabled"


def test_day_missing_from_index_is_no_sessions():
    r = fetch_session_lines("2030-01-02", _cfg({"daily_logs": [{"date": "2030-01-01"}]}))
    assert r.status == "no-sessions" and r.lines == []


def test_404_from_day_command_is_no_sessions():
    r = fetch_session_lines("2030-01-02", _cfg(day_exit=2, day_err='HTTP 404 {"detail":"Memory day not found"}'))
    assert r.status == "no-sessions"


def test_other_error_is_unavailable():
    r = fetch_session_lines("2030-01-02", _cfg(day_exit=2, day_err="HTTP 500 boom"))
    assert r.status == "unavailable" and "exit 2" in r.detail


def test_days_index_error_is_unavailable():
    r = fetch_session_lines("2030-01-02", _cfg({"dates": []}, days_exit=3))
    assert r.status == "unavailable"


def test_ok_lines_skip_tool_items():
    payload = {"daily_log": {"work_items": [{"kind": "research", "text": "moved api to :7000"},
                                             {"kind": "tool", "text": "noise :1"}]}}
    r = fetch_session_lines("2030-01-02", _cfg({"dates": ["2030-01-02"]}, payload))
    assert r.status == "ok" and r.lines == ["moved api to :7000"]


def test_missing_binary_is_unavailable():
    cfg = default_config()
    cfg["session_corroboration"] = {"enabled": True, "source": "command", "day_command": ["/nonexistent/bin/x", "{date}"]}
    assert fetch_session_lines("2030-01-02", cfg).status == "unavailable"


def test_bad_json_is_unavailable():
    cfg = default_config()
    cfg["session_corroboration"] = {"enabled": True, "source": "command", "day_command": [PY, "-c", "print('not json')"]}
    assert fetch_session_lines("2030-01-02", cfg).status == "unavailable"


def test_timeout_is_unavailable():
    cfg = default_config()
    cfg["session_corroboration"] = {"enabled": True, "source": "command", "timeout_s": 1,
                                    "day_command": [PY, "-c", "import time;time.sleep(5)"]}
    r = fetch_session_lines("2030-01-02", cfg)
    assert r.status == "unavailable" and "timed out" in r.detail


def test_date_placeholder_substituted():
    cfg = default_config()
    cfg["session_corroboration"] = {"enabled": True, "source": "command", "day_command": [
        PY, "-c", "import sys,json;print(json.dumps({'lines':[sys.argv[1]]}))", "{date}"]}
    assert fetch_session_lines("2030-01-02", cfg).lines == ["2030-01-02"]


def test_parse_days_shapes():
    assert parse_days({"daily_logs": [{"date": "a"}, {"x": 1}]}) == {"a"}
    assert parse_days({"dates": ["b"]}) == {"b"}
    assert parse_days(["c", {"date": "d"}]) == {"c", "d"}


def test_parse_day_lines_shape():
    assert parse_day({"lines": ["x", " ", "y"]}) == ["x", "y"]
    assert parse_day({}) == []


def test_propose_records_quiet_day(home):
    write_mem(home, "api on :9000\n")
    res = propose(home, run_date=date(2030, 1, 3), session_result=SessionResult("no-sessions", detail="quiet"))
    assert res.session_status == "no-sessions"
    summary = json.loads((home / "plugin-data/dream-cycle/dream-proposals/2030-01-03/summary.json").read_text())
    assert summary["session_status"] == "no-sessions" and summary["session_date"] == "2030-01-02"


def test_propose_session_disagreement(home):
    write_mem(home, "api port on :9000\n")
    res = propose(home, run_date=date(2030, 1, 3),
                  session_result=SessionResult("ok", lines=["moved the api port to :9100"]))
    assert len(res.proposals) == 1
    p = res.proposals[0]
    assert p.label == "port" and p.values == ["9000", "9100"] and "sessions said 9100" in p.note


def test_propose_session_agreement_no_proposal(home):
    write_mem(home, "api port on :9000\n")
    res = propose(home, session_result=SessionResult("ok", lines=["api port :9000 healthy"]))
    assert res.proposals == []


def test_session_value_without_memory_label_ignored(home):
    write_mem(home, "nothing labeled here\n")
    res = propose(home, session_result=SessionResult("ok", lines=["port :9100"]))
    assert res.proposals == []


def test_diff_mentions_session_status(home):
    write_mem(home, "x\n")
    res = propose(home, run_date=date(2030, 1, 3), session_result=SessionResult("unavailable", detail="exit 2"))
    diff = (home / "plugin-data/dream-cycle/dream-proposals/2030-01-03/diff.md").read_text()
    assert "unavailable" in diff and res.proposals == []


def test_explicit_session_date(home):
    write_mem(home, "x\n")
    propose(home, run_date=date(2030, 1, 3), session_date=date(2029, 12, 25),
            session_result=SessionResult("no-sessions"))
    s = json.loads((home / "plugin-data/dream-cycle/dream-proposals/2030-01-03/summary.json").read_text())
    assert s["session_date"] == "2029-12-25"
