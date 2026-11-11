"""YAML reads go through hermes_yaml, which Hermes ships. PyYAML does not."""
from __future__ import annotations

import sys
import types
from pathlib import Path

from dream_cycle.apply import _char_limit
from dream_cycle.config import default_config
from dream_cycle.user_facts import char_limits_from_home


def test_runtime_sources_do_not_import_pyyaml():
    root = Path(__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        if "tests" in path.parts or ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert "import yaml" not in text, path
        assert "yaml.safe_load" not in text, path


def test_char_limits_and_dashboard_version_use_hermes_yaml(tmp_path, monkeypatch):
    seen = []
    mod = types.ModuleType("hermes_yaml")

    class YAMLError(Exception):
        pass

    def safe_load(text):
        seen.append(text)
        if "memory_char_limit" in text:
            return {"memory": {"memory_char_limit": 1234, "user_char_limit": 4321}}
        if "version:" in text:
            return {"name": "dream-cycle", "version": "9.9.9"}
        return {}

    mod.safe_load = safe_load
    mod.YAMLError = YAMLError
    monkeypatch.setitem(sys.modules, "hermes_yaml", mod)

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "memory:\n  memory_char_limit: 1234\n  user_char_limit: 4321\n"
    )
    cfg = default_config()
    assert _char_limit(home, cfg, home / "MEMORY.md") == 1234
    limits = char_limits_from_home(home, cfg)
    assert limits["MEMORY.md"] == 1234
    assert limits["USER.md"] == 4321

    from dashboard import plugin_api
    assert plugin_api._version() == "9.9.9"
    assert any("memory_char_limit" in text for text in seen)
    assert any("version:" in text for text in seen)
