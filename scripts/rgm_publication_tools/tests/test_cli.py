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

    def test_help_lists_catalog_lifecycle_verification(self):
        result = self.run_cli("--help")

        self.assertEqual(0, result.returncode)
        self.assertIn("verify-catalog-lifecycle", result.stdout)
        self.assertEqual("", result.stderr)

        command_help = self.run_cli("verify-catalog-lifecycle", "--help")

        self.assertEqual(0, command_help.returncode)
        self.assertIn("--publication-root", command_help.stdout)
        self.assertIn("--git", command_help.stdout)
        self.assertNotIn("--credential", command_help.stdout)
        self.assertEqual("", command_help.stderr)

    def test_lifecycle_deprecation_commands_expose_only_local_inputs(self):
        result = self.run_cli("--help")

        self.assertEqual(0, result.returncode)
        self.assertIn("plan-catalog-deprecation", result.stdout)
        self.assertIn("prepare-catalog-deprecation", result.stdout)

        for command in ("plan-catalog-deprecation", "prepare-catalog-deprecation"):
            command_help = self.run_cli(command, "--help")

            self.assertEqual(0, command_help.returncode)
            self.assertIn("--catalog-version", command_help.stdout)
            self.assertIn("--successor-catalog-version", command_help.stdout)
            self.assertIn("--removal-not-before", command_help.stdout)
            self.assertNotIn("--publish", command_help.stdout)
            self.assertNotIn("--credential", command_help.stdout)
            self.assertEqual("", command_help.stderr)

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
