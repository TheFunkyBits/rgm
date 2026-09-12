# RGM public site

Static source for `https://thefunkybits.github.io/rgm-publication/`.

It hosts the public RGM privacy policy, reviewer guidance, Three in a Row artifacts and provenance, catalog trust/specification pages, and generated signed feeds under `site/catalogs/v2/`.

Shared workspace policy is in [the workspace instructions](../../.github/copilot-instructions.md).

Signed feeds are generated from the private content repository:

```text
cd ../content
python -m scripts.rgm_content_tools stage-external-catalog --release-candidate=<release-candidate.json> --private-key=<private-key.json> --publication-root=../publication --client-root=../client --java=<java>
```

The site contains no analytics, tracking scripts, forms, iframes, or remote media.

The frozen feed revisions and hashes are recorded in `publication-lock.json`. Completed Pages,
live-byte, signature, and Android cache verification evidence is recorded in
`deployment-record.json`.
