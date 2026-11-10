#!/usr/bin/env python3
"""Optional cron no-agent entry. Not the Desktop / dashboard / API path.

``deliver: origin`` only delivers when this job was created inside a live
session that supplied platform and chat id. Desktop, dashboard, and API
chats are ``api_server`` origins and are not push targets. Those surfaces
get a due reminder on the next private turn via ``pre_llm_call``, after the
plugin host reloads. Do not schedule this script for them.

Copy to ``$HERMES_HOME/scripts/`` only for a transport that truly has an
origin chat. Empty stdout is a silent tick. The emit time is a daily cap,
not a delivery receipt.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _plugin_root() -> Path:
    here = Path(__file__).resolve()
    candidates = []
    home = os.environ.get("HERMES_HOME")
    if home:
        candidates.append(Path(home) / "plugins" / "dream-cycle")
    candidates.append(here.parents[1])
    for candidate in candidates:
        if (candidate / "dream_cycle" / "remind.py").is_file():
            return candidate
    raise SystemExit(0)


def main() -> int:
    root = _plugin_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from dream_cycle.config import load_config
    from dream_cycle.paths import data_root
    from dream_cycle.remind import emit_for_scheduler

    home = Path(os.environ["HERMES_HOME"]) if os.environ.get("HERMES_HOME") else Path.cwd()
    cfg_path = data_root(home) / "config.yaml"
    cfg = load_config(cfg_path if cfg_path.is_file() else None)
    text = emit_for_scheduler(home, cfg)
    if text:
        sys.stdout.write(text.rstrip() + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # A traceback would be delivered as a cron error alert. Stay quiet.
        raise SystemExit(0)
