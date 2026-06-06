"""
OG-Core API blueprint.

HTTP contract for the OG-Core standalone module. Sits parallel to the CLEWS
blueprints and follows the same conventions (validate_json_fields,
Config.validate_path, status_code in JSON bodies), but uses a separate /ogc URL
namespace and a separate session key (ogccase, versus the CLEWS osycase).

All filesystem-touching routes pass user input through Config.validate_path
before constructing paths, to block traversal.
"""

import io
import os
import time
from pathlib import Path, PurePosixPath
from threading import Thread
from zipfile import ZipFile, BadZipFile

from flask import Blueprint, jsonify, request, session, send_file

from Classes.Base import Config
from Classes.Base.FileClass import File
from Classes.OGCore.OGCoreClass import OGCoreCase, is_safe_name
from Classes.OGCore.OGCoreRunnerClass import OGCoreRunner, load_parameter_schema
from utils import validate_json_fields

ogcore_api = Blueprint("OGCoreRoute", __name__)

# Request fields that become run-directory path components and must be validated.
_RUN_NAME_FIELDS = ("run_name", "base_run", "reform_run", "baseline_run_name")


@ogcore_api.before_request
def _block_path_traversal():
    """
    Single choke point: validate every user-supplied name that flows into a
    filesystem path (casename plus any run-name field), whether it arrives in the
    JSON body, form, or query string. This follows the CLEWS Config.validate_path
    sanitizer convention so no OG-Core route can be reached with a traversal
    payload. It closes the gap where per-run routes (notably deleteRun's rmtree)
    took run_name straight into a path. Returns 400 and short-circuits on any
    escape.
    """
    src: dict = {}
    if request.is_json:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            src.update(body)
    if request.form:
        src.update(request.form.to_dict())
    if request.args:
        src.update(request.args.to_dict())

    casename = src.get("casename")
    try:
        if casename:
            Config.validate_path(Config.OGC_DATA_STORAGE, casename)
            if not is_safe_name(casename):
                return jsonify({"message": "Invalid case name.", "status_code": "error"}), 400
        res_base = Path(Config.OGC_DATA_STORAGE, casename or "", "res")
        for field in _RUN_NAME_FIELDS:
            value = src.get(field)
            if value:
                Config.validate_path(res_base, value)
                if not is_safe_name(value):
                    return jsonify({"message": f"Invalid {field}.", "status_code": "error"}), 400
    except PermissionError:
        return jsonify({"message": "Invalid path.", "status_code": "error"}), 400
    return None  # validation passed, continue to the view


# ── Session ────────────────────────────────────────────────────────────────

@ogcore_api.route("/ogc/getSession", methods=["GET"])
def ogc_get_session():
    return jsonify({"session": session.get("ogccase", None)}), 200


@ogcore_api.route("/ogc/setSession", methods=["POST"])
def ogc_set_session():
    err, code = validate_json_fields("casename")
    if err:
        return err, code
    cs = request.json["casename"]
    if cs is None:
        session.pop("ogccase", None)
        return jsonify({"ogccase": None}), 200
    if not Path(Config.OGC_DATA_STORAGE, cs).is_dir():
        return jsonify({"message": "Case not found.", "status_code": "error"}), 404
    session["ogccase"] = cs
    return jsonify({"ogccase": cs}), 200


# ── Cases ──────────────────────────────────────────────────────────────────

@ogcore_api.route("/ogc/getCases", methods=["GET"])
def ogc_get_cases():
    try:
        cases = [f.name for f in os.scandir(Config.OGC_DATA_STORAGE) if f.is_dir()]
        return jsonify(cases), 200
    except IOError:
        return jsonify([]), 200


@ogcore_api.route("/ogc/saveCase", methods=["POST"])
def ogc_save_case():
    err, code = validate_json_fields("data")
    if err:
        return err, code
    gen_data = request.json["data"]
    casename = gen_data.get("ogc-casename")
    if not casename:
        return jsonify({"message": "ogc-casename is required.", "status_code": "error"}), 400
    try:
        Config.validate_path(Config.OGC_DATA_STORAGE, casename)
    except PermissionError:
        return jsonify({"message": "Invalid case name.", "status_code": "error"}), 400

    if not is_safe_name(casename):
        return jsonify({"message": "Invalid case name.", "status_code": "error"}), 400

    case_path = Path(Config.OGC_DATA_STORAGE, casename)
    case = OGCoreCase(casename)
    if case_path.exists():
        response = case.save_case(gen_data)
    else:
        session["ogccase"] = casename
        response = case.create_case(gen_data)
    status_code = 400 if response.get("status_code") == "error" else 200
    return jsonify(response), status_code


@ogcore_api.route("/ogc/deleteCase", methods=["POST"])
def ogc_delete_case():
    err, code = validate_json_fields("casename")
    if err:
        return err, code
    casename = request.json["casename"]
    active = session.get("ogccase")
    if not active or casename != active:
        return jsonify({"message": "Unauthorised.", "status_code": "error"}), 403
    try:
        Config.validate_path(Config.OGC_DATA_STORAGE, casename)
        response = OGCoreCase(casename).delete_case()
        session.pop("ogccase", None)
        return jsonify(response), 200
    except PermissionError:
        return jsonify({"message": "Invalid path.", "status_code": "error"}), 400
    except FileNotFoundError:
        return jsonify({"message": "Case not found.", "status_code": "error"}), 404


# ── Parameter schema (for the frontend to build input forms) ───────────────

@ogcore_api.route("/ogc/getParameterSchema", methods=["GET"])
def ogc_get_parameter_schema():
    """OG-Core's user-facing parameter metadata (labels, defaults, ranges)."""
    try:
        return jsonify(load_parameter_schema()), 200
    except Exception as exc:  # noqa: BLE001 (report ogcore-not-installed and similar)
        return jsonify({"message": str(exc), "status_code": "error"}), 500


# ── Parameters ─────────────────────────────────────────────────────────────

@ogcore_api.route("/ogc/getParams", methods=["POST"])
def ogc_get_params():
    err, code = validate_json_fields("casename", "run_name")
    if err:
        return err, code
    d = request.json
    return jsonify(OGCoreCase(d["casename"]).get_params(d["run_name"])), 200


@ogcore_api.route("/ogc/saveParams", methods=["POST"])
def ogc_save_params():
    err, code = validate_json_fields("casename", "run_name", "params")
    if err:
        return err, code
    d = request.json
    response = OGCoreCase(d["casename"]).save_params(d["run_name"], d["params"])
    status_code = 404 if response.get("status_code") == "error" else 200
    return jsonify(response), status_code


# ── Tax-function parameters (mono / mono2D) ────────────────────────────────

@ogcore_api.route("/ogc/uploadTaxParams", methods=["POST"])
def ogc_upload_tax_params():
    casename = request.form.get("casename")
    run_name = request.form.get("run_name")
    if not casename or not run_name:
        return jsonify({"message": "casename and run_name required.", "status_code": "error"}), 400
    try:
        Config.validate_path(Config.OGC_DATA_STORAGE, casename)
    except PermissionError:
        return jsonify({"message": "Invalid case name.", "status_code": "error"}), 400
    if not Path(Config.OGC_DATA_STORAGE, casename).is_dir():
        return jsonify({"message": "Case not found.", "status_code": "error"}), 404
    f = request.files.get("file")
    if not f:
        return jsonify({"message": "No file provided.", "status_code": "error"}), 400
    response = OGCoreCase(casename).save_tax_params(run_name, f.read())
    status_code = 400 if response.get("status_code") == "error" else 200
    return jsonify(response), status_code


@ogcore_api.route("/ogc/getTaxParamsInfo", methods=["GET"])
def ogc_get_tax_params_info():
    casename = request.args.get("casename")
    run_name = request.args.get("run_name")
    if not casename or not run_name:
        return jsonify({"message": "casename and run_name required.", "status_code": "error"}), 400
    try:
        Config.validate_path(Config.OGC_DATA_STORAGE, casename)
    except PermissionError:
        return jsonify({"message": "Invalid case name.", "status_code": "error"}), 400
    return jsonify(OGCoreCase(casename).get_tax_params_info(run_name)), 200


# ── Runs ───────────────────────────────────────────────────────────────────

@ogcore_api.route("/ogc/createRun", methods=["POST"])
def ogc_create_run():
    err, code = validate_json_fields("casename", "run_name", "run_type")
    if err:
        return err, code
    d = request.json
    if d["run_type"] not in ("baseline", "reform"):
        return jsonify({"message": "run_type must be 'baseline' or 'reform'.",
                        "status_code": "error"}), 400
    response = OGCoreCase(d["casename"]).create_run(
        run_name=d["run_name"],
        run_type=d["run_type"],
        baseline_run_name=d.get("baseline_run_name"),
        params=d.get("params"),
    )
    status_code = 400 if response.get("status_code") == "error" else 200
    return jsonify(response), status_code


@ogcore_api.route("/ogc/deleteRun", methods=["POST"])
def ogc_delete_run():
    err, code = validate_json_fields("casename", "run_name")
    if err:
        return err, code
    return jsonify(OGCoreCase(request.json["casename"]).delete_run(request.json["run_name"])), 200


@ogcore_api.route("/ogc/getRuns", methods=["POST"])
def ogc_get_runs():
    err, code = validate_json_fields("casename")
    if err:
        return err, code
    return jsonify(OGCoreCase(request.json["casename"]).get_runs()), 200


# ── Execution ──────────────────────────────────────────────────────────────

@ogcore_api.route("/ogc/run", methods=["POST"])
def ogc_run():
    err, code = validate_json_fields("casename", "run_name")
    if err:
        return err, code
    d = request.json
    time_path = d.get("time_path", True)
    if not isinstance(time_path, bool):
        return jsonify({"message": "time_path must be a boolean.", "status_code": "error"}), 400
    response = OGCoreRunner(d["casename"]).run(d["run_name"], time_path=time_path)
    status_code = 400 if response.get("status_code") == "error" else 200
    return jsonify(response), status_code


@ogcore_api.route("/ogc/validateParams", methods=["POST"])
def ogc_validate_params():
    err, code = validate_json_fields("casename", "run_name")
    if err:
        return err, code
    d = request.json
    return jsonify(OGCoreRunner(d["casename"]).validate_params(d["run_name"])), 200


# ── Variable retrieval ─────────────────────────────────────────────────────

@ogcore_api.route("/ogc/getSSVars", methods=["POST"])
def ogc_get_ss_vars():
    err, code = validate_json_fields("casename", "run_name")
    if err:
        return err, code
    d = request.json
    result = OGCoreRunner(d["casename"]).get_ss_vars(d["run_name"], vars=d.get("vars"))
    if result is None:
        return jsonify({"message": "SS results not found. Run the model first.",
                        "status_code": "error"}), 404
    return jsonify(result), 200


@ogcore_api.route("/ogc/getTPIVars", methods=["POST"])
def ogc_get_tpi_vars():
    err, code = validate_json_fields("casename", "run_name")
    if err:
        return err, code
    d = request.json
    result = OGCoreRunner(d["casename"]).get_tpi_vars(d["run_name"], vars=d.get("vars"))
    if result is None:
        return jsonify({"message": "TPI results not found.", "status_code": "error"}), 404
    return jsonify(result), 200


# ── Analysis tables ────────────────────────────────────────────────────────

@ogcore_api.route("/ogc/getMacroTable", methods=["POST"])
def ogc_get_macro_table():
    err, code = validate_json_fields("casename", "base_run")
    if err:
        return err, code
    d = request.json
    try:
        result = OGCoreRunner(d["casename"]).get_macro_table(
            base_run=d["base_run"], reform_run=d.get("reform_run"),
            var_list=d.get("var_list"), output_type=d.get("output_type", "pct_diff"),
            num_years=d.get("num_years", 10), start_year=d.get("start_year"),
            include_SS=d.get("include_SS", True),
        )
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"message": str(exc), "status_code": "error"}), 500


@ogcore_api.route("/ogc/getMacroTableSS", methods=["POST"])
def ogc_get_macro_table_ss():
    err, code = validate_json_fields("casename", "base_run", "reform_run")
    if err:
        return err, code
    d = request.json
    try:
        result = OGCoreRunner(d["casename"]).get_macro_table_ss(
            d["base_run"], d["reform_run"], var_list=d.get("var_list"))
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"message": str(exc), "status_code": "error"}), 500


@ogcore_api.route("/ogc/getIneqTable", methods=["POST"])
def ogc_get_ineq_table():
    err, code = validate_json_fields("casename", "base_run")
    if err:
        return err, code
    d = request.json
    try:
        result = OGCoreRunner(d["casename"]).get_ineq_table(
            d["base_run"], reform_run=d.get("reform_run"), var_list=d.get("var_list"))
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"message": str(exc), "status_code": "error"}), 500


@ogcore_api.route("/ogc/getGiniTable", methods=["POST"])
def ogc_get_gini_table():
    err, code = validate_json_fields("casename", "base_run")
    if err:
        return err, code
    d = request.json
    try:
        result = OGCoreRunner(d["casename"]).get_gini_table(
            d["base_run"], reform_run=d.get("reform_run"), var_list=d.get("var_list"))
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"message": str(exc), "status_code": "error"}), 500


@ogcore_api.route("/ogc/getWealthMomentsTable", methods=["POST"])
def ogc_get_wealth_moments_table():
    err, code = validate_json_fields("casename", "base_run")
    if err:
        return err, code
    d = request.json
    try:
        result = OGCoreRunner(d["casename"]).get_wealth_moments_table(d["base_run"])
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"message": str(exc), "status_code": "error"}), 500


@ogcore_api.route("/ogc/getTimeSeriesTable", methods=["POST"])
def ogc_get_time_series_table():
    err, code = validate_json_fields("casename", "base_run")
    if err:
        return err, code
    d = request.json
    try:
        result = OGCoreRunner(d["casename"]).get_time_series_table(
            d["base_run"], reform_run=d.get("reform_run"))
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"message": str(exc), "status_code": "error"}), 500


@ogcore_api.route("/ogc/getRevenueDecomposition", methods=["POST"])
def ogc_get_revenue_decomposition():
    err, code = validate_json_fields("casename", "base_run", "reform_run")
    if err:
        return err, code
    d = request.json
    try:
        result = OGCoreRunner(d["casename"]).get_revenue_decomposition(
            d["base_run"], d["reform_run"],
            num_years=d.get("num_years", 10), start_year=d.get("start_year"),
            include_business_tax=d.get("include_business_tax", True),
            full_break_out=d.get("full_break_out", False),
        )
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"message": str(exc), "status_code": "error"}), 500


# ── Download ───────────────────────────────────────────────────────────────

@ogcore_api.route("/ogc/downloadResults", methods=["GET"])
def ogc_download_results():
    casename = request.args.get("casename")
    base_run = request.args.get("base_run")
    reform_run = request.args.get("reform_run")
    if not casename or not base_run:
        return jsonify({"message": "casename and base_run required.",
                        "status_code": "error"}), 400
    try:
        Config.validate_path(Config.OGC_DATA_STORAGE, casename)
        csv_path = OGCoreRunner(casename).generate_output_csv(base_run, reform_run)
        return send_file(csv_path.resolve(), as_attachment=True, max_age=0)
    except PermissionError:
        return jsonify({"message": "Invalid path.", "status_code": "error"}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"message": str(exc), "status_code": "error"}), 500


# ── Backup & Restore ───────────────────────────────────────────────────────

@ogcore_api.route("/ogc/backupCase", methods=["GET"])
def ogc_backup_case():
    casename = request.args.get("casename")
    if not casename:
        return jsonify({"message": "casename required.", "status_code": "error"}), 400
    try:
        case_path = Path(Config.validate_path(Config.OGC_DATA_STORAGE, casename))
        zip_path = Path(Config.validate_path(Config.OGC_DATA_STORAGE, f"{casename}.zip"))

        with ZipFile(zip_path, "w") as zf:
            for folder, _, files in os.walk(str(case_path)):
                for filename in files:
                    file_path = os.path.join(folder, filename)
                    # PurePosixPath keeps forward slashes in the archive so a
                    # Windows-created backup restores correctly on Linux/macOS.
                    arcname = str(
                        PurePosixPath(casename)
                        / os.path.relpath(file_path, str(case_path)).replace("\\", "/")
                    )
                    zf.write(file_path, arcname=arcname)

        # Delete the temp zip shortly after sending (mirrors CLEWS pattern).
        def _cleanup(path):
            time.sleep(30)
            try:
                os.remove(path)
            except OSError:
                pass

        Thread(target=_cleanup, args=(str(zip_path),), daemon=True).start()
        return send_file(zip_path.resolve(), as_attachment=True, max_age=0)

    except PermissionError:
        return jsonify({"message": "Invalid path.", "status_code": "error"}), 400
    except IOError:
        return jsonify({"message": "Case not found.", "status_code": "error"}), 404
    except OSError as exc:
        return jsonify({"message": str(exc), "status_code": "error"}), 500


@ogcore_api.route("/ogc/restoreCase", methods=["POST"])
def ogc_restore_case():
    f = request.files.get("file")
    if not f:
        return jsonify({"message": "No file provided.", "status_code": "error"}), 400
    try:
        with ZipFile(io.BytesIO(f.read()), "r") as zf:
            gen_data_entry = next(
                (name for name in zf.namelist() if name.endswith("genData.json")), None
            )
            if not gen_data_entry:
                return jsonify({
                    "message": "ZIP is not a valid OG-Core case backup (no genData.json).",
                    "status_code": "error",
                }), 400

            import json as _json
            gen_data = _json.loads(zf.read(gen_data_entry).decode("utf-8"))

            # Reject CLEWS backups (they carry osy-casename, not ogc-casename).
            if "ogc-casename" not in gen_data:
                return jsonify({
                    "message": "ZIP is not a valid OG-Core case backup (missing ogc-casename).",
                    "status_code": "error",
                }), 400

            casename = gen_data["ogc-casename"]
            if not casename:
                return jsonify({"message": "Invalid case name in backup.",
                                "status_code": "error"}), 400

            if Path(Config.OGC_DATA_STORAGE, casename).exists():
                return jsonify({"message": f"Case '{casename}' already exists.",
                                "status_code": "exist"}), 200

            # Block path traversal: every entry must resolve inside OGC_DATA_STORAGE.
            root = str(Config.OGC_DATA_STORAGE.resolve())
            for entry in zf.namelist():
                resolved = os.path.realpath(os.path.join(root, entry))
                if not (resolved == root or resolved.startswith(root + os.sep)):
                    return jsonify({"message": "ZIP contains unsafe paths.",
                                    "status_code": "error"}), 400

            zf.extractall(str(Config.OGC_DATA_STORAGE))

        return jsonify({
            "message": f"Case '{casename}' restored.",
            "status_code": "success",
            "casename": casename,
        }), 200

    except BadZipFile:
        return jsonify({"message": "Invalid or corrupt ZIP file.", "status_code": "error"}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"message": str(exc), "status_code": "error"}), 500
