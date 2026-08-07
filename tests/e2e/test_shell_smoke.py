"""
Browser smoke test for the MUIOGO shell: model selector, per-model navigation
chrome, and the per-route model assertions, against the real app served by
waitress.

Runs only when pytest-playwright is installed (the dedicated CI job); the plain
pytest job skips this module.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("pytest_playwright")
from playwright.sync_api import expect

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTUP_TIMEOUT = 90  # seconds

expect.set_options(timeout=15_000)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base_url():
    """Real server on a free port, torn down after the session."""
    port = _free_port()
    env = dict(os.environ, PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "API" / "app.py")],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + STARTUP_TIMEOUT
    while True:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"app exited during startup (code {proc.returncode}):\n{out[-2000:]}")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    break
        except (urllib.error.URLError, ConnectionResetError, TimeoutError):
            pass
        if time.time() > deadline:
            proc.terminate()
            pytest.fail(f"app did not serve / within {STARTUP_TIMEOUT}s")
        time.sleep(0.25)
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# Each test gets a fresh browser context (empty localStorage), so every test
# starts from the no-model-selected state.

def test_fresh_visit_shows_model_pick(page, base_url):
    page.goto(base_url)
    expect(page.locator("body.osy-mode-none")).to_have_count(1)
    expect(page.locator(".osy-pickwrap")).to_be_visible()
    expect(page.locator("#osy-mb-og")).to_be_visible()
    expect(page.locator("#osy-mb-clews")).to_be_visible()


def test_switch_to_og(page, base_url):
    page.goto(base_url)
    page.locator("#osy-mb-og").click()
    expect(page.locator("body.osy-mode-og")).to_have_count(1)
    # page skeleton only: asserting catalog contents would depend on a live fetch
    expect(page.locator(".ogc-page")).to_be_visible()
    expect(page.locator("#Navi > li.nav-home")).to_be_visible()
    expect(page.locator("#Navi > li:not(.nav-home):visible")).to_have_count(0)
    expect(page.locator(".project-context")).to_be_hidden()


def test_switch_to_clews(page, base_url):
    page.goto(base_url)
    page.locator("#osy-mb-clews").click()
    expect(page.locator("body.osy-mode-clews")).to_have_count(1)
    expect(page.locator(".project-context")).to_be_visible()


def test_routes_assert_their_model(page, base_url):
    # with no model selected, the route itself must set the shell mode
    page.goto(f"{base_url}/#/Config")
    expect(page.locator("body.osy-mode-clews")).to_have_count(1)
    expect(page.locator(".project-context")).to_be_visible()
    page.goto(f"{base_url}/#/OGCore")
    expect(page.locator("body.osy-mode-og")).to_have_count(1)
    expect(page.locator(".ogc-page")).to_be_visible()
