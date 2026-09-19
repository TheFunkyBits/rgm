from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import hashlib

from scripts.rgm_publication_tools.catalog_lifecycle import (
    CatalogDeprecationRequest,
    CatalogLifecycleVerificationRequest,
    plan_catalog_deprecation,
    prepare_catalog_deprecation,
    verify_catalog_lifecycle,
)


class CatalogLifecycleVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "publication fixture"
        self.git = Path(shutil.which("git") or self.fail("git is required for this test"))
        self.write_json(
            self.repository / "catalog-releases/v4.json",
            {"catalogVersion": 4, "canonicalPath": "catalog/v4"},
        )
        self.write_json(
            self.repository / "catalog-releases/v7.json",
            {"catalogVersion": 7, "canonicalPath": "catalog/v7"},
        )
        self.write_json(
            self.repository / "catalog-releases/v8.json",
            {"catalogVersion": 8, "canonicalPath": "catalog/v8"},
        )
        self.write_json(
            self.repository / "catalog-reservations/v3.json",
            {"catalogVersion": 3, "state": "reserved"},
        )
        (self.repository / "site/catalog/v7").mkdir(parents=True)
        self.git_command("init")
        self.git_command("config", "user.name", "RGM Test")
        self.git_command("config", "user.email", "rgm-test@example.invalid")
        self.git_command("add", ".")
        self.git_command("commit", "-m", "catalog lifecycle fixture")
        self.git_command("tag", "catalog-v6-final-served")
        self.git_command("tag", "catalog-v6-removed")
        self.site_validation_calls: list[tuple[Path, Path]] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_verifies_a_clean_inventory_under_the_publication_lock(self) -> None:
        result = verify_catalog_lifecycle(
            self.request(),
            site_validator=self.validate_site,
        )

        self.assertEqual("verified", result["status"])
        self.assertEqual([4, 7, 8], result["activeVersions"])
        self.assertEqual([3], result["reservedVersions"])
        self.assertEqual([], result["pendingRetirementVersions"])
        self.assertEqual([6], result["retiredVersions"])
        self.assertEqual(8, result["highWaterVersion"])
        self.assertEqual([(self.repository.resolve(), self.git.resolve())], self.site_validation_calls)

    def test_refuses_a_dirty_publication_worktree(self) -> None:
        (self.repository / "unrelated.txt").write_text("dirty", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "requires a clean publication worktree"):
            verify_catalog_lifecycle(
                self.request(),
                site_validator=self.validate_site,
            )
        self.assertEqual([], self.site_validation_calls)

    def test_plans_a_deprecation_without_mutating_the_worktree(self) -> None:
        planned = plan_catalog_deprecation(
            self.deprecation_request(),
            site_validator=self.validate_site,
        )

        release_record = self.repository / "catalog-releases/v7.json"
        self.assertEqual("planned", planned["status"])
        self.assertEqual(
            hashlib.sha256(release_record.read_bytes()).hexdigest(),
            planned["event"]["releaseRecordSha256"],
        )
        self.assertEqual("DEPRECATED", planned["event"]["state"])
        self.assertFalse((self.repository / "catalog-lifecycle/v7").exists())
        self.assertEqual("", self.git_text("status", "--porcelain=v1"))
        self.assertEqual([(self.repository.resolve(), self.git.resolve())], self.site_validation_calls)

    def test_prepares_the_same_validated_deprecation_event_atomically(self) -> None:
        planned = plan_catalog_deprecation(
            self.deprecation_request(),
            site_validator=self.validate_site,
        )
        prepared = prepare_catalog_deprecation(
            self.deprecation_request(),
            site_validator=self.validate_site,
        )

        event_path = self.repository / "catalog-lifecycle/v7/events/0001-deprecated.json"
        self.assertEqual("prepared", prepared["status"])
        self.assertEqual(planned["event"], prepared["event"])
        self.assertEqual(planned["event"], self.read_json(event_path))
        self.assertIn(
            "catalog-lifecycle/v7/events/0001-deprecated.json",
            self.git_text("status", "--porcelain=v1", "--untracked-files=all"),
        )
        self.assertEqual(
            [(self.repository.resolve(), self.git.resolve())] * 2,
            self.site_validation_calls,
        )

    def request(self) -> CatalogLifecycleVerificationRequest:
        return CatalogLifecycleVerificationRequest(
            publication_root=self.repository,
            git=self.git,
        )

    def deprecation_request(self) -> CatalogDeprecationRequest:
        return CatalogDeprecationRequest(
            publication_root=self.repository,
            git=self.git,
            catalog_version=7,
            successor_catalog_version=8,
            announced_at="2026-09-18T00:00:00Z",
            removal_not_before="2026-10-18T00:00:00Z",
            supported_client_cutoff="8+",
            rationale="fixture retirement",
        )

    def git_command(self, *arguments: str) -> None:
        result = subprocess.run(
            [self.git, "-C", self.repository, *arguments],
            cwd=self.repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))

    def git_text(self, *arguments: str) -> str:
        result = subprocess.run(
            [self.git, "-C", self.repository, *arguments],
            cwd=self.repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
        return result.stdout.decode()

    def validate_site(self, publication_root: Path, git: Path) -> None:
        self.site_validation_calls.append((publication_root, git))

    @staticmethod
    def read_json(path: Path) -> object:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
