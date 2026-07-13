# Vendored workflow_core

This directory contains a vendored copy of the shared `workflow_core`
package (node catalog, serializer, validator, compiler) used by the
LocalServer workflow engine.

- **Source of truth**: `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/`
  (the portal Lambda layer). Never edit files under `vendor/workflow_core/`
  directly — change the source and re-vendor.
- **Import path**: `from workflow_engine.vendor import workflow_core`
  (all internal imports in workflow_core are package-relative, so no
  `sys.path` manipulation is needed).
- **Runtime dependency**: `jsonschema` (already pinned in
  `src/backend/requirements.txt`).

## Re-vendoring

From the repository root:

```bash
./src/backend/workflow_engine/vendor/re_vendor.sh
```

This copies the current source of `workflow_core` into this directory,
excluding caches. Re-run it whenever the shared package changes so the
edge stays in sync with the portal and the test sandbox.
