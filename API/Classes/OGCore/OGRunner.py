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
import time
from pathlib import Path

from Classes.Base import Config

# A run can legitimately take hours (full transition-path solves), so the default
# wall-clock ceiling is generous; the env var lets an operator tighten or extend it.
RUN_TIMEOUT_SECONDS = int(os.environ.get("MUIOGO_OGC_RUN_TIMEOUT_SECONDS", "") or 6 * 3600)

# Optional inactivity ceiling for a run that has gone silent. Disabled by default:
# a solve can legitimately print nothing for a long stretch, so the wall-clock cap
# above is the always-on guard and this is opt-in for operators who want a tighter
# one. A bad or negative value falls back to disabled rather than killing every run.
try:
    RUN_INACTIVITY_TIMEOUT_SECONDS = float(
        os.environ.get("MUIOGO_OGC_RUN_INACTIVITY_TIMEOUT", "").strip() or 0
    )
except ValueError:
    RUN_INACTIVITY_TIMEOUT_SECONDS = 0.0
if RUN_INACTIVITY_TIMEOUT_SECONDS < 0:
    RUN_INACTIVITY_TIMEOUT_SECONDS = 0.0

# The worker script is a sibling of this module; resolve it absolutely so the spawn
# does not depend on the current working directory.
WORKER_PATH = Path(__file__).parent / "ogc_worker.py"

# OG-Core's steady-state loop logs "Iteration: {n}  Distance: {d}" and TPI logs
# "Iteration: {n}"; we pull just the number for a display-only progress hint.
_ITERATION_RE = re.compile(r"Iteration:\s*(\d+)")


def _process_cmdline(pid) -> str | None:
    """Best-effort command line of a running pid, or None if it cannot be read."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        if Config.SYSTEM == "Windows":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=20,
            )
        else:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=20,
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _names_run_dir(cmdline: str, run_dir) -> bool:
    """True when cmdline names exactly this run directory.

    A plain substring test would also match a directory this one is a prefix of, so
    "…/res/base" would claim a worker running "…/res/base2". The match only counts
    where the path ends at a quote, a space, or the end of the line.
    """
    target = str(run_dir)
    start = 0
    while True:
        found = cmdline.find(target, start)
        if found == -1:
            return False
        end = found + len(target)
        if end >= len(cmdline) or cmdline[end] in "\"' \t":
            return True
        start = found + 1


def kill_worker_tree(pid, run_dir) -> bool:
    """Kill a leftover worker's process tree, but only if the pid is still that run's.

    A pid recorded before a crash may since have been reused by something unrelated,
    so this refuses unless the live command line is a worker started for this very
    run directory. Returns True if a kill was issued. Never raises.
    """
    if not pid:
        return False
    cmdline = _process_cmdline(pid)
    if not cmdline or WORKER_PATH.name not in cmdline:
        return False
    if not _names_run_dir(cmdline, run_dir):
        return False
    try:
        if Config.SYSTEM == "Windows":
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True,
            )
        else:
            os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
    except (OSError, ValueError, ProcessLookupError, subprocess.SubprocessError):
        return False
    return True


class OGRunner:
    """Owns one worker subprocess and its output pump."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.iteration: int | None = None
        self.timed_out = False   # hit the wall-clock ceiling
        self.stalled = False     # hit the inactivity ceiling
        self._tail: collections.deque = collections.deque(maxlen=200)
        self._reader: threading.Thread | None = None
        self._last_activity = 0.0  # monotonic time of the last line seen

    # ── lifecycle ──────────────────────────────────────────────────────────
    def spawn(self, python_path, run_dir) -> None:
        """Start the worker under `python_path` and begin draining its output.

        On POSIX the child is put in its own session so kill_tree can signal the
        whole process group (the solve fans out into Dask/distributed children).
        """
        cmd = [
            str(python_path),
            # -u matters: writing to a pipe, Python block-buffers stdout, so a solve's
            # progress would sit in the child's buffer until it exited and the live log
            # and iteration count would stay empty for the whole run.
            "-u",
            str(WORKER_PATH),
            "run",
            "--run-dir",
            str(run_dir),
        ]
        popen_kwargs = dict(
            env=Config.ogc_clean_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,  # read side unbuffered; -u above is what unbuffers the child
        )
        if Config.SYSTEM != "Windows":
            popen_kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(cmd, **popen_kwargs)

        self._last_activity = time.monotonic()  # start the inactivity clock
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
        self._last_activity = time.monotonic()  # output seen: reset the clock
        m = _ITERATION_RE.search(segment)
        if m:
            self.iteration = int(m.group(1))

    def supervise(self) -> int:
        """Wait for the run under its deadlines; return the exit code.

        Polls the reader (which only ends when the child closes its pipe) in short
        slices, so both ceilings can be enforced: the wall clock, and the optional
        inactivity one. Hitting either kills the presumed-hung child and returns 124,
        with timed_out or stalled set so the caller can word the failure. Otherwise
        the child has closed stdout and should be exiting, so wait briefly and fall
        back to a kill if it does not.
        """
        wall_deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
        inactivity = RUN_INACTIVITY_TIMEOUT_SECONDS

        while self._reader.is_alive():
            self._reader.join(timeout=1.0)
            if not self._reader.is_alive():
                break
            now = time.monotonic()
            if now >= wall_deadline:
                self.timed_out = True
            elif inactivity and (now - self._last_activity) > inactivity:
                self.stalled = True
            else:
                continue
            self.kill_tree()
            self._reader.join(timeout=5)
            try:
                self.proc.wait(timeout=5)  # reap, or it lingers as a zombie on POSIX
            except (subprocess.SubprocessError, OSError):
                pass
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
                capture_output=True, timeout=30,
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
    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

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
