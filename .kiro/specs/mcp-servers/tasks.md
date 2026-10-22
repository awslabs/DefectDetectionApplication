# Implementation Plan: MCP Servers for the Portal and the Edge LocalServer

## Overview

This plan implements `design.md` (revision 12) for the two MCP servers: the **Portal_MCP_Server** (`EdgeCVPortalMcpStack`: one Lambda behind its own HTTP API, Cognito access tokens from a new MCP app client on the existing user pool, dispatch to the existing handler Lambdas, RBAC through `shared_utils.RBACManager`, audit through the existing Audit_Log with the MCP_Audit_Marker) and the **Edge_MCP_Server** (a supervised loopback child process running `mcp==2.3.0` from an in-image virtualenv, reached through a new `/mcp` route on the existing Backend_API listener under the existing `authorize_request` matrix, with MCP-scoped role gating from the Local_Session_Token's synced role). No new identity store, credential type or API key is introduced (C1–C3).

The revision-9 design review returned **CHANGES_REQUESTED** with nine findings (M1, M2, N1–N7, labelled `review-9 <id>` below). Revision 10 applied all nine (Phase 0 tasks 1.1–1.10), and the revision-10 review (2026-10-21; the design reviews are kept outside the repository) judged every one **ADDRESSED**. That review again returned **CHANGES_REQUESTED**, with eight new findings: 0 HIGH, 1 MEDIUM (M1) and 7 NIT (N1–N7), labelled `review-10 <id>` below. Each is carried as a design-correction task in **Phase 0** (tasks 1.11–1.18), and the implementing task that applies each correction names it again. Revision 11 applied all eight, and the revision-11 review (2026-10-22) judged seven **ADDRESSED** and review-10 N3 **PARTIAL** (task 1.14 stays open). That review returned **APPROVED**, with six new findings: 0 HIGH, 0 MEDIUM and 6 NIT (N1–N6), labelled `review-11 <id>` below. Five are design-correction tasks 1.19–1.23; review-11 N2 continues review-10 N3 and is carried on task 1.14. The review allows applying them during the tasks sync or the first implementation task without another review loop; this sync edits `tasks.md` only, so they stay open, to be applied to `design.md` before any code. Phase 0 stays open until those are applied and the owner accepts the design: revision 10 was not accepted (owner, 2026-10-21), accepting revision 11 is the owner's call, and sign-off item (j) remains an open owner decision. **Revision 12 (2026-10-22)** applied tasks 1.14 and 1.19–1.23 to `design.md` before any code, with a structural self-check and no further review loop, as the review allowed; every Phase-0 task is ticked. The owner gave the go to implement on 2026-10-22; formal acceptance of the design text and sign-off item (j) are presented in the sign-off memo and are not marked here.

Phases follow the design's "Phasing" section. **Phase 1 (MVP, read-only) ships alone**: both servers list and accept only Phase-1 `RO` tools (`DELIVERED_PHASES = (1,)`, mutating switches default off), with full authentication, authorization, audit, client configuration, documentation, the Requirement 16 test suites, the CDK stack, the edge venv/supervisor/proxy, the on-hardware stage's read-only part, on-device verification on JP5 and JP6, and the owner sign-off items. Phase 2 (low-consequence mutating) and Phase 3 (high-consequence mutating) each ship independently afterwards.

**Phase gate decision.** The declarative catalogs (`mcp_portal/catalog.py`, `mcp_edge/child/catalog.py`) are authored complete in Phase 1 — every row of every phase — and `DELIVERED_PHASES` is the only phase gate. Reason: the design's Phase-1 suites run over the whole catalog (the RBAC matrix is "every catalog tool"; the protocol, guardrail, argument, classification and permission tests use Phase-2/3 rows such as `portal_devices_deployment_create`, `edge_registration_trigger`, `edge_static_image_pin` and `compilation.start_compilation_job` as fixed cases), and a spec outside `DELIVERED_PHASES` is neither listed nor accepted (Property 3, Requirement 17.3), so no partially delivered tool is reachable. Phases 2 and 3 therefore re-verify their rows at the then-current HEAD, collect the owner sign-offs, bump `DELIVERED_PHASES`, and run the on-device verification of their mutating tools.

Property-based tests use Hypothesis (Python) and fast-check (TypeScript), each configured for at least 100 examples (`@settings(max_examples=100, deadline=None)`) and tagged `**Feature: mcp-servers, Property {number}: {property title}**`, as the design's Correctness Properties section specifies. Each property test lives in its own file.

### Verification commands used below

- `PORTAL_MCP_PYTEST` = `cd edge-cv-portal/backend && uv run --python 3.11 --with-requirements requirements-dev.txt --with hypothesis -m pytest` — the MCP suites need Python ≥ 3.10 (design Testing Strategy, N4); the build host's default `python3` is 3.9. Hypothesis is unpinned in the repository today; the existing portal suite already depends on it.
- `PORTAL_PYTEST` = `cd edge-cv-portal/backend && python3 -m pytest` — the existing portal suite on the host interpreter (regression runs, `--ignore=tests/mcp`).
- `CDK_TEST` = `cd edge-cv-portal/infrastructure && npm run build && npx jest`.
- `EDGE_CHILD_PYTEST` = `PYTHONPATH=src/backend:test/backend-test uv run --python 3.11 --with-requirements src/backend/mcp_edge/requirements.lock.txt --with pytest==9.0.3 --with hypothesis -m pytest --confcutdir test/backend-test/mcp_edge` (repo root) — the child-side modules that import `mcp`. `--confcutdir` keeps pytest from loading `test/backend-test/conftest.py`, which imports backend-process packages (`sarge`, `fastapi`) that the child lock does not carry; `mcp_edge/` has no conftest of its own.
- `EDGE_CONTAINER_PYTEST <files>` = the flask-app container command of `.kiro/steering/builds.md` ("Security preservation gate", step 3) with `<files>` in place of the preservation directory — the backend-process modules, which import no `mcp` and need the backend's pinned FastAPI/httpx 0.24.1 (JP5 image `python3.11`, JP6 image `python3.10`; the `command -v` shim picks it).
- `GUARD_SUITE` = the two `test_preservation_*out_of_scope_guard.py` files of builds.md "Before dispatching any build", step 5.

## Tasks

### Phase 0 — Design corrections (review-9 applied in revision 10; review-10 applied in revision 11, N3 partial; review-11, verdict APPROVED with 6 NIT, applied in revision 12)

- [x] 1. Apply every open design-review finding to `design.md` before implementation starts (review-9 in revision 10, done; review-10 in revision 11, done except review-10 N3, partial; review-11 and review-10 N3 in revision 12, done)
  - [x] 1.1 [review-9 M1] Make `usecase_id` forwarding explicit on the thirteen rows that read it
    - In `.kiro/specs/mcp-servers/design.md` Tool catalogs: add `(→ query usecase_id)` to `portal_devices_list`, `portal_devices_get`, `portal_devices_deployments_list`, `portal_devices_shared_components_list`, `portal_data_datasets_list`, `portal_data_datasets_preview_get`, `portal_data_prelabeled_list`, `portal_workflows_node_catalog_get`; `(→ body usecase_id)` to `portal_data_datasets_count`, `portal_data_manifest_validate`, `portal_workflows_generate`; `(→ path id)` to `portal_admin_usecase_delete`. `portal_data_prelabeled_get` stays RBAC-only (`get_dataset` authorizes the stored record, 537)
    - Decision for `portal_admin_users_list`: keep `usecase_id?` and map it `(→ query usecase_id)` as a filter (not the RBAC scope, which stays `manage_users (G)`), because `user_management.py:72` honours it and dropping it would remove a filter the REST route offers (1.3)
    - Replace the Tool-catalogs Scope sentence ("the dispatcher maps `usecase_id` to the `{id}` path parameter") with "the dispatcher maps `usecase_id` to `{id}` only through an explicit `(→ path id)` entry", so one rule remains; correct the revision-9 response table's Sweep sentence "`usecase_id` forwarding made explicit on every row"
    - Add the thirteen rows, each with its handler read line from the finding (`devices.py:226/370`, `deployments.py:925`, `shared_components.py:990`, `datasets.py:111/262/368`, `pre_labeled_datasets.py:125/223`, `workflow_validation.py:294`, `user_management.py:72`, `workflow_generator.py:657`, `usecases.py` `DELETE /usecases/{id}`), to the `test_mcp_catalog_arguments.py` fixed cases in the Testing Strategy
    - Applied in code by tasks 5.1, 5.2, 5.4, 5.5 (rows) and 5.10 (fixed cases)
    - Verify: the row-summary check of task 1.10 lists the thirteen rows with their `param_map` entries; the revision-10 response table records M1
    - Revision-10 review: **ADDRESSED**. The converse-clause exemption it leaves open is task 1.12 [review-10 N1]
    - _Requirements: 1.3, 3.1, 5.6_

  - [x] 1.2 [review-9 M2] Forward `limit`/`offset` on the five route-paginated rows and close the test gap
    - Add `limit (→ query limit)` to `portal_admin_audit_logs_query`, `portal_workflows_builds_list`, `portal_workflows_build_logs_get`, `portal_data_datasets_preview_get`, `portal_data_captures_list`, and `offset (→ query offset)` to `portal_data_datasets_preview_get`
    - Add to Property 2's argument clause and the `test_mcp_catalog_arguments.py` row: when the handler reads `limit`/`offset`, or the spec maps a cursor (`next_token`), `limit` (and `offset` when read) must have a `param_map` entry; a dispatcher-sliced spec maps no cursor. The five rows are fixed cases; `build_jobs.get_build_logs` (route default 200 vs catalog default 50) is the failing example
    - Applied by tasks 5.1, 5.4, 5.5, 5.7 and 5.10
    - Verify: task 1.10's check shows the five `limit` mappings and the `offset` mapping; Property 2 text contains the cursor clause
    - Revision-10 review: **ADDRESSED**. The class scan also changed the edge row `edge_registration_executions_list` to `limit (→ query limit)` (`api.py:200/210`), which task 11.4 carries
    - _Requirements: 1.6, 1.3_

  - [x] 1.3 [review-9 N1] Make the edge classification walk transitive and declare accepted effects
    - Decision point 4, Property 2 edge clause, Testing Strategy `test_child_catalog_classification.py` row and the Invariants "No RO tool changes state" row: walk the transitive call graph over `src/backend` with the portal walk's resolution rules plus `self.method` and `Class.method`
    - Add `ChildToolSpec.accepted_effects: tuple[str, ...]` (`'<module>.<function>:<call>'`) with a fixed `ACCEPTED_EFFECT_NOTE`, the twin of `accepted_writes`, seeded with: `stream_ingest.protocol.sweep_segments:os.unlink` (stream_health_get, test_connection, preview, capabilities_get); `stream_ingest.capabilities.run_probe:subprocess.run` (capabilities_get); `utils.utils.run_command:subprocess.run` via `filesystem_management_utils.chown` (preview, test_connection); the CSI config write (preview); `run_retention.RunRetention._remove_dir:shutil.rmtree` (registration_executions_list); the Triton client and `model_status_shadow.report` (feature_configurations_list, gpu_status_get)
    - In the rows, describe the `/dev/shm` sweep once as process-level housekeeping
    - Applied by tasks 11.4 (spec field and seeds) and 11.13 (walk)
    - Verify: Property 2's edge clause and the Testing Strategy row name the transitive walk and `accepted_effects`
    - Revision-10 review: **ADDRESSED**, with the seed list changed on evidence. The walk finds five effects on eight tools, and those are the seeds: the sweep on eight tools, `run_probe`, and `run_command`, the empty-frame `delete_image` and `csi_capture.write_csi_config:open` on `_test_connection`/`_preview`. The retention `_remove_dir`, the Triton client and `model_status_shadow.report` are outside the walk's reach, so they stay fixed `RO` cases and are not entries. The remaining issues are tasks 1.11 [review-10 M1] (the caller-supplied preview override) and 1.18 [review-10 N7] (the JP6 Pillow staging write)
    - _Requirements: 10.1, 8.2, 8.3_

  - [x] 1.4 [review-9 N2] Make the portal classification inputs robust and complete
    - §2 `catalog.py`, Decision point 4 and the Testing Strategy portal classification row: (a) `accepted_writes` entries match on `(module.function, verb, receiver source text)` plus an occurrence count; the line number stays only as documentation in the note; rewrite the twelve existing entries in that form; (b) `SHARED_LAYER_WRITERS` is derived in the test — every first-party function in `edge-cv-portal/backend/layers/shared/python/*.py` whose body, transitively within the layer, contains a write verb, minus `log_audit_event` — and the test asserts the derived set contains the seven named writers (and reaches `user_roles_dao.assign_user_to_usecase`/`remove_user_from_usecase`/`update_user_role`/`remove_all_user_assignments`/`remove_all_usecase_assignments` and `s3_cors.ensure_bucket_cors`)
    - Applied by tasks 4.1 (entry format), 5.2–5.5 (rows) and 5.8 (test)
    - Verify: no `accepted_writes` entry in the revision-10 tables carries a line number as its key
    - Revision-10 review: **ADDRESSED**. The twelve call sites became eleven `(module.function, call, receiver source text, count)` entries, because `get_training_job`'s two calls are one entry with count 2. The derived `SHARED_LAYER_WRITERS` has fourteen writers at `c3bf88c1`: the seven named, the review's six, and `bulk_assign_users`. Nothing remains
    - _Requirements: 5.1, 5.2_

  - [x] 1.5 [review-9 N3] Let the argument test see keys read through constants
    - Testing Strategy `test_mcp_catalog_arguments.py` row and Property 2's portal argument clause: the read collector folds module-level string constants (local and imported; `Name` and `Attribute` forms) and tuple/list constants iterated against `body`/`params` (loops and comprehensions)
    - Add the fixed cases `portal_devices_deployment_create` (`deployment_preflight.ACKNOWLEDGEMENT_FIELD`, `deployments.py:1899–1900`), `portal_admin_usecase_create`/`_update` (`tuning_settings.SAMPLE_EXPORT_FIELD`/`SAMPLE_RETENTION_FIELD`, `tuning_settings.py:32/35, 151–152`) and `portal_data_synthetic_session_update` (`PATCHABLE_FIELDS`, `synthetic_data.py:89–93, 543`)
    - Applied by task 5.10
    - Verify: the Testing Strategy row names constant folding and the four fixed cases
    - Revision-10 review: **ADDRESSED**, and extended. The collector also folds function-local and set constants, follows `getattr(<module>, '<literal>')` delegation, and checks path-mapped targets against the `{name}` segments of `resource`. The new bare-identifier path rule is ambiguous on one row, which is task 1.13 [review-10 N2]
    - _Requirements: 1.3_

  - [x] 1.6 [review-9 N4] Offer `offset` on the three dispatcher-sliced unbounded collections
    - Add `offset` (dispatcher-sliced) to `portal_devices_list`, `portal_devices_deployments_list`, `portal_admin_usecases_list`; add the Property 2 clause "a dispatcher-sliced collection spec has `offset`"
    - Applied by tasks 5.1, 5.2, 5.7 and 6.5
    - Verify: task 1.10's check lists `offset` on the three rows
    - Revision-10 review: **ADDRESSED**. Nothing remains
    - _Requirements: 1.6_

  - [x] 1.7 [review-9 N5] Bind id-addressed labeling reads to the named Use_Case and add sign-off item (j)
    - Add `ToolSpec.bind_usecase_path: str | None` to §2 `catalog.py`/`shaping.py`/`audit.py`: after a 2xx, the shaper compares the body value at that dotted path with the authorized `usecase_id`; on a mismatch it drops the body and returns `not_found`, and the envelope is written with `result='denied'` and `details.reason='usecase_binding'`. Set `'job.usecase_id'` on `portal_data_labeling_job_get`
    - Remove `portal_data_labeling_manifest_get` from `CATALOG` until Backlog 18's binding lands (its body carries no Use_Case); update the tool counts (portal 185 → 184; Phase 1 88 → 87)
    - Add Phasing sign-off item (j): id-addressed Greengrass reads (`portal_devices_deployment_get`, `portal_devices_components_get`) are scoped by the account rather than by the named Use_Case in single-account deployments (Backlog 26) — the owner accepts this as REST parity or defers those two tools
    - Applied by tasks 4.1, 5.4, 6.5, 6.8 and 19.1
    - Verify: the data row, §2 and the Phasing table carry the changes; the counts in Overview, Decision point 1 and the response table agree with task 1.10's recount
    - Revision-10 review: **ADDRESSED**, and extended. An absent body value is refused like a mismatch. `portal_data_labeling_manifest_get` is listed with sign-off item (h)'s deferrals. Item (j) names seven Greengrass-backed reads instead of two: `portal_devices_get`, `_logs_list`, `_logs_get`, `_logs_analyze` (Phase 2), `_deployment_get`, `_deployment_for_target_get` and `_components_get`. The design recommends accepting them as REST parity. The remaining issues are tasks 1.14 [review-10 N3], 1.15 [review-10 N4] and 1.17 [review-10 N6]
    - _Requirements: 3.3, 1.3, 4.1_

  - [x] 1.8 [review-9 N6] Correct the phasing text for the two Phase-2 read-only tools
    - Reword Decision point 8 to "every read-only tool except `portal_devices_logs_analyze` (long fan-out) and `portal_workflows_plugin_generate_status_get` (polls a Phase-2 generation session)" and name the latter in the Phasing table's Phase-2 scope
    - Applied by tasks 16.3 (per-tool phase in the docs) and 22.1
    - Verify: Decision point 8 and the Phase-2 scope cell name both tools
    - Revision-10 review: **ADDRESSED**. Nothing remains
    - _Requirements: 17.3, 17.1_

  - [x] 1.9 [review-9 N7] Make `portal_devices_deployment_create` confirm its nameable target
    - D9, §2 `catalog.py` and §4 step 3: allow `confirm_target` to name the branches of a `oneOf` (`'target_thing_group'`, `'target_devices[0]'`); the validator requires `confirm` to equal whichever branch is present; `render_description` renders `requires confirm=<target_thing_group | target_devices[0]>`
    - Change the row from `confirm="deploy"` (`confirm_literal`) to that `confirm_target`; update Property 2's `confirm_target`/`confirm_literal` clause and the `test_mcp_protocol.py` M call (Testing Strategy) to pass the thing-group name as `confirm`
    - Applied by tasks 4.1, 5.2, 5.7 and 7.1
    - Verify: the devices row, D9, Property 2 and the Testing Strategy row agree
    - _Requirements: 5.3, 14.2_

  - [x] 1.10 Record revision 10 and recount the catalog
    - Add "Response to the design review (revision 10)" to `design.md` with one row per finding M1, M2, N1–N7 and its disposition; bump the revision note in the Overview
    - Re-run the row-summary script used for this plan (parse every catalog table row → tool, class, phase, Key arguments) and confirm portal 184 tools (87 / 53 / 44 by phase) and edge 73 (47 / 9 / 17), and that every row named in 1.1, 1.2, 1.6 carries its new `param_map` entry
    - Verify: the recount matches the counts quoted in the Overview, Decision point 1 and the response table; the owner (or the workflow's reviewer) accepts revision 10 before task 2 starts
    - Done (tasks.md sync, 2026-10-21):
      - `design.md` has "## Response to the design review (revision 10)", and the Overview says "tenth revision".
      - Independent recount from the six catalog tables, expanding every `/`-joined name and every per-name class and phase cell. Rows: admin 15, devices 31, models 15, data 27, workflows 25, edge 48, total 161.
      - Portal: **184** tools; by phase 87 / 53 / 44; by class 89 `RO` / 66 `M` / 29 `M!`. The 89 `RO` tools are 87 in Phase 1 plus 2 in Phase 2. Per view: admin 15, devices 34, models 22, data 48, workflows 65. `portal_data_labeling_manifest_get` is absent.
      - Edge: **73** tools; by phase 47 / 9 / 17; by class 47 `RO` / 12 `M` / 14 `M!`.
      - Every row named in 1.1, 1.2 and 1.6 carries its new `param_map` or `offset` entry.
      - These counts agree with every count `design.md` quotes: the Overview, Decision point 1, Decision point 4 (88 routed `RO` tools in `CATALOG`), the N6 class scan and the task 1.10 row.
    - Revision 11 recount (tasks.md sync, 2026-10-22): `design.md` has "## Response to the design review (revision 11)" (17 rows) and the Overview says "eleventh revision". Re-running the row-summary parse over the revision-11 tables gives rows admin 15, devices 31, models 15, data 27, workflows 25, edge 48, total **161** (27 changed in place, none added or removed); portal **184** tools, 87 / 53 / 44 by phase, 89 `RO` / 66 `M` / 29 `M!` (87 Phase-1 `RO` + 2 Phase-2 `RO`), per view admin 15, devices 34, models 22, data 48, workflows 65, no `portal_data_labeling_manifest_get`; edge **73** tools, 47 / 9 / 17 by phase, 47 `RO` / 12 `M` / 14 `M!`. All unchanged from revision 10, and they agree with `revise-summary.md` and with every count `design.md` quotes (Overview, Decision points 1 and 4, Testing Strategy, the revision-11 recount row)
    - Revision 12 recount (setup step, 2026-10-22): `design.md` has "## Response to the design review (revision 12)" (8 rows: review-11 N1–N6, implementation drift, recount) and the Overview says "twelfth revision". Re-running the row-summary parse over the revision-12 tables gives rows admin 15, devices 31, models 15, data 27, workflows 25, edge 48, total **161** (10 changed in place: 8 for the review-11 findings, 2 portal and 6 edge, plus 2 portal rows reworded without a change of meaning; none added or removed; 6 cells per portal row, 5 per edge row, 0 pipe mismatches); portal **184** tools, 87 / 53 / 44 by phase, 89 `RO` / 66 `M` / 29 `M!` (the build-server row read in its `_stop`/`_delete` form); edge **73** tools, 47 / 9 / 17 by phase, 47 `RO` / 12 `M` / 14 `M!`. The per-tool class and phase list is identical to revision 11's, and the counts agree with every count `design.md` quotes
    - Owner acceptance (not marked): owner go to implement given 2026-10-22; formal acceptance of the design text: see sign-off memo
    - _Requirements: 17.1, 17.4_

  - [x] 1.11 [review-10 M1] Stop the read-only preview from taking a caller-supplied pipeline or acquisition override (continues the review-9 N1 class)
    - Problem: `edge_image_source_preview` is a Phase-1 `RO` tool, and its untyped `image_source_configuration?` forwarded the route's settings override (`endpoints/image_source.py:94`), so a caller-supplied setting could reach the preview; that contradicts `ACCEPTED_EFFECT_NOTE` and Requirement 10 (Backlog 28)
    - Decision: option (b) of the review. Remove `image_source_configuration?` from `edge_image_source_preview`, so it previews the stored settings only and the CSI write stores only the source's own saved settings, keeping `ACCEPTED_EFFECT_NOTE` true. Reason: option (a)'s closed schema still persists caller tuning for CSI sources, which needs either a new owner sign-off item or dropping the CSI keys. For CAMERA sources, gain/exposure applied through the pipeline is a further effect that (a) leaves unexamined. (b) keeps the `RO` definition with no sign-off and no count change (edge 73; 47 / 9 / 17)
    - Previewing unsaved settings stays possible through the Phase-3 `edge_image_source_update` followed by the preview. A tuning preview as a separate `M` tool with (a)'s closed schema is not added. Record it in the new Backlog entry below
    - Edit in `design.md`:
      - the edge row `edge_image_source_preview` (Key arguments: `image_source_id` only; restate the CSI accepted reading as the stored settings);
      - Decision point 4 (`ACCEPTED_EFFECT_NOTE` paragraph);
      - §7 step 5;
      - Property 2's edge clause, with a fixed case: no `RO` edge spec offers `image_source_configuration`, and a call that carries it (e.g. `{'processingPipeline': 'videoconvert ! filesink location=/tmp/x'}`) is `invalid_input`;
      - the Invariants row "No `RO` tool changes Portal or device state";
      - the Testing Strategy `test_child_catalog_arguments.py` row (the fixed case);
      - a new Backlog entry: restrict the settings override of `POST /image-sources/{id}/preview` (REST-side follow-up), plus the optional Phase-2 `M` tuning-preview tool with the closed schema.
    - Applied in code by tasks 11.4 (row) and 11.13 (fixed case)
    - Verify: the row, Decision point 4, §7, Property 2, Invariants, Testing Strategy and Backlog agree; the revision-11 response table records review-10 M1; the edge recount stays 73 (47 / 9 / 17)
    - Revision-11 review: **ADDRESSED** (option (b)). The class re-derivation agrees: no `RO` tool of either catalog takes an object argument, and no `RO` string reaches a pipeline, a command or a persisted configuration. The Invariants wording that overstates the object-only check is task 1.22 [review-11 N5]
    - _Requirements: 10.1, 10.3, 10.7, 8.3_

  - [x] 1.12 [review-10 N1] Give the converse `usecase_id` clause a machine-readable exemption (continues review-9 M1, task 1.1)
    - Add `ToolSpec.usecase_not_forwarded: Literal['stored_record', 'injected', 'dead_filter'] | None = None` to §2 `catalog.py`. The converse clause of Property 2 and `test_mcp_catalog_arguments.py` ("a spec whose handler or decorator reads `usecase_id` maps it") skips specs that set it, and Property 2 validates it: set only when `usecase_id` has no `param_map` entry
    - Set `'injected'` on the ten `setdefault`-scoped specs (job, team, review, rerun and preview-run; Backlog 27). Set `'stored_record'` on `portal_data_synthetic_session_retrain` (`synthetic_data.py:1248` overwrites the body value). Set `'dead_filter'` on `portal_admin_audit_logs_query` (`audit_logs.py:84`; Backlog 25)
    - Make the last two fixed cases. `G` specs with `allow_global` or `require_builds_*` stay exempt by scope
    - Qualify the Overview sentence "forwarding is explicit on every row whose route reads them" with the audit-log dead-filter exception (review-10 §6, "Imprecise")
    - Edit in `design.md`: §2 `catalog.py`; the Tool catalogs conventions paragraph (the RBAC-only reasons); the affected rows; Property 2; the Testing Strategy `test_mcp_catalog_arguments.py` row; the Overview
    - Applied in code by tasks 4.1 (field), 5.1 and 5.4 (values), 5.7 (Property 2 clause) and 5.10 (converse clause and fixed cases)
    - Verify: §2, Property 2, the Testing Strategy row and the twelve rows agree; the revision-11 response table records review-10 N1
    - Revision-11 review: **ADDRESSED**. The field is on ten `'injected'` specs (exactly the routes behind the four `setdefault` sites), `'stored_record'` on `portal_data_synthetic_session_retrain` and `'dead_filter'` on `portal_admin_audit_logs_query`; the 47 read-none rows carry no field. Nothing remains
    - _Requirements: 1.3, 3.1, 5.6_

  - [x] 1.13 [review-10 N2] Make the bare-identifier path rule unambiguous (continues review-9 N3, task 1.5)
    - Write `camera_source_id? (→ body camera_source_id)` in the `portal_devices_camera_add` row (`camera_registry.py:1015` reads it from the body)
    - Tighten the conventions sentence to: "else, when exactly one bare identifier remains, the one unmapped path parameter; `catalog.py` raises if two bare identifiers would claim one path parameter"
    - Edit in `design.md`: the Tool catalogs conventions paragraph (bare-identifier sentence); the devices row `portal_devices_camera_add`; §2 `catalog.py`
    - Applied in code by tasks 4.1 (the raise) and 5.2 (row)
    - Verify: the row and the conventions agree, and no other row has two bare identifiers for one path parameter (re-run the path-identifier scan); the revision-11 response table records review-10 N2
    - Revision-11 review: **ADDRESSED**. The narrowed wording "(a name ending in `_id`)" leaves `model_name` unmapped on three edge rows, which is task 1.19 [review-11 N1]
    - _Requirements: 1.3_

  - [x] 1.14 [review-10 N3] Make a D36 refusal indistinguishable from a missing object (continues review-9 N5, task 1.7)
    - For `bind_usecase_path` specs, map a backend 404 to the same `ToolFailure('not_found', 'no such object in the named Use_Case')` that the binding refusal returns, instead of the handler's scrubbed text (`labeling.py:678` "Labeling job not found"). Then D36's "its existence is not confirmed" holds
    - Assert in the 16.4 audit test that the missing-id result and the other-Use_Case result are identical (same category, same message)
    - Edit in `design.md`: D36; the Error Handling portal rows (backend 404; Use_Case binding); §2 `shaping.py`; the Testing Strategy audit-test row
    - Applied in code by tasks 6.5 (mapping) and 6.8 (identical-result assertion)
    - Verify: D36, Error Handling and the Testing Strategy row agree; the revision-11 response table records review-10 N3
    - Revision-11 review: **PARTIAL**. Applied in revision 11 to D36, §2 `shaping.py`, §4 step 9, Error Handling (823/824), Property 8 and the 16.4 row. Remaining: `structured_content.status` is unpinned (§2 `errors.py` `ToolFailure(..., status=None)` while the backend-status row keeps 404), and the 16.4 assertion compares category and message only, where review-10 asked for byte-identical results
    - [review-11 N2] (continues this finding; NIT) Fix: state in D36, §2 `shaping.py` and the Error Handling backend-status row that on a `bind_usecase_path` spec both outcomes return `ToolFailure('not_found', 'no such object in the named Use_Case', status=404)`, with the real backend status kept only in the `mcp_tool` log line and the envelope's `details.status`; make Property 8 and the 16.4 test compare the whole `CallToolResult` (`content` and `structured_content`) except `request_id`. Also check D21 (`status?`), §2 `errors.py`, the Data Models log line (659) and the Tool error result (689). Applied in code by tasks 6.5 (`status=404`), 6.6 (Property 8) and 6.8 (whole-result assertion); verify that these sections agree and that the revision-12 response table records review-11 N2
    - Done in revision 12 (setup step, 2026-10-22): D21, D36, §2 `errors.py`/`shaping.py`, §4 step 9, the Data Models log line, Error Handling, Property 8 and the 16.4 row agree on `status=404` and the whole-result comparison; the revision-12 response table records review-11 N2
    - _Requirements: 3.3, 1.5, 16.4_

  - [x] 1.15 [review-10 N4] Account for the D36 refusal in Property 6, §4 step 9 and the Invariants (continues review-9 N5, task 1.7)
    - Property 6: add "on a D36 refusal the total is k + 1, the extra entry being the denied envelope with `details.reason='usecase_binding'`", and generate that outcome. `audit.py` writes the denied envelope even when `handler_audits` is True
    - §4 step 9: "… shapes every status < 300 unless step 7 refused the body, which returns `not_found`"
    - Invariants row (901): append "except the sign-off (j) reads while the owner accepts REST parity"
    - Edit in `design.md`: Property 6; §4 step 9; §2 `audit.py`; the Invariants row
    - Applied in code by tasks 6.8 (envelope), 6.9 (Property 6 generator) and 6.10 (step 9)
    - Verify: Property 6, §4, §2 `audit.py` and the Invariants row agree; the revision-11 response table records review-10 N4
    - Revision-11 review: **ADDRESSED**, also in Security C5, sign-off item (f) and the Data Models log line. Nothing remains. Sign-off item (j) stays an open owner decision (design Phasing, 1052; task 19.1)
    - _Requirements: 4.1, 4.2, 16.4_

  - [x] 1.16 [review-10 N5] Require exactly one element in `target_devices` (continues review-9 N7, task 1.9)
    - Declare `target_devices` on `portal_devices_deployment_create` as `{type: array, minItems: 1, maxItems: 1, items: {type: string, pattern: THING_NAME_PATTERN}}`, so `{'target_devices': []}` is `invalid_input` before the per-branch `confirm` check indexes `target_devices[0]` (the handler's own 400 is `deployments.py:1779`)
    - Property 2: any array that a per-branch `confirm_target` indexes has `minItems == maxItems == 1`
    - Edit in `design.md`: the devices row `portal_devices_deployment_create`; §4 step 3; Input validation; Property 2
    - Applied in code by tasks 5.2 (schema) and 5.7 (Property 2 clause); task 7.5 adds the empty-array rejection
    - Verify: the row, §4 and Property 2 agree; the revision-11 response table records review-10 N5
    - Revision-11 review: **ADDRESSED**, and extended: `target_thing_group` now carries `THING_NAME_PATTERN` (so `""` is `invalid_input` and no `/` reaches the ARN), and Property 2 requires a pattern excluding `""` on every per-branch target string. The line slips in row 449 and its response row are task 1.23 [review-11 N6]
    - _Requirements: 5.3, 14.2_

  - [x] 1.17 [review-10 N6] Spell the labeling row's tool names in full and state the shorthand rule (related to review-9 N5)
    - Write the data row's tool cell as `portal_data_labeling_jobs_list` / `portal_data_labeling_job_get`, the name D36 and the fixed cases use
    - State the shorthand once in the conventions: "`_x` is the first name's stem plus `_x`; otherwise the name is written in full"
    - Edit in `design.md`: the data labeling row; the Tool catalogs conventions; D36, Property 2 and the Testing Strategy (check that they use the full name)
    - Applied in code by task 5.4 (spec name)
    - Verify: re-running the task 1.10 recount with the stated shorthand rule expands every `/`-joined cell to the names the prose uses, with portal 184 unchanged; the revision-11 response table records review-10 N6
    - Revision-11 review: **ADDRESSED**, applied to every cell (14 cells, 44 names rewritten in full); the stated rule expands the 161 cells to 184 + 73 unique names. The other-column `_x` label rule does not match the columns, which is task 1.21 [review-11 N4]
    - _Requirements: 14.1, 3.3_

  - [x] 1.18 [review-10 N7] Name the JP6 Pillow staging write and put Pillow writes on the effect list (continues review-9 N1, task 1.3)
    - On JP6, `_add_file_image_source` stages `f'{file_path}.dda_decoded.png'` with `im.save` (`gstreamer/pipeline_builder.py:112–127, 172–190`), reached from the NVIDIA_CSI preview (109). That writes `/aws_dda/nvidia-csi-capture/latest.jpg.dda_decoded.png`: device-data scratch, the HMI preview's own effect
    - Decision: take the review's optional half as well. Add PIL `Image.save` on values from `Image.open`/`ImageOps` to the edge walk's effect list, and add the resulting entry to `accepted_effects` on `edge_image_source_test_connection` and `edge_image_source_preview`. Reason: "exactly what the walk finds" then describes the route, not only the list, and the cost is one effect-list rule
    - Name the staging as a fixed case with its line in Decision point 4's housekeeping sentence and in the two rows' accepted readings
    - Edit in `design.md`: Decision point 4; the edge rows `edge_image_source_stream_health_get` / `edge_image_source_test_connection` and `edge_image_source_preview`; the Testing Strategy `test_child_catalog_classification.py` row (effect list)
    - Applied in code by tasks 11.4 (entries) and 11.13 (effect list and fixed case)
    - Verify: Decision point 4, the two rows and the Testing Strategy row agree; the revision-11 response table records review-10 N7
    - Revision-11 review: **ADDRESSED** (Pillow on the effect list, owner-confirmed), with the effect list also covering `os.replace`/`os.rename`, `shutil` writers, file-creating `tempfile`, `Path.write_*` and `open` with a write mode. The reviewer's walk over the 45 `RO` routes finds exactly six accepted effects on eight tools, and JP5/JP6 differ only on `_test_connection`/`_preview` (`_is_jp6()`). The two Pillow definitions and the entry key are task 1.20 [review-11 N3]
    - _Requirements: 10.1, 8.2_

  - [x] 1.19 [review-11 N1] Map `model_name` to its path parameter on the three edge rows (continues review-10 N2, task 1.13)
    - Problem: revision 11 narrowed "identifier argument" to "a name ending in `_id`", so `model_name` on `edge_model_start` (`{modelName}`), `edge_model_stop` (`{modelName}`) and the Phase-1 `RO` `edge_textgen_generate` (`{model_name}`) is no bare identifier any more: `catalog.py` writes no path entry, the child fills path parameters only from `param_map` (§5 `child/dispatch.py`), and the three specs fail Property 2's argument clause or call an unfilled URL template. A scan of every path parameter of all 161 rows finds these three as the only fillers that neither end in `_id` nor carry an explicit `(→ path …)`
    - Fix: write `model_name (→ path modelName)` on `edge_model_start` and `edge_model_stop` and `model_name (→ path model_name)` on `edge_textgen_generate`; add the Property 2 fixed case `edge_model_start.param_map['model_name'] == 'modelName'`; keep "(a name ending in `_id`)" for the collision count and add to the conventions: "any other argument that fills a path parameter is written with its `(→ path …)` entry"
    - Edit in `design.md`: the Tool catalogs conventions paragraph; the edge rows `edge_model_start`, `edge_model_stop`, `edge_textgen_generate`; Property 2 (edge argument clause, fixed case); §5 `child/dispatch.py` (check the path-parameter sentence); annotate the revision-11 N2 class-scan response row
    - Applied in code by tasks 11.4 (rows) and 11.13 (fixed case)
    - Verify: the three rows, the conventions and Property 2 agree; re-running the path-parameter scan over all 161 rows finds every path parameter filled by a name ending in `_id` or by an explicit `(→ path …)` entry; the revision-12 response table records review-11 N1
    - Done in revision 12 (setup step, 2026-10-22): the three rows, the conventions, Property 2 and §5 `child/dispatch.py` agree; the path-parameter scan over the 161 rows finds no non-`_id` filler without an explicit entry
    - _Requirements: 8.3, 14.1_

  - [x] 1.20 [review-11 N3] Define a Pillow image value once and key `accepted_effects` entries by the effect-list name (continues review-10 N7, task 1.18)
    - Problem: (a) Decision point 4 says "`Image.save` on a value from `Image.open`/`Image.new`/`ImageOps`" without `Image.fromarray` or the method rule, while the Testing Strategy row covers bound names only, so a chained `ImageOps.exif_transpose(Image.open(p)).save(q)` is no effect under either; (b) every other entry's `<call>` is the call as written (`os.unlink`, `os.remove`, `subprocess.run`, `open`), but the staging site is `im.save` (`pipeline_builder.py:187`), so a walk keyed like the others reports `_stage_decoded_png:im.save` and fails a correct row; (c) latent: the list lacks the `pathlib` twins (`Path.unlink`/`rename`/`replace`, `Path.open` with a write mode, `touch`/`mkdir`/`rmdir`/`chmod`) and `os.makedirs`/`mkdir`/`rmdir`/`chmod`, of which only `os.makedirs`/`os.chmod` inside the accepted `write_csi_config` (`csi_capture.py:69/80`) are on an `RO` path
    - Fix: define a Pillow image value once in Decision point 4 ("a call of `Image.open`, `Image.new`, `Image.fromarray` or an `ImageOps` function, a method call on such a value, or a name bound from one (assignment or `with` target); `.save(...)` on such a value is the effect `Image.save`") and cite it from Property 2, the Invariants and the Testing Strategy; add to the conventions: "the `<call>` of an `accepted_effects` entry is the effect-list name the walk reports (`os.unlink`, `subprocess.run`, `open`, `Image.save`), not the receiver text"
    - Decision: also take the optional extension (the `pathlib` twins and the `os` mutators, with `csi_capture.write_csi_config:os.makedirs` and `:os.chmod` entries on `edge_image_source_test_connection` and `edge_image_source_preview`). Reason: the same as task 1.18's, so that "exactly what the walk finds" describes the route and not only the list; the cost is effect-list entries and two declared entries on rows that already accept `write_csi_config`, and no classification changes
    - Edit in `design.md`: Decision point 4; Property 2 (edge clause); the Invariants row "No `RO` tool changes Portal or device state"; the Testing Strategy `test_child_catalog_classification.py` row (effect list, the chained-call fixed case, the accepted-effect count); the Tool catalogs conventions (entry format); the edge rows `edge_image_source_test_connection` and `edge_image_source_preview` (the two `os` entries)
    - Applied in code by tasks 11.4 (entries) and 11.13 (value tracking, entry keying, extended list)
    - Verify: Decision point 4, Property 2, the Invariants and the Testing Strategy row cite the one definition; a walk over the 45 existing `RO` routes with the extended list reports exactly the declared entries, and the count quoted in the Testing Strategy matches it; the revision-12 response table records review-11 N3
    - Done in revision 12 (setup step, 2026-10-22): one Pillow-value definition cited from Property 2, the Invariants and the Testing Strategy; entries keyed by the effect-list name; the extended list adds `write_csi_config:os.makedirs`/`:os.chmod` on `_test_connection`/`_preview`, so the Testing Strategy quotes eight accepted effects on eight tools
    - _Requirements: 10.1, 8.2_

  - [x] 1.21 [review-11 N4] Make the other-column `_x` label rule match how the columns use `_x` (continues review-10 N6, task 1.17)
    - Problem: of 146 backticked `_x` labels outside the tool cells, 25 name no tool of their row (10 refer to another row's tool or a removed or renamed one, 15 are private handler functions such as `_authorize`, `_require_review_admin`, `_is_jp6`); the `portal_workflows_build_server_create` row carries the `M!` class of two tools in the bare labels `stop`/`delete`, which no rule covers; `edge_static_video_pin_get`/`_unpin` reads `edge_static_video_pin_unpin` under the stem rule (the tool is `edge_static_video_unpin`); and the response table's prefix rule (1335) is not in the conventions
    - Fix: the build-server row's Class cell → "M (`_stop`/`_delete` M!)"; the edge row `edge_static_image_pin` and Backlog 17 → "`edge_static_video_pin_get`/`edge_static_video_unpin`"; extend the conventions: "…; a backticked `_x` that matches no tool of the row is either a code identifier of the handler module (`_authorize`, `_is_jp6`) or, in descriptive text only, a tool of another row; a label that carries a class, phase, permission or argument always names a tool of its row"
    - Edit in `design.md`: the Tool catalogs conventions paragraph; the workflows row `portal_workflows_build_server_create` / `_start` / `_stop` / `_delete`; the edge row `edge_static_image_pin`; Backlog 17
    - Applied in code by task 5.5 (`portal_workflows_build_server_stop` and `_delete` stay `M!`)
    - Verify: re-run the task 1.10 recount, with its per-name class rule reading the `_stop`/`_delete` form, and confirm portal 184 (87 / 53 / 44; 89 `RO` / 66 `M` / 29 `M!`) and edge 73 unchanged; every `_x` label that carries a class, phase, permission or argument names a tool of its row; the revision-12 response table records review-11 N4
    - Done in revision 12 (setup step, 2026-10-22): recount unchanged; of 147 `_x` labels outside the tool cells, 24 name no tool of their row (15 handler-code identifiers, 9 descriptive cross-row references) and none of them carries a class, phase, permission or argument
    - _Requirements: 14.1, 5.2_

  - [x] 1.22 [review-11 N5] Make the M1 invariant a consequence of its check (continues review-10 M1, task 1.11)
    - Problem: the Invariants row says "no `RO` edge spec takes an `object`-typed argument, so nothing the caller supplies reaches a pipeline or a persisted configuration", but the review-10 M1 defect was a string (`processingPipeline`) carried in an object, and the mechanical clause stops object-typed properties only. A future `RO` spec offering `processing_pipeline?: string` passes Property 2 and `test_child_catalog_arguments.py`. The claim is true today (re-derived), but the "so" is a review conclusion, not a consequence of the check
    - Decision: option (b) of the review, with the Invariants row reworded to name the check. Add to Property 2: every `string` property of an `RO` edge spec carries a `pattern` or `enum` or is listed in `RO_FREE_TEXT_ARGS` with a one-line reason (e.g. `('edge_textgen_generate', 'prompt')`, `('edge_textgen_generate', 'image')`, `('edge_results_list', 'text_note_filter')`; the complete list from a scan of the `RO` edge string properties without a pattern or enum). Reason: every other `RO` claim in the design is held by a declared, machine-checked list (`accepted_writes`, `accepted_effects`, `usecase_not_forwarded`), and option (a) alone leaves the review-10 M1 class guarded by review only. Unbounded strings today are listed, not given new patterns, so no tool's accepted input changes
    - Reword the Invariants row to: "… no `RO` edge spec takes an `object`-typed argument, and every `RO` edge string argument has a `pattern` or `enum` or is in `RO_FREE_TEXT_ARGS` (Property 2); none of the listed ones is forwarded into a pipeline, a command or a persisted configuration (§7 step 5)"
    - Edit in `design.md`: Property 2 (edge clause); the Invariants row "No `RO` tool changes Portal or device state" (907); §7 step 5; §5 `child/catalog.py` (the `RO_FREE_TEXT_ARGS` constant); the Testing Strategy `test_child_catalog_arguments.py` row; the Overview sentence if it repeats the claim
    - Applied in code by tasks 11.4 (`RO_FREE_TEXT_ARGS`) and 11.13 (the clause)
    - Verify: Property 2, the Invariants row, §7 and the Testing Strategy row agree; `RO_FREE_TEXT_ARGS` equals the scan's list of `RO` edge string properties without a pattern or enum; the revision-12 response table records review-11 N5
    - Done in revision 12 (setup step, 2026-10-22): `RO_FREE_TEXT_ARGS` = `('edge_textgen_generate', 'prompt')`, `('edge_textgen_generate', 'image')`, `('edge_results_list', 'text_note_filter')`, `('edge_execution_node_image_get', 'port')` from the scan of the 47 `RO` edge specs
    - _Requirements: 10.1, 8.3_

  - [x] 1.23 [review-11 N6] Correct the line citations and one description in the revision-11 text
    - Fix: in the devices row `portal_devices_deployment_create` and the revision-11 N5 response row, change 1771 → 1772 (`if target_thing_group:`), 1772 → 1773 (the target ARN) and 1778 → 1779 (the handler's 400; 1778 is `else:`), as task 1.16 already cites; rewrite the revision-11 N2 class-scan clause as "`edge_workflow_run` (`workflow_id` matches `{workflow_id}`; no second identifier — `resource_id` is the result's `captureId`/`captureTaskId`)"
    - Edit in `design.md`: the devices row `portal_devices_deployment_create`; the "Response to the design review (revision 11)" rows for review-10 N2 and N5
    - Applied in code: none (citations and response-table text only; task 5.2 transcribes the schema, not the line numbers)
    - Verify: `deployments.py` at `c3bf88c1` has `if target_thing_group:` at 1772, the ARN at 1773 and the 400 at 1779, matching the row and the response row; the revision-12 response table records review-11 N6
    - Done in revision 12 (setup step, 2026-10-22): re-read at `c3bf88c1` (1772 `if target_thing_group:`, 1773 the ARN, 1778 `else:`, 1779 the 400)
    - _Requirements: 5.3, 14.2_

### Phase 1 — MVP (read-only on both servers)

#### Portal

- [x] 2. Mark audit entries and expose the channel filter (shared layer, `audit_logs.py`, audit UI)
  - [x] 2.1 Add the MCP_Audit_Marker and the request audit context to `shared_utils`
    - New `MCP_MARKER_FIELDS = ('channel', 'mcp_tool', 'mcp_server', 'mcp_client', 'mcp_request_id')`; `ATTRIBUTION_FIELDS` unchanged (five fields)
    - `attribution_from(event, user, usecase_id, identity_source)`: with a dict `event['mcp']` it adds the five marker fields (`channel='mcp'`, `'unknown'` for absent values), falls back to `event['mcp']['principal_email']` for `email`, and uses the block's `identity_source` without a registry read; byte-identical output for events without the block
    - D25: `_REQUEST_AUDIT_CONTEXT`, `_remember_request_event(event)` called first in `get_user_from_event` (kept when the stored event has an `mcp` block, the new one has none and `_X_AMZN_TRACE_ID` is unchanged — the `synthetic_data.retrain_session` case), `_current_request_mcp_event()`, and `_attribution_attributes(identity, event, usecase_id)` substituting the remembered event when both are `None`, adding `channel` on every entry and the `mcp_*` attributes only for `channel == 'mcp'`. `log_audit_event`, `record_audit_event_strict`, `finalize_audit_event` signatures unchanged
    - Files: `edge-cv-portal/backend/layers/shared/python/shared_utils.py`; new `edge-cv-portal/backend/tests/mcp/test_mcp_request_audit_context.py` (trace-id binding; in-process synthesized event keeps the mark; a browser-shaped invocation after an MCP one writes `channel='api'`)
    - Verify: `PORTAL_PYTEST tests/test_portal_registry_units.py tests/test_property_audit_attribution.py -q` passes unmodified; `PORTAL_MCP_PYTEST tests/mcp/test_mcp_request_audit_context.py -q` passes
    - _Requirements: 4.1, 4.2, 4.3, 4.5, 6.4_

  - [x] 2.2 Write property test for marker attribution and context binding
    - **Feature: mcp-servers, Property 7: Marker attribution round trip and context binding**
    - File `edge-cv-portal/backend/tests/mcp/test_property_mcp_attribution.py`; Hypothesis, min 100 examples
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_property_mcp_attribution.py -q`
    - **Validates: Requirements 4.1, 4.2, 4.3**

  - [x] 2.3 Add `channel` and `mcp_tool` filters to `GET /audit-logs`
    - `get_audit_logs` appends `(attribute_not_exists(#channel) OR #channel = :channel)` for `channel=api`, `#channel = :channel` for `channel=mcp`, `#mcp_tool = :mcp_tool` for `mcp_tool=<name>` to the existing `filter_expressions` (`audit_logs.py:122–170`); `get_available_actions()` gains `mcp_tool_call`, `authentication_failed`, `authorization_unavailable`. No table or GSI change (F5)
    - Files: `edge-cv-portal/backend/functions/audit_logs.py`; new `edge-cv-portal/backend/tests/mcp/test_audit_logs_channel_filter.py` (moto table with pre-upgrade entries lacking `channel` visible under `api`)
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_audit_logs_channel_filter.py -q`; `PORTAL_PYTEST tests -q -k audit --ignore=tests/mcp` passes
    - _Requirements: 4.6_

  - [x] 2.4 Add the channel selector to the audit-log page
    - A Cloudscape `Select` (All / Web & API / MCP) passing `channel`, and `mcp_tool` shown in the details column
    - Files: `edge-cv-portal/frontend/src/pages/AuditLogs.tsx`; new `edge-cv-portal/frontend/src/pages/AuditLogs.channelFilter.test.tsx`
    - Verify: `cd edge-cv-portal/frontend && npx vitest run src/pages/AuditLogs.channelFilter.test.tsx && npm run build`
    - _Requirements: 4.6_

- [x] 3. Create the portal MCP package foundation, discovery and token validation
  - [x] 3.1 Add the MCP Lambda layer, the package skeleton and the test-time pins
    - `edge-cv-portal/backend/layers/mcp/requirements.txt` = `mcp==2.3.0`, `mangum==0.22.0`; `edge-cv-portal/backend/layers/mcp/build.sh` following `layers/jwt/build.sh`, installing with `pip install --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.11 --implementation cp -r requirements.txt -t python/` (D19)
    - `edge-cv-portal/backend/functions/mcp_portal/__init__.py`: `__version__`, `DELIVERED_PHASES = (1,)`, `PROTOCOL_REVISIONS = ('2026-07-28', '2025-11-25', '2025-06-18', '2025-03-26')`, `VIEWS = ('data', 'models', 'devices', 'workflows', 'admin')`
    - Add `mcp==2.3.0` and `mangum==0.22.0` to `edge-cv-portal/backend/requirements-dev.txt`; leave `functions/requirements.txt` and `layers/jwt/requirements.txt` byte-identical (both are gate-pinned)
    - Verify: `bash edge-cv-portal/backend/layers/mcp/build.sh` completes and `python/` contains `mcp-2.3.0` and `mangum-0.22.0` cp311 manylinux metadata; `git diff --exit-code edge-cv-portal/backend/functions/requirements.txt edge-cv-portal/backend/layers/jwt/requirements.txt`
    - _Requirements: 7.1, 15.3_

  - [x] 3.2 Implement `discovery.py` (root and path-suffixed PRM, RFC 8414 facade)
    - Pure `prm_document(resource_url)`, `authorization_server_document()`, `routes()` (eight `GET` routes, `Cache-Control: public, max-age=3600`), environment strings end to end, import-time validation of `MCP_AS_ISSUER` (https, no path/query/fragment/trailing slash, same origin as `MCP_CANONICAL_URL`) (D27)
    - Files: `edge-cv-portal/backend/functions/mcp_portal/discovery.py`; `edge-cv-portal/backend/tests/mcp/test_mcp_discovery.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_discovery.py -q`
    - _Requirements: 2.4, 7.5_

  - [x] 3.3 Write property test for discovery document consistency
    - **Feature: mcp-servers, Property 17: Discovery document consistency**
    - File `edge-cv-portal/backend/tests/mcp/test_property_mcp_discovery.py`; Hypothesis, min 100 examples
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_property_mcp_discovery.py -q`
    - **Validates: Requirements 2.4, 7.5**

  - [x] 3.4 Implement `CognitoAccessTokenVerifier`
    - `PyJWKClient` on the pool JWKS (`cache_keys=True`), RS256, `iss == COGNITO_ISSUER`, required claims, `token_use == 'access'`, `client_id == MCP_CLIENT_ID`, required scope, `aud` absent or ∩ `MCP_ALLOWED_AUDIENCES`; returns `AccessToken(... claims=<verified payload>)` or `None`; sets `CURRENT_AUTH_FAILURE` with `sub` only when signature and issuer verified and a later check failed; never logs the token
    - Files: `edge-cv-portal/backend/functions/mcp_portal/token_verifier.py`; `edge-cv-portal/backend/tests/mcp/test_mcp_token_verifier.py` (the `test_jwt_authorizer_verification.py` pattern: no token, expired, bad signature, wrong issuer, wrong `aud`, wrong `client_id`, `token_use=id`, missing scope, `?access_token=` ignored)
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_token_verifier.py -q`
    - _Requirements: 2.1, 2.2, 2.3, 2.6, 2.7, 2.8, 16.2_

  - [x] 3.5 Write property test for access-token acceptance
    - **Feature: mcp-servers, Property 1: Access-token acceptance**
    - File `edge-cv-portal/backend/tests/mcp/test_property_mcp_token.py`; Hypothesis, min 100 examples
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_property_mcp_token.py -q`
    - **Validates: Requirements 2.1, 2.2, 2.4, 4.7**

  - [x] 3.6 Implement `AuthFailureAuditMiddleware`
    - Pure ASGI: resets `CURRENT_AUTH_FAILURE`, runs the request, on a 401 with a recorded `sub` writes one marked `authentication_failed` entry (Data Models) from `scope['aws.event']`; swallows and logs audit-write failures
    - Files: `edge-cv-portal/backend/functions/mcp_portal/auth_audit.py`; unit cases on a minimal Starlette app in `edge-cv-portal/backend/tests/mcp/test_mcp_auth_failure_audit.py` (the ASGI-transport and middleware-order cases are added in task 7.2)
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_auth_failure_audit.py -q`
    - _Requirements: 4.7, 2.7_

- [x] 4. Implement the portal catalog machinery
  - [x] 4.1 Implement `ToolSpec`, rendering, listing and the route export in `catalog.py`
    - Every §2 `ToolSpec` field, plus the revision-10 corrections: `accepted_writes` as `(module.function, call, receiver source text, count)` tuples with `STATUS_SYNC_NOTE` (eleven entries at `c3bf88c1`) [review-9 N2], `bind_usecase_path` [review-9 N5], `confirm_target` naming `oneOf` branches [review-9 N7]; and the revision-11 corrections: `usecase_not_forwarded: Literal['stored_record', 'injected', 'dead_filter'] | None` [review-10 N1], and a definition-time error when two bare identifiers would claim one path parameter [review-10 N2]
    - `SERVER_CONSUMED_ARGS = ('confirm', 'usecase_id', 'limit', 'offset')` with the rule "forwarded iff `param_map` names it"; schema generation for `limit` (`limit_max`/`limit_default`), `offset`, `page`, `next_token`, and the path-parameter identifier pattern `^(?!\.{1,2}$)[A-Za-z0-9._:@+=-]{1,256}$` on every argument mapped to a path parameter
    - `render_description`/`classification_text` (D35), `to_mcp_tool` with annotations (`open_world_hint=False`), `tools_for_view(view, delivered_phases, mutating_enabled)` sorted by `(domain, name)` with alias entries (D35, D8 — not filtered by principal), `catalog_routes()`, and `python -m mcp_portal.catalog --export-routes` writing `catalog_routes.json` and `catalog_tools.json`
    - Files: `edge-cv-portal/backend/functions/mcp_portal/catalog.py`; new `edge-cv-portal/backend/tests/mcp/test_mcp_catalog_core.py` (rendering, alias entries, `confirm` branch rendering, schema generation on synthetic specs)
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_catalog_core.py -q`
    - _Requirements: 1.2, 1.7, 3.8, 5.1, 5.3, 5.4, 14.1, 14.2, 14.4, 17.3_

  - [x] 4.2 Write property test for tool list determinism and filtering
    - **Feature: mcp-servers, Property 3: Tool list determinism and filtering**
    - File `edge-cv-portal/backend/tests/mcp/test_property_mcp_tool_list.py`; Hypothesis over views, delivered-phase sets and switch values, min 100 examples
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_property_mcp_tool_list.py -q`
    - **Validates: Requirements 1.7, 5.4, 10.7, 14.4, 17.3**

- [x] 5. Author every portal catalog row and the catalog honesty suites (revision-11 tables, all phases; only Phase 1 delivered)
  - [x] 5.1 Author the `admin` view rows
    - All admin rows from `usecases.py`, `user_management.py`, `user_admin.py`, `audit_logs.py` plus `portal_admin_whoami` (`server.whoami`, the self-identity exception); `'global'` refused on the two role rows; account-lifecycle tools absent (Backlog 2); credential-handling routes excluded (5.5)
    - Carries [review-9 M1] `portal_admin_users_list` `usecase_id? (→ query usecase_id)` and `portal_admin_usecase_delete` `(→ path id)`; [review-9 M2] `portal_admin_audit_logs_query` `limit (→ query limit)`; [review-9 N4] `offset` on `portal_admin_usecases_list`; [review-10 N1] `usecase_not_forwarded='dead_filter'` on `portal_admin_audit_logs_query`
    - Files: `edge-cv-portal/backend/functions/mcp_portal/catalog.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_catalog_core.py tests/mcp/test_property_mcp_tool_list.py -q`; the `admin` view lists exactly the table's Phase-1 tools
    - _Requirements: 1.1, 3.1, 3.7, 5.5, 5.6_

  - [x] 5.2 Author the `devices` view rows
    - Rows from `devices.py`, `device_logs.py`, `device_logs_analyzer.py`, `camera_registry.py`, `camera_video_pin.py` (static-video specs name `camera_registry` functions with backend `cameraVideoPin`), `deployments.py`, `components.py`, `component_configuration.py`, `shared_components.py`, `device_registrations.py`; component create/discover/configure and the static pins absent (Backlog 21–23)
    - Carries [review-9 M1] `(→ query usecase_id)` on `portal_devices_list`, `_get`, `_deployments_list`, `_shared_components_list`; [review-9 N4] `offset` on `portal_devices_list`, `_deployments_list`; [review-9 N7] `portal_devices_deployment_create` `confirm_target = 'target_thing_group' | 'target_devices[0]'`, with `target_devices` declared `minItems: 1, maxItems: 1` and `target_thing_group` carrying `THING_NAME_PATTERN` (revision 11's extension) [review-10 N5]; [review-10 N2] `camera_source_id? (→ body camera_source_id)` on `portal_devices_camera_add`
    - Files: `edge-cv-portal/backend/functions/mcp_portal/catalog.py`
    - Verify: as 5.1 for the `devices` view
    - _Requirements: 1.1, 1.4, 3.1, 5.2, 5.5, 5.6, 5.7_

  - [x] 5.3 Author the `models` view rows
    - Rows from `models.py`, `model_import.py`, `model_converter.py`, `training.py`, `compilation.py`, `packaging.py`, `greengrass_publish.py`; `portal_models_delete` **absent** while `models.py:522` checks `'Admin'` (Backlog 1); `portal_models_compilation_create` schema without `auto_triggered` (Backlog 20); the status-sync `accepted_writes` of `_get`, `_training_get`, `_compilation_get` in the [review-9 N2] form
    - Files: `edge-cv-portal/backend/functions/mcp_portal/catalog.py`
    - Verify: as 5.1 for the `models` view
    - _Requirements: 1.1, 1.4, 3.1, 5.2, 5.6, 6.2_

  - [x] 5.4 Author the `data` view rows
    - Rows from `datasets.py`, `pre_labeled_datasets.py`, `data_management.py`, `captures.py`, `labeling.py`, `dda_labeling.py`, `synthetic_data.py`; `setdefault`-scoped job/team/rerun specs keep `usecase_id` RBAC-only (Backlog 27); `portal_data_bucket_attach` absent (Backlog 24)
    - Carries [review-9 M1] `(→ query usecase_id)` on `portal_data_datasets_list`, `_datasets_preview_get`, `_prelabeled_list` and `(→ body usecase_id)` on `_datasets_count`, `_manifest_validate`; [review-9 M2] `limit (→ query limit)` on `_datasets_preview_get` and `_captures_list`, `offset (→ query offset)` on `_datasets_preview_get`; [review-9 N5] `bind_usecase_path='job.usecase_id'` on `portal_data_labeling_job_get` (the full name, [review-10 N6]) and **no** `portal_data_labeling_manifest_get` spec; [review-10 N1] `usecase_not_forwarded='injected'` on the ten `setdefault`-scoped specs and `'stored_record'` on `portal_data_synthetic_session_retrain`
    - Files: `edge-cv-portal/backend/functions/mcp_portal/catalog.py`
    - Verify: as 5.1 for the `data` view
    - _Requirements: 1.1, 1.6, 3.1, 3.3, 3.6, 5.6_

  - [x] 5.5 Author the `workflows` view rows
    - Rows from `workflows.py`, `workflow_validation.py` (`portal_workflows_validate` is `M`, Phase 2), `workflow_packaging.py`, `workflow_testing.py`, `workflow_generator.py`, the node-designer handlers, `build_jobs.py`, `build_fleet.py`, `build_config.py`; `portal_workflows_test_dataset_create` absent (presigned multipart)
    - Carries [review-9 M1] `(→ query usecase_id)` on `portal_workflows_node_catalog_get` and `(→ body usecase_id)` on `portal_workflows_generate`; [review-9 M2] `limit (→ query limit)` on `portal_workflows_builds_list` and `_build_logs_get`; `portal_workflows_build_server_stop` and `_delete` are `M!` (the class label task 1.21 rewrites as `_stop`/`_delete` [review-11 N4])
    - Files: `edge-cv-portal/backend/functions/mcp_portal/catalog.py`
    - Verify: as 5.1 for the `workflows` view; the aggregate view lists 87 Phase-1 tools
    - _Requirements: 1.1, 1.4, 3.1, 3.6, 5.6, 5.7_

  - [x] 5.6 Export the catalog routes and pin them (pytest half of the route test)
    - Run `python -m mcp_portal.catalog --export-routes`; check in `catalog_routes.json` and `catalog_tools.json`; `test_mcp_catalog_routes_export.py` pins both byte-for-byte and adds the handler-dispatch half (entry point → `handler_function` on a branch matching method and resource, followed through shims; `deployments.handler` discriminator case; `POST /components` and `/components/discover` absent)
    - Files: `edge-cv-portal/backend/functions/mcp_portal/catalog_routes.json`, `edge-cv-portal/backend/functions/mcp_portal/catalog_tools.json`; `edge-cv-portal/backend/tests/mcp/test_mcp_catalog_routes_export.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_catalog_routes_export.py -q`
    - _Requirements: 1.1, 1.3, 3.1_

  - [x] 5.7 Write property test for portal catalog well-formedness
    - **Feature: mcp-servers, Property 2: Catalog well-formedness**
    - File `edge-cv-portal/backend/tests/mcp/test_property_mcp_catalog.py`: every non-AST clause of Property 2 over the portal `CATALOG`, including the revision-10 clauses — cursor ⇒ forwarded `limit` [review-9 M2], dispatcher-sliced collection ⇒ `offset` [review-9 N4], `confirm_target` may name `oneOf` branches [review-9 N7], `bind_usecase_path` well-formed [review-9 N5]; and the revision-11 clauses: `usecase_not_forwarded` set only where `usecase_id` has no `param_map` entry [review-10 N1], `minItems == maxItems == 1` on any array a per-branch `confirm_target` indexes, and a `pattern` excluding `""` on every per-branch target string [review-10 N5]; Hypothesis over spec selection, min 100 examples
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_property_mcp_catalog.py -q`
    - **Validates: Requirements 1.2, 1.3, 3.1, 3.9, 5.1, 5.2, 5.6, 14.1, 14.2, 14.4, 17.1**

  - [x] 5.8 Write the portal classification honesty test
    - AST walk of `test_mcp_catalog_classification.py` per the Testing Strategy, with [review-9 N2]: `accepted_writes` matched on `(module.function, call, receiver source text)` + occurrence count, and `SHARED_LAYER_WRITERS` derived from `layers/shared/python/*.py` (minus `log_audit_event`) with the containment assertion (fourteen writers at `c3bf88c1`, including the seven revision-9 named and the five `user_roles_dao` writers and `s3_cors.ensure_bucket_cors`); fixed cases (`workflow_validation.validate_workflow` not `RO`; the eight audited reads and ten status-sync tools `RO`; `camera_registry.create_response` via module object not a write)
    - Files: `edge-cv-portal/backend/tests/mcp/test_mcp_catalog_classification.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_catalog_classification.py -q`; removing one `accepted_writes` entry makes it fail naming the tool and verb
    - _Requirements: 5.1, 5.2, 1.2_

  - [x] 5.9 Write the `handler_audits` honesty test
    - AST classification of audit calls by `result` over the call graph, `PINNED_AUDIT_FLAGS`, and every fixed regression case of the Testing Strategy row (camera reads, `list_deployments`, `list_devices`, `get_camera_binding_context`, `_fail_conversion`, revision-9 cases)
    - Files: `edge-cv-portal/backend/tests/mcp/test_mcp_handler_audit_flags.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_handler_audit_flags.py -q`
    - _Requirements: 4.1, 4.3, 16.4_

  - [x] 5.10 Write the argument ↔ route-parameter test
    - AST read collector per the Testing Strategy row (reads, `400 … required` guards without reassignment, `validate_required_fields`, `anyOf` membership, `limit` maximum), plus [review-9 N3] folding of string constants (module-level, same-module or imported; `Name` and `module.CONST`) and tuple/list/set constants (module-level or function-local) iterated against `body`/`params` in loops and comprehensions, `getattr(<module>, '<literal>')` delegation, and path-mapped targets checked against the `{name}` segments of `resource`; [review-9 M2] the cursor/`limit` rule; and [review-10 N1] the converse `usecase_id` clause skipping specs that set `usecase_not_forwarded`
    - Fixed cases: every case of the Testing Strategy row; the thirteen [review-9 M1] rows; the five [review-9 M2] rows with `build_jobs.get_build_logs` as the failing variant; the four [review-9 N3] rows plus the revision-10 additions (`update_usecase`'s `updatable_fields`, `portal_data_labeling_job_create`'s DDA branch, `usecases.handler`'s positional path argument); the three [review-9 N4] `offset` rows; `setdefault`-scoped specs have no `param_map['usecase_id']`; [review-10 N1] `portal_admin_audit_logs_query` (`dead_filter`) and `portal_data_synthetic_session_retrain` (`stored_record`)
    - Files: `edge-cv-portal/backend/tests/mcp/test_mcp_catalog_arguments.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_catalog_arguments.py -q`
    - _Requirements: 1.3, 1.6, 5.6_

  - [x] 5.11 Write the catalog ⊆ handler permission test
    - Table-driven subset check with every fixed case of the Testing Strategy row (each former permission as the failing variant; `portal_models_delete` absent)
    - Files: `edge-cv-portal/backend/tests/mcp/test_mcp_catalog_permissions.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_catalog_permissions.py -q`
    - _Requirements: 1.3, 3.1, 3.5, 3.7, 3.9_

- [ ] 6. Implement the portal request pipeline
  - [x] 6.1 Implement `principal.py` and `authz.py`
    - `principal_from_access_token` (email via per-container LRU over `AdminGetUser`, reading only `email` — D29 option (A)); `authorize(spec, principal, usecase_id)` through `shared_utils.rbac_manager.has_permission` per declared permission with the resolved scope, returning `identity_source`; marked `unauthorized_access` / `authorization_unavailable` entries with `rbac_check`'s details plus `mcp_tool`
    - Files: `edge-cv-portal/backend/functions/mcp_portal/principal.py`, `edge-cv-portal/backend/functions/mcp_portal/authz.py`; `edge-cv-portal/backend/tests/mcp/test_mcp_authz.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_authz.py -q`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.9, 4.7_

  - [x] 6.2 Write property test for the portal authorization decision
    - **Feature: mcp-servers, Property 4: Portal authorization decision**
    - File `edge-cv-portal/backend/tests/mcp/test_property_mcp_authz.py`; moto registry, real resolver as oracle, enforcement on/off, the D29 denials recorded; Hypothesis, min 100 examples
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_property_mcp_authz.py -q`
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.7, 16.3**

  - [x] 6.3 Implement `dispatch.py`
    - `build_backend_event` (REST proxy v1 shape, `claims` = the verified payload object, `event['mcp']` block, percent-safe path parameters), `invoke` with `LAMBDA` (`connect_timeout=2`, `read_timeout=20`, `total_max_attempts=1`), the `MIN_DISPATCH_BUDGET_MS = 24_000` guard on `LAMBDA_CONTEXT`, lazy SSM dispatch map with the single `ResourceNotFoundException` refresh (D30, D32)
    - Files: `edge-cv-portal/backend/functions/mcp_portal/dispatch.py`; `edge-cv-portal/backend/tests/mcp/test_mcp_dispatch.py` (every case of the Testing Strategy row)
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_dispatch.py -q`
    - _Requirements: 1.3, 1.4, 3.9, 6.2, 2.7_

  - [x] 6.4 Write property test for dispatch event faithfulness
    - **Feature: mcp-servers, Property 5: Dispatch event faithfulness**
    - File `edge-cv-portal/backend/tests/mcp/test_property_mcp_dispatch_event.py`; Hypothesis over specs and schema-valid arguments, min 100 examples
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_property_mcp_dispatch_event.py -q`
    - **Validates: Requirements 1.3, 2.7, 3.9, 4.2**

  - [x] 6.5 Implement `errors.py` and `shaping.py`
    - `ToolFailure`, `effect_unknown_message` (names `status_tool`), `map_backend_status` with the traceback/bearer scrub; collection shaping with dispatcher `limit`/`offset` slicing [review-9 N4], forwarded pagination never cut (`result_too_large` naming a smaller `limit`), `MAX_RESULT_BYTES = 200_000`, `resource_id` / `resource_id_path` extraction, and the [review-9 N5] `bind_usecase_path` check (mismatch or absent value → body dropped, `not_found`); [review-10 N3] a backend 404 on a `bind_usecase_path` spec maps to the same `ToolFailure('not_found', 'no such object in the named Use_Case')`, both outcomes with `status=404` and the real backend status only in the `mcp_tool` line and `details.status` [review-11 N2, task 1.14]
    - Files: `edge-cv-portal/backend/functions/mcp_portal/errors.py`, `edge-cv-portal/backend/functions/mcp_portal/shaping.py`; `edge-cv-portal/backend/tests/mcp/test_mcp_shaping.py` (incl. the `usecase_binding` mismatch and absent-value cases on `portal_data_labeling_job_get`, and its backend-404 case)
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_shaping.py -q`
    - _Requirements: 1.4, 1.5, 1.6, 3.3, 2.7_

  - [x] 6.6 Write property test for backend status mapping (portal)
    - **Feature: mcp-servers, Property 8: Backend status mapping and scrubbing**
    - File `edge-cv-portal/backend/tests/mcp/test_property_mcp_errors.py`; incl. the `bind_usecase_path` clause: a backend 404 and the D36 refusal give the same whole `CallToolResult` (`content` and `structured_content`) except `request_id` [review-10 N3; review-11 N2, task 1.14]; Hypothesis, min 100 examples
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_property_mcp_errors.py -q`
    - **Validates: Requirements 1.5, 2.7**

  - [x] 6.7 Write property test for result bounding (portal clauses)
    - **Feature: mcp-servers, Property 9: Result bounding**
    - File `edge-cv-portal/backend/tests/mcp/test_property_mcp_result_bounding.py`: collection, `limit`/`offset`, contiguous slice, `MAX_RESULT_BYTES`, `truncated` iff dropped; Hypothesis, min 100 examples
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_property_mcp_result_bounding.py -q`
    - **Validates: Requirements 1.6**

  - [x] 6.8 Implement the audit envelope and the per-invocation log line
    - `audit.write_envelope` with the D6/N8 rules and the `max(k, 1)` count contract, the `PINNED_AUDIT_FLAGS` exception, and the [review-9 N5] `result='denied'`, `details.reason='usecase_binding'` envelope, written even when `handler_audits` is True [review-10 N4]; the 16.4 test asserts that the missing-id and other-Use_Case results of `portal_data_labeling_job_get` are identical [review-10 N3], comparing the whole `CallToolResult` except `request_id`, not only category and message [review-11 N2, task 1.14]; `telemetry.log_tool_completion` — exactly one JSON line per completion, no arguments, token or email
    - Files: `edge-cv-portal/backend/functions/mcp_portal/audit.py`, `edge-cv-portal/backend/functions/mcp_portal/telemetry.py`; `edge-cv-portal/backend/tests/mcp/test_mcp_audit_envelope.py` (real `get_user_from_event`/`log_audit_event` in a fake handler under `_X_AMZN_TRACE_ID`; `record_audit_event_strict` failure aborts the effect → `backend_failure`)
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_audit_envelope.py -q`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 6.3, 16.4_

  - [ ] 6.9 Write property test for marked audit entries per completion
    - **Feature: mcp-servers, Property 6: Marked audit entries per completion**
    - File `edge-cv-portal/backend/tests/mcp/test_property_mcp_audit.py`; Hypothesis over outcomes (incl. the D36 refusal, total `k + 1` [review-10 N4]), flags and `k ∈ {0, 1, 2}`, min 100 examples
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_property_mcp_audit.py -q`
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.5, 16.4**

  - [ ] 6.10 Implement `server.py` and `handler.py` (per-invocation ASGI composition)
    - `build_server(view)` (low-level `Server`, `cache_hints` `ttl_ms=300_000`, `scope='private'`, the nine-step `on_call_tool` pipeline of §4 (step 9 shapes every status < 300 unless step 7 refused the body [review-10 N4]) incl. `client_label(ctx)`, alias lookup, phase gate → `-32602`, switch gate → `mutating_tools_disabled`); `handler.build_asgi_app()` with six fresh `StreamableHTTPSessionManager`s (`json_response=True`, `stateless=True`, explicit `TransportSecuritySettings` with `MCP_ALLOWED_ORIGINS`), six exact `POST` routes under `RequireAuthMiddleware`, `discovery.routes()`, middleware order `[AuthFailureAuditMiddleware, AuthenticationMiddleware, AuthContextMiddleware]`, `AsyncExitStack` lifespan; `handler(event, context)` sets `dispatch.LAMBDA_CONTEXT` and runs `Mangum(..., lifespan='auto')`
    - Files: `edge-cv-portal/backend/functions/mcp_portal/server.py`, `edge-cv-portal/backend/functions/mcp_portal/handler.py`; `edge-cv-portal/backend/tests/mcp/test_mcp_lambda_handler.py` (three invocations in one process, no `RuntimeError`, module objects reused)
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_lambda_handler.py -q`
    - _Requirements: 7.1, 7.2, 7.3, 1.7, 15.3, 6.2_

- [ ] 7. Write the portal protocol, transport, authorization and guardrail suites
  - [ ] 7.1 Write the in-memory protocol test
    - `tests/mcp/_principal.py:set_principal` (test-only, sets `auth_context_var`); `test_mcp_protocol.py` per the Testing Strategy row: `server/discover`, deterministic `tools/list` with `ttlMs`/`cacheScope`, rendered descriptions and the alias entry, one RO call (`portal_devices_list`), one M call (`portal_devices_deployment_create` with `MCP_MUTATING_TOOLS_ENABLED=true`, `DELIVERED_PHASES` patched, and `confirm` equal to the thing-group name — [review-9 N7]), error shapes incl. unknown tool `-32602`
    - Files: `edge-cv-portal/backend/tests/mcp/_principal.py`, `edge-cv-portal/backend/tests/mcp/test_mcp_protocol.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_protocol.py -q`
    - _Requirements: 16.1, 1.2, 1.5, 1.7, 5.1, 5.3, 14.4, 15.3_

  - [ ] 7.2 Write the Streamable HTTP transport and auth-failure audit tests
    - `test_mcp_http_transport.py` over `httpx2.ASGITransport(app=build_asgi_app())`: PRM documents, facade, 405 on discovery `OPTIONS`/`POST`, 401 challenge per endpoint (SDK behaviour upstream of the gateway), 403 `insufficient_scope`, `Origin` → 403, foreign `Host` → 421, `MCP-Protocol-Version`, `GET /mcp` → 405, no `Mcp-Session-Id`, no redirects; token in a query parameter → 401
    - Extend `test_mcp_auth_failure_audit.py`: marked `authentication_failed` entry for expired / wrong `client_id` / wrong scope, none for a bad signature, and `test_middleware_order`
    - Files: `edge-cv-portal/backend/tests/mcp/test_mcp_http_transport.py`, `edge-cv-portal/backend/tests/mcp/test_mcp_auth_failure_audit.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_http_transport.py tests/mcp/test_mcp_auth_failure_audit.py -q`
    - _Requirements: 2.2, 2.4, 7.3, 7.5, 4.7, 16.1, 16.2_

  - [ ] 7.3 Write the legacy-revision client test
    - `Client(..., mode="legacy")` over the ASGI transport: `initialize`, `tools/list`, one `call_tool`; an unknown version header → `UnsupportedProtocolVersionError`
    - Files: `edge-cv-portal/backend/tests/mcp/test_mcp_legacy_client.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_legacy_client.py -q`
    - _Requirements: 7.2, 16.9_

  - [ ] 7.4 Write the RBAC matrix test
    - Every catalog tool × six roles × `PORTAL_REGISTRY_ENFORCED` on/off, registry seeding as the Testing Strategy row states, expectations from the real resolver cross-checked against the per-row role mapping, marked `unauthorized_access` entries, registry outage → `authorization_unavailable`, whoami for every role, the `documented-limitation-D29` denials, and the usability guard
    - Files: `edge-cv-portal/backend/tests/mcp/test_mcp_rbac_matrix.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_rbac_matrix.py -q`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 16.3_

  - [ ] 7.5 Write the confirmation and mutating-switch test
    - Every `M!` tool rejects a missing or mismatching `confirm` (incl. both `oneOf` branches of `portal_devices_deployment_create`, the other branch's value and the former `"deploy"` — [review-9 N7] — and `target_devices: []` and `target_thing_group: ""` as `invalid_input` before the comparison [review-10 N5]); with the switch off every `M` tool is absent from `tools/list` and refused
    - Files: `edge-cv-portal/backend/tests/mcp/test_mcp_guardrails.py`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_mcp_guardrails.py -q`
    - _Requirements: 5.3, 5.4, 16.5_

- [ ] 8. Implement the portal infrastructure (`EdgeCVPortalMcpStack`) and the opt-out guard
  - [x] 8.1 Add the context helpers and the two additive members of existing stacks
    - `mcpServerEnabled(contextValue)` (default on; only a trimmed, lower-cased `false` is off) and `mcpMutatingToolsEnabled(contextValue)` (default `'false'`) in `context-helpers.ts`; `ComputeStack.mcpBackendHandlers` (the 32 keys) and `ApiGatewayStack.componentConfigurationHandler`; no public layer member
    - Files: `edge-cv-portal/infrastructure/lib/context-helpers.ts`, `edge-cv-portal/infrastructure/lib/compute-stack.ts`, `edge-cv-portal/infrastructure/lib/api-gateway-stack.ts`; extend `edge-cv-portal/infrastructure/test/context-helpers.property.test.ts` (fast-check over arbitrary context strings)
    - Verify: `CDK_TEST test/context-helpers.property.test.ts`; `python3 -m pytest test/backend-test/security/preservation/test_preservation_iam_cdk_synth.py -p no:cacheprovider --noconftest -q` (ComputeStack IAM grants unchanged)
    - _Requirements: 6.4, 6.5, 5.4_

  - [x] 8.2 Implement `mcp-stack.ts` and wire it in `bin/app.ts`
    - Every construct of design §1: `McpHttpApi`, `McpDefaultStage` (20/40; per-route 10/20 override), `McpResourceServer` and `McpUserPoolClient` in this stack's scope (`authFlows: { userSrp: false }`, code grant only, callback `http://localhost:7899/oauth/callback` + validated `mcpRedirectUris`), `McpSharedLayer` (own copy, D31), `McpLayer`, the Advanced-tier SSM parameter (D30), `McpPortalRole` with exactly six inline statements, `McpPortalHandler` (29 s, 512 MB), log group + `"Task timed out"` metric filter + alarm, the ten routes with `HttpNoneAuthorizer`, the Lambda environment (no `MCP_BACKEND_FUNCTIONS`, `MCP_ALLOWED_ORIGINS` empty by default), and the seven outputs; `bin/app.ts` instantiates it after the four producing stacks behind `mcpServerEnabled`, passing `authStack.userPoolDomain!` with the comment
    - Files: new `edge-cv-portal/infrastructure/lib/mcp-stack.ts`; `edge-cv-portal/infrastructure/bin/app.ts`
    - Verify: `cd edge-cv-portal/infrastructure && npm run build && npx cdk synth EdgeCVPortalMcpStack` with the deploy context succeeds; `npx cdk synth -c deployMcpServer=false` emits no MCP stack
    - _Requirements: 2.4, 5.4, 6.1, 6.3, 6.5, 7.3, 2.3_

  - [ ] 8.3 Write the CDK stack assertions
    - `mcp-stack.test.ts`: every assertion of the design's "Portal infrastructure" section (exact ten route keys, no `$default` route, `AuthorizationType: NONE` + one integration, no authorizer resource, throttles, environment keys and < 3,000 bytes worst-case, SSM parameter with 43 imports, six-statement role with no `*` and < 9,000 characters, two layers and no imported layer, timeout 29, log group/metric filter/alarm and no `Custom::LogRetention`, `ExplicitAuthFlows: ['ALLOW_REFRESH_TOKEN_AUTH']` and the not-SRP assertion, callback URL pattern, client and resource server in this stack, `deployMcpServer=false`, the `mcpMutatingToolsEnabled` property test) plus the `mcpBackendHandlers`/`componentConfigurationHandler` snapshot and the export-is-a-function-ARN assertion
    - Files: new `edge-cv-portal/infrastructure/test/mcp-stack.test.ts`
    - Verify: `CDK_TEST test/mcp-stack.test.ts`
    - _Requirements: 16.6, 6.1, 6.5, 2.3, 5.4_

  - [ ] 8.4 Write the jest half of the catalog-route registration test
    - Synthesizes `ComputeStack` and its nested API stacks by construct id plus `NodeDesignerApi`, `BuildFleetStack`, `SyntheticDataStack`; generalized walker with the imported-parent prefix table; every `catalog_routes.json` entry is a registered method; account-lifecycle routes absent
    - Files: new `edge-cv-portal/infrastructure/test/mcp-catalog-routes.test.ts`
    - Verify: `CDK_TEST test/mcp-catalog-routes.test.ts`
    - _Requirements: 1.3, 16.6, 3.1_

  - [ ] 8.5 Add the two-step opt-out guard
    - `portal_mcp_optout_guard "$AWS_REGION" "$DEPLOY_MCP_SERVER"` (off iff trimmed lower-cased `false`; aborts with the `npx cdk destroy EdgeCVPortalMcpStack --force` instruction while the stack exists; aborts on any other `describe-stacks` failure; appends `-c deployMcpServer=false` only when off), sourced from `deploy-infrastructure.sh` directly before the `cdk deploy` line; `deploy-portal.sh` unchanged (D33)
    - Files: new `edge-cv-portal/scripts/portal-mcp-stack.sh`; `edge-cv-portal/deploy-infrastructure.sh`; new `edge-cv-portal/tests/mcp_stack_optout.bats` (cases 1–5 of the design)
    - Verify: `cd edge-cv-portal/tests && bats mcp_stack_optout.bats && bats .`
    - _Requirements: 6.4, 16.6_

- [ ] 9. Checkpoint — portal Phase 1 suites green
  - Run `PORTAL_MCP_PYTEST tests/mcp -q`; `PORTAL_PYTEST tests -q --ignore=tests/mcp` (the shared-layer change touches every function); `CDK_TEST` (whole jest suite) and `npx tsc --noEmit`; `cd edge-cv-portal/tests && bats .`; `cd edge-cv-portal/frontend && npx vitest run && npm run build`; `python3 -m pytest test/backend-test/security/preservation -p no:cacheprovider --noconftest -q -k "iam or secrets_jwt"`
  - Ensure all tests pass; ask the user if questions arise
  - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.9, 6.4_

#### Edge

- [x] 10. Implement the Backend_API changes (backend process; no `mcp` import)
  - [x] 10.1 Share one IPC component-configuration reader
    - New public `local_auth.config.read_component_config_via_ipc() -> dict` (today's `_read_local_login_enabled_via_ipc` body up to `get_component_config`, guarded by a module-level `threading.Lock`, `{}` for non-dict results); `_read_local_login_enabled_via_ipc` delegates to it (behaviour-preserving); one `DefectDetectionConfig` instance and one IPC connection per process (M4)
    - Files: `src/backend/local_auth/config.py`; new `test/backend-test/mcp_edge/test_shared_ipc_reader.py`
    - Verify: `EDGE_CONTAINER_PYTEST test/backend-test/mcp_edge/test_shared_ipc_reader.py test/backend-test/local_auth` passes (existing `local_auth` tests unmodified)
    - _Requirements: 12.2, 12.3_

  - [x] 10.2 Return an `AuthDecision` from the authorizer without changing the matrix
    - `AuthDecision(source, username, role, expires_at)`; `_validate_local_session_token` returns the `Account` or `None`; `_session_token_expiry`; `validate_token` keeps signature, raises and `None` return, its body moved verbatim into `_introspect_token` (returns the introspection JSON); `authorize_credential` returns `None` (open access) or an `AuthDecision`; `authorize_request` stores `request.state.auth_decision` (anonymous default)
    - Modify `test/backend-test/api-endpoints/test_authorize_request.py` lines 424/430 only (`isinstance(result, AuthDecision)`, `source`), line 420 and every raise assertion byte-identical; leave `test/backend-test/utils/test_auth.py` byte-identical (gate-listed and source-pinned by `test_preservation_secrets_auth_suite.py`)
    - Files: `src/backend/utils/auth.py`; `test/backend-test/api-endpoints/test_authorize_request.py`
    - Verify: `EDGE_CONTAINER_PYTEST test/backend-test/api-endpoints/test_authorize_request.py test/backend-test/utils/test_auth.py test/backend-test/security/preservation/test_preservation_secrets_auth_suite.py`; `git diff --exit-code test/backend-test/utils/test_auth.py`
    - _Requirements: 9.1, 9.8, 12.7_

  - [x] 10.3 Write property test for the unchanged decision matrix
    - **Feature: mcp-servers, Property 14: Unchanged matrix after `AuthDecision`**
    - File `test/backend-test/mcp_edge/test_auth_decision.py` (the `configured(...)` pattern of `test_authorize_request.py`); Hypothesis, min 100 examples
    - Verify: `EDGE_CONTAINER_PYTEST test/backend-test/mcp_edge/test_auth_decision.py`
    - **Validates: Requirements 9.1, 12.7**

  - [x] 10.4 Add `GET /local-auth/whoami` on a new authenticated router
    - `authenticated_router = get_api_router()` in `endpoints/local_auth.py` returning `{username, role, source, expiresAt?}` from `request.state.auth_decision`; `/local-auth/login` and `/local-auth/status` stay on the unauthenticated router unchanged (F8)
    - Files: `src/backend/endpoints/local_auth.py`; new `test/backend-test/mcp_edge/test_whoami_route.py` (each matrix row, `expiresAt`, Existing_Token_Auth username from a faked introspection)
    - Verify: `EDGE_CONTAINER_PYTEST test/backend-test/mcp_edge/test_whoami_route.py test/backend-test/local_auth/test_local_auth_endpoints.py`
    - _Requirements: 9.1, 9.3, 10.4, 12.7_

  - [x] 10.5 Implement the backend-side `mcp_edge` modules
    - `mcp_edge/__init__.py` (`__version__`, `DELIVERED_PHASES = (1,)`, `VENV_PYTHON`, `CHILD_PORT = 5790`); `config.py` (`McpConfig`: `McpServerEnabled`, `McpMutatingToolsEnabled` via `parse_local_login_enabled`, optional `McpAllowedOrigins` CSV, 30 s poller, read failure ⇒ both disabled, `on_change`); `supervisor.py` (spawn argv/env/cwd of §5 incl. `--server-version <catalog>+<component>`, readiness on `/healthz` within 20 s, backoff 1→60 s, restart on mutating/origins/features change, `stop()` SIGTERM→SIGKILL, never raises); `log_relay.py` (stdout JSON → `mcp.access`, stderr → `mcp.child` WARNING with the `token=` line drop, exceptions swallowed); `status.py` (`build_status`)
    - Files: new `src/backend/mcp_edge/__init__.py`, `src/backend/mcp_edge/config.py`, `src/backend/mcp_edge/supervisor.py`, `src/backend/mcp_edge/log_relay.py`, `src/backend/mcp_edge/status.py`; new `test/backend-test/mcp_edge/test_supervisor.py`, `test/backend-test/mcp_edge/test_log_relay.py`
    - Verify: `EDGE_CONTAINER_PYTEST test/backend-test/mcp_edge/test_supervisor.py test/backend-test/mcp_edge/test_log_relay.py`
    - _Requirements: 10.7, 11.4, 12.1, 12.2, 12.3, 12.8, 15.3_

  - [x] 10.6 Write property test for edge configuration parsing and transitions
    - **Feature: mcp-servers, Property 11: Edge configuration parsing and transitions**
    - File `test/backend-test/mcp_edge/test_config.py`; injected `read_component_config_via_ipc`; Hypothesis, min 100 examples
    - Verify: `EDGE_CONTAINER_PYTEST test/backend-test/mcp_edge/test_config.py`
    - **Validates: Requirements 12.1, 12.2, 10.7**

  - [x] 10.7 Implement the `/mcp` proxy route and wire the supervisor into `app.py`
    - `mcp_edge/proxy.py` (`forward`: header allowlist without `Origin`, `X-Request-ID` injection, `httpx` 0.24.1 `Client` with `HTTPTransport(retries=0)`, `stream=True` send, buffered relay ≤ 4 MiB → `502 mcp_bad_upstream`, read timeout → `504 mcp_timeout`, refused → `503 mcp_unavailable`, `MCP_PROXY_UPSTREAM_READ_S = 150.0`); `endpoints/mcp_proxy.py` on `get_api_router()` (sync `POST /mcp` with the async `_mcp_body` dependency and 1 MiB 413, Origin rule, D14 exposure rule, 503 when disabled/not ready; `GET /mcp/status`)
    - `app.py`: include `mcp_proxy.router` and `local_auth.authenticated_router`; start the supervisor after `health.set_vllm_server(vllm_server)` in its own try/except with `component_version=system.get_local_server_component_version()` and `features={'vllm'}` only when `VLLM_AVAILABLE`; `mcp_supervisor.stop()` in `finally`. No change to ports, TLS selection, CORS or other routers
    - Files: new `src/backend/mcp_edge/proxy.py`, `src/backend/endpoints/mcp_proxy.py`; `src/backend/app.py`; new `test/backend-test/mcp_edge/test_proxy_route.py` (real `authorize_request` per matrix row incl. disabled account and `?token=` at `/mcp` → 401; `Origin` → 403 never forwarded; non-loopback on plaintext → 403; stalled upstream → 504; > 4 MiB → 502)
    - Verify: `EDGE_CONTAINER_PYTEST test/backend-test/mcp_edge/test_proxy_route.py test/backend-test/security/preservation/test_preservation_secrets_app_and_components.py` (the `uvicorn.Config(...)` bind calls are unchanged)
    - _Requirements: 9.1, 9.4, 9.5, 11.3, 12.3, 12.4, 12.5, 12.7, 12.8, 16.2_

  - [x] 10.8 Write property test for the proxy exposure rule
    - **Feature: mcp-servers, Property 12: Proxy exposure rule**
    - File `test/backend-test/mcp_edge/test_property_proxy_exposure.py` (the pure decision function of `endpoints/mcp_proxy.py`); Hypothesis, min 100 examples
    - Verify: `EDGE_CONTAINER_PYTEST test/backend-test/mcp_edge/test_property_proxy_exposure.py`
    - **Validates: Requirements 12.4, 12.5**

- [ ] 11. Implement the edge MCP child (venv side)
  - [x] 11.1 Resolve and pin the venv lock
    - `src/backend/mcp_edge/requirements.lock.txt` (D20): resolve with `uv pip compile --python-version 3.10 --python-platform aarch64-manylinux_2_35`; pins include `mcp==2.3.0`, `mcp-types==2.3.0`, `rpds-py==0.30.0`, `starlette==1.7.0`, and `exceptiongroup==1.3.1; python_version < "3.11"` as the only marker line
    - Prove wheel availability for every image: install the lock with `--only-binary :all:` for cp310 aarch64 (JP6, Ubuntu 22.04), cp311 aarch64 (JP5 Ubuntu 20.04 and JP7 Ubuntu 24.04) and cp311 x86_64 (amd64, x86_64_nvidia), and additionally check every wheel's manylinux tag is ≤ `manylinux_2_31` for the JP5 base (glibc 2.31 — the design's `manylinux_2_35` resolve target is above it); record the wheel file names of `rpds-py`, `pydantic-core`, `cryptography`, `cffi` in the task evidence (13.5)
    - Files: new `src/backend/mcp_edge/requirements.lock.txt`
    - Verify: `uv pip install --dry-run --only-binary :all: --python-version 3.11 --python-platform aarch64-manylinux_2_31 -r src/backend/mcp_edge/requirements.lock.txt` (and the 3.10 aarch64 and 3.11 x86_64 variants) succeed
    - _Requirements: 13.2, 13.5_

  - [x] 11.2 Implement the child's logging configuration and record builder
    - `child/__main__.py`: argparse (`--port`, `--backend-url`, `--server-version`, `--allowed-origins`, `--features`, `--mutating-enabled`, `--help`, `--version`) → `configure_logging()` (root, `httpx2`, `httpcore2`, `uvicorn*`, `mcp` at WARNING; `CredentialScrubFilter` dropping records containing `token=`, `Authorization`, `Bearer `; root handler on stderr, stdout reserved for records) → `uvicorn.run(..., host='127.0.0.1', log_config=None, access_log=False)`; `child/logging.py:emit` with a whitelisting record builder
    - Files: new `src/backend/mcp_edge/child/__init__.py`, `src/backend/mcp_edge/child/__main__.py`, `src/backend/mcp_edge/child/logging.py`
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_child_logging.py -q` (task 11.3) and `PYTHONPATH=src/backend uv run --python 3.11 --with-requirements src/backend/mcp_edge/requirements.lock.txt -m mcp_edge.child --version`
    - _Requirements: 9.7, 11.1, 11.2_

  - [x] 11.3 Write property test for edge log record hygiene
    - **Feature: mcp-servers, Property 13: Edge log record hygiene**
    - File `test/backend-test/mcp_edge/test_child_logging.py`; Hypothesis, min 100 examples
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_child_logging.py -q`
    - **Validates: Requirements 11.1, 11.2, 9.7**

  - [x] 11.4 Implement the edge `ToolSpec` and author all 73 edge catalog rows
    - `child/catalog.py`: the portal fields minus `scope`/`permissions`, plus `feature`, `role_policy`, `credential_in_query` (the five `download_file.py` image tools), the D34 fields (`scrub_paths`, `image_field`, `result_constant`, `drop_fields`), `limit_max`/`limit_default`, `body_constants` with `NOW_EPOCH_S`, `timeout_s`, `body_fields`, `text_tail_arg`, `resource_id_from_body`, and [review-9 N1] `accepted_effects` with `ACCEPTED_EFFECT_NOTE`, transcribed from the edge table (the six effects the transitive walk finds on eight tools at `c3bf88c1` in revision 11, not the seed list task 1.3 first proposed: revision 10's five plus the JP6 Pillow staging entry `gstreamer.pipeline_builder.GstPipelineBuilder._stage_decoded_png:Image.save` on `edge_image_source_test_connection`/`_preview` [review-10 N7]; the retention sweep, the Triton client and `model_status_shadow.report` are fixed `RO` cases, not entries), with entries keyed by the effect-list name and the two `write_csi_config` `os.makedirs`/`os.chmod` entries once task 1.20 lands [review-11 N3]; [review-9 M2] `edge_registration_executions_list` `limit (→ query limit)`; [review-10 M1] **no** `image_source_configuration` argument on `edge_image_source_preview`, and no `object`-typed property on any `RO` edge spec; [review-11 N1, task 1.19] `model_name (→ path modelName)` on `edge_model_start`/`edge_model_stop` and `model_name (→ path model_name)` on `edge_textgen_generate`; [review-11 N5, task 1.22] `RO_FREE_TEXT_ARGS` with a one-line reason per entry; edge `SERVER_CONSUMED_ARGS = ('confirm', 'limit', 'offset', 'tail_chars')`; `render_description` with `" on this device"`; every row of the revision-11 edge table (47 / 9 / 17), text-generation rows `feature='vllm'`, the excluded path-addressed routes absent (8.5, 10.8)
    - Files: new `src/backend/mcp_edge/child/catalog.py`
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_property_child_catalog.py -q` (task 11.13) after this task; a direct import lists 47 Phase-1 tools with `--features vllm`
    - _Requirements: 8.1, 8.2, 8.4, 10.1, 10.6, 10.8, 14.1, 14.2_

  - [x] 11.5 Implement principal resolution and the role gate
    - `child/principal.py:resolve_principal` via `GET {backend}/local-auth/whoami` with the caller's `Authorization` and `X-Request-ID` (401 → `not_authenticated`); `child/authz.py:decide` (read-only for every principal incl. `anonymous`; mutating requires the switch, `source == local_session`, role ∈ {Operator, UseCaseAdmin, PortalAdmin}; destructive requires `confirm`, incl. `confirm_literal`)
    - Files: new `src/backend/mcp_edge/child/principal.py`, `src/backend/mcp_edge/child/authz.py`; new `test/backend-test/mcp_edge/test_child_principal.py` (expired token, disabled account → `not_authenticated` on the next call; role taken only from `whoami`)
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_child_principal.py -q`
    - _Requirements: 9.1, 9.4, 9.5, 10.2, 10.3, 10.4, 10.5, 10.6, 16.2_

  - [x] 11.6 Write property test for the edge role-gate decision
    - **Feature: mcp-servers, Property 10: Edge role-gate decision**
    - File `test/backend-test/mcp_edge/test_authz.py`; Hypothesis, min 100 examples
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_authz.py -q`
    - **Validates: Requirements 10.2, 10.3, 10.4, 10.5, 10.6, 9.1, 16.3**

  - [x] 11.7 Implement the child's backend dispatch
    - `child/dispatch.py:call_backend` with `make_client(backend_url)` (`httpx2`, `verify=False` on `https://127.0.0.1:5443` only with the `nosec` note, `HTTPTransport(retries=0)`), per-spec `timeout_s`, path segments `quote(value, safe='')` after schema refusal of `/`, `.`, `..`, query parameters for GET routes, `{**resolve_constants(body_constants), **mapped_args}` bodies, the multipart `edge_static_image_pin` body with `base64.b64decode(..., validate=True)` before any request, `credential_in_query` placement, `X-Request-ID` always, `_safe_error`, the "operation may have been applied; call `<status_tool>`" message for `M`/`M!`, and the backend-status → category mapping shared with the portal vocabulary
    - Files: new `src/backend/mcp_edge/child/dispatch.py`
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_property_child_dispatch.py test/backend-test/mcp_edge/test_property_child_errors.py -q` (task 11.8)
    - _Requirements: 8.3, 8.6, 9.1, 9.7, 11.3_

  - [x] 11.8 Write property tests for edge credential forwarding and status mapping
    - **Feature: mcp-servers, Property 16: Edge credential forwarding** — file `test/backend-test/mcp_edge/test_property_child_dispatch.py`
    - **Feature: mcp-servers, Property 8: Backend status mapping and scrubbing** (edge clause) — file `test/backend-test/mcp_edge/test_property_child_errors.py`
    - Hypothesis, min 100 examples each
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_property_child_dispatch.py test/backend-test/mcp_edge/test_property_child_errors.py -q`
    - **Validates: Requirements 9.1, 9.7, 11.3, 8.6**

  - [x] 11.9 Implement the child's result shaping
    - `child/shaping.py` in the single order of D34/§5/§7: `drop_fields` → `extract_image` → `result_constant` merge → `drop_oversized_images` (≥ 16 KiB image base64) → `scrub_paths` (`PATH_KEY_RE` and the single-token device-root value rule; `{"text": …}` exempt) → dispatcher slicing → `MAX_RESULT_BYTES`; `image/*` bodies → `ImageContent` ≤ 2 MiB (`image/jpg` → `image/jpeg`); `text/plain` tail by encoded size (binary search, `MAX_RESULT_BYTES − 1 000`, `truncated`/`total_chars`); `resource_id` keys and `resource_id_from_body`
    - Files: new `src/backend/mcp_edge/child/shaping.py`
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_property_child_shaping.py -q` (task 11.10)
    - _Requirements: 8.5, 8.6_

  - [x] 11.10 Write property tests for edge result bounding and path hygiene
    - **Feature: mcp-servers, Property 9: Result bounding** (edge clauses, incl. the (a)–(f) fixed cases)
    - **Feature: mcp-servers, Property 19: Edge result path hygiene** (incl. its fixed cases and idempotence)
    - File `test/backend-test/mcp_edge/test_property_child_shaping.py`; Hypothesis, min 100 examples per property
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_property_child_shaping.py -q`
    - **Validates: Requirements 8.5, 1.6**

  - [ ] 11.11 Implement the child server and its protocol, guardrail, legacy and import-boundary tests
    - `child/server.py`: `Server(name='dda-edge-mcp', version=args.server_version, cache_hints ttl 60 000 private)`, `streamable_http_app(streamable_http_path='/mcp', json_response=True, stateless_http=True, host='127.0.0.1', transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=['127.0.0.1:5790', 'localhost:5790'], allowed_origins=args.allowed_origins), max_request_body_size=1 MiB, /healthz)`, the §7 pipeline (whoami → lookup/alias → validate → phase/switch/feature gate → `decide` → dispatch → `emit` → shape), `not_available_on_device` and list omission without `--features vllm`
    - Tests per the Testing Strategy: `test_child_protocol.py` (every listed case incl. the review-6/7/8/revision-9 cases, `test_child_rejects_origin_and_foreign_host` with the `transport_security=None` control case, and `test_credential_in_query_never_logged` with its `basicConfig(force=True)` sibling), `test_child_guardrails.py`, `test_child_legacy_client.py`, `test_child_import_boundary.py` (child imports with `utils`/`local_auth`/`dao` stubbed to raise; `app.py`/supervisor never import `mcp`)
    - Files: new `src/backend/mcp_edge/child/server.py`; new `test/backend-test/mcp_edge/test_child_protocol.py`, `test_child_guardrails.py`, `test_child_legacy_client.py`, `test_child_import_boundary.py`
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_child_protocol.py test/backend-test/mcp_edge/test_child_guardrails.py test/backend-test/mcp_edge/test_child_legacy_client.py test/backend-test/mcp_edge/test_child_import_boundary.py -q`
    - _Requirements: 8.1, 8.2, 8.4, 8.5, 9.2, 9.7, 10.7, 10.8, 12.5, 15.3, 16.1, 16.5, 16.9_

  - [ ] 11.12 Write property test for child transport security
    - **Feature: mcp-servers, Property 18: Edge child transport security**
    - File `test/backend-test/mcp_edge/test_property_child_transport_security.py` (the real Starlette app over `httpx2.ASGITransport`); Hypothesis, min 100 examples
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_property_child_transport_security.py -q`
    - **Validates: Requirements 12.5, 9.1**

  - [ ] 11.13 Write the edge catalog honesty suites and the edge catalog properties
    - `test_child_catalog_classification.py` with [review-9 N1]: the **transitive** walk over `src/backend` (portal resolution rules plus `self.f`/`cls.f` in the class and its first-party bases, `C.f`, constructor calls `C(...)` → `C.__init__`, methods of typed objects, and function references, per the Testing Strategy row), failing on any effect not listed in the spec's `accepted_effects` and on any listed entry it no longer finds; the effect list of revision 11 includes PIL `Image.save` on a Pillow value (`Image.open`/`Image.new`/`Image.fromarray`/`ImageOps.*`, a method of such a value, or a name bound from one, `with` targets included), `os.replace`/`os.rename`, every `shutil` function except `disk_usage`/`which`/`get_terminal_size`, the file-creating `tempfile` functions, `Path.write_*` and `open` with `w`/`a`/`x`/`+`, with the JP6 `.dda_decoded.png` staging as a fixed case and six accepted effects on eight tools [review-10 N7] (eight since revision 12); plus, since task 1.20 (revision 12), the one Pillow definition (incl. the chained `ImageOps.exif_transpose(Image.open(p)).save(q)` case), entries matched on the effect-list name, and the `pathlib` twins and `os` mutators [review-11 N3]; fixed cases of the Testing Strategy row (`system.get_snapshot` and `save_capture_ids_for_inference_results_download` not `RO`; the GET-but-mutating rows; the accepted readings) — the deeper effects now discovered, not hand-listed
    - `test_child_catalog_arguments.py`: FastAPI signature / request-model / `Depends()` fields, `body_fields` literal check, `limit` maximum against route bounds, every fixed case of the Testing Strategy row incl. revisions 9, 10 and 11, and [review-10 M1] no `RO` edge spec has an `object`-typed property, `edge_image_source_preview` offers no `image_source_configuration`, and a call carrying `processingPipeline` is `invalid_input`; [review-11 N1, task 1.19] the fixed case `edge_model_start.param_map['model_name'] == 'modelName'`; [review-11 N5, task 1.22] every `string` property of an `RO` edge spec has a `pattern` or `enum` or is in `RO_FREE_TEXT_ARGS`
    - **Feature: mcp-servers, Property 2: Catalog well-formedness** (edge clauses) and **Feature: mcp-servers, Property 3: Tool list determinism and filtering** (edge list) — file `test_property_child_catalog.py`; Hypothesis, min 100 examples per property
    - Files: new `test/backend-test/mcp_edge/test_child_catalog_classification.py`, `test_child_catalog_arguments.py`, `test_property_child_catalog.py`
    - Verify: `EDGE_CHILD_PYTEST test/backend-test/mcp_edge/test_child_catalog_classification.py test/backend-test/mcp_edge/test_child_catalog_arguments.py test/backend-test/mcp_edge/test_property_child_catalog.py -q`
    - **Validates: Requirements 8.2, 8.3, 8.5, 10.1, 10.7, 14.1, 14.2, 14.4, 17.3**

- [ ] 12. Ship the child in the image, add the configuration keys, and update the build gates
  - [ ] 12.1 Add the venv and the package copy to the five backend Dockerfiles and the keys to the seven recipes
    - (a) after the backend-requirements step of each file — `Dockerfile.jp5:114`, `Dockerfile.jp6:163`, `Dockerfile.jp7:245`, `Dockerfile:44`, `Dockerfile.x86_64_nvidia:60` — `COPY mcp_edge/requirements.lock.txt ./mcp_edge/requirements.lock.txt` and the `python3 -m venv /opt/dda/mcp-venv && … --only-binary :all: -r … && … -c "import mcp, mcp.server; print('mcp venv OK')"` step; (b) `COPY mcp_edge ./mcp_edge` after `COPY user_accounts_sync ./user_accounts_sync` (`Dockerfile:122`, `.jp5:230`, `.jp6:442`, `.jp7:586`, `.x86_64_nvidia:144`); re-confirm the anchors at the then-current HEAD
    - `DefaultConfiguration` gains `McpServerEnabled: "false"` and `McpMutatingToolsEnabled: "false"` beside `LocalLoginEnabled: "false"`, with a comment naming the keys, in `recipe.yaml`, `recipe-arm64.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64-jp6.yaml`, `recipe-arm64-jp7.yaml`, `recipe-amd64.yaml`, `recipe-amd64-nvidia.yaml`
    - `src/docker-compose.yaml` and `src/backend/requirements.txt` stay byte-identical (port 5790 is loopback-only)
    - Files: `src/backend/Dockerfile`, `src/backend/Dockerfile.jp5`, `src/backend/Dockerfile.jp6`, `src/backend/Dockerfile.jp7`, `src/backend/Dockerfile.x86_64_nvidia`; the seven `recipe*.yaml` at the repository root
    - Verify: `git diff --exit-code src/docker-compose.yaml src/backend/requirements.txt station_install/setup_station.sh`; the image builds in task 17 run the `mcp venv OK` step
    - _Requirements: 12.1, 12.6, 13.1, 13.5, 10.7_

  - [ ] 12.2 Add the backend-process MCP tests to the container build gate
    - Extend the pytest list at `build-custom.sh:262–267` with `test/backend-test/mcp_edge/test_proxy_route.py`, `test_config.py`, `test_shared_ipc_reader.py`, `test_auth_decision.py` and `test/backend-test/api-endpoints/test_authorize_request.py`; `test_auth.py` stays in the list unchanged
    - Files: `build-custom.sh`
    - Verify: `EDGE_CONTAINER_PYTEST` over exactly the extended list passes in the JP5 and the JP6 flask-app images
    - _Requirements: 16.7, 12.7_

  - [ ] 12.3 Rebaseline the Security_Preservation_Gate for the intended Dockerfile edits (builds.md)
    - Recompute and update, in the same change: `test/backend-test/security/baselines/docker_baseline_backend_Dockerfile.jp5_masked.txt`, `…jp6_masked.txt`, `…jp7_masked.txt`, and the `src/backend/Dockerfile` sha256 entry in `docker_baseline_out_of_scope.json` (`sha256sum src/backend/Dockerfile`); confirm at implementation time that `Dockerfile.x86_64_nvidia` and the recipe variants still have no baseline (add none unless the suite has grown one); `dependency_baseline_requirements.txt` and the compose entry do not move; no preservation test is weakened or deleted
    - Run the full preservation suite in the flask-app container (JP5 image, `python3.11`; JP6 image, `python3.10`) and `GUARD_SUITE`; move `edge-cv-portal/infrastructure/cdk.out` aside (`mv cdk.out cdk.out.bak-$(date +%Y%m%dT%H%M%SZ)`) or add its new copies to `cdk_out_baseline.json`/`secrets_cdk_out_baseline.json`
    - Files: the four baseline entries above; `test/backend-test/security/preservation/cdk_out_baseline.json` and `secrets_cdk_out_baseline.json` only if cdk.out is kept
    - Verify: `docker run … flask-app:latest … pytest test/backend-test/security/preservation -q -p no:cacheprovider` (builds.md) green on both images; `GUARD_SUITE` green
    - _Requirements: 13.1, 16.8_

- [ ] 13. Checkpoint — edge host and container suites green
  - `EDGE_CHILD_PYTEST` over the fourteen child-side files listed in Notes, and `EDGE_CONTAINER_PYTEST` over the eight backend-process files listed in Notes plus `test/backend-test/api-endpoints/test_authorize_request.py`, `test/backend-test/utils/test_auth.py` and `test/backend-test/local_auth`, in both the JP5 and the JP6 flask-app images; the preservation suite in the container
  - Ensure all tests pass; ask the user if questions arise
  - _Requirements: 16.2, 16.3, 16.5, 16.7, 16.8_

- [ ] 14. Add the on-hardware harness stage (read-only part)
  - [x] 14.1 Register the `mcp` capability and the MCP profile flags in the harness
    - `KNOWN_CAPABILITIES` gains `mcp`; device profiles accept `mcp_mutating` and `mcp_snapshot` booleans (default false; parsed now, used from Phase 2); `DDA_HARNESS_MCP_SOAK_S` (default 600) and `DDA_HARNESS_ON_DEVICE`; `devices.yaml.example` and the README stage table document them and the stage's invocation under Python 3.11 with `mcp==2.3.0`
    - Files: `test/on-hardware/harness/harnesslib/config.py`, `test/on-hardware/harness/devices.yaml.example`, `test/on-hardware/harness/README.md`, `test/on-hardware/harness/selftest/test_config.py`
    - Verify: `python3 -m pytest test/on-hardware/harness/selftest -q`
    - _Requirements: 16.7_

  - [ ] 14.2 Write `stages/test_50_mcp.py` steps (1), (2), (4), (5), (6)
    - (1) `GET /mcp/status` through the authenticated `harnesslib` client (`client.login()`, credential reference `env:`/`file:`, no stored password); (2) official `mcp` `Client` to `{base_url}/mcp` with the session token (never logged): list tools and call the RO tools the design names per present area — incl. `edge_image_source_preview` (exactly one `ImageContent`), `edge_workflow_images_list` with no `limit` on a DIO-triggered workflow (skipped otherwise; ≤ 2 items; no `/` under `*Path` keys), `edge_results_list`, `edge_registration_get` (`executions` dropped), `edge_textgen_models_list` when `vllm`; (4) `/health` healthy and container restart count unchanged across the stage and `DDA_HARNESS_MCP_SOAK_S`; (5) corrupted token → 401, `Origin` → 403; (6) on-device direct loopback checks (skipped when remote)
    - The module skips with a recorded reason (not fails) when `mcp` is not importable, so the existing Python 3.9 harness invocation of the other stages is unaffected
    - Files: new `test/on-hardware/harness/stages/test_50_mcp.py`
    - Verify: `python3 -m pytest test/on-hardware/harness/selftest -q`; the stage runs on hardware in task 17
    - _Requirements: 16.7, 13.3, 8.5, 12.3_

- [x] 15. Ship the edge login helper
  - `scripts/dda-edge-mcp-login.sh <base-url>`: prompts for the username, reads the password with `read -s` (never argv/env), POSTs `/local-auth/login`, prints exactly one `export DDA_EDGE_TOKEN=<token>` line on stdout and everything else on stderr, writes no file, and ends stderr with the shell-history reminder (`HISTCONTROL=ignorespace`, `history -d`)
  - Files: new `scripts/dda-edge-mcp-login.sh`
  - Verify: `bash -n scripts/dda-edge-mcp-login.sh`; on the JP5 device in task 17, `eval "$(scripts/dda-edge-mcp-login.sh https://<device>:5443)"` yields a token that `GET /local-auth/whoami` accepts, and a wrong password yields the uniform 401 message with no token printed
  - _Requirements: 9.3, 9.6, 2.7_

- [ ] 16. Cross-server conventions, client configuration and documentation
  - [ ] 16.1 Write property test for the naming and identifier convention
    - **Feature: mcp-servers, Property 15: Naming and identifier convention**
    - File `edge-cv-portal/backend/tests/mcp/test_property_mcp_naming.py`, loading `src/backend/mcp_edge/child/catalog.py` by path next to the portal `CATALOG`; Hypothesis over tool pairs sharing a concept argument, min 100 examples
    - Verify: `PORTAL_MCP_PYTEST tests/mcp/test_property_mcp_naming.py -q`
    - **Validates: Requirements 14.1, 14.3**

  - [ ] 16.2 Add the Kiro client configuration entries
    - Two additive entries in `.kiro/settings/mcp.json`: `dda-portal` (`url`, `oauth.clientId`, `redirectUri` `http://localhost:7899/oauth/callback`, `oauthScopes`) and `dda-edge-example` (`url`, `headers.Authorization: "Bearer ${DDA_EDGE_TOKEN}"`), both `disabled: true` with placeholder values and no literal secret; the three existing servers untouched
    - Files: `.kiro/settings/mcp.json`
    - Verify: `python3 -c "import json; d=json.load(open('.kiro/settings/mcp.json'))['mcpServers']; assert d['dda-portal']['disabled'] and d['dda-edge-example']['disabled']; assert {'awslabs.aws-documentation-mcp-server','awslabs.cdk-mcp-server','aws-diagrams'} <= set(d)"`
    - _Requirements: 7.4, 15.1, 15.2, 9.6, 2.5_

  - [ ] 16.3 Update the administrator and device documentation
    - `edge-cv-portal/ADMIN_GUIDE.md` "MCP server (AI assistants)" section with every item of the design's Documentation table (two-step disable and the guard message, outputs, `mcpRedirectUris`, `mcpMutatingToolsEnabled`, `mcpAllowedOrigins`, the `McpTimeoutAlarm` wiring, the D29 registry-enforcement limitation with the affected tool list, the refresh-only app client, no gateway authorizer, the `mcp.json` example and the literal `redirectUri` rule, the discovery layout and the observed challenge header name (filled in by task 18), reading MCP audit entries, supported revisions, tested Kiro IDE/CLI versions, `mcp` SDK version, dates, the Troubleshooting entries); `README.md` pointer paragraph; new `docs/edge-mcp-server.md` (enablement keys, LAN vs tunnel with the plaintext-5000 limitation, the login helper and history warning, Kiro example with `${DDA_EDGE_TOKEN}`, role gating, open-access residual risk, trust-boundary statement, `/mcp/status`, the component configuration keys table, the per-tool table with phase [review-9 N6], classification, shaping and limits, the 130 s text-generation note, and that the five image tools produce no `api.access` record); `docs/connect-to-device.md` "Forward the LocalServer port for MCP"
    - State in both documents that no stdio package is distributed (Backlog 4), so the portal's only connection form is remote OAuth
    - Files: `edge-cv-portal/ADMIN_GUIDE.md`, `README.md`, new `docs/edge-mcp-server.md`, `docs/connect-to-device.md`
    - Verify: the documented `mcp.json` snippets parse as JSON after stripping comments; every tool in `catalog_tools.json` and in the edge `CATALOG` appears in a docs table with its phase (a small check script run once and recorded)
    - _Requirements: 15.1, 15.4, 15.5, 15.6, 17.3, 17.4, 2.5, 2.8, 6.4, 9.6, 11.3_

#### Phase 1 release

- [ ] 17. Build the LocalServer and verify the Edge_MCP_Server on real hardware (builds.md, before any edge commit)
  - [ ] 17.1 Run the pre-build checks
    - `pgrep -af "gdk component build"` and `pgrep -af "build-custom.sh"` return nothing; no `deploy-portal.sh`/`deploy-infrastructure.sh`/`deploy-frontend.sh` is running or will run during the build; `git status`/`git diff` of the preservation-tracked files matches task 12.3's rebaseline; `edge-cv-portal/infrastructure/cdk.out` moved aside; `GUARD_SUITE` green; the container preservation suite green
    - Verify: all commands above recorded in the task evidence with their output
    - _Requirements: 13.1, 13.4, 16.8_

  - [ ] 17.2 Build the component variants strictly one at a time
    - `TARGETS="5" ./run_jp_builds.sh` (log `.gdk_build_jp5.log`), then — only after it has fully finished — `TARGETS="6" ./run_jp_builds.sh` (`.gdk_build_jp6.log`); JP7 (`TARGETS="7"`) and amd64 afterwards where a device is available; restore `gdk-config.json` when done; never two builds of the same architecture and JetPack at once (stop both and restart one at a time if it happens)
    - Verify: each log shows the `mcp venv OK` step, the extended container pytest gate (task 12.2) and the security audit gate passing
    - _Requirements: 13.3, 13.4, 13.5, 12.6_

  - [ ] 17.3 Verify on a JP5 device
    - Deploy the built component (hot-patch only for iteration; the evidence comes from the built+deployed component). With `McpServerEnabled` absent/false: no listener on 5790, no child process, one startup diagnostic only, `/mcp` → 503. Set `McpServerEnabled=true` through a deployment configuration merge and confirm the child starts within 60 s; flip `LocalLoginEnabled` and `McpServerEnabled` at runtime to exercise both pollers over the one IPC connection
    - Log in with a synced account via `scripts/dda-edge-mcp-login.sh`; run `DDA_HARNESS_DEVICE=jp5-xavier uv run --python 3.11 --with mcp==2.3.0 --with pytest==9.0.3 --with requests --with pyyaml -m pytest test/on-hardware/harness/stages/test_50_mcp.py` with the default `DDA_HARNESS_MCP_SOAK_S=600` and then the full harness (`pytest test/on-hardware/harness/stages`) to confirm the existing stages are unchanged; connect Kiro to the device through the documented transport and run `tools/list` and one RO tool per capability area incl. one image tool
    - Confirm `/health` healthy, no crash loop, no container restart, no `awscrt` abort for the sustained window; kill the child once and confirm the supervisor restarts it with backoff while the Backend_API, workflows, Triton and cameras keep working
    - Verify: harness results bundle `test/on-hardware/harness/harness-results/jp5-xavier-<ts>/` green; restart count unchanged; evidence recorded
    - _Requirements: 13.3, 12.1, 12.2, 12.3, 12.4, 12.8, 9.1, 9.3, 9.4, 11.1, 11.3_

  - [ ] 17.4 Verify on a JP6 device
    - Same procedure as 17.3 on `jp6-orinagx` (Python 3.10 venv); include `edge_textgen_models_list` where the image has vLLM (`--features vllm`), and its absence from `tools/list` on a non-vLLM image
    - Verify: harness results bundle `…/jp6-orinagx-<ts>/` green; evidence recorded
    - _Requirements: 13.3, 12.6, 8.4_

  - [ ] 17.5 Verify JP7 and amd64 where available, or record their status
    - Same procedure on `jp7-thor` and an amd64 / x86_64_nvidia device when one is available; where none is, record that the architecture is built but not hardware-verified and obtain the owner's agreement in the sign-off (Requirement 12.6 — never silently absent)
    - Verify: evidence or the owner's recorded agreement
    - _Requirements: 12.6, 13.3_

  - [ ] 17.6 Commit the edge change with its verification statement
    - Commit the edge change, its baseline updates (task 12.3) and the `test_authorize_request.py` edit together; the commit message names the rebaselined files and why, and the devices and architectures verified (builds.md)
    - Verify: `git show --stat HEAD` lists code, tests and baselines in one commit; the message carries the device list
    - _Requirements: 13.1, 13.3_

- [ ] 18. Deploy the Portal_MCP_Server and run the deployed-stack smoke check
  - [ ] 18.1 Deploy the portal with the MCP stack
    - Only when no component build is running (`pgrep` as 17.1): `./deploy-infrastructure.sh` (stack on by default; `mcpMutatingToolsEnabled` default false); afterwards move the regenerated `cdk.out` aside before the next component build
    - Verify: `aws cloudformation describe-stacks --stack-name EdgeCVPortalMcpStack` shows `CREATE_COMPLETE`/`UPDATE_COMPLETE` and the seven outputs; the existing portal web UI and REST API behave unchanged (log in, list devices)
    - _Requirements: 6.1, 6.4, 5.4_

  - [ ] 18.2 Run the smoke check and the validate-first items
    - `curl -si -X POST "$MCP_URL" -H 'Content-Type: application/json' -d '{}'` → 401; record every header containing `www-authenticate` (expected `x-amzn-remapped-www-authenticate`) and write the observed name into ADMIN_GUIDE; fetch the root PRM, `/.well-known/oauth-protected-resource/mcp` and the RFC 8414 facade and compare `authorization_servers[0]` and `issuer` byte for byte; `aws cognito-idp describe-user-pool-client` shows `ExplicitAuthFlows: ['ALLOW_REFRESH_TOKEN_AUTH']`
    - Kiro IDE **and** Kiro CLI complete the OAuth flow through the facade with `redirectUri` `http://localhost:7899/oauth/callback`; record validate-first items (ii)–(viii) (scopes, `aud` on views and after refresh, literal redirect, root PRM acceptance, no `Origin` sent — else set `mcpAllowedOrigins`/`McpAllowedOrigins` and record it); call `portal_admin_whoami` and one RO tool per view; confirm marked entries in the Audit_Log and the MCP channel filter in the audit UI; disable a test account and confirm the refresh grant fails
    - Verify: the Phase-1 evidence file records every observation above
    - _Requirements: 2.4, 2.6, 2.8, 4.1, 4.6, 7.4, 7.5, 6.3_

- [ ] 19. Obtain the Phase-1 owner sign-off and release Phase 1
  - [ ] 19.1 Present the sign-off items and record the decisions
    - Items (a)–(i) of the Phasing table — Requirement 3.9 interpretation; no gateway authorizer; the 2.4 header deviation (declining selects Backlog 10's CloudFront remedy before Phase 1 ships); the 2.4 "naming the User_Pool" reading; the `PORTAL_REGISTRY_ENFORCED=false` limitation (D29 options A/B/C); the `max(k, 1)` reading of 16.4; `aud`-less tokens on `client_id` + scope; the operation-level deferrals of 17.4; the status-sync reading — plus **(j) [review-9 N5]** the Greengrass-backed reads scoped by account, not by Use_Case: `portal_devices_get`, `_logs_list`, `_logs_get`, `_logs_analyze` (Phase 2), `_deployment_get`, `_deployment_for_target_get` and `_components_get`. Accept them as REST parity (the design's recommendation), or defer the id-addressed tools until Backlog 26 binds them. (h) also lists `portal_data_labeling_manifest_get` among the operation-level deferrals
    - Any non-default choice becomes a follow-up task before release: option (B) → a synth-time check in `bin/app.ts` and the RBAC matrix's enforcement-off expectations; option (C) → Backlog 12 and Property 5; declining (c) → the CloudFront distribution; deferring under (i)/(j) → reclassify or remove the rows and re-run tasks 5.6–5.11 and 7.4
    - Verify: the owner's decisions recorded in the spec directory's Phase-1 evidence
    - _Requirements: 17.1, 17.4, 3.6, 3.9, 2.4, 16.4_

  - [ ] 19.2 Release Phase 1
    - Confirm `mcp_portal.DELIVERED_PHASES == (1,)` and `mcp_edge.DELIVERED_PHASES == (1,)`, both mutating switches default off, the docs state the phase of every tool; the evidence bundle holds the smoke check, Kiro IDE/CLI runs, JP5/JP6 (and any JP7/amd64) on-device results, and the green suite runs of tasks 9 and 13
    - Verify: `PORTAL_MCP_PYTEST tests/mcp -q` and the task-13 commands re-run green at the release commit
    - _Requirements: 17.1, 17.3, 5.4, 10.7_

- [ ] 20. Final checkpoint — Phase 1 complete
  - Ensure all tests pass, ask the user if questions arise
  - _Requirements: 17.1_

### Phase 2 — low-consequence mutating tools

- [ ] 21. Meet the Phase-2 entry criteria
  - [ ] 21.1 Re-verify the Phase-2 rows at the current HEAD
    - Re-run tasks 5.6–5.11 and 11.13 (route, classification, audit-flag, argument, permission suites of both catalogs) at the HEAD Phase 2 is cut from; correct any row whose handler drifted since `c3bf88c1`, and re-export `catalog_routes.json`/`catalog_tools.json`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp -q` and `EDGE_CHILD_PYTEST` over the task-13 child list green
    - _Requirements: 1.3, 5.1, 10.1, 17.2_

  - [ ] 21.2 Obtain the owner sign-off on the Phase-2 destructive tools
    - The design names the static unpins (portal and edge), `edge_system_snapshot_create` and `portal_workflows_plugin_source_set`; present the complete Phase-2 `destructive=true` list generated from `catalog_tools.json` and the edge `CATALOG` so none is missed
    - Verify: the decision recorded in the Phase-2 evidence
    - _Requirements: 5.2, 10.1, 17.2_

- [ ] 22. Deliver Phase 2 on the portal
  - [ ] 22.1 Bump the delivered phases and the documentation
    - `mcp_portal.DELIVERED_PHASES = (1, 2)` and a minor `__version__` bump; ADMIN_GUIDE tool table shows the Phase-2 tools incl. `portal_devices_logs_analyze` and `portal_workflows_plugin_generate_status_get` [review-9 N6]
    - Files: `edge-cv-portal/backend/functions/mcp_portal/__init__.py`, `edge-cv-portal/ADMIN_GUIDE.md`
    - Verify: `PORTAL_MCP_PYTEST tests/mcp -q` (Property 3 and the protocol test list the Phase-2 tools; the RBAC matrix, guardrail and audit suites cover them)
    - _Requirements: 17.2, 17.3, 5.3, 5.4, 16.4, 16.5_

  - [ ] 22.2 Deploy and exercise one Phase-2 mutating tool per area
    - Deploy (sequenced against component builds as 18.1) with `-c mcpMutatingToolsEnabled=true` on a non-production portal; call one `M` tool per Phase-2 area (e.g. `portal_models_training_create`, `portal_data_folder_create`, `portal_workflows_validate`, `portal_devices_camera_add`, `portal_workflows_build_submit`) and confirm the returned `resource_id`/job id, exactly the marked entries of Property 6, and that an `M!` call without `confirm` is refused; confirm a Viewer is denied every `M` tool
    - Verify: the observations recorded in the Phase-2 evidence
    - _Requirements: 1.4, 4.3, 4.4, 5.3, 5.7, 3.5, 6.2_

- [ ] 23. Deliver Phase 2 on the edge
  - [ ] 23.1 Bump the delivered phases and add the harness's mutating step
    - `mcp_edge.DELIVERED_PHASES = (1, 2)`; `test_50_mcp.py` step (3) when the profile declares `mcp_mutating: true`: `edge_static_image_pin` → `_pin_get` → `edge_static_image_unpin` (skipped when an image is already pinned), `edge_results_summary_reset` with the restoration of review-7 N5, `edge_execution_log_get(tail_chars=2000)`, `edge_cameras_rescan` with the before/after camera list, `edge_system_snapshot_create(confirm="snapshot")` only with `mcp_snapshot: true`; `edge_image_source_capture` deliberately not called; `docs/edge-mcp-server.md` tool table updated
    - Files: `src/backend/mcp_edge/__init__.py`, `test/on-hardware/harness/stages/test_50_mcp.py`, `docs/edge-mcp-server.md`
    - Verify: the task-13 commands green; `python3 -m pytest test/on-hardware/harness/selftest -q`
    - _Requirements: 17.2, 17.3, 10.6, 16.7_

  - [ ] 23.2 Build and verify Phase 2 on JP5 and JP6
    - Repeat 17.1–17.2 (no Dockerfile or recipe change is expected, so no rebaseline — confirm with `git diff` before building); on `jp5-xavier` then `jp6-orinagx` with `McpMutatingToolsEnabled=true`: the harness stage incl. step (3), one `M` tool per edge area through Kiro, a Viewer-role and an Existing_Token_Auth principal denied every `M` tool, flipping `McpMutatingToolsEnabled` restarts the child and the list changes; sustained health as 17.3
    - Verify: harness bundles green on both devices; commit carries the device statement
    - _Requirements: 13.3, 13.4, 10.2, 10.5, 10.7, 12.3_

- [ ] 24. Release Phase 2
  - Release notes and evidence bundle; the docs show which tools Phase 2 added
  - Verify: the Phase-2 evidence holds 21.2, 22.2 and 23.2
  - _Requirements: 17.2, 17.3_

- [ ] 25. Final checkpoint — Phase 2 complete
  - Ensure all tests pass, ask the user if questions arise
  - _Requirements: 17.2_

### Phase 3 — high-consequence mutating tools

- [ ] 26. Meet the Phase-3 entry criteria
  - [ ] 26.1 Re-verify the Phase-3 rows and the prerequisites
    - Re-run the suites as 21.1; `portal_models_delete` enters `CATALOG` only if Backlog 1's `models.py:522` fix has merged (outside this spec) — otherwise `test_mcp_catalog_permissions.py` keeps asserting its absence; perform the live check of which ARN `components.delete_component` accepts before `portal_devices_component_delete` is delivered (keep `component_arn` if the base ARN works; otherwise stop and replan the row with the owner); account-lifecycle tools stay out unless Backlog 2's routes are registered first (the route test enforces the order)
    - Verify: suites green; the ARN check recorded
    - _Requirements: 1.3, 5.7, 17.2_

  - [ ] 26.2 Obtain the owner sign-off on the Phase-3 destructive list
    - The full Phase-3 `destructive=true` list from `catalog_tools.json` and the edge `CATALOG`, incl. the revision-9 additions (deployment create, role remove, edge-sync device sync, team member remove, workflow update/retry)
    - Verify: the decision recorded in the Phase-3 evidence
    - _Requirements: 5.2, 10.1, 17.2_

- [ ] 27. Deliver Phase 3 on the portal
  - `mcp_portal.DELIVERED_PHASES = (1, 2, 3)`, minor version bump, ADMIN_GUIDE tool table; deploy as 22.2 and exercise `portal_devices_deployment_create` against a test thing group with `confirm=<target_thing_group>` [review-9 N7] (preflight 409 acknowledgement path unchanged), `portal_devices_deployment_cancel`, and one other Phase-3 tool per area; confirm the `status_tool` message on a forced dispatch timeout in a test deployment
  - Files: `edge-cv-portal/backend/functions/mcp_portal/__init__.py`, `edge-cv-portal/ADMIN_GUIDE.md`
  - Verify: `PORTAL_MCP_PYTEST tests/mcp -q`; observations recorded in the Phase-3 evidence
  - _Requirements: 17.2, 17.3, 1.4, 5.3, 5.7, 6.2_

- [ ] 28. Deliver Phase 3 on the edge
  - [ ] 28.1 Bump the delivered phases and the documentation
    - `mcp_edge.DELIVERED_PHASES = (1, 2, 3)`; `docs/edge-mcp-server.md` tool table
    - Files: `src/backend/mcp_edge/__init__.py`, `docs/edge-mcp-server.md`
    - Verify: the task-13 commands green
    - _Requirements: 17.2, 17.3_

  - [ ] 28.2 Build and verify Phase 3 on JP5 and JP6 with restoration
    - Build as 23.2; on each device verify `edge_system_restart_dda(confirm="restart-dda")` (the backend returns healthy afterwards and the child restarts), `edge_registration_continuous_pause`/`_resume` with the original state restored, `edge_registration_trigger`, a workflow create → run → delete round trip on a scratch workflow, and `edge_model_stop`/`_start` restoring the original model state; sustained health as 17.3
    - Verify: evidence recorded per device; commit carries the device statement
    - _Requirements: 13.3, 13.4, 10.1, 10.6_

- [ ] 29. Release Phase 3
  - Release notes and evidence bundle
  - Verify: the Phase-3 evidence holds 26.1, 26.2, 27 and 28.2
  - _Requirements: 17.2, 17.3_

- [ ] 30. Final checkpoint — Phase 3 complete
  - Ensure all tests pass, ask the user if questions arise
  - _Requirements: 17.2_

## Notes

- No task is optional (`*` is not used): Requirement 16 coverage is a Phase-1 entry criterion, and Requirement 17.1 requires it for every Phase-1 tool.
- The catalog tables in `design.md` (revision 12, which applied tasks 1.14 and 1.19–1.23) are the single source of truth for every row; tasks 5.x and 11.4 transcribe them and the AST suites hold the transcription to the handlers.
- Child-side edge tests (`EDGE_CHILD_PYTEST`): `test_child_logging.py`, `test_child_principal.py`, `test_authz.py`, `test_property_child_dispatch.py`, `test_property_child_errors.py`, `test_property_child_shaping.py`, `test_child_protocol.py`, `test_child_guardrails.py`, `test_child_legacy_client.py`, `test_child_import_boundary.py`, `test_property_child_transport_security.py`, `test_child_catalog_classification.py`, `test_child_catalog_arguments.py`, `test_property_child_catalog.py`. Backend-process edge tests (`EDGE_CONTAINER_PYTEST`): `test_shared_ipc_reader.py`, `test_auth_decision.py`, `test_whoami_route.py`, `test_supervisor.py`, `test_log_relay.py`, `test_config.py`, `test_proxy_route.py`, `test_property_proxy_exposure.py`. The design runs the whole directory with one `uv run --python 3.11` command; that cannot hold, because the venv lock (Starlette 1.7.0, pydantic ≥ 2.12) and the backend pins (FastAPI 0.109.2, httpx 0.24.1) cannot share one interpreter, so the split above follows the design's own container-gate rule (backend-process modules import no `mcp`).
- Assumptions made where the design is silent: the harness stage skips (not fails) without `mcp` (task 14.2); the lock is also checked against `manylinux_2_31` for the JP5 base (task 11.1); Property 8's edge clause, Property 12 and Property 16 get their own files (the design lists no file for them); the edge Property 2/3 clauses get `test_property_child_catalog.py`.
- Builds run one variant at a time; no portal deploy while a component build runs; `cdk.out` is moved aside before each build (builds.md, C6).
- Edge changes (tasks 10–15) are committed only in task 17.6, after on-device verification on JP5 and JP6 (builds.md); the same rule applies to the Phase-2 and Phase-3 edge deliveries (23.2, 28.2). Portal-only changes may be committed once task 9 is green.
- Requirements 2.5 and 15.6 are conditional on a stdio form, which the design does not ship (Backlog 4); tasks 16.2 and 16.3 record that the only portal connection form is remote OAuth and that no stdio package is distributed.

## Requirement Coverage

| Requirement | Tasks |
|---|---|
| 1 Portal capabilities | 1.1, 1.2, 1.5, 1.6, 1.12–1.14, 4.1, 5.1–5.11, 6.3, 6.5–6.7, 7.1, 22.2, 27 |
| 2 Portal authentication | 3.2–3.6, 6.3, 6.4, 7.2, 8.2, 8.3, 15, 16.2, 16.3, 18.2, 19.1 |
| 3 Portal authorization | 1.1, 1.7, 1.12, 1.14, 1.17, 5.1–5.11, 6.1, 6.2, 7.4, 19.1, 22.2 |
| 4 Portal audit | 1.7, 1.15, 2.1–2.4, 3.5, 3.6, 5.9, 6.4, 6.8, 6.9, 7.2, 18.2, 22.2 |
| 5 Portal mutating safety | 1.4, 1.9, 1.12, 1.16, 1.21, 1.23, 4.1, 5.x, 7.5, 8.1–8.3, 21.2, 22.x, 26.x, 27 |
| 6 Portal limits and operations | 2.1, 5.3, 6.3, 6.8, 6.10, 8.1–8.5, 9, 18.1, 18.2 |
| 7 Portal protocol | 3.1–3.3, 6.10, 7.2, 7.3, 8.2, 16.2, 18.2 |
| 8 Edge capabilities | 1.3, 1.11, 1.18–1.20, 1.22, 11.4, 11.7–11.11, 11.13, 14.2, 17.4 |
| 9 Edge authentication | 10.2–10.4, 10.7, 11.2, 11.3, 11.5–11.8, 11.11, 11.12, 15, 16.2, 16.3, 17.3 |
| 10 Edge classification and role gating | 1.3, 1.11, 1.18, 1.20, 1.22, 4.2, 10.4–10.6, 11.4–11.6, 11.11, 11.13, 12.1, 19.2, 21.x, 23.x, 26.2, 28.2 |
| 11 Edge logging | 10.5, 10.7, 11.2, 11.3, 11.7, 11.8, 16.3, 17.3 |
| 12 Edge enablement and exposure | 10.1–10.8, 11.11, 11.12, 12.1, 12.2, 14.2, 17.3–17.5, 23.2 |
| 13 Edge build and verification | 11.1, 12.1, 12.3, 14.2, 17.1–17.6, 23.2, 28.2 |
| 14 Consistent semantics | 1.9, 1.16, 1.17, 1.19, 1.21, 1.23, 4.1, 4.2, 5.7, 11.4, 11.13, 16.1 |
| 15 Packaging and docs | 3.1, 6.10, 7.1, 10.5, 11.11, 16.2, 16.3 |
| 16 Testing | 1.14, 1.15, 3.4, 5.9, 6.2, 6.8, 6.9, 7.1–7.5, 8.3–8.5, 9, 10.7, 11.5, 11.6, 11.11, 12.2, 12.3, 13, 14.x, 22.1, 23.1 |
| 17 Phasing | 1.8, 1.10, 4.1, 4.2, 5.7, 11.13, 16.3, 19.x, 21–29 |

## Task Dependency Graph

```json
{
  "phase_0": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9"] },
    { "id": 1, "tasks": ["1.10", "1.11", "1.12", "1.13", "1.14", "1.15", "1.16", "1.17", "1.18", "1.19", "1.20", "1.21", "1.22", "1.23"] }
  ],
  "phase_1": [
    { "id": 2, "tasks": ["2.1", "3.1", "8.1", "10.1", "10.2", "11.1", "14.1"] },
    { "id": 3, "tasks": ["2.2", "2.3", "3.2", "3.4", "4.1", "10.3", "10.4", "10.5", "11.2"] },
    { "id": 4, "tasks": ["2.4", "3.3", "3.5", "3.6", "4.2", "5.1", "5.2", "5.3", "5.4", "5.5", "10.6", "10.7", "11.3", "11.4"] },
    { "id": 5, "tasks": ["5.6", "5.7", "5.8", "5.9", "5.10", "5.11", "6.1", "6.3", "6.5", "10.8", "11.5", "11.7", "11.9"] },
    { "id": 6, "tasks": ["6.2", "6.4", "6.6", "6.7", "6.8", "8.2", "11.6", "11.8", "11.10", "11.11", "15"] },
    { "id": 7, "tasks": ["6.9", "6.10", "8.3", "8.4", "8.5", "11.12", "11.13", "12.1", "16.1", "16.2"] },
    { "id": 8, "tasks": ["7.1", "7.2", "7.3", "7.4", "7.5", "12.2", "12.3", "14.2"] },
    { "id": 9, "tasks": ["9", "13", "16.3"] },
    { "id": 10, "tasks": ["17.1"] },
    { "id": 11, "tasks": ["17.2"] },
    { "id": 12, "tasks": ["17.3", "17.4", "17.5"] },
    { "id": 13, "tasks": ["17.6", "18.1"] },
    { "id": 14, "tasks": ["18.2"] },
    { "id": 15, "tasks": ["19.1"] },
    { "id": 16, "tasks": ["19.2", "20"] }
  ],
  "phase_2": [
    { "id": 17, "tasks": ["21.1", "21.2"] },
    { "id": 18, "tasks": ["22.1", "23.1"] },
    { "id": 19, "tasks": ["22.2", "23.2"] },
    { "id": 20, "tasks": ["24", "25"] }
  ],
  "phase_3": [
    { "id": 21, "tasks": ["26.1", "26.2"] },
    { "id": 22, "tasks": ["27", "28.1"] },
    { "id": 23, "tasks": ["28.2"] },
    { "id": 24, "tasks": ["29", "30"] }
  ]
}
```
