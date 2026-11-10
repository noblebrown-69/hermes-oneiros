# Dream Cycle — demo shot list (< 90 s)

Tagline on screen: **Dream the day. Wake as yourself.**

| t | Shot | Say |
|---|---|---|
| 0–10s | `cat MEMORY.md` — "llama build b12000 is live" | "My agent believes this. It stopped being true yesterday." |
| 10–30s | `./scripts/demo.sh` — DREAM section | "Overnight, Dream Cycle replays the day against memory. No model, just a deterministic pass." |
| 30–45s | `diff.md` proposal: b12000 vs b12500 | "It doesn't overwrite anything. It leaves a proposal for the morning." |
| 45–55s | AFTER: memory unchanged | "Memory is untouched until a human or the Waking Check says so." |
| 55–70s | QUIET NIGHT: `session_status=no-sessions` | "No sessions yesterday? That's a quiet night, not a crash." |
| 70–85s | `touch DISABLED` → `dream_cycle_status` | "One file turns it off." |

Everything shown is synthetic.

User-fact append demo (v0.7): `./scripts/demo-userfacts.sh`

Don't-save detector demo (v0.8): `./scripts/demo-nosave.sh`
