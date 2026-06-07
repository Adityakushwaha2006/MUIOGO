"""
OG-Core case and run management (disk-backed CRUD).

This is the OG-Core counterpart to the CLEWS Case/DataFile classes. It owns the
on-disk layout for OG-Core cases but does not execute the model; execution lives
in OGCoreRunnerClass. Keeping the two apart means this file never has to import
the heavy OG-Core package just to read or write a case.

On-disk layout owned by this class::

    OGC_DATA_STORAGE/<casename>/
        genData.json              case metadata + run index
        res/<runname>/
            ogcParams.json        JSON parameter overrides (the og_spec dict)
            ogcTaxParams.pkl      cloudpickled tax-function objects (mono/mono2D only)
            run_meta.json         run type, baseline path, time_path, status, timestamps
            SS/                   created here; SS_vars.pkl is written by OG-Core
            TPI/                  created here; TPI_vars.pkl is written by OG-Core

Parameters live per run, not per case. In OG-Core a baseline and a reform are the
same model with different policy parameters (the reform is the policy change), so
each run keeps its own complete og_spec. This mirrors OG-Core itself, which writes
a separate model_params.pkl into every run's output directory. A case is just an
organisational container for related runs.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from Classes.Base import Config
from Classes.Base.FileClass import File

logger = logging.getLogger(__name__)

# Keys an uploaded mono/mono2D tax-function pickle must contain.
_REQUIRED_TAX_KEYS = {"tax_func_type", "etr_params", "mtrx_params", "mtry_params"}

# Names that are fine as OG-Core labels but illegal or dangerous as directory
# names. A case/run name becomes a directory, so it has to be a safe single path
# component. Otherwise mkdir throws an opaque OSError, or on Windows quietly
# creates a reserved device path.
_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_UNSAFE_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(0, 32)}
_MAX_NAME_LEN = 200


def is_safe_name(name) -> bool:
    """True if ``name`` is safe to use as a single filesystem directory name."""
    if not isinstance(name, str) or not name or name in (".", ".."):
        return False
    if len(name) > _MAX_NAME_LEN:
        return False
    if any(ch in _UNSAFE_CHARS for ch in name):
        return False
    if name != name.rstrip(". "):  # Windows silently strips trailing dot/space
        return False
    if name.split(".")[0].upper() in _RESERVED_NAMES:  # reserved device names
        return False
    return True


def _utc_now_iso() -> str:
    """Timezone-aware UTC timestamp in ISO-8601 form."""
    return datetime.now(timezone.utc).isoformat()


class OGCoreCase:
    """Manages one OG-Core case directory and the runs inside it."""

    def __init__(self, casename: str):
        self.casename = casename
        self.case_path = Path(Config.OGC_DATA_STORAGE, casename)
        self.gen_data_path = self.case_path / "genData.json"
        self.res_path = self.case_path / "res"
        self._gen_data: dict | None = None  # lazy cache

    # ── Per-run path helpers ───────────────────────────────────────────────

    def run_params_path(self, run_name: str) -> Path:
        return self.res_path / run_name / "ogcParams.json"

    def run_tax_params_path(self, run_name: str) -> Path:
        return self.res_path / run_name / "ogcTaxParams.pkl"

    # ── genData cache ──────────────────────────────────────────────────────

    @property
    def gen_data(self) -> dict:
        """Case metadata, read once and cached. Invalidated on every write."""
        if self._gen_data is None:
            self._gen_data = File.readFile(self.gen_data_path)
        return self._gen_data

    def _write_gen_data(self, data: dict) -> None:
        File.writeFile(data, self.gen_data_path)
        self._gen_data = None  # invalidate cache so next read is fresh

    # ── Case CRUD ──────────────────────────────────────────────────────────

    def create_case(self, gen_data: dict) -> dict:
        """
        Create a brand-new case directory and seed its files.

        Caller (route) guarantees the case does not already exist; ``exist_ok``
        is False here so a logic error surfaces loudly rather than silently
        overwriting an existing case.
        """
        if not is_safe_name(self.casename):
            return {"message": "Invalid case name.", "status_code": "error"}
        self.case_path.mkdir(parents=True, exist_ok=False)
        self.res_path.mkdir(parents=True, exist_ok=True)
        gen_data["ogc-runs"] = []
        gen_data["ogc-version"] = "1.0"
        self._write_gen_data(gen_data)
        logger.info("Created OG-Core case '%s'", self.casename)
        return {"message": f"Case {self.casename} created.", "status_code": "created"}

    def save_case(self, gen_data: dict) -> dict:
        """
        Update an existing case's metadata. The run index and version are carried
        over from the existing file so that editing case details never wipes runs.
        """
        existing = self.gen_data
        gen_data["ogc-runs"] = existing.get("ogc-runs", [])
        gen_data["ogc-version"] = existing.get("ogc-version", "1.0")
        self._write_gen_data(gen_data)
        logger.info("Updated OG-Core case '%s'", self.casename)
        return {"message": f"Case {self.casename} updated.", "status_code": "edited"}

    def delete_case(self) -> dict:
        """Remove the entire case directory and all runs within it."""
        shutil.rmtree(self.case_path)
        logger.info("Deleted OG-Core case '%s'", self.casename)
        return {"message": f"Case {self.casename} deleted.", "status_code": "success_session"}

    # ── Parameters (per run) ───────────────────────────────────────────────

    def get_params(self, run_name: str) -> dict:
        """Return a run's og_spec override dict, or {} if none saved yet."""
        path = self.run_params_path(run_name)
        return File.readFile(path) if path.exists() else {}

    def save_params(self, run_name: str, params: dict) -> dict:
        """Persist a run's og_spec override dict verbatim."""
        run_dir = self.res_path / run_name
        if not run_dir.is_dir():
            return {"message": "Run not found.", "status_code": "error"}
        File.writeFile(params, self.run_params_path(run_name))
        return {"message": "Parameters saved.", "status_code": "success"}

    # ── Tax-function parameters (mono / mono2D, per run) ───────────────────

    def save_tax_params(self, run_name: str, pkl_bytes: bytes) -> dict:
        """
        Validate and store an uploaded tax-function pickle.

        mono/mono2D tax functions are Python callables, so they cannot live in
        JSON. The user uploads them as a (cloud)pickle produced by
        ogcore.txfunc.tax_func_estimate, and we check the required keys are
        present before saving.

        Unpickling is only safe here because this is a local, single-user desktop
        app: the user is loading a file they produced themselves with OG-Core,
        which is the same trust boundary as running OG-Core directly. We never
        load remote pickles.
        """
        import cloudpickle

        try:
            tax_params = cloudpickle.loads(pkl_bytes)
        except Exception as exc:  # noqa: BLE001 (any deserialisation failure is reported below)
            logger.warning("Tax-param pickle could not be deserialised for '%s': %s",
                           self.casename, exc)
            return {"message": "Could not deserialize pkl file.", "status_code": "error"}

        if not isinstance(tax_params, dict):
            return {"message": "Tax-param pickle must contain a dict.", "status_code": "error"}

        missing = _REQUIRED_TAX_KEYS - set(tax_params.keys())
        if missing:
            return {
                "message": f"Invalid pkl. Missing keys: {sorted(missing)}.",
                "status_code": "error",
            }

        run_dir = self.res_path / run_name
        if not run_dir.is_dir():
            return {"message": "Run not found.", "status_code": "error"}

        with open(self.run_tax_params_path(run_name), "wb") as f:
            cloudpickle.dump(tax_params, f)
        logger.info("Stored tax params (%s) for case '%s' run '%s'",
                    tax_params.get("tax_func_type"), self.casename, run_name)
        return {
            "message": "Tax params loaded.",
            "tax_func_type": tax_params["tax_func_type"],
            "status_code": "success",
        }

    def get_tax_params_info(self, run_name: str) -> dict:
        """
        Metadata about a run's loaded tax params. It does not return the function
        objects themselves, since they are not JSON-serialisable.
        """
        path = self.run_tax_params_path(run_name)
        if not path.exists():
            return {"loaded": False}
        import cloudpickle

        stat = path.stat()
        with open(path, "rb") as f:
            tp = cloudpickle.load(f)
        return {
            "loaded": True,
            "tax_func_type": tp.get("tax_func_type"),
            "uploaded_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }

    # ── Run management ─────────────────────────────────────────────────────

    def create_run(
        self,
        run_name: str,
        run_type: str,
        baseline_run_name: str | None,
        params: dict | None = None,
    ) -> dict:
        """
        Create a run directory and register it in the case run index.

        `params` is the run's initial og_spec and gets written to the run's own
        ogcParams.json, so a run can be created with its full policy in one call,
        or created empty and filled in later via save_params.

        A reform run is refused unless its baseline exists and has finished.
        OG-Core's reform TPI reads the baseline's SS/TPI pickles as inputs, so a
        reform on an incomplete baseline simply cannot run.
        """
        if not is_safe_name(run_name):
            return {"message": "Invalid run name.", "status_code": "error"}
        if not self.gen_data_path.exists():
            return {"message": "Case not found.", "status_code": "error"}

        run_path = self.res_path / run_name
        if run_path.exists():
            return {"message": "Run with same name already exists.", "status_code": "exist"}

        if run_type == "reform":
            if not baseline_run_name:
                return {"message": "baseline_run_name required for reform runs.",
                        "status_code": "error"}
            baseline_meta_path = self.res_path / baseline_run_name / "run_meta.json"
            if not baseline_meta_path.exists():
                return {"message": "Baseline run not found.", "status_code": "error"}
            baseline_meta = File.readFile(baseline_meta_path)
            if baseline_meta.get("status") != "completed":
                return {
                    "message": "Baseline run must be completed before creating a reform run.",
                    "status_code": "error",
                }
            baseline_output_path = str(self.res_path / baseline_run_name)
        else:
            baseline_output_path = None

        (run_path / "SS").mkdir(parents=True, exist_ok=True)
        (run_path / "TPI").mkdir(parents=True, exist_ok=True)

        # Seed the run's own parameter file (empty dict if none supplied).
        File.writeFile(params or {}, self.run_params_path(run_name))

        run_meta = {
            "run_name": run_name,
            "run_type": run_type,
            "baseline_output_path": baseline_output_path,
            "time_path": None,  # set at execution time
            "status": "pending",
            "error": None,
            "created_at": _utc_now_iso(),
            "completed_at": None,
        }
        File.writeFile(run_meta, run_path / "run_meta.json")

        gd = self.gen_data
        runs = gd.get("ogc-runs", [])
        runs.append({
            "RunId": f"run_{len(runs)}",
            "RunName": run_name,
            "RunType": run_type,
            "baseline_run_name": baseline_run_name,
        })
        gd["ogc-runs"] = runs
        self._write_gen_data(gd)
        logger.info("Created %s run '%s' in case '%s'", run_type, run_name, self.casename)
        return {"message": "Run created.", "status_code": "success"}

    def delete_run(self, run_name: str) -> dict:
        """Remove a run directory and drop it from the case run index."""
        if not self.gen_data_path.exists():
            return {"message": "Case not found.", "status_code": "error"}
        run_path = self.res_path / run_name
        if run_path.exists():
            shutil.rmtree(run_path)
        gd = self.gen_data
        gd["ogc-runs"] = [r for r in gd.get("ogc-runs", []) if r["RunName"] != run_name]
        self._write_gen_data(gd)
        logger.info("Deleted run '%s' from case '%s'", run_name, self.casename)
        return {"message": "Run deleted.", "status_code": "success"}

    def get_runs(self) -> list:
        """
        The run index, with each run's live status pulled from its run_meta.json.
        A run that is listed in the index but missing its meta file is reported as
        pending; that is a defensive case and should not happen normally.

        Each entry is a copy, so the transient status fields are never written back
        into the cached gen_data. That stops a later create_run/delete_run on the
        same instance from leaking run_meta state into genData.json.

        A missing case returns an empty list rather than raising.
        """
        if not self.gen_data_path.exists():
            return []
        enriched = []
        for run in self.gen_data.get("ogc-runs", []):
            item = dict(run)  # copy: do not mutate cached gen_data
            meta_path = self.res_path / item["RunName"] / "run_meta.json"
            if meta_path.exists():
                meta = File.readFile(meta_path)
                item["status"] = meta.get("status", "pending")
                item["time_path"] = meta.get("time_path")
                item["completed_at"] = meta.get("completed_at")
                item["error"] = meta.get("error")
            else:
                item["status"] = "pending"
                item["time_path"] = None
                item["completed_at"] = None
                item["error"] = None
            enriched.append(item)
        return enriched

    def get_run_meta(self, run_name: str) -> dict:
        """Raw run_meta.json for a run, or {} if it does not exist."""
        path = self.res_path / run_name / "run_meta.json"
        return File.readFile(path) if path.exists() else {}

    def update_run_status(
        self,
        run_name: str,
        status: str,
        error: str | None = None,
        time_path: bool | None = None,
    ) -> None:
        """
        Update a run's status (and optionally time_path / error) in place.
        Stamps completed_at when the run reaches a terminal state.
        """
        path = self.res_path / run_name / "run_meta.json"
        meta = File.readFile(path)
        meta["status"] = status
        meta["error"] = error
        if time_path is not None:
            meta["time_path"] = time_path
        if status in ("completed", "failed"):
            meta["completed_at"] = _utc_now_iso()
        File.writeFile(meta, path)
