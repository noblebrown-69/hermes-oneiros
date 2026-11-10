"""on_session_open warm-cache hook — feature-detect, never raises (v0.6.0)."""
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


def test_register_feature_detects_missing_hook(monkeypatch):
    ctx = FakeCtx()
    monkeypatch.setattr(plugin, "_on_session_open_supported", lambda: False)
    plugin.register(ctx)
    assert "on_session_open" not in ctx.hooks
    assert "dream_cycle_status" in ctx.tools  # rest of plugin still works


def test_register_adds_hook_when_supported(monkeypatch):
    ctx = FakeCtx()
    monkeypatch.setattr(plugin, "_on_session_open_supported", lambda: True)
    plugin.register(ctx)
    assert "on_session_open" in ctx.hooks
    # Invoke registered callback — must not raise
    ctx.hooks["on_session_open"](session_id="z", platform="acp", reason="create")


def test_on_session_open_supported_false_without_hermes(monkeypatch):
    # Import failure / missing VALID_HOOKS → False (safe on older hosts)
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "hermes_cli.plugins" or name.startswith("hermes_cli"):
            raise ImportError("no hermes")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert plugin._on_session_open_supported() is False
