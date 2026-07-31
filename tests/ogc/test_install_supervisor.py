"""Install supervisor: cancel, inactivity timeout, and process-tree kill (#501 additions).

Uses real subprocesses and the real code paths, not mocks. `_stream` returns
(returncode, reason) with reason in {None, 'cancelled', 'timeout'}.
"""
import sys
import threading
import time

from Classes.OGCore.CalibrationRegistry import CalibrationRegistry
from Classes.OGCore.InstallJob import InstallJob
from Classes.OGCore.Installer import Installer

PY = sys.executable


def wait_done(country_id, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if not InstallJob.is_country_active(country_id):
            return True
        time.sleep(0.02)
    return False


def test_stream_normal_exit_streams_output():
    lines = []
    rc, reason = Installer._stream([PY, "-c", "print('hello'); print('world')"], lines.append)
    assert rc == 0 and reason is None
    assert "hello" in lines and "world" in lines


def test_stream_preset_cancel_does_not_spawn():
    ev = threading.Event()
    ev.set()
    t0 = time.monotonic()
    rc, reason = Installer._stream([PY, "-c", "import time; time.sleep(30)"],
                                  lambda *_: None, cancel=ev)
    dt = time.monotonic() - t0
    assert reason == "cancelled"
    assert dt < 0.5, "pre-set cancel returns without spawning"


def test_stream_cancel_is_prompt():
    ev = threading.Event()
    holder = {}

    def run():
        t = time.monotonic()
        holder["rc"], holder["reason"] = Installer._stream(
            [PY, "-c", "import time; time.sleep(30)"], lambda *_: None,
            inactivity_timeout=600, cancel=ev)
        holder["dt"] = time.monotonic() - t

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.7)
    ev.set()
    t.join(10)
    assert holder.get("reason") == "cancelled"
    assert holder.get("dt", 99) < 3.0, "cancel takes effect within the ~1s poll"


def test_stream_inactivity_timeout():
    t0 = time.monotonic()
    rc, reason = Installer._stream([PY, "-c", "import time; time.sleep(30)"],
                                  lambda *_: None, inactivity_timeout=2, cancel=None)
    dt = time.monotonic() - t0
    assert reason == "timeout"
    assert dt < 5.0, "inactivity kill is prompt"


def test_process_tree_kill(tmp_path, monkeypatch):
    sentinel = tmp_path / "grandchild_ran.txt"
    monkeypatch.setenv("OGC_TEST_SENTINEL", str(sentinel))
    parent_code = (
        "import os,sys,subprocess,time;"
        "subprocess.Popen([sys.executable,'-c',"
        "\"import os,time,pathlib; time.sleep(3); "
        "pathlib.Path(os.environ['OGC_TEST_SENTINEL']).write_text('x')\"]);"
        "time.sleep(30)"
    )
    ev = threading.Event()
    t = threading.Thread(target=lambda: Installer._stream(
        [PY, "-c", parent_code], lambda *_: None, inactivity_timeout=600, cancel=ev))
    t.start()
    time.sleep(1.0)
    ev.set()
    t.join(10)
    time.sleep(4.0)
    assert not sentinel.exists(), "grandchild killed with the tree (no sentinel written)"


def _cancel_work():
    def work(progress, log, cancel):
        rc, reason = Installer._stream([PY, "-c", "import time; time.sleep(30)"], log,
                                       inactivity_timeout=600, cancel=cancel)
        if reason == "cancelled":
            return {"ok": False, "error": "Cancelled by user.", "local_path": None}
        return {"ok": True, "local_path": "x", "venv_path": "x/.venv",
                "python_path": "x/.venv/bin/python", "package_name": "p"}
    return work


def test_end_to_end_cancel_fresh_install():
    init = InstallJob._launch(country_id="ETH", country_name="Ethiopia",
                             source_type="catalog", work_fn=_cancel_work())
    iid = init["install_id"]
    time.sleep(1.0)
    assert InstallJob.cancel(iid) is True, "cancel() signals the running job"
    assert wait_done("ETH")
    job = InstallJob.get_status(iid)
    assert job["install_state"] == "failed" and job["error"] == "Cancelled by user."
    assert CalibrationRegistry.get("ETH")["install_state"] == "failed"


def test_end_to_end_cancel_update_preserved():
    CalibrationRegistry.upsert({"country_id": "ZAF", "country_name": "South Africa",
                                "source_type": "catalog", "install_state": "installed",
                                "venv_path": "/models/OG-ZAF/.venv",
                                "installed_at": "2026-01-01T00:00:00Z"})
    init = InstallJob._launch(country_id="ZAF", country_name="South Africa",
                             source_type="catalog", work_fn=_cancel_work())
    time.sleep(1.0)
    InstallJob.cancel(init["install_id"])
    assert wait_done("ZAF")
    rec = CalibrationRegistry.get("ZAF")
    assert rec["install_state"] == "installed", "cancelled update keeps the working install"
    assert rec.get("last_error") == "Cancelled by user."


def test_cancel_unknown_id_returns_false():
    assert InstallJob.cancel("install_2026_07_20_999") is False


def test_run_installer_maps_cancel_timeout_and_real_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(Installer, "ensure_installer_script", staticmethod(lambda: "dummy_script"))
    og_xx = tmp_path / "OG-XX"
    og_xx.mkdir()
    # A real install/update always has pyproject.toml; keep one here so the
    # unusable-leftover cleanup does not fire and skew this message-mapping check.
    (og_xx / "pyproject.toml").write_text("[project]\nname = 'x'\n")

    monkeypatch.setattr(Installer, "_stream", staticmethod(lambda *a, **k: (None, "cancelled")))
    ev = threading.Event()
    ev.set()
    res = Installer.run_installer(source_type="catalog", repo_name="OG-XX",
                                 dest_parent=str(tmp_path), catalog_key="og-xx", cancel=ev)
    assert res["ok"] is False and res["error"] == "Cancelled by user."

    monkeypatch.setattr(Installer, "_stream", staticmethod(lambda *a, **k: (None, "timeout")))
    res = Installer.run_installer(source_type="catalog", repo_name="OG-XX",
                                 dest_parent=str(tmp_path), catalog_key="og-xx", cancel=None)
    assert res["ok"] is False and "timed out" in res["error"]

    # A child that legitimately exits 124 must not be mislabeled as our timeout.
    monkeypatch.setattr(Installer, "_stream", staticmethod(lambda *a, **k: (124, None)))
    res = Installer.run_installer(source_type="catalog", repo_name="OG-XX",
                                 dest_parent=str(tmp_path), catalog_key="og-xx", cancel=None)
    assert res["ok"] is False and "124" in res["error"] and "timed out" not in res["error"]


def _run_over(tmp_path, monkeypatch):
    """Drive run_installer over an existing dir without running a real installer."""
    monkeypatch.setattr(
        Installer, "ensure_installer_script", staticmethod(lambda: "dummy_script")
    )
    monkeypatch.setattr(Installer, "_stream", staticmethod(lambda *a, **k: (1, None)))
    return Installer.run_installer(
        source_type="catalog", repo_name="OG-XX",
        dest_parent=str(tmp_path), catalog_key="og-xx", cancel=None,
    )


def test_unusable_git_leftover_is_cleared(tmp_path, monkeypatch):
    # A clone killed part way leaves .git behind with no working tree. The update
    # path cannot recover that, so it is removed and the install starts fresh.
    leftover = tmp_path / "OG-XX"
    (leftover / ".git").mkdir(parents=True)
    (leftover / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    _run_over(tmp_path, monkeypatch)

    assert not leftover.exists(), "the half-clone is cleared before re-cloning"


def test_unrelated_folder_of_the_same_name_is_kept(tmp_path, monkeypatch):
    # dest_parent comes from the caller, so a folder that was never a clone can sit
    # at the target path. It has no .git, so it must survive untouched.
    mine = tmp_path / "OG-XX"
    mine.mkdir()
    (mine / "notes.txt").write_text("do not delete me")

    _run_over(tmp_path, monkeypatch)

    assert (mine / "notes.txt").read_text() == "do not delete me"


def test_healthy_clone_is_not_cleared(tmp_path, monkeypatch):
    # A real clone has both, and an update over it must never delete it.
    clone = tmp_path / "OG-XX"
    (clone / ".git").mkdir(parents=True)
    (clone / "pyproject.toml").write_text("[project]\nname = 'x'\n")

    _run_over(tmp_path, monkeypatch)

    assert (clone / "pyproject.toml").exists() and (clone / ".git").exists()
