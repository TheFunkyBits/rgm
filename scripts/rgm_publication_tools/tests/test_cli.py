from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).parents[3]
COMMAND = [sys.executable, "-m", "scripts.rgm_publication_tools"]


class PublicationToolsCliTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*COMMAND, *arguments],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_help_lists_catalog_promotion(self):
        result = self.run_cli("--help")

        self.assertEqual(0, result.returncode)
        self.assertIn("promote-external-catalog", result.stdout)
        self.assertEqual("", result.stderr)

    def test_catalog_promotion_uses_only_sealed_external_inputs(self):
        result = self.run_cli("promote-external-catalog", "--help")

        self.assertEqual(0, result.returncode)
        self.assertIn("--stage-directory", result.stdout)
        self.assertIn("--catalog-candidate", result.stdout)
        self.assertNotIn("--content-root", result.stdout)
        self.assertNotIn("--private-key", result.stdout)
        self.assertEqual("", result.stderr)


if __name__ == "__main__":
    unittest.main()
