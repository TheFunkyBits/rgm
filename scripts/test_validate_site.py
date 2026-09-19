#!/usr/bin/env python3

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("validate-site.py")
MODULE_SPEC = importlib.util.spec_from_file_location("validate_site", MODULE_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError(f"Cannot load {MODULE_PATH}")
VALIDATOR = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(VALIDATOR)


def approved_payloads() -> dict[str, bytes]:
    catalog = {
        "titles": [
            {
                "id": "moo1",
                "offers": copy.deepcopy(VALIDATOR.TEST_CATALOG_OFFERS),
                "media": copy.deepcopy(VALIDATOR.TEST_CATALOG_MEDIA),
            }
        ]
    }
    payloads = {path: b"fixture" for path in VALIDATOR.TEST_CATALOG_PATHS}
    payloads["catalog.json"] = json.dumps(catalog).encode("utf-8")
    return payloads


def mutate_catalog(payloads: dict[str, bytes], mutation) -> None:
    catalog = json.loads(payloads["catalog.json"].decode("utf-8"))
    mutation(catalog["titles"][0])
    payloads["catalog.json"] = json.dumps(catalog).encode("utf-8")


class TestMoo1CatalogPolicy(unittest.TestCase):
    def test_accepts_approved_inventory_offer_and_media(self) -> None:
        VALIDATOR.validate_test_catalog(approved_payloads())

    def test_rejects_reordered_gallery(self) -> None:
        payloads = approved_payloads()
        mutate_catalog(
            payloads,
            lambda title: title["media"]["gallery"].reverse(),
        )

        with self.assertRaisesRegex(SystemExit, "media differs"):
            VALIDATOR.validate_test_catalog(payloads)

    def test_rejects_changed_offer(self) -> None:
        payloads = approved_payloads()
        mutate_catalog(
            payloads,
            lambda title: title["offers"][0].update({"url": "https://example.invalid/moo1"}),
        )

        with self.assertRaisesRegex(SystemExit, "offer differs"):
            VALIDATOR.validate_test_catalog(payloads)

    def test_rejects_stripped_media(self) -> None:
        payloads = approved_payloads()
        mutate_catalog(payloads, lambda title: title.update({"media": None}))

        with self.assertRaisesRegex(SystemExit, "media differs"):
            VALIDATOR.validate_test_catalog(payloads)

    def test_rejects_extra_logical_path(self) -> None:
        payloads = approved_payloads()
        payloads["moo1/unapproved.png"] = b"fixture"

        with self.assertRaisesRegex(SystemExit, "logical path inventory differs"):
            VALIDATOR.validate_test_catalog(payloads)


class TestPublicationIdentity(unittest.TestCase):
    def test_accepts_canonical_site_base_and_schema_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            self._write_identity_schemas(site)

            VALIDATOR.validate_publication_identity(site)

    def test_rejects_weakened_current_binding_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            self._write_identity_schemas(
                site,
                binding_pattern=r"^https://catalog.example/catalog/v[1-9][0-9]*/index\.json$",
            )

            with self.assertRaisesRegex(SystemExit, "binding schema is invalid"):
                VALIDATOR.validate_publication_identity(site)

    def test_rejects_unrecognized_root_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            html = site / "index.html"
            with self.assertRaisesRegex(SystemExit, "Unexpected root-relative reference"):
                VALIDATOR.resolve_local_reference(site, html, "/unrelated/privacy/")

    @staticmethod
    def _write_identity_schemas(site: Path, binding_pattern: str | None = None) -> None:
        required_fields = {
            "spec/catalog-v4/index.schema.json": [
                "schemaVersion",
                "catalogVersion",
                "minimumAppVersionCode",
                "files",
            ],
            "spec/catalog-v4/signatures.schema.json": [
                "schemaVersion",
                "manifestSha256",
                "signatures",
            ],
            "spec/catalog-v4/binding.schema.json": [
                "schemaVersion",
                "indexUrl",
                "catalogVersion",
                "keyId",
                "indexSha256",
                "signatureEnvelopeSha256",
            ],
        }
        for relative, schema_id in VALIDATOR.SCHEMA_IDS.items():
            path = site / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            document = {"$id": schema_id}
            if relative in required_fields:
                document["required"] = required_fields[relative]
            if relative == "spec/catalog-v4/binding.schema.json":
                document["additionalProperties"] = False
                document["properties"] = {
                    "schemaVersion": {"const": 2},
                    "indexUrl": {
                        "pattern": (
                            binding_pattern
                            or VALIDATOR.CATALOG_BINDING_INDEX_URL_PATTERN
                        )
                    },
                }
            path.write_text(json.dumps(document), encoding="utf-8")


class TestCatalogReservation(unittest.TestCase):
    def test_accepts_strict_reserved_v3_without_a_release_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            expected = self.reservation()
            self.write_reservation(repository, expected)

            self.assertEqual(expected, VALIDATOR.validate_catalog_reservation(repository, 3))

    def test_rejects_published_tree_for_a_reserved_catalog_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            self.write_reservation(repository, self.reservation())
            (repository / "site/catalog/v3").mkdir(parents=True)

            with self.assertRaisesRegex(SystemExit, "must not have a published tree"):
                VALIDATOR.validate_catalog_reservation(repository, 3)

    @staticmethod
    def reservation() -> dict:
        return {
            "schemaVersion": 2,
            "catalogVersion": 3,
            "state": "reserved",
            "reason": "fixture",
            "indexSha256": "1" * 64,
            "signatureEnvelopeSha256": "2" * 64,
        }

    @staticmethod
    def write_reservation(repository: Path, reservation: dict) -> None:
        path = repository / "catalog-reservations/v3.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(reservation), encoding="utf-8")


class TestCurrentCatalogReleaseRecord(unittest.TestCase):
    def feed(self) -> dict:
        return {
            "catalogVersion": 4,
            "indexSha256": "1" * 64,
            "signatureEnvelopeSha256": "2" * 64,
            "keyId": "test-key",
            "files": [
                {
                    "path": "catalog.json",
                    "objectPath": "objects/sha256/3" + "3" * 63,
                    "bytes": 1,
                    "sha256": "3" * 64,
                }
            ],
            "minimumAppVersionCode": 7,
        }

    def record(self) -> dict:
        return {
            "schemaVersion": 3,
            "catalogVersion": 4,
            "canonicalPath": "catalog/v4",
            "indexUrl": "https://thefunkybits.github.io/rgm/catalog/v4/index.json",
            "indexSchemaVersion": 4,
            "keyId": "test-key",
            "indexSha256": "1" * 64,
            "signatureEnvelopeSha256": "2" * 64,
            "files": self.feed()["files"],
            "minimumAppVersionCode": 7,
            "catalogCandidateSha256": "4" * 64,
            "contentCommit": "5" * 40,
            "publisherCommit": "6" * 40,
            "publicationCommit": "7" * 40,
            "tagNames": {
                "content": "catalog/v4-content",
                "publisher": "catalog/v4-publisher",
                "release": "catalog/v4",
            },
        }

    def test_accepts_a_current_release_record_bound_to_the_signed_feed(self) -> None:
        record = self.record()

        self.assertEqual(
            record,
            VALIDATOR.validate_current_release_record(record, self.feed(), "fixture"),
        )

    def test_rejects_release_record_hash_drift(self) -> None:
        record = self.record()
        record["indexSha256"] = "f" * 64

        with self.assertRaisesRegex(SystemExit, "differs from signed feed"):
            VALIDATOR.validate_current_release_record(record, self.feed(), "fixture")


class TestCurrentCatalogFeed(unittest.TestCase):
    def test_accepts_a_current_signed_feed_with_exact_object_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            self.write_feed(site)

            with mock.patch.object(VALIDATOR, "verify_signature"):
                feed = VALIDATOR.validate_current_release_feed(
                    site,
                    "catalog/v4",
                    4,
                    "openssl",
                    {"test-key": b"k" * 32},
                )

            self.assertEqual(4, feed["catalogVersion"])
            self.assertEqual("test-key", feed["keyId"])
            self.assertEqual(1, len(feed["files"]))

    def test_rejects_unreferenced_current_feed_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            feed_root = self.write_feed(site)
            (feed_root / "objects/sha256/unreferenced").write_bytes(b"extra")

            with mock.patch.object(VALIDATOR, "verify_signature"):
                with self.assertRaisesRegex(SystemExit, "object directory differs"):
                    VALIDATOR.validate_current_release_feed(
                        site,
                        "catalog/v4",
                        4,
                        "openssl",
                        {"test-key": b"k" * 32},
                    )

    @staticmethod
    def write_feed(site: Path) -> Path:
        feed_root = site / "catalog/v4"
        payload = b"catalog"
        digest = VALIDATOR.sha256(payload)
        object_path = feed_root / f"objects/sha256/{digest}"
        object_path.parent.mkdir(parents=True)
        object_path.write_bytes(payload)
        index = {
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
        }
        index_path = feed_root / "index.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        envelope = {
            "schemaVersion": 2,
            "manifestSha256": VALIDATOR.sha256(index_path.read_bytes()),
            "signatures": [
                {"algorithm": "ed25519", "keyId": "test-key", "value": "A" * 86}
            ],
        }
        (feed_root / "index.signatures.json").write_text(json.dumps(envelope), encoding="utf-8")
        return feed_root


class TestCurrentCatalogDeploymentReceipt(unittest.TestCase):
    def record(self) -> dict:
        return {
            "catalogVersion": 4,
            "files": [
                {
                    "path": "catalog.json",
                    "objectPath": "objects/sha256/1" + "1" * 63,
                    "bytes": 1,
                    "sha256": "1" * 64,
                }
            ],
        }

    def receipt(self, release_record_sha256: str) -> dict:
        return {
            "schemaVersion": 2,
            "catalogVersion": 4,
            "releaseRecordSha256": release_record_sha256,
            "sourceCommit": "2" * 40,
            "site": {
                "repository": "https://github.com/TheFunkyBits/rgm",
                "pagesUrl": "https://thefunkybits.github.io/rgm/",
                "workflowRunId": 1,
                "workflowUrl": "https://github.com/TheFunkyBits/rgm/actions/runs/1",
                "createdAt": "2026-09-18T00:00:00Z",
                "completedAt": "2026-09-18T00:01:00Z",
                "conclusion": "success",
                "servedFileCount": 3,
                "byteEqualityVerified": True,
            },
        }

    def test_accepts_a_receipt_bound_to_current_release_record_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record_path = Path(temporary) / "v4.json"
            record_path.write_text(json.dumps(self.record()), encoding="utf-8")
            receipt = self.receipt(VALIDATOR.sha256(record_path.read_bytes()))

            self.assertEqual(
                receipt,
                VALIDATOR.validate_current_deployment_receipt(
                    receipt,
                    record_path,
                    self.record(),
                    "fixture",
                ),
            )

    def test_rejects_a_receipt_with_release_record_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record_path = Path(temporary) / "v4.json"
            record_path.write_text(json.dumps(self.record()), encoding="utf-8")
            receipt = self.receipt("f" * 64)

            with self.assertRaisesRegex(SystemExit, "release record hash differs"):
                VALIDATOR.validate_current_deployment_receipt(
                    receipt,
                    record_path,
                    self.record(),
                    "fixture",
                )


class TestCurrentCatalogDeploymentSource(unittest.TestCase):
    def test_accepts_current_release_bytes_matching_the_receipt_source_commit(self) -> None:
        repository, site, record_path, record, receipt, git = self.fixture()

        VALIDATOR.verify_current_deployment_source(site, record_path, record, receipt, git)

    def test_rejects_current_release_bytes_that_drift_from_the_receipt_source_commit(self) -> None:
        repository, site, record_path, record, receipt, git = self.fixture()
        (site / "catalog/v4/index.json").write_bytes(b"changed\n")

        with self.assertRaisesRegex(SystemExit, "source bytes differ"):
            VALIDATOR.verify_current_deployment_source(site, record_path, record, receipt, git)

    def fixture(self) -> tuple[Path, Path, Path, dict, dict, str]:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        repository = Path(temporary.name)
        site = repository / "site"
        record_path = repository / "catalog-releases/v4.json"
        record = {"catalogVersion": 4, "canonicalPath": "catalog/v4"}
        record_path.parent.mkdir(parents=True)
        record_path.write_text(json.dumps(record), encoding="utf-8")
        catalog_root = site / "catalog/v4/objects/sha256"
        catalog_root.mkdir(parents=True)
        (site / "catalog/v4/index.json").write_bytes(b"index\n")
        (site / "catalog/v4/index.signatures.json").write_bytes(b"signature\n")
        (catalog_root / "object").write_bytes(b"object\n")
        git = shutil.which("git")
        self.assertIsNotNone(git)
        self.git(repository, git, "init")
        self.git(repository, git, "config", "user.name", "RGM Test")
        self.git(repository, git, "config", "user.email", "rgm-test@example.invalid")
        self.git(repository, git, "add", ".")
        self.git(repository, git, "commit", "-m", "current release")
        source_commit = self.git(repository, git, "rev-parse", "HEAD")
        receipt = {"sourceCommit": source_commit}
        return repository, site, record_path, record, receipt, git

    def git(self, repository: Path, git: str, *arguments: str) -> str:
        result = subprocess.run(
            (git, "-C", str(repository), *arguments),
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
        return result.stdout.decode().strip()


class TestRetiredCatalogAbsence(unittest.TestCase):
    def test_accepts_no_current_material_for_a_retired_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            site = repository / "site"

            VALIDATOR.validate_retired_catalog_absence(repository, site, 7)

    def test_rejects_a_retired_catalog_tree_remaining_in_the_current_site(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            site = repository / "site"
            (site / "catalog/v7").mkdir(parents=True)

            with self.assertRaisesRegex(SystemExit, "retired catalog v7 remains"):
                VALIDATOR.validate_retired_catalog_absence(repository, site, 7)


class TestDeprecationRecordBinding(unittest.TestCase):
    def test_accepts_a_deprecation_event_bound_to_exact_record_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            record_path = repository / "catalog-releases/v7.json"
            record_path.parent.mkdir(parents=True)
            record_path.write_bytes(b"record\n")
            self.write_event(repository, hashlib.sha256(record_path.read_bytes()).hexdigest())

            VALIDATOR.validate_deprecation_record_binding(repository, 7, record_path)

    def test_rejects_a_deprecation_event_with_record_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            record_path = repository / "catalog-releases/v7.json"
            record_path.parent.mkdir(parents=True)
            record_path.write_bytes(b"record\n")
            self.write_event(repository, "f" * 64)

            with self.assertRaisesRegex(SystemExit, "record hash differs"):
                VALIDATOR.validate_deprecation_record_binding(repository, 7, record_path)

    @staticmethod
    def write_event(repository: Path, release_record_sha256: str) -> None:
        event = {
            "schemaVersion": 1,
            "eventSequence": 1,
            "state": "DEPRECATED",
            "catalogVersion": 7,
            "canonicalPath": "catalog/v7",
            "releaseRecordSha256": release_record_sha256,
            "predecessorEventSha256": None,
            "successorCatalogVersion": 8,
            "successorCanonicalPath": "catalog/v8",
            "announcedAt": "2026-09-18T00:00:00Z",
            "removalNotBefore": "2026-10-18T00:00:00Z",
            "supportedClientCutoff": "8+",
            "rationale": "fixture",
            "sourceCommit": "a" * 40,
            "publicationReceiptSha256": None,
        }
        path = repository / "catalog-lifecycle/v7/events/0001-deprecated.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(event), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
