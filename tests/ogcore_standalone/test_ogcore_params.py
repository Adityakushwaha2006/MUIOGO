"""
OG-Core parameter storage: JSON round-trip fidelity and per-run isolation.

These guard the property the whole per-run refactor depends on: a baseline and a
reform in the same case keep SEPARATE parameter sets, and what we save is exactly
what we read back (so the values handed to OG-Core are not silently altered by the
JSON round-trip). Fast, no model solves.
"""

import pytest

from Classes.OGCore.OGCoreClass import OGCoreCase


def _case_with_run(name="Kenya", run="Base"):
    case = OGCoreCase(name)
    case.create_case({"ogc-casename": name})
    case.create_run(run, "baseline", None, params={})
    return case


# ── Round-trip fidelity ─────────────────────────────────────────────────────

def test_save_then_get_is_identical(isolated_storage):
    case = _case_with_run()
    spec = {
        "frisch": 0.41,
        "start_year": 2021,          # int stays int
        "cit_rate": [[0.21]],        # nested list survives
        "alpha_T": [0.09, 0.10, 0.08],
        "debt_ratio_ss": 1.0,        # float stays float
        "tax_func_type": "DEP",      # str
    }
    case.save_params("Base", spec)
    assert case.get_params("Base") == spec


def test_int_and_float_types_are_preserved(isolated_storage):
    case = _case_with_run()
    case.save_params("Base", {"S": 80, "frisch": 0.4})
    got = case.get_params("Base")
    assert isinstance(got["S"], int) and got["S"] == 80
    assert isinstance(got["frisch"], float) and got["frisch"] == 0.4


def test_get_params_empty_when_seeded_empty(isolated_storage):
    case = _case_with_run()
    assert case.get_params("Base") == {}


def test_create_run_seeds_initial_params(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    case.create_run("Base", "baseline", None, params={"frisch": 0.45})
    assert case.get_params("Base") == {"frisch": 0.45}


# ── Per-run isolation ───────────────────────────────────────────────────────

def test_baseline_and_reform_params_are_independent(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    case.create_run("Base", "baseline", None, params={"cit_rate": [[0.21]]})
    case.update_run_status("Base", "completed", time_path=True)
    case.create_run("Ref", "reform", "Base", params={"cit_rate": [[0.35]]})

    assert case.get_params("Base")["cit_rate"] == [[0.21]]
    assert case.get_params("Ref")["cit_rate"] == [[0.35]]
    assert case.get_params("Base") != case.get_params("Ref")


def test_editing_one_run_does_not_touch_the_other(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    case.create_run("Base", "baseline", None, params={"frisch": 0.41})
    case.update_run_status("Base", "completed", time_path=True)
    case.create_run("Ref", "reform", "Base", params={"frisch": 0.41})

    case.save_params("Ref", {"frisch": 0.55})
    assert case.get_params("Base") == {"frisch": 0.41}
    assert case.get_params("Ref") == {"frisch": 0.55}


def test_save_params_to_missing_run_is_error(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    resp = case.save_params("NoSuchRun", {"frisch": 0.4})
    assert resp["status_code"] == "error"


# ── Through the HTTP routes ─────────────────────────────────────────────────

def test_http_params_roundtrip(ogc_client):
    ogc_client.post("/ogc/saveCase", json={"data": {"ogc-casename": "Kenya"}})
    ogc_client.post("/ogc/createRun",
                    json={"casename": "Kenya", "run_name": "Base", "run_type": "baseline"})

    spec = {"frisch": 0.41, "cit_rate": [[0.21]], "start_year": 2021}
    r = ogc_client.post("/ogc/saveParams",
                        json={"casename": "Kenya", "run_name": "Base", "params": spec})
    assert r.status_code == 200 and r.get_json()["status_code"] == "success"

    got = ogc_client.post("/ogc/getParams",
                          json={"casename": "Kenya", "run_name": "Base"}).get_json()
    assert got == spec


def test_http_save_params_missing_run_is_404(ogc_client):
    ogc_client.post("/ogc/saveCase", json={"data": {"ogc-casename": "Kenya"}})
    r = ogc_client.post("/ogc/saveParams",
                        json={"casename": "Kenya", "run_name": "Ghost", "params": {}})
    assert r.status_code == 404
