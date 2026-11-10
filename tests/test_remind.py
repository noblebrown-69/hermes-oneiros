"""Review reminders: fixed clock, scratch profiles, fake transport. No chat sends."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dream_cycle.config import default_config, validate_config
from dream_cycle.errors import ConfigError
from dream_cycle.remind import (
    SendFn,
    SendResult,
    build_review_url,
    emit_for_scheduler,
    maybe_remind,
    review_snapshot,
    snooze,
)
from dream_cycle.review import append_event, append_review

DAY = "2026-11-01"
NOW_7 = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)   # age 7, not overdue
NOW_8 = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)    # age 8, overdue
BASE = "https://dash.example/hermes"


def _cfg():
    cfg = default_config()
    cfg["review_reminder"] = {
        "enabled": True,
        "overdue_days": 7,
        "cooldown_hours": 24,
        "snooze_days": 7,
        "dashboard_base_url": BASE,
    }
    return validate_config(cfg)


def _stage(home: Path, day: str, pid: str, note: str = "morning review"):
    stage = home / "plugin-data" / "dream-cycle" / "dream-proposals" / day
    stage.mkdir(parents=True, exist_ok=True)
    summary = stage / "summary.json"
    import json
    props = []
    if summary.is_file():
        props = json.loads(summary.read_text())["proposals"]
    props.append({
        "proposal_id": pid, "label": "port", "values": ["1", "2"],
        "file": str(home / "memories" / "MEMORY.md"), "line_nos": [1], "note": note,
    })
    summary.write_text(json.dumps({"proposals": props}))
    return stage


def _ok(text: str) -> SendResult:
    return SendResult(True, "", "confirmed")


def test_seven_days_is_not_overdue_eight_days_is(home):
    home = home / "profiles" / "fleetbot"
    _stage(home, DAY, "aaaa1111bbbb")
    young = review_snapshot(home, _cfg(), NOW_7)
    assert young["pending_review"] == 1
    assert young["overdue_count"] == 0
    assert young["oldest_pending_age_days"] == 7
    old = review_snapshot(home, _cfg(), NOW_8)
    assert old["overdue_count"] == 1
    assert old["oldest_overdue_age_days"] == 8
    assert old["dashboard_url"] == (
        "https://dash.example/hermes/oneiros?profile=fleetbot&day=2026-11-01"
    )
    assert "user:pass" not in old["dashboard_url"]


def test_accept_is_reviewed_revert_is_pending_defer_is_not_unreviewed(home):
    home = home / "profiles" / "fleetbot"
    stage = _stage(home, DAY, "aaaa1111bbbb")
    append_review(stage, "aaaa1111bbbb", "accept", "guardian", evidence="", keep="1")
    snap = review_snapshot(home, _cfg(), NOW_8)
    assert snap["pending_review"] == 0
    assert snap["overdue_count"] == 0
    assert snap["accepted_awaiting_apply"] == 1
    sent = []
    out = maybe_remind(home, _cfg(), now=NOW_8, transport=SendFn(lambda t: sent.append(t) or _ok(t)))
    assert out["reason"] == "none_overdue" and not sent

    append_event(stage, {"proposal_id": "aaaa1111bbbb", "decision": "reverted", "actor": "guardian"})
    snap = review_snapshot(home, _cfg(), NOW_8)
    assert snap["pending_review"] == 1 and snap["overdue_count"] == 1

    append_review(stage, "aaaa1111bbbb", "defer", "guardian", evidence="later")
    snap = review_snapshot(home, _cfg(), NOW_8)
    assert snap["pending_review"] == 0 and snap["deferred"] == 1 and snap["overdue_count"] == 0


def test_empty_queue_does_not_nag_and_review_resets(home):
    home = home / "profiles" / "fleetbot"
    cfg = _cfg()
    out = maybe_remind(home, cfg, now=NOW_8, transport=SendFn(_ok))
    assert out["reason"] == "none_overdue" and out["pending_review"] == 0
    stage = _stage(home, DAY, "aaaa1111bbbb")
    out = maybe_remind(home, cfg, now=NOW_8, transport=SendFn(_ok))
    assert out["reminded"] and out["reason"] == "sent"
    append_review(stage, "aaaa1111bbbb", "reject", "guardian", evidence="no")
    later = NOW_8 + timedelta(hours=25)
    out = maybe_remind(home, cfg, now=later, transport=SendFn(_ok))
    assert out["reason"] == "none_overdue" and out["overdue_count"] == 0


def test_cooldown_boundary_and_failed_delivery_does_not_mark_sent(home):
    home = home / "profiles" / "fleetbot"
    _stage(home, DAY, "aaaa1111bbbb")
    cfg = _cfg()
    fails = {"n": 0}

    def fail(_text):
        fails["n"] += 1
        return SendResult(False, "send_failed", "confirmed")

    out = maybe_remind(home, cfg, now=NOW_8, transport=SendFn(fail))
    assert out["reason"] == "delivery_failed" and out["last_sent_at"] is None
    assert fails["n"] == 1
    # Immediate retry is allowed because nothing was confirmed or emitted.
    out = maybe_remind(home, cfg, now=NOW_8, transport=SendFn(_ok))
    assert out["reminded"] and out["reason"] == "sent"
    sent_at = datetime.fromisoformat(out["last_sent_at"])
    almost = sent_at + timedelta(hours=24) - timedelta(seconds=1)
    blocked = maybe_remind(home, cfg, now=almost, transport=SendFn(_ok))
    assert blocked["reason"] == "cooldown" and blocked["reminded"] is False
    due = maybe_remind(home, cfg, now=sent_at + timedelta(hours=24), transport=SendFn(_ok))
    assert due["reminded"] and due["reason"] == "sent"


def test_snooze_blocks_nags_not_status_and_resumes_without_a_burst(home):
    home = home / "profiles" / "fleetbot"
    _stage(home, DAY, "aaaa1111bbbb")
    cfg = _cfg()
    snapped = snooze(home, cfg, NOW_8)
    assert snapped["snooze_active"] is True
    until = datetime.fromisoformat(snapped["snoozed_until"])
    assert until - NOW_8 == timedelta(days=7)
    live = review_snapshot(home, cfg, NOW_8 + timedelta(days=1))
    assert live["overdue_count"] == 1 and live["snooze_active"] is True
    assert live["pending_review"] == 1
    nag = maybe_remind(home, cfg, now=NOW_8 + timedelta(days=1), transport=SendFn(_ok))
    assert nag["reason"] == "snoozed" and nag["reminded"] is False
    # The moment snooze ends, one nag is due (no stacked catch-up), then cooldown holds.
    resumed = maybe_remind(home, cfg, now=until, transport=SendFn(_ok))
    assert resumed["reminded"] and resumed["reason"] == "sent"
    again = maybe_remind(home, cfg, now=until + timedelta(minutes=1), transport=SendFn(_ok))
    assert again["reason"] == "cooldown" and again["reminded"] is False


def test_snoozed_profile_does_not_silence_another(home):
    one = home / "profiles" / "fleetbot"
    two = home / "profiles" / "otherbot"
    _stage(one, DAY, "aaaa1111bbbb")
    _stage(two, DAY, "cccc2222dddd")
    cfg = _cfg()
    snooze(one, cfg, NOW_8)
    out = maybe_remind(two, cfg, now=NOW_8, transport=SendFn(_ok))
    assert out["reminded"] and out["profile"] == "otherbot"


def test_in_flight_claim_blocks_a_second_sender(home):
    import threading
    home = home / "profiles" / "fleetbot"
    _stage(home, DAY, "aaaa1111bbbb")
    cfg = _cfg()
    started = threading.Event()
    release = threading.Event()

    def slow(_text):
        started.set()
        assert release.wait(2)
        return _ok(_text)

    holder = {}

    def run():
        holder["out"] = maybe_remind(home, cfg, now=NOW_8, transport=SendFn(slow))

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(2)
    other = maybe_remind(home, cfg, now=NOW_8, transport=SendFn(_ok))
    assert other["reason"] == "in_flight" and other["reminded"] is False
    release.set()
    thread.join(2)
    assert holder["out"]["reminded"] is True


def test_missing_base_url_does_not_invent_a_host(home):
    home = home / "profiles" / "fleetbot"
    _stage(home, DAY, "aaaa1111bbbb")
    cfg = _cfg()
    cfg["review_reminder"]["dashboard_base_url"] = ""
    snap = review_snapshot(home, cfg, NOW_8)
    assert snap["dashboard_url"] is None
    assert snap["overdue_count"] == 1


def test_emit_sets_emit_time_not_sent_receipt(home):
    home = home / "profiles" / "fleetbot"
    _stage(home, DAY, "aaaa1111bbbb")
    text = emit_for_scheduler(home, _cfg(), NOW_8)
    assert "waiting 8 days" in text
    assert text.startswith("Oneiros:")
    assert "https://dash.example/hermes/oneiros?profile=fleetbot&day=2026-11-01" in text
    snap = review_snapshot(home, _cfg(), NOW_8)
    assert snap["last_emit_at"]
    assert snap["last_sent_at"] is None
    assert emit_for_scheduler(home, _cfg(), NOW_8 + timedelta(hours=1)) == ""


def test_dashboard_base_url_rejects_credentials():
    cfg = default_config()
    cfg["review_reminder"]["dashboard_base_url"] = "https://user:secret@dash.example"
    with pytest.raises(ConfigError, match="credentials"):
        validate_config(cfg)
    cfg = default_config()
    cfg["review_reminder"]["cooldown_hours"] = 0
    with pytest.raises(ConfigError, match="cooldown_hours"):
        validate_config(cfg)
    assert build_review_url("", "fleetbot", DAY) is None


def test_url_shape():
    assert build_review_url(BASE, "fleetbot", "2026-11-01") == (
        "https://dash.example/hermes/oneiros?profile=fleetbot&day=2026-11-01"
    )
