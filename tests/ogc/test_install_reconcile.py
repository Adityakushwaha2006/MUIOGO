"""Restart reconciliation (issue #501 additions).

After a restart, a job left mid-flight (installing/checking) with no live thread is
marked failed, a working install caught mid-update is preserved, and healthy idle
records are left alone. Drives reconcile over seeded on-disk state.
"""
from Classes.OGCore.CalibrationRegistry import CalibrationRegistry
from Classes.OGCore.InstallJob import InstallJob


def _seed_job(install_id, country_id, state):
    InstallJob._persist({
        "install_id": install_id, "country_id": country_id,
        "country_name": country_id, "source_type": "catalog",
        "install_state": state, "install_stage": "uv_sync",
        "progress_label": "Installing", "log_tail": [], "error": None,
    })


def _seed_crashed_state():
    """On-disk state as if the server died mid-install (no in-memory jobs)."""
    CalibrationRegistry.upsert({"country_id": "ETH", "country_name": "Ethiopia",
                                "source_type": "catalog", "install_state": "installing",
                                "install_id": "install_2026_07_20_001"})
    CalibrationRegistry.upsert({"country_id": "ZAF", "country_name": "South Africa",
                                "source_type": "catalog", "install_state": "installing",
                                "install_id": "install_2026_07_20_002",
                                "venv_path": "/models/OG-ZAF/.venv",
                                "installed_at": "2026-01-01T00:00:00Z"})
    CalibrationRegistry.upsert({"country_id": "KEN", "country_name": "Kenya",
                                "source_type": "catalog", "install_state": "installed",
                                "venv_path": "/models/OG-KEN/.venv",
                                "installed_at": "2026-01-01T00:00:00Z"})
    # Healthy idle record a refresh flagged for update; must not be treated as interrupted.
    CalibrationRegistry.upsert({"country_id": "GHA", "country_name": "Ghana",
                                "source_type": "catalog", "install_state": "update_available",
                                "venv_path": "/models/OG-GHA/.venv",
                                "installed_at": "2026-01-01T00:00:00Z"})
    _seed_job("install_2026_07_20_001", "ETH", "installing")
    _seed_job("install_2026_07_20_002", "ZAF", "installing")
    _seed_job("install_2026_07_20_003", "KEN", "installed")  # terminal, leave alone


def test_reconcile_marks_interrupted_and_preserves_working():
    _seed_crashed_state()
    InstallJob.reconcile_interrupted_jobs()

    eth = CalibrationRegistry.get("ETH")
    zaf = CalibrationRegistry.get("ZAF")
    ken = CalibrationRegistry.get("KEN")
    gha = CalibrationRegistry.get("GHA")
    assert eth["install_state"] == "failed"
    assert eth.get("last_error") == "Interrupted by a server restart."
    assert zaf["install_state"] == "installed", "interrupted update preserved"
    assert zaf.get("last_error") == "Interrupted by a server restart."
    assert zaf.get("venv_path") == "/models/OG-ZAF/.venv"
    assert ken["install_state"] == "installed" and "last_error" not in ken
    assert gha["install_state"] == "update_available" and "last_error" not in gha, \
        "healthy update_available left untouched"

    j1 = InstallJob.get_status("install_2026_07_20_001")
    j2 = InstallJob.get_status("install_2026_07_20_002")
    j3 = InstallJob.get_status("install_2026_07_20_003")
    assert j1["install_state"] == "failed" and j1["error"] == "Interrupted by a server restart."
    assert j2["install_state"] == "failed"
    assert j3["install_state"] == "installed" and j3.get("error") is None


def test_reconcile_is_idempotent():
    _seed_crashed_state()
    InstallJob.reconcile_interrupted_jobs()
    InstallJob.reconcile_interrupted_jobs()
    assert CalibrationRegistry.get("ZAF")["install_state"] == "installed"
    assert CalibrationRegistry.get("ETH")["install_state"] == "failed"
