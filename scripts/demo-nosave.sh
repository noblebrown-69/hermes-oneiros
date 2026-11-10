#!/usr/bin/env bash
# Oneiros Dream Cycle v0.8 — "do not save" detector demo (synthetic scratch profile only).
# Flow: seed a normal fact, a mid-session "don't save this" exchange, a quoted
# question about the phrase, and a first-message off-the-record session → propose
# → show skips in status / ledger (hashes only) → confirm memory unchanged.
# Touches nothing under a real HERMES_HOME / live bot. No model calls. No network.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; PY="${PY:-$ROOT/.venv/bin/python}"
H="$(mktemp -d)/home"; mkdir -p "$H/memories" "$H/plugin-data/dream-cycle"
printf '# Synthetic USER\n' > "$H/memories/USER.md"
printf '# Synthetic MEMORY\n§\ndemo-svc listens on port :18991\n' > "$H/memories/MEMORY.md"
printf 'apply_enabled: false\nhonor_nosave: true\nuser_facts: {enabled: true, lookback_days: 30}\nsession_corroboration: {enabled: true, source: state.db, lookback_days: 3}\n' \
  > "$H/plugin-data/dream-cycle/config.yaml"
cd "$ROOT"; export H
"$PY" - <<'PY'
import hashlib, json, os, sqlite3
from datetime import date, datetime
from pathlib import Path
from dream_cycle.config import load_config
from dream_cycle.propose import propose
from dream_cycle.review import read_reviews
from dream_cycle.status import status

H = Path(os.environ["H"])
cfg = load_config(H / "plugin-data/dream-cycle/config.yaml")
USER, MEM = H / "memories/USER.md", H / "memories/MEMORY.md"
u0, m0 = hashlib.md5(USER.read_bytes()).hexdigest(), hashlib.md5(MEM.read_bytes()).hexdigest()

ts = datetime(2026, 11, 9, 15, 0, 0).timestamp()
con = sqlite3.connect(H / "state.db")
con.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
# s-open: ordinary facts that MUST still be proposed
# s-skip: mid-session "don't save this" — trip + following port mention dropped; preference kept
# s-quote: asking about the phrase must NOT skip the PERSON fact
# s-first: first user message is off the record → whole session skipped
rows = [
    ("s-open", "user", "Alex is my sibling.", ts),
    ("s-open", "assistant", "Nice to meet the family.", ts + 1),
    ("s-skip", "user", "I prefer dark roast.", ts + 10),
    ("s-skip", "assistant", "Noted.", ts + 11),
    ("s-skip", "user", "Don't save this. I'm taking a trip to Harbor City on Nov 19.", ts + 12),
    ("s-skip", "assistant", "Have fun. confirm the endpoint url http://localhost:18992/v1", ts + 13),
    ("s-quote", "user", 'What does "don\'t save" mean? Also, Jordan is my cousin.', ts + 20),
    ("s-first", "user", "Off the record, Sam is my neighbor and I'm going to Lakeview on Nov 21.", ts + 30),
    ("s-first", "assistant", "I will not keep that.", ts + 31),
    ("s-first", "user", "I live in Harbor City.", ts + 32),
]
sids = set()
for i, (sid, role, content, t) in enumerate(rows, 1):
    if sid not in sids:
        con.execute("INSERT INTO sessions (id, started_at) VALUES (?,?)", (sid, t))
        sids.add(sid)
    con.execute("INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
                (i, sid, role, content, t))
con.commit(); con.close()

def hr(t): print(f"\n== {t}")

hr("1. propose (honor_nosave=true, synthetic only)")
r = propose(H, config=cfg, run_date=date(2026, 11, 10), session_date=date(2026, 11, 9))
summ = json.loads(Path(r.stage_path, "summary.json").read_text())
print("nosave:", json.dumps(summ.get("nosave"), indent=2))
uf = [p for p in summ["proposals"] if p.get("kind") == "user_fact"]
print("user-fact proposals:")
for p in uf:
    print(f"  - {p['category']}: {p['values'][0]} → {Path(p['file']).name}")
vals = " | ".join(p["values"][0] for p in uf)
assert any("Alex" in p["values"][0] for p in uf), "open-session PERSON must survive"
assert any("dark roast" in p["values"][0].lower() for p in uf), "preference before nosave must survive"
assert any("Jordan" in p["values"][0] for p in uf), "quoted question must not skip PERSON"
assert "Harbor City" not in vals and "Lakeview" not in vals and "Sam" not in vals, "skipped ranges leaked a fact"
port = [p for p in summ["proposals"] if p.get("label") == "port"]
for p in port:
    assert "18992" not in p.get("values", []), "skipped assistant turn must not corroborate :18992"
ns = summ["nosave"]
assert ns["honored"] is True
assert ns["sessions_skipped"] == 1, ns
assert ns["exchanges_skipped"] == 1, ns

hr("2. status (counts + reasons, no message text)")
text = status(H, cfg)["text"]
print(text)
assert "Alex" not in text and "Harbor" not in text and "Sam" not in text
assert "session(s)" in text and "exchange(s)" in text

hr("3. review ledger nosave-skip rows (hashes only)")
rows = [x for x in read_reviews(Path(r.stage_path)) if x.get("decision") == "nosave-skip"]
print(f"{len(rows)} nosave-skip row(s)")
blob = json.dumps(rows)
for row in rows:
    print(json.dumps({k: row[k] for k in ("decision", "proposal_id", "scope", "reason",
                                          "session_id_sha256", "message_sha256")}))
assert "Alex" not in blob and "Harbor" not in blob and "Don't save" not in blob
assert "s-skip" not in blob and "s-first" not in blob
assert all(row.get("message_sha256") and row.get("session_id_sha256") for row in rows)

hr("4. memory unchanged (propose-only)")
assert hashlib.md5(USER.read_bytes()).hexdigest() == u0
assert hashlib.md5(MEM.read_bytes()).hexdigest() == m0
print("USER.md and MEMORY.md hashes unchanged")

hr("5. done — scratch profile only; no live bot touched")
print("HERMES_HOME was", H)
PY
