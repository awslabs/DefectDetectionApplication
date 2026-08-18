# Vendored workflow_core

This directory contains a vendored copy of the shared `workflow_core`
package (node catalog, serializer, validator, compiler) used by the
LocalServer workflow engine.

- **Single source of truth**:
  `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/`
  (the portal Lambda layer). Every functional change — node descriptors,
  compiler capture plans, validator rules, catalog data models — is made
  there and only there.
- **This tree is generated, never authored**: `vendor/workflow_core/` is
  produced exclusively by `re_vendor.sh`. Do not hand-edit any file under it,
  not even a one-line descriptor tweak or a comment. A hand edit is
  overwritten by the next re-vendor and will be caught by the drift guard
  below in the meantime.
- **Import path**: `from workflow_engine.vendor import workflow_core`
  (all internal imports in workflow_core are package-relative, so no
  `sys.path` manipulation is needed).
- **Runtime dependency**: `jsonschema` (already pinned in
  `src/backend/requirements.txt`).

## Re-vendoring

From anywhere in the repository (paths resolve relative to the script):

```bash
./src/backend/workflow_engine/vendor/re_vendor.sh
```

`re_vendor.sh` is an exact mirror, not a merge: it `rm -rf`s
`vendor/workflow_core/` and rebuilds it with
`rsync -a --exclude='__pycache__' --exclude='*.pyc'` from the portal layer
copy, then prints the vendored `*.py` files. Because the destination is
removed first, files deleted upstream disappear here too, and nothing local
survives. Compiled caches are the only intentional difference between the
two trees.

Re-run it whenever the shared package changes so the device stays in sync
with the portal and the test sandbox. Verify with:

```bash
diff -r \
  edge-cv-portal/backend/layers/workflow_core/python/workflow_core \
  src/backend/workflow_engine/vendor/workflow_core \
  -x '__pycache__'
```

## Drift guard

`test/backend-test/workflow_engine/test_vendored_catalog_mirror.py` fails the
suite when the two copies diverge. It locates both trees by walking up from
the test file, SHA-256s the bytes of each mirrored file, and asserts
byte-equality, reporting both digests and the offending relative paths on
failure.

**Current scope**: the guard covers `catalog/nodes.py` and `catalog/models.py`
only (the `MIRRORED_FILENAMES` tuple in that module). A tree-wide walk over
every `workflow_core/**/*.py` — specified as Property 11 in
`.kiro/specs/vlm-bedrock-parity/` (Requirements 5.1, 5.2) — is **not yet
implemented**. Until it lands, drift in the compiler, validator or serializer
is not caught automatically; run the `diff -r` above after any change to the
shared package.

## Catalog-baseline regeneration (descriptor edits)

A descriptor change also needs
`edge-cv-portal/backend/layers/workflow_core/tests/catalog_baseline.json`
refreshed, because `test_bug_catalog_preservation.py` asserts the live catalog
against it.

**The baseline is not a blind snapshot of the live catalog.** Some entries
deliberately record *pre-change* values as bug-condition evidence for earlier
specs. A wholesale dump —

```python
# WRONG: overwrites deliberately-recorded pre-change values
json.dump({d.type_id: dataclasses.asdict(d) for d in NODE_CATALOG}, f)
```

— produces extra deltas on `mqtt_publish`: `broker_host.required` flipping
`True` → `False`, the added `greengrass` parameter, `python:awsiotsdk` on the
device mappings, and the `arm64_jp7` mapping. Those are the recorded
pre-change values for the `workflow-manager-integration-bugfixes` Bug 2
condition;
`test_bug_catalog_preservation.py::TestMqttPublishPreservation::test_broker_host_changed_only_in_required_flag`
asserts `baseline["required"] is True`, so a blind regeneration breaks that
preservation property.

**Scoped refresh — the correct maintenance path:**

1. Load the committed `catalog_baseline.json`.
2. Replace **only** the descriptors your feature intentionally changed, with
   their live `dataclasses.asdict(descriptor)` serialization. Leave every other
   entry exactly as committed.
3. Assert the recorded type-id set still equals the live catalog's type-id set
   (this catches an accidentally added or dropped node type).
4. Re-dump with `json.dumps(baseline, indent=2, sort_keys=True)` plus a
   trailing newline (`ensure_ascii` left at its default). This matches the
   committed file's formatting byte-for-byte, so the diff shows only your
   intended descriptor delta.
5. Review the diff. Any delta outside the descriptors you edited means stop
   and investigate — do not commit it.

Then run `cd edge-cv-portal/backend && python3 -m pytest layers/workflow_core/tests/ -q`.
