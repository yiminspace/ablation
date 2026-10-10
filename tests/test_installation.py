import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ablation_workbench import installation, updates
from ablation_workbench.engine import Workbench
from ablation_workbench.runner import ACTIVE, AgentRunner


@unittest.skipUnless(os.name == "posix", "macOS/Linux installation lock")
class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        self.cache.start()
        self.addCleanup(self.cache.stop)
        self.a = self.root / "workspace-a"
        self.b = self.root / "workspace-b"
        self.a.mkdir()
        self.b.mkdir()
        self.child_environment = {**os.environ, "PYTHONPATH": str(Path(installation.__file__).resolve().parents[1])}

    def child(self, source, *, workspace=None):
        return subprocess.Popen([sys.executable, "-c", source], cwd=workspace or self.b,
                                env=self.child_environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def test_shared_agent_and_child_replay_coexist_and_block_upgrade(self):
        with installation.installation_activity():
            process = self.child("from ablation_workbench.installation import installation_activity; import sys\n"
                                 "with installation_activity():\n print('shared',flush=True); sys.stdin.readline()")
            try:
                self.assertEqual("shared", process.stdout.readline().strip())
                with self.assertRaisesRegex(RuntimeError, "active jobs"):
                    installation.installation_upgrade()
                process.communicate("finish\n", timeout=5)
                self.assertEqual(0, process.returncode)
                with self.assertRaisesRegex(RuntimeError, "active jobs"):
                    installation.installation_upgrade()
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
        with installation.installation_upgrade():
            pass

    def test_upgrade_blocks_new_activity_in_a_second_process(self):
        source = "from ablation_workbench.installation import installation_activity\nwith installation_activity(): print('started')"
        with installation.installation_upgrade():
            process = self.child(source)
            stdout, stderr = process.communicate(timeout=5)
            self.assertNotEqual(0, process.returncode)
            self.assertEqual("", stdout)
            self.assertIn("being upgraded", stderr)
        process = self.child(source)
        stdout, _ = process.communicate(timeout=5)
        self.assertEqual(0, process.returncode)
        self.assertEqual("started", stdout.strip())

    def test_process_exit_releases_shared_lock_without_workspace_scanning(self):
        process = self.child("from ablation_workbench.installation import installation_activity; import time\n"
                             "lease=installation_activity(); print('shared',flush=True); time.sleep(60)")
        try:
            self.assertEqual("shared", process.stdout.readline().strip())
            with self.assertRaises(RuntimeError):
                installation.installation_upgrade()
            process.kill()
            process.communicate(timeout=5)
            with installation.installation_upgrade():
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_other_workspace_agent_blocks_upgrade_until_cancelled(self):
        wb = Workbench(self.a)
        experiment = wb.import_source({"kind": "request", "data": {
            "model": "fixture", "messages": [{"role": "user", "content": "hello"}]}})
        review = wb.submit_review(experiment["id"], {"id": "review_fixture", "intent": "comment", "summary": "Explain"})
        (self.a / "config.json").write_text(json.dumps({"agent": {"backend": "command", "command": [
            sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(60)"]}}))
        runner = AgentRunner(wb)
        job = runner.start(experiment["id"], review["id"])
        try:
            with patch.object(updates.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "active jobs"):
                    updates.perform_upgrade(self.b)
                run.assert_not_called()
        finally:
            runner.cancel(job["id"])
            for _ in range(100):
                if runner.status(job["id"])["status"] not in ACTIVE:
                    break
                time.sleep(0.05)
        self.assertEqual("cancelled", runner.status(job["id"])["status"])
        with installation.installation_upgrade():
            pass

    def test_validation_failure_and_duplicate_review_do_not_leak_activity(self):
        wb = Workbench(self.a)
        runner = AgentRunner(wb)
        with self.assertRaises(FileNotFoundError):
            runner.start("missing", "missing")
        with installation.installation_upgrade():
            pass
        experiment = wb.import_source({"kind": "request", "data": {
            "model": "fixture", "messages": [{"role": "user", "content": "hello"}]}})
        review = wb.submit_review(experiment["id"], {"id": "review_finished", "intent": "comment", "summary": "Explain"})
        (runner.directory / "agent_finished.json").write_text(json.dumps({
            "id": "agent_finished", "kind": "agent", "experiment_id": experiment["id"], "review_id": review["id"],
            "status": "completed", "created_at": "2026-01-01"}))
        self.assertEqual("agent_finished", runner.start(experiment["id"], review["id"])["id"])
        with installation.installation_upgrade():
            pass


if __name__ == "__main__":
    unittest.main()
