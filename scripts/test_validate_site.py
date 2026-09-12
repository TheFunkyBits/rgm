#!/usr/bin/env python3

import copy
import importlib.util
import json
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
            for relative, schema_id in VALIDATOR.SCHEMA_IDS.items():
                path = site / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"$id": schema_id}), encoding="utf-8")

            VALIDATOR.validate_publication_identity(
                site,
                {"siteBaseUrl": VALIDATOR.SITE_BASE_URL},
            )

    def test_rejects_retired_site_base_and_root_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            html = site / "index.html"
            with self.assertRaisesRegex(SystemExit, "site base URL"):
                VALIDATOR.validate_publication_identity(
                    site,
                    {"siteBaseUrl": "https://thefunkybits.github.io/rgm/"},
                )
            with self.assertRaisesRegex(SystemExit, "Unexpected root-relative reference"):
                VALIDATOR.resolve_local_reference(site, html, "/rgm/privacy/")


if __name__ == "__main__":
    unittest.main()
