"""on_session_open warm-cache hook — always registered, never raises."""
from __future__ import annotations

import json
from pathlib import Path

import plugin_tools  # noqa: F401 — ensure package path
import __init__ as plugin  # type: ignore
from dream_cycle.session_warm import warm_session_caches


class FakeCtx:
    def __init__(self):
        self.hooks = {}
        self.tools = {}
        self.commands = {}
        self.cli = {}

    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw

    def register_command(self, *a, **kw):
        self.commands[a[0] if a else kw.get("name")] = kw

    def register_cli_command(self, **kw):
        self.cli[kw["name"]] = kw

    def register_skill(self, *a, **kw):
        pass

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb


def test_warm_session_caches_writes_fast(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    (home / "plugin-data" / "memory-hygiene").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("demo listens on port :1111\n")
    (home / "plugin-data" / "dream-cycle" / "config.yaml").write_text("apply_enabled: false\n")
    (home / "plugin-data" / "memory-hygiene" / "last-report.json").write_text(
        json.dumps({"hit_count": 2}) + "\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    out = warm_session_caches(session_id="s1", platform="cli", reason="create", home=home)
    assert out["ok"] is True
    assert out["elapsed_ms"] < 5000
    assert out["reason"] == "create"
    assert out["hygiene"] is not None
    assert out["hygiene"]["hit_count"] == 2
    cache = home / "plugin-data" / "dream-cycle" / "session-warm.json"
    assert cache.is_file()
    body = json.loads(cache.read_text())
    assert body["session_id"] == "s1"
    assert body["platform"] == "cli"


def test_warm_session_caches_survives_missing_home(tmp_path):
    # Synthetic empty home — still returns a dict, never raises
    home = tmp_path / "empty"
    home.mkdir()
    out = warm_session_caches(session_id="x", platform="desktop", reason="resume", home=home)
    assert isinstance(out, dict)
    assert "elapsed_ms" in out
    assert out["elapsed_ms"] < 5000


def test_on_session_open_callback_signature_and_never_raises(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("x\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    cb = plugin._make_on_session_open()
    # Exact docs signature: session_id, platform, reason, **kwargs
    cb(session_id="abc", platform="tui", reason="resume", extra="ignored")
    cb("sid", "cli", "create")  # positional
    cb()  # empty kwargs ok


def test_register_hooks_unconditionally():
    ctx = FakeCtx()
    plugin.register(ctx)
    assert "on_session_open" in ctx.hooks
    assert "pre_llm_call" in ctx.hooks
    assert "dream_cycle_status" in ctx.tools
    ctx.hooks["on_session_open"](session_id="z", platform="acp", reason="create")


def test_register_survives_rejected_hook():
    class Boom(FakeCtx):
        def register_hook(self, name, cb, **kw):
            raise RuntimeError("unknown hook")

    ctx = Boom()
    plugin.register(ctx)
    assert "dream_cycle_status" in ctx.tools
    assert ctx.hooks == {}
