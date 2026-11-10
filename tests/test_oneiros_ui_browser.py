"""Drive the real Oneiros page against a scratch profile. No live memory."""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from test_blank_evidence_apply import _cfg, _plant
from test_dashboard_api import _load_api

ROOT = Path(__file__).resolve().parents[1]
ART = Path("/opt/cursor/artifacts")
pytest.importorskip("playwright")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def scratch(home, monkeypatch):
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True, exist_ok=True)
    (home / "plugin-data" / "dream-cycle" / "config.yaml").write_text(
        "apply_enabled: true\n"
        "memory_globs: [MEMORY.md, MISTAKES.md, NOTES.md]\n"
    )
    stage, props = _plant(home)
    assert _cfg()["apply_enabled"] is True
    mod, _client = _load_api(monkeypatch, home)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/dream-cycle")
    app.mount("/dashboard", StaticFiles(directory=str(ROOT / "dashboard")), name="dashboard")
    app.mount("/tests", StaticFiles(directory=str(ROOT / "tests")), name="tests")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "tests" / "oneiros_harness.html")

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.05)
    else:
        raise RuntimeError("preview server did not start")
    yield {"url": f"http://127.0.0.1:{port}/", "home": home, "stage": stage, "props": props}
    server.should_exit = True
    thread.join(timeout=5)


def _shot(page, name: str) -> Path:
    ART.mkdir(parents=True, exist_ok=True)
    path = ART / name
    page.screenshot(path=str(path), full_page=False)
    return path


def test_review_page_accept_apply_and_rollback(scratch):
    from playwright.sync_api import sync_playwright

    home: Path = scratch["home"]
    url = scratch["url"]
    mem = home / "memories" / "MEMORY.md"
    mis = home / "memories" / "MISTAKES.md"
    notes = home / "memories" / "NOTES.md"
    before = (mem.read_text(), mis.read_text(), notes.read_text())
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector("article.oneiros-card", timeout=15000)
        assert page.locator("article.oneiros-card").count() == 3
        assert "Last night's dreams" in page.inner_text("body")
        page.locator("article.oneiros-card").nth(1).scroll_into_view_if_needed()
        _shot(page, "oneiros-pending-desktop.png")

        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.goto(url, wait_until="domcontentloaded")
        mobile.wait_for_selector("article.oneiros-card", timeout=15000)
        mobile.locator("article.oneiros-card").first.scroll_into_view_if_needed()
        _shot(mobile, "oneiros-pending-mobile.png")
        mobile.close()

        # Accept-with-note stays enabled when evidence is empty.
        page.get_by_role("button", name="Accept with note…").first.click()
        page.wait_for_selector("[role=dialog]")
        dialog = page.locator("[role=dialog]")
        assert "Evidence is optional" in dialog.inner_text()
        write = dialog.get_by_role("button", name="Write accept")
        assert write.is_enabled()
        _shot(page, "oneiros-accept-note.png")
        dialog.get_by_role("button", name="Cancel").click()
        page.wait_for_selector("[role=dialog]", state="detached")

        first = page.locator("article.oneiros-card").first
        pid = first.get_attribute("data-proposal-id")
        # Reach Accept from the keyboard so :focus-visible is real, then activate it.
        landed = False
        for _ in range(24):
            page.keyboard.press("Tab")
            label = page.evaluate("() => (document.activeElement && document.activeElement.innerText || '').trim()")
            if label == "Accept":
                landed = True
                break
        assert landed, "Accept was not in tab order"
        outline = page.evaluate("() => getComputedStyle(document.activeElement).outlineStyle")
        assert outline == "solid"
        page.keyboard.press("Enter")
        page.wait_for_selector(f'article[data-proposal-id="{pid}"][data-state="accept"]', timeout=10000)
        assert (mem.read_text(), mis.read_text(), notes.read_text()) == before
        ledger = next((home / "plugin-data/dream-cycle/dream-proposals").glob("*/review.jsonl"))
        rows = [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]
        assert rows[-1]["proposal_id"] == pid
        assert rows[-1]["decision"] == "accept"
        assert rows[-1]["evidence"] == ""

        page.get_by_role("button", name="Accept all").click()
        page.wait_for_selector(".oneiros-undo-bar", timeout=10000)
        assert page.locator('article[data-state="accept"]').count() == 3
        assert (mem.read_text(), mis.read_text(), notes.read_text()) == before
        before_undo = ledger.read_text()
        page.get_by_role("button", name="Undo").click()
        page.wait_for_selector(".oneiros-undo-bar", state="detached", timeout=10000)
        after_undo = ledger.read_text()
        assert after_undo.startswith(before_undo)
        assert after_undo.count('"decision": "reverted"') == 2
        # the keyboard accept was not part of the batch
        page.wait_for_selector(f'article[data-proposal-id="{pid}"][data-state="accept"]')
        assert page.locator('article[data-state="reverted"]').count() == 2

        reverted = page.locator('article[data-state="reverted"]').first
        reverted.get_by_role("button", name="Reject").click()
        reject_dialog = page.locator("[role=dialog]")
        reject_write = reject_dialog.get_by_role("button", name="Write reject")
        assert reject_write.is_disabled()
        reject_dialog.locator("#oneiros-note").fill("scratch reject")
        assert reject_write.is_enabled()
        reject_write.click()
        page.wait_for_selector("[role=dialog]", state="detached")
        page.wait_for_selector('article[data-state="reject"]', timeout=10000)
        page.locator("article.oneiros-card").nth(1).scroll_into_view_if_needed()
        _shot(page, "oneiros-mixed-states.png")

        page.get_by_role("button", name="Apply accepted…").click()
        page.wait_for_selector("[role=dialog]")
        apply_dialog = page.locator("[role=dialog]")
        assert "Evidence is optional" in apply_dialog.inner_text()
        assert "dashboard dry-run" not in apply_dialog.inner_text()
        assert "because" not in apply_dialog.inner_text()
        _shot(page, "oneiros-apply-dialog.png")
        apply_dialog.get_by_role("button", name="Apply now").click()
        page.wait_for_selector("[role=dialog]", state="detached")
        page.wait_for_selector('article[data-state="applied"]', timeout=10000)
        changed = (mem.read_text(), mis.read_text(), notes.read_text())
        assert changed != before
        superseded = [
            ln for blob in changed for ln in blob.splitlines() if ln.startswith("SUPERSEDED")
        ]
        assert superseded and all("because" not in ln for ln in superseded)
        page.locator('article[data-state="applied"]').first.scroll_into_view_if_needed()
        _shot(page, "oneiros-applied.png")

        page.get_by_role("button", name="Rollback…").click()
        page.wait_for_selector("[role=dialog]")
        page.locator("[role=dialog]").get_by_role("button", name="Rollback now").click()
        page.wait_for_selector("[role=dialog]", state="detached")
        page.wait_for_timeout(400)
        assert (mem.read_text(), mis.read_text(), notes.read_text()) == before

        quiet = browser.new_page(viewport={"width": 1280, "height": 900})
        quiet.emulate_media(reduced_motion="reduce")
        quiet.goto(url, wait_until="domcontentloaded")
        quiet.wait_for_selector("article.oneiros-card")
        anim = quiet.eval_on_selector("article.oneiros-card", "el => getComputedStyle(el).animationName")
        assert anim == "none"
        browser.close()

    assert not errors, errors


def _in_view(page, locator) -> None:
    box = locator.bounding_box()
    assert box, locator
    vp = page.viewport_size
    assert box["y"] >= -1, box
    assert box["y"] + box["height"] <= vp["height"] + 1, (box, vp)
    assert box["x"] >= -1, box
    assert box["x"] + box["width"] <= vp["width"] + 1, (box, vp)


def _plant_batch(home: Path, n: int = 32):
    from datetime import date

    from dream_cycle.config import load_config
    from dream_cycle.propose import propose
    from dream_cycle.review import accept_pending
    from dream_cycle.sessions import SessionResult

    mem = home / "memories"
    mem.mkdir(parents=True, exist_ok=True)
    cfg_dir = home / "plugin-data" / "dream-cycle"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "apply_enabled: true\n"
        "max_proposals_per_run: 40\n"
        "memory_globs: ['batch-*.md']\n"
    )
    for i in range(n):
        (mem / f"batch-{i:02d}.md").write_text(
            f"batch-svc listens on port :{20000 + i}\n"
            f"batch-svc endpoint url http://localhost:{30000 + i}/v1\n"
        )
    cfg = load_config(cfg_dir / "config.yaml")
    result = propose(
        home, config=cfg, run_date=date(2026, 11, 8),
        session_result=SessionResult("disabled"),
        cost_result={"status": "unavailable", "detail": "test"},
    )
    stage = Path(result.stage_path)
    props = json.loads((stage / "summary.json").read_text())["proposals"]
    assert len(props) >= 30, len(props)
    accepted = accept_pending(stage, props, "guardian", evidence="")
    assert accepted["count"] >= 30
    return stage, props


def _close_details(page) -> None:
    page.evaluate(
        "() => document.querySelectorAll('details.oneiros-apply-row').forEach(d => { d.open = false; })"
    )


def _assert_apply_controls(page, *, keyboard: bool) -> None:
    shell = page.locator(".oneiros-apply-shell")
    page.wait_for_selector(".oneiros-apply-shell", timeout=20000)
    _close_details(page)
    ready = int(shell.get_attribute("data-ready") or "0")
    assert ready >= 30, shell.get_attribute("data-ready")
    title = page.get_by_role("heading", name="Apply accepted dreams")
    cancel = page.get_by_role("button", name="Cancel")
    apply = page.get_by_role("button", name="Apply now")
    _in_view(page, title)
    _in_view(page, cancel)
    _in_view(page, apply)
    assert apply.is_enabled()
    assert page.locator("details.oneiros-apply-row[open]").count() == 0
    metrics = page.locator(".oneiros-apply-scroll").evaluate(
        "el => ({client: el.clientHeight, scroll: el.scrollHeight})"
    )
    assert metrics["client"] > 40
    assert metrics["scroll"] > metrics["client"]
    page.locator(".oneiros-apply-scroll").evaluate("el => { el.scrollTop = el.scrollHeight; }")
    assert page.evaluate("() => window.scrollY") == 0
    _in_view(page, title)
    _in_view(page, apply)
    _in_view(page, cancel)
    box = page.locator(".oneiros-apply-scroll").bounding_box()
    page.mouse.move(box["x"] + 12, box["y"] + min(24, box["height"] / 2))
    page.mouse.wheel(0, 800)
    assert page.evaluate("() => window.scrollY") == 0
    _in_view(page, apply)
    if not keyboard:
        return
    page.locator("details.oneiros-apply-row summary").first.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("details.oneiros-apply-row[open] pre")
    _in_view(page, title)
    _in_view(page, apply)
    found = False
    for _ in range(60):
        page.keyboard.press("Tab")
        label = page.evaluate("() => (document.activeElement && document.activeElement.innerText || '').trim()")
        if label == "Apply now":
            found = True
            break
    assert found, "Apply now was not reached from the keyboard"
    _in_view(page, page.get_by_role("button", name="Apply now"))
    _in_view(page, page.get_by_role("button", name="Cancel"))
    _close_details(page)


@pytest.fixture
def batch(home, monkeypatch):
    stage, props = _plant_batch(home, 32)
    mod, _client = _load_api(monkeypatch, home)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/dream-cycle")
    app.mount("/dashboard", StaticFiles(directory=str(ROOT / "dashboard")), name="dashboard-batch")
    app.mount("/tests", StaticFiles(directory=str(ROOT / "tests")), name="tests-batch")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "tests" / "oneiros_harness.html")

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.05)
    else:
        raise RuntimeError("preview server did not start")
    yield {"url": f"http://127.0.0.1:{port}/", "home": home, "stage": stage, "count": len(props)}
    server.should_exit = True
    thread.join(timeout=5)


def test_large_batch_apply_dialog_keeps_controls_visible(batch):
    """30+ dry-runs at the reported narrow size, a desktop size, and a short screen."""
    from playwright.sync_api import sync_playwright

    home: Path = batch["home"]
    before = sorted((home / "memories").glob("batch-*.md"))
    digest = [(p.name, p.read_bytes()) for p in before]
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 649, "height": 960})
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(batch["url"], wait_until="domcontentloaded")
        page.get_by_role("button", name="Apply accepted…").click()
        _assert_apply_controls(page, keyboard=True)
        _shot(page, "oneiros-apply-batch-narrow.png")

        page.set_viewport_size({"width": 1280, "height": 900})
        page.wait_for_timeout(100)
        _assert_apply_controls(page, keyboard=False)
        _shot(page, "oneiros-apply-batch-desktop.png")

        page.set_viewport_size({"width": 1100, "height": 480})
        page.wait_for_timeout(100)
        _assert_apply_controls(page, keyboard=True)
        _shot(page, "oneiros-apply-batch-short.png")

        quiet = browser.new_page(viewport={"width": 649, "height": 960})
        quiet.emulate_media(reduced_motion="reduce")
        quiet.on("pageerror", lambda err: errors.append(str(err)))
        quiet.goto(batch["url"], wait_until="domcontentloaded")
        quiet.get_by_role("button", name="Apply accepted…").click()
        quiet.wait_for_selector(".oneiros-apply-shell", timeout=20000)
        anim = quiet.eval_on_selector(".oneiros-apply-shell", "el => getComputedStyle(el).animationName")
        assert anim == "none"
        _in_view(quiet, quiet.get_by_role("button", name="Apply now"))
        _in_view(quiet, quiet.get_by_role("heading", name="Apply accepted dreams"))
        quiet.get_by_role("button", name="Cancel").click()
        quiet.wait_for_selector(".oneiros-apply-shell", state="detached")
        browser.close()

    assert [(p.name, p.read_bytes()) for p in sorted((home / "memories").glob("batch-*.md"))] == digest
    assert not errors, errors


def test_deep_link_selects_profile_and_day_in_the_page(tmp_path, monkeypatch):
    """The Oneiros page loads the profile and dream day from the URL."""
    from playwright.sync_api import sync_playwright

    root = tmp_path / "hermes"
    home = root / "profiles" / "fleetbot"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("scratch\n")
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    (home / "plugin-data" / "dream-cycle" / "config.yaml").write_text("apply_enabled: true\n")
    for day, pid, note in (
        ("2026-11-08", "aaaa1111bbbb", "november dream"),
        ("2026-10-01", "cccc2222dddd", "october dream"),
    ):
        stage = home / "plugin-data/dream-cycle/dream-proposals" / day
        stage.mkdir(parents=True)
        (stage / "summary.json").write_text(json.dumps({
            "proposals": [{
                "proposal_id": pid, "label": "port", "values": ["1", "2"],
                "file": "MEMORY.md", "line_nos": [1], "note": note,
            }]
        }))
    mod, _client = _load_api(monkeypatch, home)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/dream-cycle")
    app.mount("/dashboard", StaticFiles(directory=str(ROOT / "dashboard")), name="deep-dash")
    app.mount("/tests", StaticFiles(directory=str(ROOT / "tests")), name="deep-tests")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "tests" / "oneiros_harness.html")

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.05)
    else:
        raise RuntimeError("preview server did not start")
    try:
        seen = []
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chrome", headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("request", lambda req: seen.append(req.url))
            page.goto(
                f"http://127.0.0.1:{port}/?profile=fleetbot&day=2026-10-01",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector('[data-day="2026-10-01"]', timeout=15000)
            assert page.locator(".oneiros-page").get_attribute("data-profile") == "fleetbot"
            assert page.locator(".oneiros-page").get_attribute("data-day") == "2026-10-01"
            body = page.inner_text("body")
            assert "october dream" in body
            assert "november dream" not in body
            assert "2026-10-01" in body
            _shot(page, "oneiros-deep-link-day.png")
            hits = [u for u in seen if "/proposals" in u]
            assert hits and "profile=fleetbot" in hits[0] and "day=2026-10-01" in hits[0]
            page.get_by_role("button", name="Snooze for a week").click()
            page.wait_for_selector("text=reminders snoozed until", timeout=10000)
            browser.close()
        state = json.loads((home / "plugin-data/dream-cycle/review-reminder.json").read_text())
        assert state.get("snooze_until")
        assert (home / "memories" / "MEMORY.md").read_text() == "scratch\n"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
