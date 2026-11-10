from dream_cycle.propose import propose
from dream_cycle.sessions import SessionResult
from dream_cycle.status import status
from conftest import write_mem


def test_status_empty(tmp_path):
    s = status(tmp_path)
    assert s["disabled"] is False and s["apply_enabled"] is False and s["latest_stage"] is None


def test_status_after_run(home):
    write_mem(home, "a :1111\nb :2222\n")
    propose(home, session_result=SessionResult("no-sessions"))
    s = status(home)
    assert s["latest"]["count"] == 1 and s["latest"]["session_status"] == "no-sessions"


def test_status_unreadable_summary(home):
    d = home / "plugin-data/dream-cycle/dream-proposals/2030-01-01"; d.mkdir(parents=True)
    (d / "summary.json").write_text("{")
    assert "unreadable" in status(home)["latest"]["error"]


def test_status_disabled(home):
    d = home / "plugin-data/dream-cycle"; d.mkdir(parents=True); (d / "DISABLED").touch()
    assert status(home)["disabled"] is True
