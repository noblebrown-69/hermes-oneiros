import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
