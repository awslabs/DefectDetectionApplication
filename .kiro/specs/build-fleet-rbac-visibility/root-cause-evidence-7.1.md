# Task 7.1 — Root-Cause Evidence Record

Investigation date: 2026-08-06 (~19:36–19:50 UTC)
Account `164152369890`, region `us-east-1`, AWS identity `assumed-role/ADMIN/ryvan-Isengard`.
All AWS calls were read-only (`logs`, `lambda get-function*`, `apigateway get-*`,
`dynamodb describe-table`/`scan`, presigned GET of the deployed code/layer assets).
No source file was modified, nothing was deployed, no build was run, no live
build job was submitted.

## Verdict

**The prior evidence from task 6 is CONFIRMED by the deployed CloudWatch record.**
The swallowed exception is **not** an RBAC/authorization error. It is a DynamoDB
`ValidationException` raised *after* `rbac_check` had already authorized the
caller, inside `submit_build` → `put_new_job` → `PutItem`, because an
**ephemeral** Build_Job carries `server_id = None` while the deployed
`dda-portal-build-jobs` table has a `server-index` GSI whose HASH key
`server_id` is typed `S`. `rbac_check`'s broad `except Exception` converts that
post-authorization failure into the generic `500 {"error":"Authorization check
failed"}`.

Commit `22a27eb`'s `user_info` threading is deployed, byte-identical to HEAD,
and working. It is **unrelated** to this failure.

## 1. The exact exception and triggering frame (deployed)

Emitting function: **`BuildJobsHandler`**
(`EdgeCVPortalBuildFleetSta-BuildJobsHandlerFC1CBC52-75bkr1HFZkVU`),
log group `/aws/lambda/EdgeCVPortalBuildFleetSta-BuildJobsHandlerFC1CBC52-75bkr1HFZkVU`,
log stream `2026/08/06/[$LATEST]80218a283e944f79a3c86b7c720356b0`.

`BuildFleetHandler` (`.../BuildFleetHandler014CDE4-Ki7FXubb6MQe`) emitted
**zero** `Error in RBAC check` events in the same 48-hour window — the reported
handler name was wrong; the catch-all came from `BuildJobsHandler`, as the CDK
route mapping predicted (see §3).

Three occurrences, all today, all identical:

| Lambda RequestId | UTC | X-Ray TraceId |
| --- | --- | --- |
| `89700182-e5cc-45b9-b44f-08cd4f186f34` | 2026-08-06T16:47:34.630Z | `1-6a74baa6-29fe864273e0ea8227549f96` |
| `61e9f945-b698-4b07-bbf8-afacf82219c4` | 2026-08-06T16:47:44.408Z | `1-6a74bab0-54c21d6f4e986a7766c4e37a` |
| `86586708-157a-4535-8335-52daf768a2a3` | 2026-08-06T17:02:34.385Z | `1-6a74be2a-41ce4d39239d022b7d4697c0` |

API Gateway access logging and data tracing are **disabled** on stage `v1`, so no
API Gateway request id exists; correlation is by Lambda RequestId + X-Ray TraceId
+ timestamp, which is sufficient (the audit log and job table timestamps line up,
§5).

Complete CloudWatch traceback (verbatim, all three events):

```
[ERROR] 2026-08-06T16:47:34.630Z 89700182-e5cc-45b9-b44f-08cd4f186f34
Error in RBAC check: An error occurred (ValidationException) when calling the PutItem
operation: One or more parameter values were invalid: Type mismatch for Index Key
server_id Expected: S Actual: NULL IndexName: server-index
Traceback (most recent call last):
  File "/var/task/rbac_middleware.py", line 142, in wrapper
    return func(event, context)
  File "/var/task/build_jobs.py", line 393, in submit_build
    stored_jobs = [put_new_job(job) for job in jobs]
  File "/var/task/build_jobs.py", line 393, in <listcomp>
    stored_jobs = [put_new_job(job) for job in jobs]
  File "/var/task/build_jobs.py", line 321, in put_new_job
    jobs_table().put_item(Item=to_dynamo_safe(item))
  File "/var/lang/lib/python3.11/site-packages/boto3/resources/factory.py", line 581, in do_action
  File "/var/lang/lib/python3.11/site-packages/boto3/resources/action.py", line 88, in __call__
  File "/var/lang/lib/python3.11/site-packages/botocore/client.py", line 606, in _api_call
  File "/var/lang/lib/python3.11/site-packages/botocore/context.py", line 123, in wrapper
  File "/var/lang/lib/python3.11/site-packages/botocore/client.py", line 1094, in _make_api_call
    raise error_class(parsed_response, operation_name)
botocore.exceptions.ClientError: An error occurred (ValidationException) when calling the
PutItem operation: One or more parameter values were invalid: Type mismatch for Index Key
server_id Expected: S Actual: NULL IndexName: server-index
```

Triggering frame: `build_jobs.py:321` `jobs_table().put_item(Item=to_dynamo_safe(item))`,
reached from `build_jobs.py:393` inside `submit_build`.

Why the response is the generic catch-all: `rbac_middleware.py:142` is
`return func(event, context)` — the wrapped handler call sits **inside**
`rbac_check`'s `try`, and the `except Exception` at `rbac_middleware.py:144-146`
logs `Error in RBAC check: ...` and returns
`create_response(500, {'error': 'Authorization check failed'})`. Because line 142
executed at all, the permission loop
(`rbac_manager.has_permission(..., user_info=user)`) had already returned
`True`. **Authorization succeeded; the 500 is a mislabeled handler error.**

## 2. Deployed configuration (traceability)

| | BuildJobsHandler | BuildFleetHandler |
| --- | --- | --- |
| Function | `EdgeCVPortalBuildFleetSta-BuildJobsHandlerFC1CBC52-75bkr1HFZkVU` | `EdgeCVPortalBuildFleetSta-BuildFleetHandler014CDE4-Ki7FXubb6MQe` |
| Handler | `build_jobs.handler` | `build_fleet.handler` |
| Version / alias | `$LATEST` (no published versions, no alias) | `$LATEST` |
| `CODE_VERSION` | `2026-02-21-build-jobs` | `2026-02-21-build-fleet` |
| CodeSha256 | `dwWyW3GD4Ke85f6HQpuaaPxKpFdoSIUMES4vjSWy1vA=` | `dwWyW3GD4Ke85f6HQpuaaPxKpFdoSIUMES4vjSWy1vA=` (same asset) |
| CodeSize | 1 403 892 | 1 403 892 |
| LastModified | 2026-08-06T16:40:13Z | 2026-08-06T16:40:02Z |
| RevisionId | `3361c78e-ca38-4043-bbc8-b06598d0fdb0` | `65bf1b79-7a0d-4413-b84b-aac3a0c2a4fc` |
| Layer | `arn:aws:lambda:us-east-1:164152369890:layer:BuildFleetSharedLayerC170FCDB:1` | same |
| `BUILD_JOBS_TABLE` | `dda-portal-build-jobs` | `dda-portal-build-jobs` |

Layer `BuildFleetSharedLayerC170FCDB`: exactly **one** version exists (`:1`,
created 2026-08-06T15:23:24Z, `CodeSha256 Cw42URS+5Te7MtxJzRLSXvB9cuHbmRZvxGiK8eGku+o=`,
runtime `python3.11`) and it is the attached one. No version-ordering ambiguity.

## 3. API Gateway integration for `POST /builds`

REST API `yqvyoowugk` ("Edge CV Portal API"), stage `v1`, resource `/builds`
(`v0qcxd`), method `POST`:

- `authorizationType = COGNITO_USER_POOLS`, authorizer `k1fw58`
- `integrationType = AWS_PROXY`
- `uri = arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:164152369890:function:EdgeCVPortalBuildFleetSta-BuildJobsHandlerFC1CBC52-75bkr1HFZkVU/invocations`

Confirms `POST /builds` → **BuildJobsHandler**, matching the log evidence in §1.

## 4. Deployed BuildJobs table schema — the crux

`aws dynamodb describe-table --table-name dda-portal-build-jobs`
(ARN `arn:aws:dynamodb:us-east-1:164152369890:table/dda-portal-build-jobs`, created
2026-08-06T15:23:20Z):

- Table key: `build_job_id` (HASH, `S`)
- Attribute definitions: `build_job_id S`, `created_at N`, `request_id S`,
  `request_order N`, **`server_id S`**, `status S`
- GSIs (all `ProjectionType: ALL`):
  - **`server-index`: HASH `server_id` (S), RANGE `created_at` (N)** ← confirmed
  - `request-index`: HASH `request_id` (S), RANGE `request_order` (N)
  - `status-index`: HASH `status` (S), RANGE `created_at` (N)

Deployed code path that violates it:
`build_domain.create_build_jobs` (deployed `build_domain.py:635-638`) sets
`'server_id': server_id if execution_mode == 'dedicated' else None`, i.e. **None
for ephemeral**. `build_jobs.to_dynamo_safe` JSON-round-trips the item, so the
`None` survives and boto3 serializes it as a `NULL` attribute. DynamoDB rejects
any item whose indexed key attribute is present with the wrong type, hence
`Type mismatch for Index Key server_id Expected: S Actual: NULL`. Ephemeral jobs
can therefore never be persisted; dedicated jobs can, because `server_id` is a
real `S` string.

## 5. Reproduction result for both execution modes

**Deployed (real traffic, user `a4b804e8-5061-7004-12f2-38a0149dcd4c`, the
Cognito `admin`):**

- Ephemeral: 3 attempts (16:47:34, 16:47:44, 17:02:34) → `Error in RBAC check`
  + generic 500; **no** `build_requested` audit record; **no** job persisted.
- Dedicated: 2 submissions succeeded 19 s and 3.5 min after the last ephemeral
  failure — `dda-portal-build-jobs` contains exactly two items, both
  `execution_mode = dedicated` with a real `server_id` string:
  - `06c9a7ac-6b65-49ee-acdd-db8bf6d0cc03`, target `AMD64`,
    `server_id = srv-5b214096-91a9-41b7-9d62-cc03ba205c15`, created
    2026-08-06T17:02:53.525Z
  - `8618148c-e5af-4a8c-9472-223eb0ada57f`, target `JP5`,
    `server_id = srv-8ff512c0-a5bd-4e2b-b6ed-9c9487f216a9`, created
    2026-08-06T17:06:05.405Z

  (Both later reached `status = failed` for unrelated build-execution reasons —
  out of scope here; what matters is that `PutItem` succeeded and the API
  returned 201.) Matching audit records `build_requested / success` exist at
  17:02:53.586Z and 17:06:05.466Z.

**Local real-boundary reproduction** (`test/backend-test/portal_builds/
test_jwt_admin_build_submit_authorization.py`, re-run during this task, real
`handler` → `submit_build` decorated by `@require_builds_submit()`, only
downstream dispatch stubbed):

```
1 failed, 1 passed
FAILED ...::test_jwt_portal_admin_submits_jp5[ephemeral]
  status 500, body {"error":"Authorization check failed"},
  recorded_jobs 1, persisted_jobs 0
  Swallowed by rbac_check: ValidationException ... Type mismatch for Index Key
  server_id Expected: S Actual: NULL IndexName: server-index
PASSED ...::test_jwt_portal_admin_submits_jp5[dedicated]
```

The same test asserts, and passes, that
`shared_utils.rbac_manager.has_permission(admin_user_id, 'global',
Permission.BUILDS_SUBMIT, user_info=<JWT PortalAdmin>)` is `True`.

Local reproduction and deployed behavior agree exactly: **ephemeral fails,
dedicated succeeds, and the failure is post-authorization.**

## 6. Artifact hash verification (deployed vs. local HEAD)

Downloaded the deployed code asset via the presigned `Code.Location` URL; its
base64 SHA-256 is `dwWyW3GD4Ke85f6HQpuaaPxKpFdoSIUMES4vjSWy1vA=`, identical to
the reported `CodeSha256`. Per-file SHA-256, deployed vs. working tree:

| File | Deployed (`/var/task/...`) | Local | Match |
| --- | --- | --- | --- |
| `rbac_middleware.py` | `f355e732c35765d45d1141e74f4131bb36e4ce7cadd7171eb41e30844ba87c75` | same | yes |
| `build_jobs.py` | `de0181b4e34787f6f50cc8079993798a49ecf0943c0013329893530ad418bb39` | same | yes |
| `build_fleet.py` | `9d225f7a730f366be694e841c2007d5e45530654a84fefee843d9564b825d744` | same | yes |
| `build_domain.py` | `959a679b47b67da8298c8e095c208c05881fa32669469484437b0bd26b5d9f61` | same | yes |
| layer `python/shared_utils.py` | `3defa7952a68bde993aaa8d098ac717659c44c469a1909d0671bf154302e2ae2` | same | yes |

Working tree is clean for all five files at HEAD `22a27eb`
(branch `feature/workflow-triggers`). Deployed traceback line numbers match the
local sources exactly (`rbac_middleware.py:142`, `build_jobs.py:393`,
`build_jobs.py:321`). The deployed `rbac_middleware.py` contains the `user_info`
threading at lines 102, 117, 136, 137, 138, 164, 172.

**No source-vs-deployed skew of any kind.**

## 7. Deployed-runtime compatibility items — all RULED OUT

Each was checked against the actual deployed artifacts, not the source tree.

1. **`user_info` keyword compatibility — RULED OUT.** Layer
   `python/shared_utils.py` (deployed) declares
   `get_user_role(self, user_id, usecase_id, user_info: Optional[Dict] = None)`
   (line 554), `get_user_permissions(..., user_info=None)` (620),
   `has_permission(self, user_id, usecase_id, permission, user_info=None)` (628),
   `is_portal_admin(self, user_id, user_info=None)` (697). Every call site in the
   deployed `rbac_middleware.py` matches. No `TypeError` is possible.
2. **`Permission.BUILDS_SUBMIT` existence and identity — RULED OUT.** Deployed
   layer defines `BUILDS_SUBMIT = "builds:submit"`, `BUILDS_CANCEL`,
   `BUILDS_READ` (shared_utils.py:370-372). The deployed function package
   contains **no** `Permission`/`Role` class of its own; `rbac_middleware.py`
   imports `rbac_manager, Role, Permission` from `shared_utils` in one statement,
   and `CommonPermissions.SUBMIT_BUILDS = [Permission.BUILDS_SUBMIT]` uses that
   same object, so enum identity is guaranteed. The layer's second enum in
   `python/rbac_utils.py` is imported only by `example_protected_endpoint.py` and
   `user_roles.py` — neither is on the `POST /builds` path.
3. **Role-permission matrix — RULED OUT (correct as merged).** Deployed layer:
   `DataScientist` and `UseCaseAdmin` each grant `BUILDS_SUBMIT`/`BUILDS_CANCEL`/
   `BUILDS_READ`; `PortalAdmin` grants `*[permission for permission in Permission]`
   (therefore all three); `Viewer` and `Operator` contain no `BUILDS_*` entries.
4. **Layer contents / version ordering / skew — RULED OUT.** Only layer version
   `:1` exists and it is the attached one; its `shared_utils.py` is byte-identical
   to local HEAD. Both handlers share one code asset and one layer version.
5. **`require_builds_submit` wiring — CORRECT.** Deployed
   `rbac_middleware.py:413-417`: `require_builds_submit()` returns
   `rbac_check(CommonPermissions.SUBMIT_BUILDS, allow_global=True)`, i.e. scope
   `global`, permission `builds:submit`.
6. **Nothing raises inside `rbac_check` before `submit_build` runs.** Proven by
   the traceback: the topmost application frame is line 142
   (`return func(event, context)`), not the resolution calls above it.

## 8. Supporting evidence that RBAC is healthy post-deploy

`dda-portal-user-roles` for `a4b804e8-5061-7004-12f2-38a0149dcd4c`: exactly two
rows, both `role = UseCaseAdmin` for concrete usecase ids
(`645504ce-...`, `9a75b104-...`). **No `usecase_id = 'global'` row** — so the
bug-condition premise (JWT-only global role) holds, and the caller was
nonetheless authorized.

`dda-portal-audit-log` for that user since 16:33 UTC:

- `unauthorized_access / denied` on `/build-servers` every ~15 s from
  16:33:24Z through 16:39:54Z — the *original* facet, on the pre-fix artifact.
- The functions were updated at 16:40:02Z / 16:40:13Z (commit `22a27eb`).
- **Zero** `unauthorized_access` records after 16:40. The three ephemeral submit
  failures produced no denial record at all, and `build_requested / success`
  records appear for the two dedicated submissions.

This is independent confirmation that the `user_info` fix is deployed and
effective, and that the remaining `POST /builds` failure is not an authorization
denial.

## 9. Root cause statement

1. `build_domain.create_build_jobs` emits `server_id = None` for
   `execution_mode = 'ephemeral'`.
2. `build_jobs.put_new_job` → `to_dynamo_safe` preserves that `None`, so
   `PutItem` sends `server_id: {"NULL": true}`.
3. The deployed `dda-portal-build-jobs` table's `server-index` GSI declares
   `server_id` as HASH of type `S`, so DynamoDB rejects the write with
   `ValidationException: Type mismatch for Index Key server_id Expected: S
   Actual: NULL IndexName: server-index`. (A GSI key attribute may be *absent* —
   the item is then simply not indexed — but it may not be present with a
   different type.)
4. `submit_build` is called from inside `rbac_check`'s `try` block
   (`rbac_middleware.py:142`), and the `except Exception` handler logs
   `Error in RBAC check: ...` and returns
   `500 {"error":"Authorization check failed"}`.
5. Net effect: an authorized JWT-only PortalAdmin submitting an ephemeral build
   gets a message that blames authorization, and no Build_Job is created.
   Dedicated mode is unaffected because `server_id` is a non-empty string.

There is **no** contradiction between the CloudWatch evidence and the local
reproduction; they agree on exception type, message, frames, and the
ephemeral/dedicated split.

## 10. Implications for task 7.2 (evidence-backed, minimum scope)

Two distinct defects are proven, both outside the role-permission matrix:

- **Primary (functional):** ephemeral Build_Jobs must not write a `NULL`
  `server_id` into a `server-index`-indexed table — omit the attribute when
  there is no server (fix in the persistence path, e.g. `put_new_job` /
  `to_dynamo_safe`, so the pure `create_build_jobs` contract and the job shape
  returned to callers stay intact).
- **Secondary (diagnosability, required by Req 2.7 / 3.2):** `rbac_check` must
  not wrap the decorated handler call in its own `except Exception`. Handler
  errors have to surface as their own error envelope, never as
  `Authorization check failed`; authorization denials must keep the existing
  403 envelope.

No change is warranted to the matrix, to `user_info` threading, to role
precedence, to the layer packaging, or to the deployed function/layer selection —
each was verified correct above.

## Deployed identifiers for downstream traceability (tasks 7.2–8)

```
BuildJobsHandler  EdgeCVPortalBuildFleetSta-BuildJobsHandlerFC1CBC52-75bkr1HFZkVU
                  $LATEST  CodeSha256 dwWyW3GD4Ke85f6HQpuaaPxKpFdoSIUMES4vjSWy1vA=
                  CODE_VERSION 2026-02-21-build-jobs  LastModified 2026-08-06T16:40:13Z
BuildFleetHandler EdgeCVPortalBuildFleetSta-BuildFleetHandler014CDE4-Ki7FXubb6MQe
                  $LATEST  same CodeSha256
                  CODE_VERSION 2026-02-21-build-fleet LastModified 2026-08-06T16:40:02Z
Layer             arn:aws:lambda:us-east-1:164152369890:layer:BuildFleetSharedLayerC170FCDB:1
                  CodeSha256 Cw42URS+5Te7MtxJzRLSXvB9cuHbmRZvxGiK8eGku+o=
API               yqvyoowugk stage v1, resource /builds (v0qcxd), POST -> BuildJobsHandler
Table             dda-portal-build-jobs (server-index: server_id S HASH, created_at N RANGE)
Source            HEAD 22a27eb on feature/workflow-triggers, clean for all involved files
```
