"""Dashboard plugin_api — synthetic temp profile only (no real MEMORY)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard" / "plugin_api.py"


def _load_api(monkeypatch, home: Path):
    # Point get_hermes_home at temp profile via env (and a stub module).
    monkeypatch.setenv("HERMES_HOME", str(home))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import os
    import types
    hc = types.ModuleType("hermes_constants")

    def _gh():
        return Path(os.environ["HERMES_HOME"])

    def _root():
        h = _gh()
        return h.parent.parent if h.parent.name == "profiles" else h

    hc.get_hermes_home = _gh
    hc.get_default_hermes_root = _root
    # monkeypatch restores sys.modules after the test, so we don't leak into
    # later tests the way a bare sys.modules[...] assignment would.
    monkeypatch.setitem(sys.modules, "hermes_constants", hc)

    spec = importlib.util.spec_from_file_location("oneiros_dash_api", DASH)
    mod = importlib.util.module_from_spec(spec)
    # Unique module name per home so reloads don't share stale bindings
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    for name in ("ReviewBody", "ApplyBody", "RollbackBody", "ReviewBatchBody", "UndoBatchBody"):
        getattr(mod, name).model_rebuild()
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/dream-cycle")
    return mod, TestClient(app)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    home = root / "profiles" / "alpha"
    (home / "memories").mkdir(parents=True)
    (home / "plugins" / "dream-cycle").mkdir(parents=True)
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    # minimal plugin.yaml for version
    (home / "plugins" / "dream-cycle" / "plugin.yaml").write_text("name: dream-cycle\nversion: 0.5.0\n")
    # copy dashboard pointer not needed — API loads from repo ROOT via _PLUGIN_ROOT
    (home / "memories" / "MEMORY.md").write_text(
        "demo-svc listens on port :18991\nother note endpoint url http://localhost:18992/v1\n"
    )
    (home / "plugin-data" / "dream-cycle" / "config.yaml").write_text("apply_enabled: true\n")
    return home


def test_hygiene_card_reads_output_and_does_not_import_plugin(profile, monkeypatch):
    import inspect
    home = profile
    plug = home / "plugins" / "memory-hygiene"
    (plug / "hygiene").mkdir(parents=True)
    marker = home / "IMPORTED"
    (plug / "hygiene" / "__init__.py").write_text("")
    (plug / "hygiene" / "config.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
    )
    pdata = home / "plugin-data" / "memory-hygiene"
    pdata.mkdir(parents=True, exist_ok=True)
    (pdata / "last-report.json").write_text('{"hit_count": 4, "scanned": ["MEMORY.md"]}\n')
    mod, client = _load_api(monkeypatch, home)
    src = inspect.getsource(mod._hygiene_card)
    assert "sys.path.insert" not in src
    assert "import hygiene" not in src
    assert "find_stale" not in src
    body = client.get("/api/plugins/dream-cycle/overview").json()
    hyg = body["hygiene"]
    assert hyg["installed"] is True
    assert hyg["hit_count"] == 4
    assert hyg["scanned"] == ["MEMORY.md"]
    assert str(plug) not in sys.path
    assert not marker.exists()
    assert "hygiene" not in sys.modules
    assert "hygiene.config" not in sys.modules


def test_hygiene_card_reads_cron_markdown(profile, monkeypatch):
    home = profile
    (home / "plugins" / "memory-hygiene").mkdir(parents=True)
    job = home / "cron" / "output" / "nightly"
    job.mkdir(parents=True)
    (job / "2026-11-11.md").write_text(
        "Memory Hygiene\n\n{\"hit_count\": 3, \"scanned\": [\"USER.md\"]}\n"
    )
    _mod, client = _load_api(monkeypatch, home)
    hyg = client.get("/api/plugins/dream-cycle/overview").json()["hygiene"]
    assert hyg["hit_count"] == 3
    assert hyg["scanned"] == ["USER.md"]
    assert hyg["source"].endswith("2026-11-11.md")


def test_hygiene_card_newer_report_wins_over_older_cron(profile, monkeypatch):
    import os
    import time
    home = profile
    (home / "plugins" / "memory-hygiene").mkdir(parents=True)
    job = home / "cron" / "output" / "nightly"
    job.mkdir(parents=True)
    cron = job / "old.md"
    cron.write_text("Memory Hygiene\n{\"hit_count\": 1, \"scanned\": []}\n")
    pdata = home / "plugin-data" / "memory-hygiene"
    pdata.mkdir(parents=True)
    report = pdata / "last-report.json"
    report.write_text('{"hit_count": 9, "scanned": ["MEMORY.md"]}\n')
    now = time.time()
    os.utime(cron, (now - 50, now - 50))
    os.utime(report, (now, now))
    _mod, client = _load_api(monkeypatch, home)
    hyg = client.get("/api/plugins/dream-cycle/overview").json()["hygiene"]
    assert hyg["hit_count"] == 9
    assert hyg["source"].endswith("last-report.json")


def test_overview_not_installed(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    home = root / "profiles" / "stark"
    home.mkdir(parents=True)
    (root / "profiles" / "alpha").mkdir()
    mod, client = _load_api(monkeypatch, home)
    r = client.get("/api/plugins/dream-cycle/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["installed"] is False
    assert "not installed" in body["message"].lower()
    assert body["oneiros_target_profile"] is True
    assert "alpha" not in body["message"]


def test_empty_allowlist_includes_installed_profile_and_a_list_can_exclude_it(profile, monkeypatch):
    home = profile
    cfg_path = home / "plugin-data/dream-cycle/config.yaml"
    cfg_path.write_text("apply_enabled: true\n")
    _mod, client = _load_api(monkeypatch, home)
    open_all = client.get("/api/plugins/dream-cycle/overview").json()
    assert open_all["installed"] is True
    assert open_all["oneiros_target_profile"] is True
    assert open_all["profile"] == home.name
    cfg_path.write_text("apply_enabled: true\nprofile_allowlist: [beta]\n")
    limited = client.get("/api/plugins/dream-cycle/overview").json()
    assert limited["oneiros_target_profile"] is False
    assert "profile_allowlist" in limited["message"]
    cfg_path.write_text("apply_enabled: true\nprofile_allowlist: []\n")
    again = client.get("/api/plugins/dream-cycle/overview").json()
    assert again["oneiros_target_profile"] is True


def test_accept_apply_rollback_loop(profile, monkeypatch):
    from dream_cycle.propose import propose
    from dream_cycle.sessions import SessionResult
    from dream_cycle.config import load_config

    home = profile
    cfg = load_config(home / "plugin-data/dream-cycle/config.yaml")
    # stage proposals
    propose(home, config=cfg, session_result=SessionResult("no-sessions"))
    m0 = hashlib.md5((home / "memories/MEMORY.md").read_bytes()).hexdigest()

    mod, client = _load_api(monkeypatch, home)
    ov = client.get("/api/plugins/dream-cycle/overview").json()
    assert ov["installed"] is True
    assert ov["gates"]["can_apply"] is True

    props = client.get("/api/plugins/dream-cycle/proposals").json()["proposals"]
    assert props
    pid = props[0]["proposal_id"]
    values = props[0]["values"]
    keep = "18992" if "18992" in values else values[-1]

    # reject then accept path — accept
    rev = client.post("/api/plugins/dream-cycle/review", json={
        "proposal_id": pid, "decision": "accept", "keep": keep,
        "evidence": "synthetic: planted keep is current", "actor_user": "tester",
    })
    assert rev.status_code == 200, rev.text
    assert rev.json()["actor"] == "dashboard:tester"
    # Decided row payload for the dashboard (badge + Change decision)
    props2 = client.get("/api/plugins/dream-cycle/proposals").json()["proposals"]
    row = next(x for x in props2 if x["proposal_id"] == pid)
    assert row["last_decision"] == "accept"
    assert row["last_actor"] == "dashboard:tester"
    assert row.get("last_ts")
    assert row["is_undecided"] is False

    # dry-run apply accepted
    dry = client.post("/api/plugins/dream-cycle/apply", json={"accepted": True, "dry_run": True})
    assert dry.status_code == 200
    assert dry.json()["results"]
    assert dry.json()["results"][0]["status"] == "dry-run"
    assert hashlib.md5((home / "memories/MEMORY.md").read_bytes()).hexdigest() == m0

    # real apply
    ap = client.post("/api/plugins/dream-cycle/apply", json={"accepted": True, "dry_run": False, "actor_user": "tester"})
    assert ap.status_code == 200, ap.text
    assert ap.json()["results"][0]["ok"] is True
    m1 = hashlib.md5((home / "memories/MEMORY.md").read_bytes()).hexdigest()
    assert m1 != m0
    rid = ap.json()["results"][0]["receipt_id"]

    # rollback dry-run + real
    rb_dry = client.post("/api/plugins/dream-cycle/rollback", json={"receipt_id": rid, "dry_run": True})
    assert rb_dry.status_code == 200
    rb = client.post("/api/plugins/dream-cycle/rollback", json={"receipt_id": rid, "dry_run": False, "actor_user": "tester"})
    assert rb.status_code == 200, rb.text
    assert rb.json()["ok"] is True
    assert hashlib.md5((home / "memories/MEMORY.md").read_bytes()).hexdigest() == m0


def test_apply_refused_when_disabled(profile, monkeypatch):
    from dream_cycle.propose import propose
    from dream_cycle.sessions import SessionResult
    from dream_cycle.config import load_config
    from dream_cycle.review import append_review
    from pathlib import Path as P

    home = profile
    cfg = load_config(home / "plugin-data/dream-cycle/config.yaml")
    r = propose(home, config=cfg, session_result=SessionResult("no-sessions"))
    assert r.stage_path, r.skipped_reason
    summ = json.loads(P(r.stage_path, "summary.json").read_text())
    pid = summ["proposals"][0]["proposal_id"]
    vals = summ["proposals"][0]["values"]
    append_review(r.stage_path, pid, "accept", "tester", evidence="synthetic evidence here", keep=vals[-1])
    (home / "plugin-data/dream-cycle/DISABLED").touch()

    mod, client = _load_api(monkeypatch, home)
    ov = client.get("/api/plugins/dream-cycle/overview").json()
    assert ov["gates"]["kill_switch"] is True
    assert ov["gates"]["can_apply"] is False
    # review blocked
    bad = client.post("/api/plugins/dream-cycle/review", json={
        "proposal_id": pid, "decision": "reject", "evidence": "x", "actor_user": "tester",
    })
    assert bad.status_code == 403
    # apply blocked
    ap = client.post("/api/plugins/dream-cycle/apply", json={"accepted": True, "dry_run": False})
    assert ap.status_code == 403


def test_apply_refused_when_apply_enabled_false(profile, monkeypatch):
    (profile / "plugin-data/dream-cycle/config.yaml").write_text("apply_enabled: false\n")
    mod, client = _load_api(monkeypatch, profile)
    ov = client.get("/api/plugins/dream-cycle/overview").json()
    assert ov["gates"]["apply_enabled"] is False
    assert ov["gates"]["can_apply"] is False


def test_overview_with_state_db_session_corroboration(profile, monkeypatch):
    """Fleet config (enabled state.db, no day_command) must not 500/502 overview.

    Same failure mode as the live plugin-host 502 on 2026-11-09: _cfg() → load_config
    raised ConfigError before any proposal work ran.
    """
    (profile / "plugin-data/dream-cycle/config.yaml").write_text(
        "apply_enabled: true\n"
        "session_corroboration:\n"
        "  enabled: true\n"
        "  source: state.db\n"
        "  lookback_days: 3\n"
        "  max_rows: 8000\n"
        "  timeout_s: 30\n"
    )
    mod, client = _load_api(monkeypatch, profile)
    r = client.get("/api/plugins/dream-cycle/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["installed"] is True
    assert body.get("version")


def _stage_props(profile):
    from dream_cycle.propose import propose
    from dream_cycle.sessions import SessionResult
    from dream_cycle.config import load_config
    cfg = load_config(profile / "plugin-data/dream-cycle/config.yaml")
    r = propose(profile, config=cfg, session_result=SessionResult("no-sessions"))
    assert r.stage_path, r.skipped_reason
    return r


def test_accept_allows_empty_evidence_and_does_not_apply(profile, monkeypatch):
    """One-click accept: empty evidence is stored; MEMORY.md is untouched."""
    import hashlib
    from pathlib import Path as P
    home = profile
    mem = home / "memories/MEMORY.md"
    before = hashlib.md5(mem.read_bytes()).hexdigest()
    _stage_props(home)
    _mod, client = _load_api(monkeypatch, home)
    props = client.get("/api/plugins/dream-cycle/proposals").json()["proposals"]
    assert props
    pid = props[0]["proposal_id"]
    keep = props[0]["values"][0]
    rev = client.post("/api/plugins/dream-cycle/review", json={
        "proposal_id": pid, "decision": "accept", "keep": keep,
        "evidence": "", "actor_user": "tester",
    })
    assert rev.status_code == 200, rev.text
    row = rev.json()["row"]
    assert row["decision"] == "accept"
    assert row.get("evidence") == ""
    assert row["keep"] == str(keep)
    assert hashlib.md5(mem.read_bytes()).hexdigest() == before
    # ledger kept the empty evidence
    ledger = list(P(home, "plugin-data/dream-cycle/dream-proposals").glob("*/review.jsonl"))
    assert ledger
    lines = ledger[0].read_text().strip().splitlines()
    assert lines, "expected an appended accept row"
    import json as _json
    last = _json.loads(lines[-1])
    assert last["decision"] == "accept"
    assert last["evidence"] == ""
    assert last["proposal_id"] == pid


def test_accept_all_and_undo_batch_scratch_profile(profile, monkeypatch):
    """Accept all pending, then undo: append-only revert, memory unchanged."""
    import hashlib
    import json as _json
    from pathlib import Path as P
    home = profile
    mem = home / "memories/MEMORY.md"
    before = hashlib.md5(mem.read_bytes()).hexdigest()
    _stage_props(home)
    _mod, client = _load_api(monkeypatch, home)
    props = client.get("/api/plugins/dream-cycle/proposals").json()
    rows = props["proposals"]
    pending = [p for p in rows if p.get("is_undecided")]
    assert len(pending) >= 1
    # reject one first — Accept all must skip decided rows
    decided = pending[0]
    if len(pending) > 1:
        rej = client.post("/api/plugins/dream-cycle/review", json={
            "proposal_id": decided["proposal_id"], "decision": "reject",
            "evidence": "guardian reject", "actor_user": "tester",
        })
        assert rej.status_code == 200, rej.text
        still = [p for p in pending[1:]]
    else:
        still = pending

    body = {
        "proposal_ids": [p["proposal_id"] for p in still],
        "actor_user": "tester",
    }
    acc = client.post("/api/plugins/dream-cycle/review/accept-all", json=body)
    assert acc.status_code == 200, acc.text
    out = acc.json()
    assert out["ok"] is True
    assert out["count"] == len(still)
    assert out["batch_id"] and out["batch_id"].startswith("b")
    assert hashlib.md5(mem.read_bytes()).hexdigest() == before

    # proposals now decided
    after = client.get("/api/plugins/dream-cycle/proposals").json()["proposals"]
    by_id = {p["proposal_id"]: p for p in after}
    for p in still:
        assert by_id[p["proposal_id"]]["last_decision"] == "accept"
        assert by_id[p["proposal_id"]]["is_undecided"] is False
    if len(pending) > 1:
        assert by_id[decided["proposal_id"]]["last_decision"] == "reject"

    # ledger has N accept lines with the batch_id; nothing deleted later
    day = out["day"]
    ledger = P(home, "plugin-data/dream-cycle/dream-proposals", day, "review.jsonl")
    before_undo = ledger.read_text()
    n_before = len(before_undo.strip().splitlines())

    undo = client.post("/api/plugins/dream-cycle/review/undo-batch", json={
        "batch_id": out["batch_id"], "day": day, "actor_user": "tester",
    })
    assert undo.status_code == 200, undo.text
    u = undo.json()
    assert u["count"] == len(still)
    assert hashlib.md5(mem.read_bytes()).hexdigest() == before

    after_undo = ledger.read_text()
    assert after_undo.startswith(before_undo), "ledger must be append-only (no deleted lines)"
    n_after = len(after_undo.strip().splitlines())
    assert n_after == n_before + len(still)
    # original accept rows still present
    assert before_undo.strip() in after_undo

    # those proposals are pending again; a prior reject stays rejected
    final = client.get("/api/plugins/dream-cycle/proposals").json()["proposals"]
    by_id = {p["proposal_id"]: p for p in final}
    for p in still:
        assert by_id[p["proposal_id"]]["last_decision"] == "reverted"
        assert by_id[p["proposal_id"]]["is_undecided"] is True
    if len(pending) > 1:
        assert by_id[decided["proposal_id"]]["last_decision"] == "reject"

    # idempotent second undo: no new reverts
    undo2 = client.post("/api/plugins/dream-cycle/review/undo-batch", json={
        "batch_id": out["batch_id"], "day": day, "actor_user": "tester",
    })
    assert undo2.status_code == 200, undo2.text
    assert undo2.json()["count"] == 0
    assert len(ledger.read_text().strip().splitlines()) == n_after


def test_accept_all_unknown_batch_404(profile, monkeypatch):
    _stage_props(profile)
    _mod, client = _load_api(monkeypatch, profile)
    r = client.post("/api/plugins/dream-cycle/review/undo-batch", json={
        "batch_id": "b" + "ab" * 6, "actor_user": "tester",
    })
    assert r.status_code == 404


def test_accept_all_respects_id_filter(profile, monkeypatch):
    _stage_props(profile)
    _mod, client = _load_api(monkeypatch, profile)
    props = client.get("/api/plugins/dream-cycle/proposals").json()["proposals"]
    pending = [p for p in props if p.get("is_undecided")]
    if len(pending) < 2:
        # plant isn't always two proposals; still assert only the requested id is accepted
        only = pending[0]
        acc = client.post("/api/plugins/dream-cycle/review/accept-all", json={
            "proposal_ids": [only["proposal_id"]], "actor_user": "tester",
        })
        assert acc.status_code == 200, acc.text
        assert acc.json()["count"] == 1
        return
    first, rest = pending[0], pending[1:]
    acc = client.post("/api/plugins/dream-cycle/review/accept-all", json={
        "proposal_ids": [first["proposal_id"]], "actor_user": "tester",
    })
    assert acc.status_code == 200, acc.text
    assert acc.json()["count"] == 1
    after = {p["proposal_id"]: p for p in
             client.get("/api/plugins/dream-cycle/proposals").json()["proposals"]}
    assert after[first["proposal_id"]]["last_decision"] == "accept"
    for p in rest:
        assert after[p["proposal_id"]]["is_undecided"] is True


def test_apply_blank_evidence_accepts_rollback_and_skips_the_rest(profile, monkeypatch):
    """Accept with no evidence, apply that accept, roll it back. Scratch profile only."""
    import hashlib
    home = profile
    mistakes = home / "memories" / "MISTAKES.md"
    mistakes.write_text(
        "demo-svc llama build b12000 is the live driver\n"
        "demo-svc llama cpp build b12500 is the new driver\n"
    )
    mem = home / "memories" / "MEMORY.md"
    before_mem = mem.read_text()
    before_mis = mistakes.read_text()
    _stage_props(home)
    _mod, client = _load_api(monkeypatch, home)
    props = client.get("/api/plugins/dream-cycle/proposals").json()["proposals"]
    assert len(props) >= 2
    first = props[0]
    # direct apply with blank evidence is ineligible until a human accepts
    early = client.post("/api/plugins/dream-cycle/apply", json={
        "proposal_id": first["proposal_id"], "keep": first["values"][0],
        "evidence": "", "actor_user": "tester",
    })
    assert early.status_code == 200, early.text
    assert early.json()["ok"] is False
    assert "not accepted" in early.json()["result"]["message"]
    assert hashlib.md5(mem.read_bytes()).hexdigest() == hashlib.md5(before_mem.encode()).hexdigest() or mem.read_text() == before_mem

    acc = client.post("/api/plugins/dream-cycle/review", json={
        "proposal_id": first["proposal_id"], "decision": "accept",
        "keep": first["values"][0], "evidence": "", "actor_user": "tester",
    })
    assert acc.status_code == 200, acc.text
    assert acc.json()["row"]["evidence"] == ""

    dry = client.post("/api/plugins/dream-cycle/apply", json={
        "accepted": True, "dry_run": True, "actor_user": "tester",
    })
    assert dry.status_code == 200, dry.text
    blob = dry.text
    assert "dashboard dry-run" not in blob
    results = dry.json()["results"]
    assert len(results) == 1 and results[0]["status"] == "dry-run"
    assert results[0]["proposal_id"] == first["proposal_id"]
    assert mem.read_text() == before_mem and mistakes.read_text() == before_mis

    ap = client.post("/api/plugins/dream-cycle/apply", json={
        "accepted": True, "dry_run": False, "actor_user": "tester",
    })
    assert ap.status_code == 200, ap.text
    assert ap.json()["results"][0]["ok"] is True
    changed = mem.read_text() != before_mem or mistakes.read_text() != before_mis
    assert changed
    for path, before in ((mem, before_mem), (mistakes, before_mis)):
        for line in path.read_text().splitlines():
            if line.startswith("SUPERSEDED"):
                assert "because" not in line
                assert "dashboard dry-run" not in line

    recs = client.get("/api/plugins/dream-cycle/receipts").json()["receipts"]
    assert recs and recs[0]["evidence"] == ""
    for rec in recs:
        if rec.get("rolled_back_at"):
            continue
        rb = client.post("/api/plugins/dream-cycle/rollback", json={
            "receipt_id": rec["receipt_id"], "actor_user": "tester",
        })
        assert rb.status_code == 200 and rb.json()["ok"], rb.text
    assert mem.read_text() == before_mem
    assert mistakes.read_text() == before_mis


def test_accept_all_blank_then_revert_is_not_applied(profile, monkeypatch):
    home = profile
    (home / "memories" / "MISTAKES.md").write_text(
        "demo-svc llama build b12000 is the live driver\n"
        "demo-svc llama cpp build b12500 is the new driver\n"
    )
    mem = home / "memories" / "MEMORY.md"
    mis = home / "memories" / "MISTAKES.md"
    before = (mem.read_text(), mis.read_text())
    _stage_props(home)
    _mod, client = _load_api(monkeypatch, home)
    acc = client.post("/api/plugins/dream-cycle/review/accept-all", json={"actor_user": "tester"})
    assert acc.status_code == 200, acc.text
    body = acc.json()
    assert body["count"] >= 2
    assert all(row.get("evidence") == "" for row in body["rows"])
    undo = client.post("/api/plugins/dream-cycle/review/undo-batch", json={
        "batch_id": body["batch_id"], "day": body["day"], "actor_user": "tester",
    })
    assert undo.status_code == 200 and undo.json()["count"] == body["count"]
    ledger = (home / "plugin-data/dream-cycle/dream-proposals" / body["day"] / "review.jsonl").read_text()
    assert ledger.count('"decision": "accept"') == body["count"]
    assert ledger.count('"decision": "reverted"') == body["count"]
    ap = client.post("/api/plugins/dream-cycle/apply", json={
        "accepted": True, "dry_run": False, "actor_user": "tester",
    })
    assert ap.status_code == 200, ap.text
    assert ap.json()["results"] == []
    assert (mem.read_text(), mis.read_text()) == before


def _write_stage(home, day, pid, note):
    stage = home / "plugin-data/dream-cycle/dream-proposals" / day
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "summary.json").write_text(json.dumps({
        "proposals": [{
            "proposal_id": pid, "label": "port", "values": ["1", "2"],
            "file": "MEMORY.md", "line_nos": [1], "note": note,
        }]
    }))
    return stage


def test_deep_link_selects_profile_and_day(profile, monkeypatch):
    """?profile=&day= loads that profile's stage. GET does not snooze."""
    home = profile
    other = home.parent / "otherbot"
    (other / "plugin-data" / "dream-cycle").mkdir(parents=True)
    (other / "memories").mkdir()
    _write_stage(home, "2026-11-08", "aaaa1111bbbb", "november dream")
    _write_stage(home, "2026-10-01", "cccc2222dddd", "october dream")
    _write_stage(other, "2026-10-01", "eeee3333ffff", "other profile dream")
    _mod, client = _load_api(monkeypatch, home)
    name = home.name
    got = client.get(f"/api/plugins/dream-cycle/proposals?profile={name}&day=2026-10-01")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["day"] == "2026-10-01"
    assert [p["proposal_id"] for p in body["proposals"]] == ["cccc2222dddd"]
    assert "october dream" in body["proposals"][0]["note"]
    other_got = client.get("/api/plugins/dream-cycle/proposals?profile=otherbot&day=2026-10-01")
    assert other_got.status_code == 200
    assert [p["proposal_id"] for p in other_got.json()["proposals"]] == ["eeee3333ffff"]
    missing = client.get("/api/plugins/dream-cycle/proposals?profile=no-such")
    assert missing.status_code == 404
    status = client.get(f"/api/plugins/dream-cycle/reminder?profile={name}")
    assert status.status_code == 200
    assert status.json()["pending_review"] >= 1
    before = status.json().get("snoozed_until")
    nope = client.get(f"/api/plugins/dream-cycle/reminder/snooze?profile={name}")
    assert nope.status_code == 405
    assert client.get(f"/api/plugins/dream-cycle/reminder?profile={name}").json().get("snoozed_until") == before
    snoozed = client.post(f"/api/plugins/dream-cycle/reminder/snooze?profile={name}", json={})
    assert snoozed.status_code == 200, snoozed.text
    assert snoozed.json()["snooze_active"] is True
    assert snoozed.json()["profile"] == name
    assert snoozed.json()["snoozed_until"]
    # Snooze did not write memory.
    assert (home / "memories" / "MEMORY.md").read_text().startswith("demo-svc")
