#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("validate_current_tree.py")
MODULE_SPEC = importlib.util.spec_from_file_location("validate_current_tree", MODULE_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError(f"Cannot load {MODULE_PATH}")
VALIDATOR = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(VALIDATOR)


def retired_host_value() -> str:
    return "-".join(("rgm", "publication"))


def retired_privacy_value() -> str:
    return "-".join(("rgm", "privacy"))


def retired_title_path_value() -> str:
    return "-".join(("three", "in", "a", "row"))


def retired_title_text_value() -> str:
    return " ".join(("Three", "in", "a", "Row"))


def retired_short_value() -> str:
    return "".join(("de", "mo"))


class CurrentTreeFixture:
    def __init__(self, temporary: Path) -> None:
        self.workspace = temporary / "workspace"
        self.container = self.workspace / "rgm"
        self.roots: dict[str, Path] = {}
        for name, leaf in VALIDATOR.REPOSITORY_LEAVES:
            root = self.container / leaf
            root.mkdir(parents=True, exist_ok=True)
            self.roots[name] = root
        for relative in VALIDATOR.WORKSPACE_FILES:
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("current\n", encoding="utf-8")
        self.allowlist = self.roots["rgm"] / "catalog-history" / "retired-content-allowlist.json"
        self.allowlist.parent.mkdir(parents=True, exist_ok=True)
        self.write_allowlist([])

    def repository(self, name: str) -> Path:
        return self.roots[name]

    def write_allowlist(self, entries: list[dict[str, object]]) -> None:
        self.allowlist.write_text(
            json.dumps({"schemaVersion": 1, "entries": entries}) + "\n",
            encoding="utf-8",
        )

    def initialize_git(self, root: Path) -> None:
        self.run(root, "init")
        self.run(root, "config", "user.email", "fixture@example.invalid")
        self.run(root, "config", "user.name", "Fixture")

    def commit(self, root: Path) -> str:
        self.run(root, "add", ".")
        self.run(root, "commit", "-m", "fixture")
        return self.run(root, "rev-parse", "HEAD").strip()

    def entry(self, repository: str, relative: str, category: str = "retired-title") -> dict[str, object]:
        root = self.repository(repository)
        commit = self.run(root, "rev-parse", "HEAD").strip()
        blob = self.run(root, "rev-parse", f"{commit}:{relative}").strip()
        payload = self.run_bytes(root, "show", f"{commit}:{relative}")
        return {
            "repository": repository,
            "path": relative,
            "sourceCommit": commit,
            "blobId": blob,
            "bytes": len(payload),
            "sha256": VALIDATOR.sha256(payload),
            "category": category,
            "reason": "immutable historical record",
        }

    @staticmethod
    def run(root: Path, *arguments: str) -> str:
        return CurrentTreeFixture.run_bytes(root, *arguments).decode("utf-8")

    @staticmethod
    def run_bytes(root: Path, *arguments: str) -> bytes:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode("utf-8"))
        return completed.stdout


class ValidateCurrentTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = CurrentTreeFixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def validate(self, require_final_origin: bool = False) -> dict[str, object]:
        return VALIDATOR.validate_current_tree(
            workspace_root=self.fixture.workspace,
            allowlist_path=self.fixture.allowlist,
            require_final_origin=require_final_origin,
        )

    def test_accepts_a_clean_canonical_layout(self) -> None:
        result = self.validate()

        self.assertTrue(result["valid"])
        self.assertEqual(6, len(result["repositories"]))
        self.assertEqual(0, result["historicalEntries"])

    def test_rejects_retired_host_bytes_in_an_untracked_file(self) -> None:
        (self.fixture.repository("rgm-client") / ".pending").write_text(
            retired_host_value(),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(VALIDATOR.CurrentTreeError, "retired-host"):
            self.validate()

    def test_rejects_retired_identity_in_a_path_component(self) -> None:
        target = self.fixture.repository("rgm-content") / retired_title_path_value()
        target.mkdir()
        (target / "current.txt").write_text("current", encoding="utf-8")

        with self.assertRaisesRegex(VALIDATOR.CurrentTreeError, "retired-title in path"):
            self.validate()

    def test_allows_an_exact_historical_title_record(self) -> None:
        root = self.fixture.repository("rgm-docs")
        self.fixture.initialize_git(root)
        relative = "design/history.md"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(retired_title_text_value(), encoding="utf-8")
        self.fixture.commit(root)
        self.fixture.write_allowlist([self.fixture.entry("rgm-docs", relative)])

        result = self.validate()

        self.assertTrue(result["valid"])
        self.assertEqual(1, result["historicalEntries"])

    def test_rejects_a_changed_historical_title_record(self) -> None:
        root = self.fixture.repository("rgm-docs")
        self.fixture.initialize_git(root)
        relative = "design/history.md"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(retired_title_text_value(), encoding="utf-8")
        self.fixture.commit(root)
        self.fixture.write_allowlist([self.fixture.entry("rgm-docs", relative)])
        path.write_text(retired_title_text_value() + " updated", encoding="utf-8")

        with self.assertRaisesRegex(VALIDATOR.CurrentTreeError, "uncommitted changes"):
            self.validate()

    def test_rejects_an_invalid_historical_category(self) -> None:
        root = self.fixture.repository("rgm-docs")
        self.fixture.initialize_git(root)
        relative = "design/history.md"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(retired_title_text_value(), encoding="utf-8")
        self.fixture.commit(root)
        self.fixture.write_allowlist([self.fixture.entry("rgm-docs", relative, "other")])

        with self.assertRaisesRegex(VALIDATOR.CurrentTreeError, "invalid identity"):
            self.validate()

    def test_optional_privacy_root_is_scanned(self) -> None:
        privacy = self.fixture.container / "privacy"
        privacy.mkdir()
        (privacy / "current.txt").write_text(retired_privacy_value(), encoding="utf-8")

        with self.assertRaisesRegex(VALIDATOR.CurrentTreeError, "retired-privacy"):
            self.validate()

    def test_final_public_origin_is_checked_on_demand(self) -> None:
        root = self.fixture.repository("rgm")
        self.fixture.initialize_git(root)
        self.fixture.run(root, "remote", "add", "origin", "https://github.com/TheFunkyBits/rgm.git")

        self.assertTrue(self.validate(require_final_origin=True)["valid"])

        self.fixture.run(root, "remote", "set-url", "origin", "https://example.invalid/other.git")
        with self.assertRaisesRegex(VALIDATOR.CurrentTreeError, "Public origin is not final"):
            self.validate(require_final_origin=True)

    def test_short_retired_word_uses_a_word_boundary(self) -> None:
        path = self.fixture.repository("rgm-client") / "current.txt"
        path.write_text("demoscene", encoding="utf-8")

        self.assertTrue(self.validate()["valid"])

        path.write_text(retired_short_value(), encoding="utf-8")
        with self.assertRaisesRegex(VALIDATOR.CurrentTreeError, "retired-title"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
