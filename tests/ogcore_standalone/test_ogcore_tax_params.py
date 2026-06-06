"""
OG-Core tax-function parameter upload (mono / mono2D path).

These params are Python objects that cannot live in JSON, so they are uploaded
as a (cloud)pickle and stored per run. Tests use a fabricated pickle (the
validation only checks structure, not the actual tax functions), so they are
fast and need no model solve. Guards: key validation, non-dict rejection,
corrupt-file rejection, per-run storage, and the info endpoint.
"""

import io

import cloudpickle

from Classes.OGCore.OGCoreClass import OGCoreCase

_VALID = {"tax_func_type": "mono", "etr_params": [], "mtrx_params": [], "mtry_params": []}


def _case_with_run(name="Kenya", run="Base"):
    case = OGCoreCase(name)
    case.create_case({"ogc-casename": name})
    case.create_run(run, "baseline", None, params={})
    return case


# ── save_tax_params validation ──────────────────────────────────────────────

def test_valid_tax_pkl_is_stored(isolated_storage):
    case = _case_with_run()
    resp = case.save_tax_params("Base", cloudpickle.dumps(_VALID))
    assert resp["status_code"] == "success"
    assert resp["tax_func_type"] == "mono"
    assert case.run_tax_params_path("Base").is_file()


def test_missing_keys_rejected(isolated_storage):
    case = _case_with_run()
    resp = case.save_tax_params("Base", cloudpickle.dumps({"tax_func_type": "mono"}))
    assert resp["status_code"] == "error"
    assert "Missing keys" in resp["message"]


def test_non_dict_pkl_rejected(isolated_storage):
    case = _case_with_run()
    resp = case.save_tax_params("Base", cloudpickle.dumps([1, 2, 3]))
    assert resp["status_code"] == "error"


def test_corrupt_bytes_rejected(isolated_storage):
    case = _case_with_run()
    resp = case.save_tax_params("Base", b"this is not a pickle")
    assert resp["status_code"] == "error"


def test_save_tax_params_missing_run_rejected(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    resp = case.save_tax_params("Ghost", cloudpickle.dumps(_VALID))
    assert resp["status_code"] == "error"


# ── get_tax_params_info ─────────────────────────────────────────────────────

def test_info_reports_not_loaded(isolated_storage):
    case = _case_with_run()
    assert case.get_tax_params_info("Base") == {"loaded": False}


def test_info_reports_loaded(isolated_storage):
    case = _case_with_run()
    case.save_tax_params("Base", cloudpickle.dumps(_VALID))
    info = case.get_tax_params_info("Base")
    assert info["loaded"] is True
    assert info["tax_func_type"] == "mono"
    assert "uploaded_at" in info


# ── Per-run isolation ───────────────────────────────────────────────────────

def test_tax_params_are_per_run(isolated_storage):
    case = OGCoreCase("Kenya")
    case.create_case({"ogc-casename": "Kenya"})
    case.create_run("Base", "baseline", None, params={})
    case.update_run_status("Base", "completed", time_path=True)
    case.create_run("Ref", "reform", "Base", params={})

    case.save_tax_params("Base", cloudpickle.dumps(_VALID))
    assert case.get_tax_params_info("Base")["loaded"] is True
    assert case.get_tax_params_info("Ref")["loaded"] is False


# ── HTTP ────────────────────────────────────────────────────────────────────

def test_http_upload_and_info(ogc_client):
    ogc_client.post("/ogc/saveCase", json={"data": {"ogc-casename": "Kenya"}})
    ogc_client.post("/ogc/createRun",
                    json={"casename": "Kenya", "run_name": "Base", "run_type": "baseline"})

    data = {
        "casename": "Kenya",
        "run_name": "Base",
        "file": (io.BytesIO(cloudpickle.dumps(_VALID)), "tax.pkl"),
    }
    r = ogc_client.post("/ogc/uploadTaxParams", data=data, content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["status_code"] == "success"

    info = ogc_client.get("/ogc/getTaxParamsInfo",
                          query_string={"casename": "Kenya", "run_name": "Base"}).get_json()
    assert info["loaded"] is True and info["tax_func_type"] == "mono"


def test_http_upload_missing_file_is_400(ogc_client):
    ogc_client.post("/ogc/saveCase", json={"data": {"ogc-casename": "Kenya"}})
    ogc_client.post("/ogc/createRun",
                    json={"casename": "Kenya", "run_name": "Base", "run_type": "baseline"})
    r = ogc_client.post("/ogc/uploadTaxParams",
                        data={"casename": "Kenya", "run_name": "Base"},
                        content_type="multipart/form-data")
    assert r.status_code == 400
