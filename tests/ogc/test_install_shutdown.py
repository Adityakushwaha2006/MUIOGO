"""Shutdown cleanup: cancel_all / active_count and the server-stop handler.

Installs run detached in their own process group, so a plain server stop would orphan
them. cancel_all signals every running job; _stop_inflight_installs (called from the
signal handler and atexit) does that and waits briefly for them to tear down.
"""
import threading
import time

from app import _stop_inflight_installs
from Classes.OGCore.InstallJob import InstallJob


def test_cancel_all_sets_every_event_and_reports_ids():
    ev1, ev2 = threading.Event(), threading.Event()
    with InstallJob._lock:
        InstallJob._cancel_by_id["install_a"] = ev1
        InstallJob._cancel_by_id["install_b"] = ev2
        InstallJob._active_by_country["ETH"] = "install_a"
        InstallJob._active_by_country["ZAF"] = "install_b"
    assert InstallJob.active_count() == 2
    ids = InstallJob.cancel_all()
    assert set(ids) == {"install_a", "install_b"}
    assert ev1.is_set() and ev2.is_set()


def test_cancel_all_empty_is_noop():
    assert InstallJob.cancel_all() == []
    assert InstallJob.active_count() == 0


def test_stop_inflight_installs_cancels_running_job():
    entered = threading.Event()

    def work(progress, log, cancel):
        entered.set()
        cancel.wait(5)
        return {"ok": False, "error": "Cancelled by user.", "local_path": None}

    InstallJob._launch(country_id="ETH", country_name="Ethiopia",
                       source_type="catalog", work_fn=work)
    assert entered.wait(5), "job is running"
    assert InstallJob.active_count() == 1

    t0 = time.monotonic()
    _stop_inflight_installs()
    dt = time.monotonic() - t0
    assert InstallJob.active_count() == 0, "job torn down after shutdown cleanup"
    assert dt < 5.0, "cleanup returns promptly once the job clears"


def test_stop_inflight_installs_noop_when_idle():
    # No running jobs: returns immediately without waiting out the deadline.
    t0 = time.monotonic()
    _stop_inflight_installs()
    assert time.monotonic() - t0 < 1.0
