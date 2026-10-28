import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        package = self.root / "src/ablation_workbench"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('__version__ = "1.2.3"\n')
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Release Test", "-c", "user.email=release@example.invalid", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        block = re.search(r"name: Verify release tag matches package version\n        run: \|\n(.*?)\n      - uses:", workflow, re.S)
        self.assertIsNotNone(block)
        self.script = textwrap.dedent(block.group(1))

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def verify(self, tag):
        return subprocess.run(["bash", "-c", self.script], cwd=self.root, env={**os.environ, "RELEASE_TAG": tag}, capture_output=True, text=True)

    def test_matching_version_and_checked_out_tag_can_publish(self):
        self.git("tag", "v1.2.3")
        result = self.verify("v1.2.3")
        self.assertEqual(0, result.returncode, result.stderr)

    def test_tag_with_different_package_version_cannot_publish(self):
        self.git("tag", "v1.2.4")
        self.assertNotEqual(0, self.verify("v1.2.4").returncode)

    def test_version_without_tag_at_checked_out_commit_cannot_publish(self):
        self.assertNotEqual(0, self.verify("v1.2.3").returncode)


if __name__ == "__main__":
    unittest.main()
