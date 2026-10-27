import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ablation_workbench.engine import Workbench
from ablation_workbench.installation import installation_upgrade
from ablation_workbench.runner import AgentRunner
from ablation_workbench.server import ACTIVE, close_jobs, make_server, save_json


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workbench = Workbench(self.temp.name)
        self.server = make_server(self.workbench)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        close_jobs(self.server)
        self.server.shutdown(); self.server.server_close(); self.temp.cleanup()

    def wait_job(self, job_id):
        for _ in range(100):
            jobs = self.server.replay_jobs.list() + self.server.agent_runner.list_jobs()
            job = next(job for job in jobs if job["id"] == job_id)
            if job["status"] not in ACTIVE: return job
            time.sleep(0.05)
        self.fail("Job did not finish")

    def request(self, path, body=None, headers=None):
        headers = {"Content-Type": "application/json", "X-Ablation-Client": "local", **(headers or {})}
        req = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urlopen(req) as res: return res.status, json.load(res)
        except HTTPError as exc: return exc.code, json.load(exc)

    def test_gui_serves_bundled_theme_without_external_asset_requests(self):
        for asset, content_type in (("favicon.svg", "image/svg+xml"), ("voyage.css", "text/css"), ("theme.js", "javascript"), ("voyage-toolbar.js", "javascript"), ("messages.js", "javascript"), ("messages.css", "text/css"), ("bifrost-LICENSE.txt", "text/plain"), ("bifrost-NOTICE.txt", "text/plain")):
            with self.subTest(asset=asset), urlopen(self.url + "/" + asset) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.headers["Content-Type"])
                self.assertGreater(len(response.read()), 100)
                self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])

    def test_demo_diff_review_reply_survives_service_restart(self):
        status, exp = self.request("/api/demo", {})
        self.assertEqual(status, 200)
        self.assertEqual(exp["runs"], [])
        eid = exp["id"]
        candidate = exp["candidates"][-1]["id"]
        status, diff = self.request(f"/api/experiments/{eid}/diff?candidate={candidate}&artifact=prompt")
        self.assertEqual(status, 200)
        row = next(r for r in diff["rows"] if r["kind"] != "equal")
        review = {"id": "review_test", "revision": exp["revision"], "base": "baseline", "candidate": candidate,
                  "scenario": "original", "intent": "comment", "summary": "Explain the authority rule.",
                  "comments": [{"artifact": "prompt", **{k: row[k] for k in ("old_line", "new_line", "before", "after")}, "text": "Why is this change needed?"}]}
        status, submitted = self.request(f"/api/experiments/{eid}/reviews", review)
        self.assertEqual(status, 200, submitted)
        status, same = self.request(f"/api/experiments/{eid}/reviews", review)
        self.assertEqual(status, 200, same)
        self.assertEqual(same["id"], submitted["id"])
        status, response = self.request(f"/api/experiments/{eid}/reviews/{submitted['id']}/replies", {"text": "The user's accepted format is the current requirement.", "comment_index": 0})
        self.assertEqual(status, 200, response)
        saved = Workbench(self.temp.name).get(eid)
        self.assertEqual(len(saved["reviews"]), 1)
        self.assertEqual(len(saved["reviews"][0]["replies"]), 1)
        self.assertEqual(saved["candidates"], exp["candidates"])
        self.assertEqual(saved["runs"], [])
        status, handoff = self.request(f"/api/experiments/{eid}/reviews/{submitted['id']}/text")
        self.assertEqual(status, 200)
        self.assertIn("Why is this change needed?", handoff["text"])

    def test_external_origin_and_missing_mutation_header_rejected(self):
        self.assertEqual(self.request("/api/demo", {}, {"Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.request("/api/demo", {}, {"X-Ablation-Client": ""})[0], 403)
        self.assertEqual(self.request("/api/experiments", headers={"Host": "other.example"})[0], 403)
        self.assertEqual(self.workbench.list_experiments(), [])

    def test_gui_cannot_import_arbitrary_files(self):
        status, result = self.request("/api/import", {"source": {"kind": "request", "path": "/etc/passwd"}})
        self.assertEqual(status, 400)
        self.assertIn("paths", result["error"]["message"])

    def test_gui_import_uses_selected_source_mapping_and_preserves_evidence(self):
        Path(self.temp.name, "config.json").write_text(json.dumps({"profiles": {"archive": {
            "source_request_pointer": "/payload/sent", "source_response_pointer": "/payload/received"
        }}}))
        request = {"model": "fixture", "input": "Example", "stream": False, "temperature": 0}
        raw = {"payload": {"sent": request, "received": {"output": []}}, "record": "sample-42"}
        status, exp = self.request("/api/import", {"source": {"kind": "log", "profile": "archive", "data": raw}})
        self.assertEqual(status, 200, exp)
        self.assertEqual(exp["source"]["kind"], "log")
        self.assertEqual(exp["source"]["fidelity"], "reconstructed")
        self.assertEqual(exp["source"]["evidence"], raw)
        self.assertEqual(exp["baseline"]["request"], request)
        self.assertEqual(exp["baseline"]["response"], {"output": []})
        _, config = self.request("/api/config")
        self.assertEqual(config["profiles"]["archive"]["source_request_pointer"], "/payload/sent")

    def test_export_import_keeps_evidence_and_mints_new_id(self):
        _, exp = self.request("/api/demo", {})
        _, bundle = self.request(f"/api/experiments/{exp['id']}/export")
        status, imported = self.request("/api/import-bundle", bundle)
        self.assertEqual(status, 200, imported)
        self.assertNotEqual(imported["id"], exp["id"])
        self.assertEqual(imported["baseline"], exp["baseline"])

    def test_config_exposes_only_environment_references(self):
        Path(self.temp.name, "config.json").write_text(json.dumps({"profiles": {"test": {"base_url": "http://localhost:1234", "api_key_env": "TEST_KEY", "api_key": "do-not-display"}}, "agent": {"backend": "command", "command": ["private"]}}))
        _, config = self.request("/api/config")
        self.assertNotIn("do-not-display", json.dumps(config))
        self.assertNotIn("command", config["agent"])
        self.assertEqual(config["agent"]["configured"], True)

    def test_replay_transport_failure_is_not_job_completion(self):
        _, exp = self.request("/api/demo", {})
        for run_status, expected in (("error", "failed"), ("completed", "completed")):
            result = {**exp, "runs": [{"id": "run_fixture", "status": run_status, "passed": False}]}
            with patch.object(self.workbench, "run", return_value=result):
                status, job = self.request(f"/api/experiments/{exp['id']}/run",
                                           {"candidates": ["baseline"], "scenarios": ["original"], "profile": "fixture"})
                self.assertEqual(status, 200, job)
                finished = self.wait_job(job["id"])
            self.assertEqual(finished["status"], expected)
            self.assertEqual(finished["run_ids"], ["run_fixture"])
            self.assertEqual(finished["summary"]["errors"], int(run_status == "error"))

    def test_closing_gui_preserves_external_jobs_and_cancels_owned_jobs(self):
        Path(self.temp.name, "config.json").write_text(json.dumps({"agent": {
            "backend": "command", "command": [sys.executable, "-c", "import time; time.sleep(20)"]}}))
        _, external_exp = self.request("/api/demo", {})
        external_review = self.workbench.submit_review(external_exp["id"], {"summary": "External request"})
        external_runner = AgentRunner(self.workbench)
        external_job = external_runner.start(external_exp["id"], external_review["id"])
        try:
            # Idempotent redispatch does not transfer ownership to this GUI.
            status, same_job = self.request(f"/api/experiments/{external_exp['id']}/reviews/{external_review['id']}/dispatch", {})
            self.assertEqual(status, 200, same_job)
            self.assertNotIn(external_job["id"], self.server.owned_agent_jobs)
            _, owned_exp = self.request("/api/demo", {})
            owned_review = self.workbench.submit_review(owned_exp["id"], {"summary": "GUI request"})
            status, owned_job = self.request(f"/api/experiments/{owned_exp['id']}/reviews/{owned_review['id']}/dispatch", {})
            self.assertEqual(status, 200, owned_job)
            self.assertIn(owned_job["id"], self.server.owned_agent_jobs)
            close_jobs(self.server)
            self.assertEqual(self.wait_job(owned_job["id"])["status"], "cancelled")
            self.assertIn(external_runner.status(external_job["id"])["status"], ACTIVE)
        finally:
            external_runner.cancel(external_job["id"])
            self.wait_job(external_job["id"])

    def test_poll_recovers_replay_owner_that_died_after_gui_started(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        save_json(self.server.replay_jobs.directory / "replay_external.json",
                  {"id": "replay_external", "kind": "replay", "status": "running", "pid": child.pid})
        _, response = self.request("/api/jobs")
        self.assertEqual(response["jobs"][0]["status"], "interrupted")

    def test_queued_replay_already_blocks_installation_upgrade(self):
        _, exp = self.request("/api/demo", {})
        queued_threads = []
        with patch("ablation_workbench.server.threading.Thread.start", lambda thread: queued_threads.append(thread)):
            job = self.server.replay_jobs.start(exp["id"], {
                "candidates": ["baseline"], "scenarios": ["original"], "profile": "fixture"})
        try:
            with self.assertRaisesRegex(RuntimeError, "active jobs"):
                installation_upgrade()
        finally:
            with patch.object(self.workbench, "run", return_value=exp):
                queued_threads[0].run()
        self.assertEqual(self.wait_job(job["id"])["status"], "completed")
        with installation_upgrade():
            pass


if __name__ == "__main__": unittest.main()
