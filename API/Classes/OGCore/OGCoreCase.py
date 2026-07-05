"""
OG-Core case and run management (disk-backed CRUD).

This class owns the on-disk case/run layout for OG-Core. It never executes the
model and never imports the ogcore package: execution runs in a separate OG
environment driven by the worker layer, so this file stays a pure filesystem
CRUD surface that any MUIOGO request thread can touch safely.

On-disk layout owned by this class::

    OGC_CASES_DIR/<casename>/
        genData.json              case metadata + run index (includes country_id)
        res/<runname>/
            ogcParams.json        the override dict (only what the user changed)
            run_meta.json         run type, baseline path, time_path, status, timestamps

SS/, TPI/, run_status.json, and results_*.json are created at run time by the
worker layer, not here; this class only lays down the case, the run directory,
and the two seed JSON files above.

Parameters live per run, not per case. In OG-Core a baseline and a reform are the
same model with different policy parameters (the reform is the policy change), so
each run keeps its own override dict. A case is just an organisational container
for related runs.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from Classes.Base import Config
from Classes.Base.FileClass import File

logger = logging.getLogger(__name__)

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
        # Cases live under their own subfolder, never inside OGC_DATA_STORAGE
        # alongside the registry / install-job / installer-cache entries.
        self.case_path = Path(Config.OGC_CASES_DIR, casename)
        self.gen_data_path = self.case_path / "genData.json"
        self.res_path = self.case_path / "res"
        self._gen_data: dict | None = None  # lazy cache

    # ── Per-run path helpers ───────────────────────────────────────────────

    def run_params_path(self, run_name: str) -> Path:
        return self.res_path / run_name / "ogcParams.json"

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

        Caller (route) guarantees the case does not already exist; the case dir
        itself is created with ``exist_ok`` False so a logic error surfaces loudly
        rather than silently overwriting an existing case. The parent cases folder
        is created first (exist_ok True) so the very first case does not fail on a
        missing OGC_CASES_DIR.

        ``gen_data`` arrives from the route already carrying country_id,
        ogc-casename, and ogc-description; this method only stamps the run index
        and schema version.
        """
        if not is_safe_name(self.casename):
            return {"message": "Invalid case name.", "status_code": "error"}
        Config.OGC_CASES_DIR.mkdir(parents=True, exist_ok=True)
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

    @classmethod
    def list_cases(cls) -> list[dict]:
        """
        A summary row for every case on disk.

        A single unreadable or malformed case directory must never break the whole
        listing, so any per-directory failure is logged and that directory skipped.
        """
        cases: list[dict] = []
        cases_dir = Config.OGC_CASES_DIR
        if not cases_dir.is_dir():
            return cases
        for entry in sorted(cases_dir.iterdir()):
            if not entry.is_dir():
                continue
            gen_path = entry / "genData.json"
            try:
                gd = File.readFile(gen_path)
                mtime = gen_path.stat().st_mtime
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("Skipping unreadable OG-Core case dir '%s': %s", entry.name, exc)
                continue
            modified_at = datetime.fromtimestamp(mtime, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            cases.append({
                "casename": gd.get("ogc-casename", entry.name),
                "country_id": gd.get("country_id"),
                "description": gd.get("ogc-description", ""),
                "modified_at": modified_at,
                "has_results": cls._case_has_results(entry),
            })
        return cases

    @staticmethod
    def _case_has_results(case_dir: Path) -> bool:
        """True if any run in the case has a completed run_meta. Defensive throughout."""
        res_dir = case_dir / "res"
        if not res_dir.is_dir():
            return False
        try:
            run_dirs = [d for d in res_dir.iterdir() if d.is_dir()]
        except OSError:
            return False
        for run_dir in run_dirs:
            meta_path = run_dir / "run_meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = File.readFile(meta_path)
            except (OSError, ValueError, KeyError):
                continue
            if isinstance(meta, dict) and meta.get("status") == "completed":
                return True
        return False

    # ── Parameters (per run) ───────────────────────────────────────────────

    def get_params(self, run_name: str) -> dict:
        """Return a run's override dict, or {} if none saved yet."""
        path = self.run_params_path(run_name)
        return File.readFile(path) if path.exists() else {}

    def save_params(self, run_name: str, params: dict) -> dict:
        """Persist a run's override dict verbatim."""
        run_dir = self.res_path / run_name
        if not run_dir.is_dir():
            return {"message": "Run not found.", "status_code": "error"}
        File.writeFile(params, self.run_params_path(run_name))
        return {"message": "Parameters saved.", "status_code": "success"}

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

        ``params`` is the run's initial override dict and gets written to the run's
        own ogcParams.json, so a run can be created with its full policy in one
        call, or created empty and filled in later via save_params. Only the run
        directory and its two seed JSON files are created here; SS/, TPI/, and
        results are laid down at execution time by the worker layer.

        A case holds exactly one baseline. A reform must name an existing baseline
        run; completion is not checked here, since the run-time guard enforces that
        a reform only executes once its baseline has finished.
        """
        if not is_safe_name(run_name):
            return {"message": "Invalid run name.", "status_code": "error"}
        if not self.gen_data_path.exists():
            return {"message": "Case not found.", "status_code": "error"}
        if run_type not in ("baseline", "reform"):
            return {"message": "run_type must be 'baseline' or 'reform'.",
                    "status_code": "error"}

        run_path = self.res_path / run_name
        if run_path.exists():
            return {"message": "Run with same name already exists.", "status_code": "exist"}

        if run_type == "baseline":
            # One baseline per case: reforms read the baseline's outputs, so there
            # is exactly one baseline to reform against.
            if self.get_baseline_name() is not None:
                return {"message": "This case already has a baseline run.",
                        "status_code": "exist"}
            baseline_output_path = None
        else:  # reform
            if not baseline_run_name:
                return {"message": "baseline_run_name required for reform runs.",
                        "status_code": "error"}
            baseline_meta_path = self.res_path / baseline_run_name / "run_meta.json"
            if not baseline_meta_path.exists():
                return {"message": "Baseline run not found.", "status_code": "error"}
            index_entry = next(
                (r for r in self.gen_data.get("ogc-runs", [])
                 if r.get("RunName") == baseline_run_name),
                None,
            )
            if index_entry is None or index_entry.get("RunType") != "baseline":
                return {"message": "baseline_run_name must name a baseline run.",
                        "status_code": "error"}
            baseline_output_path = str(self.res_path / baseline_run_name)

        run_path.mkdir(parents=True, exist_ok=True)

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

    def get_baseline_name(self) -> str | None:
        """RunName of the case's baseline index entry, or None if there is none."""
        if not self.gen_data_path.exists():
            return None
        for run in self.gen_data.get("ogc-runs", []):
            if run.get("RunType") == "baseline":
                return run.get("RunName")
        return None

    def delete_run(self, run_name: str) -> dict:
        """
        Remove a run directory and drop it from the case run index.

        Deleting the baseline invalidates every reform that depends on its outputs,
        so removing the baseline removes the entire case.
        """
        if not self.gen_data_path.exists():
            return {"message": "Case not found.", "status_code": "error"}
        if self.get_baseline_name() == run_name:
            shutil.rmtree(self.case_path)
            logger.info("Deleted baseline run '%s'; removed case '%s'",
                        run_name, self.casename)
            return {
                "message": f"Baseline removed; case {self.casename} deleted.",
                "status_code": "success_session",
            }
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

    def get_runs_shaped(self) -> dict:
        """
        The run index in contract shape: a single baseline (or None) plus the list
        of reforms, each enriched with live status exactly as get_runs does.
        """
        baseline = None
        reforms = []
        for item in self.get_runs():
            if item.get("RunType") == "baseline":
                baseline = item
            else:
                reforms.append(item)
        return {"baseline": baseline, "reforms": reforms}

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
        Stamps completed_at when the run reaches a terminal state. Statuses used
        are pending / running / completed / failed.
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

    def stamp_execution(
        self,
        run_name: str,
        time_path: bool,
        country: dict,
        status: str,
    ) -> None:
        """
        Record the execution facts chosen at launch time onto a run's meta.

        Called once when a run is claimed (status "running") or queued (status
        "pending"): it pins the time_path the user asked for and the country block
        the worker will layer, and clears any stale error from a prior attempt.
        """
        path = self.res_path / run_name / "run_meta.json"
        meta = File.readFile(path)
        meta["time_path"] = time_path
        meta["country"] = country
        meta["status"] = status
        meta["error"] = None
        File.writeFile(meta, path)

    def set_run_provenance(self, run_name: str, provenance: dict) -> None:
        """Store the worker's provenance snapshot on a completed run's meta."""
        path = self.res_path / run_name / "run_meta.json"
        meta = File.readFile(path)
        meta["provenance"] = provenance
        File.writeFile(meta, path)
