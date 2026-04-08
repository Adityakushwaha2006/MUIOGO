from flask import Blueprint, jsonify
from pathlib import Path
import pandas as pd
from Classes.Base import Config

ogcore_api = Blueprint('OGCoreRoute', __name__)


@ogcore_api.route("/ogcore/schema", methods=['GET'])
def ogcore_schema():
    """
    Returns the complete CLEWS output variable schema derived from Config.VARIABLES_C.
    Lists all solver output variables with their column definitions and whether
    they carry a dual (shadow price) value instead of a primal value.

    Intended as the static schema contract for OG-Core integration — use this
    to know what CLEWS produces and what columns to expect in each result CSV
    before calling /ogcore/results.
    """
    dual_keys = set(Config.DUALS.keys())
    schema = {}

    for variable, columns in Config.VARIABLES_C.items():
        schema[variable] = {
            "columns": columns,
            "value_column": variable,
            "is_dual": variable in dual_keys
        }

    return jsonify({
        "status_code": "success",
        "variable_count": len(schema),
        "schema": schema
    }), 200


@ogcore_api.route("/ogcore/results/<casename>/<caserunname>", methods=['GET'])
def ogcore_results(casename, caserunname):
    """
    Returns structured solver results for a completed case run.

    Reads the CSV files produced by generateCSVfromCBC() from:
      DataStorage/<casename>/res/<caserunname>/csv/<VariableName>.csv

    Returns 404 if the run directory does not exist on disk or if no CSV
    results have been written yet.
    Returns 200 with result records keyed by variable name once the run is complete.

    Only variables that have a CSV on disk are included in the response.
    The solver does not always produce all variables — this depends on model
    configuration. Missing variables are counted but not included in results.
    """
    run_dir = Path(Config.DATA_STORAGE, casename, 'res', caserunname)
    if not run_dir.is_dir():
        return jsonify({
            "message": f"Run '{caserunname}' not found for case '{casename}'.",
            "status_code": "error"
        }), 404

    csv_dir = run_dir / 'csv'
    if not csv_dir.is_dir():
        return jsonify({
            "message": "Run directory exists but no results were found. Has the solver completed successfully?",
            "status_code": "error"
        }), 404

    results = {}
    variables_missing = []

    for variable in Config.VARIABLES_C:
        csv_path = csv_dir / f"{variable}.csv"
        if csv_path.is_file():
            df = pd.read_csv(csv_path)
            results[variable] = df.to_dict(orient='records')
        else:
            variables_missing.append(variable)

    return jsonify({
        "status_code": "success",
        "casename": casename,
        "caserunname": caserunname,
        "variables_found": len(results),
        "variables_missing": len(variables_missing),
        "results": results
    }), 200
