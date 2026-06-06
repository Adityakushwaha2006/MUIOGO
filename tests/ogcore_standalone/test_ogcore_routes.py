"""
OG-Core HTTP contract + wiring tests.

Fast, no model solves. Guards the route layer: blueprint registration, the
non-destructiveness of the CLEWS routes, the lazy OG-Core import, and the
method / required-field contract of every /ogc route. Traversal, hostile names
and missing-case behaviour live in test_ogcore_edge_parity.py (not duplicated).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

# This file lives in tests/ogcore_standalone/, so the repo root is three up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
API_DIR = PROJECT_ROOT / "API"

EXPECTED_OGC_ROUTE_COUNT = 27
CLEWS_BLUEPRINTS = ["CaseRoute", "DataFileRoute", "UploadRoute", "ViewDataRoute", "SyncS3Route"]


# ── Wiring / registration ───────────────────────────────────────────────────

def test_ogcore_blueprint_registered(app):
    assert "OGCoreRoute" in app.blueprints


def test_clews_blueprints_still_registered(app):
    """Non-destructive: adding OG-Core must not unwire any CLEWS blueprint."""
    for bp in CLEWS_BLUEPRINTS:
        assert bp in app.blueprints, f"CLEWS blueprint {bp} went missing"


def test_expected_number_of_ogc_routes(app):
    ogc = [r.rule for r in app.url_map.iter_rules() if r.rule.startswith("/ogc/")]
    assert len(ogc) == EXPECTED_OGC_ROUTE_COUNT, sorted(ogc)


def test_clews_run_route_still_present(app):
    """A representative CLEWS route must still exist (non-destructive)."""
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/run" in rules and "/generateDataFile" in rules


def test_ogcore_not_imported_at_startup():
    """
    Importing the app must NOT import the heavy ogcore package (it is imported
    lazily inside the runner). Checked in a fresh subprocess so the result does
    not depend on whether an earlier test already imported ogcore.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(API_DIR)
    env.setdefault("MUIOGO_SECRET_KEY", "smoke-test-secret")
    code = "import app; import sys; print('ogcore' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", result.stdout


# ── Config ──────────────────────────────────────────────────────────────────

def test_ogc_data_storage_is_path_and_exists():
    from Classes.Base import Config
    assert isinstance(Config.OGC_DATA_STORAGE, Path)
    assert Config.OGC_DATA_STORAGE.exists() and Config.OGC_DATA_STORAGE.is_dir()


def test_ogc_data_storage_under_webapp_datastorage():
    from Classes.Base import Config
    assert Config.DATA_STORAGE in Config.OGC_DATA_STORAGE.parents


# ── Required-field contract: missing field -> 400 (never 500) ───────────────

# (path, body that is missing one required field)
MISSING_FIELD_POST = [
    ("/ogc/setSession", {}),
    ("/ogc/saveCase", {}),
    ("/ogc/deleteCase", {}),
    ("/ogc/getParams", {"casename": "v"}),
    ("/ogc/saveParams", {"casename": "v", "run_name": "r"}),
    ("/ogc/createRun", {"casename": "v", "run_name": "r"}),
    ("/ogc/deleteRun", {"casename": "v"}),
    ("/ogc/getRuns", {}),
    ("/ogc/run", {"casename": "v"}),
    ("/ogc/validateParams", {"casename": "v"}),
    ("/ogc/getSSVars", {"casename": "v"}),
    ("/ogc/getTPIVars", {"casename": "v"}),
    ("/ogc/getMacroTable", {"casename": "v"}),
    ("/ogc/getMacroTableSS", {"casename": "v", "base_run": "b"}),
    ("/ogc/getIneqTable", {"casename": "v"}),
    ("/ogc/getGiniTable", {"casename": "v"}),
    ("/ogc/getWealthMomentsTable", {"casename": "v"}),
    ("/ogc/getTimeSeriesTable", {"casename": "v"}),
    ("/ogc/getRevenueDecomposition", {"casename": "v", "base_run": "b"}),
]


@pytest.mark.parametrize("path,body", MISSING_FIELD_POST)
def test_missing_required_field_returns_400(ogc_client, path, body):
    assert ogc_client.post(path, json=body).status_code == 400


def test_upload_tax_params_missing_names_returns_400(ogc_client):
    assert ogc_client.post("/ogc/uploadTaxParams", data={}).status_code == 400


def test_get_tax_params_info_missing_names_returns_400(ogc_client):
    assert ogc_client.get("/ogc/getTaxParamsInfo").status_code == 400


def test_download_results_missing_params_returns_400(ogc_client):
    assert ogc_client.get("/ogc/downloadResults").status_code == 400


# ── Field-value validation ──────────────────────────────────────────────────

def test_create_run_bad_run_type_returns_400(ogc_client):
    r = ogc_client.post("/ogc/createRun",
                        json={"casename": "v", "run_name": "r", "run_type": "bogus"})
    assert r.status_code == 400


def test_run_non_bool_time_path_returns_400(ogc_client):
    r = ogc_client.post("/ogc/run",
                        json={"casename": "v", "run_name": "r", "time_path": "yes"})
    assert r.status_code == 400


# ── Session / read contract ─────────────────────────────────────────────────

def test_get_session_is_null_initially(ogc_client):
    r = ogc_client.get("/ogc/getSession")
    assert r.status_code == 200 and r.get_json() == {"session": None}


def test_set_session_null_clears(ogc_client):
    r = ogc_client.post("/ogc/setSession", json={"casename": None})
    assert r.status_code == 200 and r.get_json() == {"ogccase": None}


def test_set_session_nonexistent_case_returns_404(ogc_client):
    r = ogc_client.post("/ogc/setSession", json={"casename": "NoSuchCase"})
    assert r.status_code == 404


def test_get_cases_returns_empty_list_when_none(ogc_client):
    r = ogc_client.get("/ogc/getCases")
    assert r.status_code == 200 and r.get_json() == []


def test_delete_case_without_session_is_403(ogc_client):
    r = ogc_client.post("/ogc/deleteCase", json={"casename": "v"})
    assert r.status_code == 403


# ── Wrong method -> 405 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/ogc/run", "/ogc/getCases", "/ogc/createRun", "/ogc/saveParams"])
def test_wrong_method_returns_405(ogc_client, path):
    assert ogc_client.delete(path).status_code == 405
