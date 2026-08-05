"""Registry tracking for install jobs (issue #501).

A record is written the moment an install starts and updated on failure, not only on
success; a failed update over a working install is preserved; the catalogue reflects
every state and carries the install_id. Drives the real InstallJob thread path against
a temp registry, and the real routes through the app test client.
"""
import threading
import time

from Classes.OGCore.CalibrationCatalog import CalibrationCatalog
from Classes.OGCore.CalibrationRegistry import CalibrationRegistry
from Classes.OGCore.InstallJob import InstallJob


def wait_done(country_id, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if not InstallJob.is_country_active(country_id):
            return True
        time.sleep(0.02)
    return False


def good_result():
    return {
        "ok": True,
        "local_path": "/models/OG-ETH",
        "venv_path": "/models/OG-ETH/.venv",
        "python_path": "/models/OG-ETH/.venv/bin/python",
        "package_name": "ogeth",
        "repo_url": "https://github.com/EAPD-DRB/OG-ETH",
        "commit_sha": "abc123",
    }


def test_fresh_install_installing_then_installed():
    entered, release = threading.Event(), threading.Event()

    def gated_ok(progress, log, cancel):
        entered.set()
        release.wait(5)
        return good_result()

    InstallJob._launch(country_id="ETH", country_name="Ethiopia",
                       source_type="catalog", work_fn=gated_ok)
    try:
        assert entered.wait(5), "job thread started"

        mid = CalibrationRegistry.get("ETH")
        assert mid is not None and mid.get("install_state") == "installing"
        assert mid.get("install_id"), "record carries install_id mid-job"
        assert not mid.get("venv_path"), "fresh record has no venv_path yet"

        release.set()
        assert wait_done("ETH"), "job finished"
        done = CalibrationRegistry.get("ETH")
        assert done.get("install_state") == "installed"
        assert done.get("install_id"), "install_id retained on success"
        assert done.get("installed_at") and not done.get("last_updated_at"), \
            "first install sets installed_at, not last_updated_at"
        assert done.get("venv_path") == "/models/OG-ETH/.venv"
    finally:
        # Always let the worker finish, so a failed assertion above cannot leave a
        # daemon thread that later writes into the next test's registry.
        release.set()
        wait_done("ETH")


def test_fresh_install_failure_records_last_error():
    InstallJob._launch(country_id="ETH", country_name="Ethiopia", source_type="catalog",
                       work_fn=lambda p, l, c: {"ok": False, "error": "clone died"})
    assert wait_done("ETH")
    rec = CalibrationRegistry.get("ETH")
    assert rec and rec.get("install_state") == "failed"
    assert rec.get("last_error") == "clone died"


def test_failed_update_preserves_working_install():
    CalibrationRegistry.upsert({
        "country_id": "ETH", "country_name": "Ethiopia", "source_type": "catalog",
        "venv_path": "/models/OG-ETH/.venv",
        "python_path": "/models/OG-ETH/.venv/bin/python",
        "install_state": "installed", "installed_at": "2026-01-01T00:00:00Z",
    })
    InstallJob._launch(country_id="ETH", country_name="Ethiopia", source_type="catalog",
                       work_fn=lambda p, l, c: {"ok": False, "error": "update failed"})
    assert wait_done("ETH")
    rec = CalibrationRegistry.get("ETH")
    assert rec.get("install_state") == "installed", "working install preserved"
    assert rec.get("last_error") == "update failed"
    assert rec.get("venv_path") == "/models/OG-ETH/.venv"
    assert rec.get("installed_at") == "2026-01-01T00:00:00Z"


def test_successful_update_sets_last_updated_at():
    CalibrationRegistry.upsert({
        "country_id": "ETH", "country_name": "Ethiopia", "source_type": "catalog",
        "venv_path": "/old/.venv", "python_path": "/old/.venv/bin/python",
        "install_state": "installed", "installed_at": "2026-01-01T00:00:00Z",
    })
    InstallJob._launch(country_id="ETH", country_name="Ethiopia", source_type="catalog",
                       work_fn=lambda p, l, c: good_result())
    assert wait_done("ETH")
    rec = CalibrationRegistry.get("ETH")
    assert rec.get("install_state") == "installed"
    assert rec.get("installed_at") == "2026-01-01T00:00:00Z", "installed_at preserved"
    assert rec.get("last_updated_at"), "last_updated_at set on a real update"
    assert "last_error" not in rec, "stale last_error dropped on success"


def test_catalog_reflects_state_and_install_id(monkeypatch):
    monkeypatch.setattr(CalibrationCatalog, "fetch_register", classmethod(lambda cls: (
        [{"country_id": "ETH", "country_name": "Ethiopia", "catalog_key": "og-eth"},
         {"country_id": "ZAF", "country_name": "South Africa", "catalog_key": "og-zaf"}],
        "live",
    )))
    CalibrationRegistry.upsert({"country_id": "ETH", "country_name": "Ethiopia",
                                "source_type": "catalog", "install_state": "installing",
                                "install_id": "install_x"})
    countries, _ = CalibrationCatalog.get_catalog_with_state()
    by_id = {c["country_id"]: c for c in countries}
    assert by_id["ETH"]["install_state"] == "installing"
    assert by_id["ETH"].get("install_id") == "install_x", "carries install_id"
    assert by_id["ZAF"]["install_state"] == "not_installed"
    assert by_id["ZAF"].get("install_id") is None

    CalibrationRegistry.update_fields("ETH", install_state="failed", last_error="boom")
    countries, _ = CalibrationCatalog.get_catalog_with_state()
    by_id = {c["country_id"]: c for c in countries}
    assert by_id["ETH"]["install_state"] == "failed"


def test_refresh_during_active_install_is_refused(client):
    """Regression guard: a record exists at start, so refresh must not clobber it."""
    entered, release = threading.Event(), threading.Event()

    def gated_ok(progress, log, cancel):
        entered.set()
        release.wait(5)
        return good_result()

    InstallJob._launch(country_id="ETH", country_name="Ethiopia",
                       source_type="catalog", work_fn=gated_ok)
    try:
        assert entered.wait(5), "install job is running"

        resp = client.post("/ogc/refreshCalibration",
                           json={"country_id": "ETH", "check_only": True})
        body = resp.get_json()
        assert body.get("status_code") == "error", f"refresh refused while installing: {body}"
        assert "already running" in body.get("message", "").lower()
        mid = CalibrationRegistry.get("ETH")
        assert mid.get("install_state") == "installing", "state not clobbered by refresh"

        release.set()
        assert wait_done("ETH")
        assert CalibrationRegistry.get("ETH").get("install_state") == "installed"
    finally:
        release.set()
        wait_done("ETH")


def test_cancel_install_input_validation(client):
    r = client.post("/ogc/cancelInstall", json={"install_id": 123})
    assert r.status_code == 400, "non-string install_id -> 400 not 500"
    r = client.post("/ogc/cancelInstall", json={"install_id": "install_2026_07_20_777"})
    assert r.status_code == 404, "unknown well-formed install_id -> 404"
    r = client.post("/ogc/cancelInstall", json={})
    assert r.status_code == 400, "missing install_id -> 400"
