"""Promote one sealed catalog release through an append-only transaction."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any, BinaryIO, Iterator, Sequence
import uuid


ProcessRunner = Callable[..., subprocess.CompletedProcess[bytes]]
ReplaceOperation = Callable[[Path, Path], None]
EventHook = Callable[[str], None]
CATALOG_ROOT = "site/catalog"
RELEASE_ROOT = "catalog-releases"
RESERVATION_ROOT = "catalog-reservations"
SITE_BASE_URL = "https://thefunkybits.github.io/rgm/"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class PromoteCatalogRequest:
    stage_directory: Path
    catalog_candidate: Path
    publication_root: Path
    client_root: Path
    java: Path
    git: Path


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stage-directory", type=Path, required=True)
    parser.add_argument("--catalog-candidate", type=Path, required=True)
    parser.add_argument("--publication-root", type=Path, required=True)
    parser.add_argument("--client-root", type=Path, required=True)
    parser.add_argument("--java", type=Path, required=True)
    parser.add_argument("--git", type=Path, required=True)


def request_from_arguments(arguments: argparse.Namespace) -> PromoteCatalogRequest:
    return PromoteCatalogRequest(
        stage_directory=arguments.stage_directory,
        catalog_candidate=arguments.catalog_candidate,
        publication_root=arguments.publication_root,
        client_root=arguments.client_root,
        java=arguments.java,
        git=arguments.git,
    )


def promote_external_catalog(
    request: PromoteCatalogRequest,
    *,
    runner: ProcessRunner | None = None,
    replace: ReplaceOperation = os.replace,
    event_hook: EventHook = lambda _event: None,
) -> dict[str, Any]:
    runner = runner or _run_process
    stage_root = _require_directory(request.stage_directory, "Catalog stage")
    candidate_path = _require_regular_file(request.catalog_candidate, "Catalog candidate")
    publication_root = _require_directory(request.publication_root, "Publication repository")
    client_root = _require_directory(request.client_root, "Client repository")
    java = _require_regular_file(request.java, "JDK 17 Java")
    git = _require_regular_file(request.git, "Git executable")
    candidate = _verify_catalog_candidate(client_root, java, candidate_path, runner)
    stage = _verified_stage(stage_root, candidate_path, candidate)
    version = stage["catalogVersion"]
    catalog_root = publication_root / CATALOG_ROOT
    release_root = publication_root / RELEASE_ROOT

    with publication_lock(publication_root, git, runner=runner):
        _recover_transactions(catalog_root, release_root, replace)
        _require_clean_publication(publication_root, git, runner)
        actual_commit = _git_text(git, publication_root, ["rev-parse", "HEAD"], runner).strip()
        if actual_commit != stage["sourceCommits"]["publication"]:
            raise ValueError("public worktree HEAD differs from the catalog candidate")
        source = _require_relative_directory(
            stage_root,
            stage["catalogDirectory"],
            "Staged catalog release",
        )
        destination = catalog_root / f"v{version}"
        release_file = release_root / f"v{version}.json"
        _require_catalog_version_unreserved(publication_root, version)
        if (
            destination.exists()
            or destination.is_symlink()
            or release_file.exists()
            or release_file.is_symlink()
        ):
            raise ValueError(f"catalog v{version} already exists")
        _promote(
            source,
            destination,
            release_file,
            stage,
            candidate,
            replace,
            event_hook,
        )
        _require_expected_status(publication_root, git, destination, release_file, runner)
    return {
        "status": "prepared",
        "catalogVersion": version,
        "publicationRoot": publication_root.as_posix(),
    }


def _verify_catalog_candidate(
    client_root: Path,
    java: Path,
    catalog_candidate: Path,
    runner: ProcessRunner,
) -> dict[str, Any]:
    wrapper = _require_relative_file(
        client_root,
        "gradle/wrapper/gradle-wrapper.jar",
        "Client Gradle wrapper",
    )
    _invoke(
        [
            str(java),
            "-classpath",
            str(wrapper),
            "org.gradle.wrapper.GradleWrapperMain",
            ":tools:catalog-publisher:installDist",
            "--no-daemon",
            "--console=plain",
        ],
        client_root,
        "catalog publisher assembly",
        runner,
    )
    classpath = client_root / "tools/catalog-publisher/build/install/catalog-publisher/lib/*"
    source = _invoke(
        [
            str(java),
            "-classpath",
            str(classpath),
            "dev.thefunkybits.rgm.catalogpublisher.MainKt",
            "verify-catalog-candidate",
            "--candidate",
            str(catalog_candidate),
        ],
        client_root,
        "catalog candidate verification",
        runner,
    )
    document = _require_object(_loads(source), "Verified catalog candidate")
    _require_exact_keys(
        document,
        {
            "schemaVersion",
            "candidateManifestSha256",
            "catalogVersion",
            "minimumAppVersionCode",
            "sourceCommits",
            "catalogBundle",
        },
        "Verified catalog candidate",
    )
    if _require_integer(document["schemaVersion"], "Verified catalog schemaVersion") != 4:
        raise ValueError("verified catalog candidate must use schema 4")
    candidate_hash = _require_string(
        document["candidateManifestSha256"],
        "Verified catalog candidate hash",
    )
    if not SHA256.fullmatch(candidate_hash):
        raise ValueError("verified catalog candidate hash is invalid")
    if _require_integer(document["catalogVersion"], "Verified catalog version") <= 0:
        raise ValueError("verified catalog version must be positive")
    if _require_integer(document["minimumAppVersionCode"], "Verified catalog minimum app version") <= 0:
        raise ValueError("verified catalog minimum app version must be positive")
    if _require_string(document["catalogBundle"], "Verified catalog bundle") != "catalog-bundle":
        raise ValueError("verified catalog bundle is invalid")
    commits = _require_object(document["sourceCommits"], "Verified source commits")
    _require_exact_keys(commits, {"content", "client", "publication"}, "Verified source commits")
    if not all(
        GIT_SHA.fullmatch(_require_string(commits[name], f"source commit {name}"))
        for name in commits
    ):
        raise ValueError("verified source commit is invalid")
    return document


def _verified_stage(
    stage_root: Path,
    candidate_path: Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    record_path = _require_relative_file(stage_root, "catalog-stage.json", "Catalog stage record")
    stage = _require_object(_read_json(record_path), "Catalog stage record")
    _require_exact_keys(
        stage,
        {
            "schemaVersion",
            "catalogCandidateSha256",
            "catalogVersion",
            "minimumAppVersionCode",
            "sourceCommits",
            "catalogDirectory",
            "indexSha256",
            "signatureEnvelopeSha256",
            "files",
        },
        "Catalog stage record",
    )
    if _require_integer(stage["schemaVersion"], "Catalog stage schemaVersion") != 4:
        raise ValueError("catalog stage record must use schema 4")
    if stage["catalogCandidateSha256"] != candidate["candidateManifestSha256"]:
        raise ValueError("catalog stage candidate hash differs")
    if _sha256_file(candidate_path) != candidate["candidateManifestSha256"]:
        raise ValueError("verified catalog candidate hash differs from its file")
    if stage["catalogVersion"] != candidate["catalogVersion"]:
        raise ValueError("catalog stage version differs from its candidate")
    if stage["minimumAppVersionCode"] != candidate["minimumAppVersionCode"]:
        raise ValueError("catalog stage compatibility differs from its candidate")
    if stage["sourceCommits"] != candidate["sourceCommits"]:
        raise ValueError("catalog stage source commits differ from its candidate")
    version = _require_integer(stage["catalogVersion"], "Catalog stage version")
    if version <= 0:
        raise ValueError("catalog stage version must be positive")
    if _require_string(stage["catalogDirectory"], "Catalog stage directory") != f"v{version}":
        raise ValueError("catalog stage directory is invalid")
    for field in ("indexSha256", "signatureEnvelopeSha256"):
        if not SHA256.fullmatch(_require_string(stage[field], f"Catalog stage {field}")):
            raise ValueError(f"catalog stage {field} is invalid")
    commits = _require_object(stage["sourceCommits"], "Catalog stage source commits")
    _require_exact_keys(commits, {"content", "client", "publication"}, "Catalog stage source commits")
    if not all(
        GIT_SHA.fullmatch(_require_string(commits[name], f"source commit {name}"))
        for name in commits
    ):
        raise ValueError("catalog stage source commit is invalid")
    declared = _stage_files(stage["files"])
    if declared != inventory(stage_root, excluded={"catalog-stage.json"}):
        raise ValueError("catalog stage file inventory differs from catalog-stage.json")
    stage["files"] = declared
    stage["sourceCommits"] = commits
    return stage


def _stage_files(value: Any) -> list[dict[str, Any]]:
    values = _require_array(value, "Catalog stage files")
    entries = []
    for index, raw in enumerate(values):
        entry = _require_object(raw, f"Catalog stage files[{index}]")
        _require_exact_keys(entry, {"path", "bytes", "sha256"}, f"Catalog stage files[{index}]")
        path = _require_string(entry["path"], f"Catalog stage file path {index}")
        size = _require_integer(entry["bytes"], f"Catalog stage file bytes {index}")
        digest = _require_string(entry["sha256"], f"Catalog stage file hash {index}")
        if not path or path.startswith("/") or "\\" in path or size <= 0 or not SHA256.fullmatch(digest):
            raise ValueError("catalog stage file entry is invalid")
        entries.append({"path": path, "bytes": size, "sha256": digest})
    if not entries or [entry["path"] for entry in entries] != sorted({entry["path"] for entry in entries}):
        raise ValueError("catalog stage files are empty, repeated, or unsorted")
    return entries


def _promote(
    source: Path,
    destination: Path,
    release_file: Path,
    stage: dict[str, Any],
    candidate: dict[str, Any],
    replace: ReplaceOperation,
    event_hook: EventHook,
) -> None:
    catalog_root = destination.parent
    catalog_root.mkdir(parents=True, exist_ok=True)
    release_file.parent.mkdir(parents=True, exist_ok=True)
    transaction = catalog_root / f".promotion-{uuid.uuid4().hex}"
    transaction.mkdir()
    record = _release_record(source, stage, candidate)
    staged_tree = transaction / "tree"
    staged_record = transaction / "release.json"
    copy_regular_tree(source, staged_tree, "Staged catalog release")
    _write_json_atomic(staged_record, record)
    journal = {
        "schemaVersion": 1,
        "state": "prepared",
        "catalogVersion": stage["catalogVersion"],
        "treeReceipt": tree_receipt(staged_tree),
        "recordSha256": _sha256_file(staged_record),
    }
    _write_journal(transaction, journal)
    try:
        replace(staged_tree, destination)
        if tree_receipt(destination) != journal["treeReceipt"]:
            raise RuntimeError("promoted catalog tree differs from staged release")
        journal["state"] = "tree-installed"
        _write_journal(transaction, journal)
        event_hook("tree-installed")
        replace(staged_record, release_file)
        if _sha256_file(release_file) != journal["recordSha256"]:
            raise RuntimeError("promoted catalog release record differs from staged release")
        journal["state"] = "complete"
        _write_journal(transaction, journal)
        event_hook("complete")
        shutil.rmtree(transaction)
    except BaseException:
        if journal["state"] == "prepared":
            shutil.rmtree(transaction, ignore_errors=True)
        raise


def _release_record(
    source: Path,
    stage: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    index_path = _require_relative_file(source, "index.json", "Staged catalog index")
    envelope_path = _require_relative_file(source, "index.signatures.json", "Staged catalog signature")
    index = _require_object(_read_json(index_path), "Staged catalog index")
    envelope = _require_object(_read_json(envelope_path), "Staged catalog signature")
    _require_exact_keys(
        index,
        {"schemaVersion", "catalogVersion", "minimumAppVersionCode", "files"},
        "Staged catalog index",
    )
    _require_exact_keys(
        envelope,
        {"schemaVersion", "manifestSha256", "signatures"},
        "Staged catalog signature",
    )
    if index.get("schemaVersion") != 4:
        raise ValueError("staged catalog index identity is invalid")
    if index.get("catalogVersion") != stage["catalogVersion"]:
        raise ValueError("staged catalog index version differs from stage")
    if index.get("minimumAppVersionCode") != stage["minimumAppVersionCode"]:
        raise ValueError("staged catalog index compatibility differs from stage")
    if _sha256_file(index_path) != stage["indexSha256"]:
        raise ValueError("staged catalog index hash differs from stage")
    if _sha256_file(envelope_path) != stage["signatureEnvelopeSha256"]:
        raise ValueError("staged catalog signature hash differs from stage")
    if envelope["schemaVersion"] != 2 or envelope["manifestSha256"] != stage["indexSha256"]:
        raise ValueError("staged catalog signature identity is invalid")
    signatures = _require_array(envelope.get("signatures"), "Staged catalog signatures")
    signature = _require_object(signatures[0], "Staged catalog signature") if len(signatures) == 1 else None
    if signature is None:
        raise ValueError("staged catalog must contain exactly one signature")
    key_id = _require_string(signature.get("keyId"), "Staged catalog signing key")
    files = _release_files(source, index.get("files"))
    version = stage["catalogVersion"]
    return {
        "schemaVersion": 3,
        "catalogVersion": version,
        "canonicalPath": f"catalog/v{version}",
        "indexUrl": f"{SITE_BASE_URL}catalog/v{version}/index.json",
        "indexSchemaVersion": 4,
        "keyId": key_id,
        "indexSha256": stage["indexSha256"],
        "signatureEnvelopeSha256": stage["signatureEnvelopeSha256"],
        "files": files,
        "minimumAppVersionCode": stage["minimumAppVersionCode"],
        "catalogCandidateSha256": stage["catalogCandidateSha256"],
        "contentCommit": stage["sourceCommits"]["content"],
        "publisherCommit": stage["sourceCommits"]["client"],
        "publicationCommit": stage["sourceCommits"]["publication"],
        "tagNames": {
            "content": f"catalog/v{version}-content",
            "publisher": f"catalog/v{version}-publisher",
            "release": f"catalog/v{version}",
        },
    }


def _release_files(source: Path, raw_files: Any) -> list[dict[str, Any]]:
    values = _require_array(raw_files, "Staged catalog files")
    files = []
    for position, value in enumerate(values):
        entry = _require_object(value, f"Staged catalog files[{position}]")
        _require_exact_keys(
            entry,
            {"path", "objectPath", "bytes", "sha256"},
            f"Staged catalog files[{position}]",
        )
        logical_path = _require_string(entry["path"], f"Staged catalog logical path {position}")
        object_path = _require_string(entry["objectPath"], f"Staged catalog object path {position}")
        size = _require_integer(entry["bytes"], f"Staged catalog object size {position}")
        digest = _require_string(entry["sha256"], f"Staged catalog object hash {position}")
        if (
            not logical_path
            or object_path != f"objects/sha256/{digest}"
            or size <= 0
            or not SHA256.fullmatch(digest)
        ):
            raise ValueError("staged catalog logical inventory is invalid")
        object_file = _require_relative_file(source, object_path, "Staged catalog object")
        if object_file.stat().st_size != size or _sha256_file(object_file) != digest:
            raise ValueError("staged catalog object differs from signed inventory")
        files.append(
            {
                "path": logical_path,
                "objectPath": object_path,
                "bytes": size,
                "sha256": digest,
            }
        )
    paths = [entry["path"] for entry in files]
    if not files or paths != sorted(set(paths)):
        raise ValueError("staged catalog logical inventory is empty, repeated, or unsorted")
    return files


def _recover_transactions(catalog_root: Path, release_root: Path, replace: ReplaceOperation) -> None:
    if not catalog_root.exists():
        return
    if not catalog_root.is_dir() or _is_reparse_point(catalog_root):
        raise RuntimeError(f"catalog root is not a regular directory: {catalog_root}")
    for transaction in sorted(catalog_root.glob(".promotion-*")):
        journal = _read_journal(transaction)
        version = journal["catalogVersion"]
        destination = catalog_root / f"v{version}"
        release_file = release_root / f"v{version}.json"
        if journal["state"] == "prepared":
            shutil.rmtree(transaction)
            continue
        if not destination.is_dir() or tree_receipt(destination) != journal["treeReceipt"]:
            raise RuntimeError("catalog promotion recovery found an invalid installed tree")
        if release_file.exists():
            if _sha256_file(release_file) != journal["recordSha256"]:
                raise RuntimeError("catalog promotion recovery found a conflicting release record")
        else:
            staged_record = transaction / "release.json"
            if not staged_record.is_file() or _sha256_file(staged_record) != journal["recordSha256"]:
                raise RuntimeError("catalog promotion recovery lost its release record")
            release_file.parent.mkdir(parents=True, exist_ok=True)
            replace(staged_record, release_file)
        journal["state"] = "complete"
        _write_journal(transaction, journal)
        shutil.rmtree(transaction)


def _write_journal(transaction: Path, journal: dict[str, Any]) -> None:
    temporary = transaction / ".journal.json.tmp"
    temporary.write_bytes(_canonical_bytes(journal))
    os.replace(temporary, transaction / "journal.json")


def _read_journal(transaction: Path) -> dict[str, Any]:
    if not transaction.is_dir() or _is_reparse_point(transaction):
        raise RuntimeError("catalog promotion journal directory is invalid")
    journal = _require_object(
        _read_json(_require_relative_file(transaction, "journal.json", "Catalog promotion journal")),
        "Catalog promotion journal",
    )
    _require_exact_keys(
        journal,
        {"schemaVersion", "state", "catalogVersion", "treeReceipt", "recordSha256"},
        "Catalog promotion journal",
    )
    if journal["schemaVersion"] != 1 or journal["state"] not in {"prepared", "tree-installed", "complete"}:
        raise RuntimeError("catalog promotion journal is invalid")
    if _require_integer(journal["catalogVersion"], "Catalog promotion journal version") <= 0:
        raise RuntimeError("catalog promotion journal version is invalid")
    if not SHA256.fullmatch(_require_string(journal["recordSha256"], "Catalog promotion journal hash")):
        raise RuntimeError("catalog promotion journal record hash is invalid")
    return journal


def _require_catalog_version_unreserved(publication_root: Path, catalog_version: int) -> None:
    reservation = publication_root / RESERVATION_ROOT / f"v{catalog_version}.json"
    if not reservation.exists() and not reservation.is_symlink():
        return
    document = _require_object(
        _read_json(_require_regular_file(reservation, "Catalog reservation")),
        "Catalog reservation",
    )
    _require_exact_keys(
        document,
        {
            "schemaVersion",
            "catalogVersion",
            "state",
            "reason",
            "indexSha256",
            "signatureEnvelopeSha256",
        },
        "Catalog reservation",
    )
    if _require_integer(document["schemaVersion"], "Catalog reservation schemaVersion") != 2:
        raise ValueError("catalog reservation schema is invalid")
    if _require_integer(document["catalogVersion"], "Catalog reservation version") != catalog_version:
        raise ValueError("catalog reservation version differs from its path")
    if _require_string(document["state"], "Catalog reservation state") != "reserved":
        raise ValueError("catalog reservation state is invalid")
    if not _require_string(document["reason"], "Catalog reservation reason"):
        raise ValueError("catalog reservation reason is invalid")
    for field in ("indexSha256", "signatureEnvelopeSha256"):
        if not SHA256.fullmatch(_require_string(document[field], f"Catalog reservation {field}")):
            raise ValueError(f"catalog reservation {field} is invalid")
    raise ValueError(f"catalog v{catalog_version} is permanently reserved")


def _require_clean_publication(publication_root: Path, git: Path, runner: ProcessRunner) -> None:
    if _git_bytes(git, publication_root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], runner):
        raise ValueError("public catalog promotion requires a clean publication worktree")


def _require_expected_status(
    publication_root: Path,
    git: Path,
    destination: Path,
    release_file: Path,
    runner: ProcessRunner,
) -> None:
    status = _git_bytes(
        git,
        publication_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        runner,
    )
    allowed = {
        destination.relative_to(publication_root).as_posix(),
        release_file.relative_to(publication_root).as_posix(),
    }
    paths = _status_paths(status)
    if not paths or any(
        not any(path == prefix or path.startswith(f"{prefix}/") for prefix in allowed)
        for path in paths
    ):
        raise RuntimeError("promotion changed paths outside the new catalog release")


def _status_paths(source: bytes) -> list[str]:
    records = source.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(records) and records[index]:
        record = records[index].decode("utf-8", errors="surrogateescape")
        if len(record) < 4 or record[2] != " ":
            raise RuntimeError("Git returned malformed porcelain status")
        paths.append(record[3:])
        if "R" in record[:2] or "C" in record[:2]:
            index += 1
            if index >= len(records) or not records[index]:
                raise RuntimeError("Git returned incomplete rename status")
            paths.append(records[index].decode("utf-8", errors="surrogateescape"))
        index += 1
    return paths


def _git_lock_path(publication_root: Path, git: Path, runner: ProcessRunner) -> Path:
    source = _git_text(
        git,
        publication_root,
        ["rev-parse", "--path-format=absolute", "--git-path", "rgm-catalog-promotion.lock"],
        runner,
    ).strip()
    path = Path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def publication_lock(
    publication_root: Path,
    git: Path,
    *,
    runner: ProcessRunner | None = None,
) -> Iterator[None]:
    active_runner = runner or _run_process
    lock_path = _git_lock_path(publication_root, git, active_runner)
    with _exclusive_lock(lock_path, "catalog publication"):
        yield


def _git_text(git: Path, root: Path, arguments: list[str], runner: ProcessRunner) -> str:
    return _git_bytes(git, root, arguments, runner).decode("utf-8", errors="replace")


def _git_bytes(git: Path, root: Path, arguments: list[str], runner: ProcessRunner) -> bytes:
    result = runner([str(git), "-C", str(root), *arguments], cwd=root)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def _invoke(
    arguments: Sequence[str | os.PathLike[str]],
    cwd: Path,
    label: str,
    runner: ProcessRunner,
) -> str:
    result = runner(arguments, cwd=cwd)
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        detail = stderr.strip() or stdout.strip()
        raise RuntimeError(f"{label} failed with exit {result.returncode}: {detail}")
    return stdout


def _run_process(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    if isinstance(arguments, (str, bytes)):
        raise TypeError("process arguments must be a sequence")
    command = [os.fspath(argument) for argument in arguments]
    if not command or any(not argument for argument in command):
        raise ValueError("process arguments must be nonempty")
    return subprocess.run(
        command,
        cwd=cwd,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )


@contextmanager
def _exclusive_lock(path: Path, label: str) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        _lock_file(lock, label)
        try:
            yield
        finally:
            _unlock_file(lock)


def _lock_file(lock: BinaryIO, label: str) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        raise RuntimeError(f"another {label} holds the lock") from error


def _unlock_file(lock: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _loads(source: str) -> Any:
    return json.loads(
        source,
        object_pairs_hook=_strict_object,
        parse_constant=_reject_constant,
    )


def _read_json(path: Path) -> Any:
    return _loads(path.read_text(encoding="utf-8"))


def _canonical_bytes(document: Any) -> bytes:
    source = json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )
    return f"{source}\n".encode("utf-8")


def _write_json_atomic(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary_file:
        temporary = Path(temporary_file.name)
        temporary_file.write(_canonical_bytes(document))
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be a JSON object")
    return value


def _require_exact_keys(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    actual = set(value)
    if actual != keys:
        raise ValueError(
            f"{label} keys differ: missing={sorted(keys - actual)}, "
            f"unknown={sorted(actual - keys)}"
        )


def _require_string(value: Any, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _require_integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _require_array(value: Any, label: str) -> list[Any]:
    if type(value) is not list:
        raise ValueError(f"{label} must be a JSON array")
    return value


def _normalized_relative(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError(f"path must use nonempty forward-slash segments: {value!r}")
    segments = value.split("/")
    if value.startswith("/") or any(
        not segment or segment in (".", "..") or ":" in segment
        for segment in segments
    ):
        raise ValueError(f"path must be normalized and relative: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError(f"path must be normalized and relative: {value!r}")
    return path.as_posix()


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _require_regular_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().absolute()
    if not resolved.is_file() or _is_reparse_point(resolved):
        raise ValueError(f"{label} is missing or not a regular file: {resolved}")
    return resolved


def _require_relative_file(root: Path, relative: str, label: str) -> Path:
    serialized = _normalized_relative(relative)
    current = root.expanduser().absolute()
    if not current.is_dir() or _is_reparse_point(current):
        raise ValueError(f"{label} root is not a regular directory: {current}")
    for segment in serialized.split("/"):
        current /= segment
        if not current.exists() or _is_reparse_point(current):
            raise ValueError(f"{label} is missing or crosses a reparse point: {current}")
    return _require_regular_file(current, label)


def _require_relative_directory(root: Path, relative: str, label: str) -> Path:
    serialized = _normalized_relative(relative)
    current = root.expanduser().absolute()
    if not current.is_dir() or _is_reparse_point(current):
        raise ValueError(f"{label} root is not a regular directory: {current}")
    for segment in serialized.split("/"):
        current /= segment
        if not current.is_dir() or _is_reparse_point(current):
            raise ValueError(f"{label} is missing or crosses a reparse point: {current}")
    return current


def _require_directory(path: Path, label: str) -> Path:
    root = path.expanduser().absolute()
    if not root.is_dir() or _is_reparse_point(root):
        raise ValueError(f"{label} is missing or not a regular directory: {root}")
    return root


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_receipt(path: Path, relative: str) -> dict[str, Any]:
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"release input must not be empty: {path}")
    return {
        "path": _normalized_relative(relative),
        "bytes": size,
        "sha256": _sha256_file(path),
    }


def _regular_tree_files(
    root: Path,
    label: str,
    *,
    allow_empty: bool = False,
) -> list[tuple[str, Path]]:
    tree = root.expanduser().absolute()
    if not tree.is_dir() or _is_reparse_point(tree):
        raise ValueError(f"{label} is missing or not a regular directory: {tree}")
    files: list[tuple[str, Path]] = []
    for current, directories, names in os.walk(tree, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories):
            directory = current_path / name
            if _is_reparse_point(directory):
                raise ValueError(f"{label} contains a reparse point: {directory}")
        for name in sorted(names):
            source = current_path / name
            if not source.is_file() or _is_reparse_point(source):
                raise ValueError(f"{label} contains a non-regular file: {source}")
            relative = source.relative_to(tree).as_posix()
            files.append((_normalized_relative(relative), source))
    files.sort(key=lambda entry: entry[0])
    if not files and not allow_empty:
        raise ValueError(f"{label} is empty: {tree}")
    return files


def _copy_regular_file(source: Path, destination: Path, label: str) -> None:
    regular = _require_regular_file(source, label)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"release destination already exists: {destination}")
    shutil.copyfile(regular, destination)


def copy_regular_tree(source: Path, destination: Path, label: str) -> None:
    for relative, path in _regular_tree_files(source, label):
        _copy_regular_file(path, destination.joinpath(*relative.split("/")), label)


def inventory(root: Path, *, excluded: set[str] | None = None) -> list[dict[str, Any]]:
    omitted = excluded or set()
    entries = []
    for relative, path in _regular_tree_files(root, "staged release tree"):
        if relative not in omitted:
            entries.append(_file_receipt(path, relative))
    return entries


def tree_receipt(root: Path) -> dict[str, Any]:
    entries = inventory(root)
    digest = hashlib.sha256()
    total = 0
    for entry in entries:
        total += entry["bytes"]
        digest.update(
            f"{entry['path']}|{entry['bytes']}|{entry['sha256']}\n".encode("utf-8")
        )
    return {
        "fileCount": len(entries),
        "bytes": total,
        "sha256": digest.hexdigest(),
    }


def sha256_file(path: Path) -> str:
    return _sha256_file(path)
