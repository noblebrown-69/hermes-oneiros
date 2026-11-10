import argparse
import json

import importlib.util
from pathlib import Path

from conftest import FakeCtx, write_mem

ROOT = Path(__file__).resolve().parents[1]


def _load():
    import sys
    spec = importlib.util.spec_from_file_location(
        "dc_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    m = importlib.util.module_from_spec(spec); sys.modules["dc_plugin"] = m
    spec.loader.exec_module(m)
    return m


def test_register_surfaces():
    ctx = FakeCtx(); _load().register(ctx)
    assert set(ctx.tools) == {"dream_cycle_propose", "dream_cycle_status", "dream_cycle_apply",
                              "dream_cycle_rollback", "dream_cycle_receipts",
                              "dream_cycle_review_status", "dream_cycle_snooze"}
    assert "dream-cycle" in ctx.commands and "dream-cycle" in ctx.cli and "dream-cycle" in ctx.skills


def test_cli_parses_and_runs(home, capsys):
    ctx = FakeCtx(); _load().register(ctx)
    setup, handler = ctx.cli["dream-cycle"]
    p = argparse.ArgumentParser(); setup(p)
    write_mem(home, "a :1111\nb :2222\n")
    handler(p.parse_args(["propose", "--date", "2030-02-02"]))
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["count"] == 1 and out["stage_path"].endswith("2030-02-02")


def test_cli_status(home, capsys):
    ctx = FakeCtx(); _load().register(ctx)
    setup, handler = ctx.cli["dream-cycle"]
    p = argparse.ArgumentParser(); setup(p)
    handler(p.parse_args(["status"]))
    assert json.loads(capsys.readouterr().out)["ok"]


def test_slash_unknown(home):
    assert "Unknown subcommand" in _load().handle_slash("explode")


def test_tool_bad_date(home):
    from plugin_tools import tool_dream_cycle_propose
    out = json.loads(tool_dream_cycle_propose(date="tomorrow"))
    assert out["ok"] is False and "YYYY-MM-DD" in out["error"]


def test_tool_bad_config_is_friendly(home):
    from plugin_tools import tool_dream_cycle_status
    d = home / "plugin-data/dream-cycle"; d.mkdir(parents=True)
    (d / "config.yaml").write_text("labels: [{name: x, pattern: '('}]\n")
    out = json.loads(tool_dream_cycle_status())
    assert out["ok"] is False and "bad regex" in out["error"]


def test_tool_propose_ok(home):
    from plugin_tools import tool_dream_cycle_propose
    write_mem(home, "a :1111\nb :2222\n")
    out = json.loads(tool_dream_cycle_propose())
    assert out["ok"] and out["model_used"] is False and out["session_status"] == "disabled"
