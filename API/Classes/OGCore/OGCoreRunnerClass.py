"""
OG-Core execution and result reading.

This class builds an OG-Core ``Specifications`` object from a case's stored
parameters, runs the model synchronously via ``ogcore.execute.runner``, and
reads the resulting on-disk pickles back into JSON-safe structures for the API.

Design notes
------------
* OG-Core is a heavy import (numba, dask, scipy). Every ogcore import is deferred
  to the method that needs it, so Flask startup stays fast and the API stays
  importable even when OG-Core is not installed.
* Execution is synchronous and blocking, mirroring the CLEWS solver pattern
  (subprocess.run). That is deliberate for a single-user local deployment.
* Reform runs depend on a completed baseline. A reform TPI reads the baseline's
  TPI pickle as an input (the baseline GDP path drives the reform fiscal rule),
  so we refuse a reform TPI whose baseline was run SS-only.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import multiprocessing
import pickle
from functools import lru_cache
from pathlib import Path

import numpy as np

from Classes.Base import Config
from Classes.Base.FileClass import File
from Classes.OGCore.OGCoreClass import OGCoreCase

logger = logging.getLogger(__name__)

# A default with more leaf elements than this is a calibration array rather than
# a form-renderable input. We keep its metadata but drop the bulky default value.
_SCHEMA_MAX_DEFAULT_ELEMENTS = 100


def _flatten_len(value) -> int:
    """Count leaf elements of a (possibly nested) list/tuple; scalars count as 1."""
    if isinstance(value, (list, tuple)):
        return sum(_flatten_len(v) for v in value) if value else 0
    return 1


@lru_cache(maxsize=1)
def load_parameter_schema() -> dict:
    """
    User-facing OG-Core parameter metadata, for the frontend to build input forms.

    We read the installed ogcore's default_parameters.json, locating it with
    find_spec so we never import the heavy package. Because it comes from the
    installed copy, the schema always matches the running OG-Core version and
    cannot drift. Returns a dict keyed by parameter name::

        {param: {title, description, section, subsection, type, default, min, max}}

    Large calibration arrays (for example ability/demographic matrices) keep their
    metadata but report default: null and large: true, which keeps the response
    small. Cached for the process, since the file does not change at runtime.
    """
    spec = importlib.util.find_spec("ogcore")
    if spec is None or not spec.origin:
        raise RuntimeError("ogcore is not installed")
    params_path = Path(spec.origin).parent / "default_parameters.json"
    with open(params_path, encoding="utf-8") as f:
        raw = json.load(f)

    schema: dict = {}
    for name, meta in raw.items():
        # Skip the JSON "schema" header block and anything that is not a param.
        if not isinstance(meta, dict) or "type" not in meta:
            continue
        value_entries = meta.get("value") or []
        default = value_entries[0].get("value") if value_entries else None

        entry = {
            "title": meta.get("title"),
            "description": meta.get("description"),
            "section": meta.get("section_1"),
            "subsection": meta.get("section_2"),
            "type": meta.get("type"),
        }
        rng = (meta.get("validators") or {}).get("range") or {}
        if "min" in rng:
            entry["min"] = rng["min"]
        if "max" in rng:
            entry["max"] = rng["max"]

        if _flatten_len(default) > _SCHEMA_MAX_DEFAULT_ELEMENTS:
            entry["default"] = None
            entry["large"] = True
        else:
            entry["default"] = default

        schema[name] = entry
    return schema


def _num_workers() -> int:
    """Worker count for the Dask cluster, matching run_ogcore_example.py."""
    return min(multiprocessing.cpu_count() or 1, 7)


def _quiet_distributed_logs() -> None:
    """
    Quiet down Dask/distributed INFO chatter, including the harmless
    CommClosedError tracebacks logged at INFO level when a worker cluster is torn
    down at the end of a run. Those are expected shutdown notices rather than
    errors, and at INFO they spam the application log on every run. Real problems
    (WARNING/ERROR) still come through.
    """
    for name in ("distributed", "distributed.batched", "distributed.scheduler",
                 "distributed.worker", "distributed.nanny", "distributed.core",
                 "distributed.comm", "distributed.http", "distributed.http.proxy",
                 "bokeh"):
        logging.getLogger(name).setLevel(logging.WARNING)

# Default macro variables used across the table endpoints.
_DEFAULT_MACRO_VARS = ["Y", "C", "K", "L", "r", "w"]

# Structural / dimensional parameters that a reform must share with its baseline.
# A reform reads the baseline's SS pickle (arrays shaped by S, J) and TPI pickle
# (length T), and macro_table asserts equal start_year. A mismatch on any of
# these is a hard incompatibility, not a policy choice.
_STRUCTURAL_FIELDS = ("S", "T", "J", "M", "I", "start_year")


def _serialize_numpy(obj):
    """
    Recursively convert numpy/Python values to STRICT-JSON-safe types.

    Two jobs:
      1. numpy to native (Flask's jsonify cannot serialise numpy types).
      2. Non-finite floats (NaN, +/-Inf) to None. OG-Core outputs can legitimately
         contain NaN/Inf (undefined periods, ratios over zero, ceilings). Left
         alone, jsonify emits the bare tokens NaN/Infinity, which are not valid
         JSON and make a browser's JSON.parse throw. Mapping them to null gives
         the frontend a clean gap instead of a parse error.
    """
    if isinstance(obj, np.ndarray):
        if obj.dtype.kind == "f":  # float array: null out non-finite (vectorised)
            if np.isfinite(obj).all():
                return obj.tolist()
            out = obj.astype(object)
            out[~np.isfinite(obj)] = None
            return out.tolist()
        if obj.dtype.kind == "c":  # complex array
            return [_serialize_numpy(x) for x in obj.tolist()]
        return obj.tolist()  # int / bool / etc., no non-finite possible
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.complexfloating):
        return {"real": float(obj.real), "imag": float(obj.imag)}
    if isinstance(obj, dict):
        return {k: _serialize_numpy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_numpy(i) for i in obj]
    if isinstance(obj, float):  # native float (e.g. from DataFrame.to_dict)
        return obj if math.isfinite(obj) else None
    return obj


def _safe_read_pickle(path: str):
    """Read a pickle via OG-Core's safe reader (handles its custom unpickling)."""
    from ogcore.utils import safe_read_pickle

    return safe_read_pickle(path)


class OGCoreRunner:
    """Builds Specifications, runs OG-Core, and reads results for one case."""

    def __init__(self, casename: str):
        self.casename = casename
        self.case = OGCoreCase(casename)
        self.case_path = Path(Config.OGC_DATA_STORAGE, casename)
        self.res_path = self.case_path / "res"

    # ── Specifications builder ─────────────────────────────────────────────

    def _build_specs(self, run_name: str):
        """
        Build a ``Specifications`` object for the given run.

        For standard (DEP) tax functions, the override dict is passed straight to
        update_specifications. For mono/mono2D tax functions, the callable
        tax-function objects (stored separately in ogcTaxParams.pkl because they
        are not JSON-serialisable) are merged into the override dict and passed in
        one call. That lets update_specifications run them through its native mono
        deferral path: pull them out of the revision, adjust the rest, set them
        back, then compute_default_params. The ordering matters, because the tax
        functions must be in place before compute_default_params runs.
        """
        from ogcore.parameters import Specifications

        meta = self.case.get_run_meta(run_name)
        run_type = meta["run_type"]
        baseline_output_path = meta.get("baseline_output_path")

        output_base = str(self.res_path / run_name)
        # For a baseline, baseline_dir points at itself; for a reform it points
        # at the completed baseline's output directory.
        baseline_dir = baseline_output_path if baseline_output_path else output_base

        p = Specifications(
            output_base=output_base,
            baseline_dir=baseline_dir,
            baseline=(run_type == "baseline"),
            num_workers=_num_workers(),  # matches the Dask cluster created in run()
        )

        og_spec = dict(self.case.get_params(run_name))  # copy: update_specifications mutates it

        tax_params_path = self.case.run_tax_params_path(run_name)
        if tax_params_path.exists():
            import cloudpickle

            with open(tax_params_path, "rb") as f:
                tax_params = cloudpickle.load(f)
            # Merge the mono tax-function objects into the revision dict so the
            # library handles them correctly (see method docstring).
            og_spec["tax_func_type"] = tax_params["tax_func_type"]
            for key in ("etr_params", "mtrx_params", "mtry_params"):
                if key in tax_params:
                    og_spec[key] = tax_params[key]
            p.update_specifications(og_spec)
        elif og_spec:
            p.update_specifications(og_spec)

        return p

    # ── Execution ──────────────────────────────────────────────────────────

    def _structural_mismatches(self, p, base_p) -> list:
        """Return the list of structural fields where a reform diverges from its baseline."""
        mism = []
        for field in _STRUCTURAL_FIELDS:
            if int(getattr(p, field)) != int(getattr(base_p, field)):
                mism.append(f"{field} (reform={int(getattr(p, field))}, baseline={int(getattr(base_p, field))})")
        return mism

    def run(self, run_name: str, time_path: bool = True) -> dict:
        """
        Execute OG-Core synchronously for a run.

        Two pre-execution guards for reform runs. Both fail before any solve, so
        the run stays pending and the user can fix it and retry:

        1. Option B: a reform asking for the full transition path needs its
           baseline to have finished with time_path=True, otherwise the baseline
           TPI pickle the reform reads does not exist.
        2. Structural compatibility: a reform must share S, T, J, M, I and
           start_year with its baseline, or the baseline SS/TPI arrays it reads
           are dimensionally incompatible.
        """
        meta = self.case.get_run_meta(run_name)
        if not meta:
            return {"message": "Run not found.", "status_code": "error"}
        run_type = meta.get("run_type", "baseline")
        baseline_path = meta.get("baseline_output_path")

        # Guard 1: Option B (reform TPI needs baseline TPI)
        if run_type == "reform" and time_path and baseline_path:
            baseline_meta_path = Path(baseline_path) / "run_meta.json"
            baseline_meta = (
                File.readFile(baseline_meta_path) if baseline_meta_path.exists() else {}
            )
            if not baseline_meta.get("time_path", False):
                return {
                    "message": (
                        "Reform TPI requires the baseline to have been run with "
                        "time_path=True. Re-run the baseline with full transition "
                        "path, or run this reform as SS-only (time_path=False)."
                    ),
                    "status_code": "error",
                }

        # Build Specifications once (cheap, in-memory). Invalid params raise here.
        try:
            p = self._build_specs(run_name)
        except Exception as exc:  # noqa: BLE001 (invalid parameters count as a run failure)
            err_msg = str(exc)
            logger.error("OG-Core spec build failed: case=%s run=%s error=%s",
                         self.casename, run_name, err_msg)
            self.case.update_run_status(run_name, "failed", error=err_msg, time_path=time_path)
            return {"message": err_msg, "status_code": "failed"}

        # Guard 2: structural compatibility (reform must match its baseline)
        if run_type == "reform" and baseline_path:
            base_params_pkl = Path(baseline_path) / "model_params.pkl"
            if base_params_pkl.is_file():
                base_p = _safe_read_pickle(str(base_params_pkl))
                mism = self._structural_mismatches(p, base_p)
                if mism:
                    return {
                        "message": (
                            "Reform structural parameters must match the baseline: "
                            + "; ".join(mism)
                            + ". A reform may only change policy, not model dimensions."
                        ),
                        "status_code": "error",
                    }

        # Execute
        self.case.update_run_status(run_name, "running", time_path=time_path)
        client = None
        try:
            # OG-Core requires a distributed client: TPI calls client.scatter/
            # submit/gather unconditionally. We use a process-based LocalCluster,
            # which is the OG-Core example's proven config. The in-process threaded
            # cluster (processes=False) has a scatter(broadcast=True) race that
            # deadlocks the TPI loop. Dashboard off; client closed in finally.
            import logging as _logging
            from distributed import Client
            from ogcore.execute import runner
            _quiet_distributed_logs()
            client = Client(
                n_workers=_num_workers(), threads_per_worker=1,
                dashboard_address=None, silence_logs=_logging.WARNING,
            )
            logger.info(
                "OG-Core run start: case=%s run=%s type=%s time_path=%s workers=%d",
                self.casename, run_name, run_type, time_path, _num_workers(),
            )
            runner(p, time_path=time_path, client=client)
            self.case.update_run_status(run_name, "completed")
            logger.info("OG-Core run complete: case=%s run=%s", self.casename, run_name)
            return {"message": "Run completed.", "status_code": "success", "run_name": run_name}
        except Exception as exc:  # noqa: BLE001 (any failure is reported to the caller)
            err_msg = str(exc)
            logger.error(
                "OG-Core run failed: case=%s run=%s", self.casename, run_name, exc_info=True
            )
            self.case.update_run_status(run_name, "failed", error=err_msg)
            return {"message": err_msg, "status_code": "failed"}
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001 (cleanup is best-effort)
                    pass

    def validate_params(self, run_name: str) -> dict:
        """
        Validate a run's stored override dict against OG-Core's own validators.
        This checks the JSON parameters only; the mono tax-function objects stored
        in the pkl are not part of this check.
        """
        from ogcore.parameters import revision_warnings_errors

        og_spec = dict(self.case.get_params(run_name))
        result = revision_warnings_errors(og_spec)
        return {
            "valid": result["errors"] == "",
            "warnings": result["warnings"],
            "errors": result["errors"],
        }

    # ── Variable retrieval ─────────────────────────────────────────────────

    def get_ss_vars(self, run_name: str, vars: list | None = None) -> dict | None:
        """Selected steady-state variables as JSON-safe values, or None if absent."""
        ss_path = self.res_path / run_name / "SS" / "SS_vars.pkl"
        if not ss_path.is_file():
            return None
        ss = _safe_read_pickle(str(ss_path))
        if vars:
            ss = {k: ss[k] for k in vars if k in ss}
        return _serialize_numpy(ss)

    def get_tpi_vars(self, run_name: str, vars: list | None = None) -> dict | None:
        """Selected transition-path variables as JSON-safe values, or None if absent."""
        tpi_path = self.res_path / run_name / "TPI" / "TPI_vars.pkl"
        if not tpi_path.is_file():
            return None
        tpi = _safe_read_pickle(str(tpi_path))
        if vars:
            tpi = {k: tpi[k] for k in vars if k in tpi}
        return _serialize_numpy(tpi)

    # ── Result loaders (shared) ────────────────────────────────────────────

    def _load_tpi_and_params(self, run_name: str):
        tpi = _safe_read_pickle(str(self.res_path / run_name / "TPI" / "TPI_vars.pkl"))
        p = _safe_read_pickle(str(self.res_path / run_name / "model_params.pkl"))
        return tpi, p

    def _load_ss_and_params(self, run_name: str):
        ss = _safe_read_pickle(str(self.res_path / run_name / "SS" / "SS_vars.pkl"))
        p = _safe_read_pickle(str(self.res_path / run_name / "model_params.pkl"))
        return ss, p

    # ── Analysis tables ────────────────────────────────────────────────────

    def get_macro_table(
        self,
        base_run: str,
        reform_run: str | None = None,
        var_list: list | None = None,
        output_type: str = "pct_diff",
        num_years: int = 10,
        start_year: int | None = None,
        include_SS: bool = True,
    ) -> list:
        from ogcore import output_tables as ot

        var_list = var_list or _DEFAULT_MACRO_VARS
        base_tpi, base_p = self._load_tpi_and_params(base_run)
        reform_tpi, reform_p = (
            self._load_tpi_and_params(reform_run) if reform_run else (None, None)
        )
        # Default to the model's own start_year so table columns are correctly
        # year-aligned, rather than a hardcoded constant.
        if start_year is None:
            start_year = int(base_p.start_year)
        df = ot.macro_table(
            base_tpi, base_p,
            reform_tpi=reform_tpi, reform_params=reform_p,
            var_list=var_list, output_type=output_type,
            num_years=num_years, start_year=start_year, include_SS=include_SS,
        )
        return _serialize_numpy(df.to_dict(orient="records"))

    def get_macro_table_ss(
        self, base_run: str, reform_run: str, var_list: list | None = None
    ) -> list:
        from ogcore import output_tables as ot

        var_list = var_list or _DEFAULT_MACRO_VARS
        base_ss, _ = self._load_ss_and_params(base_run)
        reform_ss, _ = self._load_ss_and_params(reform_run)
        df = ot.macro_table_SS(base_ss, reform_ss, var_list=var_list)
        return _serialize_numpy(df.to_dict(orient="records"))

    def get_ineq_table(
        self, base_run: str, reform_run: str | None = None, var_list: list | None = None
    ) -> list:
        from ogcore import output_tables as ot

        var_list = var_list or ["c"]
        base_ss, base_p = self._load_ss_and_params(base_run)
        reform_ss, reform_p = (
            self._load_ss_and_params(reform_run) if reform_run else (None, None)
        )
        df = ot.ineq_table(
            base_ss, base_p, reform_ss=reform_ss, reform_params=reform_p, var_list=var_list
        )
        return _serialize_numpy(df.to_dict(orient="records"))

    def get_gini_table(
        self, base_run: str, reform_run: str | None = None, var_list: list | None = None
    ) -> list:
        from ogcore import output_tables as ot

        var_list = var_list or ["c"]
        base_ss, base_p = self._load_ss_and_params(base_run)
        reform_ss, reform_p = (
            self._load_ss_and_params(reform_run) if reform_run else (None, None)
        )
        df = ot.gini_table(
            base_ss, base_p, reform_ss=reform_ss, reform_params=reform_p, var_list=var_list
        )
        return _serialize_numpy(df.to_dict(orient="records"))

    def get_wealth_moments_table(self, base_run: str) -> list:
        from ogcore import output_tables as ot

        base_ss, base_p = self._load_ss_and_params(base_run)
        df = ot.wealth_moments_table(base_ss, base_p)
        return _serialize_numpy(df.to_dict(orient="records"))

    def get_time_series_table(self, base_run: str, reform_run: str | None = None) -> list:
        from ogcore import output_tables as ot

        base_tpi, base_p = self._load_tpi_and_params(base_run)
        reform_tpi, reform_p = (
            self._load_tpi_and_params(reform_run) if reform_run else (None, None)
        )
        df = ot.time_series_table(
            base_p, base_tpi, reform_params=reform_p, reform_tpi=reform_tpi
        )
        return _serialize_numpy(df.to_dict(orient="records"))

    def get_revenue_decomposition(
        self,
        base_run: str,
        reform_run: str,
        num_years: int = 10,
        start_year: int | None = None,
        include_business_tax: bool = True,
        full_break_out: bool = False,
    ) -> list:
        from ogcore import output_tables as ot

        base_tpi, base_p = self._load_tpi_and_params(base_run)
        base_ss, _ = self._load_ss_and_params(base_run)
        reform_tpi, reform_p = self._load_tpi_and_params(reform_run)
        reform_ss, _ = self._load_ss_and_params(reform_run)
        if start_year is None:
            start_year = int(base_p.start_year)
        df = ot.dynamic_revenue_decomposition(
            base_p, base_tpi, base_ss,
            reform_p, reform_tpi, reform_ss,
            num_years=num_years, start_year=start_year,
            include_business_tax=include_business_tax, full_break_out=full_break_out,
        )
        return _serialize_numpy(df.to_dict(orient="records"))

    # ── CSV export ─────────────────────────────────────────────────────────

    def generate_output_csv(self, base_run: str, reform_run: str | None = None) -> Path:
        """Write the macro percentage-change table to CSV and return its path."""
        from ogcore import output_tables as ot

        base_tpi, base_p = self._load_tpi_and_params(base_run)
        reform_tpi, reform_p = (
            self._load_tpi_and_params(reform_run) if reform_run else (None, None)
        )
        df = ot.macro_table(
            base_tpi, base_p,
            reform_tpi=reform_tpi, reform_params=reform_p,
            var_list=_DEFAULT_MACRO_VARS, output_type="pct_diff",
            num_years=10, start_year=int(base_p.start_year), include_SS=True,
        )
        out_path = self.case_path / "ogcore_example_output.csv"
        df.to_csv(out_path, index=False)
        return out_path
