"""
Production-context execution test.

In deployment the app is served by waitress, which dispatches each request on a
worker thread from a pool, not the main thread. OGCoreRunner.run() spins up a
process-based Dask cluster, and process spawning from a non-main thread on
Windows is the one execution path the other suites don't cover (they run on
pytest's or a script's main thread).

This test runs a full reduced baseline (SS + TPI, so the client.scatter path is
exercised too) from inside a worker thread and asserts it completes, proving the
client spawns and the model solves in the real server threading context.

Marked ``slow`` (one ~2-minute solve); opt in with ``pytest -m slow``.
"""

import threading

import pytest

from _ogcore_specs import reduced_specs  # reuse the convergent reduced config


# isolated_storage fixture is provided by conftest.py.


@pytest.mark.slow
def test_run_from_worker_thread(isolated_storage):
    """run() must spawn its process Dask client and solve from a non-main thread."""
    from Classes.OGCore.OGCoreClass import OGCoreCase
    from Classes.OGCore.OGCoreRunnerClass import OGCoreRunner

    base_spec, _ = reduced_specs()

    case = OGCoreCase("verif")
    case.create_case({"ogc-casename": "verif"})
    case.create_run("Base", "baseline", None, params=base_spec)

    result: dict = {}

    def _worker():
        try:
            result["resp"] = OGCoreRunner("verif").run("Base", time_path=True)
        except BaseException as exc:  # noqa: BLE001 (capture anything so the assert can fail)
            result["exc"] = exc

    t = threading.Thread(target=_worker, name="waitress-like-worker")
    t.start()
    t.join(timeout=600)

    assert not t.is_alive(), "run() hung in a worker thread (Dask client never returned)"
    assert "exc" not in result, f"run() raised in worker thread: {result.get('exc')!r}"
    assert result["resp"]["status_code"] == "success", result["resp"]
    # the model actually wrote its steady-state output from the worker thread
    assert (isolated_storage / "verif" / "res" / "Base" / "SS" / "SS_vars.pkl").is_file()
