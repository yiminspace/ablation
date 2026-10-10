from io import BytesIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from ablation_workbench import updates


class UpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.published = patch.object(updates, "RELEASE_PUBLISHED", True)
        self.published.start()
        self.addCleanup(self.published.stop)

    def test_unpublished_preview_never_contacts_registry_or_upgrades(self):
        with patch.object(updates, "RELEASE_PUBLISHED", False), patch.object(updates, "urlopen") as fetch:
            self.assertEqual("unpublished", updates.update_info(self.workspace, force=True)["status"])
            self.assertFalse(updates.upgrade_plan()["supported"])
            with self.assertRaisesRegex(ValueError, "unverified"):
                updates.perform_upgrade(self.workspace)
            fetch.assert_not_called()

    def test_cache_and_explicit_opt_out(self):
        response = BytesIO(json.dumps({"info": {"version": "0.2.0"}}).encode())
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
