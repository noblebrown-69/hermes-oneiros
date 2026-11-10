"""Gated apply + rollback (synthetic data only)."""
import json
from datetime import date

from dream_cycle.apply import apply_proposal, apply_accepted, rollback, sha256, list_receipts
from dream_cycle.config import default_config
from dream_cycle.propose import propose, extract_by_label
from dream_cycle.review import append_review, read_reviews, undecided
from dream_cycle.sessions import SessionResult
from conftest import write_mem

NOSESS = SessionResult("disabled")
COST = {"status": "unavailable", "detail": "test"}
MEM = ("# Synthetic memory\n§\nfoo-server listens on port :18991 (old note)\n§\n"
       "foo-server endpoint url http://localhost:18992/v1 (confirmed)\n")


def _cfg(**kw):
    c = default_config(); c["apply_enabled"] = True; c.update(kw); return c


def _stage(home, text=MEM, cfg=None):
    p = write_mem(home, text)
    r = propose(home, config=cfg or _cfg(), run_date=date(2026, 11, 8), session_result=NOSESS, cost_result=COST)
    pid = json.loads((home / "plugin-data/dream-cycle/dream-proposals/2026-11-08/summary.json").read_text()
                     )["proposals"][0]["proposal_id"]
    return p, r, pid


def _apply(home, pid, cfg=None, **kw):
    a = dict(keep="18992", evidence="synthetic: ss -ltnp shows :18992", actor="test")
    a.update(kw)
    return apply_proposal(home, pid, a["keep"], a["evidence"], a["actor"], config=cfg or _cfg(),
                          today=date(2026, 11, 8))


def test_refused_when_gate_off(home):
    p, _, pid = _stage(home)
    before = p.read_text()
    r = _apply(home, pid, cfg=default_config())
    assert r.status == "refused" and "apply_enabled" in r.message and p.read_text() == before


def test_apply_supersedes_logs_and_rolls_back(home):
    p, _, pid = _stage(home)
    before = p.read_text()
    r = _apply(home, pid)
    assert r.ok and r.status == "applied", r.message
    after = p.read_text()
    assert "SUPERSEDED 2026-11-08: 18991 → 18992, because synthetic: ss -ltnp shows :18992 [dream-cycle " in after
    assert "listens on port :18992 (old note)" in after          # line kept, token retired
    assert len(after.splitlines()) == len(before.splitlines()) + 1  # nothing deleted
    assert r.sha_before == sha256(before) and r.sha_after == sha256(after)
    snap = home / "plugin-data/dream-cycle/snapshots" / r.receipt_id / "MEMORY.md"
    assert snap.read_text() == before
    rows = read_reviews(home / "plugin-data/dream-cycle/dream-proposals/2026-11-08")
    assert rows[-1]["decision"] == "applied" and rows[-1]["sha_before"] == r.sha_before
    assert rows[-1]["evidence"] and rows[-1]["actor"] == "test" and "speaker" in rows[-1]
    # re-propose: SUPERSEDED marker is history, so no new conflict
    vals = {h[0] for h in extract_by_label(after, default_config()["labels"][2])}
    assert vals == {"18992"}
    # already applied → refused
    assert _apply(home, pid).status == "refused"
    rb = rollback(home, last=True, config=_cfg())
    assert rb.ok and sha256(p.read_text()) == sha256(before)
    assert read_reviews(home / "plugin-data/dream-cycle/dream-proposals/2026-11-08")[-1]["decision"] == "rolled-back"
    # re-apply after rollback works
    assert _apply(home, pid).status == "applied"


def test_secret_scan_refuses(home):
    p, _, pid = _stage(home)
    before = p.read_text()
    r = _apply(home, pid, evidence="token ghp_" + "a" * 30)
    assert r.status == "refused" and "secret" in r.message and p.read_text() == before
    assert not list_receipts(home)


def test_secret_on_changed_line_refuses(home):
    text = "port :18991 api_key=" + "x" * 12 + "\nendpoint port :18992\n"
    p, _, pid = _stage(home, text)
    r = _apply(home, pid)
    assert r.status == "refused" and "secret" in r.message and p.read_text() == text


def test_requires_actor_and_valid_keep_blank_evidence_needs_accept(home):
    p, _, pid = _stage(home)
    refused = _apply(home, pid, evidence="  ")
    assert refused.status == "refused" and "not accepted" in refused.message
    assert p.read_text() == MEM
    assert "actor" in _apply(home, pid, actor="").message
    assert "not one of" in _apply(home, pid, keep="9999").message


def test_char_limit_guard(home):
    p, _, pid = _stage(home)
    r = _apply(home, pid, cfg=_cfg(apply_max_chars=len(MEM) + 5))
    assert r.status == "refused" and "limit" in r.message and p.read_text() == MEM


def test_rollback_refuses_if_edited_since(home):
    p, _, pid = _stage(home)
    _apply(home, pid)
    p.write_text(p.read_text() + "someone else wrote\n")
    r = rollback(home, last=True, config=_cfg())
    assert r.status == "refused" and "changed since" in r.message
    assert rollback(home, last=True, force=True, config=_cfg()).ok
    assert p.read_text() == MEM


def test_disabled_kill_switch(home):
    p, _, pid = _stage(home)
    (home / "plugin-data/dream-cycle/DISABLED").write_text("")
    assert _apply(home, pid).status == "refused" and p.read_text() == MEM


def test_apply_accepted_from_ledger(home):
    p, _, pid = _stage(home)
    stage = home / "plugin-data/dream-cycle/dream-proposals/2026-11-08"
    append_review(stage, pid, "accept", "guardian", evidence="synthetic: config shows :18992", keep="18992")
    out = apply_accepted(home, config=_cfg(), day="2026-11-08")
    assert out and out[0]["status"] == "applied"
    assert "18991 → 18992" in p.read_text()
    assert not undecided(stage, [{"proposal_id": pid}])


def test_three_values_need_explicit_retire(home):
    text = MEM + "bar-proxy bind port :18993\n"
    p, _, pid = _stage(home, text)
    r = _apply(home, pid)
    assert r.status == "refused" and "retire" in r.message and p.read_text() == text
    r = apply_proposal(home, pid, "18992", "synthetic: ss shows :18992", "test", config=_cfg(),
                       today=date(2026, 11, 8), retire=["18991"])
    assert r.status == "applied" and "bind port :18993" in p.read_text()
