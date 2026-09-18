# Implementation Plan: Portal JWT-Role Privilege Escalation

## Overview

Stop a self-asserted `custom:role` claim from granting portal privilege, and
make every audited action attributable after the actor's Cognito user is
deleted. Verified incident (bugfix.md): a Cognito user created, authenticated,
used to submit JP7 build `2ec3d7ab` (published LocalServer **1.0.40**), and
deleted inside 6 seconds, leaving an audit row keyed on a `sub` that resolves
to nothing in any of the account's 7 user pools.

Order matters and is not negotiable: the registry writer and the backfill
must both land **before** enforcement is enabled, or the fix locks every user
— including the bootstrap `admin` — out of the portal. Tasks 1-3 are
therefore behaviour-preserving (enforcement flag off); task 5 is the flip.

Per `.kiro/steering/builds.md`: run portal backend tests with
`~/.venvs/dda-portal-tests/bin/python` from `edge-cv-portal/backend`, targeted
files only, never the whole `tests/` directory in one pytest process. No
`src/` (device) file is touched by this spec, so no component build and no
preservation rebaseline is owed.

## Tasks

- [x] 1. Capture the defect and the preserved behavior
  - [x] 1.1 Write the exploration suite that fails on unfixed code
    - `edge-cv-portal/backend/tests/test_portal_registry_privilege_exploration.py`
    - Replay the incident: seed a Cognito-only principal (claims with
      `custom:role=PortalAdmin`, no `dda-portal-user-roles` row), call the real
      `POST /builds` handler through `require_builds_submit()`, assert 403 —
      **fails today with 200**
    - The `DataScientist` and `UseCaseAdmin` claim variants (the roles that
      actually carry `builds:submit`), same expectation
    - Assert an accepted action's audit row carries `username`/`email`/
      `source_ip`/`user_agent` — **fails today** (fields absent)
    - _Requirements: 7.1, 4.1_
    - **OUTCOME**: Added `edge-cv-portal/backend/tests/test_portal_registry_privilege_exploration.py` (13 tests, no production code touched): it drives the real `build_jobs.handler` → `submit_build` behind the real `@require_builds_submit()` with real `shared_utils` role resolution and real audit writes, on the conftest moto stack plus a suite-local BuildJobs table carrying the deployed GSIs (dispatcher disabled, so no build can launch). Result on the unfixed tree: **12 failed, 1 passed** — the three unprovisioned-claim replays (PortalAdmin/DataScientist/UseCaseAdmin) are accepted 201 instead of 403, the same three grant `builds:submit` at the `has_permission` layer, a provisioned Viewer is raised by its PortalAdmin claim, there is no denial to audit, and the `build_requested` row plus the Build_Job carry none of the five Attribution_Fields / `requested_by_username`+`requested_by_email`; the one pass is the deliberate control (a registry-provisioned DataScientist is still accepted, Req 7.2), which keeps the accepted-path assertions non-vacuous. Decisions: the incident is replayed with its own submission (target JP7, ephemeral, ref `feat/capture-phase-outputs`, source IP `12.148.187.67`, UA `aws-cli/1.36.4`); `identity_source` is asserted in its own test (Req 4.1) so a partial task-2.2 implementation is visible without masking the four human-attribution fields; the Claimed_Role check on the denial row accepts any `details` value naming the claim, since design.md fixes the location (details) but not the key name. Neighbouring suites stay green in the same process (`test_build_rbac.py` 24, `test_rbac_global_scope_jwt_role.py` 5, `test_shared_utils_user_identity.py` 7, and with `test_audit_strict_helpers.py` + `test_user_admin_disable_enable.py` + `test_user_admin_delete.py`: 57 passed alongside the 12 intended failures); the two repo-root preservation guards pass (4 passed, 3 skipped). No preservation-tracked file changed, so no rebaseline was owed.

  - [x] 1.2 Write the preservation suite for everything that must not move
    - `edge-cv-portal/backend/tests/test_portal_registry_privilege_preservation.py`
    - The 6 real account shapes (bootstrap `admin` PortalAdmin, `demoadmin`,
      `demoViewer`, `ryan-labeler`, `ryan-labeler2`, `demoDataScientist`)
      keep their current authorization outcomes once a registry row exists
    - Per-Use_Case precedence over the global role; the 403 envelope
      (`{'error': 'Insufficient permissions', 'required_permissions': [...],
      'usecase_id': ...}`) byte-identical; `event_id` shape
      `f"{user_id}_{timestamp}"`; both audit GSI projections still resolve
    - _Requirements: 2.3, 1.3, and the Unchanged Behavior list_
    - **OUTCOME**: Added `edge-cv-portal/backend/tests/test_portal_registry_privilege_preservation.py` (40 tests, no production code touched) as the immutable oracle for bugfix.md's Unchanged Behavior list; it **passes on the unfixed tree (40 passed)** and must keep passing after enforcement. Every case is evaluated in the post-backfill state (a `dda-portal-user-roles` global row carrying `role`/`username`/`email`/`status='enabled'` plus the matching `custom:role` claim), which is the only state in which "unchanged" is meaningful: it pins the 6 pool accounts' resolved role at global and Use_Case scope, `is_portal_admin`, permission set, and outcomes on four real guards (`require_builds_submit`, `workflow:read` at Use_Case scope, `labeling:tasks-self`, `super_user_only`) as hand-written literals; per-Use_Case precedence plus its non-leakage to `global`/another Use_Case and `get_accessible_usecases`; the 403 / super-user-403 / 400 / 500 `Authorization check failed` envelopes compared as **exact body strings** with the default headers, the `unauthorized_access` + `unauthorized_super_user_access` audit actions and their `details` keys, the handler-outside-the-try/except contract and the four `rbac_context` keys; the audit schema (`log_audit_event` `event_id == f"{user_id}_{timestamp}"`, the strict helper's `_{8 hex}` suffix, the nine legacy keys, the 90-day ttl, the password/verifier/hash/temp* denylist through `finalize_audit_event`'s merge, and log_audit_event still swallowing its own failures); and both deployed audit GSIs (`user-actions-index`, `usecase-actions-index`) against a suite-local table carrying the `storage-stack.ts` index definitions, since the conftest audit table has none. Decisions recorded in the module docstring: `demoadmin`'s role is nowhere in-repo, so it is pinned for **both** candidate readings (UseCaseAdmin and PortalAdmin) rather than guessed; account emails are `<username>@example.com` placeholders (only usernames/roles are load-bearing); the bootstrap `admin` case omits the `email` claim, matching the real account; and three shapes are **deliberately not pinned** because design.md changes or leaves them open — global-PortalAdmin + a downgrading Use_Case row (today's short-circuit vs the Expected Behavior table), a Use_Case row with no global row (Req 1.1 vs the Glossary's "no enabled *global*" wording, settled by task 2.1), and the unprovisioned principal itself (owned by the exploration suite). Verification: preservation suite 40 passed alone; in one process with the exploration suite plus `test_build_rbac.py`, `test_rbac_global_scope_jwt_role.py`, `test_shared_utils_user_identity.py`, `test_audit_strict_helpers.py` → **89 passed, 12 failed** where the 12 are exactly task 1.1's intended exploration failures (no interference, no new failures); per-file: build_rbac 24, rbac_global_scope 5, user_identity 7, audit_strict 12; the two repo-root security preservation guards 4 passed / 3 skipped. No preservation-tracked file changed, so no rebaseline was owed.

- [x] 2. Make the registry writable and audit entries attributable (flag still off)
  - [x] 2.1 Registry read path and the failure distinction in `shared_utils.py`
    - `RegistryUnavailable`; `_lookup_identity(user_id, usecase_id)` reading
      the global row plus the Use_Case row, raising instead of swallowing
    - `get_user_role`: enforcement branch per design.md's Expected Behavior
      table, legacy branch behind `PORTAL_REGISTRY_ENFORCED` (default
      **false**) that additionally logs a `would_deny` WARNING naming the
      request it would have denied
    - `get_user_permissions` / `has_permission` propagate `None` and let
      `RegistryUnavailable` escape
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.4_
    - **OUTCOME**: `edge-cv-portal/backend/layers/shared/python/shared_utils.py` now carries the registry read path: `RegistryUnavailable`, `registry_enforcement_enabled()` (reads `PORTAL_REGISTRY_ENFORCED` per call, default **false**, truthy set `1/true/yes/on/enabled`), `_identity_is_enabled` / `_identity_role` helpers, and module-level `_lookup_identity(user_id, usecase_id)` which reads the global row plus (for a Use_Case scope) that row, applies the Expected-Behavior precedence, and converts any lookup error into `RegistryUnavailable` instead of swallowing it. `get_user_role` now dispatches to `_resolve_role_from_registry` (enforcement: no enabled global row → `None`; enabled Use_Case row overrides; `RegistryUnavailable` propagates, never a Viewer downgrade) or `_resolve_role_legacy`, which keeps today's order **verbatim** in `_legacy_role` and adds `_log_would_deny` — a WARNING containing `would_deny` naming the user, scope, Claimed_Role, claimed username and the legacy role, which can never raise or change the decision. `get_user_permissions` / `has_permission` / `is_portal_admin` are unchanged in behaviour but documented as propagating `None` (→ 403) and letting `RegistryUnavailable` escape to the decorator (→ 500, task 2.2 wires the audit). Decisions recorded: (a) the open question the preservation suite deferred to this task — a Use_Case row with **no** global row — is settled **fail-closed**, i.e. the enabled *global* row is the provisioning record and a Use_Case row alone resolves to `None`, matching design.md's Glossary definition of Unprovisioned_Principal; in practice nothing is lost because the backfill (task 3.1) writes a global row for every enabled pool user; (b) a *disabled* or role-less **Use_Case** row is treated exactly as absent and falls back to the global row (Req 1.6 "denied exactly as an absent entry is"), while a disabled global row denies; (c) a missing `status` attribute means enabled (pre-spec Team Management rows carry none) and any other value denies; (d) a row naming an unknown role grants nothing; (e) in legacy mode a lookup failure still yields today's `Role.VIEWER` fallback so the flag-off tree is behaviour-preserving. Verification (flag off, the deployed default): preservation + `test_rbac_global_scope_jwt_role.py` + `test_build_rbac.py` + `test_shared_utils_user_identity.py` → **76 passed**; exploration unchanged at **12 failed, 1 passed** (as task 1.1 recorded); `test_audit_strict_helpers.py` 12, `test_dda_labeling_rbac_role.py` 17, `test_node_designer_rbac_audit.py` 109, `test_property_synthetic_rbac.py` 3, `test_workflow_rbac_audit.py` 103, `test_reject_unverifiable_audit_before_effect.py` 8, all ten `test_user_admin_*.py` (43/27/17/27/9/16/16/31/46/2), `test_property_complete_auditing.py` 1, plus `test_dda_labeling_job_deletion.py` 10, `test_camera_registry_api.py` 41 and three property files — all passed, no new failures. With `PORTAL_REGISTRY_ENFORCED=true` the enforcement branch behaves as designed: the preservation suite still passes **40/40** (the backfilled 6 accounts and Use_Case precedence survive enforcement) and the exploration suite flips from 12 to **5 failed, 8 passed** — every bug-condition-C1 case (the three unprovisioned PortalAdmin/DataScientist/UseCaseAdmin replays, the three `has_permission` cases, and claim-cannot-raise-the-registry-role) now denies; the 5 remaining failures are all attribution fields, which are task 2.2's scope. A throwaway pytest file exercised the branch matrix (RegistryUnavailable escaping under enforcement and being swallowed in legacy, disabled global/Use_Case rows, Use_Case-row-only denial, invalid role, flag parsing — 7 passed) and was deleted, since the permanent unit tests belong to task 2.5. Repo-root security preservation guards: 4 passed, 3 skipped (unchanged); no preservation-tracked file was touched, so no rebaseline was owed.

  - [x] 2.2 Attribution on both audit paths
    - `attribution_from(event, user)` (the single reader of
      `requestContext.identity.sourceIp` / `.userAgent`, following
      `quick_setup.py:166-169`); `identity=` parameter on `log_audit_event`,
      `record_audit_event_strict`, `finalize_audit_event`; `'unknown'` for
      missing claims; `sanitize_audit_details` still applied to `details`
    - `rbac_middleware.rbac_check` / `super_user_only`: catch
      `RegistryUnavailable` → 500 + audited `failure`; pass attribution into
      every audit call; signatures and envelopes unchanged
    - `build_jobs.py`: `requested_by_username` / `requested_by_email`
      alongside `requested_by`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 1.5_
    - **OUTCOME**: `shared_utils.py` gained the attribution layer —
      `ATTRIBUTION_FIELDS`, `UNKNOWN_ATTRIBUTION`, `attribution_from(event,
      user, usecase_id=None, identity_source=None)` (the single reader of
      `requestContext.identity.sourceIp`/`.userAgent`, never raising), the
      `_identifying_value` / `_authorizer_identity_inputs` / `_claimed_email`
      / `_identity_source_for` helpers, and `_attribution_attributes`, which
      writes all five Attribution_Fields as **top-level** attributes (so the
      `details` denylist still governs `details` alone and cannot redact
      them) on `log_audit_event`, `record_audit_event_strict` and
      `finalize_audit_event` — each now taking `identity=` / `event=`, with
      `event_id` shapes, the nine legacy keys, the 90-day ttl and
      log_audit_event's swallow-on-failure untouched. `rbac_middleware` now
      catches `RegistryUnavailable` in both `rbac_check` and
      `super_user_only` → 500 `{'error': 'Authorization check failed'}` plus
      an audited `result='failure'` under the new module constant
      `AUTHORIZATION_UNAVAILABLE_ACTION = 'authorization_unavailable'`, and
      threads `attribution_from(...)` into `unauthorized_access` /
      `unauthorized_super_user_access` (adding `claimed_role` to details,
      resolving the effective role once instead of twice); signatures,
      403/400/500 envelopes and `rbac_context` are unchanged.
      `build_jobs.submit_build` / `retry_build` stamp `requested_by_username`
      / `requested_by_email` beside `requested_by`, and all six of that
      handler's audit calls carry the request's attribution
      (`build_domain` stays pure — the fields are added in the handler).
      Decisions: (a) `identity_source` is derived from a best-effort global
      Portal_Identity lookup (`registry` / `absent`) and records `'unknown'`
      when the registry itself cannot be read, since an outage decided no
      role and Req 4.1 names only the two deciding values while 4.3 fixes
      `'unknown'` as the convention for what a request cannot supply;
      (b) `email` comes from the request's `email` claim only — the
      username/`sub` substitution `get_user_from_event` performs for
      display is **not** recorded as an email (it records `'unknown'`), so
      attribution is never fabricated; (c) every audit entry gets the five
      fields defaulted to `'unknown'`, so the call sites outside this task
      (user_admin = task 2.3, quick_setup, device_registrations) already
      satisfy Req 4.1 structurally; (d) **recorded repoint** in
      `test/backend-test/portal_builds/test_source_selection_preservation.py`
      — its exact-key oracle for a handler-created Build_Job became
      `SUBMITTED_JOB_RECORD_KEYS = JOB_RECORD_KEYS | {requested_by_username,
      requested_by_email}` with the old assertion preserved verbatim in an
      adjacent comment; `JOB_RECORD_KEYS` itself (the pure
      `create_build_jobs` record, used by the Req 7.3 null-index-key tests)
      is untouched; (e) seven `portal_builds` suites that install a *fake*
      `shared_utils` gained a faithful `attribution_from` stub, since
      `build_jobs` now imports it from the layer. Verification — exploration
      suite with `PORTAL_REGISTRY_ENFORCED=true`: **13 passed** (the whole
      incident suite is now green); with the flag off (the deployed
      default): **8 failed, 5 passed**, i.e. every bug-condition-C2
      attribution case flipped to green (task 2.1 left 12 failed / 1
      passed) and the 8 remaining failures are all C1 enforcement, which
      task 5 flips. Preservation suite **40 passed** in both flag modes;
      preservation + `test_build_rbac.py` + `test_rbac_global_scope_jwt_role.py`
      + `test_shared_utils_user_identity.py` + `test_audit_strict_helpers.py`
      in one process → **88 passed**. Per-file, all passing: the ten
      `test_user_admin_*.py` (2/27/43/17/27/31/16/9/46/16),
      `test_workflow_rbac_audit.py` 103, `test_node_designer_rbac_audit.py`
      109, `test_dda_labeling_rbac_role.py` 17,
      `test_reject_unverifiable_audit_before_effect.py` 8,
      `test_property_synthetic_rbac.py` 3,
      `test_property_complete_auditing.py` 1,
      `test_camera_registry_mutation_routes.py` 12, twelve
      `test_dda_labeling_*`/preview/labeling route suites,
      `test_property_preview_api_guards.py` 3,
      `test_property_gsam_preview_routes.py` 2,
      `test_property_labeling_work_stealing.py` 3,
      `test_property_prelabel_retry_route.py` 2,
      `test_preview_flow_integration.py` 6, `test_llm_sizing_integration.py`
      4, `test_vllm_engine_config_detail_and_audit.py` 4, and the four
      registration/quick-setup suites. A full per-file sweep of
      `test/backend-test/portal_builds` (45 files) is green except
      `test_build_diagnostic_api.py` (4) and
      `test_ref_aware_bootstrap_property.py` (4), both confirmed
      **pre-existing** by re-running them against a pristine
      `git archive HEAD` copy in /tmp (identical failures). Repo-root
      security preservation guards: 4 passed, 3 skipped (unchanged); no
      preservation-tracked file was touched, so no rebaseline was owed. A
      throwaway pytest module additionally proved the RegistryUnavailable →
      500 + audited-failure path on both decorators, `identity_source`
      `'unknown'` on an unreadable registry, the no-event/absent-claim
      defaults, and attribution on both strict helpers (6 passed) and was
      deleted — the permanent property/unit coverage belongs to tasks 2.4
      and 2.5.

  - [x] 2.3 The User Manager becomes the registry's writer
    - `user_admin.py`: create writes the global row (failure → 502 + audit
      `failure`, account inert by 1.1); role change updates `role`;
      disable/enable set `status`; delete removes the row; last-PortalAdmin
      guard counts registry rows instead of scanning Cognito attributes
    - `dda-portal-user-roles` rows gain `username`, `email`, `status`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_
    - **OUTCOME**: `edge-cv-portal/backend/functions/user_admin.py` is now
      the Portal_Identity registry's writer (only file changed; +496/−54).
      A new registry section adds `_sub_of` / `_resolve_registry_key` (the
      row key is the account's Cognito `sub`, read from the
      `admin_create_user` / `admin_get_user` response the handler already
      holds, with one `admin_get_user` fallback),
      `_put_registry_identity` / `_update_registry_identity` /
      `_delete_registry_identity`, and `_count_registry_portal_admins`.
      Create writes the global row (`role`, `username`, `email`,
      `status='enabled'`, `assigned_by`, `assigned_at`) right after the
      Cognito account exists and answers **502 `account was not
      provisioned`** with the partial state audited when that write fails
      (Req 3.1/3.2 — the account is inert because 1.1 denies it); role
      change upserts `role` + `username`/`email`/`status` (from the
      account's Cognito enabled state) and answers 502 `role change
      incomplete` on failure (3.3); disable/enable set `status`
      disabled/enabled and answer 502 `<verb> incomplete` on failure
      (3.4 — Cognito's own disable only blocks new sign-ins, the row is
      what stops an already-minted token); delete removes the global row
      first and then every per-Use_Case row, with a registry-removal
      failure reported through the existing partial-deletion 502 (whose
      message still matches the frontend's `/deleted/i` + `/not removed/i`
      classifier) — both cleanups are attempted so one failure no longer
      skips the other. All 14 audit calls in the module now carry
      `identity=attribution_from(event, acting_user)`, which task 2.2 had
      deferred here (Req 4.1/4.2). Decisions: (a) **the guard's count is
      flag-gated** — `_count_enabled_portal_admins` counts enabled global
      registry rows naming PortalAdmin under `PORTAL_REGISTRY_ENFORCED`
      and keeps today's pool scan (`_count_cognito_portal_admins`, body
      verbatim) while the flag is off, because the guard must count
      whatever currently decides privilege; counting the near-empty
      pre-backfill registry would otherwise reject every PortalAdmin role
      change/disable/delete as "the last admin"; (b) when Cognito reports
      no `sub` (unreachable in production; the fakes in the standing
      `test_user_admin_*` suites do it) the registry step is **skipped
      with an ERROR log** and recorded in the audit `details`
      (`registry_entry`) rather than keyed on an invented id — fail-closed
      and it keeps all ten `test_user_admin_*.py` suites passing
      unmodified; (c) deletion sweeps per-Use_Case rows too (Property 6's
      "registry ends consistent with Cognito"), global row first since it
      is the one carrying privilege; (d) `require_portal_admin` was
      **deliberately left alone** — it gates `/admin/*` on the
      `custom:role` **claim**, not on `RBACManager`, so under enforcement
      an unprovisioned `PortalAdmin`-claiming principal could still reach
      the User Manager routes: a real gap in design.md Decision 6's "one
      enforcement point", out of this task's scope (read path = 2.1), and
      flagged here for task 5.1/5.2 — switching it to
      `rbac_manager.is_portal_admin(...)` is behaviour-preserving while
      the flag is off (legacy step 1 still grants from the claim);
      (e) task 5.1's CDK grant must be **read+write** on
      `dda-portal-user-roles` for this handler (`grantReadWriteData`): the
      delete sweep needs `Query` and the enforced guard needs `Scan`.
      Verification (deployed default, flag off): the ten
      `test_user_admin_*.py` suites **234 passed**
      (43/27/27/17/16/16/9/46/31/2); preservation **40 passed**;
      exploration unchanged at **8 failed / 5 passed** (task 2.2's state —
      the 8 are C1 enforcement, flipped by task 5); `test_build_rbac.py`
      24, `test_rbac_global_scope_jwt_role.py` 5,
      `test_shared_utils_user_identity.py` 7, `test_audit_strict_helpers.py`
      12, and in one process preservation+exploration+those four →
      **93 passed, 8 failed** (only the intended exploration failures);
      `test_workflow_rbac_audit.py` 103, `test_node_designer_rbac_audit.py`
      109, `test_dda_labeling_rbac_role.py` 17,
      `test_dda_labeling_teams.py` 18,
      `test_reject_unverifiable_audit_before_effect.py` 8,
      `test_property_synthetic_rbac.py` 3,
      `test_property_complete_auditing.py` 1, `test_camera_registry_api.py`
      41, `test_camera_registry_mutation_routes.py` 12 — all passing.
      With `PORTAL_REGISTRY_ENFORCED=true`: exploration **13 passed** and
      preservation **40 passed** (the whole incident suite is green under
      enforcement), and five last-PortalAdmin guard tests fail **only in
      that mode** because they provision PortalAdmins in Cognito alone —
      `test_user_admin_change_role.py::TestLastPortalAdminGuard::{test_demotion_allowed_when_another_enabled_portal_admin_remains,test_guard_counts_across_pagination}`,
      `test_user_admin_disable_enable.py::TestLastPortalAdminGuard::{test_disable_allowed_when_another_enabled_portal_admin_remains,test_guard_counts_across_pagination}`,
      `test_user_admin_delete.py::TestLastPortalAdminGuard::test_delete_allowed_when_another_enabled_portal_admin_remains`;
      they need a recorded repoint (seed the enabled global rows) before
      task 5.2 flips the flag, deferred there rather than editing another
      spec's suites for a non-default configuration. A throwaway pytest
      module drove all five transitions against sub-carrying Cognito fakes
      and proved the rows written/updated/removed, the three new 502 paths,
      the guard count in both flag modes (pool 2 vs registry 2 with a
      disabled and a Use_Case row excluded), that a created account
      resolves its registry role under enforcement while an unprovisioned
      twin resolves `None`, that a disabled row denies, and that audit rows
      carry the request's `username`/`email`/`source_ip`/`user_agent` —
      **14 passed**, then deleted (permanent coverage is tasks 2.4/2.5).
      Pre-existing and unrelated: `test_deployment_preflight_preservation.py::TestSourceTreeUntouched`
      (2 failures, the src/ no-diff oracle). Repo-root security
      preservation guards: 4 passed, 3 skipped; no preservation-tracked
      file was touched, so no rebaseline was owed.

  - [x]* 2.4 Property tests for the resolution and attribution changes
    - `test_property_portal_registry_roles.py` — **Property 1** (no
      Claimed_Role grants anything), **Property 2** (Effective_Role is
      exactly the registry's), **Property 3** (absent vs unavailable)
      — _Validates: 1.1-1.6_
    - `test_property_audit_attribution.py` — **Property 5** — _Validates: 4.1, 4.2, 4.3, 4.6_
    - `test_property_user_manager_registry.py` — **Property 6** — _Validates: 3.1, 3.3, 3.4_
    - **OUTCOME**: Added the three property suites (test files only — no
      production file touched, `git status` shows exactly three new
      files): `tests/test_property_portal_registry_roles.py`
      (Properties 1-3), `tests/test_property_audit_attribution.py`
      (Property 5) and `tests/test_property_user_manager_registry.py`
      (Property 6) — **5 property tests, each `@settings(max_examples=100)`
      and each confirmed at "100 passing, 0 failing" via
      `--hypothesis-show-statistics`**, tagged with the required
      `Feature: portal-jwt-role-privilege-escalation, Property {n}: {text}`
      header. All three modules exercise the real `rbac_check` /
      `super_user_only` / `RBACManager` / audit helpers / `user_admin`
      handler on the conftest moto stack, turn `PORTAL_REGISTRY_ENFORCED`
      on for their own module only (the properties are statements about
      the enforced mode) and restore it on teardown — verified, because
      the exploration suite still shows flag-off behaviour when run in the
      same process after them. Every expectation is a hand-written oracle
      from design.md (`expected_effective_role`, `expected_attribution`),
      never a call back into the implementation; Property 1 picks its
      permission modulo the real `Permission` enum so a permission added
      later is covered without editing the file. Decisions recorded:
      (a) **Property 3's deny-modes are split by what actually decided** —
      no row / disabled global row / Use_Case-row-only record
      `identity_source='absent'`, while an *enabled* global row naming a
      value that is not a `Role` records `'registry'` (a row was found and
      granted nothing; calling that 'absent' would be false), and an
      unreadable registry records `'unknown'` — which is precisely what
      makes absent and unavailable distinguishable in the audit log; the
      mapping is spelled out in `ABSENT_MODE_IDENTITY_SOURCE` and was
      found by the test failing on the first run. The permission it
      exercises is `usecases:view`, which **Viewer holds**, so the
      pre-fix Viewer fallback would authorize and fail the test rather
      than slip through. (b) **Property 5's denylist arm is asserted
      where production applies it**: both strict helpers are driven with
      generated denylisted keys (`password`/`*_hash`/`verifier`/`temp*`,
      nested) plus a generated secret value and must redact all of them,
      while the `log_audit_event` paths are checked the complementary way
      — their `details` keys must be a subset of the handler's own fixed
      metadata set and the secret planted in the request body and query
      string must not appear anywhere in the written item. **Recorded
      gap**: `log_audit_event` itself never calls
      `sanitize_audit_details` (bugfix.md quotes its body verbatim, and
      Req 4.6 preserves the *existing* denylist); no production call site
      passes caller-supplied details to it today, so nothing leaks, but a
      future caller could — flagged for task 7 rather than changing
      production code in a test-only task. (c) The registry-failure
      injection is restored **inside each Hypothesis example** via a
      context manager: a function-scoped `monkeypatch` is set up once for
      the whole `@given` run and leaked into later examples, which
      produced a fabricated failure until fixed. (d) Property 6 drives
      generated create/role-change/disable/enable/delete sequences (length
      1-6 over three account names, so double-creates, ops on deleted
      accounts and create-after-delete all occur) through the real
      `user_admin.handler` against a stateful in-memory `FakePool` that
      reports each account's `sub`, then asserts the registry matches the
      pool (role, `status`, `username`, `email`), that a deleted account
      leaves **no** rows — including a Team Management per-Use_Case grant
      — and that every disabled or deleted principal resolves no role
      while every enabled one resolves exactly its registry role; an
      enabled PortalAdmin "keeper" is seeded in both pool and registry so
      the last-PortalAdmin guard never spuriously rejects a sequence (the
      guard's own arithmetic stays task 2.5's). **Non-vacuousness proven**
      by a throwaway mutation harness that subclassed all five property
      tests with one production behaviour broken each (registry
      resolution replaced by the legacy claim path; `_attribution_
      attributes` stubbed to all-'unknown'; the User Manager's registry
      update/delete stubbed to no-ops): **all five mutants failed** while
      the five unmutated copies passed, and the harness was then deleted.
      Verification: the three files alone **5 passed** (~150 s); in one
      process with preservation + exploration + `test_build_rbac.py` +
      `test_rbac_global_scope_jwt_role.py` +
      `test_shared_utils_user_identity.py` + `test_audit_strict_helpers.py`
      → **98 passed, 8 failed**, the 8 being exactly the exploration
      suite's C1 enforcement cases task 2.3 recorded as expected with the
      flag off (task 5 flips them); Property 6 with
      `test_user_admin_{change_role,disable_enable,delete,create,listing}.py`
      → **124 passed** (all last-PortalAdmin guard tests included, so the
      flag restore is clean); `test_user_admin_{scaffold,set_password,
      forgot_password,edge_sync,audit_finalize_preservation}.py` +
      `test_workflow_rbac_audit.py` + `test_node_designer_rbac_audit.py` +
      `test_dda_labeling_rbac_role.py` + `test_property_synthetic_rbac.py`
      + `test_property_complete_auditing.py` → **343 passed**;
      `test_reject_unverifiable_audit_before_effect.py` **8 passed
      standalone** (it errors when collected alongside other suites in one
      process — reproduced with `test_workflow_rbac_audit.py` alone and
      none of this task's files, i.e. pre-existing cross-file
      interference, not mine). Repo-root security preservation guards:
      **4 passed, 3 skipped** (unchanged); no preservation-tracked file
      was changed, so no rebaseline was owed. Deferred: Property 4 (the
      backfill) is task 3.2's, and the unit tests of the same surface are
      task 2.5's.

  - [x]* 2.5 Unit tests
    - `_lookup_identity` branches; both flag modes; the `would_deny` WARNING
      (caplog); `attribution_from` with partial/absent claims; the User
      Manager's five transitions; the last-PortalAdmin count
    - _Requirements: 1.5, 2.4, 3.5, 4.3_
    - **OUTCOME**: Added two test-only files (`git status` shows exactly
      two new files; no production file touched):
      `tests/test_portal_registry_units.py` (**103 tests**) covering the
      shared layer — the enforcement flag's accepted spellings and its
      fail-safe default, `_identity_is_enabled`/`_identity_role`, every
      `_lookup_identity` branch of design.md's Expected Behavior
      precedence (global row, missing/odd `status`, Use_Case override and
      its non-leakage to `global`/another Use_Case, disabled or
      unusable-role Use_Case row falling back, Use_Case-row-only denying,
      unidentifiable caller resolving without a read) plus both failure
      conversions to `RegistryUnavailable`, `get_user_role` in **both**
      flag modes on the same rows (including one test that flips only the
      flag and gets opposite answers), the `would_deny` dry-run WARNING
      via caplog (content, silence when provisioned, `undetermined` on an
      unreadable registry, none under enforcement) and `attribution_from`
      with full/partial/absent claims; and
      `tests/test_user_manager_registry_units.py` (**34 tests**) covering
      the five User Manager transitions' row content and every failure
      branch (the three new 502s — `account was not provisioned`,
      `role change incomplete`, `<verb> incomplete` — the registry arm of
      `partial deletion`, the fail-closed no-`sub` skip, the no-op
      disable/enable writing nothing, the delete key being resolved
      before the Cognito delete) and the last-PortalAdmin count
      (`_count_registry_portal_admins` arithmetic incl. pagination, the
      flag gate in `_count_enabled_portal_admins`, and the guard's three
      transitions in both modes). Decisions: (a) both modules read the
      Portal_Identity registry from a **table private to the module**
      (`test-portal-registry-units-roles` /
      `test-user-manager-units-roles`, wired in by monkeypatching
      `shared_utils.USER_ROLES_TABLE` / `user_admin.USER_ROLES_TABLE`,
      which production resolves at call time) and empty it per test —
      without that the last-PortalAdmin **scan** would see rows other
      suites seed in the shared `test-user-roles` table, in both
      directions; (b) the legacy-mode class deliberately asserts today's
      pre-fix claim behaviour (a `PortalAdmin` claim with no row still
      grants) and says so in its docstring, since that is what makes the
      flag-off tree provably unchanged now and is inverted by task 5;
      (c) every flag-sensitive test names its mode via a fixture so
      ambient environment cannot decide the outcome (one delete test
      failed on the first run for exactly that reason and was pinned).
      **Non-vacuousness proven** by seven throwaway mutations of the real
      production files (`get_user_role` ignoring the flag;
      `_lookup_identity` swallowing failures; `_log_would_deny` removed;
      `_put_registry_identity` / `_delete_registry_identity` no-ops; the
      guard's flag gate removed; disable writing `enabled` unconditionally)
      — **every one was caught**, and both files were restored
      byte-identically (`diff -q` clean). Verification: the two files
      alone **103 passed** / **34 passed**; in one process with
      preservation + exploration + `test_build_rbac.py` +
      `test_rbac_global_scope_jwt_role.py` +
      `test_shared_utils_user_identity.py` + `test_audit_strict_helpers.py`
      → **230 passed, 8 failed**, the 8 being exactly the exploration
      suite's C1 enforcement cases tasks 2.2/2.3 recorded as expected with
      the flag off; with all ten `test_user_admin_*.py` → **268 passed**;
      with the three task-2.4 property suites → **142 passed** (~164 s);
      with `test_workflow_rbac_audit.py` +
      `test_node_designer_rbac_audit.py` + `test_dda_labeling_rbac_role.py`
      + `test_property_synthetic_rbac.py` + `test_property_complete_auditing.py`
      → **370 passed, 1 error**, the error being the pre-existing
      `test_property_complete_auditing.py` setup interference (reproduced
      with `test_node_designer_rbac_audit.py` alone and none of my files;
      it passes standalone). Repo-root security preservation guards: **4
      passed, 3 skipped**; no preservation-tracked file was changed, so no
      rebaseline was owed. Deferred: nothing from this task — the backfill
      units are task 3.1/3.2's and the CDK assertions are task 6.3's.

- [x] 3. Backfill the registry from the current pool
  - [x] 3.1 `edge-cv-portal/backfill_portal_registry.py`
    - Dry-run by default; paginate `list_users`; per **enabled** user write
      the global row only when absent (conditional write) carrying
      `role` (= `custom:role` or `Viewer`), `username`, `email`,
      `status='enabled'`, `assigned_by='backfill'`; skip disabled users;
      print a per-user plan and a summary; re-runnable
    - _Requirements: 2.1, 2.2_
    - **OUTCOME**: Added `edge-cv-portal/backfill_portal_registry.py`
      (executable, the run's only new file; no existing file touched):
      it paginates `cognito-idp list_users`, classifies each account with
      the pure `plan_entry` (`create` / `exists` / `skip-disabled` /
      `skip-no-sub`), and for an enabled account writes the global
      Portal_Identity row — `role`, `username`, `email`,
      `status='enabled'`, `assigned_by='backfill'`, `assigned_at` — in the
      exact shape `user_admin._put_registry_identity` writes, via a
      **conditional** `put_item` (`attribute_not_exists(user_id) AND
      attribute_not_exists(usecase_id)`) so an existing row is never
      overwritten (Req 2.2). It is **dry run unless `--apply`** is passed,
      prints a reviewable per-account plan line plus a counted summary
      (scanned / created / already present / skipped-disabled /
      skipped-no-sub / errors), and exits 0 / 1 (a per-account failure) /
      2 (no `--user-pool-id`); `run_backfill(...)` takes injectable
      `cognito` / `dynamodb_resource` / `out` so task 3.2 can drive it
      under moto. Decisions: (a) an **unrecognized or absent
      `custom:role` backfills `Viewer`**, because that is precisely the
      account's effective role today (the legacy step-4 fallthrough), so
      the flip changes nobody's access (Req 2.3); (b) an account whose
      Cognito record names no `sub` is **skipped with `skip-no-sub`**
      rather than keyed on an invented id, matching task 2.3's
      fail-closed rule; (c) the script is deliberately
      **self-contained** (literal `GLOBAL_SCOPE` / `STATUS_ENABLED` /
      `VALID_ROLES` mirroring the shared layer) so an operator can run it
      with nothing but boto3 — `shared_utils` builds AWS clients at
      import — and a comment marks `VALID_ROLES` as the value task 3.2
      should pin against `shared_utils.Role`; (d) only
      `usecase_id='global'` rows are touched, leaving Team Management's
      per-Use_Case grants alone; (e) a row that appears between the read
      and the conditional write is reported as `exists`, identical to a
      second run. Verification: a throwaway moto module (cognito-idp +
      DynamoDB) proved dry-run writes nothing while planning every
      enabled account, apply writes one global row per enabled account
      with the full attribute set and skips the disabled one, a second
      apply is byte-identical (idempotent) and leaves both a
      portal-assigned `Viewer` row under a `PortalAdmin` claim and a
      per-Use_Case grant untouched (the claim divergence is flagged in
      the plan), `list_users` pagination at `page_size=2` covers all 7
      accounts, `main([])` dry-runs and `main(['--apply'])` writes, a
      write `ClientError` is reported and exits non-zero, `VALID_ROLES`
      equals `{r.value for r in shared_utils.Role}`, and — the point of
      the exercise — a backfilled row makes the account resolve
      `Role.PORTAL_ADMIN` under `PORTAL_REGISTRY_ENFORCED=true` while an
      unprovisioned `sub` resolves `None`: **9 passed**, then deleted
      (permanent coverage is task 3.2's Property 4). Regression: the
      spec's suites in one process — preservation, exploration, both unit
      files, `test_build_rbac.py`, `test_rbac_global_scope_jwt_role.py`,
      `test_shared_utils_user_identity.py`, `test_audit_strict_helpers.py`
      → **230 passed, 8 failed**, the 8 being exactly the exploration
      suite's C1 enforcement cases task 2.5 recorded as expected with the
      flag off (task 5 flips them). Repo-root security preservation
      guards: **4 passed, 3 skipped** (unchanged); no preservation-tracked
      file was changed, so no rebaseline was owed. Deferred: Property 4
      (3.2), the regression repoint (3.3), and the real-account dry
      run/apply against the portal pool (5.2, which this run must not
      touch).

  - [x]* 3.2 Property test for the backfill
    - `test_property_portal_registry_backfill.py` — **Property 4**
      (idempotent, never overwrites, skips disabled, preserves access)
      — _Validates: 2.1, 2.2, 2.3_
    - **OUTCOME**: Added
      `edge-cv-portal/backend/tests/test_property_portal_registry_backfill.py`
      — **Property 4** as one Hypothesis test at
      `@settings(max_examples=100)`, confirmed at "100 passing, 0 failing"
      via `--hypothesis-show-statistics`, tagged
      `Feature: portal-jwt-role-privilege-escalation, Property 4: ...`,
      plus 4 anchor tests (5 tests, ~53 s). Each example builds a **real
      moto `cognito-idp` user pool** (per-example, `admin_create_user` /
      `admin_disable_user`, real `list_users` pagination driven by a
      generated `page_size` 1-4) and a generated registry pre-state
      (none / same-role / lower / higher / disabled global row /
      Team-Management-grant-only), then: measures each account's access
      with the **real legacy resolution** (flag off), asserts the dry run
      writes nothing and plans the right action per account, applies,
      checks every written row attribute-by-attribute against a
      hand-written oracle, re-measures under **enforcement on**, and
      applies a second time asserting `created == 0`, every action in
      {exists, skip-disabled, skip-no-sub} and a byte-identical registry
      snapshot. Decisions: (a) **a production bug was found and fixed** —
      `role_for` stripped whitespace before matching, so an account whose
      `custom:role` is `' PortalAdmin'` (effective role **Viewer** today,
      because resolution does `Role(claim)`) would have been backfilled as
      **PortalAdmin**, an escalation at the flip; the oracle matches role
      names exactly and caught it, and `role_for` now matches exactly
      (the only production edit, in the same untracked task-3.1 script,
      with the reason in its docstring); (b) three shapes are **excluded
      from "access is preserved" and asserted positively instead**, each
      because a requirement overrides 2.3: an enabled pre-existing row
      wins over a higher claim (Req 1.2/1.4), a disabled row denies like
      an absent one (Req 1.6/3.4), and a disabled Cognito account gets no
      row and is denied (Decision 5) — all spelled out in the module
      docstring; (c) `has_sub=False` is modelled by `_SubStrippingPool`, a
      thin wrapper over the real client, since moto (like Cognito) always
      reports `sub` and the `skip-no-sub` branch is what stops a row being
      keyed on an invented id; (d) the registry is a module-private table
      (`test-portal-registry-backfill-roles`) wired in by re-binding
      `shared_utils.USER_ROLES_TABLE`, and `PORTAL_REGISTRY_ENFORCED` is
      flipped only inside a per-example context manager (the task-2.4
      leak lesson) — verified, since the exploration suite still shows
      flag-off behaviour when run after this file in the same process;
      (e) three anchors pin the script's self-contained literals
      (`VALID_ROLES` == `{r.value for r in Role}`, `GLOBAL_SCOPE`,
      `STATUS_ENABLED`, `DEFAULT_ROLE`) so a role added to the enum cannot
      be silently backfilled as Viewer, and a fourth anchor covers the
      conditional write's **only unique** contribution — the
      read-then-write race, driven by a blind-`get_item` table proxy,
      because the plan's read guard otherwise masks it. **Non-vacuousness
      proven** by seven throwaway mutations of the production script:
      restoring `.strip()`, blinding the read guard, blinding the read
      guard *and* dropping the `ConditionExpression`, not skipping
      disabled accounts, omitting `username`/`email` from the row,
      omitting `status`, and ignoring the pagination token — **all seven
      caught** (the bare `ConditionExpression` removal alone is caught by
      the race anchor, not by the property, which is why that anchor
      exists); the script was restored byte-identically (`diff -q` clean)
      apart from the intended `role_for` fix, and still compiles as a CLI.
      Verification: the new file alone **5 passed**; with preservation +
      exploration + both unit files + `test_build_rbac.py` +
      `test_rbac_global_scope_jwt_role.py` +
      `test_shared_utils_user_identity.py` + `test_audit_strict_helpers.py`
      → **235 passed, 8 failed**, the 8 being exactly the exploration
      suite's C1 enforcement cases tasks 2.5/3.1 recorded as expected with
      the flag off (task 5 flips them); with the three task-2.4 property
      suites → **10 passed** (~200 s, no interference). Repo-root security
      preservation guards: **4 passed, 3 skipped** (unchanged); no
      preservation-tracked file was changed, so no rebaseline was owed.
      Deferred: the regression repoint (3.3) and the real-pool dry
      run/apply (5.2), which this run must not touch.

  - [x] 3.3 Repoint the conflicting regression suite, recorded
    - `test_rbac_global_scope_jwt_role.py` asserts a JWT `PortalAdmin` with
      **no** registry rows is authorized — the inverse of Requirement 1.1.
      Seed the registry row in each case so the original intent (the
      extracted `user_info` reaches role resolution; build routes do not
      403 spuriously) still holds, keep the assertions, and preserve the old
      ones verbatim in an adjacent comment
    - Re-run `test_build_rbac.py`, `test_shared_utils_user_identity.py` and
      the `test_user_admin_*.py` set unmodified
    - _Requirements: 7.4_
    - **OUTCOME**: Repointed
      `edge-cv-portal/backend/tests/test_rbac_global_scope_jwt_role.py`
      (the run's only changed file — `git diff --stat` shows it alone;
      no production code touched): every one of the five cases now
      **provisions** its principal with a global Portal_Identity row
      (`usecase_id='global'`, `role` = the claim's role, plus
      `username`/`email`/`status='enabled'`, written exactly as task 3.1's
      backfill and task 2.3's `_put_registry_identity` write it) and keeps
      its original assertion (200/200/403 + `Insufficient permissions`
      envelope /200/403) verbatim; each test's **superseded body is
      recorded verbatim** in its docstring under `REPOINTED (task 3.3).
      SUPERSEDED body, recorded verbatim::`, both class docstrings' old
      text is quoted, and the module docstring states the struck premise
      ("the users below have NO dda-portal-user-roles rows, only the JWT
      claim") and why it cannot coexist with Requirement 1.1. Decisions:
      (a) every case is **parametrized over both flag modes**
      (`registry_mode` = legacy / enforced, set through `monkeypatch` so
      it cannot leak — verified, the exploration suite still shows
      flag-off behaviour when run after this file in the same process),
      because "a provisioned principal is not spuriously denied" must hold
      today *and* after task 5 flips the flag; (b) the Viewer cases are
      provisioned as **Viewer** so their 403 is a permission decision
      rather than the by-product of an absent entry, preserving the "must
      not over-grant" intent; (c) added
      **`TestUserInfoStillThreaded`** (2 tests), which pins the original
      bug's literal mechanism — every `rbac_manager` call made by
      `rbac_check` / `super_user_only` receives the dict
      `get_user_from_event` extracted as `user_info`, at the `'global'`
      scope — via a recording delegate around the real `RBACManager`,
      because once the registry supplies the role an outcome assertion can
      no longer distinguish "user_info reached resolution" from "user_info
      was dropped again"; the scope assertion excludes `is_portal_admin`,
      which takes the user alone. **Non-vacuousness proven** by two
      throwaway mutations, both restored byte-identically (`diff -q`
      clean): reverting the fix in production
      (`user_info=user` → `user_info=None`, 7 occurrences in
      `rbac_middleware.py`) fails **5** tests in *both* modes (the
      DataScientist global-scope 403 plus all four threading tests), and
      removing the seeded rows from the tests fails **5** enforced-mode
      tests — so the suite catches both the original build-fleet 403 and
      an unprovisioned principal. Verification: the file **14 passed**
      (was 5); unmodified re-runs `test_build_rbac.py` **24**,
      `test_shared_utils_user_identity.py` **7**,
      `test_audit_strict_helpers.py` **12**, and all ten
      `test_user_admin_*.py` **234 passed**
      (2/27/43/17/27/31/16/9/46/16); in one process with preservation +
      exploration + both unit files + build_rbac + user_identity +
      audit_strict → **239 passed, 8 failed**, the 8 being exactly the
      exploration suite's C1 enforcement cases tasks 2.5/3.1/3.2 recorded
      as expected with the flag off (task 5 flips them). Repo-root
      security preservation guards: **4 passed, 3 skipped** (unchanged);
      no preservation-tracked file was changed, so no rebaseline was owed.
      Nothing deferred from this task.

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - **OUTCOME**: Verification-only checkpoint — **no file was created or
    modified** (`git status` unchanged from task 3.3's end), so no
    preservation rebaseline was owed and the two repo-root guards are
    **4 passed, 3 skipped** as before. Portal backend, flag off (the
    deployed default): the spec's core group — preservation 40,
    exploration 13, `test_portal_registry_units.py` 103,
    `test_user_manager_registry_units.py` 34, `test_build_rbac.py` 24,
    `test_rbac_global_scope_jwt_role.py` 14,
    `test_shared_utils_user_identity.py` 7,
    `test_audit_strict_helpers.py` 12 — in one process **239 passed,
    8 failed**, the 8 being exactly the exploration suite's bug-condition-C1
    cases every task from 2.2 on recorded as expected until task 5 flips the
    flag; the four property suites (Properties 1-6) **10 passed** (~204 s);
    all ten `test_user_admin_*.py` **234 passed**; and a **per-file sweep of
    the remaining 87 test files that import `shared_utils` /
    `rbac_middleware` / `user_admin`** (600 s cap each) → **1013 passed with
    exactly one non-green file**, the pre-existing
    `test_deployment_preflight_preservation.py::TestSourceTreeUntouched`
    pair (another spec's oracle demanding a clean `src/`). Device
    `test/backend-test/workflow_engine` **1495 passed, 3 skipped**; the full
    `security/preservation` suite **139 passed, 6 skipped, 1 failed** (the
    known stale `EdgeCVPortalComputeStack` IAM synth baseline); infrastructure
    `npm test` **177 passed / 19 suites, 2 snapshots** and `npm run build`
    clean; frontend `npx tsc --noEmit` clean (this spec touches no frontend
    file, so no vitest run was owed). Decisions: (a) the 8 exploration
    failures are **left red on purpose** — flipping enforcement is task 5's
    only job — and were proven to be flag-state and nothing else by re-running
    exploration + preservation + `test_rbac_global_scope_jwt_role.py` with
    `PORTAL_REGISTRY_ENFORCED=true` **as an environment variable only** (no
    edit): **67 passed**, i.e. all 8 flip green while the 40 preservation
    invariants and the repointed regression suite hold; (b) that same
    enforced-mode dry run surfaced **12 tests in suites task 3.3 required to
    stay unmodified** that encode the pre-fix premise and fail once
    enforcement is ambient — 9 `test_build_rbac.py::TestGrantMatrix`
    role/permission cases (claim-only principals with no registry row) and 3
    last-PortalAdmin guard cases (`test_user_admin_change_role.py` ×2,
    `test_user_admin_delete.py` ×1) that seed Cognito but no registry rows, so
    the registry-backed count sees zero admins. They are green at the deployed
    default and their behaviour is already covered in both modes by
    `test_portal_registry_units.py` / `test_user_manager_registry_units.py`, so
    I did **not** touch them here; this is **recorded as work for task 5.1/7**
    (seed a global Portal_Identity row per principal, exactly as 3.3 did) and
    is the only deferred item from this checkpoint. No question needed
    answering: every red line is either on the pre-existing list or the
    intended flag-off state.

- [ ] 5. Enable enforcement (the actual fix goes live)
  - [x] 5.1 Wire the flag and deploy with it off
    - `PORTAL_REGISTRY_ENFORCED` on every handler environment in
      `compute-stack.ts` and `build-fleet-stack.ts`; the User Manager role's
      `dda-portal-user-roles` write grant
    - Deploy, then read the `would_deny` WARNINGs from real traffic to
      confirm the backfill is complete
    - _Requirements: 2.4_
    - **OUTCOME**: Wired the flag through CDK with it **off**: new
      `portalRegistryEnforced(contextValue)` in
      `infrastructure/lib/context-helpers.ts` resolves the
      `portalRegistryEnforced` CDK context value **default-OFF** and
      normalizes it to the canonical `'true'`/`'false'`, accepting exactly
      the truthy set `shared_utils._ENFORCEMENT_TRUE_VALUES` accepts
      (`1/true/yes/on/enabled`, trimmed, case-insensitive) so a value that
      reads "on" in context can never deploy as "off" in the Lambda
      (truth table checked: `undefined/null/''/false/'false'/'0'/'no'/
      'maybe'/{}` → `'false'`; `true/'true'/'TRUE'/' True '/'1'/'yes'/'on'/
      'enabled'/'ENABLED'` → `'true'`). It is added to the shared
      `lambdaEnvironment` of `compute-stack.ts` and `build-fleet-stack.ts`
      and — a **deliberate expansion beyond the two files the task names** —
      to `node-designer-stack.ts` and `synthetic-data-stack.ts`, which are
      the only other stacks whose handlers carry `USER_ROLES_TABLE` and
      therefore resolve privilege through the same `shared_utils` read path:
      leaving them out would keep granting from the `custom:role` claim on
      those routes after 5.2's flip, i.e. a partial fix. Synth verification
      (throwaway jest suite, 5 passed, then deleted; the permanent
      assertions are task 6.3's) confirms **54 role-resolving handlers**
      carry the flag — Compute 41, BuildFleet 5, NodeDesigner 7,
      SyntheticData 1 — every one at `'false'` on a default synth, every one
      at `'true'` with `-c portalRegistryEnforced=true`, and that the flag is
      the **only** environment difference between the two synths (no
      function added, removed or otherwise changed). The User Manager's
      registry grant was found **already sufficient and left as is**:
      `createLambdaRole` grants `grantReadWriteData` on
      `dda-portal-user-roles`, and the synthesized UserAdmin role carries
      `dynamodb:GetItem/Query/Scan/PutItem/UpdateItem/DeleteItem` on that
      table (Query for the per-Use_Case delete sweep, Scan for the enforced
      last-PortalAdmin count, per task 2.3's note (e)); a comment now records
      that contract, and narrowing the *other* handlers to read-only is
      explicitly out of scope (`user_management.py` and Team Management also
      write role rows). `deploy-infrastructure.sh` gained a pass-through so
      5.2 can flip with the standard script
      (`PORTAL_REGISTRY_ENFORCED=true ./deploy-infrastructure.sh` appends
      `-c portalRegistryEnforced=...`; unset deploys enforcement off), and
      `cdk.json` context was deliberately **not** given the key, so every
      flag-less deploy stays off. Two **recorded repoints** were owed
      because the new key lands in exact-key environment oracles of other
      specs, both with the superseded expectation preserved verbatim in an
      adjacent comment and still forbidding any other addition/removal:
      `test/grounded-sam-worker-infra.test.ts` (DdaAutolabelWorker key set)
      and `test/synthetic-imaging-layer-empty.test.ts` (SyntheticDataHandler
      key set). Verification: `npm run build` clean and `npm test`
      **177 passed / 19 suites / 2 snapshots** (was 2 failed before the
      repoints); portal backend core group (preservation, exploration, both
      unit files, `test_build_rbac.py`,
      `test_rbac_global_scope_jwt_role.py`,
      `test_shared_utils_user_identity.py`, `test_audit_strict_helpers.py`)
      → **239 passed, 8 failed**, byte-for-byte the checkpoint's state (the
      8 are the exploration suite's C1 enforcement cases, which 5.2 flips);
      `test/backend-test/security/preservation` **139 passed, 6 skipped,
      1 failed** — the known stale `EdgeCVPortalComputeStack` IAM synth
      baseline, whose drift is in pre-existing uncommitted
      `compute-stack.ts`/`storage-stack.ts` work from another spec, not from
      this change (no IAM statement was added or moved) — and the two
      repo-root guards **4 passed, 3 skipped**. No Python and no
      preservation-tracked file was touched, so no rebaseline was owed.
      **Deferred, and required before 5.2**: the task's second bullet — the
      actual `cdk deploy` with the flag off and the reading of the
      `would_deny` WARNINGs from real traffic — was **not** performed; this
      run is forbidden from touching AWS. An operator must deploy the
      flag-off stacks, confirm every portal handler shows
      `PORTAL_REGISTRY_ENFORCED=false`, and mine the `would_deny` WARNINGs
      (they name the user, scope, Claimed_Role and legacy role) to size the
      backfill gap before task 5.2 applies the backfill and flips.

  - [ ] 5.2 Run the backfill against the portal account, then flip the flag
    - Dry-run, review the plan against the 6 known accounts, apply, verify
      each account resolves its expected role, set the flag true, redeploy,
      re-verify — including that the bootstrap `admin` can still reach
      `/admin/users` and that `POST /builds` still works for its operators
    - Confirm the incident sequence is now denied: create a throwaway pool
      user with `custom:role=PortalAdmin` and no registry row, present its
      token to `POST /builds`, expect 403, then delete it and confirm the
      denial's audit row names it
    - _Requirements: 1.1, 2.3, 7.1, 7.2, 7.3_

- [x] 6. Defense in depth and detection
  - [x] 6.1 Remove the non-SRP auth flows from the portal app client
    - `auth-stack.ts`: drop `userPassword` and `adminUserPassword`, leaving
      SRP; verify browser sign-in, the new-password challenge and
      forgot-password still work (no code pins `authFlowType`, verified)
    - _Requirements: 5.1, 5.2_
    - **OUTCOME**: `edge-cv-portal/infrastructure/lib/auth-stack.ts` is the
      only changed file (`git diff --stat`: +17/−2): the app client's
      `authFlows` is now `{ userSrp: true }`, so the synthesized
      `ExplicitAuthFlows` is exactly `['ALLOW_USER_SRP_AUTH',
      'ALLOW_REFRESH_TOKEN_AUTH']` — `ALLOW_USER_PASSWORD_AUTH` and
      `ALLOW_ADMIN_USER_PASSWORD_AUTH` (the "Enable ADMIN_NO_SRP_AUTH for
      testing" line) are gone (Req 5.1). An adjacent comment records that
      this is **defense in depth only** (an actor with
      `cognito-idp:UpdateUserPoolClient` can re-enable it; the registry
      enforced in the shared RBAC layer is the actual control), pointing at
      the spec, design.md Decision 8 and the operator runbook task 6.4 will
      write. Req 5.2 was verified **statically**, which is as far as this
      run can go (it is forbidden from touching AWS, so no real browser
      sign-in was performed): no repo source pins an auth flow — a
      repo-wide grep for `authFlowType` / `USER_PASSWORD_AUTH` /
      `ADMIN_NO_SRP` / `AuthFlow` / `initiate-auth` outside `cdk.out.bak-*`
      and `node_modules` finds only `backend/functions/auth.py`'s
      `AuthFlow='REFRESH_TOKEN_AUTH'`, which CDK still permits because it
      always appends `ALLOW_REFRESH_TOKEN_AUTH` (asserted in the synth) —
      and the installed dependency proves the browser path: in
      `aws-amplify@6.15.8`, `signIn()`'s switch on
      `input.options?.authFlowType` falls through `default:` to
      `signInWithSRP` (`authFlowType: 'USER_SRP_AUTH'`), and
      `AuthContext.tsx:118` calls `signIn({ username, password })` with no
      options; the new-password challenge (`confirmSignIn` →
      `RespondToAuthChallenge NEW_PASSWORD_REQUIRED`) and
      forgot-password/reset (`resetPassword` / `confirmResetPassword` →
      `ForgotPassword` / `ConfirmForgotPassword`) are not governed by
      `ExplicitAuthFlows` at all. Decisions: (a) the permanent CDK
      assertions were **not** added here — task 6.3 owns them — so the
      synth was proven by a throwaway jest suite
      (`test/zz-throwaway-auth-flows.test.ts`, 4 tests: the exact flow set
      on the default and SSO variants, the client's full property-key set
      and every other client value, and the pool/domain/outputs/password
      policy/`custom:role` schema unchanged) which passed and was then
      deleted along with its compiled artifacts; **non-vacuousness came
      free** — its first run resolved the stale compiled `lib/auth-stack.js`
      (jest/ts-jest resolves `.js` before `.ts`, so `npm run build` must
      precede `npm test` in this package) and therefore ran against the
      *unfixed* client, failing exactly the two flow assertions with the
      two password flows present; (b) `AdminSetUserPassword` remains
      available to the portal's own User Manager (the frontend's
      `setAdminUserPassword` path is untouched) — a permanent password set
      that way is still usable, just only through SRP. Verification:
      infrastructure `npm run build` clean and `npm test` **177 passed / 19
      suites / 2 snapshots** (unchanged from task 5.1; no existing infra
      test or snapshot pinned the auth flows); repo-root security
      preservation guards **4 passed, 3 skipped**, and the full
      `test/backend-test/security/preservation` suite (with
      `PYTHONPATH=src/backend:test/backend-test`) **139 passed, 6 skipped,
      1 failed** — byte-for-byte task 5.1's state, the failure being the
      known stale `EdgeCVPortalComputeStack` IAM synth baseline, untouched
      by an auth-stack-only change. No Python changed, so the portal
      backend could not regress; as a sanity check `test_build_rbac.py` +
      `test_rbac_global_scope_jwt_role.py` +
      `test_shared_utils_user_identity.py` + `test_audit_strict_helpers.py`
      → **57 passed**. No preservation-tracked file was changed, so no
      rebaseline was owed. **Deferred**: the live confirmation of Req 5.2
      (deploy the auth stack, sign in through the browser, run the
      new-password challenge and a forgot-password reset) and the
      documentation of Req 5.3, which is task 6.4's.

  - [x] 6.2 Detect out-of-band Cognito administration
    - EventBridge rule on CloudTrail management events for
      `AdminCreateUser`, `AdminSetUserPassword`, `AdminUpdateUserAttributes`,
      `AdminAddUserToGroup`, `AdminEnableUser`, `AdminDisableUser`,
      `AdminDeleteUser` on the portal pool, excluding the User Manager
      execution role, targeting an SNS topic; the signal carries event name,
      caller ARN, source IP, user agent, event time
    - _Requirements: 6.1, 6.2, 6.3_
    - **OUTCOME**: `edge-cv-portal/infrastructure/lib/compute-stack.ts` is
      the only source file changed (plus its committed `tsc` output
      `lib/compute-stack.js`/`.d.ts`): it now synthesizes the SNS topic
      `dda-portal-cognito-admin-alerts` (subscriptions managed out of band,
      as for the build fleet's alert topic) and the EventBridge rule
      `dda-portal-cognito-admin-activity` on
      `source=aws.cognito-idp` / `detail-type='AWS API Call via CloudTrail'`
      / `detail.eventSource=cognito-idp.amazonaws.com`, matching exactly the
      seven admin events the requirement names and scoped to **this** pool
      via `detail.requestParameters.userPoolId = [props.userPool.userPoolId]`
      (the account holds several pools; the others would be noise). The
      target carries an input transformer with all five Req 6.2 fields —
      `eventName`, `callerArn` (`$.detail.userIdentity.arn`), `sourceIp`
      (`$.detail.sourceIPAddress`), `userAgent`, `eventTime` — plus
      `callerType`, `userPoolId`, `eventId` and `awsRegion` so a responder
      can pull the full CloudTrail record, and a `CognitoAdminActivityTopicArn`
      output tells operators what to subscribe to. `createLambdaRole('UserAdmin')`
      is now held in a `userAdminRole` local (behaviour-identical) so the rule
      can name that role's ARN. Decisions: (a) the User Manager exclusion
      (Req 6.3) is a **two-armed `$or`**, not a single `anything-but` on
      `userIdentity.sessionContext.sessionIssuer.arn`, because EventBridge's
      `anything-but` only matches when the field is **present** — a direct
      IAM-user or root caller has no `sessionContext`, so the single-armed
      form would have silently ignored exactly the direct-admin case this
      task exists to detect; arm 1 (`userIdentity.type` anything-but
      `AssumedRole`, and `type` is always present in a CloudTrail record)
      catches every non-assumed-role caller, arm 2 catches every assumed
      role that is not the User Manager's execution role; (b) the rule lives
      in **compute-stack** because it is the only stack holding both the
      pool (props) and the User Manager role, and it adds no IAM statement
      (the EventBridge→SNS grant is a `AWS::SNS::TopicPolicy`); (c) **no
      CloudTrail trail is created** — `AWS API Call via CloudTrail` events
      only reach EventBridge where a trail logs management events, but
      portal accounts normally already have an account/organization trail
      and a second one would duplicate delivery and cost, so the
      prerequisite is documented in the code comment and the output
      description and is an operator/runbook step (task 6.4); (d) the code
      comment states plainly that this is detection only (an actor with
      `cognito-idp:Admin*` can still make the calls, one with
      `events:DisableRule` can silence the signal) and that the registry
      enforced in the shared RBAC layer is the control. Verification: infra
      `npm run build` clean and `npm test` **177 passed / 19 suites / 2
      snapshots** — unchanged from tasks 5.1/6.1, no existing test or
      snapshot moved. Permanent CDK assertions are **task 6.3's**, so the
      synth was proven by a throwaway jest suite (5 tests: the topic; the
      pattern's source/detail-type/eventSource/the seven event names/the
      pool scoping; the `$or` exclusion resolving to
      `Fn::GetAtt[UserAdminRole…, Arn]`; the SNS target's input transformer
      declaring all five required paths with every template placeholder
      declared; and EventBridge's `sns:Publish` topic policy), which passed
      and was then deleted with its compiled artifacts; **non-vacuousness
      proven** by two mutations of the production stack — dropping the
      non-AssumedRole `$or` arm and dropping `sourceIp`/`userAgent` from the
      target input — which failed exactly the exclusion test and the
      attribution-field test, after which the file was restored
      byte-identically (`diff -q` clean). Security preservation: the full
      `test/backend-test/security/preservation` suite is **139 passed, 6
      skipped, 1 failed**, byte-for-byte tasks 5.1/6.1's state (the known
      stale `EdgeCVPortalComputeStack` IAM synth baseline), and I **proved
      that failure is untouched by this change** by dumping the test's own
      `iam_statements_multiset` of a fresh compute-stack synth with and
      without the block: the two dumps are **identical** (406 lines,
      `diff` clean), i.e. this change contributes zero IAM statements; the
      two repo-root guards are **4 passed, 3 skipped** (unchanged). No
      Python changed, so the portal backend cannot regress — sanity check
      `test_build_rbac.py` + `test_rbac_global_scope_jwt_role.py` +
      `test_shared_utils_user_identity.py` + `test_audit_strict_helpers.py`
      → **57 passed**. No preservation-tracked file was changed, so no
      rebaseline was owed. **Deferred**: the live confirmation (deploy the
      compute stack, verify the trail exists in-region, subscribe an
      operator, make an out-of-band `AdminUpdateUserAttributes` call and see
      the signal, then perform the same operation through the portal's User
      Manager and see **no** signal) — this run is forbidden from touching
      AWS; and `aws events test-event-pattern` validation of the `$or`
      pattern against a real CloudTrail record shape, for the same reason.
      Documenting the topic and its detection-only nature is task 6.4's.

  - [x]* 6.3 CDK assertions
    - App client auth flows; the env var on every handler; the User Manager's
      registry write grant; the EventBridge rule pattern and its exclusion
    - _Requirements: 5.1, 6.1, 6.3_
    - **OUTCOME**: Added
      `edge-cv-portal/infrastructure/test/portal-registry-enforcement-infra.test.ts`
      — **41 tests, all passing**, the run's only new file (`git status`
      shows it alone; no production file touched, so no preservation
      rebaseline was owed). It makes the spec's four template-level claims
      permanent: (a) **Req 5.1** — the portal app client's
      `ExplicitAuthFlows` is exactly `['ALLOW_REFRESH_TOKEN_AUTH',
      'ALLOW_USER_SRP_AUTH']` on both the default and the SSO-enabled
      AuthStack, neither password flow is present, the refresh flow (which
      `auth.py` uses) survives, and the client/pool under test really are
      the portal's (`dda-portal-client` / `dda-portal-users` with the
      `custom:role` attribute declared); (b) **Req 2.4 wiring (task 5.1)** —
      the four stacks that hold role-resolving handlers (Compute,
      BuildFleet, NodeDesigner, SyntheticData) are synthesized in **two
      separate apps with identical stack ids**, one plain and one with
      `-c portalRegistryEnforced=true`, and in each mode *every* Lambda
      carrying `USER_ROLES_TABLE` carries `PORTAL_REGISTRY_ENFORCED` at the
      canonical `'false'`/`'true'`, no function carries any other value, a
      plain deploy is enforcement-OFF, and the flag is the **only**
      difference between the two synths (same function set, each function
      byte-identical once the flag key is removed, flag present on the same
      set); (c) the **User Manager's registry grant** — the UserAdmin role
      holds `GetItem`/`Query`/`Scan`/`PutItem`/`UpdateItem`/`DeleteItem` on
      the user-roles table and the `user_admin.handler` function actually
      runs as that role; (d) **Req 6.1/6.2/6.3** — the rule's
      source/detail-type/eventSource, exactly the seven watched admin
      events, the pool scoping, the two-armed `$or` exclusion (the
      non-`AssumedRole` arm plus the session-issuer arm naming
      `Fn::GetAtt[UserAdminRole…, Arn]` — the same role as (c)), the SNS
      target's five required CloudTrail paths each declared once and
      interpolated with no undeclared placeholder, EventBridge's
      `sns:Publish` topic policy, the topic and its `CfnOutput`, and the
      rule being `ENABLED` and identical in both enforcement modes.
      Decisions: (a) handler coverage is asserted as a **universal
      quantification** over functions carrying `USER_ROLES_TABLE` plus
      per-stack **lower bounds** (Compute ≥ 41, BuildFleet ≥ 5,
      NodeDesigner ≥ 7, SyntheticData ≥ 1 = task 5.1's observed 54) rather
      than exact counts — the universal statement is the security property,
      while exact counts would make every unrelated spec that adds a portal
      handler fail here for no security reason; the bounds exist only to
      keep the universal assertions non-vacuous. (b) The input transformer
      is pinned by **JSON path**, not by CDK's generated placeholder names
      (`$.detail.eventName` → `detail-eventName`), with each path required
      to be declared exactly once and actually interpolated. (c) The event
      pattern's **matching semantics are deliberately not asserted**:
      deciding that a direct-IAM-user record matches while the User
      Manager's assumed-role record does not needs EventBridge's own
      matcher (`aws events test-event-pattern`), which this run may not
      call, and re-implementing `anything-but`/`$or` locally would only
      assert my reading of them — deferred to the live verification already
      recorded under task 6.2. (d) Both AuthStack variants get their own
      `cdk.App` (CDK forbids extending a synthesized app's tree), and the
      two enforcement-mode apps reuse identical stack ids so their
      templates compare verbatim. **Non-vacuousness proven** by eight
      throwaway mutations of the production stacks, each restored
      byte-identically afterwards (`diff -q` clean on all four files):
      re-enabling `userPassword`+`adminUserPassword` → 4 failures; the
      context helper defaulting to ON → 12; NodeDesigner losing the flag →
      5; `grantReadWriteData`→`grantReadData` on the registry → 1; dropping
      the non-`AssumedRole` `$or` arm → 1; dropping `sourceIp`+`userAgent`
      from the target → 1; dropping `AdminDeleteUser` from the watched
      events → 1; dropping the pool scoping → 1. Verification (`npm run
      build` first, per the task-6.1 lesson that jest resolves stale
      compiled `lib/*.js` before `lib/*.ts`): the new file **41 passed**
      (~16 s); full infrastructure `npm test` **218 passed / 20 suites / 2
      snapshots** (was 177/19 — exactly this file's 41 tests added, no
      existing test or snapshot moved) and `npm run build` clean; the jest
      "worker process failed to exit gracefully" notice is **pre-existing**
      (reproduced with this file excluded). No Python changed, so the portal
      backend cannot regress — sanity check `test_build_rbac.py` +
      `test_rbac_global_scope_jwt_role.py` +
      `test_shared_utils_user_identity.py` + `test_audit_strict_helpers.py`
      → **57 passed**; the two repo-root security preservation guards
      **4 passed, 3 skipped**, and the full
      `test/backend-test/security/preservation` suite **139 passed, 6
      skipped, 1 failed** — byte-for-byte tasks 5.1/6.1/6.2's state, the
      failure being the known stale `EdgeCVPortalComputeStack` IAM synth
      baseline (a test-only file adds no IAM statement).

  - [x] 6.4 Runbook
    - Document, in `edge-cv-portal/ADMIN_GUIDE.md`: the registry as the
      authority, how to provision a user (portal only), the backfill
      procedure, the flag, and an explicit statement that 6.1 and 6.2 are
      defense in depth / detection — an actor with
      `cognito-idp:UpdateUserPoolClient` can re-enable the auth flow, and
      restricting `cognito-idp:Admin*` by SCP or permission boundary is the
      complementary control that lives outside this repo
    - _Requirements: 5.3_
    - **OUTCOME**: `edge-cv-portal/ADMIN_GUIDE.md` is the only file changed
      (+375/−16, documentation only — no code, no test, no
      preservation-tracked file, so no rebaseline was owed). A new
      top-level section **"Portal Users and Privilege (Portal_Identity
      Registry)"** carries the runbook: the registry's row shape and the
      four-step resolution order (absent/disabled → 403 with
      `identity_source=absent`; global row → its role; Use_Case row
      overrides within its scope; lookup failure → 500 `Authorization
      check failed`, audited `failure`, never `Viewer`), the statement that
      `custom:role` is Claimed_Role and grants nothing, the new durable
      audit attribution fields, provisioning **through the User Manager
      only** (a Cognito↔registry table for create/role-change/
      disable-enable/delete, the 502 partial-create recovery, why
      `status=disabled` — not Cognito's disable — is what stops an
      already-minted token) with the bootstrap PortalAdmin named as the one
      by-hand exception, the backfill procedure (dry run → review → `--apply`
      → dry run again, the script's flags/exit codes/IAM needs, and what
      each plan action means), the flag (`PORTAL_REGISTRY_ENFORCED`,
      default off, `PORTAL_REGISTRY_ENFORCED=true ./deploy-infrastructure.sh`
      → `-c portalRegistryEnforced=true`, the accepted truthy set, and a
      log-group-sweep command for mining the `would_deny` dry-run WARNINGs),
      a 7-step rollout order ending in the incident replay (throwaway
      `custom:role=PortalAdmin` account with no row → expect 403 → delete →
      audit row still names it), an account-verification recipe against
      `dda-portal-user-roles`, and **Req 5.3** in a "Defense in depth and
      detection — not the fix" subsection: the SRP-only client is defense in
      depth only (an actor with `cognito-idp:UpdateUserPoolClient` re-enables
      the flows, and SRP itself accepts an admin-set password), the
      EventBridge rule/SNS topic is detection only (`events:DisableRule`
      silences it) with its two operator prerequisites (subscribe the topic;
      confirm a CloudTrail trail logs management events in-region), and
      restricting `cognito-idp:Admin*` by **SCP or permission boundary** is
      the complementary preventive control that lives outside this repo.
      Decisions: (a) I also corrected the passages of the same guide that
      now contradict the fix, because leaving them would actively mislead an
      operator — `Create admin user` and onboarding `Step 4` wrote the
      **wrong table name** (`edge-cv-portal-user-roles`, the table is
      `dda-portal-user-roles`) and omitted `status`/`username`/`email`;
      onboarding `Step 1` told operators to provision with
      `admin-create-user` (now insufficient); `Role Hierarchy` claimed the
      IdP role decides global capabilities; `Setting User Roles in Cognito`
      instructed operators to grant PortalAdmin with
      `admin-update-user-attributes` (now grants nothing) — each rewritten
      and cross-linked to the new section, plus a Troubleshooting entry for
      "Insufficient permissions (403) for a user who should have access"
      that distinguishes the 403 (missing/disabled row) from the 500
      (registry unavailable); (b) a short **Known caveat** records honestly
      that `user_admin.require_portal_admin` still gates `/admin/*` on the
      claim (task 2.3's note (d)), so `AdminUpdateUserAttributes` on the
      pool must be treated as User-Manager-equivalent until that moves —
      beyond the task's bullet list but the runbook would otherwise
      overstate the enforcement boundary; (c) every fact was checked against
      the code rather than the spec prose (table/pool/client/topic/rule
      names, the 403/500 envelopes, the `Registry denial:` and `would_deny`
      log lines and their fields, the backfill script's flags, plan actions
      and exit codes, the `Create user` label and `/admin/user-manager`
      route, `Role` including `DataLabeler`), and the CDK-generated Lambda
      names made a literal log-group example wrong, so the `would_deny`
      query sweeps `/aws/lambda/EdgeCVPortal` by prefix instead.
      Verification (documentation-only, so the goal was proving nothing
      regressed): a script check that all 14 internal anchors resolve to a
      heading, no duplicate heading slugs, and the code fences balance;
      repo-root security preservation guards **4 passed, 3 skipped** and the
      full `test/backend-test/security/preservation` suite **139 passed, 6
      skipped, 1 failed** — byte-for-byte tasks 5.1/6.1/6.2/6.3's state, the
      failure being the known stale `EdgeCVPortalComputeStack` IAM synth
      baseline; portal backend sanity `test_build_rbac.py` +
      `test_rbac_global_scope_jwt_role.py` +
      `test_shared_utils_user_identity.py` + `test_audit_strict_helpers.py`
      → **57 passed**, and exploration + preservation → **45 passed, 8
      failed**, again exactly the checkpoint state (the 8 are the C1
      enforcement cases that only pass with the flag on). Infrastructure
      `npm test` was **not** re-run: no TypeScript changed. **Deferred**:
      nothing of this task, but the runbook documents steps that only an
      operator with AWS access can perform (the deploy, the backfill apply,
      the topic subscription, the trail check) — task 5.2 remains the live
      execution.

- [x] 7. Final verification
  - Portal backend: the four new suites plus `test_build_rbac.py`,
    `test_rbac_global_scope_jwt_role.py`, `test_shared_utils_user_identity.py`,
    every `test_user_admin_*.py`, and a per-file sweep of the files importing
    `shared_utils` / `rbac_middleware`
  - Infrastructure: `npm run build` and `npm test`
  - Security preservation guards from the repo root per the steering
  - Confirm the exploration suite now passes and record which of its cases
    flipped
  - **OUTCOME**: Verification-only task — **no file was created or modified**
    (`git status` byte-for-byte task 6.4's state; the only artifact, a stray
    `edge-cv-portal/__pycache__` from a `py_compile` check, was deleted), so no
    preservation rebaseline was owed. Portal backend at the **deployed default
    (flag off)**: the eight suites this spec added plus the four named
    regression suites in one process → **239 passed, 8 failed**; the four
    property suites (Properties 1-6, ≥100 examples each) → **10 passed**
    (~201 s); all ten `test_user_admin_*.py` → **234 passed**
    (2/27/43/17/27/31/16/9/46/16); and a **per-file sweep of the 89 test files
    importing `shared_utils` / `rbac_middleware` / `user_admin`** (600 s cap
    each, excluding this spec's own eight, the four named suites, the ten
    user_admin files and the forbidden
    `test_property_untouched_families_and_dimensions.py`) → **1013 passed, 86
    of 89 files fully green**, the three non-green being two grep-matched
    non-test helper modules (`tests/conftest.py`, `tests/synthetic_env.py`,
    exit 5 "no tests collected") and the pre-existing
    `test_deployment_preflight_preservation.py::TestSourceTreeUntouched` pair
    (another spec's clean-`src/` oracle). **Exploration suite confirmed: all 8
    remaining red cases flip green under enforcement.** Because task 5.2 (the
    live backfill + flag flip) is an AWS-only task this run may not perform,
    the confirmation was made with `PORTAL_REGISTRY_ENFORCED=true` **as an
    environment variable only, no edit**: exploration + preservation +
    `test_rbac_global_scope_jwt_role.py` → **67 passed** (13 + 40 + 14), i.e.
    the whole incident suite green while all 40 preservation invariants and the
    repointed regression suite hold in both modes. The 8 that flipped are
    exactly bug-condition C1 —
    `TestIncidentReplay::test_unprovisioned_claim_is_denied[PortalAdmin|DataScientist|UseCaseAdmin]`,
    `::test_unprovisioned_claim_grants_no_permission[PortalAdmin|DataScientist|UseCaseAdmin]`,
    `::test_claim_cannot_raise_the_registry_role` and
    `::test_denial_is_audited_with_absent_identity_source`; the other five
    (`TestAcceptedActionAttribution`, bug-condition C2) have been green since
    task 2.2 and stay green in both modes. Infrastructure: `npm run build`
    clean and `npm test` **218 passed / 20 suites / 2 snapshots** (task 6.3's
    41 CDK assertions included). Security preservation: the two repo-root
    guards **4 passed, 3 skipped**, and the full
    `test/backend-test/security/preservation` suite **139 passed, 6 skipped, 1
    failed** — byte-for-byte tasks 5.1/6.1/6.2/6.3/6.4's state; I re-confirmed
    that one failure is not this spec's by reading the drift itself: the
    `EdgeCVPortalComputeStack` mismatch is a **`logs:*` statement count 42 vs
    the baseline's 41**, i.e. one extra Lambda execution role from another
    spec's in-flight handlers, and no SNS / EventBridge / user-roles statement
    appears in the diff (task 6.2 had already shown this spec's compute-stack
    block contributes zero IAM statements). Extra confirmations beyond the
    task's bullets: device `test/backend-test/workflow_engine` **1495 passed, 3
    skipped**, frontend `npx tsc --noEmit` clean (this spec touches no frontend
    file, so no vitest run was owed), and `backfill_portal_registry.py`
    compiles with its CLI `--help` resolving. Decisions: (a) the 8 exploration
    failures are **left red on purpose** at the deployed default — enabling
    enforcement is task 5.2's job and it is manual/live, so a green tree here
    would mean the flag had been flipped without the backfill; (b) I did
    **not** repoint the 14 tests that fail when enforcement is ambient in the
    test environment (9 `test_build_rbac.py::TestGrantMatrix` claim-only
    role/permission cases plus 5 `TestLastPortalAdminGuard` cases across
    `test_user_admin_change_role.py` / `test_user_admin_delete.py` /
    `test_user_admin_disable_enable.py`, whose exact subset varies with process
    composition because those suites seed Cognito-only PortalAdmins into a
    shared registry table): they are green at the deployed default, nothing
    sets `PORTAL_REGISTRY_ENFORCED` in `conftest.py` or any test environment so
    flipping the *production* flag cannot turn them red, and the same
    behaviour is already pinned in **both** modes by
    `test_portal_registry_units.py` / `test_user_manager_registry_units.py` —
    editing three other specs' suites for a non-default configuration was the
    less conservative option in an unattended run. The remedy, if anyone ever
    runs the portal suite with the flag ambient, is task 3.3's recipe verbatim:
    seed an enabled global Portal_Identity row per principal and keep the
    assertions. **Outstanding for the spec (not for this task): task 5.2** —
    the real-account dry run/apply of the backfill, the flag flip and redeploy,
    and the live re-verification (bootstrap `admin` reaching `/admin/users`,
    `POST /builds` for its operators, and the throwaway-account incident replay
    expecting 403), plus the live confirmations tasks 5.1/6.1/6.2 deferred (the
    `would_deny` WARNING sweep, browser SRP sign-in, the CloudTrail trail check
    and SNS subscription). Everything provable without AWS is proven.

## Notes

- Tasks marked `*` are optional; core implementation tasks are never optional
- Six correctness properties, one property-based test each, ≥ 100 examples,
  tagged `Feature: portal-jwt-role-privilege-escalation, Property {n}: {text}`
- Task 3.3 is a **recorded repoint**, not a weakening: the suite keeps
  proving what it was written to prove
- Task 5 is the only task that changes production authorization behavior; it
  is deliberately last and deliberately manual
- Out of scope (design.md Decisions 1, 6): Cognito groups as the role source,
  reviving the unattached `jwt_authorizer.py`, and any control that requires
  restricting the AWS account's own IAM

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3"] },
    { "id": 3, "tasks": ["2.4", "2.5", "3.1"] },
    { "id": 4, "tasks": ["3.2", "3.3"] },
    { "id": 5, "tasks": ["4"] },
    { "id": 6, "tasks": ["5.1"] },
    { "id": 7, "tasks": ["5.2"] },
    { "id": 8, "tasks": ["6.1", "6.2", "6.3", "6.4"] },
    { "id": 9, "tasks": ["7"] }
  ],
  "dependencies": {
    "2.1": ["1.1", "1.2"],
    "2.2": ["2.1"], "2.3": ["2.1"],
    "2.4": ["2.2", "2.3"], "2.5": ["2.2", "2.3"],
    "3.1": ["2.3"], "3.2": ["3.1"], "3.3": ["3.1"],
    "4": ["2.4", "2.5", "3.2", "3.3"],
    "5.1": ["4"], "5.2": ["5.1", "3.1"],
    "6.1": ["5.2"], "6.2": ["5.2"], "6.3": ["6.1", "6.2"], "6.4": ["5.2"],
    "7": ["6.3", "6.4"]
  }
}
```
