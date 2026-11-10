#!/usr/bin/env bash
# Oneiros Dream Cycle v0.7 — user-fact demo (synthetic scratch profile only).
# Flow: seed user assertions → propose → dry-run → accept → apply → rollback.
# Touches nothing under a real HERMES_HOME / live bot. No model calls. No network.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; PY="${PY:-$ROOT/.venv/bin/python}"
H="$(mktemp -d)/home"; mkdir -p "$H/memories" "$H/plugin-data/dream-cycle"
printf '# Synthetic USER\n' > "$H/memories/USER.md"
printf '# Synthetic MEMORY\n§\ndemo-svc listens on port :18991\n' > "$H/memories/MEMORY.md"
printf 'apply_enabled: true\nuser_facts: {enabled: true, lookback_days: 30}\nsession_corroboration: {enabled: false}\n' \
  > "$H/plugin-data/dream-cycle/config.yaml"
cd "$ROOT"; export H
"$PY" - <<'PY'
import hashlib, json, os, sqlite3, time
from datetime import date, datetime
from pathlib import Path
from dream_cycle.config import load_config
from dream_cycle.propose import propose
from dream_cycle.apply import apply_proposal, rollback
from dream_cycle.review import append_review
from dream_cycle.status import status

H = Path(os.environ["H"])
cfg = load_config(H / "plugin-data/dream-cycle/config.yaml")
USER = H / "memories/USER.md"
MEM = H / "memories/MEMORY.md"
md5 = lambda p: hashlib.md5(p.read_bytes()).hexdigest()

# Synthetic user messages (placeholder names/places — not real people).
ts = time.mktime(datetime(2026, 11, 9, 15, 0, 0).timetuple())
con = sqlite3.connect(H / "state.db")
con.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
rows = [
    ("demo-s1", "user", "I'm taking a trip to Harbor City on Nov 19.", ts),
    ("demo-s1", "user", "Alex is my sibling.", ts + 1),
    ("demo-s1", "user", "Who is Alex?", ts + 2),  # must NOT become a fact by itself
    ("demo-s1", "assistant", "Jordan is my cousin.", ts + 3),  # must NOT mine assistant
]
for sid, role, content, t in rows:
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                (sid, role, content, t))
con.commit(); con.close()

def hr(t): print(f"\n== {t}")

hr("1. status (before)"); print(status(H, cfg)["text"])
hr("2. propose (user facts from role=user only)")
r = propose(H, config=cfg, run_date=date(2026, 11, 10), session_date=date(2026, 11, 9))
summ = json.loads(Path(r.stage_path, "summary.json").read_text())
print("user_fact_status:", summ.get("user_fact_status"), summ.get("user_fact_detail"))
uf = [p for p in summ["proposals"] if p.get("kind") == "user_fact"]
print("user-fact proposals:", len(uf))
for p in uf:
    print(f"  - {p['category']}: {p['values']} → {Path(p['file']).name} action={p.get('action')}")
    print(f"    line: {p.get('proposed_line')}")
assert any(p["category"] == "EVENT" for p in uf), "expected EVENT trip proposal"
assert any(p["category"] == "PERSON" and "Alex" in p["values"][0] for p in uf), "expected PERSON Alex"
assert not any("Jordan" in str(p.get("values")) for p in uf), "assistant text must not yield facts"
assert not any(p["category"] == "PERSON" and p["values"][0].startswith("Who") for p in uf)

event = next(p for p in uf if p["category"] == "EVENT")
person = next(p for p in uf if p["category"] == "PERSON")
u0, m0 = md5(USER), md5(MEM)

hr("3. dry-run EVENT append")
d = apply_proposal(H, event["proposal_id"], event["values"][0],
                   "synthetic user: trip assertion", "demo", config=cfg, dry_run=True)
print(d.status, "-", d.message, "| evidence:", d.evidence_status, "| md5 unchanged:", md5(MEM) == m0)

hr("4. guardian accept + apply EVENT")
print(json.dumps(append_review(Path(r.stage_path), event["proposal_id"], "accept", "reviewer",
                               evidence="synthetic user trip assertion", keep=event["values"][0])))
a = apply_proposal(H, event["proposal_id"], event["values"][0],
                   "synthetic user: trip assertion", "demo", config=cfg)
print(a.status, "-", a.message, "| receipt", a.receipt_id)
print(MEM.read_text())
assert "STATUS: trip to Harbor City on 2026-11-19" in MEM.read_text()

hr("5. apply PERSON to USER.md")
append_review(Path(r.stage_path), person["proposal_id"], "accept", "reviewer",
              evidence="synthetic user person assertion", keep=person["values"][0])
b = apply_proposal(H, person["proposal_id"], person["values"][0],
                   "synthetic user: person assertion", "demo", config=cfg)
print(b.status, "-", b.message)
print(USER.read_text())
assert "PERSON: Alex" in USER.read_text()

hr("6. rollback last (PERSON)")
rb = rollback(H, last=True, config=cfg, actor="demo")
print(rb.status, "-", rb.message, "| USER md5 back:", md5(USER) == u0)

hr("7. rollback last (EVENT)")
rb2 = rollback(H, last=True, config=cfg, actor="demo")
print(rb2.status, "-", rb2.message, "| MEMORY md5 back:", md5(MEM) == m0)

hr("8. done — scratch profile only; no live bot touched")
print("HERMES_HOME was", H)
PY
