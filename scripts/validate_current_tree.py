#!/usr/bin/env python3
"""Verify that canonical current RGM trees contain no retired identities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Iterable


REPOSITORY_LEAVES = (
    ("rgm", "rgm"),
    ("rgm-client", "rgm-client"),
    ("rgm-content", "rgm-content"),
    ("rgm-tools", "rgm-tools"),
    ("rgm-authoring", "rgm-authoring"),
    ("rgm-docs", "rgm-docs"),
)
OPTIONAL_PRIVACY_LEAF = ("privacy", "privacy")
WORKSPACE_FILES = (
    Path(".github/copilot-instructions.md"),
    Path(".vscode/tasks.json"),
    Path(".vscode/settings.json"),
)
SKIPPED_DIRECTORY_NAMES = {
    ".git",
    ".gradle",
    ".idea",
    ".kotlin",
    ".cxx",
    "__pycache__",
    "build",
    "node_modules",
    "out",
    "out-build",
    "out-vscode",
    "out-vscode-min",
    "out-vscode-reh-min",
    "out-vscode-reh-web-min",
}
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _joined(*parts: bytes) -> bytes:
    return b"".join(parts)


def retired_patterns() -> tuple[tuple[str, re.Pattern[bytes]], ...]:
    word_start = rb"(?<![a-z0-9])"
    word_end = rb"(?![a-z0-9])"
    return (
        ("retired-host", re.compile(_joined(b"rgm", b"-", b"publication"), re.IGNORECASE)),
        ("retired-privacy", re.compile(_joined(b"rgm", b"-", b"privacy"), re.IGNORECASE)),
        (
            "retired-title",
            re.compile(_joined(b"three", b"-in-", b"a-row"), re.IGNORECASE),
        ),
        (
            "retired-title",
            re.compile(_joined(b"three", rb"\s+in\s+a\s+", b"row"), re.IGNORECASE),
        ),
        (
            "retired-title",
            re.compile(_joined(b"three", b"in", b"a", b"row"), re.IGNORECASE),
        ),
        (
            "retired-title",
            re.compile(_joined(word_start, b"de", b"mo", word_end), re.IGNORECASE),
        ),
    )


class CurrentTreeError(ValueError):
    pass


class RepositoryRoot:
    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path


class HistoricalEntry:
    def __init__(
        self,
        repository: str,
        path: str,
        source_commit: str,
        blob_id: str,
        byte_count: int,
        sha256: str,
        category: str,
        reason: str,
    ) -> None:
        self.repository = repository
        self.path = path
        self.source_commit = source_commit
        self.blob_id = blob_id
        self.byte_count = byte_count
        self.sha256 = sha256
        self.category = category
        self.reason = reason


def is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def require_directory(path: Path, label: str) -> Path:
    candidate = path.expanduser().absolute()
    if not candidate.is_dir() or is_reparse_point(candidate):
        raise CurrentTreeError(f"{label} is missing or not a regular directory: {candidate}")
    return candidate.resolve(strict=True)


def require_file(path: Path, label: str) -> Path:
    candidate = path.expanduser().absolute()
    if not candidate.is_file() or is_reparse_point(candidate):
        raise CurrentTreeError(f"{label} is missing or not a regular file: {candidate}")
    return candidate.resolve(strict=True)


def canonical_roots(workspace_root: Path) -> tuple[Path, list[RepositoryRoot]]:
    workspace = require_directory(workspace_root, "Workspace root")
    container = require_directory(workspace / "rgm", "RGM container")
    roots = [
        RepositoryRoot(name, require_directory(container / leaf, f"Repository {name}"))
        for name, leaf in REPOSITORY_LEAVES
    ]
    privacy_name, privacy_leaf = OPTIONAL_PRIVACY_LEAF
    privacy = container / privacy_leaf
    if privacy.exists() or privacy.is_symlink():
        roots.append(
            RepositoryRoot(
                privacy_name,
                require_directory(privacy, "Temporary privacy repository"),
            )
        )
    return workspace, roots


def read_allowlist(path: Path, roots: dict[str, RepositoryRoot]) -> dict[tuple[str, str], HistoricalEntry]:
    allowlist_path = require_file(path, "Historical allowlist")
    try:
        document = json.loads(allowlist_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CurrentTreeError(f"Historical allowlist is invalid: {error}") from error
    if set(document) != {"schemaVersion", "entries"} or document["schemaVersion"] != 1:
        raise CurrentTreeError("Historical allowlist has an invalid shape")
    entries = document["entries"]
    if not isinstance(entries, list):
        raise CurrentTreeError("Historical allowlist entries must be an array")
    allowed: dict[tuple[str, str], HistoricalEntry] = {}
    required_fields = {
        "repository",
        "path",
        "sourceCommit",
        "blobId",
        "bytes",
        "sha256",
        "category",
        "reason",
    }
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) != required_fields:
            raise CurrentTreeError("Historical allowlist entry has an invalid shape")
        repository = raw["repository"]
        relative = raw["path"]
        if (
            not isinstance(repository, str)
            or repository not in roots
            or not is_repository_relative_path(relative)
            or not isinstance(raw["sourceCommit"], str)
            or not GIT_SHA.fullmatch(raw["sourceCommit"])
            or not isinstance(raw["blobId"], str)
            or not GIT_SHA.fullmatch(raw["blobId"])
            or type(raw["bytes"]) is not int
            or raw["bytes"] <= 0
            or not isinstance(raw["sha256"], str)
            or not SHA256.fullmatch(raw["sha256"])
            or raw["category"] != "retired-title"
            or not isinstance(raw["reason"], str)
            or not raw["reason"].strip()
        ):
            raise CurrentTreeError("Historical allowlist entry has an invalid identity")
        entry = HistoricalEntry(
            repository=repository,
            path=relative,
            source_commit=raw["sourceCommit"],
            blob_id=raw["blobId"],
            byte_count=raw["bytes"],
            sha256=raw["sha256"],
            category=raw["category"],
            reason=raw["reason"],
        )
        key = (entry.repository, entry.path)
        if key in allowed:
            raise CurrentTreeError(f"Historical allowlist entry is duplicated: {repository}/{relative}")
        verify_historical_entry(entry, roots[repository])
        allowed[key] = entry
    return allowed


def is_repository_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def verify_historical_entry(entry: HistoricalEntry, root: RepositoryRoot) -> None:
    current = root.path / entry.path
    try:
        current_resolved = require_file(current, "Allowlisted historical artifact")
    except CurrentTreeError as error:
        raise CurrentTreeError(f"Historical allowlist target is invalid: {entry.repository}/{entry.path}") from error
    if not current_resolved.is_relative_to(root.path):
        raise CurrentTreeError(f"Historical allowlist target escapes its repository: {entry.repository}/{entry.path}")
    relative = current_resolved.relative_to(root.path).as_posix()
    if relative != entry.path:
        raise CurrentTreeError(f"Historical allowlist path is noncanonical: {entry.repository}/{entry.path}")
    revision = f"{entry.source_commit}:{entry.path}"
    if git_text(root.path, "merge-base", "--is-ancestor", entry.source_commit, "HEAD") is None:
        raise CurrentTreeError(f"Historical source commit is not reachable: {entry.repository}/{entry.path}")
    blob_id = git_output(root.path, "rev-parse", revision).decode("ascii").strip()
    if blob_id != entry.blob_id:
        raise CurrentTreeError(f"Historical blob identity differs: {entry.repository}/{entry.path}")
    historical = git_output(root.path, "show", revision)
    if len(historical) != entry.byte_count or sha256(historical) != entry.sha256:
        raise CurrentTreeError(f"Historical blob bytes differ: {entry.repository}/{entry.path}")
    require_clean_git_path(root.path, entry.path)


def require_clean_git_path(repository: Path, relative: str) -> None:
    for arguments in (
        ("diff", "--quiet", "--", relative),
        ("diff", "--cached", "--quiet", "--", relative),
    ):
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            check=False,
        )
        if completed.returncode == 1:
            raise CurrentTreeError(f"Historical allowlist target has uncommitted changes: {relative}")
        if completed.returncode != 0:
            raise CurrentTreeError(
                f"Git history check failed for {repository}: {' '.join(arguments)}: "
                f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
            )


def git_output(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CurrentTreeError(
            f"Git history check failed for {repository}: {' '.join(arguments)}: "
            f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return completed.stdout


def git_text(repository: Path, *arguments: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout.decode("utf-8", errors="replace").strip()
    if arguments[:2] == ("merge-base", "--is-ancestor") and completed.returncode == 1:
        return None
    raise CurrentTreeError(
        f"Git history check failed for {repository}: {' '.join(arguments)}: "
        f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
    )


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def iter_tree(root: Path) -> Iterable[Path]:
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        included_directories: list[str] = []
        for name in sorted(directories):
            path = current_path / name
            if name in SKIPPED_DIRECTORY_NAMES:
                continue
            if is_reparse_point(path):
                raise CurrentTreeError(f"Current tree contains a symbolic link or reparse point: {path}")
            included_directories.append(name)
            yield path
        directories[:] = included_directories
        for name in sorted(files):
            path = current_path / name
            if is_reparse_point(path) or not path.is_file():
                raise CurrentTreeError(f"Current tree contains a non-regular file: {path}")
            yield path


def matched_categories(value: bytes) -> set[str]:
    return {category for category, pattern in retired_patterns() if pattern.search(value)}


def scan_path(root: RepositoryRoot, path: Path) -> set[str]:
    relative = path.relative_to(root.path).as_posix().encode("utf-8", errors="surrogateescape")
    return matched_categories(relative)


def scan_file_contents(path: Path) -> set[str]:
    try:
        return matched_categories(path.read_bytes())
    except OSError as error:
        raise CurrentTreeError(f"Cannot read current tree file: {path}: {error}") from error


def scan_repository(
    root: RepositoryRoot,
    allowlist: dict[tuple[str, str], HistoricalEntry],
) -> tuple[list[str], set[tuple[str, str]], int]:
    findings: list[str] = []
    used_entries: set[tuple[str, str]] = set()
    files = 0
    for path in iter_tree(root.path):
        relative = path.relative_to(root.path).as_posix()
        path_categories = scan_path(root, path)
        for category in sorted(path_categories):
            findings.append(f"{root.name}/{relative}: {category} in path")
        if path.is_dir():
            continue
        files += 1
        categories = scan_file_contents(path)
        entry = allowlist.get((root.name, relative))
        for category in sorted(categories):
            if category == "retired-title" and entry is not None and entry.category == category:
                used_entries.add((root.name, relative))
                continue
            findings.append(f"{root.name}/{relative}: {category} in content")
    return findings, used_entries, files


def scan_workspace_files(workspace: Path) -> tuple[list[str], int]:
    findings: list[str] = []
    files = 0
    for relative in WORKSPACE_FILES:
        path = require_file(workspace / relative, "RGM-owned workspace file")
        categories = matched_categories(path.read_bytes())
        for category in sorted(categories):
            findings.append(f"workspace/{relative.as_posix()}: {category} in content")
        files += 1
    return findings, files


def require_final_public_origin(roots: dict[str, RepositoryRoot]) -> None:
    current = git_output(roots["rgm"].path, "remote", "get-url", "origin").decode("utf-8").strip()
    expected = "https://github.com/TheFunkyBits/rgm.git"
    if current != expected:
        raise CurrentTreeError(f"Public origin is not final: {current}")


def validate_current_tree(
    workspace_root: Path,
    allowlist_path: Path,
    require_final_origin: bool = False,
) -> dict[str, object]:
    workspace, roots_list = canonical_roots(workspace_root)
    roots = {root.name: root for root in roots_list}
    allowlist = read_allowlist(allowlist_path, roots)
    findings: list[str] = []
    used_entries: set[tuple[str, str]] = set()
    files = 0
    for root in roots_list:
        root_findings, root_entries, root_file_count = scan_repository(root, allowlist)
        findings.extend(root_findings)
        used_entries.update(root_entries)
        files += root_file_count
    workspace_findings, workspace_file_count = scan_workspace_files(workspace)
    findings.extend(workspace_findings)
    files += workspace_file_count
    unused = sorted(set(allowlist) - used_entries)
    findings.extend(f"{repository}/{path}: historical allowlist entry is unused" for repository, path in unused)
    if require_final_origin:
        require_final_public_origin(roots)
    if findings:
        raise CurrentTreeError("Current tree contains retired identities:\n" + "\n".join(sorted(findings)))
    return {
        "valid": True,
        "repositories": [root.name for root in roots_list],
        "files": files,
        "historicalEntries": len(allowlist),
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "catalog-history"
        / "retired-content-allowlist.json",
    )
    parser.add_argument("--require-final-public-origin", action="store_true")
    parsed = parser.parse_args(arguments)
    try:
        result = validate_current_tree(
            workspace_root=parsed.workspace_root,
            allowlist_path=parsed.allowlist,
            require_final_origin=parsed.require_final_public_origin,
        )
    except CurrentTreeError as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
