"""Oneiros dashboard decided-row rules (v0.5.1)."""
from pathlib import Path

from dashboard.decision_ui import is_decided, show_decision_actions

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "dashboard" / "dist" / "index.js"


def test_is_decided_covers_guardian_and_apply_states():
    for d in ("accept", "reject", "defer", "applied", "rolled-back"):
        assert is_decided(d) is True
        assert show_decision_actions(d, editing=False) is False
        assert show_decision_actions(d, editing=True) is True
    assert is_decided(None) is False
    assert is_decided("undecided") is False
    assert show_decision_actions(None, editing=False) is True


def test_index_js_hides_buttons_until_change_decision():
    src = JS.read_text(encoding="utf-8")
    assert "Change decision" in src
    assert "showDecisionActions" in src
    assert "isDecided" in src
    assert "last_actor" in src
    assert "last_ts" in src


def test_index_js_one_click_accept_and_accept_all():
    src = JS.read_text(encoding="utf-8")
    assert "oneClickAccept" in src
    assert "Accept all" in src
    assert "Accept with note…" in src
    assert "acceptAllShown" in src
    assert "undoAcceptAll" in src
    assert "/review/accept-all" in src
    assert "/review/undo-batch" in src
    # Write accept no longer requires evidence
    assert "disabled: !keep || !!busy" in src
    assert "disabled: !keep || !evidence.trim() || !!busy" not in src
    assert "optional — real source that shows the current value" in src
    # One-click Accept sends empty evidence
    assert "function oneClickAccept" in src
    assert src.index("function oneClickAccept") < src.index('evidence: ""', src.index("function oneClickAccept"))
    assert "Evidence is optional" in src
    assert "dashboard dry-run" not in src
    assert "window.prompt" not in src
    css = (ROOT / "dashboard" / "dist" / "style.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in css
    assert ":focus-visible" in css
    assert ".oneiros-card[data-state=\"pending\"]" in css
    assert ".oneiros-card[data-state=\"accept\"]" in css
    assert ".oneiros-card[data-state=\"applied\"]" in css
    assert "oneiros-apply-scroll" in src
    assert "oneiros-apply-foot" in src
    assert "oneiros-apply-row" in src
    assert "function pageParams" in src
    assert "function withPageQuery" in src
    assert "Snooze for a week" in src
    assert "/reminder/snooze" in src
    assert 'data-day' in src
    assert 'data-profile' in src
    assert "oneiros-apply-shell" in css
    assert "overscroll-behavior: contain" in css
