"""v0.4.1 cross-day decision carry — synthetic data only."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from dream_cycle.apply import apply_accepted, apply_proposal
from dream_cycle.config import default_config
from dream_cycle.propose import propose
from dream_cycle.review import (
    append_review,
    carry_prior_decisions,
    latest_decisions,
    latest_decisions_across,
    read_reviews,
    undecided,
)
from dream_cycle.sessions import SessionResult
from dream_cycle.status import status
from conftest import write_mem

NOSESS = SessionResult("no-sessions")
MEM = (
    # Same service, two ports — a real conflict (distinct-service pairs are skipped in v0.6.1).
    "demo-svc listens on port :18991\n"
    "demo-svc endpoint url http://localhost:18992/v1\n"
)


def _cfg(apply=False):
    c = default_config()
    c["apply_enabled"] = apply
    return c


def test_prior_reject_suppresses_undecided_across(home):
    write_mem(home, MEM)
    r1 = propose(home, config=_cfg(), run_date=date(2026, 11, 8), session_result=NOSESS)
    s1 = json.loads(Path(r1.stage_path, "summary.json").read_text())
    assert len(s1["proposals"]) == 1
    pid = s1["proposals"][0]["proposal_id"]
    append_review(r1.stage_path, pid, "reject", "guardian",
                  evidence="ss shows two live services, not a stale value",
                  note="two services, not a stale value")

    # Same conflict on the next day — without reading today's empty ledger alone,
    # across-days undecided must already be empty.
    r2 = propose(home, config=_cfg(), run_date=date(2026, 11, 9), session_result=NOSESS)
    s2 = json.loads(Path(r2.stage_path, "summary.json").read_text())
    assert s2["proposals"] and s2["proposals"][0]["proposal_id"] == pid
    assert s2["undecided"] == []
    assert pid in s2.get("carried_decisions", [])
    rows = read_reviews(r2.stage_path)
    assert any(r.get("decision") == "reject" and r.get("carried_from") == "2026-11-08" for r in rows)
    assert undecided(Path(r2.stage_path), s2["proposals"]) == []
    st = status(home, config=_cfg())
    assert st["latest"]["undecided"] == 0


def test_prior_defer_carried_still_open(home):
    write_mem(home, MEM)
    r1 = propose(home, config=_cfg(), run_date=date(2026, 11, 8), session_result=NOSESS)
    pid = json.loads(Path(r1.stage_path, "summary.json").read_text())["proposals"][0]["proposal_id"]
    append_review(r1.stage_path, pid, "defer", "guardian", evidence="need more checks")
    r2 = propose(home, config=_cfg(), run_date=date(2026, 11, 9), session_result=NOSESS)
    s2 = json.loads(Path(r2.stage_path, "summary.json").read_text())
    # defer is carried but still counts as undecided (open)
    assert pid in s2["undecided"]
    assert any(r.get("carried_from") == "2026-11-08" and r.get("decision") == "defer"
               for r in read_reviews(r2.stage_path))


def test_prior_accept_not_auto_applied(home):
    """A prior-day accept must not fire apply --accepted on a new stage."""
    write_mem(home, "demo-svc listens on port :18991\nother line port :18992\n")
    r1 = propose(home, config=_cfg(apply=True), run_date=date(2026, 11, 8), session_result=NOSESS)
    s1 = json.loads(Path(r1.stage_path, "summary.json").read_text())
    pid = s1["proposals"][0]["proposal_id"]
    append_review(r1.stage_path, pid, "accept", "guardian",
                  evidence="planted synthetic keep is 18992", keep="18992", retire=["18991"])
    # New day re-stages the same conflict; no accept in today's ledger.
    r2 = propose(home, config=_cfg(apply=True), run_date=date(2026, 11, 9), session_result=NOSESS)
    s2 = json.loads(Path(r2.stage_path, "summary.json").read_text())
    assert s2["proposals"][0]["proposal_id"] == pid
    # Accept is NOT carried.
    assert not any(r.get("decision") == "accept" for r in read_reviews(r2.stage_path))
    assert s2.get("carried_decisions") == []
    before = (home / "memories" / "MEMORY.md").read_text()
    results = apply_accepted(home, config=_cfg(apply=True), day="2026-11-09")
    assert results == []  # nothing to apply from today's ledger
    assert (home / "memories" / "MEMORY.md").read_text() == before


def test_cross_day_reject_blocks_apply(home):
    write_mem(home, MEM)
    r1 = propose(home, config=_cfg(apply=True), run_date=date(2026, 11, 8), session_result=NOSESS)
    pid = json.loads(Path(r1.stage_path, "summary.json").read_text())["proposals"][0]["proposal_id"]
    append_review(r1.stage_path, pid, "reject", "guardian", evidence="two live services")
    r2 = propose(home, config=_cfg(apply=True), run_date=date(2026, 11, 9), session_result=NOSESS)
    # Even if someone tries apply with keep, refuse because of reject.
    res = apply_proposal(home, pid, "8089", "try anyway", "tester",
                         config=_cfg(apply=True), day="2026-11-09")
    assert res.ok is False and "reject" in res.message


def test_carry_skips_when_today_already_has_row(home):
    write_mem(home, MEM)
    r1 = propose(home, config=_cfg(), run_date=date(2026, 11, 8), session_result=NOSESS)
    pid = json.loads(Path(r1.stage_path, "summary.json").read_text())["proposals"][0]["proposal_id"]
    append_review(r1.stage_path, pid, "reject", "guardian", evidence="prior")
    # Pre-seed today's ledger with a fresh defer before carry runs via propose.
    stage9 = home / "plugin-data/dream-cycle/dream-proposals/2026-11-09"
    stage9.mkdir(parents=True)
    append_review(stage9, pid, "defer", "morning-reviewer", evidence="re-opened today")
    carried = carry_prior_decisions(stage9, [{"proposal_id": pid}])
    assert carried == []
    assert latest_decisions(stage9)[pid]["decision"] == "defer"
    assert latest_decisions_across(stage9.parent)[pid]["decision"] == "defer"
