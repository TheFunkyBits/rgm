from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.rgm_publication_tools.catalog_promotion import (
    PromoteCatalogRequest,
    inventory,
    promote_external_catalog,
    sha256_file,
)


class CatalogPromotionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "promotion fixture with spaces"
        self.stage = self.root / "stage"
        self.publication = self.root / "publication"
        self.client = self.root / "client"
        self.candidate = self.root / "catalog-candidate.json"
        self.java = self.root / "fake-java"
        self.git = Path(shutil.which("git") or self.fail("git is required for this test"))
        self._write(self.client / "gradle/wrapper/gradle-wrapper.jar", b"wrapper")
        self._write(self.java, b"java")
        self._write_stage()
        self._init_publication()
        self.head = self.git_text("rev-parse", "HEAD")
        self.commits = {
            "content": "2" * 40,
            "client": "3" * 40,
            "publication": self.head,
        }
        self._write(self.candidate, b'{"fixture":true}\n')
        self.candidate_hash = sha256_file(self.candidate)
        self._write_stage_record()
        self.java_calls: list[list[str]] = []

    def tearDown(self):
        self.temporary.cleanup()

    def test_prepares_one_append_only_catalog_release_and_provenance_record(self):
        result = promote_external_catalog(self.request(), runner=self.runner)

        self.assertEqual("prepared", result["status"])
        self.assertEqual(4, result["catalogVersion"])
        self.assertEqual(4, self.read_json(self.catalog_root / "v4/index.json")["catalogVersion"])
        record = self.read_json(self.publication / "catalog-releases/v4.json")
        self.assertEqual(3, record["schemaVersion"])
        self.assertEqual("catalog/v4", record["canonicalPath"])
        self.assertEqual("https://thefunkybits.github.io/rgm/catalog/v4/index.json", record["indexUrl"])
        self.assertEqual(4, record["indexSchemaVersion"])
        self.assertEqual(self.read_json(self.stage / "v4/index.json")["files"], record["files"])
        self.assertEqual(self.commits["content"], record["contentCommit"])
        self.assertEqual(self.commits["client"], record["publisherCommit"])
        self.assertEqual(self.commits["publication"], record["publicationCommit"])
        self.assertEqual(
            {
                "content": "catalog/v4-content",
                "publisher": "catalog/v4-publisher",
                "release": "catalog/v4",
            },
            record["tagNames"],
        )
        self.assertNotIn("objectCount", record)
        self.assertNotIn("sourceCommits", record)
        self.assertEqual(self.head, self.git_text("rev-parse", "HEAD"))
        status = self.git_text("status", "--porcelain=v1", "--untracked-files=all")
        self.assertIn("site/catalog/v4/index.json", status)
        self.assertIn("catalog-releases/v4.json", status)
        self.assertEqual([], list(self.catalog_root.glob(".promotion-*")))

    def test_catalog_promotion_invokes_only_catalog_publisher(self):
        promote_external_catalog(self.request(), runner=self.runner)

        wrapper = self.client / "gradle/wrapper/gradle-wrapper.jar"
        classpath = self.client / "tools/catalog-publisher/build/install/catalog-publisher/lib/*"
        self.assertEqual(
            [
                [
                    str(self.java),
                    "-classpath",
                    str(wrapper),
                    "org.gradle.wrapper.GradleWrapperMain",
                    ":tools:catalog-publisher:installDist",
                    "--no-daemon",
                    "--console=plain",
                ],
                [
                    str(self.java),
                    "-classpath",
                    str(classpath),
                    "dev.thefunkybits.rgm.catalogpublisher.MainKt",
                    "verify-catalog-candidate",
                    "--candidate",
                    str(self.candidate),
                ],
            ],
            self.java_calls,
        )

    def test_existing_catalog_version_is_refused(self):
        self._write(self.catalog_root / "v4/index.json", b"old")
        self.git_command("add", ".")
        self.git_command("commit", "-m", "existing release")
        self.commits["publication"] = self.git_text("rev-parse", "HEAD")
        self._write_stage_record()

        with self.assertRaisesRegex(ValueError, "v4 already exists"):
            promote_external_catalog(self.request(), runner=self.runner)

        self.assertEqual("old", (self.catalog_root / "v4/index.json").read_text())

    def test_reserved_catalog_version_is_refused(self):
        self.write_json_atomic(
            self.publication / "catalog-reservations/v4.json",
            reservation(4),
        )
        self.git_command("add", ".")
        self.git_command("commit", "-m", "reserve version")
        self.commits["publication"] = self.git_text("rev-parse", "HEAD")
        self._write_stage_record()

        with self.assertRaisesRegex(ValueError, "v4 is permanently reserved"):
            promote_external_catalog(self.request(), runner=self.runner)

        self.assertFalse((self.catalog_root / "v4").exists())

    def test_dirty_publication_is_refused_before_promotion(self):
        self._write(self.publication / "unrelated.txt", b"dirty")

        with self.assertRaisesRegex(ValueError, "requires a clean publication worktree"):
            promote_external_catalog(self.request(), runner=self.runner)

        self.assertFalse((self.catalog_root / "v4").exists())

    def request(self):
        return PromoteCatalogRequest(
            stage_directory=self.stage,
            catalog_candidate=self.candidate,
            publication_root=self.publication,
            client_root=self.client,
            java=self.java,
            git=self.git,
        )

    def runner(self, arguments, *, cwd, stdin=None):
        command = [str(argument) for argument in arguments]
        if command[0] == str(self.git):
            return subprocess.run(
                command,
                cwd=cwd,
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                check=False,
            )
        self.java_calls.append(command)
        if "verify-catalog-candidate" in command:
            verified = {
                "schemaVersion": 4,
                "candidateManifestSha256": self.candidate_hash,
                "catalogVersion": 4,
                "minimumAppVersionCode": 7,
                "sourceCommits": self.commits,
                "catalogBundle": "catalog-bundle",
            }
            return subprocess.CompletedProcess(command, 0, (json.dumps(verified) + "\n").encode(), b"")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def _write_stage(self):
        payload = b"catalog"
        pending = self.stage / "v4/objects/sha256/pending"
        self._write(pending, payload)
        digest = sha256_file(pending)
        pending.rename(pending.with_name(digest))
        self.write_json_atomic(
            self.stage / "v4/index.json",
            {
                "schemaVersion": 4,
                "catalogVersion": 4,
                "minimumAppVersionCode": 7,
                "files": [
                    {
                        "path": "catalog.json",
                        "objectPath": f"objects/sha256/{digest}",
                        "bytes": len(payload),
                        "sha256": digest,
                    }
                ],
            },
        )
        self.write_json_atomic(
            self.stage / "v4/index.signatures.json",
            {
                "schemaVersion": 2,
                "manifestSha256": sha256_file(self.stage / "v4/index.json"),
                "signatures": [{"algorithm": "ed25519", "keyId": "test-key", "value": "A" * 86}],
            },
        )

    def _write_stage_record(self):
        self.write_json_atomic(
            self.stage / "catalog-stage.json",
            {
                "schemaVersion": 4,
                "catalogCandidateSha256": self.candidate_hash,
                "catalogVersion": 4,
                "minimumAppVersionCode": 7,
                "sourceCommits": self.commits,
                "catalogDirectory": "v4",
                "indexSha256": sha256_file(self.stage / "v4/index.json"),
                "signatureEnvelopeSha256": sha256_file(self.stage / "v4/index.signatures.json"),
                "files": inventory(self.stage, excluded={"catalog-stage.json"}),
            },
        )

    def _init_publication(self):
        self._write(self.catalog_root / ".keep", b"")
        self._write(self.publication / "catalog-releases/.keep", b"")
        for arguments in (
            ("init",),
            ("config", "core.autocrlf", "false"),
            ("config", "user.name", "RGM Test"),
            ("config", "user.email", "rgm-test@example.invalid"),
            ("add", "."),
            ("commit", "-m", "initial release root"),
        ):
            self.git_command(*arguments)

    @property
    def catalog_root(self):
        return self.publication / "site/catalog"

    def git_command(self, *arguments):
        result = subprocess.run(
            [self.git, "-C", self.publication, *arguments],
            cwd=self.publication,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))

    def git_text(self, *arguments):
        result = subprocess.run(
            [self.git, "-C", self.publication, *arguments],
            cwd=self.publication,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
        return result.stdout.decode().strip()

    @staticmethod
    def read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def write_json_atomic(path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _write(path: Path, data: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def reservation(catalog_version: int) -> dict:
    return {
        "schemaVersion": 2,
        "catalogVersion": catalog_version,
        "state": "reserved",
        "reason": "fixture",
        "indexSha256": "a" * 64,
        "signatureEnvelopeSha256": "b" * 64,
    }


if __name__ == "__main__":
    unittest.main()
