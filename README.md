# RGM public site

Static source for `https://thefunkybits.github.io/rgm/`.

It hosts the public RGM privacy policy, reviewer guidance, catalog trust/specification pages, and generated signed releases under `site/catalog/vN/`.

Shared workspace policy is in [the workspace instructions](../../.github/copilot-instructions.md).

Catalog candidates and signed stages are created by the private content repository; this repository
owns promotion into its reviewed worktree:

```text
cd ../rgm-content
python -m scripts.rgm_content_tools catalog-candidate --catalog-release=../rgm/catalog-requests/vN.json --minimum-app-version-code=<n> --client-root=../rgm-client --publication-root=../rgm --java=<java> --git=<git>
python -m scripts.rgm_content_tools stage-external-catalog --catalog-candidate=<catalog-candidate.json> --private-key=<private-key.json> --publication-root=../rgm --client-root=../rgm-client --java=<java>
cd ../rgm
python -m scripts.rgm_publication_tools promote-external-catalog --stage-directory=<catalog-stage> --catalog-candidate=<catalog-candidate.json> --publication-root=. --client-root=../rgm-client --java=<java> --git=<git>
```

The site contains no analytics, tracking scripts, forms, iframes, or remote media.

Current immutable release identities are recorded at `catalog-releases/vN.json`. Historical
catalog evidence is retained under `catalog-history/`; retired root records are recoverable from
the external cutover history index and are not current release inputs.

After a source record and catalog tree are committed and successfully deployed, a separate
`deployment-receipts/vN.json` records the deployed source commit, Pages workflow, and direct
byte-equality result. Receipts are never created before deployment and do not modify release
records. From a clean checkout at that deployed source commit, record the observed successful
workflow and direct byte comparison:

```text
cd ../rgm-client
java -classpath gradle/wrapper/gradle-wrapper.jar org.gradle.wrapper.GradleWrapperMain :tools:catalog-publisher:run --args="record-deployment-receipt --publication=../rgm --public-key=../rgm/site/trust/catalog-keys.json --git=<git> --catalog-version=<n> --source-commit=<commit> --workflow-run-id=<n> --created-at=<instant> --completed-at=<instant> --served-file-count=<n> --byte-equality-verified=true" --no-daemon --console=plain
```

Commit only that receipt separately, then use `verify-deployment-receipt` against the committed
file. The recorder derives the receipt path and release-record hash from the catalog version and
refuses an existing receipt.
