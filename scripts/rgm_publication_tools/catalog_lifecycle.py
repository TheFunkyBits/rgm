"""Read-only catalog lifecycle verification under the publication lock."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import uuid

from scripts.catalog_lifecycle import scan_catalog_inventory

from .catalog_promotion import ProcessRunner, publication_lock


SiteValidator = Callable[[Path, Path], None]


@dataclass(frozen=True)
class CatalogLifecycleVerificationRequest:
    publication_root: Path
    git: Path


@dataclass(frozen=True)
class CatalogDeprecationRequest:
    publication_root: Path
    git: Path
    catalog_version: int
    successor_catalog_version: int
    announced_at: str
    removal_not_before: str
    supported_client_cutoff: str
    rationale: str


def add_verification_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--publication-root", type=Path, required=True)
    parser.add_argument("--git", type=Path, required=True)


def add_deprecation_arguments(parser: argparse.ArgumentParser) -> None:
    add_verification_arguments(parser)
    parser.add_argument("--catalog-version", type=int, required=True)
    parser.add_argument("--successor-catalog-version", type=int, required=True)
    parser.add_argument("--announced-at", required=True)
    parser.add_argument("--removal-not-before", required=True)
    parser.add_argument("--supported-client-cutoff", required=True)
    parser.add_argument("--rationale", required=True)


def verification_request_from_arguments(
    arguments: argparse.Namespace,
) -> CatalogLifecycleVerificationRequest:
    return CatalogLifecycleVerificationRequest(
        publication_root=arguments.publication_root,
        git=arguments.git,
    )


def deprecation_request_from_arguments(arguments: argparse.Namespace) -> CatalogDeprecationRequest:
    return CatalogDeprecationRequest(
        publication_root=arguments.publication_root,
        git=arguments.git,
        catalog_version=arguments.catalog_version,
        successor_catalog_version=arguments.successor_catalog_version,
        announced_at=arguments.announced_at,
        removal_not_before=arguments.removal_not_before,
        supported_client_cutoff=arguments.supported_client_cutoff,
        rationale=arguments.rationale,
    )


def verify_catalog_lifecycle(
    request: CatalogLifecycleVerificationRequest,
    *,
    runner: ProcessRunner | None = None,
    site_validator: SiteValidator | None = None,
) -> dict[str, object]:
    active_runner = runner or _run_process
    active_site_validator = site_validator or _validate_site
    publication_root = _require_directory(request.publication_root, "Publication repository")
    git = _require_regular_file(request.git, "Git executable")
    with publication_lock(publication_root, git, runner=active_runner):
        _require_clean_publication(publication_root, git, active_runner)
        active_site_validator(publication_root, git)
        tag_names = _git_text(git, publication_root, ("tag", "--list"), active_runner).splitlines()
        inventory = scan_catalog_inventory(publication_root, tag_names=tag_names)
    return {
        "status": "verified",
        "activeVersions": sorted(inventory.active_versions),
        "reservedVersions": sorted(inventory.reserved_versions),
        "deprecatedVersions": sorted(inventory.deprecated_versions),
        "pendingRetirementVersions": sorted(inventory.pending_retirement_versions),
        "retiredVersions": sorted(inventory.retired_versions),
        "highWaterVersion": inventory.high_water_version,
    }


def plan_catalog_deprecation(
    request: CatalogDeprecationRequest,
    *,
    runner: ProcessRunner | None = None,
    site_validator: SiteValidator | None = None,
) -> dict[str, object]:
    active_runner = runner or _run_process
    active_site_validator = site_validator or _validate_site
    publication_root = _require_directory(request.publication_root, "Publication repository")
    git = _require_regular_file(request.git, "Git executable")
    with publication_lock(publication_root, git, runner=active_runner):
        event, event_path = _plan_catalog_deprecation_locked(
            request,
            publication_root,
            git,
            active_runner,
            active_site_validator,
        )
    return {
        "status": "planned",
        "event": event,
        "eventPath": event_path.relative_to(publication_root).as_posix(),
    }


def prepare_catalog_deprecation(
    request: CatalogDeprecationRequest,
    *,
    runner: ProcessRunner | None = None,
    site_validator: SiteValidator | None = None,
) -> dict[str, object]:
    active_runner = runner or _run_process
    active_site_validator = site_validator or _validate_site
    publication_root = _require_directory(request.publication_root, "Publication repository")
    git = _require_regular_file(request.git, "Git executable")
    with publication_lock(publication_root, git, runner=active_runner):
        event, event_path = _plan_catalog_deprecation_locked(
            request,
            publication_root,
            git,
            active_runner,
            active_site_validator,
        )
        _write_json_atomic(event_path, event)
        tag_names = _git_text(git, publication_root, ("tag", "--list"), active_runner).splitlines()
        inventory = scan_catalog_inventory(publication_root, tag_names=tag_names)
        if inventory.classify_catalog_version(request.catalog_version).value != "DEPRECATED":
            raise RuntimeError("prepared lifecycle event did not produce a deprecated catalog state")
    return {
        "status": "prepared",
        "event": event,
        "eventPath": event_path.relative_to(publication_root).as_posix(),
    }


def _plan_catalog_deprecation_locked(
    request: CatalogDeprecationRequest,
    publication_root: Path,
    git: Path,
    runner: ProcessRunner,
    site_validator: SiteValidator,
) -> tuple[dict[str, object], Path]:
    _require_clean_publication(publication_root, git, runner)
    site_validator(publication_root, git)
    catalog_version = _require_catalog_version(request.catalog_version, "catalog version")
    successor_catalog_version = _require_catalog_version(
        request.successor_catalog_version,
        "successor catalog version",
    )
    if successor_catalog_version <= catalog_version:
        raise ValueError("successor catalog version must be later than the deprecated catalog version")

    tag_names = _git_text(git, publication_root, ("tag", "--list"), runner).splitlines()
    inventory = scan_catalog_inventory(publication_root, tag_names=tag_names)
    if catalog_version in inventory.reserved_versions:
        raise ValueError("reserved catalog versions cannot be deprecated")
    if catalog_version in inventory.deprecated_versions:
        raise ValueError("catalog version is already deprecated")
    if catalog_version in inventory.retired_versions:
        raise ValueError("catalog version is already retired")
    if successor_catalog_version not in inventory.active_versions:
        raise ValueError("successor catalog version is not an active current release")

    record_path = _catalog_record_path(publication_root, catalog_version)
    is_current_release = catalog_version in inventory.active_versions
    if not is_current_release and record_path.parent.name != "catalog-history":
        raise ValueError("catalog version is not a current release")
    _require_directory(
        publication_root / "site" / "catalog" / f"v{catalog_version}",
        "Catalog release tree",
    )

    event_path = (
        publication_root
        / "catalog-lifecycle"
        / f"v{catalog_version}"
        / "events"
        / "0001-deprecated.json"
    )
    lifecycle_version_root = event_path.parents[1]
    if lifecycle_version_root.exists() or lifecycle_version_root.is_symlink():
        raise ValueError("catalog lifecycle event chain already exists")

    event = {
        "schemaVersion": 1,
        "eventSequence": 1,
        "state": "DEPRECATED",
        "catalogVersion": catalog_version,
        "canonicalPath": f"catalog/v{catalog_version}",
        "releaseRecordSha256": hashlib.sha256(record_path.read_bytes()).hexdigest(),
        "predecessorEventSha256": None,
        "successorCatalogVersion": successor_catalog_version,
        "successorCanonicalPath": f"catalog/v{successor_catalog_version}",
        "announcedAt": request.announced_at,
        "removalNotBefore": request.removal_not_before,
        "supportedClientCutoff": request.supported_client_cutoff,
        "rationale": request.rationale,
        "sourceCommit": _git_text(git, publication_root, ("rev-parse", "HEAD"), runner).strip(),
        "publicationReceiptSha256": None,
    }
    _validate_planned_deprecation_event(event, catalog_version)
    return event, event_path


def _catalog_record_path(publication_root: Path, catalog_version: int) -> Path:
    candidates = []
    for directory in ("catalog-releases", "catalog-history"):
        candidate = publication_root / directory / f"v{catalog_version}.json"
        if candidate.exists() or candidate.is_symlink():
            candidates.append(_require_regular_file(candidate, "Catalog release record"))
    if len(candidates) != 1:
        raise ValueError("catalog release/history record is missing or ambiguous")
    return candidates[0]


def _require_catalog_version(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_planned_deprecation_event(event: dict[str, object], catalog_version: int) -> None:
    with tempfile.TemporaryDirectory(prefix="rgm-catalog-lifecycle-") as temporary:
        repository = Path(temporary)
        event_path = repository / "catalog-lifecycle" / f"v{catalog_version}" / "events" / "0001-deprecated.json"
        _write_json_atomic(event_path, event)
        inventory = scan_catalog_inventory(repository, tag_names=())
        if inventory.classify_catalog_version(catalog_version).value != "DEPRECATED":
            raise RuntimeError("planned lifecycle event is not deprecated")


def _write_json_atomic(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        source = json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        temporary_path.write_text(f"{source}\n", encoding="utf-8", newline="\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists() or temporary_path.is_symlink():
            temporary_path.unlink()


def _require_directory(path: Path, label: str) -> Path:
    candidate = path.expanduser().absolute()
    if not candidate.is_dir() or _is_reparse_point(candidate):
        raise ValueError(f"{label} is not a regular directory: {candidate}")
    return candidate.resolve(strict=True)


def _require_regular_file(path: Path, label: str) -> Path:
    candidate = path.expanduser().absolute()
    if not candidate.is_file() or _is_reparse_point(candidate):
        raise ValueError(f"{label} is not a regular file: {candidate}")
    return candidate.resolve(strict=True)


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _require_clean_publication(
    publication_root: Path,
    git: Path,
    runner: ProcessRunner,
) -> None:
    status = _git_bytes(
        git,
        publication_root,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        runner,
    )
    if status:
        raise ValueError("catalog lifecycle verification requires a clean publication worktree")


def _git_text(
    git: Path,
    publication_root: Path,
    arguments: tuple[str, ...],
    runner: ProcessRunner,
) -> str:
    return _git_bytes(git, publication_root, arguments, runner).decode(
        "utf-8",
        errors="replace",
    )


def _git_bytes(
    git: Path,
    publication_root: Path,
    arguments: tuple[str, ...],
    runner: ProcessRunner,
) -> bytes:
    result = runner(
        (str(git), "-C", str(publication_root), *arguments),
        cwd=publication_root,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def _run_process(
    arguments: tuple[str, ...],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )


def _validate_site(publication_root: Path, git: Path) -> None:
    script = _require_regular_file(
        publication_root / "scripts" / "validate-site.py",
        "Site validator",
    )
    site = _require_directory(publication_root / "site", "Site root")
    result = subprocess.run(
        (
            sys.executable,
            "-B",
            str(script),
            str(site),
            "--git",
            str(git),
        ),
        cwd=publication_root,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"site validation failed with exit {result.returncode}: {detail}")
