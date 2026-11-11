import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_hermes_yaml() -> None:
    """Production code imports Hermes's ``hermes_yaml``. A checkout that is not
    a Hermes install can still parse the same documents with PyYAML."""
    try:
        import hermes_yaml  # noqa: F401
        return
    except ImportError:
        pass
    try:
        import yaml as pyyaml
    except ImportError as exc:
        raise ImportError(
            "Plugin tests need Hermes's hermes_yaml on PYTHONPATH, or PyYAML, "
            "so config YAML can be parsed."
        ) from exc
    import types
    mod = types.ModuleType("hermes_yaml")
    mod.safe_load = pyyaml.safe_load
    mod.YAMLError = pyyaml.YAMLError
    sys.modules["hermes_yaml"] = mod


_ensure_hermes_yaml()

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def write_mem(home, text, name="MEMORY.md"):
    p = home / "memories" / name
    p.write_text(text)
    return p


class FakeCtx:
    def __init__(self):
        self.tools, self.commands, self.cli, self.skills, self.hooks = {}, {}, {}, {}, {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)

    def register_skill(self, name, path, description=""):
        self.skills[name] = path

    def register_hook(self, name, fn):
        self.hooks[name] = fn
