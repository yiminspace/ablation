import json
import os
from pathlib import Path
import sys
import subprocess
import signal
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ablation_workbench.runner import AgentRunner, ACTIVE, _write


class WorkbenchStub:
    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.reviews = [{"id": "r1", "intent": "comment", "comments": [{"text": "Why?"}], "replies": []},
                        {"id": "r2", "intent": "changes", "replies": []}]

    def get(self, experiment_id):
        return {"id": experiment_id, "reviews": self.reviews, "candidates": [], "runs": []}

    def review_text(self, experiment_id, review_id):
        return f"Review {review_id} in {experiment_id}: explain why this change was made."

    def reply(self, experiment_id, review_id, *, text, author, comment_index=None):
        review = next(r for r in self.reviews if r["id"] == review_id)
        review["replies"].append({"text": text, "author": author, "comment_index": comment_index})
        return review


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.workbench = WorkbenchStub(self.workspace)
        self.runner = AgentRunner(self.workbench)

    def configure(self, code, timeout=5):
        (self.workspace / "config.json").write_text(json.dumps({"agent": {
            "backend": "command", "command": [sys.executable, "-c", code], "timeout_seconds": timeout}}))

    def wait(self, job):
        for _ in range(100):
            result = self.runner.status(job["id"])
            if result["status"] not in ACTIVE:
                return result
            time.sleep(0.05)
        self.fail("Agent did not terminate")

    def test_comment_round_trip_persists_reply_and_idempotency(self):
        self.configure("import sys,os; p=sys.stdin.read(); print(os.environ['ABLATION_AGENT_INTENT']+': '+p)")
        job = self.runner.start("exp1", "r1")
        again = self.runner.start("exp1", "r1")
        self.assertEqual(job["id"], again["id"])
        result = self.wait(job)
        self.assertEqual("completed", result["status"])
        reply = self.workbench.reviews[0]["replies"][0]["text"]
        self.assertIn("comment: Review r1", reply)
        self.assertIn("Explanation only", reply)
        self.assertEqual(1, len(self.workbench.reviews[0]["replies"]))
        self.assertEqual(0, self.workbench.reviews[0]["replies"][0]["comment_index"])
        self.assertEqual(job["id"], AgentRunner(self.workbench).start("exp1", "r1")["id"])

    def test_serializes_same_experiment_and_cancels(self):
        self.configure("import time; time.sleep(20)")
        job = self.runner.start("exp1", "r1")
        with self.assertRaisesRegex(ValueError, "active job"):
            self.runner.start("exp1", "r2")
        self.runner.cancel(job["id"])
        self.assertEqual("cancelled", self.wait(job)["status"])
        self.assertFalse(self.workbench.reviews[0]["replies"])

    def test_timeout_does_not_claim_success(self):
        self.configure("import time; time.sleep(20)", timeout=1)
        result = self.wait(self.runner.start("exp1", "r1"))
        self.assertEqual("timed_out", result["status"])
        self.assertFalse(self.workbench.reviews[0]["replies"])

    def test_failed_exit_and_empty_response(self):
        self.configure("import sys; print('partial output'); sys.exit(3)")
        result = self.wait(self.runner.start("exp1", "r1"))
        self.assertEqual("failed", result["status"])
        self.assertEqual(3, result["exit_code"])
        self.configure("pass")
        result = self.wait(self.runner.start("exp1", "r2"))
        self.assertEqual("failed", result["status"])
        self.assertIn("without a reply", result["error"])

    def test_codex_resume_uses_explicit_thread_and_intent_isolation(self):
        prior = {"schema_version": 1, "id": "agent_old", "kind": "agent", "experiment_id": "exp1",
                 "review_id": "r0", "status": "completed", "intent": "comment", "backend": "codex",
                 "created_at": "2026-01-01", "thread_id": "thread-explicit", "cwd": str(self.workspace)}
        _write(self.workspace / "jobs/agent_old.json", prior)
        config = {"backend": "codex", "cwd": str(self.workspace)}
        command = self.runner._command({"experiment_id": "exp1", "intent": "comment"}, config, self.workspace / "reply")
        self.assertIn("resume", command)
        self.assertIn("thread-explicit", command)
        self.assertNotIn("--last", command)
        self.assertEqual("read-only", command[command.index("--sandbox") + 1])
        self.assertFalse(any("network_access=true" in part or "writable_roots" in part for part in command))
        command = self.runner._command({"experiment_id": "exp1", "intent": "changes"}, config, self.workspace / "reply")
        self.assertNotIn("resume", command)

    def test_changes_permissions_include_external_workspace_for_new_and_resumed_sessions(self):
        project = self.workspace / "business-project"
        project.mkdir()
        config = {"backend": "codex", "cwd": str(project)}
        job = {"experiment_id": "exp1", "intent": "changes"}
        for resumed in (False, True):
            if resumed:
                _write(self.workspace / "jobs/agent_previous.json", {
                    "id": "agent_previous", "kind": "agent", "experiment_id": "exp1", "review_id": "previous",
                    "status": "completed", "intent": "changes", "backend": "codex", "created_at": "2026-01-01",
                    "thread_id": "changes-thread", "cwd": str(project)})
            command = self.runner._command(job, config, self.workspace / "reply")
            self.assertEqual("workspace-write", command[command.index("--sandbox") + 1])
            self.assertIn("sandbox_workspace_write.network_access=true", command)
            roots = next(part.partition("=")[2] for part in command if part.startswith("sandbox_workspace_write.writable_roots="))
            self.assertEqual([str(self.workspace.resolve())], json.loads(roots))
            self.assertNotIn("danger-full-access", command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.assertEqual(resumed, "resume" in command)
            if resumed:
                self.assertIn("changes-thread", command)
            else:
                self.assertEqual(str(project), command[command.index("-C") + 1])

    def test_codex_events_persist_session_and_failed_turn_is_not_a_reply(self):
        (self.workspace / "config.json").write_text(json.dumps({"agent": {"backend": "codex", "timeout_seconds": 5}}))
        real_popen = subprocess.Popen
        events = [{"type": "thread.started", "thread_id": "verified-thread"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "The prompt now requires evidence."}},
                  {"type": "turn.completed"}]
        def launch_stub(command, **kwargs):
            code = "import sys; sys.stdin.read(); print(" + repr("\n".join(json.dumps(e) for e in events)) + ")"
            return real_popen([sys.executable, "-c", code], **kwargs)
        with patch("ablation_workbench.runner.subprocess.Popen", side_effect=launch_stub):
            completed = self.wait(self.runner.start("exp1", "r1"))
            self.assertEqual("completed", completed["status"])
            self.assertEqual("verified-thread", completed["thread_id"])
            events[-1] = {"type": "turn.failed", "error": {"message": "fixture failure"}}
            failed = self.wait(self.runner.start("exp1", "r2"))
        self.assertEqual("failed", failed["status"])
        self.assertFalse(self.workbench.reviews[1]["replies"])

    def test_interrupted_owner_is_recovered_and_replay_jobs_excluded(self):
        _write(self.workspace / "jobs/agent_old.json", {"id": "agent_old", "kind": "agent", "experiment_id": "exp1",
               "created_at": "2026", "status": "running", "owner_pid": 12345})
        _write(self.workspace / "jobs/replay_old.json", {"id": "replay_old", "kind": "replay", "status": "running"})
        with patch("ablation_workbench.runner._alive", return_value=False):
            jobs = self.runner.list_jobs()
        self.assertEqual(1, len(jobs))
        self.assertEqual("interrupted", jobs[0]["status"])

    def test_rejects_shell_command_and_upgrade_race(self):
        (self.workspace / "config.json").write_text(json.dumps({"agent": {"backend": "command", "command": "echo hi"}}))
        with self.assertRaisesRegex(ValueError, "argv"):
            self.runner.start("exp1", "r1")
        self.configure("print('ok')")
        (self.workspace / ".upgrade-running").write_text("{}")
        with self.assertRaisesRegex(ValueError, "upgrade"):
            self.runner.start("exp1", "r1")

    @unittest.skipUnless(os.name == "posix", "Independent POSIX process groups")
    def test_stop_cleans_term_ignoring_children_even_after_leader_exit(self):
        child_code = ("import os,signal,time,pathlib,sys; "
                      "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                      "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)")
        parent_code = ("import subprocess,sys,time,pathlib; "
                       "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); "
                       "marker=pathlib.Path(sys.argv[2]); "
                       "\nwhile not marker.exists(): time.sleep(.01)"
                       "\nif sys.argv[3]=='wait': time.sleep(60)")
        for leader_exits in (False, True):
            with self.subTest(leader_exits=leader_exits):
                marker = self.workspace / f"child-{leader_exits}.pid"
                process = subprocess.Popen([sys.executable, "-c", parent_code, child_code, str(marker),
                                            "exit" if leader_exits else "wait"], start_new_session=True)
                try:
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    child_pid = int(marker.read_text())
                    if leader_exits:
                        process.wait(timeout=5)
                    started = time.monotonic()
                    with patch("ablation_workbench.runner.STOP_GRACE_SECONDS", 0.15):
                        self.runner._stop(process, process_group=process.pid)
                    self.assertLess(time.monotonic() - started, 2)
                    state = ""
                    for _ in range(20):
                        state = subprocess.run(["ps", "-o", "stat=", "-p", str(child_pid)],
                                               capture_output=True, text=True, timeout=2).stdout.strip()
                        if not state or state.startswith("Z"):
                            break
                        time.sleep(.02)
                    self.assertTrue(not state or state.startswith("Z"), state)
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)

    @unittest.skipUnless(os.name == "posix", "Independent POSIX process groups")
    def test_stop_reaps_and_retries_transient_group_permission_error(self):
        process = Mock(pid=12345)
        process.poll.side_effect = [None, None, -signal.SIGTERM, -signal.SIGTERM]
        with patch("ablation_workbench.runner.os.getpgid", return_value=process.pid), \
                patch("ablation_workbench.runner.os.killpg", side_effect=[None, PermissionError(), ProcessLookupError()]) as killpg:
            self.runner._stop(process, process_group=process.pid)
        self.assertEqual([signal.SIGTERM, 0, 0], [call.args[1] for call in killpg.call_args_list])
        self.assertEqual(4, process.poll.call_count)
        process.poll.side_effect = None
        process.poll.return_value = -signal.SIGTERM
        with patch("ablation_workbench.runner.os.killpg", side_effect=PermissionError()) as killpg:
            with self.assertRaises(PermissionError):
                self.runner._stop(process, process_group=process.pid)
        self.assertEqual(3, killpg.call_count)

    @unittest.skipUnless(os.name == "posix", "Independent POSIX process groups")
    def test_stop_rejects_the_callers_process_group(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with self.assertRaisesRegex(ValueError, "unowned"):
                self.runner._stop(process, process_group=os.getpgrp())
            self.assertIsNone(process.poll())
        finally:
            process.kill()
            process.wait(timeout=3)

    @unittest.skipUnless(os.name == "posix", "Independent POSIX process groups")
    def test_cli_dispatch_cleans_children_before_publishing_success_or_failure(self):
        from ablation_workbench.engine import Workbench
        workbench = Workbench(self.workspace)
        experiment = workbench.import_source({"kind": "request", "data": {
            "model": "fixture", "messages": [{"role": "user", "content": "hello"}]}})
        child_code = ("import os,signal,time,pathlib,sys; "
                      "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                      "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)")
        for parent_exit, expected_status in ((0, "completed"), (3, "failed")):
            with self.subTest(parent_exit=parent_exit):
                marker = self.workspace / f"cli-child-{parent_exit}.pid"
                parent_code = ("import subprocess,sys,time,pathlib; sys.stdin.read(); "
                               f"subprocess.Popen([sys.executable,'-c',{child_code!r},{str(marker)!r}]); "
                               f"marker=pathlib.Path({str(marker)!r}); "
                               "\nwhile not marker.exists(): time.sleep(.01)"
                               f"\nprint('Fixture explanation',flush=True); sys.exit({parent_exit})")
                self.configure(parent_code)
                review = workbench.submit_review(experiment["id"], {
                    "id": f"review_cli_{parent_exit}", "intent": "comment", "summary": "Explain the fixture"})
                command = [sys.executable, "-m", "ablation_workbench", "--workspace", str(self.workspace),
                           "review", "dispatch", experiment["id"], review["id"]]
                environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
                try:
                    result = subprocess.run(command, cwd=self.workspace, env=environment,
                                            capture_output=True, text=True, timeout=12)
                    self.assertEqual(0 if parent_exit == 0 else 4, result.returncode, result.stderr)
                    self.assertEqual(expected_status, json.loads(result.stdout)["status"])
                    child_pid = int(marker.read_text())
                    state = subprocess.run(["ps", "-o", "stat=", "-p", str(child_pid)],
                                           capture_output=True, text=True, timeout=2).stdout.strip()
                    self.assertTrue(not state or state.startswith("Z"), state)
                finally:
                    if marker.exists():
                        try:
                            os.kill(int(marker.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
