# Repository Agent Guidance

## Scope

This public repository owns the static RGM site, publication locks and evidence, signed catalog
feeds, public artifacts, and site validation. Keep private content, policy sources, captures,
credentials, signing keys, tester data, and generated private release packets outside it.

## Portable Automation

Site automation supports Windows and Linux. Use Python 3.11 or newer with only the standard library
for validation and filesystem orchestration.

- Pass external process arguments as arrays. Never invoke a local shell to interpret a command.
- Discover or accept an executable path once, validate it, and use that explicit path thereafter.
- Serialize paths with `/` on every host.
- Check filesystem containment after normalization and symbolic-link or reparse-point resolution.
- Stage writes beside their destination and replace atomically; use a journalled rollback for a
  multi-file mutation.
- Write machine-readable results to stdout and human diagnostics to stderr.

Keep implementation and tests in this repository. Share serialized schemas and golden vectors,
not a cross-repository utility package. Pages publication remains an explicitly authorized CI
operation and must depend on successful read-only validation.
