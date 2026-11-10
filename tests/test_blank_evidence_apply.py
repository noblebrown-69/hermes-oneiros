"""Blank evidence is enough after an explicit accept. Scratch fixtures only.

Apply still refuses unaccepted / reverted rows, stale lines, conflicting
writes, secrets, and unverified values when grounding is required.
"""
import json
from datetime import date

import dream_cycle.apply as A
from dream_cycle.apply import apply_accepted, apply_proposal, list_receipts, rollback, sha256
from dream_cycle.config import default_config
from dream_cycle.propose import propose
from dream_cycle.review import accept_pending, append_review, read_reviews, revert_batch
from dream_cycle.sessions import SessionResult
from conftest import write_mem

NOSESS = SessionResult("disabled")
COST = {"status": "unavailable", "detail": "test"}
DAY = date(2026, 11, 8)
STAGE = "plugin-data/dream-cycle/dream-proposals/2026-11-08"

MEM = "demo-svc listens on port :18991\ndemo-svc endpoint url http://localhost:18992/v1\n"
BUILD = ("demo-svc llama build b12000 is the live driver\n"
         "demo-svc llama cpp build b12500 is the new driver\n")
RATE = ("demo-svc throughput 12 tok/s on the box\n"
        "demo-svc throughput 40 tok/s after the change\n")


def _cfg(**kw):
    c = default_config()
    c["apply_enabled"] = True
    c["memory_globs"] = ["MEMORY.md", "MISTAKES.md", "NOTES.md"]
    c.update(kw)
    return c


def _plant(home):
    files = {
        "MEMORY.md": MEM,
        "MISTAKES.md": BUILD,
        "NOTES.md": RATE,
    }
    for name, text in files.items():
        write_mem(home, text, name)
    propose(home, config=_cfg(), run_date=DAY, session_result=NOSESS, cost_result=COST)
    stage = home / STAGE
    props = json.loads((stage / "summary.json").read_text())["proposals"]
    assert len(props) == 3, [p["label"] for p in props]
    return stage, props


def _accept(stage, prop, evidence=""):
    keep = prop["values"][0]
    return append_review(stage, prop["proposal_id"], "accept", "guardian",
                         evidence=evidence, keep=keep)


def test_individual_blank_accept_applies_and_rolls_back(home):
    stage, props = _plant(home)
    prop = next(p for p in props if p["label"] == "port")
    target = home / "memories" / "MEMORY.md"
    before = target.read_text()
    _accept(stage, prop, evidence="")
    # The stored accept is blank. Apply must not ask for a new accept.
    out = apply_accepted(home, config=_cfg(), day="2026-11-08")
    mine = [r for r in out if r["proposal_id"] == prop["proposal_id"]]
    assert len(mine) == 1 and mine[0]["status"] == "applied", mine
    after = target.read_text()
    assert after != before
    marker = next(ln for ln in after.splitlines() if ln.startswith("SUPERSEDED"))
    assert "because" not in marker
    assert "[dream-cycle " in marker
    assert "dashboard dry-run" not in after
    rec = json.loads((home / "plugin-data/dream-cycle/receipts" / f"{mine[0]['receipt_id']}.json").read_text())
    assert rec["evidence"] == ""
    applied = [r for r in read_reviews(stage) if r["decision"] == "applied"]
    assert applied and applied[-1]["evidence"] == ""
    # original accept row is still in the append-only ledger
    assert any(r["decision"] == "accept" and r["evidence"] == "" for r in read_reviews(stage))
    rb = rollback(home, receipt_id=mine[0]["receipt_id"], config=_cfg(), actor="guardian")
    assert rb.ok and target.read_text() == before
    assert read_reviews(stage)[-1]["decision"] == "rolled-back"


def test_keep_must_match_the_blank_accept(home):
    stage, props = _plant(home)
    prop = next(p for p in props if p["label"] == "port")
    _accept(stage, prop, evidence="")
    other = prop["values"][1]
    r = apply_proposal(home, prop["proposal_id"], other, "", "guardian", config=_cfg(), today=DAY)
    assert r.status == "refused" and "does not match" in r.message
    assert (home / "memories" / "MEMORY.md").read_text() == MEM


def test_batch_blank_accept_applies_and_rolls_back(home):
    stage, props = _plant(home)
    originals = {name: (home / "memories" / name).read_text()
                 for name in ("MEMORY.md", "MISTAKES.md", "NOTES.md")}
    out = accept_pending(stage, props, "guardian", evidence="", note="accept-all")
    assert out["count"] == 3 and out["batch_id"]
    applied = apply_accepted(home, config=_cfg(), day="2026-11-08")
    assert len(applied) == 3
    assert all(r["status"] == "applied" and r["ok"] for r in applied), applied
    for name, before in originals.items():
        assert (home / "memories" / name).read_text() != before
    for rec in reversed(list_receipts(home)):
        rb = rollback(home, receipt_id=rec["receipt_id"], config=_cfg(), actor="guardian")
        assert rb.ok, rb.message
    for name, before in originals.items():
        assert (home / "memories" / name).read_text() == before
    decisions = [r["decision"] for r in read_reviews(stage)]
    assert decisions.count("accept") == 3
    assert decisions.count("applied") == 3
    assert decisions.count("rolled-back") == 3


def test_unaccepted_and_reverted_are_not_eligible(home):
    stage, props = _plant(home)
    port = next(p for p in props if p["label"] == "port")
    others = [p for p in props if p is not port]
    _accept(stage, port, evidence="")
    out = apply_accepted(home, config=_cfg(), day="2026-11-08")
    assert [r["proposal_id"] for r in out] == [port["proposal_id"]]
    assert (home / "memories" / "MISTAKES.md").read_text() == BUILD
    assert (home / "memories" / "NOTES.md").read_text() == RATE
    # a direct blank apply of a never-accepted proposal is refused
    refused = apply_proposal(home, others[0]["proposal_id"], others[0]["values"][0], "",
                             "guardian", config=_cfg(), today=DAY)
    assert refused.status == "refused" and "not accepted" in refused.message

    batch = accept_pending(stage, others, "guardian", evidence="")
    assert batch["count"] == 2
    undone = revert_batch(stage, batch["batch_id"], "guardian")
    assert undone["count"] == 2
    # ledger still contains the accept rows
    text = (stage / "review.jsonl").read_text()
    assert text.count('"decision": "accept"') >= 3
    assert text.count('"decision": "reverted"') == 2
    again = apply_accepted(home, config=_cfg(), day="2026-11-08")
    # port is already applied; reverted rows are not accepts
    assert again == []
    blank = apply_proposal(home, others[0]["proposal_id"], others[0]["values"][0], " ",
                           "guardian", config=_cfg(), today=DAY)
    assert blank.status == "refused" and "not accepted" in blank.message
    assert (home / "memories" / "MISTAKES.md").read_text() == BUILD


def test_stale_and_conflicting_writes_still_refused(home, monkeypatch):
    stage, props = _plant(home)
    prop = next(p for p in props if p["label"] == "port")
    target = home / "memories" / "MEMORY.md"
    _accept(stage, prop, evidence="")
    target.write_text(MEM.replace(":18991", ":18995"))
    stale = apply_accepted(home, config=_cfg(), day="2026-11-08")
    assert stale and stale[0]["status"] == "refused" and "stale" in stale[0]["message"]
    assert target.read_text() == MEM.replace(":18991", ":18995")
    assert read_reviews(stage)[-1]["decision"] == "apply-refused"

    target.write_text(MEM)
    # re-propose so the accept's proposal matches the restored file
    propose(home, config=_cfg(), run_date=DAY, session_result=NOSESS, cost_result=COST)
    props = json.loads((stage / "summary.json").read_text())["proposals"]
    prop = next(p for p in props if p["label"] == "port")
    _accept(stage, prop, evidence="")
    monkeypatch.setattr(A, "_pre_write_hook", lambda path: path.write_text(path.read_text() + "rogue\n"))
    clash = apply_proposal(home, prop["proposal_id"], prop["values"][0], "", "guardian",
                           config=_cfg(), today=DAY)
    assert clash.status == "refused" and "changed since read" in clash.message
    assert target.read_text() == MEM + "rogue\n"
    assert read_reviews(stage)[-1]["decision"] == "apply-refused"
    assert sha256(target.read_text()) != sha256(MEM)


def test_blank_evidence_does_not_skip_secret_or_grounding(home):
    text = "port :18991 api_key=" + "x" * 12 + "\nendpoint port :18992\n"
    target = write_mem(home, text)
    cfg = _cfg()
    propose(home, config=cfg, run_date=DAY, session_result=NOSESS, cost_result=COST)
    stage = home / STAGE
    prop = json.loads((stage / "summary.json").read_text())["proposals"][0]
    # Keep the other value so the line that carries the secret is the one rewritten.
    append_review(stage, prop["proposal_id"], "accept", "guardian", evidence="", keep="18992")
    secret = apply_accepted(home, config=cfg, day="2026-11-08")
    assert secret and secret[0]["status"] == "refused" and "secret" in secret[0]["message"]
    assert target.read_text() == text

    target.write_text(MEM)
    propose(home, config=_cfg(require_grounded_evidence=True), run_date=DAY,
            session_result=NOSESS, cost_result=COST)
    prop = next(p for p in json.loads((stage / "summary.json").read_text())["proposals"]
                if p["label"] == "port")
    _accept(stage, prop, evidence="")
    grounded = apply_accepted(home, config=_cfg(require_grounded_evidence=True), day="2026-11-08")
    mine = next(r for r in grounded if r["proposal_id"] == prop["proposal_id"])
    assert mine["status"] == "refused" and "unverified" in mine["message"]
    assert (home / "memories" / "MEMORY.md").read_text() == MEM
