from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ablation_workbench.cli import main, read_json


class CLITests(unittest.TestCase):
    def test_multiline_rationale_file_and_inline_text_share_diff_output(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "request.json"
            request.write_text(json.dumps({"model": "fixture", "instructions": "Original instruction", "input": "Task"}))
            status, output, error = self.invoke(directory, ["import", str(request)])
            self.assertEqual(status, 0, error)
            experiment = json.loads(output)
            patch_file = Path(directory) / "patch.json"
            patch_file.write_text(json.dumps([{"op": "replace", "path": "/instructions", "value": "Updated instruction"}]))
            explanation = "减少工具选择绕路。\n修改后端生成[工具描述](https://github.com/example/project/blob/main/tools.py#L10)的地方，不改工具参数。"
            rationale_file = Path(directory) / "rationale.md"
            rationale_file.write_text(explanation, encoding="utf-8")
            for flags in (["--rationale-file", str(rationale_file)], ["--rationale", explanation]):
                status, output, error = self.invoke(directory, ["candidate", experiment["id"], "--label", "Updated", "--patch", str(patch_file), *flags])
                self.assertEqual(status, 0, error)
                candidate = json.loads(output)["candidates"][-1]
                self.assertEqual(candidate["rationale"], explanation)
                status, output, error = self.invoke(directory, ["diff", experiment["id"], "--candidate", candidate["id"], "--artifact", "changes"])
                self.assertEqual(status, 0, error)
                self.assertEqual(json.loads(output)["rationale"], explanation)

    def invoke(self, directory, args):
        out, error = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            status = main(["--workspace", directory, *args])
        return status, out.getvalue(), error.getvalue()

    def test_clean_workspace_has_demo_and_machine_readable_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["--workspace", directory, "demo"]), 0)
            exp = json.loads(out.getvalue())
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["--workspace", directory, "diff", exp["id"], "--candidate", exp["candidates"][-1]["id"], "--artifact", "prompt"]), 0)
            result = json.loads(out.getvalue())
            self.assertTrue(result["rows"])
            self.assertEqual(exp["runs"], [])

    def test_config_rejects_secret_value_in_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            out, error = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(error):
                status = main(["--workspace", directory, "config", "profile", "test", "--api-key-env", "sk-fake-secret"])
            self.assertEqual(status, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertNotIn("sk-fake-secret", error.getvalue())
            self.assertFalse(Path(directory, "config.json").exists())

    def test_gui_port_defaults_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory, patch("ablation_workbench.server.serve") as serve:
            for options, port in (([], None), (["--port", "9000"], 9000), (["--port", "0"], 0)):
                status, _, error = self.invoke(directory, ["gui", "--no-open", *options])
                self.assertEqual(status, 0, error)
                self.assertEqual(serve.call_args.kwargs["port"], port)

    def test_generic_profile_mapping_flags_and_local_import(self):
        with tempfile.TemporaryDirectory() as directory:
            status, _, error = self.invoke(directory, ["config", "profile", "source", "--source-base-url", "https://logs.example", "--source-path", "/records/{ref}", "--source-request-pointer", "/payload", "--source-response-pointer", "/result", "--source-fields-json", '{"model":"/deployment"}', "--token-env", "EXAMPLE_SOURCE_TOKEN"])
            self.assertEqual(status, 0, error)
            source = {"payload": {"model": "original", "messages": [], "custom_flag": False}, "deployment": "override", "result": {"choices": []}}
            path = Path(directory) / "local.json"
            path.write_text(json.dumps(source))
            with patch("ablation_workbench.sources.fetch") as fetch:
                status, output, error = self.invoke(directory, ["import", str(path), "--format", "log", "--profile", "source"])
            self.assertEqual(status, 0, error)
            fetch.assert_not_called()
            experiment = json.loads(output)
            self.assertEqual(experiment["source"]["kind"], "log")
            self.assertEqual(experiment["baseline"]["request"], {"model": "override", "messages": [], "custom_flag": False})
            self.assertEqual(experiment["baseline"]["response"], {"choices": []})

    def test_template_refs_are_explicit_and_ambiguous_paths_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            status, _, error = self.invoke(directory, ["config", "profile", "source", "--source-base-url", "https://logs.example", "--source-path", "/exports/{ref}"])
            self.assertEqual(status, 0, error)
            response = (b'{"model":"fixture","input":"Hello"}', "application/json")
            with patch("ablation_workbench.sources.fetch", return_value=response) as fetch:
                status, output, error = self.invoke(directory, ["import", "record-17", "--profile", "source"])
                self.assertEqual(status, 0, error)
                self.assertEqual(fetch.call_args.args[0], "https://logs.example/exports/record-17")
                fetch.reset_mock()
                status, _, error = self.invoke(directory, ["import", "missing.json", "--profile", "source"])
                self.assertEqual(status, 2)
                self.assertIn("--ref", error)
                fetch.assert_not_called()
                status, _, error = self.invoke(directory, ["import", "group/record", "--profile", "source", "--ref"])
                self.assertEqual(status, 0, error)
                self.assertEqual(fetch.call_args.args[0], "https://logs.example/exports/group%2Frecord")
            status, _, _ = self.invoke(directory, ["import", "record-17"])
            self.assertEqual(status, 2)

    def test_url_import_does_not_probe_filesystem_or_guess_format(self):
        with tempfile.TemporaryDirectory() as directory:
            status, _, error = self.invoke(directory, ["config", "profile", "source", "--source-base-url", "https://logs.example"])
            self.assertEqual(status, 0, error)
            url = "https://logs.example/" + "x" * 5000
            with patch("ablation_workbench.sources.fetch", return_value=(b'{"model":"fixture","input":[]}', "application/json")) as fetch:
                status, output, error = self.invoke(directory, ["import", url, "--profile", "source"])
            self.assertEqual(status, 0, error)
            self.assertEqual(fetch.call_args.args[0], url)
            self.assertEqual(json.loads(output)["source"]["kind"], "request")

    def test_profile_rejects_unsafe_templates_and_invalid_mapping(self):
        options = (["--source-path", "//foreign.example/{ref}"], ["--source-fields-json", '{"model":42}'], ["--source-request-pointer", "not-a-pointer"], ["--token-env", "SOURCE_TOKEN", "--source-user-env", "SOURCE_USER", "--source-password-env", "SOURCE_PASSWORD"])
        for option in options:
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                status, _, _ = self.invoke(directory, ["config", "profile", "bad", *option])
                self.assertEqual(status, 2)
                self.assertFalse(Path(directory, "config.json").exists())

    def test_read_json_expands_user_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text('{"input": "fixture"}')
            with patch.object(Path, "expanduser", return_value=path):
                self.assertEqual(read_json("~/input.json"), {"input": "fixture"})


if __name__ == "__main__": unittest.main()
