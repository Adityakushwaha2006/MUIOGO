"""OG-Core case and run endpoints (disk CRUD only).

All routes live under /ogc. They wrap OGCoreCase, which owns the on-disk case/run
layout; nothing here executes OG-Core or imports the ogcore package. Model runs
happen in a separate OG environment driven by the worker layer. See:
    Track1-API-Schema-Discussion/OGCore-API-Schema-FINAL.md
"""
import csv
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Blueprint,
    after_this_request,
    jsonify,
    request,
    send_file,
    session,
)

from Classes.Base import Config
from Classes.Base.FileClass import File
from Classes.OGCore import OGTables
from Classes.OGCore.CalibrationRegistry import CalibrationRegistry
from Classes.OGCore.OGCoreCase import OGCoreCase, is_safe_name
from Classes.OGCore.OGResults import OGResults
from Classes.OGCore.RunJob import RunJob

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


def _utc_now_z():
    """ISO-8601 UTC timestamp with a trailing Z, seconds precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    if RunJob.case_busy(casename):
        return _err("A run in this case is running or queued; stop it first.")
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
    if RunJob.case_busy(casename) and (
        RunJob.is_busy(casename, run_name) or case.get_baseline_name() == run_name
    ):
        return _err("That run is running or queued; stop it first.")

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


def _run_log_tail(case, run_name, n=50):
    """Last ``n`` lines of a run's persisted run_log.txt, or [] if none/unreadable."""
    path = case.res_path / run_name / "run_log.txt"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    return lines[-n:]


# ── 11. start (or queue) a model run ─────────────────────────────────────────
@ogcore_run_api.route("/run", methods=["POST"])
def run():
    blocked = _blocked_cross_site()
    if blocked:
        return blocked
    data = request.get_json(silent=True)
    if data is None:
        return _err("Request body must be valid JSON.")
    miss = _missing(data, "casename", "run_name", "time_path")
    if miss:
        return _err(f"Missing required field: {miss}")
    casename = data["casename"]
    run_name = data["run_name"]
    time_path = data["time_path"]
    if not isinstance(time_path, bool):
        return _err("time_path must be a boolean.")
    bad = _unsafe_name(casename, run_name)
    if bad:
        return bad

    case = OGCoreCase(casename)
    if not case.case_path.is_dir():
        return _err("Case not found.", http=404)
    in_index = any(
        r.get("RunName") == run_name for r in case.gen_data.get("ogc-runs", [])
    )
    if not in_index:
        return _err("Run not found.", http=404)

    result = RunJob.start(casename, run_name, time_path)
    if result.get("status_code") == "error":
        return jsonify(result), 400
    return jsonify(result), 200


# ── 12. read a run's live execution status ───────────────────────────────────
@ogcore_run_api.route("/getRunStatus", methods=["POST"])
def getRunStatus():
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
    meta = case.get_run_meta(run_name)
    if not meta:
        return _err("Run not found.", http=404)

    live = RunJob.get_live(casename, run_name)
    run_state = meta.get("status")
    # A run marked running with no live supervisor was orphaned by a restart; repair
    # its meta to a terminal failed state so it does not appear stuck forever.
    if run_state == "running" and live is None:
        case.update_run_status(
            run_name, "failed", error="Run was interrupted by an application restart."
        )
        meta = case.get_run_meta(run_name)
        run_state = meta.get("status")

    if live:
        run_stage = live.get("stage_label") or (
            "Queued" if live.get("queued") else None
        )
        run_iteration = live.get("iteration") or None
        run_log = live.get("log_tail")
    else:
        run_stage = None
        run_iteration = None
        run_log = _run_log_tail(case, run_name)

    return jsonify({
        "status_code": "success",
        "run_state": run_state,
        "run_stage": run_stage,
        "run_iteration": run_iteration,
        "run_log": run_log,
    }), 200


# ── 13. cancel a running or queued run ───────────────────────────────────────
@ogcore_run_api.route("/cancelRun", methods=["POST"])
def cancelRun():
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

    result = RunJob.cancel(casename, run_name)
    if result.get("status_code") == "cancelled":
        return jsonify({"status_code": "cancelled"}), 200
    return jsonify(result), 400


# ── results: shared validation ───────────────────────────────────────────────
def _bad_vars(vars_arg):
    """Error response if an optional ``vars`` field is present but not a list of
    strings, else None. Absent (None) is always fine."""
    if vars_arg is None:
        return None
    if not isinstance(vars_arg, list) or not all(isinstance(v, str) for v in vars_arg):
        return _err("vars must be a list of strings.")
    return None


# ── 14. read a run's steady-state variables ──────────────────────────────────
@ogcore_run_api.route("/getSSVars", methods=["POST"])
def getSSVars():
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
    vars_arg = data.get("vars")
    bad_vars = _bad_vars(vars_arg)
    if bad_vars:
        return bad_vars

    case = OGCoreCase(casename)
    meta = case.get_run_meta(run_name)
    if not meta:
        return _err("Run not found.", http=404)
    if meta.get("status") != "completed":
        return _err("No results - run it first", http=404)
    ss = OGResults.load_ss(case.res_path / run_name)
    if ss is None:
        return _err("No results - run it first", http=404)
    return jsonify(OGResults.subset(ss, vars_arg)), 200


# ── 15. read a run's transition-path variables ───────────────────────────────
@ogcore_run_api.route("/getTPIVars", methods=["POST"])
def getTPIVars():
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
    vars_arg = data.get("vars")
    bad_vars = _bad_vars(vars_arg)
    if bad_vars:
        return bad_vars

    case = OGCoreCase(casename)
    meta = case.get_run_meta(run_name)
    if not meta:
        return _err("Run not found.", http=404)
    if meta.get("status") != "completed":
        return _err("No results - run it first", http=404)
    tpi = OGResults.load_tpi(case.res_path / run_name)
    if tpi is None:
        return _err("No transition path results for this run.", http=404)
    return jsonify(OGResults.subset(tpi, vars_arg)), 200


def _results_gate(case, casename, run_name):
    """None if the run has usable results, else the response to return now.

    A run in progress or queued returns the running envelope; a failed run or one
    with no results returns the error envelope; a missing meta is a 404. Both
    envelopes carry the casename so the dashboard can key its state to the case.
    """
    meta = case.get_run_meta(run_name)
    if not meta:
        return _err("Run not found.", http=404)
    status = meta.get("status")
    # "In progress" means the run is actually active or queued right now. A run
    # that is merely pending (created but never started) has no results to wait
    # for, so it gets the run-it-first envelope, not a spinner.
    if RunJob.is_busy(casename, run_name):
        return jsonify({
            "status_code": "running",
            "casename": casename,
            "message": "Solve in progress",
        }), 200
    if status != "completed":
        return jsonify({
            "status_code": "error",
            "casename": casename,
            "message": "No results - run it first",
        }), 404
    return None


# ── 16. read the consolidated results object ──────────────────────────────────
@ogcore_run_api.route("/getResults", methods=["POST"])
def getResults():
    data = request.get_json(silent=True)
    if data is None:
        return _err("Request body must be valid JSON.")
    miss = _missing(data, "casename", "base_run")
    if miss:
        return _err(f"Missing required field: {miss}")
    casename = data["casename"]
    base_run = data["base_run"]
    reform_run = data.get("reform_run")
    names = [casename, base_run]
    if reform_run is not None:
        names.append(reform_run)
    bad = _unsafe_name(*names)
    if bad:
        return bad

    case = OGCoreCase(casename)
    if not case.case_path.is_dir():
        return _err("Case not found.", http=404)

    gate = _results_gate(case, casename, base_run)
    if gate:
        return gate
    if reform_run is not None:
        gate = _results_gate(case, casename, reform_run)
        if gate:
            return gate

    base_dir = case.res_path / base_run
    reform_dir = case.res_path / reform_run if reform_run is not None else None
    payload, err = OGResults.consolidated(
        casename, base_run, reform_run, base_dir, reform_dir
    )
    if err is not None:
        message, http = err
        return jsonify({
            "status_code": "error",
            "casename": casename,
            "message": message,
        }), http
    return jsonify(payload), 200


# ── analysis tables: shared endpoint ─────────────────────────────────────────
def _table_endpoint(table_key):
    """Serve one OG-Core analysis table built by the worker.

    Validates the request against the table's TABLES spec, gates on the run(s)
    having usable results, then spawns the worker's tables mode and returns the
    list of row objects it produced. A worker failure surfaces as a 502.
    """
    worker_key, reform_required, allowed = OGTables.TABLES[table_key]

    data = request.get_json(silent=True)
    if data is None:
        return _err("Request body must be valid JSON.")
    miss = _missing(data, "casename", "base_run")
    if miss:
        return _err(f"Missing required field: {miss}")
    casename = data["casename"]
    base_run = data["base_run"]
    reform_run = data.get("reform_run")
    if reform_required and not reform_run:
        return _err("This table requires a reform run.")

    names = [casename, base_run]
    if reform_run is not None:
        names.append(reform_run)
    bad = _unsafe_name(*names)
    if bad:
        return bad

    # Only whitelisted, present, non-null option fields pass through.
    options = {}
    for key in allowed:
        if key in data and data[key] is not None:
            options[key] = data[key]

    # OG-Core's macro table defaults to percent-change output, which asserts on
    # reform data. A baseline-only request is only meaningful as levels, so that
    # becomes the default when no reform is selected (the caller can still say
    # otherwise explicitly and get OG-Core's own refusal).
    if table_key == "macro" and reform_run is None and "output_type" not in options:
        options["output_type"] = "levels"

    case = OGCoreCase(casename)
    if not case.case_path.is_dir():
        return _err("Case not found.", http=404)

    gate = _results_gate(case, casename, base_run)
    if gate:
        return gate
    if reform_run is not None:
        gate = _results_gate(case, casename, reform_run)
        if gate:
            return gate

    python_path, err = OGTables.resolve_python(case)
    if err:
        return _err(err)

    base_dir = case.res_path / base_run
    reform_dir = case.res_path / reform_run if reform_run is not None else None
    argv = OGTables.table_args(table_key, base_dir, reform_dir, options)
    payload, werr = OGTables.run_worker_mode(python_path, argv)
    if werr is not None:
        return jsonify({"message": werr, "status_code": "error"}), 502

    rows = payload.get("rows") if isinstance(payload, dict) else None
    if rows is None:
        return jsonify(
            {"message": "The table result was malformed.", "status_code": "error"}
        ), 502
    return jsonify(rows), 200


@ogcore_run_api.route("/getMacroTable", methods=["POST"])
def getMacroTable():
    return _table_endpoint("macro")


@ogcore_run_api.route("/getMacroTableSS", methods=["POST"])
def getMacroTableSS():
    return _table_endpoint("macro_ss")


@ogcore_run_api.route("/getIneqTable", methods=["POST"])
def getIneqTable():
    return _table_endpoint("ineq")


@ogcore_run_api.route("/getGiniTable", methods=["POST"])
def getGiniTable():
    return _table_endpoint("gini")


@ogcore_run_api.route("/getWealthMomentsTable", methods=["POST"])
def getWealthMomentsTable():
    return _table_endpoint("wealth_moments")


@ogcore_run_api.route("/getTimeSeriesTable", methods=["POST"])
def getTimeSeriesTable():
    return _table_endpoint("time_series")


@ogcore_run_api.route("/getRevenueDecomposition", methods=["POST"])
def getRevenueDecomposition():
    return _table_endpoint("revenue_decomp")


# ── validate a run's parameters against OG-Core's own rules ───────────────────
@ogcore_run_api.route("/validateParams", methods=["POST"])
def validateParams():
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
    run_dir = case.res_path / run_name
    if not run_dir.is_dir():
        return _err("Run not found.", http=404)

    python_path, err = OGTables.resolve_python(case)
    if err:
        return _err(err)

    payload, werr = OGTables.run_worker_mode(
        python_path, ["validate", "--run-dir", str(run_dir)]
    )
    if werr is not None:
        return jsonify({"message": werr, "status_code": "error"}), 502
    return jsonify(payload), 200


# ── upload a run's tax-function parameter pickle ─────────────────────────────
@ogcore_run_api.route("/uploadTaxParams", methods=["POST"])
def uploadTaxParams():
    blocked = _blocked_cross_site()
    if blocked:
        return blocked

    casename = request.form.get("casename")
    run_name = request.form.get("run_name")
    if not casename or not run_name:
        return _err("Missing required field: casename and run_name are required.")
    bad = _unsafe_name(casename, run_name)
    if bad:
        return bad

    case = OGCoreCase(casename)
    run_dir = case.res_path / run_name
    if not run_dir.is_dir():
        return _err("Run not found.", http=404)

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return _err("No file uploaded.")
    if not upload.filename.lower().endswith(".pkl"):
        return _err("The tax parameter file must be a .pkl file.")

    max_bytes = 16 * 1024 * 1024
    if request.content_length is not None and request.content_length > max_bytes:
        return _err("The uploaded file is too large (max 16MB).", http=413)

    python_path, err = OGTables.resolve_python(case)
    if err:
        return _err(err)

    fd, tmp_path = tempfile.mkstemp(suffix=".pkl")
    os.close(fd)
    try:
        upload.save(tmp_path)
        payload, werr = OGTables.run_worker_mode(
            python_path, ["taxcheck", "--file", tmp_path]
        )
        if werr is not None:
            return jsonify({"message": werr, "status_code": "error"}), 502
        if not payload.get("valid"):
            return jsonify({
                "message": payload.get("message") or "Invalid tax parameter file.",
                "status_code": "error",
            }), 400

        tax_func_type = payload.get("tax_func_type")
        try:
            os.replace(tmp_path, run_dir / "ogcTaxParams.pkl")
        except OSError:
            # The temp dir can sit on a different drive than DataStorage, where
            # os.replace cannot atomically move; fall back to a copying move.
            import shutil

            shutil.move(tmp_path, run_dir / "ogcTaxParams.pkl")
        tmp_path = None  # moved into place; do not delete in finally
        File.writeFile(
            {"tax_func_type": tax_func_type, "uploaded_at": _utc_now_z()},
            run_dir / "ogcTaxParams.info.json",
        )
        return jsonify({
            "message": "Tax params loaded.",
            "tax_func_type": tax_func_type,
            "status_code": "success",
        }), 200
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ── read the stored tax-params info sidecar ──────────────────────────────────
@ogcore_run_api.route("/getTaxParamsInfo", methods=["GET"])
def getTaxParamsInfo():
    casename = request.args.get("casename")
    run_name = request.args.get("run_name")
    if not casename or not run_name:
        return _err("Missing required field: casename and run_name are required.")
    bad = _unsafe_name(casename, run_name)
    if bad:
        return bad

    case = OGCoreCase(casename)
    info_path = case.res_path / run_name / "ogcTaxParams.info.json"
    if not info_path.exists():
        return jsonify({"loaded": False}), 200
    try:
        info = File.readFile(info_path)
    except (OSError, ValueError, IndexError):
        return jsonify({"loaded": False}), 200
    return jsonify({
        "loaded": True,
        "tax_func_type": info.get("tax_func_type"),
        "uploaded_at": info.get("uploaded_at"),
    }), 200


# ── download the macro comparison table as a CSV file ────────────────────────
@ogcore_run_api.route("/downloadResults", methods=["GET"])
def downloadResults():
    casename = request.args.get("casename")
    base_run = request.args.get("base_run")
    reform_run = request.args.get("reform_run")
    if not casename or not base_run:
        return _err("Missing required field: casename and base_run are required.")

    names = [casename, base_run]
    if reform_run is not None:
        names.append(reform_run)
    bad = _unsafe_name(*names)
    if bad:
        return bad

    case = OGCoreCase(casename)
    if not case.case_path.is_dir():
        return _err("Case not found.", http=404)

    gate = _results_gate(case, casename, base_run)
    if gate:
        return gate
    if reform_run is not None:
        gate = _results_gate(case, casename, reform_run)
        if gate:
            return gate

    python_path, err = OGTables.resolve_python(case)
    if err:
        return _err(err)

    base_dir = case.res_path / base_run
    reform_dir = case.res_path / reform_run if reform_run is not None else None
    # Baseline-only downloads must be levels; percent change needs a reform.
    options = {} if reform_run is not None else {"output_type": "levels"}
    argv = OGTables.table_args("macro", base_dir, reform_dir, options)
    payload, werr = OGTables.run_worker_mode(python_path, argv)
    if werr is not None:
        return jsonify({"message": werr, "status_code": "error"}), 502

    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not rows:
        return jsonify(
            {"message": "No results to download.", "status_code": "error"}
        ), 502

    # Header is the union of row keys in first-appearance order (first row first).
    header = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                header.append(key)

    fd, csv_path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
    except OSError:
        try:
            os.unlink(csv_path)
        except OSError:
            pass
        return jsonify(
            {"message": "Failed to build the download.", "status_code": "error"}
        ), 500

    @after_this_request
    def _cleanup(response):
        try:
            os.unlink(csv_path)
        except OSError:
            pass
        return response

    return send_file(
        csv_path,
        as_attachment=True,
        download_name=f"{casename}_{base_run}_results.csv",
        mimetype="text/csv",
    )
