import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ablation_workbench.engine import Workbench
from ablation_workbench.replay import replay_request
from ablation_workbench.server import close_jobs, make_server
from ablation_workbench.sources import fetch


class FixtureResponse(io.BytesIO):
    def __init__(self, body, content_type="application/json"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}


class LargePayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = "x" * (32 * 1024 * 1024 + 1)

    @classmethod
    def tearDownClass(cls):
        del cls.text

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workbench = Workbench(self.temp.name)
        self.request = {"model": "fixture-model", "input": "Synthetic request"}
        self.profile = {"base_url": "http://127.0.0.1:1"}

    def test_replay_can_save_reopen_and_extend_experiment_above_32_mib(self):
        (Path(self.temp.name) / "config.json").write_text(json.dumps({
            "schema_version": 1, "profiles": {"fixture": self.profile},
        }))
        experiment = self.workbench.import_source({"kind": "request", "data": self.request})
        responses = [
            {"status": "completed", "response": {"output_text": self.text, "output": []}, "elapsed_ms": 1},
            {"status": "completed", "response": {"output": []}, "elapsed_ms": 1},
        ]
        with patch("ablation_workbench.engine.replay_request", side_effect=responses):
            result = self.workbench.run(experiment["id"], candidates=["baseline"],
                                        scenarios=["original"], repeats=2, profile="fixture")
        self.assertGreater(self.workbench.store.experiment_path(experiment["id"]).stat().st_size,
                           32 * 1024 * 1024)
        reopened = self.workbench.get(experiment["id"])
        self.assertEqual(len(reopened["runs"]), 2)
        self.assertTrue(all(run["status"] == "completed" for run in reopened["runs"]))
        self.assertEqual(reopened["runs"][0]["response"]["output_text"], self.text)
        self.assertEqual(reopened["baseline"]["hash"], result["baseline"]["hash"])

    def test_remote_import_body_above_32_mib_is_not_truncated(self):
        body = json.dumps({"image": self.text}).encode()
        opener = Mock()
        opener.open.return_value = FixtureResponse(body)
        with patch("ablation_workbench.sources.urllib.request.build_opener", return_value=opener):
            actual, content_type = fetch("http://127.0.0.1/source", {})
        self.assertEqual(actual, body)
        self.assertEqual(content_type, "application/json")

    def test_large_model_response_in_json_and_sse(self):
        response = {"output": [], "output_text": self.text}
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                body = json.dumps({"type": "response.completed", "response": response} if streaming else response).encode()
                if streaming:
                    body = b"data: " + body + b"\n\n"
                opener = Mock()
                opener.open.return_value = FixtureResponse(
                    body, "text/event-stream" if streaming else "application/json",
                )
                with patch("ablation_workbench.replay.urllib.request.build_opener", return_value=opener):
                    result = replay_request(self.request, self.profile, "responses", on_text=lambda _: None)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["response"], response)

    def test_gui_accepts_large_json_upload_and_rejects_negative_length(self):
        server = make_server(self.workbench)
        self.addCleanup(server.server_close)
        self.addCleanup(close_jobs, server)
        body = json.dumps({"image": self.text}).encode()
        handler = SimpleNamespace(
            headers={"Content-Length": str(len(body)), "Content-Type": "application/json"},
            rfile=io.BytesIO(body),
        )
        self.assertEqual(server.RequestHandlerClass.read_body(handler), {"image": self.text})
        handler.headers["Content-Length"] = "-1"
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            server.RequestHandlerClass.read_body(handler)
