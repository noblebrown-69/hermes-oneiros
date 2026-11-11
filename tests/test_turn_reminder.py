"""Registration surface and pre_llm_call semantics. No live session, no send."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from hermes_yaml import safe_load

from dream_cycle.remind import review_snapshot
from test_plugin import _load

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)
BASE = "https://dash.example/hermes"


class HermesCtx:
    """The registration methods Hermes v0.23.0 actually calls."""

    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.prompts = {}
        self.commands = {}
        self.cli = {}
        self.skills = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"schema": schema, "handler": handler, **kw}

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = handler_fn

    def register_skill(self, name, path, description=""):
        self.skills[name] = path

    def register_hook(self, name, callback, **kwargs):
        self.hooks[name] = callback

    def register_system_prompt_section(self, id, content, **kwargs):
        self.prompts[id] = content


def _stage(home: Path):
    (home / "memories").mkdir(parents=True, exist_ok=True)
    (home / "memories" / "MEMORY.md").write_text("scratch\n")
    cfg_dir = home / "plugin-data" / "dream-cycle"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "apply_enabled: false\n"
        "review_reminder:\n"
        "  enabled: true\n"
        "  overdue_days: 7\n"
        "  cooldown_hours: 24\n"
        "  snooze_days: 7\n"
        f"  dashboard_base_url: {BASE}\n"
    )
    stage = cfg_dir / "dream-proposals" / "2026-11-01"
    stage.mkdir(parents=True)
    (stage / "summary.json").write_text(json.dumps({
        "proposals": [{
            "proposal_id": "aaaa1111bbbb", "label": "port", "values": ["1", "2"],
            "file": "MEMORY.md", "line_nos": [1], "note": "old dream",
        }]
    }))


def _register(monkeypatch):
    plugin = _load()
    ctx = HermesCtx()
    plugin.register(ctx)
    return plugin, ctx


def test_manifest_lists_registered_tools_and_prompt(monkeypatch):
    plugin, ctx = _register(monkeypatch)
    manifest = safe_load((ROOT / "plugin.yaml").read_text())
    assert set(manifest["provides_tools"]) == set(ctx.tools)
    assert "dream_cycle_review_status" in ctx.tools
    assert "dream_cycle_snooze" in ctx.tools
    assert manifest["provides_hooks"] == ["pre_llm_call", "on_session_open"]
    assert set(manifest["provides_hooks"]) == set(ctx.hooks)
    assert manifest.get("python_dependencies") in ([], None)
    assert manifest["version"] == "0.8.8"
    prompt = ctx.prompts["dream-cycle.review"]
    assert "dream_cycle_review_status" in prompt
    assert "dream_cycle_snooze" in prompt
    assert "never invent" in prompt
    # The tool schemas the host stores are the ones register() passed.
    assert ctx.tools["dream_cycle_review_status"]["schema"]["name"] == "dream_cycle_review_status"
    assert ctx.tools["dream_cycle_snooze"]["schema"]["name"] == "dream_cycle_snooze"


def test_register_hooks_without_importing_hermes(monkeypatch):
    """Unknown hooks are registered anyway. Hermes logs a warning; we do not probe it."""
    import builtins
    plugin = _load()
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "hermes_cli.plugins" or (isinstance(name, str) and name.startswith("hermes_cli")):
            raise ImportError("no hermes")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    ctx = HermesCtx()
    plugin.register(ctx)
    assert "pre_llm_call" in ctx.hooks
    assert "on_session_open" in ctx.hooks
    assert "VALID_HOOKS" not in Path(plugin.__file__).read_text()


def test_pre_llm_call_only_private_owner_turns(home, monkeypatch):
    home = Path(home)
    _stage(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _plugin, ctx = _register(monkeypatch)
    hook = ctx.hooks["pre_llm_call"]

    assert hook(platform="telegram", chat_type="group", sender_id="owner") is None
    assert hook(platform="telegram", chat_type="", sender_id="owner") is None
    assert hook(platform="desktop", chat_type="", parent_session_id="parent") is None
    state = home / "plugin-data" / "dream-cycle" / "review-reminder.json"
    assert not state.is_file()

    age = (datetime.now(timezone.utc).date() - datetime(2026, 11, 1, tzinfo=timezone.utc).date()).days
    assert age > 7
    for platform in ("", "cli", "tui", "desktop", "dashboard", "api_server"):
        if state.is_file():
            state.unlink()
        result = hook(platform=platform, chat_type="", session_id="s", user_message="hi")
        assert isinstance(result, dict) and "context" in result, platform
        assert f"waiting {age} days" in result["context"]
        assert "profile=default" in result["context"]
        assert "day=2026-11-01" in result["context"]
        assert "snooze" in result["context"].lower()
        # Same turn surface again is inside the 24h cap.
        assert hook(platform=platform, chat_type="") is None

    state.unlink()
    dm = hook(platform="telegram", chat_type="dm", sender_id="owner", chat_id="1")
    assert dm and f"waiting {age} days" in dm["context"]

    state.unlink()
    from dream_cycle.config import load_config
    from dream_cycle.remind import snooze
    cfg = load_config(home / "plugin-data" / "dream-cycle" / "config.yaml")
    snooze(home, cfg, NOW)
    assert hook(platform="api_server", chat_type="") is None
    live = review_snapshot(home, cfg, NOW)
    assert live["pending_review"] == 1 and live["overdue_count"] == 1
    assert live["snooze_active"] is True
    assert live["dashboard_url"]
