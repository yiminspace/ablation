"""Persisted review delivery to a configured local agent."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from uuid import uuid4

from .installation import installation_activity


ACTIVE = {"queued", "running", "cancelling"}
STOP_GRACE_SECONDS = 3
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_LOCK = threading.Lock()
_LOCK_DEPTH = threading.local()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, value: dict):
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


@contextmanager
def workspace_lock(workspace):
    directory = Path(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    with _LOCKS_LOCK:
        lock = _LOCAL_LOCKS.setdefault(str(directory.resolve()), threading.RLock())
    key = str(directory.resolve())
    with lock:
        depths = getattr(_LOCK_DEPTH, "depths", {})
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        depths[key] = 1
        _LOCK_DEPTH.depths = depths
        try:
            with _file_lock(directory):
                yield
        finally:
            depths[key] = 0


@contextmanager
def _file_lock(directory):
    with (directory / ".activity.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


class AgentRunner:
    def __init__(self, workbench):
        self.workbench = workbench
        self.workspace = Path(workbench.workspace).resolve()
        self.directory = self.workspace / "jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._processes: dict[str, subprocess.Popen] = {}

    def _path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id):
            raise ValueError("Invalid job ID")
        return self.directory / f"{job_id}.json"

    def _configuration(self):
        path = self.workspace / "config.json"
        configuration = json.loads(path.read_text()) if path.exists() else {}
        agent = configuration.get("agent", {})
        if agent.get("backend") not in {"codex", "command"}:
            raise ValueError("Configure agent.backend as codex or command before dispatching")
        command = agent.get("command")
        if agent["backend"] == "command" and (
            not isinstance(command, list) or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise ValueError("agent.command must be a nonempty argv array; shell strings are unsupported")
        timeout = agent.get("timeout_seconds", 900)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 7200:
            raise ValueError("agent.timeout_seconds must be between 1 and 7200")
        cwd = Path(agent.get("cwd") or self.workspace).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError("agent.cwd must be an existing directory")
        return {**agent, "cwd": str(cwd), "timeout_seconds": timeout}

    def list_jobs(self, experiment_id=None):
        jobs = []
        with workspace_lock(self.workspace):
            for path in self.directory.glob("*.json"):
                job = json.loads(path.read_text())
                if job.get("kind") != "agent":
                    continue
                if job.get("status") in ACTIVE and not _alive(job.get("owner_pid")):
                    job.update(status="interrupted", finished_at=_now(),
                               error="Agent host exited; execution outcome is unknown. Review artifacts before retrying.")
                    _write(path, job)
                if experiment_id is None or job["experiment_id"] == experiment_id:
                    jobs.append(job)
        return sorted(jobs, key=lambda job: job["created_at"])

    def status(self, job_id):
        path = self._path(job_id)
        if not path.exists():
            raise ValueError("Agent job not found")
        job = json.loads(path.read_text())
        if job.get("kind") != "agent":
            raise ValueError("Not an agent job")
        if job.get("status") in ACTIVE and not _alive(job.get("owner_pid")):
            self.list_jobs()
            job = json.loads(path.read_text())
        return job

    def start(self, experiment_id, review_id):
        lease = installation_activity()
        transferred = False
        try:
            job, transferred = self._start(experiment_id, review_id, lease)
            return job
        finally:
            if not transferred:
                lease.close()

    def _start(self, experiment_id, review_id, lease):
        experiment = self.workbench.get(experiment_id)
        review = next((r for r in experiment["reviews"] if r["id"] == review_id), None)
        if review is None:
            raise ValueError("Review not found")
        self.list_jobs()
        with workspace_lock(self.workspace):
            jobs = [json.loads(path.read_text()) for path in self.directory.glob("*.json")]
            for job in jobs:
                if job.get("kind") == "agent" and job.get("experiment_id") == experiment_id and job.get("review_id") == review_id:
                    return job, False
            configuration = self._configuration()
            if (self.workspace / ".upgrade-running").exists():
                raise ValueError("An upgrade is running; restart the GUI after it finishes")
            if any(job.get("experiment_id") == experiment_id and job.get("status") in ACTIVE for job in jobs):
                raise ValueError("This experiment already has an active job")
            job_id = "agent_" + uuid4().hex[:16]
            job = {"schema_version": 1, "id": job_id, "kind": "agent", "experiment_id": experiment_id,
                   "review_id": review_id, "intent": review["intent"], "backend": configuration["backend"],
                   "status": "queued", "created_at": _now(), "owner_pid": os.getpid(),
                   "cancel_requested": False, "process_outcome": None}
            _write(self._path(job_id), job)
            threading.Thread(target=self._execute, args=(job_id, configuration, lease), daemon=True,
                             name=f"ablation-{job_id}").start()
            return job, True

    def cancel(self, job_id):
        with workspace_lock(self.workspace):
            job = self.status(job_id)
            if job["status"] in ACTIVE:
                job.update(cancel_requested=True, status="cancelling")
                _write(self._path(job_id), job)
            return job

    def _update(self, job_id, **fields):
        with workspace_lock(self.workspace):
            job = self.status(job_id)
            job.update(fields)
            _write(self._path(job_id), job)
            return job

    def _command(self, job, config, output_path):
        if config["backend"] == "command":
            return config["command"][:]
        prior = [j for j in self.list_jobs(job["experiment_id"])
                 if j.get("thread_id") and j.get("intent") == job["intent"] and j.get("backend") == "codex"
                 and j.get("cwd") == config["cwd"] and j.get("status") == "completed"]
        sandbox = "read-only" if job["intent"] == "comment" else "workspace-write"
        command = ["codex", "exec", "--sandbox", sandbox,
                   "-c", 'approval_policy="never"']
        if job["intent"] == "changes":
            roots = json.dumps([str(self.workspace)], ensure_ascii=False)
            command += ["-c", f"sandbox_workspace_write.writable_roots={roots}",
                        "-c", "sandbox_workspace_write.network_access=true"]
        if prior:
            command += ["resume", "--json", "--skip-git-repo-check", "-o", str(output_path), prior[-1]["thread_id"], "-"]
        else:
            command += ["--json", "--skip-git-repo-check", "-C", config["cwd"], "-o", str(output_path), "-"]
        return command

    @staticmethod
    def _stop(process, *, process_group=None):
        if os.name == "posix" and process_group is not None:
            if process_group != process.pid or process_group <= 0 or process_group == os.getpgrp():
                raise ValueError("Refusing to stop an unowned process group")
            if process.poll() is None:
                try:
                    if os.getpgid(process.pid) != process_group:
                        raise ValueError("Agent is no longer in its owned process group")
                except ProcessLookupError:
                    pass

            def signal_group(sig):
                for attempt in range(3):
                    try:
                        os.killpg(process_group, sig)
                        return True
                    except ProcessLookupError:
                        return False
                    except PermissionError:
                        # macOS may report EPERM while the group leader awaits reaping.
                        process.poll()
                        if attempt == 2:
                            raise
                        time.sleep(0.01)

            if signal_group(signal.SIGTERM):
                deadline = time.monotonic() + STOP_GRACE_SECONDS
                remaining = True
                while time.monotonic() < deadline:
                    process.poll()
                    if not signal_group(0):
                        remaining = False
                        break
                    time.sleep(0.05)
                # Group members may outlive the leader; zombies need no further wait.
                if remaining:
                    signal_group(signal.SIGKILL)
            if process.poll() is None:
                process.wait(timeout=3)
            return
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        except ProcessLookupError:
            pass

    def _execute(self, job_id, configuration, lease):
        output_path = self.directory / f"{job_id}.reply.txt"
        stdout_path = self.directory / f"{job_id}.stdout.log"
        stderr_path = self.directory / f"{job_id}.stderr.log"
        process_stopped = False
        final_fields = {"status": "failed", "error": "Agent worker exited before confirming an outcome"}
        try:
            job = self.status(job_id)
            prompt = self.workbench.review_text(job["experiment_id"], job["review_id"])
            prompt += ("\nReturn your answer as the final message. The runner appends it to the review; "
                       "do not call the reply command yourself. Do not change business repository files.\n")
            if job["intent"] == "comment":
                prompt += "Explanation only: do not mutate experiments, add candidates, call model endpoints, or replay.\n"
            environment = os.environ.copy()
            environment.update(ABLATION_AGENT_INTENT=job["intent"], ABLATION_WORKSPACE=str(self.workspace),
                               ABLATION_EXPERIMENT_ID=job["experiment_id"], ABLATION_REVIEW_ID=job["review_id"],
                               ABLATION_AGENT_JOB_ID=job_id, ABLATION_UPDATE_CHECK="0")
            command = self._command(job, configuration, output_path)
            if self.status(job_id).get("cancel_requested"):
                final_fields = {"status": "cancelled"}
                return
            self._update(job_id, status="running", started_at=_now(), cwd=configuration["cwd"])
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                stdout_path.chmod(0o600)
                stderr_path.chmod(0o600)
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                           cwd=configuration["cwd"], env=environment, start_new_session=os.name == "posix")
                self._processes[job_id] = process
                self._update(job_id, process_pid=process.pid)
                writer_error = []
                def write_prompt():
                    try:
                        process.stdin.write(prompt.encode("utf-8"))
                        process.stdin.close()
                    except (BrokenPipeError, OSError) as error:
                        writer_error.append(str(error))
                threading.Thread(target=write_prompt, daemon=True).start()
                started = time.monotonic()
                terminal = None
                while process.poll() is None:
                    if self.status(job_id).get("cancel_requested"):
                        terminal = "cancelled"
                    elif time.monotonic() - started > configuration["timeout_seconds"]:
                        terminal = "timed_out"
                    if terminal:
                        self._stop(process, process_group=process.pid if os.name == "posix" else None)
                        process_stopped = True
                        break
                    time.sleep(0.1)
            fields = {"finished_at": _now(), "exit_code": process.returncode,
                      "process_outcome": "exited", "stdout_path": str(stdout_path), "stderr_path": str(stderr_path)}
            text = stdout_path.read_text(encoding="utf-8", errors="replace")
            if configuration["backend"] == "codex":
                messages = []
                failed_event = None
                for line in text.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "thread.started" and event.get("thread_id"):
                        fields["thread_id"] = event["thread_id"]
                    if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                        messages.append(event["item"].get("text", ""))
                    if event.get("type") in {"turn.failed", "error"}:
                        failed_event = "Agent reported a failed turn; inspect its local log"
                reply = output_path.read_text(encoding="utf-8") if output_path.exists() else "\n\n".join(messages)
            else:
                reply = text
                failed_event = None
            if terminal:
                fields.update(status=terminal, error="Agent cancelled" if terminal == "cancelled" else "Agent exceeded configured timeout")
            elif process.returncode != 0 or failed_event:
                fields.update(status="failed", error=failed_event or f"Agent exited with status {process.returncode}; inspect its local log")
            elif not reply.strip():
                fields.update(status="failed", error="Agent exited without a reply; no result was confirmed")
            else:
                reviewed = next(review for review in self.workbench.get(job["experiment_id"])["reviews"]
                                if review["id"] == job["review_id"])
                comment_index = 0 if len(reviewed.get("comments", [])) == 1 else None
                self.workbench.reply(job["experiment_id"], job["review_id"], text=reply.strip(),
                                     comment_index=comment_index, author="agent")
                fields.update(status="completed", reply_saved=True,
                              result_note="Agent replied. Inspect experiment artifacts to verify any claimed edits or replays.")
            final_fields = fields
        except Exception as error:
            final_fields = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
        finally:
            try:
                process = self._processes.pop(job_id, None)
                if process is not None and not process_stopped:
                    self._stop(process, process_group=process.pid if os.name == "posix" else None)
            except Exception as error:
                final_fields.update(status="failed", error=f"Agent cleanup failed: {type(error).__name__}: {error}")
            finally:
                try:
                    final_fields["finished_at"] = _now()
                    self._update(job_id, **final_fields)
                finally:
                    lease.close()
