"""Shared setup for the OG-Core case/run pipeline tests.

Points OG state at a per-test temp dir and clears RunJob's in-memory state, so no
test sees another's cases, runs, or queue. Autouse, scoped to this package only.
"""
import threading

import pytest

from Classes.Base import Config
from Classes.OGCore import RunJob as RunJobModule
from Classes.OGCore.CalibrationRegistry import CalibrationRegistry
from Classes.OGCore.OGCoreCase import OGCoreCase
from Classes.OGCore.RunJob import RunJob


def _drain_runs():
    """Let any supervision thread finish before clearing shared state.

    Blanking _active under a live worker would make it finalize into the previous
    test's directories and drain the next test's queue.
    """
    with RunJob._lock:
        active = RunJob._active
        thread = active.get("thread") if active else None
        RunJob._queue.clear()
    if thread is not None and thread.is_alive():
        thread.join(timeout=10)
    with RunJob._lock:
        RunJob._active = None
        RunJob._queue.clear()


@pytest.fixture(autouse=True)
def ogc_state(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "OGC_DATA_STORAGE", tmp_path)
    monkeypatch.setattr(
        Config, "OGC_INSTALLED_REGISTRY", tmp_path / "og_calibrations_installed.json"
    )
    monkeypatch.setattr(Config, "OGC_INSTALL_JOBS_DIR", tmp_path / "install_jobs")
    monkeypatch.setattr(Config, "OGC_CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(Config, "OGC_MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(Config, "OGC_CATALOG_CACHE", tmp_path / "catalog_cache.json")
    monkeypatch.setattr(Config, "OGC_INSTALLER_CACHE_DIR", tmp_path / "installer")
    _drain_runs()
    yield
    _drain_runs()


@pytest.fixture
def calibration(tmp_path):
    """An installed ETH calibration whose interpreter path exists on disk."""
    python_path = tmp_path / "venv_python"
    python_path.write_text("")
    CalibrationRegistry.upsert({
        "country_id": "ETH", "country_name": "Ethiopia", "source_type": "catalog",
        "python_path": str(python_path), "venv_path": str(tmp_path / ".venv"),
        "package_name": "ogcore", "install_state": "installed",
    })
    return python_path


@pytest.fixture
def make_case():
    """Build a case, optionally with runs. Returns a factory."""
    def _make(casename="c1", country_id="ETH", runs=()):
        case = OGCoreCase(casename)
        case.create_case({"ogc-casename": casename, "country_id": country_id})
        for run_name, run_type, baseline in runs:
            case.create_run(run_name, run_type, baseline, {})
        return case
    return _make


class FakeRunner:
    """Stands in for OGRunner: no subprocess, but the same surface RunJob drives.

    The worker is replaced, not the run machinery, so the real thread, supervision
    and finalize paths still execute. The solve blocks until released (or killed),
    which is what lets a test hold the execution slot.
    """

    instances = []

    def __init__(self):
        self.released = threading.Event()
        self.spawned = threading.Event()
        self.killed = False
        self.iteration = None
        self.rc = 0
        FakeRunner.instances.append(self)

    def spawn(self, python_path, run_dir):
        self.spawned.set()

    def supervise(self):
        self.released.wait(10)
        return self.rc

    def kill_tree(self):
        self.killed = True
        self.released.set()

    def log_tail(self):
        return []

    def write_log(self, run_dir):
        pass

    def finish(self, rc=0):
        """Let the solve complete with this exit code."""
        self.rc = rc
        self.released.set()


@pytest.fixture
def fake_runner(monkeypatch):
    """Run the real RunJob thread path against a worker that never spawns.

    Returns the list of FakeRunner instances, newest last, so a test can hold a run
    open, then release it and watch the queue advance.
    """
    FakeRunner.instances = []
    monkeypatch.setattr(RunJobModule, "OGRunner", FakeRunner)
    yield FakeRunner.instances
    for runner in FakeRunner.instances:
        runner.released.set()


@pytest.fixture
def stub_launch(monkeypatch):
    """Claim the execution slot without any worker at all.

    For tests that only care about queue bookkeeping (which run holds the slot, what
    order the queue drains) and never need a run to finish.
    """
    launched = []

    def fake_launch(cls, casename, run_name, time_path, python_path):
        launched.append((casename, run_name))
        cls._active = {"casename": casename, "run_name": run_name,
                      "runner": FakeRunner(), "thread": None, "cancelled": False}

    monkeypatch.setattr(RunJob, "_launch", classmethod(fake_launch))
    return launched
