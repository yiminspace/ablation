from io import BytesIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from ablation_workbench import updates


class UpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        major, minor, patch_version = map(int, updates.__version__.split("."))
        self.next_version = f"{major}.{minor}.{patch_version + 1}"

    def test_normal_install_checks_registry_without_preview_gate(self):
        response = BytesIO(json.dumps({"info": {"version": self.next_version}}).encode())
        with patch.object(updates, "_editable", return_value=False), patch.object(updates, "urlopen", return_value=response) as fetch, patch.dict("os.environ", {"ABLATION_UPDATE_CHECK": "1"}):
            result = updates.update_info(self.workspace, force=True)
        self.assertTrue(result["available"])
        self.assertEqual("checked", result["status"])
        self.assertEqual("https://pypi.org/pypi/ablation-workbench/json", fetch.call_args.args[0])

    def test_expired_cache_fetches_new_release(self):
        responses = [BytesIO(json.dumps({"info": {"version": version}}).encode()) for version in [updates.__version__, self.next_version]]
        with patch.object(updates, "_editable", return_value=False), patch.object(updates, "urlopen", side_effect=responses) as fetch, patch.dict("os.environ", {"ABLATION_UPDATE_CHECK": "1"}), patch.object(updates.time, "time", return_value=1000) as clock:
            self.assertFalse(updates.update_info(self.workspace)["available"])
            clock.return_value = 1000 + updates.CACHE_SECONDS + 1
            self.assertTrue(updates.update_info(self.workspace)["available"])
        self.assertEqual(2, fetch.call_count)

    def test_background_checker_recovers_and_stops_without_installing(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = [False, True]
        with patch.object(updates, "update_info", side_effect=[OSError("offline"), {}]) as check, patch.object(updates, "perform_upgrade") as install:
            updates._update_loop(self.workspace, stop)
        self.assertEqual(2, check.call_count)
        install.assert_not_called()

    def test_gui_advertises_explicit_command_and_published_release(self):
        with patch.object(updates, "update_info", return_value={"latest": self.next_version, "available": True}), patch.object(updates, "upgrade_plan", return_value={"command": ["pipx", "upgrade", "ablation-workbench"]}):
            result = updates.gui_update_info(self.workspace)
        self.assertEqual("pipx upgrade ablation-workbench", result["upgrade_command"])
        self.assertEqual(f"https://github.com/yiminspace/ablation/releases/tag/v{self.next_version}", result["release_url"])

    def test_cache_and_explicit_opt_out(self):
        response = BytesIO(json.dumps({"info": {"version": self.next_version}}).encode())
        with patch.object(updates, "_editable", return_value=False), patch.object(updates, "urlopen", return_value=response) as fetch, patch.dict("os.environ", {"ABLATION_UPDATE_CHECK": "1"}):
            first = updates.update_info(self.workspace)
            second = updates.update_info(self.workspace)
            self.assertTrue(first["available"])
            self.assertEqual(first, second)
            self.assertEqual(1, fetch.call_count)
            with patch.dict("os.environ", {"ABLATION_UPDATE_CHECK": "0"}):
                self.assertEqual("disabled", updates.update_info(self.workspace, force=True)["status"])
                self.assertEqual(1, fetch.call_count)

    def test_missing_distribution_not_reported_as_update(self):
        with patch.object(updates, "_editable", return_value=False), patch.object(updates, "urlopen", side_effect=HTTPError("url", 404, "missing", {}, None)), patch.dict("os.environ", {"ABLATION_UPDATE_CHECK": "1"}):
            result = updates.update_info(self.workspace)
        self.assertEqual("unavailable", result["status"])
        self.assertFalse(result["available"])

    def test_source_checkout_skips_network_and_upgrade(self):
        with patch.object(updates, "_editable", return_value=True), patch.object(updates, "urlopen") as fetch:
            self.assertEqual("disabled", updates.update_info(self.workspace)["status"])
            self.assertFalse(updates.upgrade_plan()["supported"])
            fetch.assert_not_called()

    def test_detects_package_managers(self):
        for marker, manager in [("pipx_metadata.json", "pipx"), ("uv-receipt.toml", "uv")]:
            path = self.workspace / marker
            path.touch()
            with patch.object(updates, "_editable", return_value=False), patch.object(updates.sys, "prefix", str(self.workspace)), patch.object(updates.shutil, "which", return_value=f"/bin/{manager}"):
                plan = updates.upgrade_plan()
                self.assertEqual(manager, plan["manager"])
                self.assertEqual(f"/bin/{manager}", plan["command"][0])
            path.unlink()

    def test_local_wheel_upgrade_switches_to_registry_requirement(self):
        (self.workspace / "uv-receipt.toml").touch()
        distribution = Mock()
        distribution.read_text.return_value = json.dumps({"url": "file:///temporary/source", "dir_info": {}})
        with patch.object(updates, "_editable", return_value=False), patch.object(updates, "_distribution", return_value=distribution), patch.object(updates.sys, "prefix", str(self.workspace)), patch.object(updates.shutil, "which", return_value="/bin/uv"):
            plan = updates.upgrade_plan()
        self.assertEqual(["/bin/uv", "tool", "install", "--upgrade", "--force", updates.PACKAGE], plan["command"])

    def test_active_job_blocks_upgrade(self):
        directory = self.workspace / "jobs"
        directory.mkdir()
        (directory / "j.json").write_text(json.dumps({"status": "running"}))
        with patch.object(updates.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "active jobs"):
                updates.perform_upgrade(self.workspace)
            run.assert_not_called()

    def test_upgrade_is_explicit_argv_and_cleans_marker(self):
        plan = {"supported": True, "command": ["pipx", "upgrade", updates.PACKAGE]}
        with patch.object(updates, "upgrade_plan", return_value=plan), patch.object(updates.subprocess, "run", return_value=subprocess.CompletedProcess(plan["command"], 0)) as run:
            result = updates.perform_upgrade(self.workspace)
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["restart_required"])
        self.assertFalse((self.workspace / ".upgrade-running").exists())
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(plan["command"], run.call_args.args[0])

    def test_offline_changelog(self):
        self.assertIn("0.1.0", updates.changelog())


if __name__ == "__main__":
    unittest.main()
