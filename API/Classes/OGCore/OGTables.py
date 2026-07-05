"""MUIOGO-side helper for the worker's short-call (ephemeral) modes.

The heavy work of every analysis table, the parameter check, and the tax-file
check happens in the OG environment via ogc_worker.py. This module is the thin
MUIOGO side of that call: it resolves the calibration's interpreter, spawns one
short worker process against a temporary --out file, and returns the parsed
result. Like the rest of MUIOGO it never imports ogcore and uses only the
standard library; pandas and ogcore live behind the process boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from Classes.Base import Config
from Classes.OGCore.CalibrationRegistry import CalibrationRegistry

# The worker script is a sibling of this module; resolve it absolutely so the
# spawn does not depend on the current working directory. Kept local (not
# imported from OGRunner) so the table layer and the run layer do not couple.
WORKER_PATH = Path(__file__).parent / "ogc_worker.py"

# Endpoint table key -> (worker table key, reform_required, allowed option keys).
# The allowed keys are the passthrough options each endpoint exposes; they are a
# subset of the worker's own whitelist for that table.
TABLES = {
    "macro": (
        "macro",
        False,
        ("var_list", "output_type", "num_years", "start_year", "include_SS"),
    ),
    "macro_ss": ("macro_ss", True, ("var_list",)),
    "ineq": ("ineq", False, ("var_list",)),
    "gini": ("gini", False, ("var_list",)),
    "wealth_moments": ("wealth_moments", False, ()),
    "time_series": ("time_series", False, ()),
    "revenue_decomp": (
        "revenue_decomp",
        True,
        ("num_years", "start_year", "include_business_tax", "full_break_out"),
    ),
}


def resolve_python(case):
    """Return (python_path, error) for a case's country calibration interpreter.

    Mirrors RunJob._resolve_country_env, but only the interpreter part: the
    ephemeral modes do not need the country block. error is None on success;
    otherwise python_path is None and error is the user-facing message.
    """
    gd = case.gen_data
    country_id = gd.get("country_id")
    rec = CalibrationRegistry.get(country_id)
    if rec is None:
        return None, "That country calibration is not installed."
    python_path = rec.get("python_path")
    if not python_path or not Path(python_path).exists():
        return None, "The calibration's environment is missing; reinstall it."
    return python_path, None


def table_args(table_key, base_dir, reform_dir, options):
    """Build the worker argv (excluding --out) for a tables call."""
    worker_key = TABLES[table_key][0]
    argv = ["tables", "--base-dir", str(base_dir), "--table", worker_key]
    if reform_dir is not None:
        argv += ["--reform-dir", str(reform_dir)]
    if options:
        argv += ["--args-json", json.dumps(options)]
    return argv


def run_worker_mode(python_path, argv_list, timeout=180):
    """Spawn one ephemeral worker call and return (payload, error).

    A temp --out file is created and always deleted. On timeout the child is
    killed (subprocess.run does this) and error is a fixed message. A nonzero
    exit yields the worker's stderr, or a code fallback. On success the --out
    JSON is parsed and returned; a missing or corrupt result is an error.
    Exactly one of (payload, error) is meaningful; the other is None.
    """
    fd, out_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        cmd = [str(python_path), str(WORKER_PATH), *argv_list, "--out", out_path]
        try:
            proc = subprocess.run(
                cmd,
                env=Config.ogc_clean_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None, "The operation timed out."

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            error = stderr if stderr else f"Worker exited with code {proc.returncode}."
            return None, error

        try:
            with open(out_path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError) as exc:
            return None, f"Worker produced no readable result: {exc}"
        return payload, None
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
