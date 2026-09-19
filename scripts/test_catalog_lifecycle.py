from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.catalog_lifecycle import (
    CatalogLifecycleState,
    LIFECYCLE_EVENT_FIELDS,
    scan_catalog_inventory,
    validate_next_catalog_version,
)


class CatalogLifecycleTest(unittest.TestCase):
    def test_event_schema_matches_the_strict_runtime_contract(self) -> None:
        schema_path = Path(__file__).with_name("catalog-lifecycle-event.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(1, schema["properties"]["schemaVersion"]["const"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(LIFECYCLE_EVENT_FIELDS), set(schema["required"]))

    def test_accepts_a_generic_append_only_deprecation_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            self.write_json(
                repository / "catalog-lifecycle/v7/events/0001-deprecated.json",
                self.deprecation_event(),
            )

            inventory = scan_catalog_inventory(repository, tag_names=())

            self.assertEqual(frozenset({7}), inventory.deprecated_versions)
            self.assertEqual(
                CatalogLifecycleState.DEPRECATED,
                inventory.classify_catalog_version(7),
            )
            self.assertEqual(7, inventory.high_water_version)

    def test_deprecated_release_remains_current_during_its_notice_period(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            self.write_json(
                repository / "catalog-releases/v7.json",
                {"catalogVersion": 7, "canonicalPath": "catalog/v7"},
            )
            self.write_json(
                repository / "catalog-lifecycle/v7/events/0001-deprecated.json",
                self.deprecation_event(),
            )

            inventory = scan_catalog_inventory(repository, tag_names=())

            self.assertEqual(frozenset({7}), inventory.active_versions)
            self.assertEqual(frozenset({7}), inventory.deprecated_versions)
            self.assertEqual(
                CatalogLifecycleState.DEPRECATED,
                inventory.classify_catalog_version(7),
            )

    def test_accepts_a_nonconsecutive_successor_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            event = self.deprecation_event()
            event.update(
                {
                    "catalogVersion": 5,
                    "canonicalPath": "catalog/v5",
                    "successorCatalogVersion": 8,
                    "successorCanonicalPath": "catalog/v8",
                }
            )
            self.write_json(
                repository / "catalog-lifecycle/v5/events/0001-deprecated.json",
                event,
            )

            inventory = scan_catalog_inventory(repository, tag_names=())

            self.assertEqual(frozenset({5}), inventory.deprecated_versions)

    def test_rejects_a_removal_window_before_its_announcement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            event = self.deprecation_event()
            event["removalNotBefore"] = "2026-09-17T00:00:00Z"
            self.write_json(
                repository / "catalog-lifecycle/v7/events/0001-deprecated.json",
                event,
            )

            with self.assertRaisesRegex(ValueError, "removal-not-before time must follow"):
                scan_catalog_inventory(repository, tag_names=())

    def test_rejects_an_event_with_the_wrong_predecessor_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            first_path = repository / "catalog-lifecycle/v7/events/0001-deprecated.json"
            self.write_json(first_path, self.deprecation_event())
            second_event = self.deprecation_event()
            second_event.update(
                {
                    "eventSequence": 2,
                    "predecessorEventSha256": "0" * 64,
                    "publicationReceiptSha256": "c" * 64,
                }
            )
            self.write_json(
                repository / "catalog-lifecycle/v7/events/0002-deprecated.json",
                second_event,
            )

            with self.assertRaisesRegex(ValueError, "predecessor hash is invalid"):
                scan_catalog_inventory(repository, tag_names=())

    def test_accepts_a_deprecation_receipt_as_a_later_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            first_path = repository / "catalog-lifecycle/v7/events/0001-deprecated.json"
            self.write_json(first_path, self.deprecation_event())
            receipt_event = self.deprecation_event()
            receipt_event.update(
                {
                    "eventSequence": 2,
                    "predecessorEventSha256": hashlib.sha256(first_path.read_bytes()).hexdigest(),
                    "publicationReceiptSha256": "c" * 64,
                }
            )
            self.write_json(
                repository / "catalog-lifecycle/v7/events/0002-deprecated.json",
                receipt_event,
            )

            inventory = scan_catalog_inventory(repository, tag_names=())

            self.assertEqual(frozenset({7}), inventory.deprecated_versions)

    def test_rejects_a_terminal_event_left_in_the_current_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            first_path = repository / "catalog-lifecycle/v7/events/0001-deprecated.json"
            self.write_json(first_path, self.deprecation_event())
            terminal_event = self.deprecation_event()
            terminal_event.update(
                {
                    "eventSequence": 2,
                    "state": "REMOVED_TO_GIT_HISTORY",
                    "predecessorEventSha256": hashlib.sha256(first_path.read_bytes()).hexdigest(),
                    "publicationReceiptSha256": "c" * 64,
                }
            )
            self.write_json(
                repository / "catalog-lifecycle/v7/events/0002-removed-to-git-history.json",
                terminal_event,
            )

            with self.assertRaisesRegex(ValueError, "terminal lifecycle records"):
                scan_catalog_inventory(repository, tag_names=())

    def test_retirement_tags_raise_high_water_without_a_current_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            self.write_json(
                repository / "catalog-releases/v4.json",
                {"catalogVersion": 4, "canonicalPath": "catalog/v4"},
            )
            self.write_json(
                repository / "catalog-reservations/v3.json",
                {"catalogVersion": 3, "state": "reserved"},
            )

            inventory = scan_catalog_inventory(
                repository,
                tag_names=("catalog-v7-final-served", "catalog-v7-removed"),
            )

            self.assertEqual(frozenset({4}), inventory.active_versions)
            self.assertEqual(frozenset({3}), inventory.reserved_versions)
            self.assertEqual(frozenset({7}), inventory.retired_versions)
            self.assertEqual(7, inventory.high_water_version)
            self.assertEqual(
                CatalogLifecycleState.ACTIVE,
                inventory.classify_catalog_version(4),
            )
            self.assertEqual(
                CatalogLifecycleState.REMOVED_TO_GIT_HISTORY,
                inventory.classify_catalog_version(7),
            )
            self.assertFalse((repository / "catalog-lifecycle/v7").exists())

            with self.assertRaisesRegex(ValueError, "must exceed catalog high water 7"):
                validate_next_catalog_version(inventory, 7)

            validate_next_catalog_version(inventory, 8)

    def test_final_served_tag_marks_a_pending_terminal_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inventory = scan_catalog_inventory(
                Path(temporary),
                tag_names=("catalog-v7-final-served",),
            )

            self.assertEqual(frozenset({7}), inventory.pending_retirement_versions)
            self.assertEqual(frozenset(), inventory.retired_versions)
            self.assertEqual(7, inventory.high_water_version)

    def test_ignores_nonversioned_reservation_fixture_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "catalog-reservations/test").mkdir(parents=True)
            self.write_json(
                repository / "catalog-reservations/v3.json",
                {"catalogVersion": 3, "state": "reserved"},
            )

            inventory = scan_catalog_inventory(repository, tag_names=())

            self.assertEqual(frozenset({3}), inventory.reserved_versions)

    @staticmethod
    def deprecation_event() -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "eventSequence": 1,
            "state": "DEPRECATED",
            "catalogVersion": 7,
            "canonicalPath": "catalog/v7",
            "releaseRecordSha256": "a" * 64,
            "predecessorEventSha256": None,
            "successorCatalogVersion": 8,
            "successorCanonicalPath": "catalog/v8",
            "announcedAt": "2026-09-18T00:00:00Z",
            "removalNotBefore": "2026-10-18T00:00:00Z",
            "supportedClientCutoff": "7+",
            "rationale": "fixture",
            "sourceCommit": "b" * 40,
            "publicationReceiptSha256": None,
        }

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
