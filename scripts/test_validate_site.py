#!/usr/bin/env python3

import copy
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
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


class TestLegacyCatalogRecord(unittest.TestCase):
    def record(self) -> dict:
        return {
            "schemaVersion": 2,
            "kind": "legacy-catalog-evidence",
            "legacyCatalogId": "test",
            "legacyRevision": 2,
            "catalogPath": "catalog/v2",
            "indexSchemaVersion": 2,
            "keyId": "test-key",
            "indexSha256": "1" * 64,
            "signatureEnvelopeSha256": "2" * 64,
            "files": [
                {
                    "path": "catalog.json",
                    "objectPath": "objects/sha256/3" + "3" * 63,
                    "bytes": 1,
                    "sha256": "3" * 64,
                }
            ],
            "minimumAppVersionCode": 2,
            "contentCommit": "4" * 40,
            "publisherCommit": "5" * 40,
            "publicationCommit": "6" * 40,
            "historicalSource": {
                "commit": "6" * 40,
                "path": "site/catalogs/v2/test",
                "tree": "7" * 40,
            },
        }

    def test_accepts_strict_legacy_catalog_record(self) -> None:
        record = self.record()

        self.assertEqual(record, VALIDATOR.validate_legacy_record(record, "fixture"))

    def test_rejects_catalog_version_in_legacy_record(self) -> None:
        record = self.record()
        record["catalogVersion"] = 2

        with self.assertRaisesRegex(SystemExit, "must not contain catalogVersion"):
            VALIDATOR.validate_legacy_record(record, "fixture")

    def test_rejects_reordered_legacy_inventory(self) -> None:
        record = self.record()
        record["files"].insert(
            0,
            {
                "path": "z.json",
                "objectPath": "objects/sha256/4" + "4" * 63,
                "bytes": 1,
                "sha256": "4" * 64,
            },
        )

        with self.assertRaisesRegex(SystemExit, "file inventory is invalid"):
            VALIDATOR.validate_legacy_record(record, "fixture")

    def test_rejects_invalid_historical_source_provenance(self) -> None:
        record = self.record()
        record["historicalSource"]["path"] = "site/catalogs/test/v2"

        with self.assertRaisesRegex(SystemExit, "source provenance is invalid"):
            VALIDATOR.validate_legacy_record(record, "fixture")


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


class TestHistoricalCatalogSource(unittest.TestCase):
    def test_current_flat_v2_bytes_match_the_recorded_historical_tree(self) -> None:
        repository, site, record, git = self.fixture()

        VALIDATOR.verify_historical_source(site, record, git)

        self.assertTrue((repository / "site/catalog/v2/index.json").is_file())

    def test_changed_current_flat_v2_bytes_are_rejected(self) -> None:
        _, site, record, git = self.fixture()
        (site / "catalog/v2/index.json").write_bytes(b"changed\n")

        with self.assertRaisesRegex(SystemExit, "historical source bytes differ"):
            VALIDATOR.verify_historical_source(site, record, git)

    def fixture(self) -> tuple[Path, Path, dict, str]:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        repository = Path(temporary.name)
        git = shutil.which("git")
        self.assertIsNotNone(git)
        source = repository / "site/catalogs/v2/test"
        source.mkdir(parents=True)
        (source / "index.json").write_bytes(b"index\n")
        (source / "objects/sha256").mkdir(parents=True)
        (source / "objects/sha256/object").write_bytes(b"object\n")
        self.git(repository, git, "init")
        self.git(repository, git, "config", "user.name", "RGM Test")
        self.git(repository, git, "config", "user.email", "rgm-test@example.invalid")
        self.git(repository, git, "add", ".")
        self.git(repository, git, "commit", "-m", "historical v2")
        commit = self.git(repository, git, "rev-parse", "HEAD")
        tree = self.git(repository, git, "rev-parse", f"{commit}:site/catalogs/v2/test")
        shutil.copytree(source, repository / "site/catalog/v2")
        return (
            repository,
            repository / "site",
            {
                "catalogPath": "catalog/v2",
                "historicalSource": {
                    "commit": commit,
                    "path": "site/catalogs/v2/test",
                    "tree": tree,
                },
            },
            git,
        )

    def git(self, repository: Path, git: str, *arguments: str) -> str:
        result = subprocess.run(
            (git, "-C", str(repository), *arguments),
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
        return result.stdout.decode().strip()


if __name__ == "__main__":
    unittest.main()
