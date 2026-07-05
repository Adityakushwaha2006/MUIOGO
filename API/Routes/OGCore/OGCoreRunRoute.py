"""OG-Core case and run endpoints (disk CRUD only).

All routes live under /ogc. They wrap OGCoreCase, which owns the on-disk case/run
layout; nothing here executes OG-Core or imports the ogcore package. Model runs
happen in a separate OG environment driven by the worker layer. See:
    Track1-API-Schema-Discussion/OGCore-API-Schema-FINAL.md
"""
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request, session

from Classes.Base import Config
from Classes.OGCore.CalibrationRegistry import CalibrationRegistry
from Classes.OGCore.OGCoreCase import OGCoreCase, is_safe_name

ogcore_run_api = Blueprint("OGCoreRunRoute", __name__, url_prefix="/ogc")

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


# ── small helpers (local copies of the installer route's, do not modify those) ─
def _err(message, http=400, status="error"):
    return jsonify({"message": message, "status_code": status}), http


def _blocked_cross_site():
    """Refuse a state-changing request that a cross-site page drove via the browser.

    A browser always attaches an Origin header on a cross-origin POST, so if one is
    present it must be the local app. Non-browser callers (the desktop shell, curl,
    tests) send no Origin and are allowed, matching the app's local-only model.
    Returns an error response to short-circuit, or None to proceed.
    """
    origin = request.headers.get("Origin")
    if origin:
        host = urlparse(origin).hostname
        if host not in _LOCAL_HOSTS:
            return _err("Cross-site request refused.", http=403)
    return None


def _missing(data, *fields):
    """First field absent from the body, or None."""
    for field in fields:
        if field not in data:
            return field
    return None


def _unsafe_name(*names):
    """Error response if any name is unsafe as a path component, else None.

    Every casename/run_name becomes a directory under OGC_CASES_DIR, so each one
    is checked before any Path is built from it. Blocks traversal like '..' and
    separator characters at the door.
    """
    for name in names:
        if not is_safe_name(name):
            return _err("Invalid name.")
    return None


# ── 1. read active-case session ──────────────────────────────────────────────
@ogcore_run_api.route("/getSession", methods=["GET"])
def getSession():
    return jsonify({"ogccase": session.get("ogccase") or None}), 200


# ── 2. set active-case session ───────────────────────────────────────────────
@ogcore_run_api.route("/setSession", methods=["POST"])
def setSession():
    blocked = _blocked_cross_site()
    if blocked:
        return blocked
    data = request.get_json(silent=True)
    if data is None:
        return _err("Request body must be valid JSON.")
    if "casename" not in data:
        return _err("Missing required field: casename")
    casename = data["casename"]
    if casename is None:
        session.pop("ogccase", None)
        return jsonify({"ogccase": None}), 200
    if not is_safe_name(casename):
        return _err("Invalid case name.")
    if not Path(Config.OGC_CASES_DIR, casename).is_dir():
        return _err("Case not found.", http=404)
    session["ogccase"] = casename
    return jsonify({"ogccase": casename}), 200


# ── 3. list cases ────────────────────────────────────────────────────────────
@ogcore_run_api.route("/getCases", methods=["GET"])
def getCases():
    return jsonify(OGCoreCase.list_cases()), 200


# ── 4. create or edit a case ─────────────────────────────────────────────────
@ogcore_run_api.route("/saveCase", methods=["POST"])
def saveCase():
    blocked = _blocked_cross_site()
    if blocked:
        return blocked
    body = request.get_json(silent=True)
    if body is None:
        return _err("Request body must be valid JSON.")
    data = body.get("data")
    if not isinstance(data, dict):
        return _err("Missing required field: data")
    name = data.get("ogc-casename")
    if not name:
        return _err("Missing required field: ogc-casename")
    if not is_safe_name(name):
        return _err("Invalid case name.")
    data.setdefault("ogc-description", "")

    case = OGCoreCase(name)
    if not case.case_path.is_dir():
        # Create path: country_id is required and its calibration must be installed.
        country_id = data.get("country_id")
        if not country_id:
            return _err("Missing required field: country_id")
        if CalibrationRegistry.get(country_id) is None:
            return _err("That country calibration is not installed.")
        case.create_case(data)
        session["ogccase"] = name
        return jsonify({"message": f"Case {name} created.", "status_code": "created"}), 200

    # Edit path: country_id is immutable. Carry the stored value forward when the
    # edit body omits it, so save_case never drops it from genData.
    stored = case.gen_data
    if "country_id" in data and data["country_id"] != stored.get("country_id"):
        return _err("country_id cannot be changed on an existing case.")
    data["country_id"] = stored.get("country_id")
    case.save_case(data)
    return jsonify({"message": f"Case {name} updated.", "status_code": "edited"}), 200


# ── 5. delete a case ─────────────────────────────────────────────────────────
@ogcore_run_api.route("/deleteCase", methods=["POST"])
def deleteCase():
    blocked = _blocked_cross_site()
    if blocked:
        return blocked
    data = request.get_json(silent=True)
    if data is None:
        return _err("Request body must be valid JSON.")
    miss = _missing(data, "casename")
    if miss:
        return _err(f"Missing required field: {miss}")
    casename = data["casename"]
    bad = _unsafe_name(casename)
    if bad:
        return bad

    active = session.get("ogccase")
    if active is None:
        return _err("No active session.", http=403)
    if active != casename:
        return _err("Unauthorised: case does not match active session.", http=403)

    case = OGCoreCase(casename)
    if not case.case_path.is_dir():
        return _err("Case not found.", http=404)
    result = case.delete_case()
    session["ogccase"] = None
    return jsonify(result), 200


# ── 6. create a run ──────────────────────────────────────────────────────────
@ogcore_run_api.route("/createRun", methods=["POST"])
def createRun():
    blocked = _blocked_cross_site()
    if blocked:
        return blocked
    data = request.get_json(silent=True)
    if data is None:
        return _err("Request body must be valid JSON.")
    miss = _missing(data, "casename", "run_name", "run_type")
    if miss:
        return _err(f"Missing required field: {miss}")
    casename = data["casename"]
    run_name = data["run_name"]
    run_type = data["run_type"]
    bad = _unsafe_name(casename, run_name)
    if bad:
        return bad

    case = OGCoreCase(casename)
    if not case.case_path.is_dir():
        return _err("Case not found.", http=404)
    if run_type == "reform" and not data.get("baseline_run_name"):
        return _err("Missing required field: baseline_run_name")

    params = data.get("params")
    if params is not None and not isinstance(params, dict):
        return _err("params must be an object.")

    result = case.create_run(run_name, run_type, data.get("baseline_run_name"), params)
    sc = result.get("status_code")
    if sc == "error":
        return jsonify(result), 400
    if sc == "exist":
        return jsonify(result), 200
    return jsonify({"message": "Run created.", "status_code": "success"}), 200


# ── 7. list a case's runs ────────────────────────────────────────────────────
@ogcore_run_api.route("/getRuns", methods=["POST"])
def getRuns():
    data = request.get_json(silent=True)
    if data is None:
        return _err("Request body must be valid JSON.")
    miss = _missing(data, "casename")
    if miss:
        return _err(f"Missing required field: {miss}")
    bad = _unsafe_name(data["casename"])
    if bad:
        return bad
    case = OGCoreCase(data["casename"])
    if not case.case_path.is_dir():
        return _err("Case not found.", http=404)
    return jsonify(case.get_runs_shaped()), 200


# ── 8. delete a run ──────────────────────────────────────────────────────────
@ogcore_run_api.route("/deleteRun", methods=["POST"])
def deleteRun():
    blocked = _blocked_cross_site()
    if blocked:
        return blocked
    data = request.get_json(silent=True)
    if data is None:
        return _err("Request body must be valid JSON.")
    miss = _missing(data, "casename", "run_name")
    if miss:
        return _err(f"Missing required field: {miss}")
    casename = data["casename"]
    run_name = data["run_name"]
    bad = _unsafe_name(casename, run_name)
    if bad:
        return bad

    case = OGCoreCase(casename)
    if not case.case_path.is_dir():
        return _err("Case not found.", http=404)
    in_index = any(r.get("RunName") == run_name for r in case.gen_data.get("ogc-runs", []))
    if not in_index:
        return _err("Run not found.", http=404)

    result = case.delete_run(run_name)
    if result.get("status_code") == "success_session":
        session["ogccase"] = None
    return jsonify(result), 200


# ── 9. read a run's parameters ───────────────────────────────────────────────
@ogcore_run_api.route("/getParams", methods=["POST"])
def getParams():
    data = request.get_json(silent=True)
    if data is None:
        return _err("Request body must be valid JSON.")
    miss = _missing(data, "casename", "run_name")
    if miss:
        return _err(f"Missing required field: {miss}")
    bad = _unsafe_name(data["casename"], data["run_name"])
    if bad:
        return bad
    case = OGCoreCase(data["casename"])
    run_dir = case.res_path / data["run_name"]
    if not run_dir.is_dir():
        return _err("Run not found.", http=404)
    return jsonify(case.get_params(data["run_name"])), 200


# ── 10. save a run's parameters ──────────────────────────────────────────────
@ogcore_run_api.route("/saveParams", methods=["POST"])
def saveParams():
    blocked = _blocked_cross_site()
    if blocked:
        return blocked
    data = request.get_json(silent=True)
    if data is None:
        return _err("Request body must be valid JSON.")
    miss = _missing(data, "casename", "run_name", "params")
    if miss:
        return _err(f"Missing required field: {miss}")
    params = data["params"]
    if not isinstance(params, dict):
        return _err("params must be an object.")
    bad = _unsafe_name(data["casename"], data["run_name"])
    if bad:
        return bad
    case = OGCoreCase(data["casename"])
    run_dir = case.res_path / data["run_name"]
    if not run_dir.is_dir():
        return _err("Run not found.", http=404)
    case.save_params(data["run_name"], params)
    return jsonify({"message": "Parameters saved.", "status_code": "success"}), 200
