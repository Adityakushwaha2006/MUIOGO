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


def test_local_folder_update_action_is_manual(page, base_url):
    page.goto(f"{base_url}/#/OGCore")
    html = page.evaluate("""async () => {
        const { default: OGCore } = await import(new URL('App/Controller/OGCore.js', location.href).href);
        return OGCore.actionsHtml(
            { install_state: 'update_available' },
            { source_type: 'local_path' },
        );
    }""")
    assert 'data-act="update"' not in html
    assert 'data-act="check"' in html
    assert 'Update the local folder' in html


def test_changed_add_form_disables_previous_check(page, base_url):
    page.goto(f"{base_url}/#/OGCore")
    expect(page.locator(".ogc-page")).to_be_visible()
    page.evaluate("""async () => {
        const { default: OGCore } = await import(new URL('App/Controller/OGCore.js', location.href).href);
        OGCore.openAdd();
        OGCore.checkedValues = { source: '/tmp/OG-KEN', label: 'Kenya', code: 'KEN', valid: true };
    }""")
    page.locator("#ogcAddSource").fill("/tmp/OG-ETH")
    expect(page.locator('[data-act="add-confirm"]')).to_be_disabled()


def test_failed_job_reopens_with_retry_action(page, base_url):
    page.goto(f"{base_url}/#/OGCore")
    expect(page.locator(".ogc-page")).to_be_visible()
    result = page.evaluate("""async () => {
        const { default: OGCore } = await import(new URL('App/Controller/OGCore.js', location.href).href);
        OGCore.model = { calibrations: [], records: {} };
        $('#ogcGrid').html(OGCore.cardHtml({
            country_id: 'KEN', country_name: 'Kenya', install_state: 'installing'
        }));
        OGCore.applyJob('KEN', {
            country_id: 'KEN', country_name: 'Kenya', install_state: 'failed',
            log_tail: ['failed'], error: 'install failed'
        }, OGCore.pageID);
        OGCore.openLog('KEN');
        return {
            heading: $('#ogcModalHead').text(),
            retry: $('#ogcModalFoot [data-act="retry-modal"]').length,
        };
    }""")
    assert 'install failed' in result['heading']
    assert result['retry'] == 1


def test_navigation_invalidates_old_og_page_load(page, base_url):
    page.goto(f"{base_url}/#/OGCore")
    expect(page.locator(".ogc-page")).to_be_visible()
    old_page_id = page.evaluate("""async () => {
        const { default: OGCore } = await import(new URL('App/Controller/OGCore.js', location.href).href);
        return OGCore.pageID;
    }""")
    page.evaluate("""async () => {
        const { default: OGCore } = await import(new URL('App/Controller/OGCore.js', location.href).href);
        OGCore.invalidatePage();
    }""")
    result = page.evaluate("""async (oldPageID) => {
        const { default: OGCore } = await import(new URL('App/Controller/OGCore.js', location.href).href);
        return { old_is_current: OGCore.isCurrent(oldPageID), new_page_id: OGCore.pageID };
    }""", old_page_id)
    assert result['old_is_current'] is False
    assert result['new_page_id'] > old_page_id


def test_og_page_survives_round_trip_navigation(page, base_url):
    """OG -> CLEWS -> OG must re-render the grid; a stale PAGE_ID would leave it empty."""
    page.goto(f"{base_url}/#/OGCore")
    expect(page.locator(".ogc-addcard")).to_be_visible()
    page.evaluate("window.__stamp = 'og1'")

    page.goto(f"{base_url}/#/Config")
    expect(page.locator("body.osy-mode-clews")).to_have_count(1)
    # #osy-title is shared across CLEWS views; waiting for it works around a real
    # router race where a late .load() callback can paint the previous view over
    # the current OGCore view.
    expect(page.locator("#osy-title")).to_be_visible()

    page.goto(f"{base_url}/#/OGCore")
    expect(page.locator("body.osy-mode-og")).to_have_count(1)
    # if the stamp is gone the browser reloaded, and the test is not exercising
    # a round trip within one document — which is the whole point
    assert page.evaluate("window.__stamp") == 'og1'
    expect(page.locator(".ogc-page")).to_be_visible()
    # the last thing renderGrid appends: present only if the reload was not
    # discarded as stale work from the previous visit
    expect(page.locator(".ogc-addcard")).to_be_visible()


def test_polling_stops_when_leaving_og_page(page, base_url):
    """No OG-Core requests continue after leaving the OG page."""
    page.goto(f"{base_url}/#/OGCore")
    expect(page.locator(".ogc-addcard")).to_be_visible()

    calls = []
    page.on("request", lambda r: calls.append(r.url) if "/ogc/" in r.url else None)

    # a bogus id produces an observable OG-Core request before the page is left.
    # This test covers the no-traffic guarantee; the page-ID bump mechanism is
    # covered by test_navigation_invalidates_old_og_page_load.
    page.evaluate("""async () => {
        const { default: OGCore } = await import(new URL('App/Controller/OGCore.js', location.href).href);
        OGCore.pollJob('KEN', 'no-such-job', OGCore.pageID);
        location.hash = '#/Config';
    }""")
    expect(page.locator("body.osy-mode-clews")).to_have_count(1)
    assert len(calls) > 0, "pollJob issued no request; the test would pass vacuously"
    settled = len(calls)
    page.wait_for_timeout(8_000)          # > 2x POLL_MS (3500)
    assert len(calls) == settled, f"polling outlived the page: {calls[settled:]}"
