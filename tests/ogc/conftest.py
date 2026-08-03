"""Shared setup for the OG-Core install tests.

Points all OG on-disk state at a per-test temp dir and resets InstallJob's in-memory
class state, so every test runs against a clean registry with no leftover jobs. Autouse,
but scoped to this package only, so it never touches the rest of the suite.
"""
import time

import pytest

from Classes.Base import Config
from Classes.OGCore.CalibrationCatalog import CalibrationCatalog
from Classes.OGCore.InstallJob import InstallJob


def _clear_jobs():
    with InstallJob._lock:
        InstallJob._jobs.clear()
        InstallJob._active_by_country.clear()
        InstallJob._cancel_by_id.clear()
        InstallJob._shutting_down = False


def _drain_jobs(timeout=6.0):
    """Wait for any launched worker to finish before clearing state.

    A test that fails before releasing a gated worker leaves a daemon thread that would
    otherwise wake later and finalize into the next test's (freshly monkeypatched)
    registry. Draining here keeps one failure from cascading into unrelated tests.
    """
    deadline = time.monotonic() + timeout
    while InstallJob.active_count() and time.monotonic() < deadline:
        time.sleep(0.05)


@pytest.fixture(autouse=True)
def ogc_state(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "OGC_DATA_STORAGE", tmp_path)
    monkeypatch.setattr(
        Config, "OGC_INSTALLED_REGISTRY", tmp_path / "og_calibrations_installed.json"
    )
    monkeypatch.setattr(Config, "OGC_INSTALL_JOBS_DIR", tmp_path / "install_jobs")
    monkeypatch.setattr(Config, "OGC_CATALOG_CACHE", tmp_path / "catalog_cache.json")
    monkeypatch.setattr(Config, "OGC_MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(Config, "OGC_INSTALLER_CACHE_DIR", tmp_path / "installer")
    # Hard sandbox: never let a test reach the live register over the network. A test
    # that needs catalogue entries stubs fetch_register itself; this is the safe default.
    monkeypatch.setattr(
        CalibrationCatalog, "fetch_register", classmethod(lambda cls: ([], "none"))
    )
    _clear_jobs()
    yield
    _drain_jobs()
    _clear_jobs()
