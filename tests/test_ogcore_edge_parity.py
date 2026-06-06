"""
OG-Core ingestion-layer edge / parity tests.

Focus: inputs that are VALID for a direct OG-Core call but exercise the seams
our wrapper adds (JSON output serialization, filesystem path derivation, error
pass-through). These guard against silent divergence between "direct OG-Core"
and "via our pipeline" — the cases OG-Core itself would accept but our extra
machinery could mishandle.

Most tests are pure-Python and fast (default suite). The one test that imports
OG-Core to confirm faithful error pass-through is marked ``slow``.
"""

import json
import math

import numpy as np
import pytest

from Classes.OGCore.OGCoreClass import OGCoreCase, is_safe_name
from Classes.OGCore.OGCoreRunnerClass import _serialize_numpy


# ── Isolation ──────────────────────────────────────────────────────────────

# isolated_storage and ogc_client fixtures are provided by conftest.py.


# ── SEAM 2: NaN / Inf in output must become valid JSON (null) ───────────────

def test_serialize_numpy_maps_non_finite_to_null():
    payload = {
        "arr": np.array([1.0, np.nan, np.inf, -np.inf]),
        "scalar_nan": np.float64("nan"),
        "scalar_inf": np.float64("inf"),
        "py_nan": float("nan"),
        "nested": {"x": np.array([[np.inf, 2.0]])},
        "ints": np.array([1, 2, 3]),
        "finite": np.array([1.5, 2.5]),
    }
    out = _serialize_numpy(payload)

    assert out["arr"] == [1.0, None, None, None]
    assert out["scalar_nan"] is None
    assert out["scalar_inf"] is None
    assert out["py_nan"] is None
    assert out["nested"]["x"] == [[None, 2.0]]
    assert out["ints"] == [1, 2, 3]
    assert out["finite"] == [1.5, 2.5]


def test_serialized_output_is_strict_json():
    """No NaN/Infinity tokens — a browser's JSON.parse must not choke."""
    out = _serialize_numpy({"v": np.array([np.nan, np.inf, 3.0])})
    blob = json.dumps(out)
    assert "NaN" not in blob and "Infinity" not in blob
    # strict reparse (reject the non-standard constants)
    def _reject(token):
        raise ValueError(token)
    json.loads(blob, parse_constant=_reject)  # must not raise


def test_flask_jsonify_of_serialized_has_no_nan(app):
    """End-to-end: jsonify of sanitized data carries no NaN/Infinity tokens."""
    with app.app_context():
        from flask import jsonify
        body = jsonify(_serialize_numpy({"v": np.array([np.nan, np.inf])})).get_data(as_text=True)
    assert "NaN" not in body and "Infinity" not in body


# ── SEAM 3: filesystem-hostile names ───────────────────────────────────────

@pytest.mark.parametrize("name,safe", [
    ("Base", True),
    ("Kenya_2024", True),
    ("DEMO CASE", True),       # spaces are fine
    ("v1.0", True),
    ("CON", False),            # Windows reserved device
    ("NUL.txt", False),        # reserved even with extension
    ("run:1", False),          # colon
    ("a*b", False),            # wildcard
    ("a/b", False),            # separator
    ("a\\b", False),           # separator
    ("trail. ", False),        # trailing dot/space
    ("..", False),
    (".", False),
    ("", False),
    ("x" * 201, False),        # too long
    (None, False),
])
def test_is_safe_name(name, safe):
    assert is_safe_name(name) is safe


def test_create_run_rejects_hostile_names(isolated_storage):
    case = OGCoreCase("fstest")
    case.create_case({"ogc-casename": "fstest"})
    before = {p.name for p in case.res_path.iterdir()} if case.res_path.exists() else set()
    for hostile in ("CON", "run:1", "a*b", "trail. ", "x" * 300, ".."):
        resp = case.create_run(hostile, "baseline", None, params={})
        assert resp["status_code"] == "error", f"{hostile!r} should be rejected, got {resp}"
    # no new run directory was created by any hostile name
    after = {p.name for p in case.res_path.iterdir()} if case.res_path.exists() else set()
    assert before == after


def test_create_case_rejects_hostile_name(isolated_storage):
    resp = OGCoreCase("CON").create_case({"ogc-casename": "CON"})
    assert resp["status_code"] == "error"


def test_http_hostile_run_name_is_400_not_500(ogc_client):
    r = ogc_client.post("/ogc/createRun",
                    json={"casename": "safe", "run_name": "bad:name", "run_type": "baseline"})
    assert r.status_code == 400


def test_http_hostile_case_name_is_400(ogc_client):
    r = ogc_client.post("/ogc/saveCase", json={"data": {"ogc-casename": "CON"}})
    assert r.status_code == 400


# ── Path traversal blocked on every route (the before_request guard) ───────

@pytest.mark.parametrize("path,payload", [
    ("/ogc/getRuns", {"casename": "../evil"}),
    ("/ogc/getParams", {"casename": "v", "run_name": "../../x"}),
    ("/ogc/deleteRun", {"casename": "v", "run_name": "../../etc"}),
    ("/ogc/saveParams", {"casename": "v", "run_name": "../../x", "params": {}}),
    ("/ogc/getSSVars", {"casename": "v", "run_name": "../../x"}),
    ("/ogc/getMacroTable", {"casename": "v", "base_run": "../../x"}),
    ("/ogc/getMacroTable", {"casename": "v", "base_run": "B", "reform_run": "../../x"}),
])
def test_http_traversal_blocked(ogc_client, path, payload):
    assert ogc_client.post(path, json=payload).status_code == 400


def test_http_traversal_blocked_query_string(ogc_client):
    assert ogc_client.get("/ogc/downloadResults",
                      query_string={"casename": "v", "base_run": "../../x"}).status_code == 400


# ── Missing case must be clean (no 500) ────────────────────────────────────

def test_get_runs_missing_case_returns_empty(ogc_client):
    r = ogc_client.post("/ogc/getRuns", json={"casename": "NoSuchCase"})
    assert r.status_code == 200 and r.get_json() == []


def test_create_run_missing_case_not_500(ogc_client):
    r = ogc_client.post("/ogc/createRun",
                    json={"casename": "NoSuchCase", "run_name": "B", "run_type": "baseline"})
    assert r.status_code == 400


# ── SEAM 1: faithful pass-through of OG-Core's own validation error ─────────

@pytest.mark.slow
def test_error_passthrough_matches_direct_ogcore(isolated_storage):
    """
    A bad-format param must surface OG-Core's OWN validation message verbatim,
    so the caller sees exactly what a direct OG-Core call would report. (Marked
    slow only because it imports OG-Core; it does not solve the model.)
    """
    import tempfile
    from ogcore.parameters import Specifications
    from Classes.OGCore.OGCoreRunnerClass import OGCoreRunner

    bad = {"frisch": 5.0}  # > max 0.62

    case = OGCoreCase("ptest")
    case.create_case({"ogc-casename": "ptest"})
    case.create_run("Base", "baseline", None, params=bad)
    resp = OGCoreRunner("ptest").run("Base", time_path=True)

    direct_err = None
    d = tempfile.mkdtemp()
    try:
        p = Specifications(baseline=True, num_workers=1, baseline_dir=d, output_base=d)
        p.update_specifications(dict(bad), raise_errors=True)
    except Exception as exc:  # noqa: BLE001
        direct_err = str(exc)

    assert resp["status_code"] == "failed"
    assert direct_err is not None
    assert "frisch 5.0 > max 0.62" in resp["message"]
    assert "frisch 5.0 > max 0.62" in direct_err
