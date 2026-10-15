# Bugfix Requirements Document

## Introduction

A review of request handling in the LocalServer backend found four
input-validation and output-encoding gaps in shipped device code, and one
persistence/redaction gap in the workflow trigger path. This spec fixes them
together, in one LocalServer build round. Owner decision D5 (b) adds one
frontend change to the same round: the result-history download link that
feeds Item 3's route (Item 3c). It is framed as input validation,
path containment, output encoding and error-response hardening. Every behavior
change is called out below. The one change that alters what a run reads back
(Item 5, full form) is an explicit owner decision (D1); it is never adopted by
default.

Default access. Every route named here is reachable without a credential **in
the shipped default configuration**. All recipe variants set
`LocalLoginEnabled: "false"` and no station authorization file is present by
default, so `authorize_credential` (`utils/auth.py:188-189`) takes the
open-access row and returns before any check, and the backend serves its
plaintext listener (`app.py:396`). With station authorization or local login
on, the same routes require a valid credential. The fixes don't change
authorization in either configuration.

### Items in scope

- **Item 1: 500-response body.** The global uncaught-exception handler
  (`src/backend/exceptions/handlers/exception_handlers.py:97-107`, registered
  at `app.py:154`) returns the caught exception's class name and `str(value)`
  in the response body (`:107`). The full traceback is already logged with
  `exc_info` (`:105`). The device SPA shows `data.message` to the operator
  (for example `live-result/ManualDisplay.tsx:83`, `RefreshDisplay.tsx:76`).
- **Item 2: inference-results path containment.**
  `GET /workflows/{workflowId}/capture-details/{captureId}/input-image`
  (`src/backend/endpoints/download_file.py:98-140`). Its Triton branch runs on
  every image, because `setup_triton()` sets `is_triton=True` at startup
  (`app.py:309`, called at `:418`). When the database has no inference-result
  row for `captureId` (`:108-109`), the handler builds
  `f"/aws_dda/inference-results/{workflowId}/{captureId}.jsonl"` (`:111`) and
  opens it (`:113`) with no containment check. Both ids are single path
  segments (the router matches `[^/]+`), so the only movement is a literal
  `..` workflow id, which reaches `/aws_dda/<captureId>.jsonl`. A read or
  parse failure returns a 500 whose detail carries the exception text,
  including the opened path (`:117-121`).
- **Item 3: export query-path anchoring and containment.**
  `GET /workflows/{workflow_id}/results/export` (`download_file.py:298-357`)
  takes the **query** parameter `captureIdPath`, validated against
  `CAPTURED_IDS_PATH_PATTERN` (`src/backend/utils/constants.py:109`). The
  request validator applies the pattern with search semantics, so a value only
  has to contain the capture-id-path shape. A value with extra path parts and
  `..` segments is accepted and passed to `os.path.exists` (`:318`) and `open`
  (`:323`). It opens a file outside `/aws_dda/system` when a directory with the
  capture-file name exists along its path. The 404 detail echoes the value
  (`:321`).
  - **Item 3b: the log line of a rejected query.** The request-validation
    handler (`exception_handlers.py:71-87`) logs `query_params` unmasked
    (`:76`, `:80-81`). The result-history download link joins its credential
    without `&`
    (`src/frontend/src/components/result-history/ResultCardContent.tsx:302`),
    so on a station with authorization or local login on, `captureIdPath`
    arrives as `<path>token=<credential>`. The shared log redactor masks
    neither form of that value. The handler logs the re-encoded
    `token%3D<credential>` form. The raw `token=<credential>` form isn't
    masked either: the redactor matches a name only as a whole token
    (`workflow_engine/vendor/workflow_core/stream_url.py:411-421`), and here
    `token` directly follows the timestamp's last digit.
    Today that value passes the unanchored pattern. The handler raises 401
    (`utils/auth.py:126-131`), and no backend log line carries the value:
    - the download routes are on a plain `APIRouter()`
      (`download_file.py:56`), not the access-logging route class
      (`endpoints/route/access_log_router.py`);
    - uvicorn's own access log is discarded
      (`dda_logging/uvicorn_disable_logging.json`);
    - the 401 path logs nothing.

    Once Item 3 anchors the pattern, the value is rejected at validation, and
    this handler would log the credential. Item 3b closes that in the same
    change.
  - **Item 3c: the export link itself (owner decision D5 (b)).** The same
    link interpolates `captureIdPath` unencoded and appends
    `token=<credential>` with no `&` before it, so the server sees one
    `captureIdPath` value and no `token` parameter. Today the result-history
    download on such a station fails with 401; after Item 3 alone it would
    fail with 400. A `buildExportUrl` helper percent-encodes the path and
    joins the credential as its own `&token=` parameter only when auth is
    on, which repairs that download. Item 3b doesn't depend on it: a client
    that still sends the mis-joined value is rejected at validation, and its
    query is withheld from the log.
- **Item 4: text-generation failure reason.** On vLLM-capable images,
  `_failure_reason` (`src/backend/endpoints/text_generation.py:423-426`)
  returns the error's truthy `.reason` attribute, else `str(error)`
  (`reason = getattr(error, "reason", None)`, then
  `return reason if reason else str(error)`). The catch-all
  `except Exception` in `generate_text` (`:647`) puts it in the 502 body
  (`:658-664`), and `generate_text_stream` puts it in its single SSE error
  event (`:708-713`). For an unexpected error, raw error text reaches the
  caller, and the non-streaming path logs nothing.
- **Item 5: signed query material in the trigger payload (OWNER DECISION D1,
  designed both ways).** The MQTT subscribers store the decoded payload in the
  Trigger_Context (`src/backend/workflow_engine/trigger_runtime.py:1845-1852`,
  `:2147-2154`). It is persisted to `WorkflowExecution.trigger_context_json` by
  `serialize_trigger_context` (`:498-504`; writers at `:545` and
  `continuous_runner.py:168`), reloaded into the run and seeded as
  `tag_values["trigger"]` (`pipeline_executor.py`, `WorkflowExecutor.execute`),
  and then:
  - written in full to `{output_dir}/{capture_id}.json`
    (`WorkflowExecutor._persist_run_metadata`);
  - served verbatim by `GET /workflows/executions/{execution_id}/metadata`
    (`workflow_engine/api.py:543-561`);
  - logged in the run-completion `tags:` line (`WorkflowExecutor.execute`),
    which reaches the application log and the run's own `run.log`. The run log
    is served by `GET /workflows/executions/{execution_id}/log`
    (`api.py:483-497`). The shared redactor masks the `X-Amz-Signature` and
    `X-Amz-Security-Token` values in that line, but not `X-Amz-Credential` or
    `X-Amz-Date` (its name set covers `signature` and `token` only,
    `workflow_engine/vendor/workflow_core/stream_url.py:78-95`).

  Option (a) accepts the current behavior. Option (b) masks the signed values,
  in one of two scopes:
  - (b1) the served and logged sinks, for new and earlier runs, with no change
    to what a run reads back;
  - (b2) also the stored column. Every SigV4 value a run reads back from its
    trigger payload is then masked. Presigned-URL fetches taken from the
    payload stop working, and signed values surfaced through a metadata
    binding or a `{trigger.…}` placeholder read `***`.

  The design recommends (b1), and the owner chose (b1) (task 9). All three
  options ride the same build round as Items 1-4.

### Items reviewed and left unchanged (static-analysis dispositions)

Six static-analysis findings need no code change. They were dismissed on
2026-10-15 with the platform's fixed reasons (owner decision D4). This spec
changes none of their files, so they didn't wait for the build or the rescan;
task 11 handles only the five findings this spec changes. Each justification
below fits the platform's 280-character limit.

| Location | Rule category | Dismissal reason | Justification |
|---|---|---|---|
| `src/backend/endpoints/download_file.py:90` | path handling | false positive | `fileName` is one URL path segment (`[^/]+`); encoded slashes don't match the route, so a value joined onto the fixed `/aws_dda/system` directory stays in it. The route serves the snapshot archives there, which is its purpose. |
| `src/frontend/src/components/image-source/image-capture/CapturedCards.tsx:158` | markup output | false positive | A route parameter reaches an `<img src>` URL attribute. An image URL can't run script, React escapes attributes, and the device SPA has no unescaped-HTML sink. |
| `src/frontend/src/components/live-result/InteractableImage.tsx:114` | markup output | false positive | Same shape: a route parameter reaches `<img src={imageSrc}>`; React escapes attributes and the SPA has no unescaped-HTML sink. |
| `src/edgemlsdk/src/docs/samples/C++/MessageBroker/mqtt.cpp:28` | format arguments | won't fix | Documentation sample. It compiles only inside the transient edgemlsdk build image and isn't installed into the shipped `.deb` or any component. |
| `src/edgemlsdk/src/docs/samples/C++/MessageBroker/mqtt.cpp:51` | format arguments | won't fix | Documentation sample (second site in the same file). It compiles only inside the transient edgemlsdk build image and isn't installed into the shipped `.deb` or any component. |
| `test/backend-test/security/test_docker_base_image_bug_condition_exploration.py:120` | URL substring check | used in tests | A test assertion over Dockerfile text that checks a base-image registry prefix was replaced; not a production host check. |

The optional one-line hardenings (`os.path.basename` for the snapshot route,
`encodeURIComponent` for the two image URLs) aren't required for correctness
and aren't implemented here.

### Bug conditions

- **C1 (Item 1).** WHEN the uncaught-exception handler builds a 500 response
  THEN the body message carries the exception's class name and `str(value)`
  instead of a fixed text.
- **C2 (Item 2).** WHEN the input-image route finds no inference-result row
  THEN it opens a path built from `workflowId` and `captureId` without checking
  that it stays below `INFERENCE_RESULTS_DIR`, so a `..` workflow id opens a
  file one level up.
- **C3 (Item 3).** WHEN the export route validates `captureIdPath` THEN a value
  that merely contains the capture-id-path shape is accepted and passed to
  `os.path.exists` and `open`; it opens a file outside `DDA_SYSTEM_FOLDER` when
  an intermediate directory with the capture-file name exists.
- **C3b (Item 3b).** WHEN the request-validation handler logs a rejected
  request THEN a credential inside a query value is logged unmasked. The
  handler logs `query_params` without masking. The shared redactor matches
  neither the re-encoded `token%3D<value>` form nor a `token=<value>` that
  directly follows a letter or digit.
- **C3c (Item 3c).** WHEN the result-history download link is built THEN
  `captureIdPath` is interpolated unencoded and, with auth on, the credential
  is appended as `token=<value>` with no `&` before it, so parsing the link's
  query doesn't return the path and the credential as two parameters.
- **C4 (Item 4).** WHEN `_failure_reason` gets an error that is not a
  `GenerationError` THEN the caller receives that error's `.reason` attribute
  or `str(error)`.
- **C5 (Item 5).** WHEN a trigger payload carries a presigned URL THEN its
  signed values appear in the stored column, the persisted run-metadata JSON,
  the `/metadata` response, and the `tags:` line in the application log and
  the `/log` response (`X-Amz-Credential` and `X-Amz-Date` there).

Non-buggy inputs (`¬C`): every other route and handler; every slash-free id
pair other than a `..` workflow id or an id with an embedded NUL; every
`captureIdPath` of the exact writer shape; every query string that names no
sensitive key; every `GenerationError` with a truthy `.reason`; every trigger
payload without SigV4 values; every export link built with auth off for a
writer-shape path (it parses back to the same `captureIdPath`).

### Validation constraint

Fix and preservation checks are finite unit and property tests in the repo's
existing styles. They run on the host (`--noconftest`) and, where they need
the device's interpreter, libraries or a writable `/aws_dda`, in a throwaway
container of the local backend image. Item 3c's checks are jest unit and
property tests run by the SPA's own test script. End-to-end behavior on a
built image is validated by the OWNER-gated on-device checks. The push, the
build round, the on-device checks, the trunk merge and the post-rescan
reconciliation are separate gates.

### Completion criterion

The spec is design-and-local-validation complete when the fixes chosen for
D1, their tests and the preservation suites pass locally and the feature
branch is pushed. It closes only after every affected architecture has an
on-device pass or an owner-recorded built-only disposition (task 8), the
verified ref is merged to the trunk (task 10), and the post-merge rescan is
reconciled (task 11).

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN an unhandled exception reaches `unhandled_exception_handler`
(`exception_handlers.py:97-107`) THEN the system returns status 500 with
`{"message": "Internal Server Error. Error: '<ExceptionClass>: <str(value)>.'", "request_id": <id>}`.

1.2 WHEN the input-image route finds no inference-result row for `captureId`
THEN the system opens `/aws_dda/inference-results/{workflowId}/{captureId}.jsonl`
(`download_file.py:111-113`) with no containment check; with
`workflowId=".."` it opens `/aws_dda/<captureId>.jsonl`; and a read or parse
failure returns 500 with the exception text, including the path, in its detail
(`:117-121`).

1.3 WHEN the export route receives `captureIdPath` (`download_file.py:306`)
THEN the system accepts any value containing the capture-id-path shape and
passes it unchanged to `os.path.exists` (`:318`) and `open` (`:323`); such a
value with extra parts and `..` segments opens a file outside
`DDA_SYSTEM_FOLDER` when an intermediate directory with the capture-file name
exists; and the 404 detail echoes the value (`:321`).

1.4 WHEN `_failure_reason` (`text_generation.py:423-426`) receives an error
that is not a `GenerationError` THEN the system returns that error's truthy
`.reason` attribute or `str(error)` to the 502 body (`:662`) and the SSE error
event (`:713`), and the non-streaming path writes no log line for it.

1.5 WHEN a trigger payload carries a presigned URL THEN the system keeps its
signed values verbatim in `trigger_context_json`, in
`{output_dir}/{capture_id}.json` and in the `/metadata` response
(`api.py:543-561`), and the `tags:` line in the application log and in the
`/log` response (`api.py:483-497`) keeps the `X-Amz-Credential` and
`X-Amz-Date` values.

1.6 WHEN the request-validation handler logs a rejected request
(`exception_handlers.py:80-81`) THEN the system logs `query_params` without
masking. The shared redactor misses both forms of a mis-joined credential
(the re-encoded `token%3D<value>`, and a raw `token=<value>` that follows a
digit), so a credential inside a rejected query value is written to the log
unmasked.

1.7 WHEN the result-history download link is built
(`ResultCardContent.tsx:302`) THEN the system interpolates `captureIdPath`
unencoded and, with auth on, appends `token=<credential>` with no `&` before
it, so the server receives one `captureIdPath` value carrying the credential
and no `token` parameter.

### Expected Behavior (Correct)

2.1 WHEN the uncaught-exception handler builds a 500 response THEN the system
SHALL return exactly `{"message": "Internal Server Error.", "request_id": <id>}`
and SHALL CONTINUE TO log the full traceback with `exc_info`.

2.2 WHEN the input-image route uses its inference-results fallback THEN the
system SHALL resolve the path with `os.path.realpath` and SHALL open it only if
the resolved path lies strictly below `realpath(INFERENCE_RESULTS_DIR)`,
answering 404 with the fixed detail `"Server cannot find the input image path."`
otherwise; an id with an embedded NUL SHALL count as not contained; a read or
parse failure of a contained path SHALL return 500 with that same fixed detail
and SHALL be logged at WARNING; legitimate ids SHALL resolve and serve exactly
as today.

2.3 WHEN the export route validates `captureIdPath` THEN the system SHALL
accept only a value that matches the capture-id-path shape from start to end,
so any other value gets the app's request-validation 400 before the handler
runs; and the system SHALL open an accepted value only if its resolved path
lies strictly below `realpath(DDA_SYSTEM_FOLDER)` and exists, answering 404
with the fixed detail
`"The server can't get the capture id file. Check the path and try again."`
otherwise. A value produced by the companion POST writer
(`endpoints/inference_result.py:164-188`) SHALL be accepted and served exactly
as today.

2.4 WHEN `_failure_reason` receives an error THEN the system SHALL return
`error.reason` only for a `GenerationError` with a truthy `.reason`; otherwise
it SHALL return the fixed string `"internal error; see the LocalServer log"`
and SHALL log the error at ERROR with `exc_info`.

2.5 (OWNER DECISION D1) The system SHALL behave per the owner's answer:
- (a): no change; this requirement and Acceptance Criteria 10 and 11 are N/A.
- (b1): the system SHALL replace every SigV4 value with `***` (a value being
  the maximal run of characters other than `&`, `;`, `,`, `)`, `]`, `}`, `#`,
  a quote or whitespace) in the `trigger`
  section of the persisted run-metadata JSON, of the `tags:` log argument and
  of the `/metadata` response, and in the `/log` response text, for new runs
  and for runs recorded before the upgrade, preserving every other byte. What a
  run reads back SHALL be unchanged.
- (b2): (b1), and the system SHALL also mask every SigV4 value in **every
  string leaf** of the Trigger_Context before `trigger_context_json` is stored
  (the same values Layer A masks at the served and logged sinks), preserving
  every other byte and never re-serializing the `payload`. A run then reads
  back `***` for every SigV4 value in its trigger payload (owner memo D2.1).

2.6 WHEN the request-validation handler logs a rejected request THEN the system
SHALL withhold the query string from the log when its text names a sensitive
key (`_body_for_log`), and SHALL log a query string that names none
byte-identically to today. The 400 response SHALL be unchanged.

2.7 (owner decision D5 (b)) WHEN the result-history download link is built
THEN the system SHALL percent-encode `captureIdPath` with
`encodeURIComponent` and, only when auth is on, SHALL join the credential as
its own `&token=` parameter, percent-encoded the same way. Parsing the link's
query SHALL return exactly the `captureIdPath` and, with auth on, exactly the
credential as `token`; with auth off the link SHALL carry no `token`.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a route raises an explicit `HTTPException` or a mapped domain
exception, or a request fails validation, THEN the system SHALL CONTINUE TO
return that handler's exact body and status. Only the uncaught-500 body (2.1)
changes, plus the validation handler's log line for a query that names a
sensitive key (2.6).

3.2 WHEN the input-image or export route gets legitimate values THEN the
system SHALL CONTINUE TO serve the same image or zip, with the same status
codes and response shapes. The input-image route's database branch (a row
exists for `captureId`) SHALL be unchanged.

3.3 WHEN text generation fails with a `GenerationError` carrying a `.reason`
THEN the system SHALL CONTINUE TO return that reason. The 502 body
`{model_name, reason}` and the SSE event `{"error": {"reason": ...}}` SHALL be
byte-identical, and the 409, 422 and 504 mappings and the transient-retry path
SHALL be unchanged.

3.4 WHEN a trigger payload carries no SigV4 values THEN the system SHALL
CONTINUE TO store, persist, serve and log it byte-identically under every D1
option. Under (a) and (b1), every run SHALL read back exactly what it reads
today. Under every option, metadata bindings and `{trigger.…}` placeholders
over non-signed fields SHALL resolve unchanged, and the continuous-run
prefilter (`run_retention.py:93`) SHALL still match.

3.5 WHEN an image without the vLLM wheel runs THEN Item 4's change SHALL have
no runtime effect (the text-generation router is registered only when
`find_spec("vllm")` succeeds, `app.py:131-133`, `:196-197`).

3.6 WHEN the preservation suites run THEN they SHALL stay green. Any
intentional edit to a tracked file SHALL be rebaselined through its sanctioned
path and named in the commit. The one hash-pinned file this spec changes is
`src/backend/workflow_engine/pipeline_executor.py`, pinned in
`test/backend-test/csi_nvargus_optional/goldens/backend_csi_consumers_sha256.json`.
It changes only under D1 (b1) or (b2).

3.7 WHEN the frontend is built THEN it SHALL be unchanged except the
result-history download link (2.7, owner decision D5 (b)). That link SHALL
CONTINUE TO target the same route with the same `workflowId` segment, and
with auth off the server SHALL receive the same `captureIdPath` value as
today. Every other request the SPA builds SHALL be unchanged.

## Acceptance Criteria

1. (Item 1) On the fixed tree, an uncaught exception yields a 500 whose body is
   exactly `{"message": "Internal Server Error.", "request_id": <id>}`. The
   exception's class name and `str(value)` don't appear in it. (Req 1.1, 2.1)
2. (Item 1) The handler's log record for that exception carries `exc_info`
   naming the raised exception. (Req 2.1)
3. (Items 1, 3b) An explicit `HTTPException` and the request-validation 400
   return byte-identical bodies and statuses to the unfixed tree. (Req 3.1)
4. (Item 2) For a `captureId` with no inference-result row:
   - legitimate ids serve the seeded jsonl's image (200);
   - `workflowId=".."` returns 404 and opens nothing, even when
     `/aws_dda/<captureId>.jsonl` exists;
   - an id with an embedded NUL returns 404 on the shipped interpreters
     (Python 3.10 and 3.11);
   - a contained id whose jsonl is missing or malformed returns 500 with the
     fixed detail;
   - a missing image returns 404.

   With an inference-result row, the route serves the row's image as today.
   (Req 1.2, 2.2, 3.2)
5. (Item 3) The exact capture-id-path shape is accepted. Extra path parts,
   `..` segments, a trailing newline, or an appended `token=…` are rejected
   with 400 by the app's request-validation handler, body `{message,
   request_id}`, before the handler runs. An accepted value that resolves
   outside `DDA_SYSTEM_FOLDER` (a symlinked capture-file name) returns 404
   before any `open`, and so does a missing file. (Req 1.3, 2.3)
6. (Item 3) The `captureIdPath` returned by the companion POST writer still
   yields the retraining zip, with the same members. (Req 3.2)
7. (Item 3b)
   - For a rejected query whose value carries a dummy `token=` credential,
     neither the 400 body nor the handler's log record contains the dummy
     value.
   - A rejected query that names no sensitive key is logged byte-identically
     to the unfixed tree.
   - The download router stays a plain `APIRouter()` without the
     access-logging route class, so that handler record is the request's
     only backend log line.

   (Req 1.6, 2.6, 3.1)
8. (Item 4) `_failure_reason` returns the fixed string, and logs with
   `exc_info`, for a non-`GenerationError`. That includes a non-runtime
   exception that carries a `.reason` attribute. It returns `.reason`
   unchanged for a `GenerationError` that carries one. (Req 1.4, 2.4)
9. (Item 4) For a `GenerationError` with a `.reason`, the 502 and SSE bodies
   are byte-identical to the unfixed tree. The 409, 504 and transient-retry
   behavior is unchanged. (Req 3.3)
10. (Item 5; N/A under D1 (a)) A run whose trigger payload carries dummy SigV4
    values:
    - none of the dummy values appears in the `trigger` section of the
      persisted metadata JSON, the `tags:` line after the log filter and the
      `/metadata` response, or anywhere in the `/log` response;
    - each SigV4 value reads `***`, and every other byte is preserved;
    - a run recorded before the upgrade is served masked by `/metadata` and
      `/log`;
    - under (b2), the stored column is masked at every string leaf:
      `load_trigger_context(stored)["payload"]` equals
      `redact_signed_query(original_payload)` (so `payload_json` re-derives with
      the same keys), and a SigV4 value in a non-`payload` string leaf of the
      stored context reads `***`.

    (Req 1.5, 2.5)
11. (Item 5; N/A under D1 (a)) `redact_signed_query` is total: a non-string is
    returned unchanged. It is a fixed point: a second pass writes the same
    mask. This holds over the literal `name=value` form. (Req 2.5)
12. (Item 5; every D1 option) For contexts without SigV4 values, the stored
    column, the persisted metadata JSON, the `tags:` line, and the `/metadata`
    and `/log` responses are byte-identical to the unfixed tree. (Req 3.4)
13. The preservation suites stay green:
    - under (b1) or (b2), the CSI consumer golden entry for
      `pipeline_executor.py` is rebaselined in the Layer A commit, and
      recomputed from the merged file on the combined build ref;
    - the guard suite and the build-gate replay are green before each build.

    (Req 3.6)
14. (OWNER-gated) On each affected architecture, a built-and-deployed
    component passes this spec's device checks. A soak of at least 30 minutes
    follows, with at least 10 workflow runs and at least 10 requests per
    exercised route, and no backend crash, container restart or crash-loop.
    The MIC-730 and Orin passes include the jpeg folder-source non-regression
    check (defined in design "Per-item device check content"; it doesn't
    replace the jpeg spec's thor1 check). At least one auth-enabled station
    passes the frontend export-link check (Item 3c). The lab Dell checks are
    GATED on the owner confirming the lab Dell is up. (Req 2.1-2.7; steering
    `builds.md`)
15. (OWNER-gated) After the trunk merge and the trunk rescan, each of the five
    changed static-analysis findings is closed or dismissed with a recorded
    reason:
    - `exception_handlers.py:107` (Item 1);
    - `download_file.py:113` (Item 2);
    - `download_file.py:318` and `:323` (Item 3);
    - `text_generation.py:660`, with its second path at `:713` (Item 4).

    The six dismissals above were applied on 2026-10-15 (owner decision D4)
    and aren't part of this criterion. Items 3b, 3c and 5 have no standalone
    finding. They're verified by Acceptance Criteria 7, 10-12 and 16, not
    counted among the five. (Completion criterion)
16. (Item 3c; owner decision D5 (b)) `buildExportUrl` returns
    `<workflows endpoint>/<workflowId>/results/export?captureIdPath=<encoded>`,
    plus `&token=<encoded credential>` only when auth is on. For any path and
    credential, parsing its query returns exactly the `captureIdPath` and,
    with auth on, exactly the credential as `token` (property test). The
    result-history download link is built only through it, and the SPA's
    test suite and build stay green against their pre-change baseline.
    (Req 1.7, 2.7, 3.7)

## Out of Scope

- The default open-access configuration, the plaintext listener and the
  per-request authorization matrix. The fixes hold in both configurations and
  don't change authorization.
- The six items left unchanged and their optional one-line hardenings.
- Other SPA links that carry `token=`. They already join it with `&` (for
  example `api/WorkflowAPI.ts:179`); only the result-history link changes
  (Item 3c).
- The access-logging route class (`endpoints/route/access_log_router.py`) and
  the shared redactor's name-matching rule. The access-log route logs the raw
  request line only for the routers built on it (`get_api_router()`,
  `local_auth`, `auth_info`). It doesn't log the download routes, so it isn't
  a sink for Item 3b. Item 3b masks at the validation handler instead of
  changing either shared component.
- About fifteen other `HTTPException(detail=f"…{e}…")` sites that put error
  text in a mapped response (for example `endpoints/workflow.py:148`,
  `endpoints/streams.py:303`, `resources/accessors/workflow_accessor.py:251`,
  `utils/gg_utils.py:83`). This spec covers the uncaught-exception path and the
  two download routes it changes.
- Rewriting files already on the device. Layer A masks the served views of
  earlier runs; it doesn't rewrite their on-disk metadata JSON or `run.log`.
  Under (b2), it doesn't rewrite stored rows either. Earlier application-log
  lines age out with the existing rotation: hourly files kept 14 days, size
  capped (`dda_logging/custom_logging.py:138-149`).
- SigV4 values outside the literal `name=value` form. Examples are the
  percent-encoded `name%3Dvalue` of a URL nested inside another URL's query,
  and header-style JSON keys such as `"x-amz-security-token": "…"`.
- The tuning sample export's metadata snippet
  (`workflow_engine/tuning/sample_export.py:762-800`). It carries the `trigger`
  section only when a node's prompt template references `{trigger.…}`. After
  Layer A, backfilled samples, which are read from the persisted metadata,
  carry the masked form, and live samples carry the live form.
- `.kiro/specs/security-scan-remediation-high/requirements.md`, whose content
  is pinned.
- The vLLM runtime's loopback companion server and its own error bodies
  (`vllm_runtime/server.py:160`, `:215`). It binds the loopback interface only
  and is not a LocalServer API route.
