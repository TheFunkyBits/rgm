# RGM public site

Static source for `https://thefunkybits.github.io/rgm/`.

It hosts the public RGM privacy policy, reviewer guidance, Three in a Row artifacts and provenance, catalog trust/specification pages, and generated signed feeds under `site/catalogs/v2/`.

Signed feeds are generated from the private content repository; private signing keys are stored outside all repositories:

```powershell
../rgm-content/scripts/Publish-ExternalCatalog.ps1
```

The site contains no analytics, tracking scripts, forms, iframes, or remote media.
