import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ablation_workbench.engine import Workbench
from ablation_workbench.sources import normalize_source, validate_request


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.config = {"schema_version": 1, "profiles": {}}
        self.request = {"model": "fixture-model", "instructions": "Use recorded evidence.", "input": [{"role": "user", "content": "What happened?"}], "tools": [{"type": "function", "name": "inspect", "description": "Read evidence", "parameters": {"type": "object"}}]}

    def test_request_import_is_lossless(self):
        provenance, request, response, kind = normalize_source({"kind": "request", "data": self.request, "response": {"output": []}}, self.config)
        self.assertEqual(request, self.request)
        self.assertEqual(response, {"output": []})
        self.assertEqual(kind, "responses")
        self.assertEqual(provenance["fidelity"], "provided")
        self.assertEqual(provenance["evidence"], self.request)

    def test_file_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "request.json"
            path.write_text(json.dumps(self.request))
            self.assertEqual(normalize_source({"kind": "request", "path": str(path)}, self.config)[1], self.request)

    def test_log_preserves_params_false_zero_unknown_fields_and_tools(self):
        log = {"model": "fixture-model", "params": {"instructions": "Task", "parallel_tool_calls": False, "temperature": 0, "vendor_option": {"enabled": False}}, "input": [{"role": "user", "content": "Test"}], "response": {"output": []}, "tools": self.request["tools"]}
        source, request, response, kind = normalize_source({"kind": "log", "data": log}, self.config)
        self.assertIs(request["parallel_tool_calls"], False)
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(request["vendor_option"], {"enabled": False})
        self.assertEqual(request["tools"], self.request["tools"])
        self.assertEqual(response, log["response"])
        self.assertNotIn("stream", request)
        self.assertEqual(source["fidelity"], "reconstructed")
        self.assertEqual(source["kind"], "log")
        self.assertEqual(kind, "responses")

    def test_two_unrelated_shapes_use_explicit_profile_mappings(self):
        first = {"event": {"request": dict(self.request, custom_parameter=0), "result": {"output": []}}, "overrides": {"parallel": False}}
        second = {"record": {"deployment": "fixture-chat", "history": [{"role": "user", "content": "Hello"}], "functions": self.request["tools"], "result": {"choices": []}}, "controls": {"temperature": 0, "parallel": False}, "params": {"vendor_flag": "keep"}}
        configs = [
            {"source_request_pointer": "/event/request", "source_response_pointer": "/event/result", "source_fields": {"parallel_tool_calls": "/overrides/parallel"}},
            {"source_fields": {"model": "/record/deployment", "messages": "/record/history", "tools": "/record/functions", "temperature": "/controls/temperature", "parallel_tool_calls": "/controls/parallel"}, "source_response_pointer": "/record/result"},
        ]
        results = []
        for data, profile in zip((first, second), configs):
            source, request, response, kind = normalize_source({"kind": "log", "data": data, "profile": "mapping"}, {"profiles": {"mapping": profile}})
            self.assertIs(request["parallel_tool_calls"], False)
            self.assertEqual(source["evidence"], data)
            self.assertEqual(source["mapping"], profile)
            self.assertEqual(source["fidelity"], "reconstructed")
            self.assertEqual(request["tools"], self.request["tools"])
            results.append(request)
        self.assertEqual(results[0]["custom_parameter"], 0)
        self.assertEqual(results[1]["vendor_flag"], "keep")
        self.assertEqual(results[1]["temperature"], 0)

    def test_root_pointer_and_local_import_without_profile(self):
        profiles = {"profiles": {"mapping": {"source_request_pointer": "/does-not-exist"}}}
        self.assertEqual(normalize_source({"kind": "request", "data": self.request}, profiles)[1], self.request)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "request.json"
            path.write_text(json.dumps(self.request))
            self.assertEqual(normalize_source({"kind": "request", "path": str(path)}, profiles)[1], self.request)
        source, request, _, _ = normalize_source({"kind": "log", "data": self.request, "profile": "root"}, {"profiles": {"root": {"source_request_pointer": ""}}})
        self.assertEqual(request, self.request)
        self.assertEqual(source["fidelity"], "reconstructed")

    def test_capture_unwraps_request_and_response_bodies(self):
        expected = {"choices": [{"message": {"content": "Hello"}}]}
        for captured_request in (self.request, {"body": json.dumps(self.request)}):
            data = {"request": captured_request, "response": {"status": 200, "body": json.dumps(expected)}}
            source, request, response, _ = normalize_source({"kind": "capture", "data": data}, self.config)
            self.assertEqual(request, self.request)
            self.assertEqual(response, expected)
            self.assertEqual(source["fidelity"], "recorded-json-body")
            self.assertEqual(source["evidence"], data)

    def test_mapping_fails_for_missing_pointer_or_conflicting_api_families(self):
        for fields in ({"model": "/missing"}, {"messages": "/input"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                normalize_source({"kind": "request", "data": self.request, "profile": "mapped"}, {"profiles": {"mapped": {"source_fields": fields}}})
        with self.assertRaises(ValueError):
            normalize_source({"kind": "log", "data": {"unknown": self.request}}, self.config)

    def test_previously_imported_baseline_is_not_remapped(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbench = Workbench(temporary)
            experiment = workbench.import_source({"kind": "request", "data": self.request})
            original = experiment["baseline"]
            experiment["source"]["kind"] = "legacy-provider"
            workbench.store.save(experiment)
            (Path(temporary) / "config.json").write_text(json.dumps({"profiles": {"mapping": {"source_request_pointer": "/missing"}}}))
            reopened = Workbench(temporary).get(experiment["id"])
            self.assertEqual(reopened["baseline"], original)
            self.assertEqual(reopened["source"]["kind"], "legacy-provider")

    def test_credential_fields_are_redacted_without_damaging_schema(self):
        data = {"request": self.request, "headers": {"Authorization": "Bearer hidden"}, "response": {"output": [], "api_key": "hidden"}}
        data["request"]["tools"][0]["parameters"]["properties"] = {"password": {"type": "string"}}
        source, request, response, _ = normalize_source({"kind": "capture", "data": data}, self.config)
        self.assertEqual(source["evidence"]["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(response["api_key"], "[REDACTED]")
        self.assertEqual(request["tools"][0]["parameters"]["properties"]["password"], {"type": "string"})

    def test_multivalue_and_name_value_headers_are_redacted(self):
        fixtures = [
            {"Authorization": ["SYNTHETIC-SECRET-ONE", "SYNTHETIC-SECRET-TWO"], "Accept": ["application/json"]},
            [{"name": "Authorization", "value": "SYNTHETIC-SECRET-ONE"}, {"name": "Cookie", "value": ["SYNTHETIC-SECRET-TWO"]}, {"name": "Accept", "value": "application/json"}],
            [["Proxy-Authorization", "SYNTHETIC-SECRET-ONE"], ["Set-Cookie", ["SYNTHETIC-SECRET-TWO"]], ["Accept", "application/json"]],
            [{"key": "X-Api-Key", "values": ["SYNTHETIC-SECRET-ONE"]}, {"Name": "AUTHORIZATION", "Value": "SYNTHETIC-SECRET-TWO"}],
            ["Authorization", "SYNTHETIC-SECRET-ONE", "Accept", "application/json", "Cookie", "SYNTHETIC-SECRET-TWO"],
            ["Authorization: SYNTHETIC-SECRET-ONE", "Accept: application/json", "Cookie: SYNTHETIC-SECRET-TWO"],
        ]
        for headers in fixtures:
            with self.subTest(headers=headers):
                source, request, _, _ = normalize_source({"kind": "capture", "data": {"request": self.request, "headers": headers}}, self.config)
                encoded = json.dumps(source)
                self.assertNotIn("SYNTHETIC-SECRET", encoded)
                self.assertIn("[REDACTED]", encoded)
                self.assertEqual(request, self.request)
                if "Accept" in json.dumps(headers):
                    self.assertIn("application/json", encoded)

    def test_header_redaction_preserves_schema_and_unrelated_name_value_data(self):
        schema = {"type": "object", "properties": {"headers": {"type": "object", "properties": {"Authorization": {"type": "array", "items": {"type": "string"}}, "Accept": {"type": "string"}}}, "password": {"type": "string"}}}
        self.request["tools"][0]["parameters"] = schema
        data = {"request": self.request, "fields": [{"name": "Authorization", "value": "ordinary field label"}], "headers": {"Accept": ["application/json"], "X-Trace-Id": "trace-123"}}
        source, request, _, _ = normalize_source({"kind": "capture", "data": data}, self.config)
        self.assertEqual(request["tools"][0]["parameters"], schema)
        self.assertEqual(source["evidence"]["fields"], data["fields"])
        self.assertEqual(source["evidence"]["headers"], data["headers"])

    def test_import_and_export_never_persist_multivalue_header_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbench = Workbench(temporary)
            data = {"request": {"body": self.request, "headers": {"Authorization": ["SYNTHETIC-SECRET-MAP"]}}, "response": {"output": []}, "response_headers": [{"name": "Set-Cookie", "value": "SYNTHETIC-SECRET-PAIR"}]}
            experiment = workbench.import_source({"kind": "capture", "data": data})
            persisted = workbench.store.experiment_path(experiment["id"]).read_text()
            exported = workbench.export_bundle(experiment["id"], Path(temporary) / "bundle.json").read_text()
            for contents in (persisted, exported):
                self.assertNotIn("SYNTHETIC-SECRET", contents)
                self.assertIn("[REDACTED]", contents)
            imported = workbench.import_bundle(Path(temporary) / "bundle.json")
            self.assertEqual(imported["baseline"]["request"], self.request)

    def test_configured_credentials_are_redacted_from_text(self):
        config = {"profiles": {"test": {"api_key_env": "ABLATION_TEST_SECRET"}}}
        with patch.dict(os.environ, {"ABLATION_TEST_SECRET": "test-secret-value"}):
            request = dict(self.request, instructions="Echo test-secret-value")
            source, body, _, _ = normalize_source({"kind": "request", "data": request}, config)
        self.assertNotIn("test-secret-value", json.dumps(source))
        self.assertEqual(body["instructions"], "Echo [REDACTED]")

    def test_basic_username_does_not_redact_message_roles_or_ordinary_text(self):
        config = {"profiles": {"source": {"source_user_env": "ABLATION_TEST_USER", "source_password_env": "ABLATION_TEST_PASSWORD"}}}
        request = {"model": "fixture-chat", "messages": [{"role": "user", "content": "The user supplied evidence."}]}
        data = {"request": request, "password": "SYNTHETIC-PASSWORD", "headers": {"Authorization": "Basic synthetic"}}
        with patch.dict(os.environ, {"ABLATION_TEST_USER": "user", "ABLATION_TEST_PASSWORD": "SYNTHETIC-PASSWORD"}):
            source, body, _, _ = normalize_source({"kind": "capture", "data": data}, config)
        self.assertEqual(body, request)
        self.assertEqual(source["evidence"]["password"], "[REDACTED]")
        self.assertEqual(source["evidence"]["headers"]["Authorization"], "[REDACTED]")

    def test_encoded_request_credentials_are_redacted_from_evidence(self):
        request = dict(self.request, api_key="never-persist-this")
        source, body, _, _ = normalize_source({"kind": "capture", "data": {"request": {"body": json.dumps(request)}}}, self.config)
        self.assertNotIn("never-persist-this", json.dumps(source))
        self.assertEqual(body["api_key"], "[REDACTED]")

    def test_invalid_sources_rejected(self):
        for source in ({"kind": "request"}, {"kind": "request", "url": "https://invalid.test"}, {"kind": "request", "data": self.request, "path": "/other"}, {"kind": "log", "data": {"model": "x"}}):
            with self.subTest(source=source), self.assertRaises(ValueError):
                normalize_source(source, self.config)
        with self.assertRaises(ValueError):
            validate_request({"model": "x", "input": [], "messages": []})


class RemoteSourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                cls.calls.append((self.path, self.headers.get("Authorization")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://foreign.example/private")
                    self.end_headers()
                    return
                if self.path.startswith("/records/"):
                    value = {"model": "fixture", "params": {"temperature": 0}, "input": [{"role": "user", "content": "Hi"}]}
                elif self.path.startswith("/unrelated-route"):
                    value = {"event": {"body": {"model": "fixture", "messages": [{"role": "user", "content": "Hi"}], "parallel_tool_calls": False}}, "result": {"choices": []}}
                else:
                    value = {"request": {"body": {"model": "fixture", "input": "Hi"}}, "response": {"body": {"output": []}}}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_arbitrary_json_route_basic_auth_independent_of_format(self):
        config = {"profiles": {"source": {"source_base_url": self.base, "source_user_env": "ABLATION_TEST_USER", "source_password_env": "ABLATION_TEST_PASSWORD"}}}
        with patch.dict(os.environ, {"ABLATION_TEST_USER": "fixture-user", "ABLATION_TEST_PASSWORD": "fixture-password"}):
            source, request, response, _ = normalize_source({"kind": "capture", "url": self.base + "/arbitrary/export?id=42", "profile": "source"}, config)
        self.assertEqual(request["input"], "Hi")
        self.assertEqual(response, {"output": []})
        self.assertTrue(self.calls[-1][1].startswith("Basic "))
        self.assertNotIn("fixture-password", json.dumps(source))
        self.assertEqual(source["profile"], "source")

    def test_remote_mapping_and_token_are_generic(self):
        profile = {"source_base_url": self.base, "source_request_pointer": "/event/body", "source_response_pointer": "/result", "token_env": "ABLATION_SOURCE_TOKEN", "api_key_env": "ABLATION_INFERENCE_TOKEN"}
        with patch.dict(os.environ, {"ABLATION_SOURCE_TOKEN": "source-secret", "ABLATION_INFERENCE_TOKEN": "inference-secret"}):
            source, request, response, kind = normalize_source({"kind": "request", "url": self.base + "/unrelated-route?format=json"}, {"profiles": {"mapped": profile}})
        self.assertEqual(kind, "chat")
        self.assertIs(request["parallel_tool_calls"], False)
        self.assertEqual(response, {"choices": []})
        self.assertEqual(self.calls[-1][1], "Bearer source-secret")
        self.assertEqual(source["fidelity"], "reconstructed")

    def test_reference_template_quotes_identifier_and_preserves_base_path(self):
        profile = {"source_base_url": self.base + "/records", "source_path": "/exports/{ref}?encoding=json"}
        _, request, _, _ = normalize_source({"kind": "log", "ref": "team/item ?#%"}, {"profiles": {"source": profile}})
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(self.calls[-1][0], "/records/exports/team%2Fitem%20%3F%23%25?encoding=json")

    def test_query_credentials_rejected_before_fetch_without_echoing_values(self):
        count = len(self.calls)
        secret = "SYNTHETIC-QUERY-SECRET"
        for key in ("api_key", "ACCESS_TOKEN", "%74oken", "X-Amz-Signature", "password"):
            for source, profile in (
                ({"kind": "capture", "url": self.base + "/capture?" + key + "=" + secret}, {"source_base_url": self.base}),
                ({"kind": "capture", "ref": "example"}, {"source_base_url": self.base, "source_path": "/capture/{ref}?" + key + "=" + secret}),
            ):
                with self.subTest(key=key, source=source), self.assertRaises(ValueError) as error:
                    normalize_source(source, {"profiles": {"source": profile}})
                self.assertIn("environment-backed headers", str(error.exception))
                self.assertNotIn(secret, str(error.exception))
        self.assertEqual(len(self.calls), count)

    def test_inference_credentials_are_never_used_to_read_sources(self):
        profile = {"source_base_url": self.base, "api_key_env": "ABLATION_INFERENCE_TOKEN"}
        with patch.dict(os.environ, {"ABLATION_INFERENCE_TOKEN": "inference-only"}):
            normalize_source({"kind": "capture", "url": self.base + "/capture"}, {"profiles": {"source": profile}})
        self.assertIsNone(self.calls[-1][1])
        with self.assertRaises(ValueError):
            normalize_source({"kind": "capture", "url": self.base + "/capture"}, {"profiles": {"inference": {"base_url": self.base}}})

    def test_external_credential_urls_templates_and_redirects_rejected(self):
        config = {"profiles": {"source": {"source_base_url": self.base}}}
        count = len(self.calls)
        for url in ("https://foreign.example/data", self.base.replace("http://", "http://user:password@") + "/data", "//foreign.example/data", self.base + "/data#fragment", self.base + "/data\\foreign"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_source({"kind": "capture", "url": url}, config)
        for template in ("https://foreign.example/{ref}", "//foreign.example/{ref}", "/%2Fforeign/{ref}", "/../{ref}", "/data/{other}", "/data/{ref}#fragment"):
            with self.subTest(template=template), self.assertRaises(ValueError):
                normalize_source({"kind": "capture", "ref": "id"}, {"profiles": {"source": {"source_base_url": self.base, "source_path": template}}})
        with self.assertRaises(ValueError):
            normalize_source({"kind": "capture", "ref": "id"}, config)
        self.assertEqual(len(self.calls), count)
        with self.assertRaisesRegex(ValueError, "HTTP 302"):
            normalize_source({"kind": "capture", "url": self.base + "/redirect"}, config)
        self.assertEqual(len(self.calls), count + 1)

    def test_ambiguous_source_auth_rejected(self):
        profile = {"source_base_url": self.base, "token_env": "TOKEN", "source_user_env": "USER", "source_password_env": "PASSWORD"}
        with self.assertRaisesRegex(ValueError, "not both"):
            normalize_source({"kind": "capture", "url": self.base + "/capture"}, {"profiles": {"source": profile}})


if __name__ == "__main__":
    unittest.main()
