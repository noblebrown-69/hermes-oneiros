"""Hermes calls tool handlers as handler(args_dict, **kwargs) (tools/registry.py)."""
import importlib.util, json, sys
from pathlib import Path
from conftest import FakeCtx

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("dc_plugin_dispatch", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m); return m


def test_registry_style_calls(home):
    ctx = FakeCtx(); _load().register(ctx)
    for name in ("dream_cycle_status", "dream_cycle_receipts"):
        assert json.loads(ctx.tools[name]({}, task_id="t1"))["ok"] is True
    out = json.loads(ctx.tools["dream_cycle_apply"]({"proposal_id": "x", "keep": "1", "dry_run": True}, task_id="t"))
    assert out["status"] == "refused"  # no stage yet, but the args reached the tool
    assert json.loads(ctx.tools["dream_cycle_status"]())["ok"] is True  # keyword style still works


def test_context_override_home(tmp_path, monkeypatch):
    import types
    monkeypatch.delenv("HERMES_HOME", raising=False)
    fake = types.ModuleType("hermes_constants"); fake.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    m = _load()
    import plugin_tools
    assert plugin_tools._home() == tmp_path
