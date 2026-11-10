"""Safe-write guard (2026-11-08): Hermes MEMORY.md.lock, CAS before write, line-scoped edits,
same-line keep+retired refusal, stale-proposal refusal. Synthetic data only."""
import fcntl, json, os, threading, time
from datetime import date

import dream_cycle.apply as A
from dream_cycle.apply import apply_proposal, rollback, plan_edit, ApplyRefused, sha256
from dream_cycle.config import default_config
from dream_cycle.propose import propose
from dream_cycle.review import read_reviews
from dream_cycle.sessions import SessionResult
from conftest import write_mem
import pytest

NOSESS = SessionResult("disabled")
COST = {"status": "unavailable", "detail": "test"}
STAGE = "plugin-data/dream-cycle/dream-proposals/2026-11-08"
PORT = default_config()["labels"][2]


def _cfg(**kw):
    c = default_config(); c["apply_enabled"] = True; c["memory_lock_timeout_s"] = 0.5; c.update(kw); return c


def _stage(home, text):
    p = write_mem(home, text)
    propose(home, config=_cfg(), run_date=date(2026, 11, 8), session_result=NOSESS, cost_result=COST)
    props = json.loads((home / STAGE / "summary.json").read_text())["proposals"]
    return p, props


def _apply(home, pid, keep="18992", **kw):
    return apply_proposal(home, pid, keep, "synthetic: ss shows :18992", "test", config=_cfg(),
                          today=date(2026, 11, 8), **kw)


MEM = "demo-svc listens on port :18991\n§\ndemo-svc endpoint url http://localhost:18992/v1\n"


def test_propose_stores_freshness(home):
    p, props = _stage(home, MEM)
    assert props[0]["file_sha256"] == sha256(p.read_text())
    assert props[0]["lines"] == {"1": "demo-svc listens on port :18991",
                                 "3": "demo-svc endpoint url http://localhost:18992/v1"}


def test_same_line_keep_and_retired_refused(home):
    text = "demo-svc :18992 primary and demo-svc :18991 fallback on the same line\n"
    p, props = _stage(home, text)
    r = _apply(home, props[0]["proposal_id"])
    assert r.status == "refused" and "both" in r.message and p.read_text() == text


def test_plan_edit_scoped_to_proposal_lines():
    text = "a port :18991\nb port :18992\nc port :18991\n"
    new, changes, _ = plan_edit(text, PORT, ["18991"], "18992", "ev", "pid", "2026-11-08", line_nos=[1, 2])
    assert [c["line_no"] for c in changes] == [1] and new.endswith("c port :18991\n")
    new2, changes2, _ = plan_edit(text, PORT, ["18991"], "18992", "ev", "pid", "2026-11-08", all_lines=True)
    assert [c["line_no"] for c in changes2] == [1, 3]
    with pytest.raises(ApplyRefused):
        plan_edit(text, PORT, ["18991"], "18992", "ev", "pid", "2026-11-08", line_nos=[])


def test_stale_proposal_refused_and_logged(home):
    p, props = _stage(home, MEM)
    p.write_text(MEM.replace(":18991", ":18995"))  # cited line changed after propose
    before = p.read_text()
    r = _apply(home, props[0]["proposal_id"])
    assert r.status == "refused" and "stale" in r.message and p.read_text() == before
    rows = read_reviews(home / STAGE)
    assert rows[-1]["decision"] == "apply-refused" and "stale" in rows[-1]["evidence"]


def test_unrelated_change_is_not_stale(home):
    p, props = _stage(home, MEM)
    p.write_text(MEM + "§\nan unrelated new entry\n")  # cited lines unchanged
    r = _apply(home, props[0]["proposal_id"])
    assert r.status == "applied", r.message
    assert "an unrelated new entry" in p.read_text()
    rec = json.loads(next((home / "plugin-data/dream-cycle/receipts").glob("*.json")).read_text())
    assert rec["file_changed_since_propose"] is True and rec["scope"] == "proposal-lines"


def test_old_stage_without_freshness_refused(home):
    p, props = _stage(home, MEM)
    sp = home / STAGE / "summary.json"
    s = json.loads(sp.read_text())
    for q in s["proposals"]:
        q.pop("file_sha256"); q.pop("lines")
    sp.write_text(json.dumps(s))
    r = _apply(home, props[0]["proposal_id"])
    assert r.status == "refused" and "freshness" in r.message


def test_cas_refuses_lock_ignoring_writer(home, monkeypatch):
    p, props = _stage(home, MEM)
    monkeypatch.setattr(A, "_pre_write_hook", lambda path: path.write_text(path.read_text() + "rogue\n"))
    r = _apply(home, props[0]["proposal_id"])
    assert r.status == "refused" and "changed since read" in r.message
    assert p.read_text() == MEM + "rogue\n"  # the other writer's edit survives; nothing clobbered
    assert read_reviews(home / STAGE)[-1]["decision"] == "apply-refused"


def _hold_hermes_lock(path, seconds, then_append=None, started=None):
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    if started: started.set()
    time.sleep(seconds)
    if then_append:
        path.write_text(path.read_text() + then_append)
    fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def test_lock_busy_refuses(home):
    p, props = _stage(home, MEM)
    ev = threading.Event()
    t = threading.Thread(target=_hold_hermes_lock, args=(p, 1.5), kwargs={"started": ev}); t.start(); ev.wait()
    r = _apply(home, props[0]["proposal_id"])
    t.join()
    assert r.status == "refused" and "lock busy" in r.message and p.read_text() == MEM


def test_concurrent_memory_tool_write_not_lost(home):
    """A Hermes-style writer holds MEMORY.md.lock and appends; apply waits, then sees the append."""
    p, props = _stage(home, MEM)
    ev = threading.Event()
    t = threading.Thread(target=_hold_hermes_lock, args=(p, 0.2),
                         kwargs={"then_append": "§\nwritten by memory tool\n", "started": ev}); t.start(); ev.wait()
    r = apply_proposal(home, props[0]["proposal_id"], "18992", "synthetic: ss shows :18992", "test",
                       config=_cfg(memory_lock_timeout_s=5), today=date(2026, 11, 8))
    t.join()
    out = p.read_text()
    assert r.status == "applied", r.message
    assert "written by memory tool" in out and "listens on port :18992" in out


def test_rollback_holds_lock_and_cas(home, monkeypatch):
    p, props = _stage(home, MEM)
    assert _apply(home, props[0]["proposal_id"]).status == "applied"
    applied = p.read_text()
    ev = threading.Event()
    t = threading.Thread(target=_hold_hermes_lock, args=(p, 1.5), kwargs={"started": ev}); t.start(); ev.wait()
    r = rollback(home, last=True, config=_cfg())
    t.join()
    assert r.status == "refused" and "lock busy" in r.message and p.read_text() == applied
    monkeypatch.setattr(A, "_pre_write_hook", lambda path: path.write_text(path.read_text() + "rogue\n"))
    r = rollback(home, last=True, config=_cfg())
    assert r.status == "refused" and "concurrent" in r.message and p.read_text() == applied + "rogue\n"
    monkeypatch.setattr(A, "_pre_write_hook", None)
    p.write_text(applied)
    assert rollback(home, last=True, config=_cfg()).status == "rolled-back" and p.read_text() == MEM
