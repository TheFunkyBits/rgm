from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable


CATALOG_RECORD_NAME = re.compile(r"^v([1-9][0-9]*)\.json$")
LIFECYCLE_DIRECTORY_NAME = re.compile(r"^v([1-9][0-9]*)$")
LIFECYCLE_EVENT_NAME = re.compile(
    r"^([0-9]{4,})-(deprecated|removed-to-git-history)\.json$"
)
RETIREMENT_TAG_NAME = re.compile(r"^catalog-v([1-9][0-9]*)-(final-served|removed)$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LIFECYCLE_EVENT_FIELDS = frozenset(
    {
        "schemaVersion",
        "eventSequence",
        "state",
        "catalogVersion",
        "canonicalPath",
        "releaseRecordSha256",
        "predecessorEventSha256",
        "successorCatalogVersion",
        "successorCanonicalPath",
        "announcedAt",
        "removalNotBefore",
        "supportedClientCutoff",
        "rationale",
        "sourceCommit",
        "publicationReceiptSha256",
    }
)


class CatalogLifecycleState(str, Enum):
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    REMOVED_TO_GIT_HISTORY = "REMOVED_TO_GIT_HISTORY"


@dataclass(frozen=True)
class CatalogInventory:
    active_versions: frozenset[int]
    reserved_versions: frozenset[int]
    deprecated_versions: frozenset[int]
    pending_retirement_versions: frozenset[int]
    retired_versions: frozenset[int]

    def __post_init__(self) -> None:
        terminal_versions = self.pending_retirement_versions | self.retired_versions
        if terminal_versions & (self.active_versions | self.deprecated_versions):
            raise ValueError("terminal catalog versions must not overlap current lifecycle states")
        if self.pending_retirement_versions & self.retired_versions:
            raise ValueError("pending and complete terminal retirement versions must not overlap")
        if self.reserved_versions & (
            self.active_versions | self.deprecated_versions | terminal_versions
        ):
            raise ValueError("catalog reservations must not overlap lifecycle states")

    @property
    def high_water_version(self) -> int:
        versions = (
            self.active_versions
            | self.reserved_versions
            | self.deprecated_versions
            | self.pending_retirement_versions
            | self.retired_versions
        )
        return max(versions, default=0)

    def classify_catalog_version(self, catalog_version: int) -> CatalogLifecycleState | None:
        if catalog_version in self.deprecated_versions:
            return CatalogLifecycleState.DEPRECATED
        if catalog_version in self.retired_versions:
            return CatalogLifecycleState.REMOVED_TO_GIT_HISTORY
        if catalog_version in self.active_versions:
            return CatalogLifecycleState.ACTIVE
        return None


def scan_catalog_inventory(repository: Path, *, tag_names: Iterable[str]) -> CatalogInventory:
    pending_retirement_versions, retired_versions = _retirement_versions(tag_names)
    return CatalogInventory(
        active_versions=_scan_active_versions(repository / "catalog-releases"),
        reserved_versions=_scan_reserved_versions(repository / "catalog-reservations"),
        deprecated_versions=_scan_deprecated_versions(repository / "catalog-lifecycle"),
        pending_retirement_versions=pending_retirement_versions,
        retired_versions=retired_versions,
    )


def validate_next_catalog_version(inventory: CatalogInventory, catalog_version: int) -> None:
    if type(catalog_version) is not int or catalog_version <= 0:
        raise ValueError("catalog version must be a positive integer")
    if catalog_version <= inventory.high_water_version:
        raise ValueError(
            f"catalog version {catalog_version} must exceed catalog high water "
            f"{inventory.high_water_version}"
        )


def _scan_active_versions(directory: Path) -> frozenset[int]:
    versions: set[int] = set()
    for path, catalog_version, record in _versioned_records(directory, "release"):
        if record.get("catalogVersion") != catalog_version:
            raise ValueError(f"{path}: catalogVersion does not match filename")
        if record.get("canonicalPath") != f"catalog/v{catalog_version}":
            raise ValueError(f"{path}: canonicalPath is invalid")
        versions.add(catalog_version)
    return frozenset(versions)


def _scan_reserved_versions(directory: Path) -> frozenset[int]:
    versions: set[int] = set()
    for path, catalog_version, record in _versioned_records(directory, "reservation"):
        if record.get("catalogVersion") != catalog_version:
            raise ValueError(f"{path}: catalogVersion does not match filename")
        if record.get("state") != "reserved":
            raise ValueError(f"{path}: reservation state is invalid")
        versions.add(catalog_version)
    return frozenset(versions)


def _scan_deprecated_versions(directory: Path) -> frozenset[int]:
    if not directory.exists():
        return frozenset()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"{directory}: lifecycle directory is invalid")

    deprecated_versions: set[int] = set()
    for version_directory in sorted(directory.iterdir()):
        if version_directory.is_symlink() or not version_directory.is_dir():
            raise ValueError(f"{version_directory}: lifecycle version directory is invalid")
        match = LIFECYCLE_DIRECTORY_NAME.fullmatch(version_directory.name)
        if match is None:
            raise ValueError(f"{version_directory}: lifecycle version directory name is invalid")
        catalog_version = int(match.group(1))
        final_state = _validate_lifecycle_events(version_directory, catalog_version)
        if final_state is CatalogLifecycleState.DEPRECATED:
            deprecated_versions.add(catalog_version)
        else:
            raise ValueError(
                f"{version_directory}: terminal lifecycle records must not remain in the current tree"
            )
    return frozenset(deprecated_versions)


def _validate_lifecycle_events(
    version_directory: Path,
    catalog_version: int,
) -> CatalogLifecycleState:
    entries = list(version_directory.iterdir())
    if [entry.name for entry in entries] != ["events"]:
        raise ValueError(f"{version_directory}: lifecycle version directory must contain only events")
    events_directory = version_directory / "events"
    if events_directory.is_symlink() or not events_directory.is_dir():
        raise ValueError(f"{events_directory}: lifecycle events directory is invalid")

    previous_hash: str | None = None
    previous_state: CatalogLifecycleState | None = None
    final_state: CatalogLifecycleState | None = None
    event_paths = sorted(events_directory.iterdir())
    if not event_paths:
        raise ValueError(f"{events_directory}: lifecycle event chain is empty")
    for expected_sequence, path in enumerate(event_paths, start=1):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{path}: lifecycle event is invalid")
        match = LIFECYCLE_EVENT_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"{path}: lifecycle event filename is invalid")
        if int(match.group(1)) != expected_sequence:
            raise ValueError(f"{path}: lifecycle event sequence is invalid")
        state_from_name = (
            CatalogLifecycleState.DEPRECATED
            if match.group(2) == "deprecated"
            else CatalogLifecycleState.REMOVED_TO_GIT_HISTORY
        )
        event_bytes = path.read_bytes()
        event = _read_lifecycle_event(path, event_bytes)
        state = CatalogLifecycleState(event["state"])
        if state is not state_from_name:
            raise ValueError(f"{path}: lifecycle event state does not match filename")
        _validate_lifecycle_event(
            path,
            event,
            expected_sequence,
            catalog_version,
            previous_hash,
            previous_state,
        )
        previous_hash = hashlib.sha256(event_bytes).hexdigest()
        previous_state = state
        final_state = state
    if final_state is None:
        raise ValueError(f"{events_directory}: lifecycle event chain is empty")
    return final_state


def _read_lifecycle_event(path: Path, event_bytes: bytes) -> dict[str, object]:
    try:
        event = json.loads(event_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{path}: lifecycle event is invalid: {error}") from error
    if not isinstance(event, dict) or set(event) != LIFECYCLE_EVENT_FIELDS:
        raise ValueError(f"{path}: lifecycle event fields are invalid")
    return event


def _validate_lifecycle_event(
    path: Path,
    event: dict[str, object],
    expected_sequence: int,
    catalog_version: int,
    previous_hash: str | None,
    previous_state: CatalogLifecycleState | None,
) -> None:
    if event["schemaVersion"] != 1 or event["eventSequence"] != expected_sequence:
        raise ValueError(f"{path}: lifecycle event version or sequence is invalid")
    if event["catalogVersion"] != catalog_version:
        raise ValueError(f"{path}: lifecycle event catalog version is invalid")
    if event["canonicalPath"] != f"catalog/v{catalog_version}":
        raise ValueError(f"{path}: lifecycle event canonical path is invalid")
    if not isinstance(event["releaseRecordSha256"], str) or not SHA256.fullmatch(
        event["releaseRecordSha256"]
    ):
        raise ValueError(f"{path}: lifecycle event release record hash is invalid")
    if event["predecessorEventSha256"] != previous_hash:
        raise ValueError(f"{path}: lifecycle event predecessor hash is invalid")
    successor_catalog_version = event["successorCatalogVersion"]
    if (
        type(successor_catalog_version) is not int
        or successor_catalog_version <= catalog_version
    ):
        raise ValueError(f"{path}: lifecycle event successor version is invalid")
    if event["successorCanonicalPath"] != f"catalog/v{successor_catalog_version}":
        raise ValueError(f"{path}: lifecycle event successor path is invalid")
    announced_at = _parse_utc_instant(event["announcedAt"], path, "announcement time")
    removal_not_before = _parse_utc_instant(
        event["removalNotBefore"],
        path,
        "removal-not-before time",
    )
    if removal_not_before <= announced_at:
        raise ValueError(f"{path}: lifecycle event removal-not-before time must follow announcement time")
    if not isinstance(event["supportedClientCutoff"], str) or not event["supportedClientCutoff"]:
        raise ValueError(f"{path}: lifecycle event supported-client cutoff is invalid")
    if not isinstance(event["rationale"], str) or not event["rationale"]:
        raise ValueError(f"{path}: lifecycle event rationale is invalid")
    if not isinstance(event["sourceCommit"], str) or not GIT_SHA.fullmatch(event["sourceCommit"]):
        raise ValueError(f"{path}: lifecycle event source commit is invalid")
    receipt_hash = event["publicationReceiptSha256"]
    if receipt_hash is not None and (
        not isinstance(receipt_hash, str) or not SHA256.fullmatch(receipt_hash)
    ):
        raise ValueError(f"{path}: lifecycle event publication receipt hash is invalid")

    state = CatalogLifecycleState(event["state"])
    if previous_state is None:
        if state is not CatalogLifecycleState.DEPRECATED:
            raise ValueError(f"{path}: lifecycle event chain must start deprecated")
    elif previous_state is CatalogLifecycleState.REMOVED_TO_GIT_HISTORY:
        raise ValueError(f"{path}: lifecycle event follows terminal removal")


def _parse_utc_instant(value: object, path: Path, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{path}: lifecycle event {label} is invalid")
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValueError(f"{path}: lifecycle event {label} is invalid") from error


def _versioned_records(directory: Path, label: str) -> Iterable[tuple[Path, int, dict[str, object]]]:
    if not directory.exists():
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"{directory}: {label} directory is invalid")

    records: list[tuple[Path, int, dict[str, object]]] = []
    for path in sorted(directory.glob("v*.json")):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{path}: {label} record is invalid")
        match = CATALOG_RECORD_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"{path}: {label} record filename is invalid")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"{path}: {label} record is invalid: {error}") from error
        if not isinstance(record, dict):
            raise ValueError(f"{path}: {label} record is not an object")
        records.append((path, int(match.group(1)), record))
    return tuple(records)


def _retirement_versions(tag_names: Iterable[str]) -> tuple[frozenset[int], frozenset[int]]:
    stages_by_version: dict[int, set[str]] = {}
    for tag_name in tag_names:
        match = RETIREMENT_TAG_NAME.fullmatch(tag_name)
        if match is None:
            continue
        catalog_version = int(match.group(1))
        stages_by_version.setdefault(catalog_version, set()).add(match.group(2))

    pending_retirement_versions: set[int] = set()
    retired_versions: set[int] = set()
    for catalog_version, stages in stages_by_version.items():
        if stages == {"final-served"}:
            pending_retirement_versions.add(catalog_version)
            continue
        if stages != {"final-served", "removed"}:
            raise ValueError(f"catalog v{catalog_version} retirement tags are incomplete")
        retired_versions.add(catalog_version)
    return frozenset(pending_retirement_versions), frozenset(retired_versions)
