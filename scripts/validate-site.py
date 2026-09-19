#!/usr/bin/env python3

import argparse
import base64
from datetime import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

try:
    from catalog_lifecycle import scan_catalog_inventory
except ModuleNotFoundError:
    from scripts.catalog_lifecycle import scan_catalog_inventory

REQUIRED = (
    "index.html",
    "privacy/index.html",
    "trust/catalog-keys.json",
    "spec/catalog-v4/index.schema.json",
    "spec/catalog-v4/signatures.schema.json",
    "spec/catalog-v4/binding.schema.json",
    "catalog/v4/index.json",
    "catalog/v4/index.signatures.json",
)
SITE_BASE_URL = "https://thefunkybits.github.io/rgm/"
SITE_BASE_PATH = "/rgm/"
SCHEMA_IDS = {
    "spec/catalog-v4/index.schema.json": f"{SITE_BASE_URL}spec/catalog-v4/index.schema.json",
    "spec/catalog-v4/signatures.schema.json": f"{SITE_BASE_URL}spec/catalog-v4/signatures.schema.json",
    "spec/catalog-v4/binding.schema.json": f"{SITE_BASE_URL}spec/catalog-v4/binding.schema.json",
}
SPKI_ED25519_PREFIX = bytes.fromhex("302a300506032b6570032100")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
KEY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
CATALOG_BINDING_INDEX_URL_PATTERN = (
    r"^https://thefunkybits\.github\.io/rgm/catalog/v[1-9][0-9]*/index\.json$"
)
PLACEHOLDER = re.compile(r"TODO|TBD|REPLACE|example\.invalid|localhost|C:\\Users\\")
FORBIDDEN_HTML = re.compile(r"<(?:script|iframe|form)(?:\s|>)", re.IGNORECASE)
REMOTE_MEDIA = re.compile(
    r"(?:src|poster)\s*=\s*[\"']https?://|@import|url\(\s*[\"']?https?://",
    re.IGNORECASE,
)
LOCAL_REFERENCE = re.compile(r"(?:href|src)=\"([^\"]+)\"")
TEST_CATALOG_PATHS = (
    "catalog.json",
    "moo1/1.3/profile.json",
    "moo1/cover.png",
    "moo1/intro.mp4",
    "moo1/resolve.json",
    "moo1/star-map.png",
    "moo1/tech-breakthrough.png",
)
TEST_CATALOG_OFFERS = [
    {
        "provider": "GOG",
        "url": "https://www.gog.com/game/master_of_orion_1_2",
        "disclosureRequired": False,
    }
]
TEST_CATALOG_MEDIA = {
    "cover": "moo1/cover.png",
    "gallery": [
        {
            "kind": "video",
            "path": "moo1/intro.mp4",
            "poster": "moo1/cover.png",
        },
        {"kind": "image", "path": "moo1/star-map.png"},
        {"kind": "image", "path": "moo1/tech-breakthrough.png"},
    ],
}


def fail(message: str) -> None:
    raise SystemExit(message)


def base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"Invalid JSON {path}: {error}")


def verify_signature(openssl: str, public_key: bytes, payload: Path, signature: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix="rgm-catalog-verify-") as temporary:
        root = Path(temporary)
        public_path = root / "public.der"
        signature_path = root / "signature.bin"
        public_path.write_bytes(SPKI_ED25519_PREFIX + public_key)
        signature_path.write_bytes(signature)
        result = subprocess.run(
            (
                openssl,
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(public_path),
                "-keyform",
                "DER",
                "-rawin",
                "-in",
                str(payload),
                "-sigfile",
                str(signature_path),
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            fail(f"Ed25519 verification failed for {payload}: {result.stderr.strip()}")


def find_openssl() -> str | None:
    on_path = shutil.which("openssl")
    if on_path:
        return on_path
    if os.name != "nt":
        return None
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(variable)
        if root:
            candidate = Path(root) / "Git" / "usr" / "bin" / "openssl.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def validate_test_catalog(payloads: dict[str, bytes]) -> None:
    if tuple(payloads) != TEST_CATALOG_PATHS:
        fail("test: logical path inventory differs from the MOO1 media contract")
    try:
        catalog = json.loads(payloads["catalog.json"].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        fail(f"test: catalog.json is invalid: {error}")
    titles = catalog.get("titles")
    if not isinstance(titles, list) or len(titles) != 1 or titles[0].get("id") != "moo1":
        fail("test: catalog must contain only MOO1")
    title = titles[0]
    if title.get("offers") != TEST_CATALOG_OFFERS:
        fail("test: MOO1 offer differs from the approved GOG offer")
    if title.get("media") != TEST_CATALOG_MEDIA:
        fail("test: MOO1 media differs from the approved cover and gallery order")


def validate_current_release_feed(
    site: Path,
    relative_path: str,
    catalog_version: int,
    openssl: str,
    keys: dict[str, bytes],
) -> dict:
    feed = site / relative_path
    index_path = feed / "index.json"
    envelope_path = feed / "index.signatures.json"
    index_bytes = index_path.read_bytes()
    index = read_json(index_path)
    envelope = read_json(envelope_path)
    digest = sha256(index_bytes)

    if set(index) != {"schemaVersion", "catalogVersion", "minimumAppVersionCode", "files"}:
        fail(f"{relative_path}: current index fields are invalid")
    if set(envelope) != {"schemaVersion", "manifestSha256", "signatures"}:
        fail(f"{relative_path}: current signature envelope fields are invalid")
    if (
        index["schemaVersion"] != 4
        or index["catalogVersion"] != catalog_version
        or type(index["minimumAppVersionCode"]) is not int
        or index["minimumAppVersionCode"] <= 0
    ):
        fail(f"{relative_path}: current index identity is invalid")
    if envelope["schemaVersion"] != 2 or envelope["manifestSha256"] != digest:
        fail(f"{relative_path}: current signature envelope identity is invalid")

    verified_key_id = None
    signatures = envelope["signatures"]
    if not isinstance(signatures, list):
        fail(f"{relative_path}: current signatures are invalid")
    for signature in signatures:
        if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
            continue
        key_id = signature.get("keyId")
        value = signature.get("value")
        public_key = keys.get(key_id)
        if public_key is None or not isinstance(value, str):
            continue
        verify_signature(openssl, public_key, index_path, base64url(value))
        if verified_key_id is not None:
            fail(f"{relative_path}: current feed has multiple trusted signatures")
        verified_key_id = key_id
    if verified_key_id is None:
        fail(f"{relative_path}: current feed has no signature using a published trusted key")

    files = index["files"]
    if not isinstance(files, list) or not files:
        fail(f"{relative_path}: current file inventory is invalid")
    paths = []
    referenced_objects = set()
    for position, record in enumerate(files):
        if not isinstance(record, dict) or set(record) != {"path", "objectPath", "bytes", "sha256"}:
            fail(f"{relative_path}: current file {position} fields are invalid")
        logical_path = record["path"]
        object_path = record["objectPath"]
        expected_hash = record["sha256"]
        expected_size = record["bytes"]
        if (
            not isinstance(logical_path, str)
            or not logical_path
            or not isinstance(object_path, str)
            or not isinstance(expected_hash, str)
            or not SHA256.fullmatch(expected_hash)
            or object_path != f"objects/sha256/{expected_hash}"
            or type(expected_size) is not int
            or expected_size <= 0
        ):
            fail(f"{relative_path}: current file {position} is invalid")
        object_file = feed / object_path
        if not object_file.is_file():
            fail(f"{relative_path}: missing {object_path}")
        data = object_file.read_bytes()
        if len(data) != expected_size or sha256(data) != expected_hash:
            fail(f"{relative_path}: object size or hash differs for {logical_path}")
        paths.append(logical_path)
        referenced_objects.add(object_file.resolve())
    if paths != sorted(set(paths)):
        fail(f"{relative_path}: current file inventory is unsorted or duplicated")

    object_root = feed / "objects" / "sha256"
    if not object_root.is_dir():
        fail(f"{relative_path}: current object directory is missing")
    actual_objects = {path.resolve() for path in object_root.iterdir() if path.is_file()}
    if actual_objects != referenced_objects:
        fail(f"{relative_path}: current object directory differs from signed inventory")
    return {
        "catalogVersion": catalog_version,
        "indexSha256": digest,
        "signatureEnvelopeSha256": sha256(envelope_path.read_bytes()),
        "keyId": verified_key_id,
        "files": files,
        "minimumAppVersionCode": index["minimumAppVersionCode"],
    }


def validate_current_release_record(record: object, feed: dict, label: str) -> dict:
    if not isinstance(record, dict):
        fail(f"{label}: current release record is not an object")
    expected = {
        "schemaVersion",
        "catalogVersion",
        "canonicalPath",
        "indexUrl",
        "indexSchemaVersion",
        "keyId",
        "indexSha256",
        "signatureEnvelopeSha256",
        "files",
        "minimumAppVersionCode",
        "catalogCandidateSha256",
        "contentCommit",
        "publisherCommit",
        "publicationCommit",
        "tagNames",
    }
    if set(record) != expected:
        fail(f"{label}: current release record fields are invalid")
    catalog_version = feed["catalogVersion"]
    if (
        record["schemaVersion"] != 3
        or record["catalogVersion"] != catalog_version
        or record["canonicalPath"] != f"catalog/v{catalog_version}"
        or record["indexUrl"] != f"{SITE_BASE_URL}catalog/v{catalog_version}/index.json"
        or record["indexSchemaVersion"] != 4
    ):
        fail(f"{label}: current release record identity is invalid")
    if not isinstance(record["keyId"], str) or not KEY_ID.fullmatch(record["keyId"]):
        fail(f"{label}: current release record key id is invalid")
    for field in ("indexSha256", "signatureEnvelopeSha256", "catalogCandidateSha256"):
        if not isinstance(record[field], str) or not SHA256.fullmatch(record[field]):
            fail(f"{label}: current release record {field} is invalid")
    for field in ("contentCommit", "publisherCommit", "publicationCommit"):
        if not isinstance(record[field], str) or not GIT_SHA.fullmatch(record[field]):
            fail(f"{label}: current release record {field} is invalid")
    if (
        record["keyId"] != feed["keyId"]
        or record["indexSha256"] != feed["indexSha256"]
        or record["signatureEnvelopeSha256"] != feed["signatureEnvelopeSha256"]
        or record["files"] != feed["files"]
        or record["minimumAppVersionCode"] != feed["minimumAppVersionCode"]
    ):
        fail(f"{label}: current release record differs from signed feed")
    expected_tags = {
        "content": f"catalog/v{catalog_version}-content",
        "publisher": f"catalog/v{catalog_version}-publisher",
        "release": f"catalog/v{catalog_version}",
    }
    if record["tagNames"] != expected_tags:
        fail(f"{label}: current release record tag names are invalid")
    return record


def validate_current_deployment_receipt(
    receipt: object,
    release_record_path: Path,
    record: dict,
    label: str,
) -> dict:
    if not isinstance(receipt, dict):
        fail(f"{label}: current deployment receipt is not an object")
    expected = {"schemaVersion", "catalogVersion", "releaseRecordSha256", "sourceCommit", "site"}
    if set(receipt) != expected:
        fail(f"{label}: current deployment receipt fields are invalid")
    if receipt["schemaVersion"] != 2 or receipt["catalogVersion"] != record["catalogVersion"]:
        fail(f"{label}: current deployment receipt identity is invalid")
    if receipt["releaseRecordSha256"] != sha256(release_record_path.read_bytes()):
        fail(f"{label}: current deployment receipt release record hash differs")
    if not isinstance(receipt["sourceCommit"], str) or not GIT_SHA.fullmatch(receipt["sourceCommit"]):
        fail(f"{label}: current deployment receipt source commit is invalid")
    site = receipt["site"]
    expected_site = {
        "repository",
        "pagesUrl",
        "workflowRunId",
        "workflowUrl",
        "createdAt",
        "completedAt",
        "conclusion",
        "servedFileCount",
        "byteEqualityVerified",
    }
    if not isinstance(site, dict) or set(site) != expected_site:
        fail(f"{label}: current deployment site receipt fields are invalid")
    workflow_run_id = site["workflowRunId"]
    if (
        site["repository"] != "https://github.com/TheFunkyBits/rgm"
        or site["pagesUrl"] != SITE_BASE_URL
        or type(workflow_run_id) is not int
        or workflow_run_id <= 0
        or site["workflowUrl"] != f"https://github.com/TheFunkyBits/rgm/actions/runs/{workflow_run_id}"
        or site["conclusion"] != "success"
        or site["servedFileCount"] != len(record["files"]) + 2
        or site["byteEqualityVerified"] is not True
    ):
        fail(f"{label}: current deployment site receipt is invalid")
    created_at = _parse_utc_instant(site["createdAt"], label, "created time")
    completed_at = _parse_utc_instant(site["completedAt"], label, "completed time")
    if completed_at < created_at:
        fail(f"{label}: current deployment site receipt times are invalid")
    return receipt


def _parse_utc_instant(value: object, label: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        fail(f"{label}: current deployment {field} is invalid")
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        fail(f"{label}: current deployment {field} is invalid: {error}")


def validate_catalog_reservation(repository: Path, catalog_version: int) -> dict:
    path = repository / "catalog-reservations" / f"v{catalog_version}.json"
    if not path.is_file() or path.is_symlink():
        fail(f"Catalog v{catalog_version} reservation is missing")
    reservation = read_json(path)
    expected = {
        "schemaVersion",
        "catalogVersion",
        "state",
        "reason",
        "indexSha256",
        "signatureEnvelopeSha256",
    }
    if not isinstance(reservation, dict) or set(reservation) != expected:
        fail(f"Catalog v{catalog_version} reservation fields are invalid")
    if (
        reservation["schemaVersion"] != 2
        or reservation["catalogVersion"] != catalog_version
        or reservation["state"] != "reserved"
        or not isinstance(reservation["reason"], str)
        or not reservation["reason"]
    ):
        fail(f"Catalog v{catalog_version} reservation identity is invalid")
    for field in ("indexSha256", "signatureEnvelopeSha256"):
        if not isinstance(reservation[field], str) or not SHA256.fullmatch(reservation[field]):
            fail(f"Catalog v{catalog_version} reservation {field} is invalid")
    if (
        (repository / "site" / "catalog" / f"v{catalog_version}").exists()
        or (repository / "site" / "catalog" / f"v{catalog_version}").is_symlink()
        or (repository / "catalog-releases" / f"v{catalog_version}.json").exists()
        or (repository / "catalog-releases" / f"v{catalog_version}.json").is_symlink()
    ):
        fail(f"Catalog v{catalog_version} reservation must not have a published tree or release record")
    return reservation


def validate_retired_catalog_absence(repository: Path, site: Path, catalog_version: int) -> None:
    current_paths = (
        site / "catalog" / f"v{catalog_version}",
        site / "spec" / f"catalog-v{catalog_version}",
        repository / "catalog-history" / f"v{catalog_version}.json",
        repository / "catalog-lifecycle" / f"v{catalog_version}",
        repository / "catalog-releases" / f"v{catalog_version}.json",
        repository / "catalog-requests" / f"v{catalog_version}.json",
    )
    for path in current_paths:
        if path.exists() or path.is_symlink():
            fail(
                f"retired catalog v{catalog_version} remains in current tree: "
                f"{path.relative_to(repository).as_posix()}"
            )


def find_current_catalog_record(repository: Path, catalog_version: int) -> Path | None:
    candidates = []
    for directory in ("catalog-releases", "catalog-history"):
        path = repository / directory / f"v{catalog_version}.json"
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                fail(f"catalog v{catalog_version} record is invalid: {path}")
            candidates.append(path)
    if len(candidates) > 1:
        fail(f"catalog v{catalog_version} release/history records are ambiguous")
    return candidates[0] if candidates else None


def validate_deprecation_record_binding(
    repository: Path,
    catalog_version: int,
    record_path: Path,
) -> None:
    events_root = repository / "catalog-lifecycle" / f"v{catalog_version}" / "events"
    if not events_root.is_dir() or events_root.is_symlink():
        fail(f"deprecated catalog v{catalog_version} lifecycle events are missing")
    event_paths = sorted(events_root.glob("*.json"))
    if not event_paths:
        fail(f"deprecated catalog v{catalog_version} lifecycle events are missing")
    expected_hash = sha256(record_path.read_bytes())
    for event_path in event_paths:
        event = read_json(event_path)
        if event.get("releaseRecordSha256") != expected_hash:
            fail(f"deprecated catalog v{catalog_version} lifecycle record hash differs")


def verify_current_deployment_source(
    site: Path,
    release_record_path: Path,
    record: dict,
    receipt: dict,
    git: str,
) -> None:
    repository = site.parent
    source_commit = receipt["sourceCommit"]
    ancestor = subprocess.run(
        (git, "-C", str(repository), "merge-base", "--is-ancestor", source_commit, "HEAD"),
        capture_output=True,
        check=False,
    )
    if ancestor.returncode != 0:
        fail("current deployment source commit is not reachable")

    record_relative = release_record_path.relative_to(repository).as_posix()
    historical_record = git_bytes(
        git,
        repository,
        ("show", f"{source_commit}:{record_relative}"),
        "current deployment release record",
    )
    if historical_record != release_record_path.read_bytes():
        fail("current deployment release record differs from source bytes")

    source_path = f"site/{record['canonicalPath']}"
    listed = git_bytes(
        git,
        repository,
        ("ls-tree", "-r", "--name-only", source_commit, "--", source_path),
        "current deployment catalog tree",
    ).decode("utf-8").splitlines()
    prefix = f"{source_path}/"
    if not listed or any(not path.startswith(prefix) for path in listed):
        fail("current deployment catalog source inventory is invalid")
    historical_paths = [path.removeprefix(prefix) for path in listed]
    current_root = site / record["canonicalPath"]
    current_paths = sorted(
        path.relative_to(current_root).as_posix()
        for path in current_root.rglob("*")
        if path.is_file()
    )
    if historical_paths != current_paths:
        fail("current deployment catalog source inventory differs")
    for relative in historical_paths:
        historical_bytes = git_bytes(
            git,
            repository,
            ("show", f"{source_commit}:{source_path}/{relative}"),
            "current deployment catalog tree",
        )
        if historical_bytes != (current_root / relative).read_bytes():
            fail(f"current deployment catalog source bytes differ for {relative}")


def git_bytes(git: str, repository: Path, arguments: tuple[str, ...], label: str) -> bytes:
    result = subprocess.run(
        (git, "-C", str(repository), *arguments),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        fail(f"{label}: Git verification failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def resolve_local_reference(site: Path, html: Path, reference: str) -> Path | None:
    parsed = urlparse(reference)
    if parsed.scheme or reference.startswith("#") or reference.startswith("mailto:"):
        return None
    clean = parsed.path
    if clean.startswith(SITE_BASE_PATH):
        target = site / clean.removeprefix(SITE_BASE_PATH)
    elif clean == SITE_BASE_PATH.rstrip("/"):
        target = site
    elif clean.startswith("/"):
        fail(f"Unexpected root-relative reference in {html}: {reference}")
    else:
        target = html.parent / clean
    if target.is_dir() or clean.endswith("/"):
        target = target / "index.html"
    return target


def validate_publication_identity(site: Path) -> None:
    for relative, expected_id in SCHEMA_IDS.items():
        if read_json(site / relative).get("$id") != expected_id:
            fail(f"Published schema id is invalid: {relative}")
    current_index = read_json(site / "spec/catalog-v4/index.schema.json")
    current_envelope = read_json(site / "spec/catalog-v4/signatures.schema.json")
    current_binding = read_json(site / "spec/catalog-v4/binding.schema.json")
    if current_index.get("required") != [
        "schemaVersion",
        "catalogVersion",
        "minimumAppVersionCode",
        "files",
    ]:
        fail("Published current catalog index schema is invalid")
    if current_envelope.get("required") != ["schemaVersion", "manifestSha256", "signatures"]:
        fail("Published current catalog signature schema is invalid")
    if current_binding.get("required") != [
        "schemaVersion",
        "indexUrl",
        "catalogVersion",
        "keyId",
        "indexSha256",
        "signatureEnvelopeSha256",
    ]:
        fail("Published current catalog binding schema is invalid")
    binding_properties = current_binding.get("properties")
    if (
        current_binding.get("additionalProperties") is not False
        or not isinstance(binding_properties, dict)
        or binding_properties.get("schemaVersion", {}).get("const") != 2
        or binding_properties.get("indexUrl", {}).get("pattern")
        != CATALOG_BINDING_INDEX_URL_PATTERN
    ):
        fail("Published current catalog binding schema is invalid")


def validate_current_catalog_release(
    site: Path,
    openssl: str,
    git: str,
    keys: dict[str, bytes],
) -> dict:
    catalog_version = 4
    relative_path = f"catalog/v{catalog_version}"
    feed = validate_current_release_feed(
        site,
        relative_path,
        catalog_version,
        openssl,
        keys,
    )
    release_record_path = site.parent / "catalog-releases" / f"v{catalog_version}.json"
    record = validate_current_release_record(
        read_json(release_record_path),
        feed,
        release_record_path.relative_to(site.parent).as_posix(),
    )
    receipt_path = site.parent / "deployment-receipts" / f"v{catalog_version}.json"
    receipt = validate_current_deployment_receipt(
        read_json(receipt_path),
        release_record_path,
        record,
        receipt_path.relative_to(site.parent).as_posix(),
    )
    verify_current_deployment_source(site, release_record_path, record, receipt, git)
    print(
        f"verified {relative_path}: version={catalog_version} "
        f"index={feed['indexSha256']} objects={len(feed['files'])}"
    )
    return feed


def validate_site(site: Path, openssl: str, git: str) -> None:
    if not site.is_dir():
        fail(f"Site root does not exist: {site}")
    for relative in REQUIRED:
        if not (site / relative).is_file():
            fail(f"Missing required site file: {relative}")
    if any(path.is_symlink() for path in site.rglob("*")):
        fail("Site contains a symbolic link")

    key_document = read_json(site / "trust" / "catalog-keys.json")
    if key_document.get("schemaVersion") != 1 or key_document.get("algorithm") != "ed25519":
        fail("Published catalog key document is invalid")
    keys = {key_document["keyId"]: base64url(key_document["value"])}
    if any(len(value) != 32 for value in keys.values()):
        fail("Published Ed25519 key is not 32 raw bytes")

    validate_publication_identity(site)
    tag_names = git_bytes(git, site.parent, ("tag", "--list"), "catalog lifecycle").decode(
        "utf-8"
    ).splitlines()
    inventory = scan_catalog_inventory(site.parent, tag_names=tag_names)
    validate_current_catalog_release(site, openssl, git, keys)
    for catalog_version in inventory.reserved_versions:
        validate_catalog_reservation(site.parent, catalog_version)
    for catalog_version in (
        inventory.pending_retirement_versions | inventory.retired_versions
    ):
        validate_retired_catalog_absence(site.parent, site, catalog_version)
    for catalog_version in inventory.deprecated_versions:
        record_path = find_current_catalog_record(site.parent, catalog_version)
        if record_path is None:
            fail(f"deprecated catalog v{catalog_version} has no current release material")
        validate_deprecation_record_binding(site.parent, catalog_version, record_path)

    text_files = [
        path for path in site.rglob("*") if path.is_file() and path.suffix in {".html", ".json", ".css"}
    ]
    for path in text_files:
        text = path.read_text(encoding="utf-8")
        if PLACEHOLDER.search(text):
            fail(f"Placeholder or private path found in {path}")
        if path.suffix == ".html" and FORBIDDEN_HTML.search(text):
            fail(f"Executable or embedded interactive content found in {path}")
        if REMOTE_MEDIA.search(text):
            fail(f"Remote embedded asset found in {path}")

    html_files = list(site.rglob("*.html"))
    for html in html_files:
        source = html.read_text(encoding="utf-8")
        for reference in LOCAL_REFERENCE.findall(source):
            target = resolve_local_reference(site, html, reference)
            if target is not None and not target.is_file():
                fail(f"Broken local reference in {html}: {reference}")

    print(f"verified site: files={sum(1 for path in site.rglob('*') if path.is_file())} html={len(html_files)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("site", type=Path)
    parser.add_argument("--openssl", default=find_openssl())
    parser.add_argument("--git", default=shutil.which("git"))
    arguments = parser.parse_args()
    if not arguments.openssl:
        fail("OpenSSL is required for Ed25519 verification")
    if not arguments.git:
        fail("Git is required for historical catalog verification")
    validate_site(arguments.site.resolve(), arguments.openssl, arguments.git)


if __name__ == "__main__":
    main()
