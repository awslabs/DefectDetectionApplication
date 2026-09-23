# Implementation Plan: Custom Node Source Lifecycle

## Overview

Implementation follows the dependency order the design lays out. Backend foundations come first because every surface depends on them: the Source_Revision/stale model and the extended source routes in `plugin_records.py`, the union semantics and architectures route in `plugin_builds.py`, and the artifact-set republish in `plugin_components.py`. The Git_Sync_Service is an independent backend track (new Lambda, tables, CodeBuild runner) that only shares the version item's `git` block with the source routes. The code-assist extension is a third independent backend track. Infrastructure (jp7 project and image, git-sync project, tables, Lambda, routes) lands once the handler modules exist, then the frontend wires the Detail_Page, the Source_Editor, the Git panels, and the CodeAssistPanel changes, and a final checkpoint runs every baseline.

Baselines that must stay green throughout: portal backend `pytest` scoped to `tests/` from `edge-cv-portal/backend` (moto-backed conftest stack), `npx vitest run` plus `npm run build` from `edge-cv-portal/frontend`, and `npm test` plus `npm run build` from `edge-cv-portal/infrastructure`. Python property tests use `hypothesis` (no hardcoded `max_examples`; the project default provides ≥100 iterations) as `test_property_*.py`; TypeScript property tests use `fast-check` with `numRuns: 100`. Each property test is tagged `**Feature: custom-node-source-lifecycle, Property {number}: {property_text}**`.

## Task Dependency Graph

```mermaid
graph TD
    T1[1. plugin_records: revision, bulk read, replace/delete, lock, new-version] --> T4[4. Checkpoint - records/builds/components]
    T2[2. plugin_builds: sourceRevision, union, architectures route, registry] --> T4
    T3[3. plugin_components: republish detection, v.0.n] --> T4
    T2 --> T3
    T5[5. git_sync.py Lambda module] --> T7[7. Checkpoint - git sync + code assist]
    T6[6. git-sync runner.sh + shell tests] --> T7
    T8[8. code_assist: diagnostics, context.files, plugin_source, target_file] --> T7
    T1 --> T5
    T4 --> T9[9. Infrastructure: jp7 project + image, git-sync project, tables, Lambda, routes]
    T7 --> T9
    T9 --> T10[10. Frontend: api.ts + types + pure modules]
    T10 --> T11[11. Frontend: SourceEditor + PluginDetail integration]
    T10 --> T12[12. Frontend: Git connections page + GitSyncPanel]
    T10 --> T13[13. Frontend: CodeAssistPanel diagnostics + Fix with AI]
    T11 --> T14[14. Final checkpoint]
    T12 --> T14
    T13 --> T14
```

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2", "6", "8"], "description": "Independent foundations: the source-revision model and extended source routes (records), the union/architectures/registry changes (builds), the git runner script with its offline shell tests, and the code-assist request/response extension" },
    { "wave": 2, "tasks": ["3", "5"], "description": "Consumers of wave 1: artifact-set republish in plugin_components (needs the builds semantics) and the git_sync Lambda module (needs the version item's git block and new-version copy helper)" },
    { "wave": 3, "tasks": ["4", "7"], "description": "Backend checkpoints: records/builds/components suite, then git sync + code assist suites" },
    { "wave": 4, "tasks": ["9"], "description": "Infrastructure: jp7 build project and image, git-sync CodeBuild project and role, tables, GitSyncHandler, EventBridge rule, API routes, CDK tests and snapshot" },
    { "wave": 5, "tasks": ["10"], "description": "Frontend foundations: API client, types, and pure modules with their property tests" },
    { "wave": 6, "tasks": ["11", "12", "13"], "description": "Frontend surfaces: SourceEditor on the Detail_Page, Git connections page and sync panel, diagnostics-aware CodeAssistPanel and Fix with AI" },
    { "wave": 7, "tasks": ["14"], "description": "Final checkpoint: all baselines pass" }
  ]
}
```

## Tasks

- [x] 1. Extend `plugin_records.py` with the Source_Revision model and the Source_Editor routes
  - [x] 1.1 Add `source_revision` and `git` to the version model and views
    - `new_version_item` sets `source_revision: 1`; `version_detail` exposes `source_revision` (default 1), `git`, and `active_sync_operation`; `record_summary` exposes `source_revision` and `git_linked`; pure `stale_architectures(item)` (entry `sourceRevision` default 1 `<` item `source_revision` default 1) shared with `plugin_builds` via `plugin_records` import
    - _Requirements: 1.8, 1.9, 10.1_

  - [x] 1.2 Implement the bulk source read
    - `GET /plugins/{id}/versions/{v}/source?all=true`: list every object, inline UTF-8 content for files ≤ 512 KiB (`binary: true` otherwise), 4 MiB total inline cap with `truncated`, `source_revision` in the response; existing list and `?file=` behaviors byte-identical
    - _Requirements: 1.1, 1.2_

  - [x] 1.3 Extend `put_version_source` with `delete`, `mode`, the lifecycle guard, and revision handling
    - Body `{files, delete?, mode?: merge|replace, expected_source_revision?}`; path confinement over `files` and `delete` (400 `INVALID_FILE_PATH`); `lifecycle_state != dev` → 409 `SOURCE_LOCKED`; mismatched `expected_source_revision` → 409 `SOURCE_REVISION_CONFLICT`; pure `resulting_tree(listing, files, delete, mode)` feeding `scaffold_defects` (hook content read from the submitted map or S3 as needed) → 422 `SCAFFOLD_INVALID` before any write; put/delete objects then `source_revision = if_not_exists(...)+1`; response adds `deleted`, `source_revision`, `stale_architectures`; audit gains `deleted` and `source_revision`; `mode` defaults to `merge` so the wizards are unchanged
    - _Requirements: 1.3, 1.4, 1.5, 1.7, 1.8, 10.3, 10.4_

  - [x] 1.4 Implement `POST /plugins/{id}/versions/{v}/new-version`
    - Server-side `copy_object` of version `v`'s tree to `latest+1` minus `delete`/overridden paths, then `files`; scaffold validation on the resulting map before any write; item via `new_version_item` plus copied `requested_architectures`, `git` (without `last_sync`), `deepstream`, `source_revision: 1`, `provenance ∪ {forkedFrom: v}`; `attribute_not_exists(version)` with copied-object cleanup and 409 `VERSION_CONFLICT` on failure; audit `create_plugin_record_version` with `forked_from`; 201 `{plugin, source_revision}`; handler routing added
    - _Requirements: 1.6, 1.7_

  - [x]* 1.5 Write property test for source path validity
    - **Feature: custom-node-source-lifecycle, Property 2: Source path validity predicate**
    - **Validates: Requirements 1.3, 3.1**
    - hypothesis over arbitrary strings (unicode, `..` segments, leading `/`, `.`, empty, long): the backend confinement accepts iff `posixpath.normpath` is non-empty, not `.`, does not start with `/` or `..`, and has no `..` segment

  - [x]* 1.6 Write property test for resulting-tree computation
    - **Feature: custom-node-source-lifecycle, Property 3: Resulting-tree computation**
    - **Validates: Requirements 1.4, 1.7**
    - hypothesis over listings, file maps, delete lists, and modes: `resulting_tree` equals `(listing ∪ keys) − delete` for merge and `keys − delete` for replace; the moto-backed save writes exactly the result's new/changed keys and deletes exactly the removed ones

  - [x]* 1.7 Write property test for staleness
    - **Feature: custom-node-source-lifecycle, Property 4: Staleness is exactly a revision comparison**
    - **Validates: Requirements 1.8, 1.9, 10.1**
    - hypothesis over items with/without `source_revision` and artifact maps with/without `sourceRevision`: `stale_architectures` is exactly the set with entry revision `<` item revision; legacy items yield the empty set

  - [x]* 1.8 Write unit tests for the source routes
    - `test_source_editing.py`: bulk read shapes incl. binary/oversize/truncation; merge vs replace vs delete against moto S3; `SOURCE_LOCKED` on test/prod; `SOURCE_REVISION_CONFLICT`; scaffold defects write nothing; new-version copies the tree, applies edits/deletes, copies `git`/`requested_architectures`, records `forkedFrom`, and cleans copied objects on a conditional failure; wizard-style merge save on a fresh dev v1 unchanged; RBAC (manage required; read-only roles get 403 with audit)
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6, 1.7, 9.1, 9.3, 10.3_

- [x] 2. Extend `plugin_builds.py` with revision stamping, union semantics, the registry, and the architectures route
  - [x] 2.1 Stamp `sourceRevision`, make `requested_architectures` a union, and expose the registry
    - `submit_arch_builds` and the prebuilt path stamp `sourceRevision`; `handle_build_result` preserves it on settlement; `start_builds` writes `sorted(set(requested) | set(all_archs))`; unconfigured arches → 400 `BUILD_TARGET_UNAVAILABLE {architectures, buildable}` replacing the 500; `builds_view` adds per-arch `stale`/`sourceRevision`, top-level `source_revision`, `buildable_architectures`, and the component pointer summary `{version, revision, architectures, status}`
    - _Requirements: 1.9, 6.5, 6.7, 6.8_

  - [x] 2.2 Implement `POST /plugins/{id}/versions/{v}/architectures`
    - Pure `validate_addition(requested, existing, registry, deepstream)` with per-arch reasons; `prod` → 409 `LIFECYCLE_LOCKED`; building/queued entries → 409 `BUILDS_IN_PROGRESS`; scaffold kinds: pure `plan_build_configs(declaration, existing, added, present_paths)` → render via `render_scaffold` and `put_object` only where `head_object` is 404, rewrite `provenance.scaffoldDeclaration` with the extended list, bump `source_revision` only when a file was written; `submit_arch_builds(item, added)`; one `update_item` merging artifacts, union `requested_architectures`, `REMOVE components_triggered`; audit `add_plugin_architectures`; 202 `builds_view`; handler routing
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 10.4_

  - [x]* 2.3 Write property test for the monotonic union
    - **Feature: custom-node-source-lifecycle, Property 5: Requested architectures are a monotonic union**
    - **Validates: Requirements 6.4, 6.5**
    - hypothesis over sequences of build rounds and additions against a moto-backed record: `requested_architectures` equals the sorted union of everything requested; `builds_settled` iff every union member is settled

  - [x]* 2.4 Write property test for addition validation
    - **Feature: custom-node-source-lifecycle, Property 6: Architecture_Addition validation**
    - **Validates: Requirements 6.1, 6.8**
    - hypothesis over requested lists, existing sets, registries, and the DeepStream flag: rejections are exactly unknown/already_requested/unavailable/deepstream_restricted with reasons; acceptance iff no rejection and non-empty

  - [x]* 2.5 Write property test for build-config planning
    - **Feature: custom-node-source-lifecycle, Property 7: Scaffold build configurations are rendered only where missing**
    - **Validates: Requirements 6.3**
    - hypothesis over valid declarations, existing/added arch lists, and present-path sets: planned paths are exactly `build_config_path(added)` minus present; the returned declaration's architectures is the ordered union

  - [x]* 2.6 Write unit tests for builds changes
    - `test_add_architectures.py`: happy path adds only new arches and preserves existing artifacts; meson rendered only when missing (moto S3); declaration rewritten; `source_revision` bumped only on file writes; prod → `LIFECYCLE_LOCKED`; in-progress → `BUILDS_IN_PROGRESS`; `builds_view` fields; retry of one failed arch keeps the other arches requested (regression for the replace behavior); `BUILD_TARGET_UNAVAILABLE` 400
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.7, 6.8_

- [x] 3. Replace the registered short-circuit in `plugin_components.py` with artifact-set republish
  - [x] 3.1 Implement change detection and Component_Revision
    - `component_version_for(plugin_version, revision=0)`; pure `artifact_checksums(item)` and `needs_republish(item)` (legacy pointers compared by architecture set); revision increment on republish; staging/promotion keys gain the `{revision}` segment; `set_component_pointer` records `revision` and `artifact_checksums`; `ConflictException` re-describe path keeps the pointer consistent; `platform_block` unchanged (already emits `variant` for every aarch64 arch)
    - _Requirements: 7.5, 8.1, 8.2, 8.3, 8.4, 10.2_

  - [x]* 3.2 Write property test for republish detection
    - **Feature: custom-node-source-lifecycle, Property 8: Republish detection and revision numbering**
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.6, 10.2**
    - hypothesis over pointers (absent, legacy, full) and artifact maps: `needs_republish` iff not registered or checksum-set differs (legacy: arch-set); next version is `v.0.0` first then `v.0.(n+1)`; every produced version satisfies `workflow_packaging.plugin_version_requirement(v)`

  - [x]* 3.3 Write unit tests for republish and downstream compatibility
    - `test_plugin_components_republish.py`: unchanged artifacts short-circuit; rebuilt checksum publishes `v.0.1` with all built arches and leaves `v.0.0` artifacts intact; added arch publishes with the union of manifests incl. `arm64_jp7` carrying `variant: arm64_jp7`; `deployments.parse_plugin_component_ref` reads the major of `v.0.n`; `deployments.plugin_component_architectures` follows the updated pointer; `workflow_packaging.plugin_version_requirement` range pins
    - _Requirements: 7.5, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

- [x] 4. Checkpoint - records, builds, and components
  - Run the portal backend suite (pytest scoped to `tests/` from `edge-cv-portal/backend`); ensure all tests pass, including the pre-existing node-designer suites against the union semantics and the new `builds_view` fields; ask the user if questions arise.

- [x] 5. Implement the Git_Sync_Service Lambda module `git_sync.py`
  - [x] 5.1 Implement Git_Connection CRUD and verification
    - New `edge-cv-portal/backend/functions/git_sync.py` following `plugin_records.py` conventions; routes `GET/POST /git-connections`, `GET/PUT/DELETE /git-connections/{cid}`, `POST /git-connections/{cid}/verify`; pure `validate_connection` (provider, https URL with host, name, default branch); `CreateSecret` under `dda-portal/git-connections/{usecase}/{cid}` with tags, item with `status: verifying`, verify Sync_Operation started, rollback (`ForceDeleteWithoutRecovery`) when the item write fails; `PutSecretValue` on token update and re-verify when url/token change; `DeleteSecret` (default recovery window) + item delete; responses never include `secret_arn` or tokens; RBAC read/manage; audit for create/update/verify/delete
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.7, 2.8, 2.9, 2.10_

  - [x] 5.2 Implement Git_Link management and Push/Pull starts
    - `PUT/DELETE /plugins/{id}/versions/{v}/git` (default branch from the connection, default path `sanitize_plugin_name`, 400 `INVALID_REPO_PATH`); `POST .../git/push {message?, force?}` and `POST .../git/pull {ref?, mode}` with guards `CONNECTION_NOT_VERIFIED`, `GIT_LINK_REQUIRED`, `SOURCE_LOCKED` (in_place on non-dev), single-flight `active_sync_operation` conditional lock → `SYNC_IN_PROGRESS`; `start_sync_operation` writes the operation item and StartBuilds `dda-plugin-git-sync` with the design's environment override table (`GIT_TOKEN` as `SECRETS_MANAGER` `{secret_arn}:token`, everything else `PLAINTEXT`, Sync_Manifest and commit message built server-side); StartBuild failure settles the op as `internal` and releases the lock; `GET .../git/operations` (newest first) and `GET /git-sync-operations/{opId}`; audit on start
    - _Requirements: 3.1, 3.2, 3.10, 3.11, 3.12, 4.1, 4.2, 4.10, 9.1, 9.4_

  - [x] 5.3 Implement result handling and Pull installation
    - EventBridge branch (`source == aws.codebuild`, `project-name == GIT_SYNC_PROJECT_NAME`): idempotent on `build_id`; read `result.json` (fallback to the CloudWatch log tail as `internal`); `verify` → connection status/verification; `push` → operation result + version `git.last_sync`; `pull` → `install_pulled_tree`: scaffold validation of the staged tree (`invalid_source {defects}` on failure), `in_place` copy-then-delete with `source_revision += 1`, `new_version` via the shared new-version helper from task 1.4 with `provenance.gitPull`, `git.last_sync` on the affected version, staging cleanup; always settle status/`finished_at`, redact excerpts (`redact`), release the lock conditionally, audit `git_sync_operation_settled` as the initiating user; pure `classify_failure(stderr)` mirror used when the runner result lacks a category
    - _Requirements: 2.5, 3.6, 3.7, 3.9, 3.10, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.11, 9.4, 9.5_

  - [x]* 5.4 Write property test for sync request validation
    - **Feature: custom-node-source-lifecycle, Property 9: Sync request validation**
    - **Validates: Requirements 2.1, 2.2, 3.1**
    - hypothesis over connection payloads (providers, schemes, hosts, blanks) and link paths: acceptance iff provider ∈ {github, gitlab}, https URL with host, non-empty name/branch; path acceptance matches Property 2

  - [x]* 5.5 Write property test for the start-build environment
    - **Feature: custom-node-source-lifecycle, Property 10: Start-build environment never carries the token**
    - **Validates: Requirements 2.3, 2.4**
    - hypothesis over connections, links, and random token strings with a recording CodeBuild stub: exactly one `SECRETS_MANAGER` variable (`GIT_TOKEN` = `{secret_arn}:token`), all others `PLAINTEXT`, no plaintext value contains the token; the Secrets Manager stub records no `GetSecretValue`

  - [x]* 5.6 Write property test for classification and redaction
    - **Feature: custom-node-source-lifecycle, Property 11: Failure classification is total and redaction is complete**
    - **Validates: Requirements 4.11, 9.5**
    - hypothesis over stderr texts seeded with the marker strings and over logs with injected `ghp_`/`github_pat_`/`glpat-` tokens and `https://user:token@host` URLs: exactly one category; `redact` removes every injected secret and is the identity on secret-free text

  - [x]* 5.7 Write property test for settlement idempotency
    - **Feature: custom-node-source-lifecycle, Property 12: Sync settlement is idempotent and releases the lock**
    - **Validates: Requirements 3.10, 3.12, 4.9**
    - hypothesis over operations and duplicated result deliveries (moto DynamoDB/S3): first delivery settles and writes `last_sync` for successful push/pull; later deliveries are no-ops; `active_sync_operation` absent afterwards

  - [x]* 5.8 Write property test for pull installation
    - **Feature: custom-node-source-lifecycle, Property 13: Pull installation is all-or-nothing**
    - **Validates: Requirements 4.3, 4.6, 4.7, 4.8**
    - hypothesis over staged trees and existing target trees (moto S3): `in_place` leaves target == staged with revision +1; `new_version` leaves the original untouched and creates one version equal to staged; validation failure changes nothing

  - [x]* 5.9 Write unit tests for the git sync module
    - `test_git_sync.py`: connection CRUD with mocked Secrets Manager (no `GetSecretValue` ever; rollback on item-write failure; token never in any response/audit/log record); guards (`CONNECTION_NOT_VERIFIED`, `GIT_LINK_REQUIRED`, `SOURCE_LOCKED`, `SYNC_IN_PROGRESS`); StartBuild failure settles `internal`; result handling for verify/push (incl. `no_changes`, `diverged` with `changed_files`, `push_rejected`)/pull (incl. `not_found`, `invalid_source` limits and defects); missing `result.json` → log-tail `internal`; RBAC matrix; disconnected link rendering after connection delete
    - _Requirements: 2.3, 2.4, 2.6, 2.8, 3.6, 3.7, 3.8, 3.9, 3.12, 4.2, 4.4, 4.5, 4.6, 9.1, 9.3, 10.5_

- [x] 6. Implement the git-sync runner and its offline shell tests
  - [x] 6.1 Write `edge-cv-portal/plugin-build-images/git-sync/runner.sh`
    - `bash -eu -o pipefail`; EXIT trap guaranteeing `/tmp/result.json` (`internal` when nothing else was written); `GIT_ASKPASS` helper echoing `$GIT_TOKEN`, `GIT_TERMINAL_PROMPT=0`, `credential.username=$GIT_USERNAME` via `-c`, token never in the URL; `classify()` from stderr; `verify` (`ls-remote --symref HEAD` → `default_branch`); `push` (s3 sync source, clone branch / create from default / init empty repo, Divergence_Guard with unreachable-commit-as-diverged unless `FORCE=1`, wholesale `REPO_PATH` replacement, `dda-plugin.json`, no-change detection, commit, push with one fetch-rebase-retry and `rebase --abort` on failure → `push_rejected`); `pull` (init + shallow fetch of `REF` with full-fetch fallback, path existence, symlink/manifest/.git exclusion, 2,000 files / 50 MiB limits, `s3 sync --delete` to `STAGING_PREFIX`); controlled outcomes exit 0 with categories in `result.json`
    - _Requirements: 2.4, 2.5, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 4.3, 4.4, 4.5, 9.5_

  - [x]* 6.2 Write offline shell tests for the runner
    - `edge-cv-portal/plugin-build-images/git-sync/tests/` driven by a pytest wrapper in `edge-cv-portal/backend/tests/test_git_sync_runner.py` (skipped when `git` is unavailable): local bare repository as the remote, a stub `aws` on PATH mapping `s3 sync/cp` to directories; cases: verify ok/auth-failure classification (stub askpass returning a wrong token against a credential-checked local HTTP server is out of scope — classification is unit-tested from canned stderr), push happy path, branch creation, empty repository, no-change, Divergence_Guard hit and `FORCE`, moving-remote retry then `push_rejected`, pull happy path, missing ref/path, symlink/manifest exclusion, limits, `result.json` schema for every case
    - _Requirements: 3.3, 3.5, 3.6, 3.7, 3.8, 3.9, 4.3, 4.4, 4.5_

- [x] 7. Checkpoint - git sync and code assist backend
  - Run the portal backend suite (pytest scoped to `tests/` from `edge-cv-portal/backend`) including the runner wrapper; ensure all tests pass; ask the user if questions arise.

- [x] 8. Extend `code_assist.py` with diagnostics, multi-file context, the `plugin_source` contract, and `target_file`
  - [x] 8.1 Extend validation, contracts, and prompt assembly
    - `validate_request`: optional `diagnostics {kind ∈ build|simulation|user, architecture? ∈ DEVICE_ARCHITECTURES, text ≤ 16 KiB}` → 400 `INVALID_DIAGNOSTICS`; `context.files` (≤ 256 KiB total, ≤ 64 entries), `context.file_paths`, `context.active_file ∈ file_paths`, `context.kind` → 400 `INVALID_CONTEXT`; `CONTRACTS['plugin_source']` with `PLUGIN_SOURCE_ENVIRONMENT`; shared `BUILD_PLATFORMS` constant (new `workflow_core.catalog.platforms`, consumed by `plugin_importer.PLATFORM_GSTREAMER_VERSIONS`) rendered into the node-designer system prompt with `SCAFFOLD_LAYOUT` and the `DIAGNOSTIC MODE` block; `build_user_message` appends `ACTIVE FILE`, fenced `OTHER FILES`, `FILE PATHS` (omitted contents noted), and fenced `DIAGNOSTIC OUTPUT`; workflow-builder contracts accept `diagnostics` (kind `user`) and ignore `context.files`
    - _Requirements: 5.3, 5.4, 5.5, 5.6, 5.10, 7.4_

  - [x] 8.2 Extend the tool schema, response validation, and response shape
    - `provide_code` gains optional `target_file`; pure `resolve_target(target_file, context)` (absent → active file; not in `file_paths` → 422 `INVALID_TARGET_FILE`); effective contract `frame_hook` iff the target is `workflow_core.scaffold.HOOK_FILE`, else `plugin_source` (non-empty check, 422 `NO_CODE_RETURNED`); response `{code, notes, model_id, contract, target_file}`
    - _Requirements: 5.7, 5.8, 5.9_

  - [x]* 8.3 Write property test for target-file resolution
    - **Feature: custom-node-source-lifecycle, Property 15: Target-file resolution and contract selection**
    - **Validates: Requirements 5.7, 5.8, 5.9**
    - hypothesis over contexts (path lists incl. the hook file) and model outputs (target absent/in-tree/out-of-tree, code empty/whitespace/non-empty/parsing/non-parsing): active-file default, out-of-tree rejection, `frame_hook` iff hook target, `plugin_source` accepts exactly non-whitespace outputs

  - [x]* 8.4 Write property test for diagnostics embedding
    - **Feature: custom-node-source-lifecycle, Property 16: Diagnostics are bounded and embedded verbatim**
    - **Validates: Requirements 5.4, 5.5, 5.10**
    - hypothesis over prompts, contracts, contexts, and diagnostic texts (≤ 16 KiB): assembled messages contain the text verbatim, the kind, and the architecture when present; node-designer system prompts contain the scaffold-layout and build-platform markers (incl. `arm64_jp7`, `1.24`); oversized text → 400

  - [x]* 8.5 Write unit tests for the code-assist extension
    - `test_code_assist_diagnostics.py`: request 400 matrix for diagnostics/context; `plugin_source` happy path via mocked Converse; `target_file` redirect to `builds/x86_64/meson.build`; `INVALID_TARGET_FILE`; hook-target still entry-point validated; workflow-builder `user` diagnostics accepted; existing `test_code_assist*` suites unchanged
    - _Requirements: 5.3, 5.4, 5.5, 5.7, 5.8, 5.9, 5.12_

- [x] 9. Infrastructure: jp7 build target, git-sync project, tables, Lambda, routes
  - [x] 9.1 Add the `arm64_jp7` plugin build target
    - `node-designer-stack.ts`: `PLUGIN_BUILD_ARCHITECTURES` gains `'arm64_jp7'` (sixth project `dda-plugin-build-arm64_jp7`, ARM image from ECR tag `arm64_jp7`, role scoped to `plugin-staging/*/arm64_jp7/*` and `workflow-plugins/custom/*/arm64_jp7/*`, added to the build-results rule and `BUILD_PROJECTS_JSON`); new `edge-cv-portal/plugin-build-images/Dockerfile.arm64_jp7` FROM the digest-pinned `nvcr.io/nvidia/cuda:13.0.2-devel-ubuntu24.04@sha256:5dc1bca23d05bd37b011be68ec470c03b403a5da07ec3a86e41af9470e9d0cc6` base of `src/backend/Dockerfile.jp7`, the `Dockerfile.arm64_jp6` apt set (noble names), AWS CLI v2 aarch64, `COPY dda-plugin-build`; `build-and-push.sh` `ALL_ARCHES` gains `arm64_jp7`; `plugin_importer` platform label table gains `arm64_jp7` and `PLATFORMS_WITH_SUBPROJECT_FALLBACK` gains it (GStreamer version now derived from `BUILD_PLATFORMS`)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.6_

  - [x] 9.2 Add the git-sync CodeBuild project, runner asset, and role
    - `GitSyncRole`: prefix-conditioned `s3:ListBucket`, `s3:GetObject` on `plugin-sources/*` and `plugin-git-sync/*`, `s3:PutObject/DeleteObject` on `plugin-git-sync/*` only, `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:{region}:{account}:secret:dda-portal/git-connections/*`, logs on `/aws/codebuild/dda-plugin-git-sync`; project `dda-plugin-git-sync` (STANDARD_7_0, SMALL, 15 min, no VPC) with `codebuild.Source.s3` pointing at the runner asset uploaded by a `BucketDeployment` under `plugin-git-sync/runner/`, inline buildspec calling `runner.sh` and uploading `/tmp/result.json` in `post_build`
    - _Requirements: 2.4, 9.5_

  - [x] 9.3 Add tables, the GitSyncHandler Lambda, the EventBridge rule, and routes
    - Tables `dda-portal-git-connections` (PK `connection_id`, GSI `usecase-connections-index`) and `dda-portal-git-sync-operations` (PK `operation_id`, GSI `plugin-operations-index`, TTL `ttl`), PITR, RETAIN; `GitSyncHandler` (`git_sync.handler`, 120 s) with the base role plus table RW, `secretsmanager:CreateSecret/PutSecretValue/DeleteSecret/DescribeSecret/TagResource` on the git-connections ARN pattern (explicitly no `GetSecretValue`), `codebuild:StartBuild/BatchGetBuilds` on the git-sync project, logs read on its group, env `GIT_CONNECTIONS_TABLE`, `GIT_SYNC_OPERATIONS_TABLE`, `GIT_SYNC_PROJECT_NAME`, `GIT_SECRET_PREFIX`, `PLUGIN_GIT_SYNC_PREFIX`; rule `dda-portal-git-sync-results` → the handler; `node-designer-api-stack.ts` gains `gitSyncIntegration`, root resources `git-connections` (+`{cid}`, `{cid}/verify`) and `git-sync-operations/{opId}` (added to the deployment `addDependency` list), and version-level `git` (PUT/DELETE), `git/push`, `git/pull`, `git/operations`, `architectures` (POST → builds), `new-version` (POST → records); stack outputs gain the git-sync project name
    - _Requirements: 2.3, 2.4, 3.2, 4.1, 6.4, 1.6_

  - [x]* 9.4 Write infrastructure tests
    - Extend `test/node-designer-stack.test.ts`: expected project names now six builds + fetch + git-sync; jp7 in `test.each` per-arch IAM assertions and the EventBridge project list; `no VpcConfig` still holds; git-sync role assertions (`GetSecretValue` only on the `dda-portal/git-connections/*` pattern, S3 read limited to `plugin-sources/*` + `plugin-git-sync/*`, write limited to `plugin-git-sync/*`, no `workflow-plugins/` or `plugin-staging/` path); GitSyncHandler role has no `secretsmanager:GetSecretValue`; `dda-portal-git-sync-results` rule shape; two new tables with keys/GSI/TTL; new API methods present via the salted deployment; `jest -u` snapshot update
    - _Requirements: 2.4, 7.1, 9.5_

- [x] 10. Frontend foundations: API client, types, and pure modules
  - [x] 10.1 Extend `pages/node-designer/api.ts` and `types.ts`, and `services/api.ts`
    - `getVersionSource(pluginId, version, {all?, file?})`, `putVersionSource(pluginId, version, {files, delete?, mode?, expected_source_revision?})`, `createNewVersion`, `addArchitectures`, `listGitConnections`, `createGitConnection`, `updateGitConnection`, `deleteGitConnection`, `verifyGitConnection`, `setGitLink`, `removeGitLink`, `pushGit`, `pullGit`, `listSyncOperations`, `getSyncOperation`; types `SourceFileEntry`, `SourceTreeResponse`, `SaveSourceRequest/Response`, `GitConnection`, `GitLink`, `SyncOperation`, `SyncFailureCategory`, `PluginBuildsView` gains `source_revision`, `buildable_architectures`, per-arch `stale`/`sourceRevision`, `component`; `PluginVersionDetail` gains `source_revision`, `git`, `active_sync_operation`; `services/api.ts` `CodeAssistContract` gains `plugin_source`, `CodeAssistRequest` gains `diagnostics` and the extended `context`, `CodeAssistResponse` gains `target_file`
    - _Requirements: 1.1, 1.6, 2.9, 3.2, 4.1, 4.10, 5.6, 6.6, 6.7_

  - [x] 10.2 Implement the pure modules
    - `sourceEditorState.ts` (`applyEdit`, `addFile`, `deleteFile`, `dirtyPaths`, `toSaveRequest`, `resetToBaseline`, save-succeeded/failed transitions), `sourcePath.ts` (`isValidSourcePath`), `fileContext.ts` (`otherTextFiles(files, active, limit)` + `allPaths`), `diagnosticsFile.ts` (`pickFileForDiagnostics(log, paths, arch)`), `syncFailures.ts` (`describeSyncFailure(category, details)` plain-language table), `codeAssistState.ts` extended with `diagnosticsText`/`truncated` in idle/submitting and `targetFile` in reviewing (16 KiB tail truncation on submit); `canManageNodeDesigner(role)` helper in `utils/nodeDesignerAccess.ts` replacing the inline role checks in CreateWizard/GeneratePanel
    - _Requirements: 1.3, 1.11, 1.12, 4.11, 5.1, 5.4, 5.6, 9.2_

  - [x]* 10.3 Write property test for save-request reduction
    - **Feature: custom-node-source-lifecycle, Property 1: Save-request reduction is exact**
    - **Validates: Requirements 1.3, 1.4, 1.12**
    - fast-check over baselines and edit/add/delete sequences: `files` = changed-or-added, `delete` = removed, `replace` with the full map iff a deletion exists, `expected_source_revision` carried; applying the request to the baseline reproduces the editor map

  - [x]* 10.4 Write property test for the path predicate (frontend twin)
    - **Feature: custom-node-source-lifecycle, Property 2: Source path validity predicate**
    - **Validates: Requirements 1.3, 3.1**
    - fast-check over strings: `isValidSourcePath` matches the normalized-path rule; corpus shared with the backend test via a fixed JSON fixture of accepted/rejected samples

  - [x]* 10.5 Write property test for bounded file context
    - **Feature: custom-node-source-lifecycle, Property 14: Bounded file context**
    - **Validates: Requirements 5.6**
    - fast-check over file maps (incl. binary markers) and limits: active and binary files excluded, total ≤ limit, largest dropped first, `file_paths` lists every path once

  - [x]* 10.6 Write property test for the editor reducer
    - **Feature: custom-node-source-lifecycle, Property 17: Editor reducer invariants**
    - **Validates: Requirements 1.11, 1.12**
    - fast-check over event sequences: `dirtyPaths` empty iff map == baseline and no deletions; save-failed leaves the map; save-succeeded rebaselines and clears deletions; a deleted path is never in `files`

  - [x]* 10.7 Write property test for diagnostics file picking
    - **Feature: custom-node-source-lifecycle, Property 18: Failed-arch file picking is deterministic and in-tree**
    - **Validates: Requirements 5.1**
    - fast-check over logs (with/without path mentions, meson/ninja/gcc markers) and scaffold path lists: result is in the list; precedence mentioned-path > meson-for-arch > C source > hook

- [x] 11. Frontend: SourceEditor and Detail_Page integration
  - [x] 11.1 Implement `SourceEditor.tsx`
    - Cloudscape `Tabs` with dirty markers, `Textarea rows=24 spellcheck=false`, Add file `Modal` (validated by `isValidSourcePath`), per-tab Delete with `ConfirmationModal`, read-only binary/oversize tabs with size text, `CodeAssistPanel` under every editable tab for UseCaseAdmin/PortalAdmin with `contract = path === HOOK_FILE ? 'frame_hook' : 'plugin_source'`, `context = {parameters, active_file, files: otherTextFiles(...), file_paths, kind}`, `diagnostics` pass-through, `onAccept(code, targetFile)` writing into `files[targetFile ?? path]`
    - _Requirements: 1.1, 1.2, 1.3, 1.13, 5.6, 5.11, 9.2_

  - [x] 11.2 Integrate into `PluginDetail.tsx`
    - Load `getVersionSource(..., {all: true})`; replace the Select/`<pre>` viewer; header `Save` (dev, disabled unless dirty) and `Save as new version` (always; primary on test/prod); `SOURCE_LOCKED` → guidance toward new version; `SOURCE_REVISION_CONFLICT` → reload prompt keeping edits; success `Alert` "Saved — N architectures need a rebuild" with `Rebuild` → `startBuilds(stale ∪ failed)`; `beforeunload` + in-app navigation confirmation while dirty; per-arch `stale` warning badge; Build panel and new "Add architectures" `Multiselect` restricted to `builds.buildable_architectures` (addition excludes requested arches) → `addArchitectures`; component summary line "Deployable component v.0.n (archs)"; new-version success navigates to the new version; read-only roles see no save/add/assist controls
    - _Requirements: 1.4, 1.5, 1.6, 1.8, 1.10, 1.11, 1.12, 6.6, 6.7, 8.3, 9.2, 10.5_

  - [x]* 11.3 Write component tests for the editor and Detail_Page
    - `SourceEditor.test.tsx` and `PluginDetail.test.tsx` extensions: all files rendered from the bulk response; binary tab read-only; add/delete/edit produce the expected save request (merge vs replace); dev save success shows stale count and Rebuild calls `startBuilds` with stale ∪ failed; test/prod shows Save as new version and `SOURCE_LOCKED` guidance; navigation blocked while dirty; stale badges; Add architectures lists only registry − requested and calls `addArchitectures`; Build panel excludes non-registry arches; Viewer/Operator/DataScientist see read-only editor without assistant
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6, 1.8, 1.10, 1.11, 6.6, 6.7, 9.2_

- [x] 12. Frontend: Git connections page and GitSyncPanel
  - [x] 12.1 Implement `GitConnections.tsx` (`/node-designer/git-connections`)
    - Use_Case-scoped table (name, provider badge, repo URL, default branch, verification `StatusIndicator` with failure explanation), create/edit `Modal` (token `Input type="password"`, never pre-filled, "leave blank to keep" on edit), Verify and Delete (`ConfirmationModal`) actions, polling while `verifying`; nav entry under Node Designer; mutations gated by `canManageNodeDesigner`
    - _Requirements: 2.1, 2.2, 2.5, 2.7, 2.8, 2.9_

  - [x] 12.2 Implement `GitSyncPanel.tsx` on the Detail_Page
    - Unlinked: connection `Select` (verified only) + branch/path inputs → `setGitLink`; linked: connection summary, last sync (kind, short SHA linked to the provider commit URL, by, at), `Push` (message input; `force` checkbox shown only after a `diverged` failure; dirty-editor `ConfirmationModal` "Save or discard first"), `Pull` (ref input, mode radio defaulting `in_place` on dev / `new_version` otherwise with `in_place` disabled on test/prod), `Unlink`; operations table (kind, status, category badge, `describeSyncFailure` text, redacted excerpt `Popover`, commit, user, timestamps) polling every 4 s while queued/running; disconnected state when the connection no longer exists; pull `new_version` success navigates to the new version
    - _Requirements: 2.6, 2.8, 3.1, 3.2, 3.7, 3.8, 3.12, 3.13, 4.1, 4.2, 4.10, 4.11, 10.5_

  - [x]* 12.3 Write component tests for the git surfaces
    - `GitConnections.test.tsx`: create sends token once and never renders it back; verifying → verified/failed rendering; delete confirmation; role gating. `GitSyncPanel.test.tsx`: link defaults; push blocked while dirty; `diverged` reveals force; pull mode defaults per lifecycle; operation polling to settlement; category explanations; disconnected rendering
    - _Requirements: 2.4, 2.6, 2.9, 3.7, 3.8, 3.13, 4.2, 4.10, 4.11_

- [x] 13. Frontend: diagnostics-aware CodeAssistPanel and Fix with AI
  - [x] 13.1 Extend `CodeAssistPanel.tsx`
    - Props `diagnostics?` (seeded `ExpandableSection` "Attached error output ({kind}{, arch})" with clear button) and an always-present `ExpandableSection` "Error output" `Textarea` (kind `user`); `onAccept(code, targetFile?)`; reviewing view shows "Applies to `{target_file}`" with a warning `Badge` when it differs from the active file; submit sends `diagnostics` (16 KiB tail, `truncated` note) and the extended `context`; `describeCodeAssistError` maps `INVALID_TARGET_FILE`; NodeConfigPanel surfaces unchanged apart from the new error-output field
    - _Requirements: 5.3, 5.4, 5.7, 5.11, 5.12_

  - [x] 13.2 Wire "Fix with AI" on the Detail_Page and simulator view
    - Failed-arch rows get an inline `Fix with AI` button that seeds `{kind: 'build', architecture, text: logTail}`, focuses the tab from `pickFileForDiagnostics`, and scrolls to the assistant; `SimulatorView.tsx` failure view gets the same action with `{kind: 'simulation', text: message + errorOutput}` navigating to the Detail_Page with the seeded context (router state); after Accept, the save `Alert` offers "Save and rebuild failed architectures"
    - _Requirements: 5.1, 5.2, 5.11_

  - [x]* 13.3 Write component tests for the assist changes
    - `CodeAssistPanel.test.tsx` extensions: seeded diagnostics rendered and sent; pasted error sent as `user`; truncation note at > 16 KiB; target-file badge and `onAccept` receiving the target; `INVALID_TARGET_FILE` alert retains prompt and diagnostics. `PluginDetail.test.tsx`: Fix with AI seeds the panel and focuses the picked tab; simulator failure action carries the context
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.7, 5.11_

- [x] 14. Final checkpoint
  - Ensure all baselines pass: portal backend pytest scoped to `tests/` from `edge-cv-portal/backend` (including the runner wrapper), `npx vitest run` plus `npm run build` from `edge-cv-portal/frontend`, and `npm test` plus `npm run build` from `edge-cv-portal/infrastructure`; ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP
- Each task references specific requirements for traceability; all 18 design properties are covered: Properties 2, 3, 4 (tasks 1.5–1.7 and 10.4), Properties 5, 6, 7 (2.3–2.5), Property 8 (3.2), Properties 9–13 (5.4–5.8), Properties 15, 16 (8.3, 8.4), Properties 1, 14, 17, 18 (10.3, 10.5–10.7)
- Python property tests use hypothesis with no hardcoded `max_examples` (project default ≥100 iterations); TypeScript property tests use fast-check with `numRuns: 100`; each tagged `**Feature: custom-node-source-lifecycle, Property {number}: {property_text}**`
- No portal Lambda ever reads a Git_Credential: the GitSyncHandler role deliberately lacks `secretsmanager:GetSecretValue`, and the only reader is the `dda-plugin-git-sync` CodeBuild role via a `SECRETS_MANAGER`-typed environment variable
- `requested_architectures` becomes a monotonic union (task 2.1); the existing `adjust_revision` check `architecture in requested_architectures` keeps working because the union only grows
- The `arm64_jp7` build image must be built and pushed out of band with `plugin-build-images/build-and-push.sh arm64_jp7` before the sixth CodeBuild project can run a build; until then `arm64_jp7` builds fail at image pull (the project still deploys)
- Bedrock is exercised only through mocked Converse responses; git is exercised only against local bare repositories; no test contacts GitHub, GitLab, or Bedrock
- Out of scope (candidate follow-ups): private-repository import through a Git_Connection in the ImportView; adopting `SourceEditor` in CreateWizard/GeneratePanel; GitHub App / GitLab OAuth authentication; webhook-triggered pulls
