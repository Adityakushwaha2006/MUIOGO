"""Serialize OG model runs: one solve at a time, ever.

A single active run plus a FIFO queue, guarded by one process-wide lock. Solves are
CPU-and-memory heavy (each spins up its own Dask cluster), so two at once would
thrash the machine; this layer admits exactly one and queues the rest.

Completion is decided from the worker's exit code plus the terminal run_status.json
it wrote (stage "complete", ok True), never from stdout. Single-writer discipline
holds throughout: MUIOGO writes run_meta.json and run_log.txt; the worker owns
run_status.json and the results files, and this layer only reads them.
"""

from __future__ import annotations

import collections
import json
import threading
from pathlib import Path

from Classes.OGCore.CalibrationRegistry import CalibrationRegistry
from Classes.OGCore.OGCoreCase import OGCoreCase
from Classes.OGCore.OGRunner import OGRunner

# The four model dimensions a reform must share with its baseline. If a reform
# changed S/T/J/M it would no longer be comparable to the baseline it reforms.
_DIMS = ("S", "T", "J", "M")


def _read_json(path: Path):
    """Read a JSON object from path, tolerating missing/corrupt -> None."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class RunJob:
    _lock = threading.RLock()
    _active: dict | None = None       # {casename, run_name, runner, thread, cancelled}
    _queue: collections.deque = collections.deque()  # (casename, run_name, time_path)

    # ── run-dir helper ─────────────────────────────────────────────────────
    @staticmethod
    def _run_dir(casename: str, run_name: str) -> Path:
        return OGCoreCase(casename).res_path / run_name

    # ── country / interpreter resolution ───────────────────────────────────
    @staticmethod
    def _resolve_country_env(case: OGCoreCase):
        """Return (country_block, python_path, error).

        error is None on success; otherwise country/python_path are None and error
        is the user-facing message the caller surfaces.
        """
        gd = case.gen_data
        country_id = gd.get("country_id")
        rec = CalibrationRegistry.get(country_id)
        if rec is None:
            return None, None, "That country calibration is not installed."
        country = {
            "package_name": rec.get("package_name"),
            "is_base": rec.get("package_name") in (None, "", "ogcore"),
            "commit_sha": rec.get("commit_sha"),
        }
        python_path = rec.get("python_path")
        if not python_path or not Path(python_path).exists():
            return None, None, "The calibration's environment is missing; reinstall it."
        return country, python_path, None

    # ── status file read (worker-owned) ────────────────────────────────────
    @staticmethod
    def _read_status(run_dir: Path):
        return _read_json(Path(run_dir) / "run_status.json")

    # ── busy check ─────────────────────────────────────────────────────────
    @classmethod
    def is_busy(cls, casename: str, run_name: str) -> bool:
        with cls._lock:
            act = cls._active
            if act and act["casename"] == casename and act["run_name"] == run_name:
                return True
            return any(
                q[0] == casename and q[1] == run_name for q in cls._queue
            )

    @classmethod
    def case_busy(cls, casename: str) -> bool:
        """True if any run of this case is active or queued.

        Deleting a case while its worker holds files open would corrupt the tree,
        so the delete endpoints refuse while this is true.
        """
        with cls._lock:
            act = cls._active
            if act and act["casename"] == casename:
                return True
            return any(q[0] == casename for q in cls._queue)

    # ── start ──────────────────────────────────────────────────────────────
    @classmethod
    def start(cls, casename: str, run_name: str, time_path: bool) -> dict:
        """Validate, then either launch immediately or queue the run."""
        with cls._lock:
            case = OGCoreCase(casename)
            meta = case.get_run_meta(run_name)
            if not meta:
                return {"status_code": "error", "message": "Run not found."}

            if cls.is_busy(casename, run_name):
                return {
                    "status_code": "error",
                    "message": "This run is already running or queued.",
                }

            # Guard 1: reform must run against a completed baseline, and a transition
            # -path reform needs the baseline itself to have solved the full path.
            if meta.get("run_type") == "reform":
                baseline_meta = _read_json(
                    Path(meta.get("baseline_output_path") or "") / "run_meta.json"
                ) if meta.get("baseline_output_path") else None
                if time_path:
                    if (
                        baseline_meta is None
                        or baseline_meta.get("status") != "completed"
                        or baseline_meta.get("time_path") is not True
                    ):
                        return {
                            "status_code": "error",
                            "message": (
                                "The baseline must be run with the full transition "
                                "path before this reform."
                            ),
                        }
                if baseline_meta is None or baseline_meta.get("status") != "completed":
                    return {
                        "status_code": "error",
                        "message": "The baseline must complete before running a reform.",
                    }

                # Guard 2: reform must share the baseline's model dimensions.
                reform_params = _read_json(case.run_params_path(run_name)) or {}
                baseline_params = _read_json(
                    Path(meta["baseline_output_path"]) / "ogcParams.json"
                ) or {}
                for dim in _DIMS:
                    in_reform = dim in reform_params
                    in_baseline = dim in baseline_params
                    if in_reform != in_baseline:
                        return cls._dim_mismatch()
                    if in_reform and reform_params[dim] != baseline_params[dim]:
                        return cls._dim_mismatch()

            # Country block + interpreter.
            country, python_path, err = cls._resolve_country_env(case)
            if err:
                return {"status_code": "error", "message": err}

            # Atomic claim, or queue behind the active run.
            if cls._active is None:
                case.stamp_execution(run_name, time_path, country, "running")
                cls._launch(casename, run_name, time_path, python_path)
                return {"status_code": "success", "message": "Run started."}

            cls._queue.append((casename, run_name, time_path))
            case.stamp_execution(run_name, time_path, country, "pending")
            return {"status_code": "success", "message": "Run queued."}

    @staticmethod
    def _dim_mismatch() -> dict:
        return {
            "status_code": "error",
            "message": (
                "A reform must use the same model dimensions as its baseline "
                "(S, T, J, M)."
            ),
        }

    # ── launch (caller holds the lock) ─────────────────────────────────────
    @classmethod
    def _launch(cls, casename: str, run_name: str, time_path: bool, python_path) -> None:
        """Create the runner, record it active, and start the solve thread.

        The runner is created here (not inside the thread) so cancel() and
        get_live() have a live reference from the instant the run is claimed.
        """
        runner = OGRunner()
        thread = threading.Thread(
            target=cls._run_one,
            args=(casename, run_name, time_path, python_path, runner),
            daemon=True,
        )
        cls._active = {
            "casename": casename,
            "run_name": run_name,
            "runner": runner,
            "thread": thread,
            "cancelled": False,
        }
        thread.start()

    # ── worker supervision thread ──────────────────────────────────────────
    @classmethod
    def _run_one(cls, casename, run_name, time_path, python_path, runner) -> None:
        case = OGCoreCase(casename)
        run_dir = cls._run_dir(casename, run_name)
        try:
            try:
                runner.spawn(python_path, run_dir)
                rc = runner.supervise()
            except Exception:
                # Failure to even launch the worker is a run failure, not a crash.
                rc = -1

            status = cls._read_status(run_dir)

            with cls._lock:
                cancelled = bool(cls._active and cls._active.get("cancelled"))

            if cancelled:
                case.update_run_status(run_name, "failed", error="Cancelled by user.")
            elif rc == 124:
                case.update_run_status(
                    run_name,
                    "failed",
                    error="Run exceeded the maximum run time and was stopped.",
                )
            elif (
                rc == 0
                and status is not None
                and status.get("stage") == "complete"
                and status.get("ok") is True
            ):
                prov = status.get("provenance")
                if isinstance(prov, dict):
                    case.set_run_provenance(run_name, prov)
                case.update_run_status(run_name, "completed", time_path=time_path)
            else:
                error = None
                if status is not None and status.get("error"):
                    error = status.get("error")
                if not error:
                    error = f"Worker exited with code {rc}."
                case.update_run_status(run_name, "failed", error=error)

            try:
                runner.write_log(run_dir)
            except OSError:
                pass
        finally:
            with cls._lock:
                cls._active = None
                cls._start_next_locked()

    @classmethod
    def _start_next_locked(cls) -> None:
        """Pop and launch the next queued run. Caller holds the lock.

        Drains past any run whose calibration vanished while it waited, marking each
        such run failed, so one broken entry cannot wedge the whole queue.
        """
        while cls._queue:
            casename, run_name, time_path = cls._queue.popleft()
            case = OGCoreCase(casename)
            try:
                country, python_path, err = cls._resolve_country_env(case)
                if err:
                    cls._fail_quietly(case, run_name, err)
                    continue
                # Stamping rewrites run_meta.json and can fail on its own, so it is
                # inside the guard too: otherwise one unreadable run breaks out of
                # the drain and strands everything queued behind it.
                case.stamp_execution(run_name, time_path, country, "running")
            except (OSError, ValueError, KeyError, IndexError):
                cls._fail_quietly(case, run_name, "The run could not be started.")
                continue
            cls._launch(casename, run_name, time_path, python_path)
            return

    @staticmethod
    def _fail_quietly(case, run_name, message) -> None:
        """Mark a run failed, tolerating a meta file that cannot be written."""
        try:
            case.update_run_status(run_name, "failed", error=message)
        except (OSError, ValueError, KeyError, IndexError):
            pass

    # ── cancel ─────────────────────────────────────────────────────────────
    @classmethod
    def cancel(cls, casename: str, run_name: str) -> dict:
        with cls._lock:
            act = cls._active
            if act and act["casename"] == casename and act["run_name"] == run_name:
                act["cancelled"] = True
                try:
                    act["runner"].kill_tree()
                except OSError:
                    pass
                # The supervision thread's finalize writes the terminal status.
                return {"status_code": "cancelled"}

            for item in list(cls._queue):
                if item[0] == casename and item[1] == run_name:
                    cls._queue.remove(item)
                    # Dropping it from the queue is not enough. Nothing revisits a
                    # pending run later (only a running one is repaired on status
                    # read), so it has to be written terminal here or it reads as
                    # pending forever.
                    try:
                        OGCoreCase(casename).update_run_status(
                            run_name, "failed", error="Cancelled by user."
                        )
                    except (OSError, ValueError, KeyError, IndexError):
                        # File.writeFile turns any write error into IndexError.
                        pass
                    return {"status_code": "cancelled"}

            return {
                "status_code": "error",
                "message": "That run is not running or queued.",
            }

    # ── live view ──────────────────────────────────────────────────────────
    @classmethod
    def get_live(cls, casename: str, run_name: str) -> dict | None:
        with cls._lock:
            act = cls._active
            if act and act["casename"] == casename and act["run_name"] == run_name:
                runner = act["runner"]
                queued = False
            elif any(q[0] == casename and q[1] == run_name for q in cls._queue):
                runner = None
                queued = True
            else:
                return None

        stage_label = None
        if runner is not None:
            status = cls._read_status(cls._run_dir(casename, run_name))
            if status is not None:
                stage_label = status.get("label")

        return {
            "stage_label": stage_label,
            "iteration": runner.iteration if runner is not None else None,
            "log_tail": runner.log_tail() if runner is not None else [],
            "queued": queued,
        }
