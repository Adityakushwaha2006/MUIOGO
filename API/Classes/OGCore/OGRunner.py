"""Spawn and supervise ONE OG worker process.

This layer owns streaming, the wall-clock watchdog, and the kill. It never decides
business state: it launches the worker under the calibration's own interpreter,
drains its output, enforces a hard run-time deadline, and reports back an exit code.
Whether a run "completed" or "failed" is decided by RunJob from that exit code plus
the worker's terminal run_status.json, never from anything this class reads.

The streaming mechanism deliberately mirrors Installer._stream (a daemon reader
thread draining 256-byte chunks, splitting on \\r and \\n) rather than importing it:
the install layer and the run layer must not couple, so the pattern is replicated.
"""

from __future__ import annotations

import codecs
import collections
import os
import re
import signal
import subprocess
import threading
from pathlib import Path

from Classes.Base import Config

# A run can legitimately take hours (full transition-path solves), so the default
# wall-clock ceiling is generous; the env var lets an operator tighten or extend it.
RUN_TIMEOUT_SECONDS = int(os.environ.get("MUIOGO_OGC_RUN_TIMEOUT_SECONDS", "") or 6 * 3600)

# The worker script is a sibling of this module; resolve it absolutely so the spawn
# does not depend on the current working directory.
WORKER_PATH = Path(__file__).parent / "ogc_worker.py"

# OG-Core's steady-state loop logs "Iteration: {n}  Distance: {d}" and TPI logs
# "Iteration: {n}"; we pull just the number for a display-only progress hint.
_ITERATION_RE = re.compile(r"Iteration:\s*(\d+)")


class OGRunner:
    """Owns one worker subprocess and its output pump."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.iteration: int | None = None
        self.timed_out = False
        self._tail: collections.deque = collections.deque(maxlen=200)
        self._reader: threading.Thread | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────
    def spawn(self, python_path, run_dir) -> None:
        """Start the worker under `python_path` and begin draining its output.

        On POSIX the child is put in its own session so kill_tree can signal the
        whole process group (the solve fans out into Dask/distributed children).
        """
        cmd = [
            str(python_path),
            str(WORKER_PATH),
            "run",
            "--run-dir",
            str(run_dir),
        ]
        popen_kwargs = dict(
            env=Config.ogc_clean_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,  # unbuffered: surface iteration lines as the child flushes
        )
        if Config.SYSTEM != "Windows":
            popen_kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(cmd, **popen_kwargs)

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        """Drain stdout, split on \\r/\\n, keep the tail and the latest iteration."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        try:
            while True:
                chunk = self.proc.stdout.read(256)
                if not chunk:
                    break
                pending += decoder.decode(chunk)
                while True:
                    match = re.search(r"[\r\n]", pending)
                    if match is None:
                        break
                    segment = pending[: match.start()].strip()
                    pending = pending[match.end():]
                    if segment:
                        self._record(segment)
            pending += decoder.decode(b"", final=True)
            if pending.strip():
                self._record(pending.strip())
        except (OSError, ValueError):
            pass

    def _record(self, segment: str) -> None:
        self._tail.append(segment)
        m = _ITERATION_RE.search(segment)
        if m:
            self.iteration = int(m.group(1))

    def supervise(self) -> int:
        """Wait for the run under the wall-clock deadline; return the exit code.

        Joins the reader (which only ends when the child closes its pipe). If the
        deadline is hit while the reader is still alive the child is presumed hung
        and killed, returning 124. Otherwise the child has closed stdout and should
        be exiting, so wait briefly and fall back to a kill if it does not.
        """
        self._reader.join(timeout=RUN_TIMEOUT_SECONDS)

        if self._reader.is_alive():
            self.kill_tree()
            self.timed_out = True
            self._reader.join(timeout=5)
            self._close_stdout()
            return 124

        try:
            rc = self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.kill_tree()
            rc = self.proc.wait(timeout=5) if self.proc.poll() is None else self.proc.returncode
            if rc is None:
                rc = 124
        self._close_stdout()
        return rc

    def kill_tree(self) -> None:
        """Kill the worker and its children. Idempotent: a no-op if already dead."""
        if self.proc is None:
            return
        pid = self.proc.pid
        if Config.SYSTEM == "Windows":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

    def _close_stdout(self) -> None:
        if self.proc is not None and self.proc.stdout is not None:
            try:
                self.proc.stdout.close()
            except OSError:
                pass

    # ── introspection ──────────────────────────────────────────────────────
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def log_tail(self) -> list:
        return list(self._tail)

    def write_log(self, run_dir) -> None:
        """Persist the captured tail to run_dir/run_log.txt (atomic tmp + replace)."""
        run_dir = Path(run_dir)
        lines = list(self._tail)
        text = ("\n".join(lines) + "\n") if lines else ""
        target = run_dir / "run_log.txt"
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
