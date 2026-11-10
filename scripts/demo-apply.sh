#!/usr/bin/env bash
# Oneiros Dream Cycle apply demo — synthetic memory + synthetic state.db in a throwaway HERMES_HOME.
# Touches nothing real. Flow: status → propose → dry-run → accept row → apply --accepted →
# SUPERSEDED line + receipt → rollback → status, then the safe-write guard refusals.
# Dashboard (v0.5.0): open Hermes dashboard → Oneiros tab (the active profile).
# Status / proposals / Apply accepted (dry-run confirm) / receipts Rollback. Actor writes as dashboard:<user>.
# API: /api/plugins/dream-cycle/{overview,proposals,review,apply,receipts,rollback}?profile=<name>
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; PY="${PY:-$ROOT/.venv/bin/python}"
H="$(mktemp -d)/home"; mkdir -p "$H/memories" "$H/plugin-data/dream-cycle"
printf '# Synthetic memory\n§\ndemo-svc listens on port :18991 (old note)\n§\ndemo-svc endpoint url http://localhost:18992/v1\n' > "$H/memories/MEMORY.md"
printf 'apply_enabled: true\nsession_costs: {enabled: true}\n' > "$H/plugin-data/dream-cycle/config.yaml"
cd "$ROOT"; export H
"$PY" - <<'PY'
import hashlib, json, os, sqlite3, time
from pathlib import Path
from dream_cycle.config import load_config
from dream_cycle.propose import propose
from dream_cycle.apply import apply_proposal, apply_accepted, rollback
from dream_cycle.review import append_review
from dream_cycle.status import status
H = Path(os.environ["H"]); M = H / "memories/MEMORY.md"; cfg = load_config(H / "plugin-data/dream-cycle/config.yaml")
# synthetic state.db: today a tool call printed the live port (this is what "grounded" means)
con = sqlite3.connect(H / "state.db")
con.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('demo-s1','tool','LISTEN 127.0.0.1 port :18992 demo-svc', ?)", (time.time(),))
con.commit(); con.close()
md5 = lambda: hashlib.md5(M.read_bytes()).hexdigest()
def hr(t): print(f"\n== {t}")
hr("1. status (before)"); print(status(H, cfg)["text"])
hr("2. propose"); r = propose(H, config=cfg)
pid = json.loads(Path(r.stage_path, "summary.json").read_text())["proposals"][0]["proposal_id"]
print("proposal", pid, r.proposals[0].values, "| MEMORY md5", md5())
m0 = md5()
hr("3. dry-run"); d = apply_proposal(H, pid, "18992", "synthetic: ss -ltnp shows :18992", "demo", config=cfg, dry_run=True)
print(d.status, "-", d.message, "| evidence:", d.evidence_status, "| md5 unchanged:", md5() == m0)
print("secret scan:", apply_proposal(H, pid, "18992", "token ghp_" + "a" * 30, "demo", config=cfg).message)
hr("4. guardian accept row (review.jsonl)")
print(json.dumps(append_review(Path(r.stage_path), pid, "accept", "reviewer", evidence="synthetic: ss -ltnp shows :18992", keep="18992")))
hr("5. apply --accepted"); out = apply_accepted(H, config=cfg)
a = out[0]; print(a["status"], "-", a["message"], "| receipt", a["receipt_id"], "| evidence:", a["evidence_status"])
m1 = md5(); print("MEMORY md5", m0, "->", m1)
hr("6. SUPERSEDED line + receipt"); print(M.read_text())
rec = json.loads((H / "plugin-data/dream-cycle/receipts" / f"{a['receipt_id']}.json").read_text())
print(json.dumps({k: rec[k] for k in ("receipt_id", "keep", "retired", "sha_before", "sha_after", "scope", "evidence_status", "rollback")}, indent=1))
print("grounding:", rec["grounding"]["detail"], [m["session_id"] for m in rec["grounding"]["matches"]])
hr("7. rollback --last"); rb = rollback(H, last=True, config=cfg)
print(rb.status, "-", rb.message, "| md5", md5(), "== before:", md5() == m0)
hr("8. status (after)"); print(status(H, cfg)["text"])
hr("9. safe-write guard refusals (v0.3.1)")
M.write_text("alpha port :18992 primary; fallback box port :18991 on the lan\n")
r2 = propose(H, config=cfg); p2 = json.loads(Path(r2.stage_path, "summary.json").read_text())["proposals"][0]["proposal_id"]
print("same-line:", apply_proposal(H, p2, "18992", "synthetic", "demo", config=cfg).message)
M.write_text("demo-svc listens on port :18991\n§\ndemo-svc endpoint url http://localhost:18992/v1\n")
r3 = propose(H, config=cfg); p3 = json.loads(Path(r3.stage_path, "summary.json").read_text())["proposals"][0]["proposal_id"]
M.write_text(M.read_text().replace(":18991", ":18995"))
print("stale:    ", apply_proposal(H, p3, "18992", "synthetic", "demo", config=cfg).message)
PY
