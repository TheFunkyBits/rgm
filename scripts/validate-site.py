#!/usr/bin/env python3

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

REQUIRED = (
    "index.html",
    "privacy/index.html",
    "trust/catalog-keys.json",
    "spec/catalog-v2/index.schema.json",
    "spec/catalog-v4/index.schema.json",
    "spec/catalog-v4/signatures.schema.json",
    "spec/catalog-v4/binding.schema.json",
    "catalog/v2/index.json",
    "catalog/v2/index.signatures.json",
)
HISTORICAL_RECORDS = (
    "catalog-history/v2.json",
)
RESERVED_CATALOG_VERSIONS = (3,)
HISTORICAL_V2_SOURCE_PATH = "site/catalogs/v2/test"
SITE_BASE_URL = "https://thefunkybits.github.io/rgm/"
SITE_BASE_PATH = "/rgm/"
SCHEMA_IDS = {
    "spec/catalog-v2/index.schema.json": f"{SITE_BASE_URL}spec/catalog-v2/index.schema.json",
    "spec/catalog-v2/signatures.schema.json": f"{SITE_BASE_URL}spec/catalog-v2/signatures.schema.json",
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


def validate_feed(
    site: Path,
    relative_path: str,
    catalog_id: str,
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

    if index.get("schemaVersion") != 2:
        fail(f"{relative_path}: index schema is not 2")
    if envelope.get("schemaVersion") != 1:
        fail(f"{relative_path}: signature schema is not 1")
    if index.get("catalogId") != catalog_id:
        fail(f"{relative_path}: catalog identity is invalid")
    if index.get("catalogId") != envelope.get("catalogId"):
        fail(f"{relative_path}: index and signature identities differ")
    if digest != envelope.get("manifestSha256"):
        fail(f"{relative_path}: index digest differs from signature envelope")

    verified_key_id = None
    for signature in envelope.get("signatures", []):
        if signature.get("algorithm") != "ed25519":
            continue
        public_key = keys.get(signature.get("keyId"))
        if public_key is None:
            continue
        verify_signature(openssl, public_key, index_path, base64url(signature["value"]))
        verified_key_id = signature["keyId"]
        break
    if verified_key_id is None:
        fail(f"{relative_path}: no signature uses a published trusted key")

    files = index.get("files", [])
    paths = [record.get("path") for record in files]
    if not files or paths != sorted(paths) or len(paths) != len(set(paths)):
        fail(f"{relative_path}: logical path inventory is empty, unsorted, or duplicated")

    referenced_objects = set()
    payloads = {}
    for record in files:
        logical_path = record.get("path")
        object_path = record.get("objectPath", "")
        expected_hash = record.get("sha256", "")
        if object_path != f"objects/sha256/{expected_hash}":
            fail(f"{relative_path}: object path is not content-addressed for {logical_path}")
        object_file = feed / object_path
        if not object_file.is_file():
            fail(f"{relative_path}: missing {object_path}")
        data = object_file.read_bytes()
        if len(data) != record.get("bytes") or sha256(data) != expected_hash:
            fail(f"{relative_path}: object size or hash differs for {logical_path}")
        referenced_objects.add(object_file.resolve())
        payloads[logical_path] = data

    if catalog_id == "test":
        validate_test_catalog(payloads)

    object_root = feed / "objects" / "sha256"
    actual_objects = {path.resolve() for path in object_root.iterdir() if path.is_file()}
    if actual_objects != referenced_objects:
        fail(f"{relative_path}: object directory differs from signed inventory")

    print(
        f"verified {relative_path}: revision={index['revision']} "
        f"index={digest} objects={len(files)}"
    )
    return {
        "legacyRevision": index["revision"],
        "indexSha256": digest,
        "signatureEnvelopeSha256": sha256(envelope_path.read_bytes()),
        "keyId": verified_key_id,
        "files": files,
        "minimumAppVersionCode": index["minimumAppVersionCode"],
    }


def validate_legacy_record(record: object, label: str) -> dict:
    if not isinstance(record, dict):
        fail(f"{label}: legacy record is not an object")
    if "catalogVersion" in record:
        fail(f"{label}: legacy record must not contain catalogVersion")
    expected = {
        "schemaVersion",
        "kind",
        "legacyCatalogId",
        "legacyRevision",
        "catalogPath",
        "indexSchemaVersion",
        "keyId",
        "indexSha256",
        "signatureEnvelopeSha256",
        "files",
        "minimumAppVersionCode",
        "contentCommit",
        "publisherCommit",
        "publicationCommit",
        "historicalSource",
    }
    if set(record) != expected:
        fail(f"{label}: legacy record fields are invalid")
    if record["schemaVersion"] != 2 or record["kind"] != "legacy-catalog-evidence":
        fail(f"{label}: legacy record identity is invalid")
    if record["legacyCatalogId"] != "test":
        fail(f"{label}: legacy record catalog id is invalid")
    if type(record["legacyRevision"]) is not int or record["legacyRevision"] <= 0:
        fail(f"{label}: legacy record revision is invalid")
    catalog_path = record["catalogPath"]
    if catalog_path != "catalog/v2":
        fail(f"{label}: legacy record canonical path is invalid")
    if record["indexSchemaVersion"] != 2:
        fail(f"{label}: legacy record index schema is invalid")
    if not isinstance(record["keyId"], str) or not KEY_ID.fullmatch(record["keyId"]):
        fail(f"{label}: legacy record key id is invalid")
    for field in ("indexSha256", "signatureEnvelopeSha256"):
        if not isinstance(record[field], str) or not SHA256.fullmatch(record[field]):
            fail(f"{label}: legacy record {field} is invalid")
    for field in ("contentCommit", "publisherCommit", "publicationCommit"):
        if not isinstance(record[field], str) or not GIT_SHA.fullmatch(record[field]):
            fail(f"{label}: legacy record {field} is invalid")
    historical_source = record["historicalSource"]
    if (
        not isinstance(historical_source, dict)
        or set(historical_source) != {"commit", "path", "tree"}
        or historical_source["commit"] != record["publicationCommit"]
        or historical_source["path"] != HISTORICAL_V2_SOURCE_PATH
        or not isinstance(historical_source["tree"], str)
        or not GIT_SHA.fullmatch(historical_source["tree"])
    ):
        fail(f"{label}: legacy source provenance is invalid")
    if type(record["minimumAppVersionCode"]) is not int or record["minimumAppVersionCode"] <= 0:
        fail(f"{label}: legacy record compatibility is invalid")
    files = record["files"]
    if not isinstance(files, list) or not files:
        fail(f"{label}: legacy record files are invalid")
    paths = []
    for position, entry in enumerate(files):
        if not isinstance(entry, dict) or set(entry) != {"path", "objectPath", "bytes", "sha256"}:
            fail(f"{label}: legacy record file {position} is invalid")
        path = entry["path"]
        object_path = entry["objectPath"]
        digest = entry["sha256"]
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(object_path, str)
            or object_path != f"objects/sha256/{digest}"
            or type(entry["bytes"]) is not int
            or entry["bytes"] <= 0
            or not isinstance(digest, str)
            or not SHA256.fullmatch(digest)
        ):
            fail(f"{label}: legacy record file {position} is invalid")
        paths.append(path)
    if paths != sorted(set(paths)):
        fail(f"{label}: legacy record file inventory is invalid")
    return record


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


def verify_historical_source(site: Path, record: dict, git: str) -> None:
    source = record["historicalSource"]
    repository = site.parent
    commit = source["commit"]
    source_path = source["path"]
    ancestor = subprocess.run(
        (git, "-C", str(repository), "merge-base", "--is-ancestor", commit, "HEAD"),
        capture_output=True,
        check=False,
    )
    if ancestor.returncode != 0:
        fail(f"{record['catalogPath']}: historical source commit is not reachable")
    tree = git_bytes(git, repository, ("rev-parse", f"{commit}:{source_path}"), record["catalogPath"])
    if tree.decode("ascii").strip() != source["tree"]:
        fail(f"{record['catalogPath']}: historical source tree differs from recorded provenance")
    listed = git_bytes(
        git,
        repository,
        ("ls-tree", "-r", "--name-only", commit, "--", source_path),
        record["catalogPath"],
    ).decode("utf-8").splitlines()
    prefix = f"{source_path}/"
    if not listed or any(not path.startswith(prefix) for path in listed):
        fail(f"{record['catalogPath']}: historical source inventory is invalid")
    historical_paths = [path.removeprefix(prefix) for path in listed]
    current_root = site / record["catalogPath"]
    current_paths = sorted(
        path.relative_to(current_root).as_posix()
        for path in current_root.rglob("*")
        if path.is_file()
    )
    if historical_paths != current_paths:
        fail(f"{record['catalogPath']}: historical source inventory differs from current tree")
    for relative in historical_paths:
        historical_bytes = git_bytes(
            git,
            repository,
            ("show", f"{commit}:{source_path}/{relative}"),
            record["catalogPath"],
        )
        if historical_bytes != (current_root / relative).read_bytes():
            fail(f"{record['catalogPath']}: historical source bytes differ for {relative}")


def git_bytes(git: str, repository: Path, arguments: tuple[str, ...], label: str) -> bytes:
    result = subprocess.run(
        (git, "-C", str(repository), *arguments),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        fail(f"{label}: historical source Git verification failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def validate_recorded_legacy_feed(
    site: Path,
    record: dict,
    openssl: str,
    keys: dict[str, bytes],
) -> None:
    result = validate_feed(
        site,
        record["catalogPath"],
        record["legacyCatalogId"],
        openssl,
        keys,
    )
    for field in (
        "legacyRevision",
        "indexSha256",
        "signatureEnvelopeSha256",
        "keyId",
        "files",
        "minimumAppVersionCode",
    ):
        if record[field] != result[field]:
            fail(f"{record['catalogPath']}: legacy record differs from catalog tree")


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
    records = {
        relative: validate_legacy_record(read_json(site.parent / relative), relative)
        for relative in HISTORICAL_RECORDS
    }
    validate_recorded_legacy_feed(
        site,
        records["catalog-history/v2.json"],
        openssl,
        keys,
    )
    verify_historical_source(site, records["catalog-history/v2.json"], git)
    for catalog_version in RESERVED_CATALOG_VERSIONS:
        validate_catalog_reservation(site.parent, catalog_version)

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
