import threading
import uuid
import time
from typing import Optional, Dict, Any

# Job status constants
JOB_PENDING   = "pending"
JOB_RUNNING   = "running"
JOB_COMPLETED = "completed"
JOB_ERROR     = "error"
JOB_CANCELLED = "cancelled"

MAX_JOBS = 100    # Maximum jobs kept in memory before eviction
JOB_TTL  = 86400  # Seconds before terminal jobs are evicted (24h)


class Job:
    """Tracks a single solver run — its status, result, and how to cancel it."""
    def __init__(self, job_id: str, casename: str, caserunname: str, solver: str):
        self.job_id      = job_id
        self.casename    = casename
        self.caserunname = caserunname
        self.solver      = solver
        self.status      = JOB_PENDING
        self.result      = None
        self.error       = None
        self.created_at  = time.time()
        self.started_at  = None
        self.finished_at = None
        self.cancel_event = threading.Event()
        self.process      = None

    def to_dict(self) -> Dict[str, Any]:
        """Job snapshot, safe to return directly in an HTTP response."""
        return {
            "job_id":       self.job_id,
            "casename":     self.casename,
            "caserunname":  self.caserunname,
            "solver":       self.solver,
            "status":       self.status,
            "result":       self.result,
            "error":        self.error,
            "created_at":   self.created_at,
            "started_at":   self.started_at,
            "finished_at":  self.finished_at,
        }

    def cancel(self):
        """Tell the solver to stop and kill the process if it's still running."""
        self.cancel_event.set()
        if self.process is not None:
            try:
                self.process.kill()
            except OSError:
                pass  # already finished


class JobManager:
    """
    Keeps track of all solver jobs. One instance shared across the app.
    Route handlers create jobs here; worker threads update them.
    """
    _instance: Optional["JobManager"] = None
    _class_lock = threading.Lock()

    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._jobs_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "JobManager":
        """Get the shared instance, creating it if it doesn't exist yet."""
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # Job lifecycle

    def create_job(self, casename: str, caserunname: str, solver: str) -> Job:
        """Start tracking a new job and return it."""
        job_id = str(uuid.uuid4())
        job = Job(job_id, casename, caserunname, solver)
        with self._jobs_lock:
            self._evict_old_jobs()
            self._jobs[job_id] = job
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        """Look up a job by its ID. Returns None if it doesn't exist."""
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def start_job(self, job_id: str):
        """Mark the job as running."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job and job.status == JOB_PENDING:
                job.status = JOB_RUNNING
                job.started_at = time.time()

    def complete_job(self, job_id: str, result: dict):
        """Store the result and mark the job as done."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job and job.status == JOB_RUNNING:
                job.status = JOB_COMPLETED
                job.result = result
                job.finished_at = time.time()

    def fail_job(self, job_id: str, error: str):
        """Store the error and mark the job as failed."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job and job.status == JOB_RUNNING:
                job.status = JOB_ERROR
                job.error = error
                job.finished_at = time.time()

    def cancel_job(self, job_id: str) -> bool:
        """
        Cancel a job that is still in progress.
        Returns True if the signal was sent, False if not found or already finished.
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in (JOB_COMPLETED, JOB_ERROR, JOB_CANCELLED):
                return False
            job.status = JOB_CANCELLED
            job.finished_at = time.time()
        job.cancel()
        return True

    # Cleanup

    def _evict_old_jobs(self):
        """Remove old finished jobs before adding a new one."""
        now = time.time()
        terminal = (JOB_COMPLETED, JOB_ERROR, JOB_CANCELLED)

        stale = [
            jid for jid, j in self._jobs.items()
            if j.status in terminal and j.finished_at is not None
            and (now - j.finished_at) > JOB_TTL
        ]
        for jid in stale:
            del self._jobs[jid]

        if len(self._jobs) >= MAX_JOBS:
            terminal_jobs = sorted(
                [(jid, j) for jid, j in self._jobs.items() if j.status in terminal],
                key=lambda x: x[1].finished_at or 0
            )
            for jid, _ in terminal_jobs[:len(self._jobs) - MAX_JOBS + 1]:
                del self._jobs[jid]
