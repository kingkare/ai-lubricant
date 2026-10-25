"""Background job registry (dsh ``jobs`` + ``tool-bash`` background, ported).

``code_run`` waits for its subprocess — a dev server or a multi-minute migration
blocks the turn and eventually dies on the timeout. With ``background=true`` the
spawn returns a job id immediately and the model manages the process through the
``job`` tool (``output`` / ``list`` / ``kill``) in later turns of the same run.

Lifecycle is scoped to one agent run, matching the ToolRegistry's own lifetime
(a fresh registry is built per message): pipes are drained by asyncio reader
tasks into capped buffers, reads are incremental via a per-job cursor, and
``close_all`` (wired into ``agent_runner_loop``'s teardown) terminates whatever
is still running so a background process can never silently outlive its run.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

# Per-stream buffer cap. Beyond it reading stops and a drop marker is appended;
# the model is expected to poll incrementally rather than let a chatty server
# fill the cap between reads.
_MAX_BUF_BYTES = 512 * 1024
_DROP_MARKER = "\n...[output truncated: buffer cap reached; {dropped} bytes dropped]...\n"
# Grace period between terminate() and the kill() ladder in close_all.
_KILL_GRACE_SECONDS = 5.0


class Job:
    """One background subprocess plus its drained output buffers."""

    def __init__(self, job_id: str, cmd: list[str], proc: asyncio.subprocess.Process) -> None:
        self.job_id = job_id
        self.cmd = cmd
        self.proc = proc
        self.started_at = time.time()
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.stdout_pos = 0  # incremental-read cursors
        self.stderr_pos = 0
        self.dropped = {"stdout": 0, "stderr": 0}
        self.exit_code: int | None = None
        # Reader tasks for stdout/stderr; _reap awaits them so that "not running"
        # implies "all output is buffered" — a read after exit is then complete.
        self.readers: list[asyncio.Task] = []

    @property
    def running(self) -> bool:
        return self.proc.returncode is None and self.exit_code is None

    def summary(self) -> dict[str, Any]:
        return {
            # Keyed process_id (not job_id): the scheduler's capability_call uses
            # job_id for scheduled tasks, and the two must never be confused.
            "process_id": self.job_id,
            "cmd": " ".join(self.cmd)[:200],
            "running": self.running,
            "exit_code": self.exit_code,
            "stdout_bytes": len(self.stdout),
            "stderr_bytes": len(self.stderr),
            "elapsed_s": int(time.time() - self.started_at),
        }

    def _append(self, stream: str, chunk: bytes) -> None:
        buf = self.stdout if stream == "stdout" else self.stderr
        if len(buf) + len(chunk) > _MAX_BUF_BYTES:
            self.dropped[stream] += len(chunk)
            marker = _DROP_MARKER.format(dropped=self.dropped[stream]).encode("utf-8")
            room = _MAX_BUF_BYTES - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
            # Replace the tail with the marker so it is always visible.
            del buf[max(0, len(buf) - len(marker)):]
            buf.extend(marker)
            return
        buf.extend(chunk)


class JobRegistry:
    """Owns the background jobs of one agent run."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    async def start(self, cmd: list[str], *, cwd: str) -> Job:
        """Spawn ``cmd`` with drained pipes; return the registered job."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        job = Job(f"job_{uuid.uuid4().hex[:8]}", cmd, proc)
        self._jobs[job.job_id] = job
        job.readers.append(asyncio.create_task(self._drain(job, "stdout", proc.stdout)))
        job.readers.append(asyncio.create_task(self._drain(job, "stderr", proc.stderr)))
        asyncio.create_task(self._reap(job))
        return job

    async def _drain(self, job: Job, stream: str, reader: asyncio.StreamReader | None) -> None:
        if reader is None:
            return
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                job._append(stream, chunk)
        except Exception:  # noqa: BLE001 - a broken pipe must not crash the run
            pass

    async def _reap(self, job: Job) -> None:
        """Record the exit code once the process is gone AND output is drained.

        Awaiting the readers before setting ``exit_code`` makes "not running" imply
        "every byte is buffered", so a read issued after exit returns complete
        output instead of racing the reader tasks.
        """
        try:
            code = await job.proc.wait()
        except Exception:  # noqa: BLE001
            return
        if job.readers:
            await asyncio.gather(*job.readers, return_exceptions=True)
        job.exit_code = code

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> dict[str, Any]:
        jobs = [j.summary() for j in self._jobs.values()]
        return {"status": "ok", "jobs": jobs, "count": len(jobs)}

    def read(self, job_id: str, stream: str = "stdout") -> dict[str, Any]:
        """Return the not-yet-read chunk of one stream and advance the cursor."""
        job = self._jobs.get(job_id)
        if job is None:
            return {"status": "error", "msg": f"unknown process_id: {job_id}"}
        if stream not in ("stdout", "stderr"):
            return {"status": "error", "msg": f"stream must be stdout|stderr, got {stream}"}
        buf = job.stdout if stream == "stdout" else job.stderr
        pos = job.stdout_pos if stream == "stdout" else job.stderr_pos
        chunk = bytes(buf[pos:]).decode("utf-8", errors="replace")
        if stream == "stdout":
            job.stdout_pos = len(buf)
        else:
            job.stderr_pos = len(buf)
        out: dict[str, Any] = {
            "status": "ok",
            "process_id": job_id,
            "stream": stream,
            "running": job.running,
            "exit_code": job.exit_code,
            "new_output": chunk,
        }
        if not job.running:
            out["note"] = "process has exited; this stream is complete"
        return out

    async def kill(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return {"status": "error", "msg": f"unknown process_id: {job_id}"}
        if not job.running:
            return {"status": "ok", "process_id": job_id, "running": False,
                    "exit_code": job.exit_code, "msg": "already exited"}
        await self._terminate(job)
        return {"status": "ok", "process_id": job_id, "running": job.running,
                "exit_code": job.exit_code}

    async def _terminate(self, job: Job) -> None:
        """terminate → grace wait → kill ladder."""
        try:
            job.proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(job.proc.wait(), timeout=_KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            try:
                job.proc.kill()
            except ProcessLookupError:
                pass

    async def close_all(self) -> None:
        """Terminate every still-running job (run teardown)."""
        for job in list(self._jobs.values()):
            if job.running:
                await self._terminate(job)


__all__ = ["Job", "JobRegistry"]
