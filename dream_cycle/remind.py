"""Owner review reminders and on-demand Oneiros status (v0.8.4).

Decides from the local review ledger and a caller-supplied clock. No model
and no network. Delivery is a transport the caller supplies: this module
records a confirmed send only when that transport reports success.

Proactive nags (overdue unreviewed dreams) are skipped while snoozed and are
capped at one per rolling cooldown per profile. An explicit status read is
never suppressed by snooze or cooldown.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from .config import default_config, validate_config
from .paths import data_root, lock_exclusive, unlock
from .review import latest_decisions_across

STATE_NAME = "review-reminder.json"
# A human has already looked. Reverted and missing rows are still pending.
REVIEWED = frozenset({"accept", "reject", "defer", "applied", "rolled-back"})
CLAIM_TTL = timedelta(minutes=2)

# Surfaces that are the operator's own session, not a shared room.
# Empty platform is classic CLI. api_server is Desktop / dashboard / API chat.
LOCAL_OWNER_PLATFORMS = frozenset({
    "", "cli", "tui", "desktop", "dashboard", "web", "api", "api_server", "acp", "local",
})
PRIVATE_CHAT_TYPES = frozenset({"dm", "private", "direct", "direct_message", "im", "private_chat"})
GROUP_CHAT_TYPES = frozenset({
    "group", "supergroup", "channel", "guild", "room", "thread", "topic",
})


@dataclass
class SendResult:
    ok: bool
    error: str = ""
    # confirmed: transport saw delivery succeed. emitted: handed to a scheduler
    # that delivers after this process exits (not a receipt).
    kind: str = "confirmed"


class SendFn:
    """Callable transport: send(text) -> SendResult."""

    def __init__(self, fn: Callable[[str], SendResult]):
        self._fn = fn

    def send(self, text: str) -> SendResult:
        return self._fn(text)


def post_message_result(handle: Any) -> SendResult:
    """Interpret Hermes ``ctx.post_message`` (object or dict). Never raises."""
    if isinstance(handle, dict):
        ok = bool(handle.get("ok"))
        err = str(handle.get("error") or "")
    else:
        ok = bool(getattr(handle, "ok", False))
        err = str(getattr(handle, "error", "") or "")
    return SendResult(ok, err, "confirmed")


def profile_name(home: Path) -> str:
    home = Path(home)
    if home.parent.name == "profiles":
        return home.name
    return "default"


def build_review_url(base: str, profile: str, day: Optional[str]) -> Optional[str]:
    """``{base}/oneiros?profile=&day=``. None when base is unset. No secrets."""
    base = (base or "").strip().rstrip("/")
    if not base:
        return None
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    path = (parts.path or "").rstrip("/") + "/oneiros"
    query = {"profile": profile}
    if day:
        query["day"] = day
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def _now(now: Optional[datetime]) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _state_path(home: Path) -> Path:
    return data_root(home) / STATE_NAME


def _empty_state() -> Dict[str, Any]:
    return {"last_sent_at": None, "last_emit_at": None, "claim": None, "snooze_until": None}


def _load_state(home: Path) -> Dict[str, Any]:
    path = _state_path(home)
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    base = _empty_state()
    base.update({k: data.get(k) for k in base})
    return base


def _save_state(home: Path, state: Dict[str, Any]) -> None:
    path = _state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def _lock(home: Path):
    path = data_root(home) / "review-reminder.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    try:
        lock_exclusive(fh.fileno(), nonblocking=False)
    except Exception:
        fh.close()
        raise
    return fh


def _unlock(fh) -> None:
    try:
        unlock(fh.fileno())
    finally:
        fh.close()


def _summary_proposals(stage: Path) -> List[Dict[str, Any]]:
    path = stage / "summary.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    props = data.get("proposals") if isinstance(data, dict) else None
    return [p for p in (props or []) if isinstance(p, dict) and p.get("proposal_id")]


def _cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return validate_config(default_config() if config is None else config)


def review_snapshot(home: Path, config: Optional[Dict[str, Any]] = None,
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    """Live counts for this profile. Does not send and ignores snooze."""
    cfg = _cfg(config)
    # validate_config mutates; callers may pass default_config copies.
    rr = cfg["review_reminder"]
    now = _now(now)
    home = Path(home)
    root = data_root(home) / cfg.get("stage_subdir", "dream-proposals")
    decisions = latest_decisions_across(root) if root.is_dir() else {}
    first_day: Dict[str, str] = {}
    if root.is_dir():
        for stage in sorted(p for p in root.iterdir() if p.is_dir()):
            for prop in _summary_proposals(stage):
                pid = prop["proposal_id"]
                first_day.setdefault(pid, stage.name)
    pending: List[Dict[str, Any]] = []
    awaiting: List[Dict[str, Any]] = []
    deferred = 0
    threshold = int(rr["overdue_days"])
    for pid, day in first_day.items():
        decision = (decisions.get(pid) or {}).get("decision")
        try:
            age = (now.date() - datetime.fromisoformat(day).date()).days
        except ValueError:
            continue
        row = {"proposal_id": pid, "day": day, "age_days": age, "decision": decision}
        if decision == "accept":
            awaiting.append(row)
        elif decision == "defer":
            deferred += 1
        elif decision not in REVIEWED:
            pending.append(row)
    pending.sort(key=lambda r: (r["day"], r["proposal_id"]))
    overdue = [r for r in pending if r["age_days"] > threshold]
    state = _load_state(home)
    snooze_until = state.get("snooze_until")
    link_day = None
    if overdue:
        link_day = overdue[0]["day"]
    elif pending:
        link_day = pending[0]["day"]
    elif awaiting:
        link_day = sorted(awaiting, key=lambda r: r["day"])[0]["day"]
    elif first_day:
        link_day = max(first_day.values())
    url = build_review_url(rr.get("dashboard_base_url") or "", profile_name(home), link_day)
    oldest_age = overdue[0]["age_days"] if overdue else (pending[0]["age_days"] if pending else None)
    return {
        "profile": profile_name(home),
        "pending_review": len(pending),
        "overdue_count": len(overdue),
        "overdue_days": threshold,
        "oldest_pending_day": pending[0]["day"] if pending else None,
        "oldest_overdue_day": overdue[0]["day"] if overdue else None,
        "oldest_overdue_age_days": overdue[0]["age_days"] if overdue else None,
        "oldest_pending_age_days": oldest_age if pending else None,
        "accepted_awaiting_apply": len(awaiting),
        "deferred": deferred,
        "link_day": link_day,
        "dashboard_url": url,
        "snoozed_until": snooze_until,
        "snooze_active": _snooze_active(state, now),
        "last_sent_at": state.get("last_sent_at"),
        "last_emit_at": state.get("last_emit_at"),
        "model_used": False,
    }


def _snooze_active(state: Dict[str, Any], now: datetime) -> bool:
    until = _parse_dt(state.get("snooze_until"))
    return bool(until and now < until)


def _cooldown_blocks(state: Dict[str, Any], now: datetime, hours: float) -> bool:
    window = timedelta(hours=hours)
    for key in ("last_sent_at", "last_emit_at"):
        stamp = _parse_dt(state.get(key))
        if stamp and now - stamp < window:
            return True
    return False


def _claim_fresh(state: Dict[str, Any], now: datetime) -> bool:
    claim = state.get("claim") or None
    if not isinstance(claim, dict):
        return False
    at = _parse_dt(claim.get("at"))
    return bool(at and now - at < CLAIM_TTL)


def format_reminder(snap: Dict[str, Any]) -> str:
    n = int(snap["overdue_count"])
    age = snap.get("oldest_overdue_age_days")
    noun = "dream" if n == 1 else "dreams"
    verb = "has" if n == 1 else "have"
    lines = [f"Oneiros: {n} {noun} {verb} been waiting {age} days for a review."]
    if snap.get("dashboard_url"):
        lines.append(str(snap["dashboard_url"]))
    lines.append("Snooze for a week with /dream-cycle snooze")
    return "\n".join(lines)


def snooze(home: Path, config: Optional[Dict[str, Any]] = None,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Suppress proactive nags for this profile. Status reads stay live."""
    cfg = _cfg(config)
    now = _now(now)
    until = now + timedelta(days=int(cfg["review_reminder"]["snooze_days"]))
    fh = _lock(home)
    try:
        state = _load_state(home)
        state["snooze_until"] = until.isoformat(timespec="seconds")
        state["claim"] = None
        _save_state(home, state)
    finally:
        _unlock(fh)
    snap = review_snapshot(home, cfg, now)
    snap["snoozed_until"] = state["snooze_until"]
    snap["snooze_active"] = True
    return snap


def maybe_remind(home: Path, config: Optional[Dict[str, Any]] = None, *,
                 now: Optional[datetime] = None, transport: SendFn,
                 kind_default: str = "confirmed") -> Dict[str, Any]:
    """Send one proactive reminder when due. Does not mark a failed delivery as sent."""
    cfg = _cfg(config)
    rr = cfg["review_reminder"]
    now = _now(now)
    snap = review_snapshot(home, cfg, now)
    result = {**snap, "reminded": False, "reason": "", "message": ""}
    if not rr.get("enabled"):
        result["reason"] = "disabled"
        return result
    if snap["overdue_count"] <= 0:
        result["reason"] = "none_overdue"
        return result
    fh = _lock(home)
    token = f"{now.timestamp():.6f}-{os.getpid()}"
    try:
        state = _load_state(home)
        if _snooze_active(state, now):
            result["reason"] = "snoozed"
            result["snoozed_until"] = state.get("snooze_until")
            result["snooze_active"] = True
            return result
        if _cooldown_blocks(state, now, float(rr["cooldown_hours"])):
            result["reason"] = "cooldown"
            return result
        if _claim_fresh(state, now):
            result["reason"] = "in_flight"
            return result
        state["claim"] = {"at": now.isoformat(timespec="seconds"), "token": token}
        _save_state(home, state)
    finally:
        _unlock(fh)

    text = format_reminder(snap)
    try:
        sent = transport.send(text)
    except Exception as exc:
        sent = SendResult(False, exc.__class__.__name__, kind_default)
    fh = _lock(home)
    try:
        state = _load_state(home)
        claim = state.get("claim") or {}
        if claim.get("token") != token:
            result["reason"] = "lost_claim"
            result["delivery_error"] = sent.error
            return result
        state["claim"] = None
        if sent.ok and sent.kind == "confirmed":
            state["last_sent_at"] = now.isoformat(timespec="seconds")
            result["reminded"] = True
            result["reason"] = "sent"
        elif sent.ok and sent.kind == "emitted":
            state["last_emit_at"] = now.isoformat(timespec="seconds")
            result["reminded"] = True
            result["reason"] = "emitted"
        else:
            result["reason"] = "delivery_failed"
            result["delivery_error"] = sent.error or "delivery_failed"
        _save_state(home, state)
        result["last_sent_at"] = state.get("last_sent_at")
        result["last_emit_at"] = state.get("last_emit_at")
    finally:
        _unlock(fh)
    result["message"] = text if result["reminded"] else ""
    return result


def private_owner_turn(**kwargs: Any) -> bool:
    """True only for the operator's own private turn.

    Local surfaces (CLI, TUI, Desktop, dashboard, API) are that session.
    A messaging platform must say the chat is a DM. Group, channel, thread,
    and an unlabeled messaging chat are refused — this does not guess.
    """
    if (kwargs.get("parent_session_id") or "").strip():
        return False
    platform = str(kwargs.get("platform") or "").strip().lower()
    chat_type = str(kwargs.get("chat_type") or "").strip().lower()
    if chat_type in GROUP_CHAT_TYPES:
        return False
    if platform in LOCAL_OWNER_PLATFORMS and chat_type in (PRIVATE_CHAT_TYPES | {""}):
        return True
    return chat_type in PRIVATE_CHAT_TYPES


def turn_reminder_context(home: Path, config: Optional[Dict[str, Any]] = None,
                          now: Optional[datetime] = None, **kwargs: Any) -> Optional[str]:
    """Context for the current private turn, or None.

    Returning text is the delivery: Hermes appends ``pre_llm_call`` context to
    that turn. It does not push to a platform. Snooze, the 24h cap, and the
    overdue threshold are the same rules as a proactive nag. A refused turn
    does not touch the sent marker.
    """
    if not private_owner_turn(**kwargs):
        return None

    def _send(text: str) -> SendResult:
        return SendResult(True, "", "confirmed")

    out = maybe_remind(home, config, now=now, transport=SendFn(_send))
    if out.get("reminded") and out.get("message"):
        return (
            "[Oneiros] Mention this once, briefly, using only these facts.\n"
            + out["message"]
        )
    return None


def emit_for_scheduler(home: Path, config: Optional[Dict[str, Any]] = None,
                       now: Optional[datetime] = None) -> str:
    """Cron no-agent handoff: return the message or "". Does not claim a receipt.

    Hermes delivers non-empty stdout to the job's ``deliver: origin`` chat
    after this process exits, so this records ``last_emit_at`` only. A later
    confirmed transport still uses ``last_sent_at``. Failures of the scheduler
    itself are not visible here; the emit timestamp is the rolling cap.
    """
    def _send(text: str) -> SendResult:
        return SendResult(True, "", "emitted")

    out = maybe_remind(home, config, now=now, transport=SendFn(_send))
    if out.get("reminded") and out.get("reason") == "emitted":
        return out.get("message") or ""
    return ""
