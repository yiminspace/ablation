import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ablation_workbench.engine import Workbench, request_for
from ablation_workbench.models import apply_patch, digest
from ablation_workbench.replay import evaluate_checks, parse_response


class EngineFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workbench = Workbench(self.temp.name)
        self.request = {"model": "fixture-model", "instructions": "Report only verified outcomes.", "input": [{"role": "user", "content": "Did it send?"}], "tools": [{"type": "function", "name": "inspect", "description": "Read evidence"}, {"type": "function", "name": "finish", "description": "Report a verified result"}], "stream": False}
        self.experiment = self.workbench.import_source({"kind": "request", "data": self.request}, title="Synthetic send verification")
        self.id = self.experiment["id"]

    def add_candidate(self):
        experiment = self.workbench.add_candidate(self.id, label="General completion rule", patch=[{"op": "replace", "path": "/tools/1/description", "value": "Report success only when observed evidence confirms completion."}], rationale="Require observed evidence, independent of downstream step names.")
        return experiment["candidates"][-1]["id"]

    def add_scenario(self):
        experiment = self.workbench.add_scenario(self.id, label="Unconfirmed send", description="Synthetic condition: the request was issued but there is no delivery confirmation.", patch=[{"op": "replace", "path": "/input/0/content", "value": "The request was issued, but delivery is unconfirmed."}], checks=[{"kind": "text_contains", "value": "unconfirmed"}])
        return experiment["scenarios"][-1]["id"]


class EngineTests(EngineFixture):
    def test_changes_only_show_candidate_prompt_not_input_or_scenario(self):
        experiment = self.workbench.add_candidate(self.id, label="Remove message", patch=[{"op": "remove", "path": "/input/0"}], rationale="Check input isolation.")
        candidate = experiment["candidates"][-1]["id"]
        diff = self.workbench.compare(self.id, candidate=candidate, artifact="changes")
        self.assertEqual(diff["before"], diff["after"])
        self.assertNotIn("/input", diff["before"])
        raw = self.workbench.compare(self.id, candidate=candidate, artifact="request")
        self.assertEqual(json.loads(raw["after"])["input"], [])
        scenario = self.add_scenario()
        tool_candidate = self.add_candidate()
        diff = self.workbench.compare(self.id, candidate=tool_candidate, scenario=scenario, artifact="changes")
        self.assertNotIn("Did it send?", diff["before"])
        self.assertNotIn("unconfirmed", diff["after"])
        self.assertIn("observed evidence", diff["after"])
        original = self.workbench.compare(self.id, candidate=tool_candidate, artifact="changes")
        self.assertEqual(diff["rows"], original["rows"])

    def test_readable_changes_preserve_request_and_review_evidence(self):
        experiment = self.workbench.add_candidate(self.id, label="Multiline", patch=[{"op": "replace", "path": "/instructions", "value": "Report verified outcomes.\nCheck evidence first."}], rationale="Readable review.")
        candidate = experiment["candidates"][-1]["id"]
        diff = self.workbench.compare(self.id, candidate=candidate, artifact="changes")
        self.assertIn("字段：/instructions\nReport only verified outcomes.", diff["before"])
        self.assertIn("字段：/instructions\nReport verified outcomes.\nCheck evidence first.", diff["after"])
        self.assertNotIn("\\n", diff["after"])
        raw = self.workbench.compare(self.id, candidate=candidate, artifact="request")
        self.assertEqual(json.loads(raw["before"]), self.request)
        self.assertEqual(json.loads(raw["after"])["instructions"], "Report verified outcomes.\nCheck evidence first.")
        row = next(row for row in diff["rows"] if row["kind"] == "replace")
        review = self.workbench.submit_review(self.id, {"candidate": candidate, "comments": [dict(row, artifact="changes", text="Explain this instruction.")]})
        self.assertEqual(review["comments"][0]["after"], row["after"])
        self.assertEqual(review["snapshot"]["diffs"]["changes"]["after"], diff["after"])

    def test_requirement_annotations_preserve_request_and_comment_anchors(self):
        candidate = self.add_candidate()
        before = self.workbench.get(self.id)
        raw = self.workbench.compare(self.id, candidate=candidate, artifact="changes")
        groups = [{"id": "coverage", "title": "改进判定", "reason": "明确完成证据", "effect": "判断可解释", "matches": ["observed evidence"], "sources": [{"label": "定义", "url": "https://github.com/example/repo/blob/main/tools.py#L1"}]}]
        self.workbench.set_change_groups(self.id, candidate=candidate, groups=groups)
        annotated = self.workbench.compare(self.id, candidate=candidate, artifact="changes")
        self.assertEqual(raw["rows"], annotated["rows"])
        self.assertEqual(before["baseline"], self.workbench.get(self.id)["baseline"])
        self.assertEqual(request_for(before, candidate), request_for(self.workbench.get(self.id), candidate))
        indices = annotated["change_groups"][0]["row_indices"]
        self.assertTrue(indices)
        self.assertTrue(all(annotated["rows"][i]["kind"] != "equal" for i in indices))
        row = annotated["rows"][indices[0]]
        review = self.workbench.submit_review(self.id, {"candidate": candidate, "comments": [dict(row, artifact="changes", text="为什么这样改？")]})
        self.assertEqual(review["comments"][0]["after"], row["after"])
        self.assertEqual(self.workbench.compare(self.id, candidate=candidate, artifact="request")["change_groups"], [])

    def test_requirement_annotations_reject_unsafe_links_and_unmatched_anchors(self):
        candidate = self.add_candidate()
        group = {"id": "coverage", "title": "判定", "reason": "原因", "effect": "效果", "matches": ["observed evidence"], "sources": []}
        before = self.workbench.get(self.id)
        for invalid in [{**group, "matches": ["absent anchor"]}, {**group, "sources": [{"label": "危险链接", "url": "javascript:alert(1)"}]}]:
            with self.assertRaises(ValueError):
                self.workbench.set_change_groups(self.id, candidate=candidate, groups=[invalid])
            self.assertEqual(before, self.workbench.get(self.id))
        with self.assertRaises(ValueError):
            self.workbench.set_change_groups(self.id, candidate=candidate, groups=[group, {**group, "id": "other"}])

    def test_pre_upgrade_drafts_retain_legacy_json_snapshots(self):
        candidate = self.add_candidate()
        for artifact in ("changes", "prompt"):
            old = self.workbench.compare(self.id, candidate=candidate, artifact=artifact, format="legacy")
            row = next(row for row in old["rows"] if row["kind"] != "equal")
            review = self.workbench.submit_review(self.id, {"candidate": candidate, "comments": [dict(row, artifact=artifact, text="旧版草稿")]})
            self.assertEqual(review["snapshot"]["diffs"][artifact]["rows"], old["rows"])
            self.assertEqual(review["snapshot"]["diffs"][artifact]["before"], old["before"])
            self.assertNotEqual(self.workbench.compare(self.id, candidate=candidate, artifact=artifact)["before"], old["before"])

    def test_bundle_import_rejects_overlapping_and_unmatched_groups_before_save(self):
        candidate = self.add_candidate()
        path = self.workbench.export_bundle(self.id, Path(self.temp.name) / "groups.json")
        original = json.loads(path.read_text())
        group = {"id": "one", "title": "判定", "reason": "原因", "effect": "效果", "matches": ["observed evidence"], "sources": []}
        for groups in ([group, {**group, "id": "two"}], [{**group, "matches": ["missing anchor"]}]):
            data = copy.deepcopy(original)
            data["experiment"]["candidates"][-1]["change_groups"] = groups
            path.write_text(json.dumps(data))
            before = self.workbench.list_experiments()
            with self.assertRaises(ValueError):
                self.workbench.import_bundle(path)
            self.assertEqual(before, self.workbench.list_experiments())

    def test_immutable_baseline_and_full_tools_array(self):
        candidate = self.add_candidate()
        experiment = self.workbench.get(self.id)
        self.assertEqual(experiment["baseline"]["request"], self.request)
        self.assertEqual(experiment["baseline"]["hash"], digest(self.request))
        request = request_for(experiment, candidate)
        self.assertEqual(len(request["tools"]), 2)
        self.assertEqual(request["tools"][0], self.request["tools"][0])
        self.assertEqual(request["input"], self.request["input"])

    def test_candidate_and_scenario_reject_sensitive_patch_targets_before_saving(self):
        original = self.workbench.get(self.id)
        for path in ("/api_key", "/input/0/password"):
            value = "SYNTHETIC-PATCH-SECRET"
            for transient in (False, True):
                operations = [{"op": "add", "path": path, "value": value}]
                if transient:
                    operations.append({"op": "remove", "path": path})
                for kind in ("candidate", "scenario"):
                    with self.subTest(path=path, transient=transient, kind=kind), self.assertRaisesRegex(ValueError, "sensitive request"):
                        if kind == "candidate":
                            self.workbench.add_candidate(self.id, label="Unsafe", patch=operations, rationale="Synthetic regression")
                        else:
                            self.workbench.add_scenario(self.id, label="Unsafe", patch=operations, description="Synthetic regression")
                    self.assertEqual(self.workbench.get(self.id), original)
        exported = self.workbench.export_bundle(self.id, Path(self.temp.name) / "safe.json").read_text()
        self.assertNotIn("SYNTHETIC-PATCH-SECRET", exported)

    def test_candidate_lineage_and_scenario_are_composed(self):
        candidate = self.add_candidate()
        experiment = self.workbench.add_candidate(self.id, label="Concise", parent=candidate, patch=[{"op": "replace", "path": "/instructions", "value": "Be concise."}], rationale="Reduce verbosity while retaining evidence rule.")
        child = experiment["candidates"][-1]["id"]
        scenario = self.add_scenario()
        experiment = self.workbench.get(self.id)
        request = request_for(experiment, child, scenario)
        self.assertEqual(request["instructions"], "Be concise.")
        self.assertIn("observed evidence", request["tools"][1]["description"])
        self.assertEqual(request["input"], request_for(experiment, "baseline", scenario)["input"])

    def test_overlapping_scenario_and_candidate_are_rejected(self):
        candidate = self.add_candidate()
        experiment = self.workbench.add_scenario(self.id, label="Conflict", patch=[{"op": "remove", "path": "/tools"}], description="Remove tools for a test.")
        with self.assertRaisesRegex(ValueError, "overlap"):
            request_for(experiment, candidate, experiment["scenarios"][-1]["id"])

    def test_diff_separates_prompt_input_source_and_untested_response(self):
        candidate, scenario = self.add_candidate(), self.add_scenario()
        prompt = self.workbench.compare(self.id, candidate=candidate, scenario=scenario, artifact="prompt")
        inputs = self.workbench.compare(self.id, candidate=candidate, scenario=scenario, artifact="input")
        self.assertTrue(any(row["kind"] == "replace" for row in prompt["rows"]))
        self.assertIn("observed evidence", prompt["after"])
        self.assertNotIn("unconfirmed", prompt["after"])
        self.assertIn("unconfirmed", inputs["after"])
        self.assertIn("synthetic", prompt["notes"][0])
        self.assertIn("untested", self.workbench.compare(self.id, candidate=candidate, artifact="response")["after"])
        self.assertIn("Normalized", self.workbench.compare(self.id, artifact="source")["candidate_label"])

    def test_review_is_idempotent_and_snapshot_survives_new_candidate(self):
        candidate = self.add_candidate()
        diff = self.workbench.compare(self.id, candidate=candidate, artifact="prompt")
        row = next(row for row in diff["rows"] if row["kind"] == "replace")
        payload = {"id": "review_fixed", "candidate": candidate, "intent": "comment", "comments": [dict(row, artifact="prompt", text="Why is this more general?")]}
        review = self.workbench.submit_review(self.id, payload)
        revision = self.workbench.get(self.id)["revision"]
        self.assertEqual(self.workbench.submit_review(self.id, payload), review)
        self.assertEqual(self.workbench.get(self.id)["revision"], revision)
        self.add_candidate()
        self.assertEqual(self.workbench.get(self.id)["reviews"][0]["snapshot"], review["snapshot"])
        conflicting = copy.deepcopy(payload)
        conflicting["comments"][0]["text"] = "Changed comment"
        with self.assertRaisesRegex(ValueError, "different content"):
            self.workbench.submit_review(self.id, conflicting)

    def test_stale_anchor_rejected(self):
        candidate = self.add_candidate()
        row = next(row for row in self.workbench.compare(self.id, candidate=candidate)["rows"] if row["kind"] == "replace")
        row["before"] = "not the original text"
        with self.assertRaisesRegex(ValueError, "anchor"):
            self.workbench.submit_review(self.id, {"candidate": candidate, "comments": [dict(row, artifact="request", text="Why?")]})

    def test_explanation_only_adds_reply(self):
        review = self.workbench.submit_review(self.id, {"summary": "Why this condition?", "intent": "comment"})
        before = self.workbench.get(self.id)
        result = self.workbench.reply(self.id, review["id"], text="It requires observable completion evidence.")
        after = self.workbench.get(self.id)
        self.assertEqual(before["candidates"], after["candidates"])
        self.assertEqual(before["runs"], after["runs"])
        self.assertEqual(result["replies"][0]["text"], "It requires observable completion evidence.")
        text = self.workbench.review_text(self.id, review["id"])
        self.assertIn("Do not change prompts", text)
        self.assertIn("--text", text)
        from ablation_workbench.cli import parser
        command = next(line for line in text.splitlines() if line.startswith("ablation --workspace"))
        args = parser().parse_args(shlex.split(command)[1:])
        self.assertEqual(args.review_action, "reply")
        self.assertEqual(args.review, review["id"])

    def test_export_import_preserves_history_without_overwrite(self):
        self.add_candidate()
        review = self.workbench.submit_review(self.id, {"summary": "Why?"})
        self.workbench.reply(self.id, review["id"], text="Because the original evidence is incomplete.")
        export_path = Path(self.temp.name) / "bundle.json"
        self.workbench.export_bundle(self.id, export_path)
        imported = self.workbench.import_bundle(export_path)
        self.assertNotEqual(imported["id"], self.id)
        original = self.workbench.get(self.id)
        self.assertEqual(imported["baseline"], original["baseline"])
        self.assertEqual(imported["reviews"], original["reviews"])
        self.assertEqual(len(self.workbench.list_experiments()), 2)
        with self.assertRaises(FileExistsError):
            self.workbench.export_bundle(self.id, export_path)

    def test_malformed_bundles_do_not_poison_existing_workspace(self):
        candidate = self.add_candidate()
        row = self.workbench.compare(self.id, candidate=candidate)["rows"][0]
        review = self.workbench.submit_review(self.id, {"candidate": candidate, "comments": [dict(row, artifact="request", text="Explain this evidence.")]})
        self.workbench.reply(self.id, review["id"], text="A synthetic explanation.")
        exported = self.workbench.export_bundle(self.id, Path(self.temp.name) / "valid.json")
        bundle = json.loads(exported.read_text())
        mutations = [
            lambda item: item.pop("title"),
            lambda item: item["source"].update(notes="wrong shape"),
            lambda item: item["candidates"][1].update(label={}),
            lambda item: item["scenarios"][0].update(checks="wrong shape"),
            lambda item: item["runs"].append({"id": "run_bad", "status": "completed"}),
            lambda item: item["reviews"][0].pop("snapshot"),
            lambda item: item["reviews"][0]["replies"][0].update(comment_index=50),
            lambda item: item["reviews"][0]["snapshot"]["diffs"]["request"].update(rows=[]),
            lambda item: item["source"].update(ref="https://source.example/log?token=SYNTHETIC-BUNDLE-SECRET"),
        ]
        original = self.workbench.get(self.id)
        for index, mutate in enumerate(mutations):
            corrupted = copy.deepcopy(bundle)
            mutate(corrupted["experiment"])
            path = Path(self.temp.name) / f"invalid-{index}.json"
            path.write_text(json.dumps(corrupted))
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.workbench.import_bundle(path)
            self.assertEqual([item["id"] for item in self.workbench.list_experiments()], [self.id])
            self.assertEqual(self.workbench.get(self.id), original)
        self.assertEqual(self.workbench.import_bundle(exported)["reviews"], original["reviews"])

    def test_hash_tampering_path_traversal_and_secret_config_rejected(self):
        with self.assertRaises(ValueError):
            self.workbench.get("../../config")
        path = self.workbench.store.experiment_path(self.id)
        data = json.loads(path.read_text())
        data["baseline"]["request"]["instructions"] = "tampered"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.workbench.get(self.id)
        (self.workbench.workspace / "config.json").write_text(json.dumps({"profiles": {"test": {"api_key": "do-not-store"}}}))
        with self.assertRaisesRegex(ValueError, "environment"):
            self.workbench.store.config()

    def test_reopening_marks_dead_process_run_unconfirmed(self):
        experiment = self.workbench.get(self.id)
        experiment["runs"].append({"id": "run_orphan", "status": "running", "owner_pid": 2147483000, "passed": None})
        self.workbench.store.save(experiment)
        reopened = Workbench(self.temp.name).get(self.id)
        self.assertEqual(reopened["runs"][-1]["status"], "interrupted")
        self.assertIn("unconfirmed", reopened["runs"][-1]["error"])

    def test_open_gui_recovers_child_replay_that_dies_later(self):
        child = subprocess.run(
            [sys.executable, "-c", "\n".join([
                "import os, sys",
                "from ablation_workbench.engine import Workbench",
                "workbench = Workbench(sys.argv[1])",
                "with workbench.store.lock(sys.argv[2]):",
                "    experiment = workbench.store.load(sys.argv[2])",
                "    experiment['runs'].append({'id': 'run_child', 'status': 'running', 'owner_pid': os.getpid(), 'passed': None})",
                "    workbench.store.save(experiment)",
                "os._exit(9)",
            ]), self.temp.name, self.id],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(child.returncode, 9, child.stderr)
        before = self.workbench.store.load(self.id)
        self.assertEqual(before["runs"][-1]["status"], "running")
        recovered = self.workbench.get(self.id)
        self.assertEqual(recovered["runs"][-1]["status"], "interrupted")
        self.assertIn("unconfirmed", recovered["runs"][-1]["error"])
        self.assertIsNone(recovered["runs"][-1]["passed"])
        self.assertEqual(recovered["revision"], before["revision"] + 1)
        self.assertEqual(self.workbench.get(self.id)["revision"], recovered["revision"])

    def test_recovery_rechecks_after_concurrent_writer_finishes(self):
        experiment = self.workbench.get(self.id)
        experiment["runs"].append({"id": "run_race", "status": "running", "owner_pid": 2147483000, "passed": None})
        self.workbench.store.save(experiment)
        observed, result, errors = threading.Event(), [], []

        def read_experiment():
            try:
                result.append(self.workbench.get(self.id))
            except Exception as error:
                errors.append(error)

        def alive(_):
            observed.set()
            return False

        with patch("ablation_workbench.runner._alive", side_effect=alive):
            with self.workbench.store.lock(self.id):
                reader = threading.Thread(target=read_experiment, daemon=True)
                reader.start()
                self.assertTrue(observed.wait(timeout=2))
                latest = self.workbench.store.load(self.id)
                latest["runs"][-1].update(status="completed", response={"output": []})
                self.workbench.store.save(latest)
            reader.join(timeout=2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]["runs"][-1]["status"], "completed")
        self.assertEqual(result[0]["revision"], latest["revision"])

    def test_concurrent_replies_are_append_only(self):
        review = self.workbench.submit_review(self.id, {"summary": "Explain"})
        workers = [threading.Thread(target=self.workbench.reply, args=(self.id, review["id"]), kwargs={"text": f"Reply {index}"}) for index in range(5)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(len(self.workbench.get(self.id)["reviews"][0]["replies"]), 5)

    def test_read_only_agent_cannot_modify_experiment(self):
        with patch.dict(os.environ, {"ABLATION_AGENT_INTENT": "comment"}):
            with self.assertRaises(PermissionError):
                self.add_candidate()
            with self.assertRaises(PermissionError):
                self.add_scenario()
            with self.assertRaises(PermissionError):
                self.workbench.run(self.id, candidates=["baseline"], scenarios=["original"])

    def test_json_patch_array_semantics_and_pointer_escaping(self):
        value = {"a/b": {"~name": ["a", "b"]}}
        result = apply_patch(value, [{"op": "add", "path": "/a~1b/~0name/1", "value": "middle"}, {"op": "remove", "path": "/a~1b/~0name/0"}, {"op": "replace", "path": "/a~1b/~0name/1", "value": "last"}])
        self.assertEqual(result, {"a/b": {"~name": ["middle", "last"]}})
        self.assertEqual(value, {"a/b": {"~name": ["a", "b"]}})
        for pointer in ("/a~2b", "/missing/0", "/a~1b/~0name/-1", "/a~1b/~0name/01"):
            with self.subTest(pointer=pointer), self.assertRaises(ValueError):
                apply_patch(value, [{"op": "remove", "path": pointer}])


class ReplayTests(EngineFixture):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.calls.append((self.path, request))
                if request["model"] == "error-model":
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"rate limited"}}')
                    return
                if self.path.endswith("chat/completions"):
                    response = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "Delivery is unconfirmed."}, "finish_reason": "stop"}]}
                else:
                    response = {"id": "fixture-response", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Delivery is unconfirmed."}]}]}
                self.send_response(200)
                if request.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(("data: " + json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode())
                else:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(response).encode())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        super().setUp()
        (self.workbench.workspace / "config.json").write_text(json.dumps({"schema_version": 1, "profiles": {"local-fixture": {"base_url": f"http://127.0.0.1:{self.server.server_port}"}}}))

    def test_paired_replay_records_actual_request_and_assertions(self):
        candidate, scenario = self.add_candidate(), self.add_scenario()
        experiment = self.workbench.run(self.id, candidates=["baseline", candidate], scenarios=[scenario], repeats=2, profile="local-fixture")
        self.assertEqual(len(experiment["runs"]), 4)
        self.assertTrue(all(run["status"] == "completed" and run["passed"] is True for run in experiment["runs"]))
        for first, second in zip(experiment["runs"][::2], experiment["runs"][1::2]):
            self.assertEqual(first["request"]["input"], second["request"]["input"])
            self.assertEqual(first["request_hash"], digest(first["request"]))
        self.assertEqual(experiment["baseline"]["hash"], self.experiment["baseline"]["hash"])

    def test_no_checks_means_unscored(self):
        experiment = self.workbench.run(self.id, candidates=["baseline"], scenarios=["original"])
        self.assertIsNone(experiment["runs"][-1]["passed"])
        self.assertEqual(experiment["runs"][-1]["status"], "completed")

    def test_response_storage_failure_records_terminal_error_and_stops_batch(self):
        from ablation_workbench import storage
        result = {"status": "completed", "response": {"output_text": "x" * 20000}, "elapsed_ms": 1}
        def oversized_response(*_):
            storage.MAX_BYTES = self.workbench.store.experiment_path(self.id).stat().st_size
            return result
        with patch("ablation_workbench.storage.MAX_BYTES", 8000), patch("ablation_workbench.engine.replay_request", side_effect=oversized_response) as replay:
            experiment = self.workbench.run(self.id, candidates=["baseline"], scenarios=["original"], repeats=2)
        self.assertEqual(replay.call_count, 1)
        self.assertEqual(len(experiment["runs"]), 1)
        run = experiment["runs"][0]
        self.assertEqual(run["status"], "error")
        self.assertIsNone(run["passed"])
        self.assertIsNone(run["response"])
        self.assertEqual(run["checks"], [])
        self.assertIn("not retained", run["error"])
        self.assertNotIn("_completion_reserve", run)
        self.assertEqual(self.workbench.get(self.id)["runs"], experiment["runs"])

    def test_external_error_is_not_assertion_failure(self):
        experiment = self.workbench.add_candidate(self.id, label="Error fixture", patch=[{"op": "replace", "path": "/model", "value": "error-model"}], rationale="Exercise HTTP error handling.")
        candidate, scenario = experiment["candidates"][-1]["id"], self.add_scenario()
        experiment = self.workbench.run(self.id, candidates=[candidate], scenarios=[scenario])
        run = experiment["runs"][-1]
        self.assertEqual(run["status"], "error")
        self.assertIsNone(run["passed"])
        self.assertEqual(run["checks"], [])
        self.assertIn("429", run["error"])

    def test_cancellation_and_bounds(self):
        cancelled = threading.Event()
        cancelled.set()
        self.assertEqual(self.workbench.run(self.id, candidates=["baseline"], scenarios=["original"], cancel_event=cancelled)["runs"], [])
        with self.assertRaises(ValueError):
            self.workbench.run(self.id, candidates=["baseline"], scenarios=["original"], repeats=11)
        with self.assertRaises(ValueError):
            self.workbench.run(self.id, candidates=["baseline", "baseline"], scenarios=["original"])

    def test_upgrade_marker_prevents_new_replay(self):
        (self.workbench.workspace / ".upgrade-running").write_text("{}")
        with self.assertRaisesRegex(ValueError, "upgrade"):
            self.workbench.run(self.id, candidates=["baseline"], scenarios=["original"])
        self.assertEqual(self.workbench.get(self.id)["runs"], [])

    def test_responses_sse_is_parsed_and_preserved(self):
        experiment = self.workbench.add_candidate(self.id, label="Stream", patch=[{"op": "replace", "path": "/stream", "value": True}], rationale="Exercise streamed transport.")
        experiment = self.workbench.run(self.id, candidates=[experiment["candidates"][-1]["id"]], scenarios=["original"])
        run = experiment["runs"][-1]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["response"]["id"], "fixture-response")
        self.assertEqual(run["stream_events"][0]["type"], "response.completed")

    def test_new_replay_invalidates_old_response_comment_draft(self):
        experiment = self.workbench.run(self.id, candidates=["baseline"], scenarios=["original"])
        revision = experiment["revision"]
        diff = self.workbench.compare(self.id, artifact="response")
        row = diff["rows"][0]
        self.workbench.run(self.id, candidates=["baseline"], scenarios=["original"])
        with self.assertRaisesRegex(ValueError, "Response changed"):
            self.workbench.submit_review(self.id, {"revision": revision, "comments": [dict(row, artifact="response", text="Why this result?")]})

    def test_historical_response_followup_preserves_saved_parent_snapshot(self):
        experiment = self.workbench.run(self.id, candidates=["baseline"], scenarios=["original"])
        row = self.workbench.compare(self.id, artifact="response")["rows"][0]
        parent = self.workbench.submit_review(self.id, {"revision": experiment["revision"], "comments": [dict(row, artifact="response", text="Why this result?")]})
        self.workbench.run(self.id, candidates=["baseline"], scenarios=["original"])
        payload = {key: parent[key] for key in ("base", "candidate", "scenario", "revision")}
        payload.update(id="review_followup", comments=[dict(parent["comments"][0], text="Explain the original evidence further.")], parent_review={"id": parent["id"], "comment_index": 0})
        followup = self.workbench.submit_review(self.id, payload)
        self.assertEqual(followup["snapshot"], parent["snapshot"])
        self.assertEqual(self.workbench.submit_review(self.id, payload), followup)
        self.assertIn(f"Follow-up to review {parent['id']}, comment 0", self.workbench.review_text(self.id, followup["id"]))
        without_parent = copy.deepcopy(payload)
        without_parent.pop("id")
        without_parent.pop("parent_review")
        with self.assertRaisesRegex(ValueError, "Response changed"):
            self.workbench.submit_review(self.id, without_parent)
        mutations = [
            lambda item: item["comments"][0].update(before="different evidence"),
            lambda item: item["comments"][0].update(new_line=1000),
            lambda item: item.update(revision=self.workbench.get(self.id)["revision"]),
            lambda item: item["parent_review"].update(comment_index=1),
            lambda item: item["comments"].append(copy.deepcopy(item["comments"][0])),
            lambda item: item["parent_review"].update(id=item["id"]),
        ]
        for index, mutate in enumerate(mutations):
            invalid = copy.deepcopy(payload)
            invalid["id"] = f"review_invalid_{index}"
            mutate(invalid)
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.workbench.submit_review(self.id, invalid)
        path = self.workbench.export_bundle(self.id, Path(self.temp.name) / "followup.json")
        imported = self.workbench.import_bundle(path)
        self.assertEqual(imported["reviews"], self.workbench.get(self.id)["reviews"])
        bundle = json.loads(path.read_text())
        bundle["experiment"]["reviews"][-1]["snapshot"]["captured_revision"] += 1
        invalid_path = Path(self.temp.name) / "invalid-followup.json"
        invalid_path.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(ValueError, "follow-up snapshot"):
            self.workbench.import_bundle(invalid_path)


class ResponseParsingTests(unittest.TestCase):
    def test_tool_argument_assertion_supports_both_api_families(self):
        check = {"kind": "tool_argument_equals", "name": "finish", "pointer": "/status", "value": "unconfirmed"}
        responses = {"output": [{"type": "function_call", "name": "finish", "arguments": '{"status":"unconfirmed"}'}]}
        chat = {"choices": [{"message": {"tool_calls": [{"type": "function", "function": {"name": "finish", "arguments": '{"status":"unconfirmed"}'}}]}}]}
        for response in (responses, chat):
            self.assertTrue(evaluate_checks(response, [check])[0]["passed"])
        wrong = copy.deepcopy(responses)
        wrong["output"][0]["arguments"] = '{"status":"success"}'
        self.assertFalse(evaluate_checks(wrong, [check])[0]["passed"])
        wrong["output"][0]["arguments"] = 'malformed'
        result = evaluate_checks(wrong, [check])[0]
        self.assertFalse(result["passed"])
        self.assertTrue(result["errors"])

    def test_chat_sse_accumulates_content_and_tool_arguments(self):
        chunks = [{"choices": [{"index": 0, "delta": {"content": "Not ", "tool_calls": [{"index": 0, "id": "call_one", "function": {"name": "inspect", "arguments": '{"id":'}}]}}]}, {"choices": [{"index": 0, "delta": {"content": "confirmed.", "tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]}, "finish_reason": "tool_calls"}]}]
        raw = ("\n\n".join("data: " + json.dumps(chunk) for chunk in chunks) + "\n\ndata: [DONE]\n\n").encode()
        response, events = parse_response(raw, "text/event-stream", "chat")
        message = response["choices"][0]["message"]
        self.assertEqual(message["content"], "Not confirmed.")
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], '{"id":"x"}')
        results = evaluate_checks(response, [{"kind": "tool_called", "name": "inspect"}, {"kind": "text_contains", "value": "confirmed"}, {"kind": "json_equals", "pointer": "/choices/0/message/content", "value": "Not confirmed."}])
        self.assertTrue(all(item["passed"] for item in results))
        self.assertEqual(len(events), 2)

    def test_incomplete_stream_is_not_success(self):
        with self.assertRaisesRegex(ValueError, "without response.completed"):
            parse_response(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n', "text/event-stream", "responses")
        with self.assertRaisesRegex(ValueError, "before completion"):
            parse_response(b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n', "text/event-stream", "chat")

    def test_text_assertions_ignore_metadata_and_missing_paths_fail(self):
        response = {"metadata": {"text": "confirmed"}, "output": []}
        results = evaluate_checks(response, [{"kind": "text_contains", "value": "confirmed"}, {"kind": "json_equals", "pointer": "/missing", "value": None}])
        self.assertTrue(all(not check["passed"] for check in results))


if __name__ == "__main__":
    unittest.main()
