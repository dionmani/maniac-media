"""Browser end-to-end tests (Playwright).

Starts a real server on a free port against an empty temporary data directory and drives the
built SPA. Run with:  pytest tests/e2e -m e2e   (requires `playwright install chromium` and a
built frontend in frontend/dist).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
playwright = pytest.importorskip("playwright.sync_api")

if not (ROOT / "frontend" / "dist" / "index.html").exists():
    pytest.skip("frontend not built (npm run build)", allow_module_level=True)

PASSWORD = "E2e-Passw0rd!"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    data = Path(tempfile.mkdtemp(prefix="maniac-e2e-"))
    lib = data / "lib"
    for d in ("movies", "tv", "downloads"):
        (lib / d).mkdir(parents=True)
    (data / "config.json").write_text(
        '{"media_root_movies": "%s", "media_root_tv": "%s", "download_folder": "%s", "watch_download_folder": false}'
        % tuple(str(lib / d).replace("\\", "\\\\") for d in ("movies", "tv", "downloads"))
    )
    env = {**os.environ, "MANIAC_DATA_DIR": str(data), "MANIAC_MASTER_KEY": "e2e-master-key", "MANIAC_PORT": str(port), "MANIAC_LOG_LEVEL": "WARNING",
           "MANIAC_BACKGROUND_WORKERS": "1"}  # conftest turns them off for in-process tests; the real server needs them
    proc = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("server did not start")
    yield base
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binaries not installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    ctx.close()


def _login(page, base, username="admin", password=PASSWORD):
    page.goto(base)
    page.locator("input[autocomplete=username]").fill(username)
    page.locator("input[type=password]").first.fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_selector("text=Dashboard")


def test_first_run_setup_creates_admin(server, page):
    page.goto(server)
    page.wait_for_selector("text=Create administrator")
    page.locator("input[autocomplete=username]").fill("admin")
    page.locator("input[type=password]").nth(0).fill(PASSWORD)
    page.locator("input[type=password]").nth(1).fill(PASSWORD)
    page.get_by_role("button", name="Create administrator").click()
    page.wait_for_selector("text=Library coverage")
    assert not page.errors


def test_every_page_renders(server, page):
    _login(page, server)
    for path, marker in [
        ("/library", "movies in your library"), ("/wanted", "Wanted"), ("/calendar", "Calendar"), ("/activity", "Queue is empty"),
        ("/import", "Manual import"), ("/renamer", "Renamer"), ("/settings?tab=quality", "HD-1080p"), ("/settings?tab=indexers", "No indexers yet"),
        ("/settings?tab=database", "Migration wizard"), ("/settings?tab=users", "Users & roles"), ("/system", "Health"), ("/system?tab=tasks", "RSS Sync"),
        ("/discover", "Discover needs a TMDB API key"), ("/settings?tab=metadata", "Original languages"), ("/system?tab=tasks", "Discover Refresh"),
    ]:
        page.goto(server + path)
        page.wait_for_selector(f"text={marker}", timeout=15000)
    assert not page.errors


def test_mode_switch_changes_library(server, page):
    _login(page, server)
    page.goto(server + "/library")
    page.wait_for_selector("text=movies in your library")
    page.get_by_role("button", name="TV Shows").first.click()
    page.wait_for_selector("text=series in your library")


def test_mode_toggle_opens_library(server, page):
    _login(page, server)
    page.goto(server + "/wanted")
    page.wait_for_selector("text=Wanted")
    page.get_by_role("button", name="Movies").first.click()
    page.wait_for_url("**/library")
    page.wait_for_selector("text=movies in your library")


def test_naming_preview_rejects_unknown_tokens(server, page):
    _login(page, server)
    page.goto(server + "/settings?tab=naming")
    field = page.get_by_label("Movie file")
    field.fill("{Title} {Bogus}")
    page.wait_for_selector("text=Unknown token {Bogus}")
    field.fill("{Title} ({Year})")
    page.wait_for_selector("text=Example: Avatar (2009)")


def test_issues_filters_come_from_the_url(server, page):
    """The dashboard's issue pills link to /issues?type=…; the page must open already filtered."""
    _login(page, server)
    page.goto(server + "/issues?type=duplicate_file&status=dismissed")
    page.wait_for_selector("text=Nothing dismissed matches these filters.")
    assert page.get_by_role("tab", name="Dismissed").get_attribute("aria-selected") == "true"
    assert page.get_by_role("combobox", name="Type").inner_text().strip() == "Duplicate files"
    page.get_by_role("tab", name="Open").click()
    page.wait_for_url("**/issues?type=duplicate_file")
    page.wait_for_selector("text=Nothing open matches these filters.")
    assert not page.errors


def test_demo_pipeline_through_ui(server, page):
    token = httpx.post(f"{server}/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    httpx.post(f"{server}/api/v1/indexers", json={"name": "Demo", "implementation": "demo"}, headers=h).raise_for_status()
    httpx.post(f"{server}/api/v1/download-clients", json={"name": "Sim", "client_type": "simulator"}, headers=h).raise_for_status()
    movie = httpx.post(f"{server}/api/v1/movies", json={"title": "Primer", "year": 2004}, headers=h, timeout=30).json()

    _login(page, server)
    page.goto(f"{server}/movies/{movie['id']}")
    page.get_by_role("button", name="Interactive search").click()
    page.wait_for_selector("text=Primer.2004", timeout=20000)
    page.get_by_role("button", name="Grab").first.click()
    page.goto(server + "/activity")
    page.wait_for_selector("text=Primer", timeout=10000)
    for _ in range(40):  # simulator finishes within 15 s, then the import runs
        detail = httpx.get(f"{server}/api/v1/movies/{movie['id']}", headers=h).json()
        if detail["has_file"]:
            break
        time.sleep(1)
    assert detail["has_file"] and detail["file_path"].endswith(".mkv")
    assert not page.errors


def test_read_only_user_cannot_see_admin_settings(server, page):
    token = httpx.post(f"{server}/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}).json()["access_token"]
    httpx.post(f"{server}/api/v1/users", json={"username": "viewer", "password": "Viewer-Passw0rd", "role": "read_only"},
               headers={"Authorization": f"Bearer {token}"}).raise_for_status()
    _login(page, server, "viewer", "Viewer-Passw0rd")
    assert page.locator("text=Add New").count() == 0
    page.goto(server + "/settings")
    page.wait_for_selector("text=Media management")
    assert page.get_by_role("tab", name="Users").count() == 0
    assert page.get_by_role("tab", name="Database").count() == 0
    assert page.get_by_role("tab", name="Media management").count() == 1
