"""Warm Waking Check / status caches on ``on_session_open`` (Hermes ≥ PR #151120).

Must stay well under the turn-wait budget (~5s). Never raises to the host.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional


def warm_session_caches(*, session_id: str = "", platform: str = "", reason: str = "",
                        home: Optional[Path] = None) -> Dict[str, Any]:
    """Refresh lightweight Oneiros status (+ hygiene report pointer) into a cache file."""
    t0 = time.monotonic()
    out: Dict[str, Any] = {
        "ok": True,
        "session_id": session_id,
        "platform": platform,
        "reason": reason,
        "elapsed_ms": 0,
        "status": None,
        "hygiene": None,
    }
    try:
        if home is None:
            try:
                from hermes_constants import get_hermes_home
                home = Path(get_hermes_home())
            except Exception:
                import os
                home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
        home = Path(home)

        from .config import load_config
        from .status import status as dc_status
        from .paths import data_root

        cfg_path = home / "plugin-data" / "dream-cycle" / "config.yaml"
        cfg = load_config(cfg_path) if cfg_path.is_file() else load_config()
        st = dc_status(home, cfg)
        out["status"] = {
            "version": st.get("version"),
            "freshness": st.get("freshness"),
            "last_run_at": st.get("last_run_at"),
            "undecided": (st.get("latest") or {}).get("undecided"),
            "apply_enabled": st.get("apply_enabled"),
            "disabled": st.get("disabled"),
            "session_corroboration": st.get("session_corroboration"),
        }

        # Waking Check: prefer the last memory-hygiene report (no re-scan — keep <5s).
        hyg_root = home / "plugin-data" / "memory-hygiene"
        report = None
        for cand in (
            hyg_root / "last-report.json",
            hyg_root / "waking-check.json",
            hyg_root / "status.json",
        ):
            if cand.is_file():
                report = cand
                break
        if report is None and hyg_root.is_dir():
            # newest *.json under reports/ if present
            reps = sorted(hyg_root.glob("**/report*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if reps:
                report = reps[0]
        if report is not None:
            try:
                body = json.loads(report.read_text())
                out["hygiene"] = {
                    "path": str(report),
                    "mtime": report.stat().st_mtime,
                    "hit_count": body.get("hit_count", body.get("hits", body.get("count"))),
                }
            except Exception:
                out["hygiene"] = {"path": str(report), "mtime": report.stat().st_mtime}

        root = data_root(home)
        root.mkdir(parents=True, exist_ok=True)
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        (root / "session-warm.json").write_text(json.dumps(out, indent=2) + "\n")
    except Exception as e:
        out["ok"] = False
        out["error"] = e.__class__.__name__
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        try:
            if home is not None:
                from .paths import data_root
                root = data_root(Path(home))
                root.mkdir(parents=True, exist_ok=True)
                (root / "session-warm.json").write_text(json.dumps(out, indent=2) + "\n")
        except Exception:
            pass
    return out
