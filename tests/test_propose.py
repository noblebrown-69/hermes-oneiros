import json
from datetime import date
from pathlib import Path

import pytest

from dream_cycle.errors import LockBusy
from dream_cycle.paths import acquire_lock, release_lock, resolve_memory_files
from dream_cycle.propose import propose
from conftest import write_mem


def test_propose_stages(home):
    write_mem(home, "a on :1111\nb on :2222\n")
    res = propose(home)
    assert res.stage_path and Path(res.stage_path, "summary.json").exists()
    assert res.proposals and res.proposals[0].values == ["1111", "2222"]


def test_summary_says_no_model(home):
    write_mem(home, "a on :1111\n")
    res = propose(home)
    s = json.loads(Path(res.stage_path, "summary.json").read_text())
    assert s["model_used"] is False and s["suite"] == "Oneiros Dream Cycle"


def test_disabled(home):
    d = home / "plugin-data" / "dream-cycle"; d.mkdir(parents=True)
    (d / "DISABLED").write_text("")
    write_mem(home, "x :1\ny :2\n")
    res = propose(home)
    assert res.skipped_reason == "DISABLED" and res.stage_path is None


def test_no_conflict(home):
    write_mem(home, "only one port :9119\n")
    res = propose(home)
    assert res.proposals == []
    assert "(no candidates)" in Path(res.stage_path, "diff.md").read_text()


def test_memory_never_modified(home):
    p = write_mem(home, "a :1111\nb :2222\n")
    before = p.read_bytes()
    propose(home)
    assert p.read_bytes() == before


def test_cap_and_overflow(home):
    cfg_text = "".join(f"svc{i} port :{1000 + i}\nother{i} build b{10000 + i}\n" for i in range(3))
    write_mem(home, cfg_text)
    from dream_cycle.config import default_config
    c = default_config(); c["max_proposals_per_run"] = 1
    res = propose(home, config=c)
    s = json.loads(Path(res.stage_path, "summary.json").read_text())
    assert len(res.proposals) == 1 and s["overflow"] >= 1


def test_stage_folder_named_by_date(home):
    write_mem(home, "x\n")
    res = propose(home, run_date=date(2030, 5, 6))
    assert res.stage_path.endswith("dream-proposals/2030-05-06")


def test_rerun_same_day_overwrites(home):
    write_mem(home, "a :1111\nb :2222\n")
    propose(home, run_date=date(2030, 5, 6))
    write_mem(home, "a :1111\n")
    res = propose(home, run_date=date(2030, 5, 6))
    assert json.loads(Path(res.stage_path, "summary.json").read_text())["proposals"] == []


def test_mistakes_file_scanned(home):
    write_mem(home, "a :1111\nb :2222\n", name="MISTAKES.md")
    assert propose(home).proposals[0].file.endswith("MISTAKES.md")


def test_hidden_dirs_skipped(home):
    hidden = home / "memories" / ".staged"; hidden.mkdir()
    (hidden / "MEMORY.md").write_text("a :1\nb :2\n")
    assert resolve_memory_files([home / "memories"], ["MEMORY.md"]) == []


def test_explicit_memory_roots(home, tmp_path):
    other = tmp_path / "elsewhere"; other.mkdir()
    (other / "MEMORY.md").write_text("a :1111\nb :2222\n")
    assert propose(home, memory_roots=[other]).proposals


def test_lock_busy(home, monkeypatch):
    lock = home / "plugin-data" / "dream-cycle" / "dream-cycle.lock"
    fd = acquire_lock(lock)
    try:
        import importlib
        P = importlib.import_module("dream_cycle.propose")
        monkeypatch.setattr(P, "acquire_lock", lambda p: acquire_lock(p, timeout_s=0))
        with pytest.raises(LockBusy, match="Another Dream Cycle run"):
            propose(home)
    finally:
        release_lock(fd)


def test_lock_released_after_run(home):
    write_mem(home, "x\n")
    propose(home)
    fd = acquire_lock(home / "plugin-data" / "dream-cycle" / "dream-cycle.lock", timeout_s=0)
    release_lock(fd)



def test_home_under_hidden_dir_is_scanned(tmp_path):
    """Regression 2026-11-08: HERMES_HOME under ~/.hermes must still be scanned."""
    from dream_cycle.paths import resolve_memory_files
    h = tmp_path / ".hermes" / "profiles" / "x"
    (h / "memories" / ".consolidation-staged").mkdir(parents=True)
    (h / "memories" / "MEMORY.md").write_text("port :18991\n")
    (h / "memories" / ".consolidation-staged" / "MEMORY.md").write_text("hidden\n")
    files = resolve_memory_files([h / "memories", h], ["MEMORY.md"])
    assert [f.name for f in files] == ["MEMORY.md"] and ".consolidation-staged" not in str(files[0])


def test_snapshots_and_plugin_data_not_scanned(home):
    """Regression 2026-11-08: apply snapshots under plugin-data must not be re-proposed."""
    from dream_cycle.paths import resolve_memory_files
    (home / "memories" / "MEMORY.md").write_text("port :18992\n")
    snap = home / "plugin-data" / "dream-cycle" / "snapshots" / "r1"
    snap.mkdir(parents=True); (snap / "MEMORY.md").write_text("port :18991\n")
    (home / "MEMORY.md").write_text("top-level ok\n")
    files = resolve_memory_files([home / "memories", home], ["MEMORY.md"], shallow_roots=[home])
    assert sorted(str(f.relative_to(home)) for f in files) == ["MEMORY.md", "memories/MEMORY.md"]
