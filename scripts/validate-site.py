#!/usr/bin/env python3

import argparse
import base64
import hashlib
import json
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
    "catalogs/v2/demo/index.json",
    "catalogs/v2/demo/index.signatures.json",
    "catalogs/v2/test/index.json",
    "catalogs/v2/test/index.signatures.json",
)
FEEDS = ("demo", "test")
SPKI_ED25519_PREFIX = bytes.fromhex("302a300506032b6570032100")
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


def validate_feed(site: Path, feed_name: str, openssl: str, keys: dict[str, bytes]) -> dict:
    feed = site / "catalogs" / "v2" / feed_name
    index_path = feed / "index.json"
    envelope_path = feed / "index.signatures.json"
    index_bytes = index_path.read_bytes()
    index = read_json(index_path)
    envelope = read_json(envelope_path)
    digest = sha256(index_bytes)

    if index.get("schemaVersion") != 2:
        fail(f"{feed_name}: index schema is not 2")
    if envelope.get("schemaVersion") != 1:
        fail(f"{feed_name}: signature schema is not 1")
    if index.get("catalogId") != envelope.get("catalogId"):
        fail(f"{feed_name}: index and signature identities differ")
    if digest != envelope.get("manifestSha256"):
        fail(f"{feed_name}: index digest differs from signature envelope")

    verified = False
    for signature in envelope.get("signatures", []):
        if signature.get("algorithm") != "ed25519":
            continue
        public_key = keys.get(signature.get("keyId"))
        if public_key is None:
            continue
        verify_signature(openssl, public_key, index_path, base64url(signature["value"]))
        verified = True
        break
    if not verified:
        fail(f"{feed_name}: no signature uses a published trusted key")

    files = index.get("files", [])
    paths = [record.get("path") for record in files]
    if not files or paths != sorted(paths) or len(paths) != len(set(paths)):
        fail(f"{feed_name}: logical path inventory is empty, unsorted, or duplicated")

    referenced_objects = set()
    payloads = {}
    for record in files:
        logical_path = record.get("path")
        object_path = record.get("objectPath", "")
        expected_hash = record.get("sha256", "")
        if object_path != f"objects/sha256/{expected_hash}":
            fail(f"{feed_name}: object path is not content-addressed for {logical_path}")
        object_file = feed / object_path
        if not object_file.is_file():
            fail(f"{feed_name}: missing {object_path}")
        data = object_file.read_bytes()
        if len(data) != record.get("bytes") or sha256(data) != expected_hash:
            fail(f"{feed_name}: object size or hash differs for {logical_path}")
        referenced_objects.add(object_file.resolve())
        payloads[logical_path] = data

    if feed_name == "test":
        validate_test_catalog(payloads)

    object_root = feed / "objects" / "sha256"
    actual_objects = {path.resolve() for path in object_root.iterdir() if path.is_file()}
    if actual_objects != referenced_objects:
        fail(f"{feed_name}: object directory differs from signed inventory")

    print(
        f"verified {feed_name}: revision={index['revision']} "
        f"index={digest} objects={len(files)}"
    )
    return {
        "revision": index["revision"],
        "indexSha256": digest,
        "signatureEnvelopeSha256": sha256(envelope_path.read_bytes()),
        "objectCount": len(files),
    }


def resolve_local_reference(site: Path, html: Path, reference: str) -> Path | None:
    parsed = urlparse(reference)
    if parsed.scheme or reference.startswith("#") or reference.startswith("mailto:"):
        return None
    clean = parsed.path
    if clean.startswith("/rgm/"):
        target = site / clean.removeprefix("/rgm/")
    elif clean == "/rgm":
        target = site
    elif clean.startswith("/"):
        return None
    else:
        target = html.parent / clean
    if target.is_dir() or clean.endswith("/"):
        target = target / "index.html"
    return target


def validate_site(site: Path, openssl: str) -> None:
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

    lock_path = site.parent / "publication-lock.json"
    publication_lock = read_json(lock_path)
    if publication_lock.get("schemaVersion") != 1:
        fail("Publication lock schema is not 1")
    if publication_lock.get("keyId") != key_document.get("keyId"):
        fail("Publication lock key id differs from the published key")
    feed_results = {
        feed_name: validate_feed(site, feed_name, openssl, keys)
        for feed_name in FEEDS
    }
    if publication_lock.get("feeds") != feed_results:
        fail("Publication lock differs from the signed feed trees")

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
    parser.add_argument("--openssl", default=shutil.which("openssl"))
    arguments = parser.parse_args()
    if not arguments.openssl:
        fail("OpenSSL is required for Ed25519 verification")
    validate_site(arguments.site.resolve(), arguments.openssl)


if __name__ == "__main__":
    main()
