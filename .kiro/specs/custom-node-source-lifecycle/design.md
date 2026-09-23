# Design Document: Custom Node Source Lifecycle

## Overview

This feature extends the Node_Designer's Plugin_Record Detail_Page from a read-only status view into the place where a plugin lives after creation:

- **Source_Editor** — the existing read-only file viewer on `PluginDetail.tsx` becomes the same Tabs + Textarea editor the CreateWizard and GeneratePanel already use, backed by the existing `GET/PUT /plugins/{id}/versions/{v}/source` routes extended with a bulk read, a `replace` mode with deletions, a lifecycle guard, and a `Source_Revision` counter that marks existing Plugin_Artifacts stale. "Save as new version" is a new `POST .../versions/{v}/new-version` route that server-side-copies the Source_Tree to `v+1` and applies the edits.
- **Git sync** — a new `git_sync.py` Lambda with its own DynamoDB tables (`GitConnections`, `GitSyncOperations`), a Secrets Manager secret per Git_Connection, and a new `dda-plugin-git-sync` CodeBuild project (the only place in the portal with a `git` binary and outbound internet; modelled on the existing `dda-plugin-fetch` project). Verify, Push, and Pull are all asynchronous Sync_Operations that settle through EventBridge exactly like plugin builds and imports do today. The token is resolved inside CodeBuild via a `SECRETS_MANAGER`-typed environment variable, so no Lambda ever holds it.
- **Diagnostic-aware code assistance** — `code_assist.py` gains an optional `diagnostics` block, a `plugin_source` contract for non-Python files, and a multi-file request/response shape (`context.files` in, optional `target_file` out). `CodeAssistPanel` gains a collapsible "Error output" field and accepts a pre-seeded Diagnostic_Context; the Detail_Page's failed-arch rows and the simulator's failure view get a "Fix with AI" action that opens the editor with that context.
- **Architecture_Addition and republish** — a new `POST .../versions/{v}/architectures` route in `plugin_builds.py` renders any missing scaffold `builds/{arch}/meson.build`, appends to `requested_architectures` (which becomes a monotonic union everywhere), and builds only the added architectures. `plugin_components.py` replaces its "registered → short-circuit" rule with artifact-set change detection and publishes `v.0.{n+1}` patch versions; because workflow recipes already pin `>=v.0.0 <v+1.0.0` and the deployment gate already reads the record's component pointer, newly built architectures become deployable without repackaging workflows. `arm64_jp7` joins the Build_Target_Registry with a sixth CodeBuild project and build image.

### Key findings from investigation

- **Editing already exists server-side, not client-side.** `plugin_records.put_version_source` (PUT `.../source`, body `{files}`) writes files under `plugin-sources/{usecase}/{plugin}/{version}/`, validates scaffold kinds via `workflow_core.scaffold.scaffold_defects`, and is called only from CreateWizard/GeneratePanel. It never deletes files, has no lifecycle guard, and leaves `artifacts` untouched — so an edited-and-rebuilt version cannot be distinguished from one whose binaries match the source. `get_version_source` lists `{file, size}` and serves one file per request (512 KiB cap), which is why the Detail_Page viewer is a Select + `<pre>`.
- **New versions never copy source.** `PUT /plugins/{id}` with `new_version: true` mints `v+1` with an empty `plugin-sources/.../{v+1}/` prefix and does not carry `requested_architectures`. Requirement 1.6 needs a server-side S3 copy plus edit application, hence a dedicated route.
- **`requested_architectures` is replaced per build round** (`start_builds`: `SET ... requested_architectures = :r`), so a retry of one failed arch makes `builds_view.settled` and packaging consider only that arch, and `adjust_revision` validates `architecture in requested_architectures`. Requirement 6.5 makes it a union; `list_append`-style merging in `start_builds` keeps every existing caller working.
- **Registered components never update.** `plugin_components.package_plugin_component` short-circuits whenever `component.status == 'registered'`, and Greengrass component versions (`{v}.0.0`) are immutable. Any rebuild after registration — the exact thing the Source_Editor enables — leaves devices on stale binaries. `workflow_packaging.plugin_version_requirement` pins `>={v}.0.0 <{v+1}.0.0`, and `deployments.plugin_component_architectures` reads `record.component.architectures`, so patch-version republish is compatible with everything downstream as long as the component pointer is updated.
- **Scaffold build configs are per architecture.** `render_scaffold` emits `builds/{arch}/meson.build` for each declared architecture and `scaffold_defects` requires one per architecture in `provenance.scaffoldDeclaration`; adding an architecture must render that file and update the declaration or every later save is rejected.
- **The fetch project is the git template.** `dda-plugin-fetch` (STANDARD_7_0, no VPC, role scoped to `plugin-sources/*`) already proves CodeBuild can clone from the internet with no credentials; it deletes `.git` and never pushes. Lambdas have no git binary, so all git work must run in CodeBuild. No Secrets Manager, SSM SecureString, or symmetric KMS usage exists anywhere in the portal — credential storage is greenfield; the KMS key in the stack is ECC sign/verify only.
- **`arm64_jp7` is half-plumbed.** It is in `workflow_core.DEVICE_ARCHITECTURES`, the frontend `DEVICE_ARCHITECTURES`/`ARCHITECTURE_LABELS`, `workflow_packaging.ARCH_TO_GG_PLATFORM` (`aarch64`), the LocalServer component map, and `deployments.py`; it is missing from `PLUGIN_BUILD_ARCHITECTURES` (CDK), `plugin_importer.PLATFORM_GSTREAMER_VERSIONS`, `build-and-push.sh`, and there is no `Dockerfile.arm64_jp7` under `edge-cv-portal/plugin-build-images/`. Picking it in the Build panel today returns 500 `BUILD_PROJECT_UNCONFIGURED`. `src/backend/Dockerfile.jp7` (spec jetpack7-support) pins the base `nvcr.io/nvidia/cuda:13.0.2-devel-ubuntu24.04@sha256:5dc1bca2…` — Ubuntu 24.04 ships GStreamer 1.24 and meson 1.3.
- **The code assistant is single-file and context-blind.** `code_assist.py` accepts `{usecase_id, surface, contract, prompt, current_code?, context?{node_type, parameters}}`, validates via `ast.parse` + entry-point intersection, and knows nothing about build logs, other files, or the scaffold layout. `CodeAssistPanel` props are `{usecaseId, surface, contract, context?, editorCode, onAccept}` with a pure reducer (`idle`/`submitting`/`reviewing`). The Detail_Page already holds the material to feed it: `PluginArtifactEntry.logTail` per failed arch and `SimulationResultsDocument.error {code?, message?, errorOutput?}`.
- **Route capacity.** Node_Designer routes live in the nested `node-designer-api-stack.ts` (own Cognito authorizer, salted `CfnDeployment`), not in the ~491/500-resource `api-gateway-stack.ts`. New routes go there; new root resources must be added to the deployment's `addDependency` list.
- **Frontend conventions.** Plain Cloudscape (`Tabs`, `Textarea rows=24`, `Multiselect`, `Alert`, `StatusIndicator`, `ConfirmationModal`), `nodeDesignerApi` in `pages/node-designer/api.ts`, types in `types.ts`, pure logic in sibling `.ts` modules with vitest/fast-check tests, role gating inline via `useAuth().user.role`.

## Architecture

```mermaid
graph TB
    subgraph Frontend[React frontend - pages/node-designer]
        PD[PluginDetail.tsx<br/>lifecycle, builds, Add architectures,<br/>Fix with AI, Git panel]
        SE[SourceEditor.tsx<br/>Tabs + Textarea, add/delete file,<br/>Save / Save as new version, dirty tracking]
        GP[GitSyncPanel.tsx<br/>link, push, pull, operation history]
        GC[GitConnections.tsx<br/>per-Use_Case connection CRUD]
        CAP[CodeAssistPanel<br/>+ error-output field, diagnostics prop,<br/>target_file display]
        PD --> SE
        PD --> GP
        SE --> CAP
    end
    subgraph Records[PluginRecordsHandler]
        PR[plugin_records.py<br/>GET source?all=true, PUT source mode=replace,<br/>POST new-version, source_revision]
    end
    subgraph Builds[PluginBuildsHandler]
        PB[plugin_builds.py<br/>POST architectures, union requested_architectures,<br/>sourceRevision + stale in builds_view,<br/>buildable_architectures]
    end
    subgraph Components[PluginComponentsHandler]
        PC[plugin_components.py<br/>artifact-set change detection,<br/>v.0.n republish]
    end
    subgraph Sync[GitSyncHandler - new]
        GS[git_sync.py<br/>connections CRUD, link, push, pull,<br/>operation polling, EventBridge results]
        SM[Secrets Manager<br/>dda-portal/git-connections/*]
        CB[CodeBuild dda-plugin-git-sync<br/>verify | push | pull runner,<br/>token via SECRETS_MANAGER env]
        GS --> SM
        GS -->|StartBuild| CB
        CB -->|result.json + EventBridge| GS
    end
    subgraph Assist[WorkflowGeneratorHandler]
        CA[code_assist.py<br/>diagnostics, context.files,<br/>plugin_source contract, target_file]
    end
    SE --> PR
    PD --> PB
    PB -->|builds settled| PC
    GP --> GS
    CAP --> CA
    CB -->|s3 sync| S3[(portal artifacts bucket<br/>plugin-sources/ , plugin-git-sync/)]
    PR --> S3
```

### Request flows

1. **Edit and rebuild (dev version).** Detail_Page loads `GET .../source?all=true` → user edits → `PUT .../source` `{files, delete, mode: 'replace', expected_source_revision}` → backend validates scaffold, writes/deletes objects, `source_revision += 1` → response `{source_revision, stale_architectures}` → Detail_Page shows "rebuild required" markers → user clicks "Rebuild" → `POST .../build` (existing) → `start_builds` stamps `sourceRevision` on each new arch entry.
2. **Save as new version (test/prod).** `POST .../versions/{v}/new-version` `{files, delete}` → backend copies every object of `v`'s prefix to `v+1`'s prefix (server-side `CopyObject`), applies edits/deletions, validates scaffold on the resulting map, writes the `v+1` item (dev, pending, `git` link and `requested_architectures` copied, `provenance.forkedFrom = v`, `source_revision = 1`) → 201 → Detail_Page navigates to `v+1`.
3. **Connect a repository.** UseCaseAdmin creates a Git_Connection → Lambda `CreateSecret` (`dda-portal/git-connections/{usecase}/{connection_id}`, value `{"token": ...}`) → item written with `status: verifying` → `StartBuild dda-plugin-git-sync` with `SYNC_KIND=verify` → CodeBuild `git ls-remote` using the token it resolved itself → writes `plugin-git-sync/{operation_id}/result.json` → EventBridge → `git_sync.handle_sync_result` → connection `status: verified | failed`.
4. **Push.** `POST .../versions/{v}/git/push` `{message?, force?}` → single-flight check → Sync_Operation `queued` → `StartBuild` with `SYNC_KIND=push`, `SOURCE_PREFIX`, `BRANCH`, `REPO_PATH`, `LAST_SYNC_COMMIT`, `FORCE`, `MANIFEST_JSON` → runner clones, runs the Divergence_Guard, replaces the directory, commits, pushes (one rebase-and-retry) → result.json → EventBridge → operation `succeeded` + version `git.last_sync` updated.
5. **Pull.** `POST .../versions/{v}/git/pull` `{ref?, mode}` → lifecycle check for `in_place` → `StartBuild` with `SYNC_KIND=pull`, `REF`, `REPO_PATH`, `STAGING_PREFIX=plugin-git-sync/{operation_id}/tree/` → runner fetches, filters, size-checks, `aws s3 sync` to staging → result.json → Lambda validates (scaffold kinds), then installs: in-place = delete target prefix objects, copy staging → target, `source_revision += 1`; new-version = create `v+1` item and copy staging → its prefix; cleans staging; records `git.last_sync`.
6. **Fix with AI.** Failed arch row → "Fix with AI" → Detail_Page opens the Source_Editor on the most likely file (see Components §6) with `CodeAssistPanel` pre-seeded `diagnostics={kind:'build', architecture, text: logTail}` → user submits → `POST /code-assist` with `current_code`, `context.files`, `context.active_file`, `diagnostics` → model returns `{code, notes, target_file?}` → panel shows "applies to plugin/meson.build" → Accept replaces that file in the editor → "Save and rebuild failed architectures".
7. **Add architectures.** Detail_Page → "Add architectures" Multiselect (Build_Target_Registry minus requested) → `POST .../versions/{v}/architectures` `{architectures}` → lifecycle guard (not prod) → scaffold kinds: render missing `builds/{arch}/meson.build`, update `provenance.scaffoldDeclaration`, bump `source_revision` if files were added → `submit_arch_builds(new)` → `requested_architectures = union` → builds settle → `trigger_component_packaging` → `package_plugin_component` sees the artifact set changed → publishes `v.0.{n+1}` → component pointer updated → `deployments.plugin_component_architectures` now includes the new arch.

### Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Where git runs | A dedicated CodeBuild project `dda-plugin-git-sync` (STANDARD_7_0, SMALL, no VPC), separate from `dda-plugin-fetch` | Lambdas have no git; the fetch project's role must stay credential-free (public imports) and its buildspec is clone-only. A separate project gets its own role with `secretsmanager:GetSecretValue` on exactly the git-connection secret ARN pattern. |
| Token handling | Secrets Manager secret per Git_Connection; Lambda role has Create/Put/Delete/Describe but **not** GetSecretValue; CodeBuild resolves `GIT_TOKEN` via `environmentVariablesOverride` `type: SECRETS_MANAGER` | No portal Lambda can ever read a token; CodeBuild masks Secrets-Manager-typed values in logs; rotation is `PutSecretValue`; deletion is `DeleteSecret` with the default recovery window. |
| Git auth in the runner | `GIT_ASKPASS` helper echoing `$GIT_TOKEN`; username `x-access-token` (GitHub) or `oauth2` (GitLab) via `GIT_USERNAME`; token never placed in the remote URL | URLs appear in git error output and CodeBuild logs; askpass keeps the token out of both. |
| Structured results | Runner always writes `plugin-git-sync/{operation_id}/result.json` in `post_build`; Lambda reads it and falls back to the CloudWatch log tail only when it is missing | Controlled failures (`diverged`, `not_found`, `authentication`) need machine-readable categories and file lists; EventBridge only carries SUCCEEDED/FAILED. Same delivery path as builds/imports (`aws.codebuild` state change → Lambda). |
| Result delivery | New EventBridge rule `dda-portal-git-sync-results` filtered to the git-sync project, targeting `GitSyncHandler` | Keeps `plugin_builds.py` and the existing rule/test untouched; the git-sync Lambda handles API + EventBridge like `plugin_builds.handler` does. |
| Pull safety | Runner syncs to a per-operation staging prefix; the Lambda validates and installs atomically (copy, then delete stale objects) and creates the new version item only after validation | A Pull must never leave a half-written Source_Tree or a version item without source (Requirements 4.3–4.8). |
| Divergence_Guard | `git diff --quiet {LAST_SYNC_COMMIT} HEAD -- {REPO_PATH}` when a last-sync commit exists and is reachable; unreachable last-sync commit ⇒ treated as diverged unless `force` | Detects out-of-band repository edits under the mirrored path without a full three-way merge; the portal's model is "replace the directory", so any divergence is a conflict by definition. |
| Push commit scope | Replace `REPO_PATH` wholesale (`git rm -r --cached` + copy + `git add -A REPO_PATH`), write `dda-plugin.json`, never touch other paths, never `--force` | Requirements 3.3–3.4; the manifest gives pulls and humans provenance and is excluded on pull. |
| Git_Link storage | `git` block on each version item, inherited by `new-version` and by Pull new-version mode; editable per version | There is no plugin-level item; inheriting keeps the common case (one link per plugin) automatic while letting an old version target a different branch. |
| Stale tracking | `source_revision` (int) on the version item; `sourceRevision` on each arch artifact entry stamped at build start; `stale = entry.sourceRevision < item.source_revision`; missing values read as 1 | Cheap, monotonic, backward compatible (Requirement 10.1); no checksum-of-tree needed. |
| `requested_architectures` semantics | Monotonic union in `start_builds` and the new architectures route; `builds_settled` unchanged | Requirement 6.5; fixes the retry-shrinks-the-set gap without touching `builds_view` consumers. |
| Component republish | Change detection on `{arch: checksum}` vs `component.artifact_checksums`; publish `{v}.0.{revision+1}`; pointer gains `revision` and `artifact_checksums` | Greengrass versions are immutable; the workflow pin range already tolerates patches; the deployment gate already reads the pointer. Also fixes stale binaries after any rebuild. |
| Lifecycle rules | Edit/pull-in-place: `dev` only; new-version: any state; Architecture_Addition: `dev` or `test`; Push: any state | Mirrors custom-node-designer 9.3/9.13/14.1; `prod` binaries change only through a new reviewed version. |
| Multi-file code assist | Request carries `context.active_file`, `context.files` (other text files, ≤ 256 KiB total, largest omitted first), `diagnostics`; response may name `target_file` from the provided paths | Build failures are usually in C/meson while the user sits on the Python tab; a bounded whole-tree context lets one call diagnose and fix the right file. |
| New contract | `plugin_source`: any non-hook file; validation = non-empty | C, meson, and README have no Python entry point; `frame_hook` validation still applies when `target_file == plugin/frame_processing_hook.py`. |
| jp7 build image | `Dockerfile.arm64_jp7` FROM the same digest-pinned `nvidia/cuda:13.0.2-devel-ubuntu24.04` base as `src/backend/Dockerfile.jp7`; same apt set as `Dockerfile.arm64_jp6` (noble names), same `dda-plugin-build` entrypoint | Toolchain parity with the JP7 LocalServer; GStreamer 1.24 and meson 1.3 from noble; `PLATFORMS_WITH_SUBPROJECT_FALLBACK` gains `arm64_jp7`. |
| Build_Target_Registry exposure | `buildable_architectures` field on `GET .../builds` (already polled by the Detail_Page); `start_builds`/architectures route return 400 `BUILD_TARGET_UNAVAILABLE` for others | No new route; the frontend stops hardcoding `DEVICE_ARCHITECTURES` for build pickers. |

## Components and Interfaces

### 1. `plugin_records.py` — Source_Editor backend (PluginRecordsHandler)

**`GET /plugins/{id}/versions/{v}/source?all=true`** (extends `get_version_source`)

Returns every object under `source_s3_prefix` in one response:

```json
{
  "source_revision": 3,
  "files": [
    {"file": "plugin/frame_processing_hook.py", "size": 1820, "content": "..."},
    {"file": "builds/x86_64/meson.build", "size": 640, "content": "..."},
    {"file": "assets/sample.bin", "size": 812345, "binary": true}
  ],
  "count": 3,
  "truncated": false
}
```

Rules: a file gets `content` when `size ≤ MAX_SOURCE_FILE_BYTES` (512 KiB) and its bytes decode as UTF-8; otherwise `binary: true` and no content (Requirement 1.2). Total inline content is capped at 4 MiB; beyond that further files are listed without content and `truncated: true` is set. Without `all=true` the existing list/single-file behaviors are byte-identical.

**`PUT /plugins/{id}/versions/{v}/source`** (extends `put_version_source`)

```json
{
  "files": {"relative/path": "content"},
  "delete": ["relative/path"],
  "mode": "merge" | "replace",
  "expected_source_revision": 3
}
```

- `mode` defaults to **`replace`** — the wizards' existing contract: CreateWizard/GeneratePanel PUT the complete scaffold map with no `mode`, and the pre-existing buildability check (`test_non_buildable_scaffold_source_is_rejected`) depends on the submitted map being the whole tree (Requirement 10.3). `replace` means `files` is the complete Source_Tree: objects not in `files` are deleted. The Source_Editor sends `mode: 'merge'` explicitly for partial saves (write the given files, touch nothing else). `delete` is honored in both modes. `files` may be empty when `delete` is non-empty. *(Implementation note: an earlier draft of this design said the default was `merge`; it is `replace`.)*
- Path confinement applies to every key of `files` and `delete` (`os.path.normpath`, no `..`, not `.`/empty, and — so that normalization is a fixed point — no segment with leading/trailing whitespace and no ASCII control character; found by the Property 2 test with `"0\r/"`, which used to normalize to `"0\r"` and then to `"0"`) → 400 `INVALID_FILE_PATH {file}` (Requirement 1.3). The frontend `normalizeSourcePath` applies the identical rule.
- Lifecycle guard: `lifecycle_state != 'dev'` → 409 `SOURCE_LOCKED {lifecycle_state, hint: 'save as new version'}` (Requirement 1.5). The guard is bypassed for no caller; the wizards always operate on a fresh `dev` v1.
- Optimistic concurrency: when `expected_source_revision` is present and differs from the stored value → 409 `SOURCE_REVISION_CONFLICT {current}` (protects two tabs / a concurrent Pull).
- Scaffold validation runs on the **resulting** tree: `resulting = (current listing − delete − keys not in files when replace) ∪ files`; contents of unchanged files are not fetched — `scaffold_defects` only needs presence plus the contents of files that are in `files` (the hook file content check reads from the submitted map when present, else from S3 for that one file). Defects → 422 `SCAFFOLD_INVALID {defects}` and nothing written (Requirement 1.7).
- On success: put/delete objects, then one `update_item`: `SET source_revision = if_not_exists(source_revision, :one) + :inc, updated_at = :t`. Response:

```json
{"files": ["..."], "deleted": ["..."], "count": 4, "source_revision": 4,
 "stale_architectures": ["x86_64", "arm64_jp6"]}
```

`stale_architectures` = arches whose artifact `sourceRevision` (default 1) `<` new `source_revision` (Requirement 1.8). Audit `update_plugin_source` gains `deleted` and `source_revision`.

**`POST /plugins/{id}/versions/{v}/new-version`** (new)

```json
{"files": {...}, "delete": [...], "name": "...", "description": "..."}
```

Requires `node-designer:manage`. Steps: `latest = get_latest_version_item`; `target = latest.version + 1`; list objects of `v`'s prefix; server-side `copy_object` each to the target prefix except paths in `delete` or overridden in `files`; put `files`; validate scaffold on the resulting map (kind `scaffold` with declaration) **before** any write (the resulting map is computable from the listing); build the item with `new_version_item(...)` plus `requested_architectures` (copied), `git` (copied, minus `last_sync`), `source_revision: 1`, `provenance = latest.provenance ∪ body.provenance ∪ {forkedFrom: v, createdBy, createdAt}`; `put_item` with `attribute_not_exists(version)`; on a conditional failure delete the copied objects and return 409 `VERSION_CONFLICT`. Audit `create_plugin_record_version` with `forked_from`. Returns 201 `{plugin: version_detail(item), source_revision: 1}` (Requirement 1.6).

`version_detail` additionally exposes `source_revision` (default 1) and `git` (see Data Models).

### 2. `plugin_builds.py` — stale tracking, union semantics, architectures route (PluginBuildsHandler)

- `submit_arch_builds` stamps `sourceRevision: item.get('source_revision', 1)` on every `building` entry; `store_signed_artifact` callers (prebuilt) stamp it too; `handle_build_result` preserves the entry's `sourceRevision` when it settles (`entry['sourceRevision'] = current.get('sourceRevision', 1)`).
- `is_stale(item, entry) -> bool` = `int(entry.get('sourceRevision', 1)) < int(item.get('source_revision', 1))` (Requirements 1.9, 10.1).
- `builds_view` adds per-arch `stale` and `sourceRevision`, plus top-level `source_revision`, `buildable_architectures: sorted(BUILD_PROJECTS)` (Requirement 6.7), and `component: {version, revision, architectures, status}` (the pointer summary, so the Detail_Page can show which arches the deployable component currently carries).
- `start_builds`: `requested = sorted(set(requested_architectures(item)) | set(all_archs))` written instead of `all_archs` (Requirement 6.5). Unconfigured arches → 400 `BUILD_TARGET_UNAVAILABLE {architectures, buildable}` instead of the current 500 (Requirement 6.8).
- **`POST /plugins/{id}/versions/{v}/architectures`** (new, `add_architectures`):

```json
{"architectures": ["arm64_jp7", "x86_64_nvidia"]}
```

  Validation (400 `INVALID_ARCHITECTURES` with per-arch reasons `unknown | already_requested | unavailable | deepstream_restricted`), lifecycle guard (`prod` → 409 `LIFECYCLE_LOCKED {lifecycle_state, hint}`), single-flight (any arch currently `building`/`queued` → 409 `BUILDS_IN_PROGRESS`). For `kind == 'scaffold'` with a declaration: `render_scaffold(declaration ∪ {architectures: existing + added})`, take `build_config_path(arch)` for each added arch, `put_object` only where `head_object` returns 404 (never overwrite, Requirement 6.3), then `provenance.scaffoldDeclaration` rewritten with the extended `architectures` list, and `source_revision += 1` **only if** at least one file was written. Then `entries = submit_arch_builds(item, added)`; one `update_item`: `SET artifacts = :a (merged), requested_architectures = :union, updated_at REMOVE components_triggered` (+ the provenance/source_revision sets when applicable). Audit `add_plugin_architectures`. Returns 202 `builds_view` (Requirement 6.4).

  Existing artifacts are never modified by this route (the newly rendered meson file changes nothing for already-built arches — but the Source_Revision bump does mark them stale by definition; the Detail_Page explains "build configuration added for arm64_jp7" as the reason, and a rebuild of the old arches is optional). To avoid spurious staleness the bump is applied only when files were actually added.

### 3. `plugin_components.py` — artifact-set change detection and Component_Revision

```python
def component_version_for(plugin_version: int, revision: int = 0) -> str:
    return f"{int(plugin_version)}.0.{int(revision)}"

def artifact_checksums(item) -> Dict[str, str]:
    return {arch: entry['checksum'] for arch, entry in (item.get('artifacts') or {}).items()
            if entry.get('buildStatus') == 'succeeded' and entry.get('checksum')}

def needs_republish(item) -> bool:
    component = item.get('component') or {}
    if component.get('status') != 'registered':
        return True
    recorded = component.get('artifact_checksums') or {a: None for a in component.get('architectures') or []}
    return artifact_checksums(item) != recorded
```

`package_plugin_component`: `if not needs_republish(item): short-circuit` (Requirement 8.2); otherwise `revision = int(component.get('revision', 0)) + (1 if component.get('status') == 'registered' else 0)` — the first registration of a version is `v.0.0`; each subsequent publish increments. Staging/promotion keys gain the revision segment (`{COMPONENT_S3_PREFIX}/{plugin_id}/{version}/{revision}/{arch}/`) so previous component versions' artifacts stay intact (Requirement 8.4). `set_component_pointer` writes `{name, version: 'v.0.n', revision: n, arn, architectures, artifact_checksums, status, packagedAt, failure}` (Requirement 8.3). A registration `ConflictException` for `v.0.n` (retry after a crash) re-describes the component and records the pointer as today.

Legacy pointers (`status: registered`, no `revision`, no `artifact_checksums`) compare by architecture set only (`{a: None}` above), so an unchanged legacy component short-circuits and the first real change publishes `v.0.1` (Requirement 10.2).

`trigger_component_packaging` is unchanged (guarded by `builds_settled` + `components_triggered`); both `start_builds` and the architectures route `REMOVE components_triggered`, so every build round can re-trigger.

### 4. `deployments.py` / `workflow_packaging.py` — no behavioral change required

- `plugin_component_architectures(record)` already reads `record.component.architectures`; the republish updates it (Requirement 8.5). `parse_plugin_component_ref` reads the leading integer of the component version (patch-insensitive) — a unit test pins this.
- `plugin_version_requirement` already emits `>={v}.0.0 <{v+1}.0.0` (Requirement 8.6) — a unit test pins it.
- `components.list_components` shows `dda.plugin.*` versions; the newest `v.0.n` appears alongside earlier ones. The Detail_Page shows the current pointer's version.

### 5. `git_sync.py` — Git_Sync_Service (new GitSyncHandler Lambda, 120 s)

Same conventions as `plugin_records.py` (`parse_body`, `error_response`, `authorize_record_access`, `forbidden_response`, audit). Reads `GIT_CONNECTIONS_TABLE`, `GIT_SYNC_OPERATIONS_TABLE`, `GIT_SYNC_PROJECT_NAME`, `GIT_SECRET_PREFIX` (`dda-portal/git-connections`), `PLUGIN_GIT_SYNC_PREFIX` (`plugin-git-sync`).

Routes (all Cognito-authorized, registered in `node-designer-api-stack.ts`):

| Method | Path | Permission | Behavior |
|---|---|---|---|
| GET | `/git-connections?usecase_id=` | `node-designer:read` | list connections (no `secret_arn`) |
| POST | `/git-connections` | `node-designer:manage` | create (Requirement 2.1–2.5) → 202 `{connection, operation}` |
| GET | `/git-connections/{cid}` | read | detail incl. last verification |
| PUT | `/git-connections/{cid}` | manage | update name/url/branch/token; re-verify when url or token changed → 202 |
| DELETE | `/git-connections/{cid}` | manage | `DeleteSecret` (default 30-day recovery window), delete item (Requirement 2.8) |
| POST | `/git-connections/{cid}/verify` | manage | re-run verification → 202 |
| PUT | `/plugins/{id}/versions/{v}/git` | manage | set Git_Link `{connection_id, branch?, path?}` (Requirement 3.1) |
| DELETE | `/plugins/{id}/versions/{v}/git` | manage | remove Git_Link (keeps `last_sync` in provenance history) |
| POST | `/plugins/{id}/versions/{v}/git/push` | manage | start Push → 202 `{operation}` |
| POST | `/plugins/{id}/versions/{v}/git/pull` | manage | start Pull → 202 `{operation}` |
| GET | `/plugins/{id}/versions/{v}/git/operations` | read | history newest first (Requirement 4.10) |
| GET | `/git-sync-operations/{opId}` | read (Use_Case of the op) | poll |

Validation: `repo_url` must parse with scheme `https` and a host (400 `INVALID_REPO_URL`, Requirement 2.2); `provider ∈ {github, gitlab}`; `path` normalized with `posixpath.normpath`, rejected if absolute, empty, `.`, or containing `..` (400 `INVALID_REPO_PATH`, Requirement 3.1); default path `sanitize_plugin_name(item.name, plugin_id)` (reuse from `plugin_builds`); default branch from the connection.

Guards: connection `status != 'verified'` → 409 `CONNECTION_NOT_VERIFIED {status}` (Requirement 2.6); in-flight op on the version (conditional `SET active_sync_operation = :op` with `attribute_not_exists(active_sync_operation)`) → 409 `SYNC_IN_PROGRESS {operation_id}` (Requirement 3.12); Pull `in_place` on non-dev → 409 `SOURCE_LOCKED` (Requirement 4.2); unlinked version → 409 `GIT_LINK_REQUIRED`.

`start_sync_operation(kind, connection, item?, target)`: writes the `GitSyncOperations` item (`queued`), then `codebuild.start_build(projectName=GIT_SYNC_PROJECT_NAME, environmentVariablesOverride=[...])` with:

| Variable | Type | Value |
|---|---|---|
| `SYNC_KIND` | PLAINTEXT | `verify` / `push` / `pull` |
| `OPERATION_ID`, `USECASE_ID`, `PLUGIN_ID`, `PLUGIN_VERSION` | PLAINTEXT | attribution (echoed back in the EventBridge detail) |
| `REPO_URL`, `GIT_PROVIDER`, `GIT_USERNAME` | PLAINTEXT | connection fields; username `x-access-token` for github, `oauth2` for gitlab |
| `GIT_TOKEN` | **SECRETS_MANAGER** | `{secret_arn}:token` — resolved by CodeBuild, masked in logs |
| `BRANCH`, `REPO_PATH`, `DEFAULT_BRANCH` | PLAINTEXT | Git_Link / connection |
| `SOURCE_PREFIX` | PLAINTEXT | push: the version's `source_s3_prefix` |
| `LAST_SYNC_COMMIT`, `FORCE` | PLAINTEXT | push: Divergence_Guard inputs (`''`/`0` when absent) |
| `COMMIT_MESSAGE`, `MANIFEST_JSON` | PLAINTEXT | push: message and Sync_Manifest content (built by the Lambda) |
| `REF`, `STAGING_PREFIX` | PLAINTEXT | pull: requested ref (default `BRANCH`) and `plugin-git-sync/{operation_id}/tree/` |
| `RESULT_KEY` | PLAINTEXT | `plugin-git-sync/{operation_id}/result.json` |

The `build_id` is stored on the operation. StartBuild failure → operation `failed` with category `internal` and the active-op lock released.

`handle_sync_result(detail)` (EventBridge, `project-name == GIT_SYNC_PROJECT_NAME`): idempotent on `build_id` (skip if the operation already settled); read `RESULT_KEY` → `result.json` `{ok, category?, message?, commit?, files?, changed_files?, default_branch?, no_changes?, tree_files?, tree_bytes?}`; missing/unparseable → `{ok: false, category: 'internal', message: log tail}` via the same `fetch_log_tail` approach as `plugin_builds` (the Lambda has `logs:GetLogEvents` on `/aws/codebuild/dda-plugin-git-sync`). Then per kind:

- `verify`: connection `status = verified | failed`, `verification = {at, category, message, defaultBranchDetected}`.
- `push`: on ok → version `git.last_sync = {kind: 'push', commit, branch, path, source_revision, by, at}` (Requirement 3.10); operation `result`.
- `pull`: on ok → `install_pulled_tree(operation)`: list staging; for scaffold kinds fetch the hook/C/meson/README contents (only the files `scaffold_defects` inspects; total ≤ 4 MiB) and validate → on defects: operation `failed` `invalid_source {defects}` (Requirement 4.6); `in_place`: list target prefix, `copy_object` staging → target for every staged file, delete target objects not in staging, `source_revision += 1` (Requirement 4.7); `new_version`: create the `v+1` item as in §1 `new-version` (source copied from staging, `provenance.gitPull = {commit, ref, by, at}`) (Requirement 4.8); then `git.last_sync = {kind: 'pull', commit, ref, branch, path, by, at, version}` on the affected version; delete staging objects.
- Always: operation `status`, `finished_at`, `result`/`failure` (with `redact(log_excerpt)`), release `active_sync_operation` on the version (conditional on it equalling this op), audit `git_sync_operation_settled` as the initiating user (Requirement 9.4).

`redact(text)`: replaces `https://[^/@\s]+@` with `https://***@`, any run matching `(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]+` or `glpat-[A-Za-z0-9_-]+`, and the literal token value if (defensively) present in the environment (Requirement 9.5).

### 6. CodeBuild project `dda-plugin-git-sync` and runner script

Infrastructure (`node-designer-stack.ts`): `GitSyncRole` with `s3:GetObject/ListBucket` (prefix-conditioned) on `plugin-sources/*` and `plugin-git-sync/*`, `s3:PutObject/DeleteObject` on `plugin-git-sync/*` only, `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:{region}:{account}:secret:dda-portal/git-connections/*`, CloudWatch logs on `/aws/codebuild/dda-plugin-git-sync`; `codebuild.Project` `dda-plugin-git-sync`, `STANDARD_7_0`, `SMALL`, `timeout 15 min`, no VPC, env `ARTIFACTS_BUCKET` + placeholders. Inline buildspec:

```yaml
version: '0.2'
phases:
  build:
    commands:
      - bash -eu -o pipefail plugin-git-sync/runner.sh   # copied from the source asset below
  post_build:
    commands:
      - aws s3 cp /tmp/result.json "s3://$ARTIFACTS_BUCKET/$RESULT_KEY" || true
```

The runner is kept as a real file, `edge-cv-portal/plugin-build-images/git-sync/runner.sh`, delivered to the build via the project's S3 source (`codebuild.Source.s3` pointing at an asset the stack uploads with `s3deploy.BucketDeployment` under `plugin-git-sync/runner/`) so it is unit-testable with bats-style shell tests and not an inline string. It always ends by writing `/tmp/result.json` (`trap` on EXIT writes an `internal` failure if nothing else did).

Runner behavior (bash, git 2.40+ on STANDARD_7_0):

```
askpass: printf '%s\n' "$GIT_TOKEN"   (GIT_ASKPASS=/tmp/askpass.sh, GIT_TERMINAL_PROMPT=0)
remote URL: "$REPO_URL" with credential.username="$GIT_USERNAME" via -c (never embedded)
classify(): exit-code/stderr → authentication ("Authentication failed"|"could not read Username"|"403"|"401"),
            not_found ("not found"|"404"|"couldn't find remote ref"|"pathspec"),
            unreachable ("Could not resolve host"|"Connection timed out"|"unable to access"), else internal
verify: git ls-remote --symref "$REPO_URL" HEAD → {ok, default_branch}
push:   aws s3 sync "s3://$ARTIFACTS_BUCKET/$SOURCE_PREFIX" /tmp/src --exact-timestamps
        git clone --branch "$BRANCH" --single-branch "$REPO_URL" /tmp/repo
          || (clone default branch; git checkout -b "$BRANCH")   # 3.5
          || (empty repo: git init /tmp/repo; git checkout -b "$BRANCH")
        if [ -n "$LAST_SYNC_COMMIT" ] && [ "$FORCE" != 1 ]; then
          if ! git cat-file -e "$LAST_SYNC_COMMIT^{commit}" || ! git diff --quiet "$LAST_SYNC_COMMIT" HEAD -- "$REPO_PATH"; then
            changed=$(git diff --name-only "$LAST_SYNC_COMMIT" HEAD -- "$REPO_PATH" 2>/dev/null || echo "<unknown>")
            result diverged changed_files=...; exit 0                                     # 3.7
          fi
        fi
        git rm -r -q --cached --ignore-unmatch -- "$REPO_PATH"; rm -rf "/tmp/repo/$REPO_PATH"
        mkdir -p "/tmp/repo/$REPO_PATH"; cp -a /tmp/src/. "/tmp/repo/$REPO_PATH/"
        printf '%s' "$MANIFEST_JSON" > "/tmp/repo/$REPO_PATH/dda-plugin.json"
        git add -A -- "$REPO_PATH"
        if git diff --cached --quiet; then result ok no_changes=true commit=$(git rev-parse HEAD); exit 0; fi   # 3.6
        git -c user.name="DDA Portal" -c user.email="dda-portal@noreply" commit -q -m "$COMMIT_MESSAGE"
        git push origin "$BRANCH" || { git fetch origin "$BRANCH" && git rebase "origin/$BRANCH" \
            && git push origin "$BRANCH"; } || { result push_rejected; exit 0; }              # 3.9
        result ok commit=$(git rev-parse HEAD) files=$(git ls-files "$REPO_PATH" | wc -l)      # 3.10
pull:   git init /tmp/repo && git remote add origin "$REPO_URL"
        git fetch --depth 1 origin "$REF" || git fetch origin "$REF" || { result not_found; exit 0; }
        git checkout -q FETCH_HEAD
        [ -d "/tmp/repo/$REPO_PATH" ] && [ -n "$(ls -A "/tmp/repo/$REPO_PATH")" ] || { result not_found; exit 0; }   # 4.4
        find "/tmp/repo/$REPO_PATH" -type l -delete; rm -f "/tmp/repo/$REPO_PATH/dda-plugin.json"; rm -rf "/tmp/repo/$REPO_PATH/.git"
        files=$(find ... -type f | wc -l); bytes=$(du -sb ... | cut -f1)
        [ "$files" -le 2000 ] && [ "$bytes" -le 52428800 ] || { result invalid_source limit=...; exit 0; }           # 4.5
        aws s3 sync "/tmp/repo/$REPO_PATH/" "s3://$ARTIFACTS_BUCKET/$STAGING_PREFIX" --delete
        result ok commit=$(git rev-parse FETCH_HEAD) tree_files=$files tree_bytes=$bytes
```

Controlled outcomes exit 0 so EventBridge reports SUCCEEDED and the Lambda reads the category from `result.json`; only crashes produce FAILED. A rebase conflict inside `REPO_PATH` cannot occur (the portal's tree replaces the directory), but `git rebase` failing for any reason aborts (`git rebase --abort`) and lands in `push_rejected`.

### 7. `code_assist.py` — diagnostics, multi-file context, `plugin_source` contract (WorkflowGeneratorHandler)

Request additions (all optional, validated in `validate_request`):

```json
{
  "contract": "frame_hook" | "plugin_source" | ...,
  "current_code": "...",
  "context": {
    "node_type": "...", "parameters": [...],
    "active_file": "plugin/gstmyelement.c",
    "files": {"plugin/frame_processing_hook.py": "...", "builds/x86_64/meson.build": "..."},
    "file_paths": ["plugin/frame_processing_hook.py", "plugin/gstmyelement.c", "builds/x86_64/meson.build", "README.md"],
    "kind": "scaffold" | "generated" | "imported"
  },
  "diagnostics": {"kind": "build" | "simulation" | "user", "architecture": "arm64_jp6", "text": "..."}
}
```

- `diagnostics.text` ≤ 16 KiB (400 `INVALID_DIAGNOSTICS` above that; the frontend truncates first, Requirement 5.4); `kind` must be one of the three; `architecture` must be in `DEVICE_ARCHITECTURES` when present.
- `context.files` total ≤ 256 KiB and ≤ 64 entries (400 `INVALID_CONTEXT`); `context.active_file` must be in `context.file_paths` when both are present.
- New `CONTRACTS['plugin_source'] = {entry_points: frozenset(), require_exactly_one: False, signature: 'complete file content', environment: PLUGIN_SOURCE_ENVIRONMENT}`.
- `build_system_prompt(contract, context, diagnostics)` adds, for `surface == 'node-designer'` contracts (`frame_hook`, `plugin_source`): `SCAFFOLD_LAYOUT` (C skeleton element embedding the Python hook via appsink/appsrc, GObject properties → `params`, `builds/{arch}/meson.build` per architecture, `README.md`), the `BUILD_PLATFORMS` table rendered from a shared constant (see Data Models) — one line per Target_Architecture with OS release, GStreamer version, meson/compiler notes — and, when `diagnostics` is present, a `DIAGNOSTIC MODE` block: "Diagnose the root cause in the `{kind}` output below{ for architecture X}, explain it in `notes`, and return the corrected COMPLETE file; if the fix belongs in a different file among FILE PATHS, set `target_file` to that path" (Requirements 5.5, 5.10).
- `build_user_message(prompt, current_code, context, diagnostics)` appends `ACTIVE FILE: {path}`, `OTHER FILES:` with each provided file fenced, `FILE PATHS:` listing all paths (omitted contents noted as `[content omitted, N bytes]`), and `DIAGNOSTIC OUTPUT ({kind}{, arch}):` fenced text (Requirements 5.5–5.6).
- Tool schema gains optional `target_file: string`. `validate_response(code, target_file, contract, context)`: `target_file` absent → active file (or the editor file when no context); present but not in `context.file_paths` → 422 `INVALID_TARGET_FILE {target_file}` (Requirement 5.8); effective contract = `frame_hook` when the effective target is `plugin/frame_processing_hook.py` (`workflow_core.scaffold.HOOK_FILE`), else `plugin_source` when the request contract was `frame_hook`/`plugin_source`; `plugin_source` validation = `code.strip() != ''` (422 `NO_CODE_RETURNED`) (Requirement 5.9). Response: `{code, notes, model_id, contract, target_file}`.
- Workflow-builder contracts are unchanged except that `diagnostics` of kind `user` is accepted and rendered (Requirement 5.3); `context.files` is ignored for them.

### 8. Frontend — Source_Editor, Git panels, code-assist changes (`pages/node-designer`, `components/code-assist`)

**`SourceEditor.tsx`** (new, extracted so CreateWizard/GeneratePanel can adopt it later; they are not modified by this feature): props `{ files: ScaffoldFiles; binaryFiles: {file, size}[]; activeFile; onChange(path, content); onAdd(path); onDelete(path); dirtyPaths: Set<string>; readOnly: boolean; assist?: {usecaseId, contract, parameters, kind, diagnostics?, onTargetApplied} }`. Cloudscape `Tabs` (dirty tabs get a `•` suffix and a "Modified" badge), `Textarea rows=24 spellcheck=false`, an "Add file" `Modal` (path `Input`, validated by the pure `isValidSourcePath` — no leading `/`, no `..` segment, non-empty, ≤ 200 chars), a per-tab "Delete file" with `ConfirmationModal`, binary/oversize files rendered as a disabled tab with size text (Requirement 1.2). `CodeAssistPanel` rendered under the Textarea for every editable tab when the user is UseCaseAdmin/PortalAdmin, with `contract = path === HOOK_FILE ? 'frame_hook' : 'plugin_source'`, `context = {parameters, active_file: path, files: otherTextFiles(files, path, 256 KiB), file_paths: allPaths, kind}`, `onAccept(code, targetFile)` writing into `files[targetFile ?? path]` and marking it dirty (Requirements 1.13, 5.11).

**`sourceEditorState.ts`** (pure): `{ files, baseline, deleted: string[], binary }` with `applyEdit`, `addFile`, `deleteFile`, `dirtyPaths(state)` (paths whose content differs from baseline, plus added and deleted), `toSaveRequest(state, sourceRevision) -> {files: changedOrAdded, delete: deleted, mode: 'replace', expected_source_revision}`, `resetToBaseline(files)`. `mode: 'replace'` is sent with the **full** current map when any file was deleted (so the server's completeness check is exact); otherwise `merge` with only changed files — `toSaveRequest` picks this.

**`PluginDetail.tsx`** changes: loads `getVersionSource(pluginId, version, {all: true})`; replaces the Select/`<pre>` viewer with `SourceEditor`; header gains `Save` (dev) or `Save as new version` (test/prod, also offered on dev as a secondary action), disabled when not dirty; save success → `Alert` "Saved. N architectures need a rebuild" with a `Rebuild` button calling `startBuilds(pluginId, version, stale ∪ failed)`; `beforeunload` + in-app `useBlocker`-style confirmation when dirty (Requirement 1.11); per-arch rows show a `stale` warning badge (Requirement 1.8) and a `Fix with AI` inline button on `failed` rows that sets `assistDiagnostics = {kind:'build', architecture, text: logTail}` and focuses the tab chosen by `pickFileForDiagnostics(logTail, paths)` (pure: first Source_Tree path mentioned in the log; else `builds/{arch}/meson.build` for meson/ninja errors; else the C source for `.c`/gcc errors; else the hook file); "Add architectures" `Multiselect` from `builds.buildable_architectures − requested_architectures` → `addArchitectures` (Requirements 6.6–6.7); Build panel also restricted to `buildable_architectures`; component summary line "Deployable component v.0.n: archs".

**`GitSyncPanel.tsx`** (new, on the Detail_Page): unlinked → `Select` of the Use_Case's verified connections + branch/path inputs → `linkGit`; linked → connection name, branch, path, last sync (kind, short SHA linked to `{repo_url}/commit/{sha}` for github/gitlab hosts, by, at), `Push` (with `force` checkbox revealed only after a `diverged` failure), `Pull` (ref input, mode radio defaulting per lifecycle, `in_place` disabled for test/prod), operations table with `StatusIndicator`, category badge, plain-language explanation from the pure `describeSyncFailure(category)`, and the redacted excerpt in a `Popover` (Requirements 4.10–4.11). Push with dirty editor → `ConfirmationModal` "Save or discard first" (Requirement 3.13). Polls `getSyncOperation` every 4 s while queued/running.

**`GitConnections.tsx`** (new page `/node-designer/git-connections`, nav under Node Designer — the "Node Designer" side-navigation entry becomes an `expandable-link-group` with a "Git connections" child, the same shape as "Workflow Tuning"): table per Use_Case with name, provider badge, repo URL, default branch, verification `StatusIndicator` (a `failed` status opens a `Popover` with the `describeSyncFailure` explanation); create/edit `Modal` (token `Input type=password`, never pre-filled, blank on edit keeps the stored token; `repoUrlError` mirrors the backend https rule client-side), `Verify`, `Delete` with `ConfirmationModal`; the list polls every 4 s while any connection is `verifying`. Gated to UseCaseAdmin/PortalAdmin for mutations (Requirement 2.9).

**`CodeAssistPanel.tsx`** additions: props `diagnostics?: CodeAssistDiagnosticsState | null` (pre-seed, applied while idle) and `activeFile`; one `ExpandableSection` (footer variant) whose header reads "Attached error output (build, arm64_jp6)" when something is attached and "Error output (optional)" otherwise, holding the editable `Textarea` for pasted errors (kind `user`) and — in the section body, because Cloudscape ignores `headerActions` on the footer variant — a "Clear attached error output" link (Requirement 5.3); the submit button reads "Diagnose and fix" while diagnostics are attached; `onAccept(code, targetFile?)`; reviewing state shows an "Applies to `{target_file}`" `Badge` and an "Apply to `{target_file}`" primary action when the Target_File differs from `activeFile` (Requirement 5.7). Reducer gains `diagnostics` in every phase and `targetFile` in `reviewing`; `submit` truncates diagnostics to the last 16 KiB and marks `truncated` (Requirement 5.4); accept clears prompt and attachment, failure keeps both. `describeCodeAssistError` maps `INVALID_TARGET_FILE`. The simulator's failure `Alert` (not timeouts) gains a "Fix with AI" action that navigates to the Detail_Page with `state.assistDiagnostics = {kind: 'simulation', text}` (Requirement 5.2).

*RBAC note (implementation):* `shared_utils` resolves any authenticated JWT user without an explicit grant to Viewer/read in every Use_Case (legacy fallback), so "outsider gets 404" is not testable for the new routes; the tests assert the 403 manage-gating and the Viewer read paths instead.

**`api.ts`** additions: `getVersionSource(pluginId, version, {all?: boolean, file?: string})`, `putVersionSource(pluginId, version, {files, delete?, mode?, expected_source_revision?})`, `createNewVersion(pluginId, version, body)`, `addArchitectures(pluginId, version, architectures)`, `listGitConnections(usecaseId)`, `createGitConnection`, `updateGitConnection`, `deleteGitConnection`, `verifyGitConnection`, `setGitLink`, `removeGitLink`, `pushGit(pluginId, version, {message?, force?})`, `pullGit(pluginId, version, {ref?, mode})`, `listSyncOperations`, `getSyncOperation`. `services/api.ts` `CodeAssistRequest` gains `diagnostics` and the extended `context`; `CodeAssistResponse` gains `target_file`.

### 9. Infrastructure (`node-designer-stack.ts`, `node-designer-api-stack.ts`, `plugin-build-images/`)

- `PLUGIN_BUILD_ARCHITECTURES` gains `'arm64_jp7'` → sixth `dda-plugin-build-arm64_jp7` project (ARM image from ECR tag `arm64_jp7`), role scoped identically (`plugin-staging/*/arm64_jp7/*`, `workflow-plugins/custom/*/arm64_jp7/*`), added to the build-results EventBridge rule and `BUILD_PROJECTS_JSON` (Requirement 7.1).
- `Dockerfile.arm64_jp7`: `FROM nvcr.io/nvidia/cuda:13.0.2-devel-ubuntu24.04@sha256:5dc1bca23d05bd37b011be68ec470c03b403a5da07ec3a86e41af9470e9d0cc6` (same digest as `src/backend/Dockerfile.jp7`); apt set of `Dockerfile.arm64_jp6` (all names exist on noble/arm64; `python3-dev` is 3.12 → `python3-embed` pkg-config present); AWS CLI v2 aarch64; `COPY dda-plugin-build` (Requirement 7.2). `build-and-push.sh` `ALL_ARCHES` gains `arm64_jp7` (Requirement 7.6).
- `plugin_importer.PLATFORM_GSTREAMER_VERSIONS['arm64_jp7'] = '1.24'`; `PLATFORMS_WITH_SUBPROJECT_FALLBACK` gains `arm64_jp7`; platform label table gains it (Requirement 7.4). `plugin_components.platform_block` already emits `variant: arch` for every `aarch64` mapping (Requirement 7.5) — a unit test pins `arm64_jp7`.
- New tables `dda-portal-git-connections` (PK `connection_id`, GSI `usecase-connections-index` on `usecase_id`) and `dda-portal-git-sync-operations` (PK `operation_id`, GSI `plugin-operations-index` `plugin_id` + `started_at`, TTL `ttl` = 180 days), PITR on, RETAIN.
- `GitSyncHandler` Lambda (`git_sync.handler`, 120 s) with the base handler role plus RW on the two tables, `secretsmanager:CreateSecret/PutSecretValue/DeleteSecret/DescribeSecret/TagResource` on `secret:dda-portal/git-connections/*` (no `GetSecretValue`), `codebuild:StartBuild/BatchGetBuilds` on the git-sync project, `logs:GetLogEvents/FilterLogEvents/DescribeLogStreams` on `/aws/codebuild/dda-plugin-git-sync*`, `s3:*Object` on `plugin-sources/*` and `plugin-git-sync/*` (the base role already has bucket RW).
- EventBridge rule `dda-portal-git-sync-results` (source `aws.codebuild`, terminal statuses, `project-name: [dda-plugin-git-sync]`) → `GitSyncHandler`.
- `node-designer-api-stack.ts`: `gitSyncIntegration`; root resources `git-connections` (+`{cid}`, `{cid}/verify`) and `git-sync-operations/{opId}` added to the deployment `addDependency` list; version-level `git` (PUT/DELETE), `git/push`, `git/pull`, `git/operations`, `architectures` (POST → `buildsIntegration`), `new-version` (POST → `recordsIntegration`). The route salt changes automatically.
- `bin/app.ts`: no new stack; the node-designer stack gains the resources.

## Data Models

### Plugin_Record version item — additions

```
source_revision: int                 # default 1 when absent (10.1); +1 per save/pull/in-place file addition
requested_architectures: [arch]      # now the monotonic union of every round (6.5)
artifacts[arch].sourceRevision: int  # stamped at build start; default 1 when absent (1.9, 10.1)
git: {                               # Git_Link; absent when unlinked
  connection_id, branch, path,
  linked_by, linked_at,
  last_sync?: {kind: 'push'|'pull', commit, branch, path, ref?, source_revision?, by, at, version?}
}
active_sync_operation?: operation_id # single-flight lock (3.12), removed on settlement
component: {                         # pointer, extended
  name, version: 'v.0.n', revision: n, arn, architectures: [arch],
  artifact_checksums: {arch: sha256}, status, packagedAt, failure
}
provenance.forkedFrom?: int          # new-version source (1.6)
provenance.gitPull?: {commit, ref, by, at}   # new version created by a Pull (4.8)
provenance.scaffoldDeclaration       # JSON string; `architectures` extended by Architecture_Addition (6.3)
```

`version_detail` exposes `source_revision`, `git`, and `active_sync_operation`; `record_summary` exposes `source_revision` and `git_linked: bool`.

### `GitConnections` table (`dda-portal-git-connections`)

```
connection_id (PK, uuid), usecase_id (GSI usecase-connections-index), name,
provider: 'github' | 'gitlab', repo_url, default_branch, secret_arn,
status: 'verifying' | 'verified' | 'failed',
verification: {at, category?, message?, default_branch_detected?, operation_id},
created_by, created_at, updated_by, updated_at
```

Secret: name `dda-portal/git-connections/{usecase_id}/{connection_id}`, `SecretString` `{"token": "<pat>"}`, tags `dda-portal:managed`, `usecase-id`, `connection-id`. API responses never include `secret_arn` (Requirement 2.4).

### `GitSyncOperations` table (`dda-portal-git-sync-operations`)

```
operation_id (PK, uuid), usecase_id, connection_id,
plugin_id?, version? (GSI plugin-operations-index: plugin_id, started_at),
kind: 'verify' | 'push' | 'pull',
target: {branch, path, ref?, mode?: 'in_place' | 'new_version', force?: bool},
status: 'queued' | 'running' | 'succeeded' | 'failed',
build_id, started_by, started_at, finished_at?,
result?: {commit, files?, no_changes?, version?, default_branch?},
failure?: {category, message, changed_files?, defects?, log_excerpt},
ttl (180 days)
```

### Sync_Manifest (`dda-plugin.json`, written by Push)

```json
{"ddaPlugin": 1, "pluginId": "...", "version": 3, "sourceRevision": 4, "kind": "scaffold",
 "name": "...", "scaffoldDeclaration": {...} | null, "pushedBy": "user", "pushedAt": "2026-09-22T10:00:00Z",
 "portal": "edge-cv-portal"}
```

### Runner result (`plugin-git-sync/{operation_id}/result.json`)

```json
{"ok": true, "kind": "push", "commit": "abc123…", "files": 6, "no_changes": false}
{"ok": false, "kind": "push", "category": "diverged", "message": "…", "changed_files": ["plugins/x/plugin/gstx.c"]}
{"ok": true, "kind": "pull", "commit": "…", "tree_files": 6, "tree_bytes": 18234}
{"ok": true, "kind": "verify", "default_branch": "main"}
```

### Build platform table (shared constant, backend)

`workflow_core.catalog.models` (or a new `workflow_core.catalog.platforms`) gains `BUILD_PLATFORMS`, consumed by `plugin_importer.PLATFORM_GSTREAMER_VERSIONS` (derived), the code-assist system prompt, and the frontend labels via the existing catalog route:

| arch | os | gstreamer | notes |
|---|---|---|---|
| x86_64 | Ubuntu 22.04 | 1.20 | meson 0.61, gcc 11 |
| x86_64_nvidia | Ubuntu 22.04 + CUDA | 1.20 | meson 0.61, gcc 11, CUDA toolkit |
| arm64_jp4 | L4T r32 / Ubuntu 18.04 | 1.14 | meson 0.45 (pip meson in image), gcc 7 |
| arm64_jp5 | L4T r35 / Ubuntu 20.04 | 1.16 | meson 0.53, gcc 9 |
| arm64_jp6 | L4T r36 / Ubuntu 22.04 | 1.20 | meson 0.61, gcc 11 |
| arm64_jp7 | Ubuntu 24.04 + CUDA 13 | 1.24 | meson 1.3, gcc 13 |

### Code-assist API additions (frontend `services/api.ts`)

```typescript
export type CodeAssistContract = 'process_frame' | 'process_frame_or_handle' | 'frame_hook' | 'produce_frame' | 'plugin_source';
export interface CodeAssistDiagnostics { kind: 'build' | 'simulation' | 'user'; architecture?: string; text: string; truncated?: boolean }
export interface CodeAssistRequest {
  usecase_id: string; surface: 'workflow-builder' | 'node-designer'; contract: CodeAssistContract;
  prompt: string; current_code?: string;
  context?: { nodeType?: string; parameters?: {...}[]; active_file?: string; files?: Record<string, string>;
              file_paths?: string[]; kind?: 'scaffold' | 'generated' | 'imported' };
  diagnostics?: CodeAssistDiagnostics;
}
export interface CodeAssistResponse { code: string; notes: string; model_id: string; contract: CodeAssistContract; target_file?: string }
```

### Error codes introduced

| Code | Status | Where |
|---|---|---|
| `SOURCE_LOCKED` | 409 | PUT source / Pull in_place on non-dev |
| `SOURCE_REVISION_CONFLICT` | 409 | PUT source with stale `expected_source_revision` |
| `VERSION_CONFLICT` | 409 | new-version race |
| `LIFECYCLE_LOCKED` | 409 | Architecture_Addition on prod |
| `BUILDS_IN_PROGRESS` | 409 | Architecture_Addition while builds run |
| `BUILD_TARGET_UNAVAILABLE` | 400 | build / architectures for arch outside the registry (replaces the 500) |
| `INVALID_REPO_URL`, `INVALID_REPO_PATH`, `INVALID_PROVIDER` | 400 | Git_Connection / Git_Link |
| `CONNECTION_NOT_VERIFIED` | 409 | push/pull through an unverified connection |
| `GIT_LINK_REQUIRED` | 409 | push/pull on an unlinked version |
| `SYNC_IN_PROGRESS` | 409 | second push/pull on a version |
| `CONNECTION_NOT_FOUND`, `OPERATION_NOT_FOUND` | 404 | |
| `INVALID_DIAGNOSTICS`, `INVALID_CONTEXT` | 400 | code-assist request |
| `INVALID_TARGET_FILE` | 422 | code-assist model output |

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

The pure functions of this design — save-request reduction, path validation, resulting-tree computation, staleness, union of requested architectures, republish detection and revision numbering, sync-request validation, failure classification, redaction, file-context bounding, target-file resolution, and the panel reducer — are deterministic transformations and the property-test targets below. Python properties use `hypothesis`; TypeScript properties use `fast-check` (`numRuns: 100`); each is tagged `**Feature: custom-node-source-lifecycle, Property {n}: {text}**`.

### Property 1: Save-request reduction is exact
*For any* baseline file map, sequence of edits/additions/deletions, and source revision, `toSaveRequest` contains exactly the paths whose content differs from the baseline or were added under `files`, exactly the removed paths under `delete`, uses `mode: 'replace'` (with the full current map) if and only if at least one path was deleted, and carries the given `expected_source_revision`; applying the request to the baseline reproduces the editor state.
**Validates: Requirements 1.3, 1.4, 1.12**

### Property 2: Source path validity predicate
*For any* string, the frontend `isValidSourcePath` and the backend path confinement accept it if and only if `posixpath.normpath` of the string is non-empty, is not `.`, does not start with `/` or `..`, and contains no `..` segment.
**Validates: Requirements 1.3, 3.1**

### Property 3: Resulting-tree computation
*For any* current listing, `files` map, `delete` list, and mode, `resulting_tree(listing, files, delete, mode)` equals `(listing ∪ keys(files)) − delete` for `merge` and `keys(files) − delete` for `replace`; scaffold validation is evaluated on that set, and every path written is in the result while every deleted object is not.
**Validates: Requirements 1.4, 1.7**

### Property 4: Staleness is exactly a revision comparison
*For any* version item and per-arch artifact map, `stale_architectures(item)` is exactly the set of architectures whose entry `sourceRevision` (default 1) is strictly lower than the item's `source_revision` (default 1); items and entries without recorded revisions produce no stale architectures.
**Validates: Requirements 1.8, 1.9, 10.1**

### Property 5: Requested architectures are a monotonic union
*For any* sequence of build rounds and architecture additions applied to a version, the resulting `requested_architectures` equals the sorted union of every architecture ever requested, and `builds_settled` is true if and only if every architecture in that union has a settled entry.
**Validates: Requirements 6.4, 6.5**

### Property 6: Architecture_Addition validation
*For any* requested list, existing requested set, Build_Target_Registry, and DeepStream flag, `validate_addition` rejects exactly the architectures that are unknown, already requested, outside the registry, or non-Jetson under DeepStream, each with its reason, and accepts the request if and only if the rejection set is empty and the list is non-empty.
**Validates: Requirements 6.1, 6.8**

### Property 7: Scaffold build configurations are rendered only where missing
*For any* valid scaffold declaration, existing architecture list, added architecture list, and set of already-present paths, `plan_build_configs` yields exactly `build_config_path(arch)` for the added architectures not already present, never a path already present, and the returned declaration's `architectures` equals the ordered union.
**Validates: Requirements 6.3**

### Property 8: Republish detection and revision numbering
*For any* component pointer (absent, legacy without checksums, or full) and artifact map, `needs_republish` is true if and only if the pointer is not `registered` or the successfully built `{arch: checksum}` set differs from the recorded set (legacy pointers compared by architecture set), and the next component version is `v.0.0` for a first registration and `v.0.(n+1)` otherwise; the workflow pin `>=v.0.0 <v+1.0.0` is satisfied by every such version.
**Validates: Requirements 8.1, 8.2, 8.3, 8.6, 10.2**

### Property 9: Sync request validation
*For any* Git_Connection payload, `validate_connection` accepts if and only if the provider is `github` or `gitlab`, the URL parses with scheme `https` and a non-empty host, name and default branch are non-empty; *for any* Git_Link payload, the path is accepted if and only if Property 2 holds for it.
**Validates: Requirements 2.1, 2.2, 3.1**

### Property 10: Start-build environment never carries the token
*For any* Sync_Operation start, the `environmentVariablesOverride` list contains exactly one variable of type `SECRETS_MANAGER` (`GIT_TOKEN`, value `{secret_arn}:token`), every other variable is `PLAINTEXT`, and no plaintext value equals or contains the token string.
**Validates: Requirements 2.3, 2.4**

### Property 11: Failure classification is total and redaction is complete
*For any* runner stderr text, `classify_failure` returns exactly one Failure_Category, with the design's marker strings mapping to `authentication`, `not_found`, and `unreachable` and everything else to `internal`; *for any* log text containing an injected token (GitHub `ghp_…`/`github_pat_…`, GitLab `glpat-…`, or a `https://user:token@host` URL), `redact` removes every occurrence while leaving text without secrets unchanged.
**Validates: Requirements 4.11, 9.5**

### Property 12: Sync settlement is idempotent and releases the lock
*For any* Sync_Operation and sequence of duplicate result deliveries, the first delivery settles the operation (status, result/failure, `last_sync` for successful push/pull) and subsequent deliveries change nothing; after settlement the version's `active_sync_operation` is absent.
**Validates: Requirements 3.10, 3.12, 4.9**

### Property 13: Pull installation is all-or-nothing
*For any* staged tree and target Source_Tree, a validated `in_place` install leaves the target equal to the staged tree (every staged file present, every other prior file deleted) with `source_revision` incremented by exactly one; a `new_version` install leaves the original version's tree and revision unchanged and creates exactly one new version whose tree equals the staged tree; a failed validation changes neither.
**Validates: Requirements 4.3, 4.6, 4.7, 4.8**

### Property 14: Bounded file context
*For any* Source_Tree map and active file, `otherTextFiles(files, active, limit)` excludes the active file and binary files, never exceeds `limit` bytes in total, drops the largest files first when over the limit, and the accompanying `file_paths` lists every path of the tree exactly once.
**Validates: Requirements 5.6**

### Property 15: Target-file resolution and contract selection
*For any* request context and model output, `resolve_target(output.target_file, context)` returns the active file when `target_file` is absent, rejects any `target_file` not in `context.file_paths`, and selects the `frame_hook` contract if and only if the effective target is `plugin/frame_processing_hook.py`, otherwise `plugin_source` whose validation accepts exactly the outputs with a non-whitespace character.
**Validates: Requirements 5.7, 5.8, 5.9**

### Property 16: Diagnostics are bounded and embedded verbatim
*For any* diagnostic text, the panel submits the last 16 KiB (flagging truncation when shortened); *for any* accepted request, the assembled Bedrock messages contain the diagnostic text verbatim, the kind, and the architecture when present, and the system prompt contains the scaffold layout and build-platform markers for node-designer contracts.
**Validates: Requirements 5.4, 5.5, 5.10**

### Property 17: Editor reducer invariants
*For any* sequence of editor events (edit, add, delete, save-succeeded, save-failed, reset), `dirtyPaths` is empty if and only if the current map equals the baseline and no deletions are pending; `save-failed` leaves the map unchanged; `save-succeeded` sets the baseline to the saved map and clears deletions; a deleted path is never also in `files`.
**Validates: Requirements 1.11, 1.12**

### Property 18: Failed-arch file picking is deterministic and in-tree
*For any* log text and Source_Tree path list containing the scaffold files, `pickFileForDiagnostics` returns a path from the list; it returns the first listed path mentioned in the log when any is, else the failing architecture's meson file for meson/ninja markers, else the C source for compiler markers, else the hook file.
**Validates: Requirements 5.1**

## Error Handling

- **Save conflicts and locks** return 409 with `SOURCE_LOCKED`, `SOURCE_REVISION_CONFLICT`, or `LIFECYCLE_LOCKED`; the Source_Editor keeps the edits, shows the reason, and (for `SOURCE_LOCKED`) surfaces "Save as new version" as the primary action (Requirements 1.5, 1.12).
- **Scaffold defects** on save, new-version, or pull are reported in full (`details.defects`) and nothing is written (Requirements 1.7, 4.6).
- **Sync failures** always settle the operation with a Failure_Category and redacted excerpt; the lock is released even when the runner crashes (the Lambda treats a `FAILED` build without `result.json` as `internal` with the log tail). A StartBuild failure settles the operation synchronously (`internal`). The UI explains each category: `authentication` → "The access token was rejected; update the token on the Git connection", `not_found` → "Repository, branch, ref, or path does not exist", `unreachable` → "The Git host could not be reached", `diverged` → "The repository changed under `{path}` since the last sync; pull first or push with overwrite", `push_rejected` → "The branch moved during the push; retry", `invalid_source` → the limit or defects, `internal` → "Unexpected failure; see the excerpt".
- **Secrets Manager failures** during create/update roll back: a failed `CreateSecret` stores no connection; a failed item write after `CreateSecret` deletes the secret (`ForceDeleteWithoutRecovery`) before returning 500.
- **Republish failures** are recorded on the component pointer (`status: failed`, `failure`) exactly as today and never fail the build; a later build round retries.
- **Code-assist** errors keep the existing `describeCodeAssistError` behavior; `INVALID_TARGET_FILE` renders "The assistant named a file that is not part of this plugin"; the prompt and attached diagnostics are retained.
- **Unconfigured build targets** are a client error (`BUILD_TARGET_UNAVAILABLE`) and the UI never offers them, so the current 500 disappears.

## Testing Strategy

Baselines that must stay green: portal backend `pytest` scoped to `tests/` from `edge-cv-portal/backend` (moto-backed conftest), `npx vitest run` and `npm run build` from `edge-cv-portal/frontend`, `npm test` and `npm run build` from `edge-cv-portal/infrastructure`.

- **Backend unit/property tests** (`edge-cv-portal/backend/tests/`): `test_source_editing.py` (bulk read shapes, merge/replace/delete, lifecycle guard, revision conflict, new-version copy incl. rollback on conditional failure, staleness); `test_property_source_tree.py` (Properties 2–4); `test_add_architectures.py` + `test_property_requested_architectures.py` (Properties 5–7, meson render-only-where-missing against moto S3); `test_property_component_republish.py` (Property 8) + `test_plugin_components_republish.py` (legacy pointer, staging keys with revision, ConflictException path); `test_git_sync.py` (connection CRUD with mocked Secrets Manager — assert no `GetSecretValue` call ever happens and no response contains the token; StartBuild env assertions; guards; result handling for all three kinds; pull install both modes; idempotent duplicates) and `test_property_git_sync.py` (Properties 9–13); `test_code_assist_diagnostics.py` + `test_property_code_assist_context.py` (Properties 15–16, request validation matrix, `plugin_source` contract, `target_file` handling); jp7 additions asserted in `test_plugin_importer.py` (platform table) and `test_plugin_components.py` (variant manifest).
- **Runner shell tests** (`edge-cv-portal/plugin-build-images/git-sync/tests/`, plain bash + a local bare repository as "remote", no network): verify/push/pull happy paths, branch creation, empty repo, no-change push, Divergence_Guard (with and without `force`), push rejection retry against a moving bare remote, pull of missing ref/path, symlink and manifest exclusion, size limits, and the `result.json` contract. Run by a pytest wrapper so they join the backend baseline.
- **Frontend tests** (vitest + Testing Library + fast-check): `sourceEditorState.property.test.ts` (Properties 1, 17), `sourcePath.property.test.ts` (Property 2), `fileContext.property.test.ts` (Property 14), `pickFileForDiagnostics.property.test.ts` (Property 18), `SourceEditor.test.tsx`, `PluginDetail.test.tsx` extensions (save/lock/new-version flows, stale badges, Fix with AI seeding, Add architectures picker limited to `buildable_architectures`, role gating), `GitSyncPanel.test.tsx`, `GitConnections.test.tsx`, `CodeAssistPanel.test.tsx` extensions (error-output field, seeded diagnostics, target-file display, truncation).
- **Infrastructure tests** (jest): extend `node-designer-stack.test.ts` expected project list with `dda-plugin-build-arm64_jp7` and `dda-plugin-git-sync`, per-arch IAM assertions for jp7, new assertions for the git-sync role (`GetSecretValue` only on the `dda-portal/git-connections/*` ARN pattern, S3 limited to `plugin-sources/*` read and `plugin-git-sync/*` read/write, no VpcConfig), the Lambda role having no `secretsmanager:GetSecretValue`, the new EventBridge rule, the two new tables, and the new API routes; update the snapshot.
- **Manual/integration** (documented, not automated): build and push `dda-plugin-build:arm64_jp7`, run a scaffold build on the ARM fleet, connect a real GitHub and a real GitLab repository with a PAT, push/pull round trip, and deploy a republished `v.0.1` component to a Test_Device.
  - Build the `arm64_jp7` image through the DDA build system (see `.kiro/steering/builds.md`) rather than a local `docker build`: run it on the dedicated build server that is already up, so the layer cache is reused. `plugin-build-images/build-and-push.sh` already lists `arm64_jp7` in `ALL_ARCHES`; no image build is needed for any of this feature's automated tests.
