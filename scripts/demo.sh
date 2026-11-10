#!/usr/bin/env bash
# Oneiros Dream Cycle — one-command synthetic demo (~5s). Never touches a live HERMES_HOME.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP=$(mktemp -d /tmp/dream-cycle-demo.XXXXXX); trap 'rm -rf "$TMP"' EXIT
export HERMES_HOME="$TMP/home"
mkdir -p "$HERMES_HOME/memories" "$HERMES_HOME/plugin-data/dream-cycle"
cat > "$HERMES_HOME/memories/MEMORY.md" <<'MD'
# Memory (synthetic)
- dashboard port on :9119
- llama build b12000 is live
MD
# A fake, read-only "session source": yesterday's sessions mention a new build.
cat > "$TMP/sessions.py" <<'PY'
import json, sys
cmd, day = sys.argv[1], sys.argv[2]
if cmd == "days":
    print(json.dumps({"dates": ["2030-01-01"]}))
else:
    print(json.dumps({"lines": ["upgraded llama build to b12500 this afternoon"]}))
PY
cat > "$HERMES_HOME/plugin-data/dream-cycle/config.yaml" <<EOF
session_corroboration:
  enabled: true
  days_command: ["python3", "$TMP/sessions.py", "days", "{date}"]
  day_command: ["python3", "$TMP/sessions.py", "day", "{date}"]
EOF
cd "$ROOT"
echo "🌙 Oneiros Dream Cycle — Dream the day. Wake as yourself."
echo; echo "── BEFORE: MEMORY.md (the agent still believes build b12000) ──"; cat "$HERMES_HOME/memories/MEMORY.md"
python3 - <<'PY'
import os, json
from datetime import date
from pathlib import Path
from dream_cycle.config import load_config
from dream_cycle.propose import propose
home = Path(os.environ["HERMES_HOME"])
cfg = load_config(home / "plugin-data/dream-cycle/config.yaml")
mem = (home / "memories/MEMORY.md").read_bytes()
print("\n── DREAM: replaying 2030-01-01 against memory (no model) ──")
r = propose(home, config=cfg, run_date=date(2030, 1, 2))
print(Path(r.stage_path, "diff.md").read_text())
assert r.proposals and r.proposals[0].values == ["b12000", "b12500"], r.proposals
assert (home / "memories/MEMORY.md").read_bytes() == mem
print("── AFTER: MEMORY.md unchanged — the proposal waits for morning review ──")
print("\n── QUIET NIGHT: a day with no sessions is not an error ──")
r2 = propose(home, config=cfg, run_date=date(2030, 1, 5))
print(f"session_status={r2.session_status}  ({r2.session_detail})")
assert r2.session_status == "no-sessions"
print("\nDEMO OK — model_used=false, memory untouched")
PY
