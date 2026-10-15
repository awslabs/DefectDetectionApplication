# LocalServer Input-Handling and Error-Response Hardening Bugfix Design

## Overview

This design hardens five request-handling points in the shipped LocalServer
backend, in one build round: a 500-response body that returns internal error
text (Item 1); two request-input-to-path flows that open a file without a
containment check (Items 2 and 3), with the log line of a now-rejected export
query closed alongside (Item 3b); a text-generation failure reason that returns
raw error text on vLLM-capable images (Item 4); and the trigger payload's
retention of presigned-URL query material across its stored, served and logged
sinks (Item 5, an owner decision designed both ways). Each fix is local to the
handler or helper that owns the input. Every change preserves current behavior
except the two places called out: Item 1's 500 body, and Item 5's full form,
which the owner gates.

The technology stack is locked and unchanged: the Python FastAPI backend
(pydantic 2.6.4 / fastapi 0.109.2 / starlette 0.36.3 / uvicorn 0.23.2 on the
device image; backend interpreter 3.11 on JP5/JP7/x86 and 3.10 on JP6); the
`os.path.realpath` + strictly-below `startswith` containment idiom already used
in the backend (`run_retention._within`); the standard library `re` for the new
SigV4 masker; and the repo's `pytest` + `hypothesis` stack. No new dependency,
framework or module is added, and no analyzer suppression is introduced.

Items 1-4 are deterministic, local edits validated by host-run unit and
property tests. The two containment properties also run in the device
container, because `os.path.realpath` treats an embedded NUL differently by
interpreter. The development host's Python 3.9.5 doesn't raise. The device
interpreters (3.10 on JP6, 3.11 elsewhere) raise `ValueError`. That was
confirmed offline under 3.11.9 in the local backend image and under a local
3.10.12 interpreter. Testing Strategy records which image the container runs
use.

Item 5 is a persistence/redaction change validated the same way and gated by
an owner decision. Its full form (masking the stored `trigger_context_json`)
changes what a run reads back: every SigV4 value the run takes from its
trigger payload reads `***`.

## Glossary

- **Containment check** — resolving a candidate path with `os.path.realpath`
  and requiring it to lie **strictly below** a resolved base directory, using
  the prefix check `resolved.startswith(base + os.sep)`, with the `realpath`
  calls wrapped in `except ValueError` so an embedded NUL or an unrelated drive
  counts as not contained. "Strictly below" excludes `resolved == base`. This
  is the idiom already in `workflow_engine/run_retention.py:268-272`
  (`realpath(path).startswith(real_root.rstrip(os.sep) + os.sep)`); the
  `realpath`-inside-`try` placement follows
  `workflow_engine/python_bridge.py:1470-1484`. See DD1 for why the prefix form
  is chosen over `os.path.commonpath([resolved, base]) == base`.
- **`INFERENCE_RESULTS_DIR`** — `/aws_dda/inference-results` (`constants.py:34`).
- **`DDA_SYSTEM_FOLDER`** — `/aws_dda/system` (`constants.py:32`).
- **`GenerationError`** — the vLLM runtime error carrying `.model_name` and
  `.reason` (`vllm_runtime/manager.py:560-569`); the intended backend-reason
  contract for text generation (Requirement 5.7).
- **vLLM-capable image** — an image whose build installed the vLLM wheel, so
  `app.py`'s `importlib.util.find_spec("vllm")` probe (`:131`) registers the
  text-generation router (`:196-197`): JP6 and JP7 (`VLLM_ENABLE=1` default).
  JP5 defaults to `VLLM_ENABLE=0`; the generic amd64, arm64 CPU and
  x86_64_nvidia images install no wheel.
- **SigV4 query parameters** — the signed query parameters of a presigned
  object-store URL: `X-Amz-Algorithm`, `X-Amz-Credential`, `X-Amz-Date`,
  `X-Amz-Expires`, `X-Amz-SignedHeaders`, `X-Amz-Signature`,
  `X-Amz-Security-Token`.
- **Trigger_Context** — the `{topic, payload, qos, timestamp}` dict the MQTT
  subscribers build (`trigger_runtime.py:1845-1852`, `:2147-2154`), persisted
  as JSON in `trigger_context_json` and reloaded into the run.
- **Layer A / Layer B** — Item 5's two independently-adoptable layers: Layer A
  masks the served and logged sinks (no change to what a run reads back); Layer
  B also masks the stored column (the behavior change).

## Bug Details

For each item, `isBugCondition(input)` below is a predicate over what the
shipped code does for a concrete input. It holds on the unfixed tree. Each
task-1 exploration test drives that input through the real shipped code, not
a stub, and asserts the expected behavior, `not isBugCondition(input)`. So
every exploration test FAILS on the unfixed tree, and that failure is the
recorded counterexample. It then PASSES, unchanged, after the fix. This is
the repo's convention (for example `workflow-output-bindings-fixes` task 1).
Inputs outside the bug condition (`¬C`) are preservation cases and live in
task 2, not task 1.

### Item 1 — 500-response body (`exception_handlers.py:97-107`)

```
isBugCondition(exc):
    # raised and handled inside an except block, as in a real request
    try:
        raise exc
    except type(exc):
        resp = asyncio.run(unhandled_exception_handler(request, exc))
    body = json(resp.body)
    return body["message"] != "Internal Server Error."
    # unfixed: the message embeds type(exc).__name__ and str(exc)
```

The handler reads `sys.exc_info()` (`:102`), so it must be called inside the
`except` for the predicate to be meaningful: called outside one it sees
`(None, None, None)` and the body is literally `'None: None'` (confirmed on the
host). For `KeyError("source-ref")` raised and handled, the unfixed body is
`Internal Server Error. Error: 'KeyError: 'source-ref'.'`. The SPA surfaces
`data.message` to the operator; the full traceback is already logged with
`exc_info` (`:105`) and is not in the body. The exploration test asserts
`body["message"] == "Internal Server Error."`. The kept `exc_info` record is
a preservation case (task 2).

### Item 2 — input-image inference-results path (`download_file.py:98-140`)

```
isBugCondition(workflowId, captureId):   # evaluated via the lifted route fn
    # no inference-result row for captureId -> the jsonl fallback runs
    opened = the path the route passes to open()
    return realpath(opened) is not strictly below realpath(INFERENCE_RESULTS_DIR)
    # satisfiable with workflowId == "..", captureId slash-free
```

The jsonl fallback runs only when `inference_result_accessor.get_inference_result`
returns `None` (`:108-109`); with a row the route serves `inputImageFilePath`
and ignores `workflowId`. The branch itself runs on every image, because
`setup_triton()` sets `is_triton=True` at startup (`app.py:309`, `:418`), so
`get_is_triton()` is true. Both ids are starlette `[^/]+` segments — no slash,
and uvicorn decodes `%2F` before routing, so an encoded-slash request 404s at
the router (reproduced offline). The only movement is a literal `..` workflow
id, which opens `/aws_dda/<captureId>.jsonl`. The file is `json.load`-ed and
only `source-ref` is returned, so it is not a general read primitive — but
request input reaches a filesystem path uncontained, which is the condition.
A second, output-encoding predicate covers the inner 500: for a contained id
whose jsonl is malformed, the unfixed detail interpolates the exception text
(`:117-121`). The exploration tests assert the negations: `workflowId=".."`
gets 404 with no `open` call, and the malformed contained jsonl gets 500 with
exactly `"Server cannot find the input image path."`.

### Item 3 — export captureIdPath (`download_file.py:298-357`, `constants.py:109`)

```
validatorAccepts(captureIdPath):    # through the mounted lifted route
    return GET export?captureIdPath=<value> reaches the handler (status != 400)

routeOpensOutside(captureIdPath):   # direct call of the lifted fn, temp tree
    return the route opens realpath(captureIdPath), which is not strictly
           below realpath(DDA_SYSTEM_FOLDER)

isBugCondition(captureIdPath):
    return validatorAccepts(captureIdPath) or routeOpensOutside(captureIdPath)
    # satisfiable with ".../capture-id-path-<ts>/../../../<target>" when the
    # ".../capture-id-path-<ts>" directory exists on the path
```

The exploration test asserts both negations for the `..`-segment value: the
mounted route answers 400, and the direct call raises 404 with no `open` of
the target. A third assertion covers output encoding: the 404 detail for a
missing accepted value is the fixed string, without the echoed input (`:321`).

The request validator applies `pattern=` with search, not full-match
semantics. `re.fullmatch` already rejects the `..`-segment value against the
unfixed constant; `re.search` accepts it. That's why the validator half is
asserted through the mounted route, which runs the real engine, rather than
through Python `re`. The `re.search`/`re.fullmatch` contrast on the unfixed
constant is recorded in `verification-notes.md` "Task 1" as an observation,
not kept as a frozen test: it would fail after the anchor.

For the open to happen, the directory named like the capture file must exist
along the path, because Linux returns ENOTDIR for `file/..`. So the
direct-call half of the test seeds `<tmp>/aws_dda/system/capture-id-path-<ts>/`
as a directory and a `target.json` list outside it. It patches
`DDA_SYSTEM_FOLDER` to `<tmp>/aws_dda/system` and passes
`.../capture-id-path-<ts>/../../../target.json`. The unfixed route opens it;
the fixed route returns 404. The companion POST writer
(`inference_result.py:181-187`) returns a server-generated
`/aws_dda/system/capture-id-path-<ts>` path, so a legitimate value never needs
`..` or extra parts.

### Item 3b — the log of a rejected export query (`exception_handlers.py:71-87`)

```
isBugCondition(query_value_naming_token):
    exc = RequestValidationError(..., ) for a rejected captureIdPath
    logged = caplog of request_validation_exception_handler(request, exc)
    return DUMMY_TOKEN_VALUE in logged
```

The handler logs a dict holding the `QueryParams` object (`:76`, `:80-81`).
The browser sends the mis-joined link (`ResultCardContent.tsx:302`, no `&`
before the appended `token=`) with `=` and `/` raw, because neither is in the
URL query percent-encode set. So `captureIdPath` arrives as
`/aws_dda/system/capture-id-path-<ts>token=<credential>`. The shared
redactor (`stream_url.redact`) masks neither form of that value:

- the handler logs `repr(QueryParams)`, which re-encodes the value to
  `...token%3D<credential>`, and the `name=value` rule doesn't match `%3D`;
- the raw form isn't masked either. `_SECRET_VALUE_RE` requires that no
  letter, digit or `_` precede the name (`stream_url.py:411-421`), and here
  `token` follows the timestamp's last digit.

Both behaviors were confirmed offline with dummy values in the local backend
image. `redact` left the dummy value in both forms. It masked the
properly joined `...&token=<value>` form to `token=***`.

Sinks for this request. The validation handler is the only backend log line
that carries the value:
- The download routes are registered on a plain `APIRouter()`
  (`download_file.py:56`), so they don't use `AccessLogRoute`.
  `include_router` keeps a route's class. The offline probe confirmed it: the
  same request emitted one `api.access` record (credential unmasked) from an
  `AccessLogRoute` route and none from a plain-router route. `health.py`'s
  module docstring describes the same shape: a plain `APIRouter`, "the
  `download_file.unauthenticated_router` shape", used instead of
  `AccessLogRoute` so its requests stay out of the access log.
- uvicorn's access logger writes to a `NullHandler` with propagation off
  (`dda_logging/uvicorn_disable_logging.json`; `custom_logging.py` also clears
  it).
- `context_var_middleware` logs nothing.

Today the unanchored pattern accepts the value. The handler then raises 401
through `_raise_unauthorized` (`utils/auth.py:126-131`), which doesn't log,
and `http_exception_handler` doesn't log either. After Item 3 anchors the
pattern, the value is rejected at validation, and this handler would log the
credential. Item 3b masks it. The exploration test asserts that the dummy
value is absent from both the handler's log records and the 400 body.

### Item 4 — `_failure_reason` (`text_generation.py:423-426`, sinks at `:662`, `:713`)

```
isBugCondition(error):
    return (not isinstance(error, GenerationError)) and \
        _failure_reason(error) != "internal error; see the LocalServer log"
    # unfixed body (:425-426): reason = getattr(error, "reason", None)
    #                          return reason if reason else str(error)
    # e.g. ValueError("boom") -> "boom", or a non-GenerationError object
    # carrying a .reason attribute -> that attribute
```

The exploration test asserts the negation: for those errors
`_failure_reason` returns exactly `"internal error; see the LocalServer log"`.

`ModelUnavailableError` is mapped to 409 earlier (`:640-646`) and never reaches
`_failure_reason`. `GenerationError` is the only runtime error that carries
`.reason` (`manager.py:560-569`), and the engine wraps `str(err)` into that
reason (`manager.py:1807`). Any other unexpected exception reaches
`_failure_reason` with no contract `.reason`, so the caller gets its truthy
`.reason` attribute or `str(error)` — raw error text — in the 502 body
(`:662`) and the SSE event (`:713`). The two sinks share the one helper.

### Item 5 — trigger payload presigned-URL material

```
isBugCondition_A(payload_with_sigv4_url):   # served/logged sinks; dummy values only
    persist a run whose trigger payload embeds the dummy presigned URL
    served  = read_run_metadata(output_dir, capture_id)   # == /metadata body
    logged  = tags line formatted through RedactingFilter  # == /log tags line
    return any_dummy_sigv4_value_in(served) or any_dummy_sigv4_value_in(logged)

isBugCondition_B(payload_with_sigv4_url):   # stored column; only under D1 (b2)
    return any_dummy_sigv4_value_in(trigger_context_json(row))
```

The predicates test the dummy **values**, not the parameter names (the names
survive the fix). The task-1 exploration test asserts `not isBugCondition_A`:
it fails on the unfixed tree and passes after Layer A. Under D1 (a) it stays
failing, so it's marked `xfail(strict=True)` to record the accepted behavior.
`not isBugCondition_B` is asserted only under (b2), by the Layer B case added
in task 4.3. Under (b1), the stored column keeps the values by design.
Offline reproduction against the vendored redactor (dummy
values only) fixes the masking boundary: in the `tags:` line, `X-Amz-Signature`
and `X-Amz-Security-Token` are masked (the `-` before the name makes
`signature`/`token` whole tokens for the existing rule), while
`X-Amz-Credential` and `X-Amz-Date` are not; the served run-metadata JSON and
the stored column keep the whole URL. The payload-reference fetch path has its
own redactor that drops the whole query (`payload_fetch.py:663-674`), but it
is used only for that path's logging, not for the metadata or `tags:` paths.

## Expected Behavior (Preservation Requirements)

- P-handlers: `http_exception_handler` (`:90-95`), the request-validation
  handler's response body (`:85-87`) and the mapped domain handlers
  (`:111-162`) are untouched. Only the uncaught-500 body (Change 1) changes,
  plus the validation handler's **log** line for a query naming a sensitive
  key (Change 3b). A secret-free query's log line is byte-identical (Req 3.1).
- P-routes: legitimate input-image and export calls serve the same artifact
  with the same status and shape; the input-image database branch is unchanged
  (Req 3.2).
- P-genreason: a `GenerationError` with `.reason` returns that reason; the
  502/SSE JSON shapes are byte-identical; 409/504/retry unchanged (Req 3.3).
- P-trigger: a context with no SigV4 value is stored, persisted, served and
  logged byte-identically under every D1 option; `{trigger.…}` resolution over
  non-signed fields and the continuous-run prefilter are unchanged (Req 3.4).

## Hypothesized Root Cause (confirmed)

- Item 1: the handler echoes the exception summary for operator convenience,
  but puts it in the response body rather than only in the `exc_info` log.
- Items 2 and 3: the download routes predate the containment idiom now standard
  in the backend; Item 3 additionally relies on a pattern that the validator
  matches by search, so it constrains a substring, not the whole value.
- Item 3b: the validation handler logs `query_params` unmasked, trusting the
  shared redactor. That redactor misses a mis-joined credential in both forms:
  `%3D` in the re-encoded text, and its whole-token rule on the raw text. The
  earlier unanchored pattern meant the auth-enabled download value never
  reached this handler.
- Item 4: `_failure_reason` falls back to `str(error)` for the general case
  instead of distinguishing the `GenerationError` contract from an unexpected
  error.
- Item 5: redaction exists for the stream-credential and payload-fetch-logging
  paths, but not for the trigger persist/serve/`tags:` paths, and the shared
  redactor's name set omits `credential` and `date`.

## Correctness Properties

- **Property 1 (Item 1 fix).** For every exception raised and handled inside an
  `except`, the 500 body `message` equals the fixed constant
  `"Internal Server Error."` and the body carries `request_id`.
- **Property 2 (Item 1 preservation).** `http_exception_handler` and the
  request-validation handler produce byte-identical **response bodies** to the
  unfixed tree over a generated exception set. The uncaught handler's log
  record keeps carrying `exc_info` for the raised exception.
- **Property 3 (Item 2 fix + containment).** Over ids drawn from
  `st.text(alphabet=st.characters(blacklist_characters="/\x00"))` ∪
  `{".", ".."}` on a seeded temp tree, when the resolved jsonl path is not
  strictly below `realpath(INFERENCE_RESULTS_DIR)` the route answers 404 and
  never calls `open`; when it is contained the outcome matches today's — 200
  for a readable jsonl whose `source-ref` exists, 500 with the fixed detail for
  a missing or unparseable jsonl, 404 for a missing image. A slash-free id with
  no `..` resolves to exactly today's path. The embedded-NUL case is a separate
  explicit member (`"a\x00b"`) carrying `skipif(sys.version_info < (3, 10))`,
  not a generated id: the generator excludes `\x00` because on the host
  interpreter (3.9.5) `realpath` does not raise on a NUL, so a generated NUL id
  would resolve as contained, `open` would raise, and the route would answer
  500 — a false failure, not the 404 the property asserts. The device container
  (3.11) and the explicit skip-guarded member cover the NUL semantics. Run in
  the device container.
- **Property 4 (Item 3 anchor + containment).** `captureIdPath` is accepted by
  the validator iff it matches the anchored shape start-to-end; an accepted
  value is opened only if its resolved path is strictly below
  `realpath(DDA_SYSTEM_FOLDER)` and exists; the POST writer's output is
  accepted. The validator property is asserted through the mounted route, since
  the engine's anchoring (not Python `re`) is what governs. Run in the device
  container for the containment half (NUL semantics).
- **Property 5 (Item 4 fix).** `_failure_reason(e)` returns `e.reason` iff `e`
  is a `GenerationError` with a truthy `.reason`, else `"internal error; see
  the LocalServer log"`; the else branch emits one ERROR record with `exc_info`.
- **Property 6 (Item 4 preservation).** The 502/SSE JSON shapes are byte-
  identical for a `GenerationError` with `.reason`.
- **Property 7 (Item 5 fix).** For strings embedding SigV4 parameters in the
  literal `name=value` form in random order and casing,
  `redact_signed_query` replaces every SigV4 value with `***`, preserves every
  other byte, is a fixed point (a second pass writes the same mask), and
  returns a non-string unchanged. Scoped to the literal form; the
  percent-encoded and JSON-key forms are out of scope (bugfix Out of Scope).
- **Property 8 (Item 5 preservation).** A context with no SigV4 value is
  returned byte-identically by `redacted_run_view` and `redact_signed_query`.
  Checked at the post-fix checkpoint (it describes the fixed helper).
- **Property 9 (Item 3b fix + sink guard).** For a rejected request whose
  query value carries a dummy `token=` credential, neither the 400 body nor
  the handler's log record contains the dummy value. For generated query
  strings that name no sensitive key, the handler's log record is
  byte-identical to the unfixed tree (observation-first, task 2). The generator
  defines "names no sensitive key" on the re-encoded text the handler actually
  logs: `not any(k in str(QueryParams(raw)).lower() for k in
  _SENSITIVE_BODY_KEYS)`. This is required because `str(QueryParams)`
  normalizes percent-encoding — a raw `a=%74oken` carries no literal `token`
  but renders as `a=token` — so filtering on the raw string would admit a value
  that the logged form withholds, a false mismatch. Structural guard:
  `download_file.unauthenticated_router` is built as `APIRouter()` without a
  `route_class` keyword. So the validation handler's record is the only backend
  log line for a rejected export query, and moving the download routes onto
  `AccessLogRoute` fails this test instead of silently adding an unmasked sink.

### Properties summary table

| Property | Item | Kind | Requirements | Where tested |
|---|---|---|---|---|
| 1 | 1 | fix | 1.1, 2.1 | host unit/property on the handler (inside `except`) |
| 2 | 1 | preservation | 3.1 | host unit/property on the two handlers' bodies |
| 3 | 2 | fix + containment | 1.2, 2.2, 3.2 | property on the lifted route fn — device container |
| 4 | 3 | fix + anchor + containment | 1.3, 2.3, 3.2 | property via the mounted route + lifted fn — device container |
| 5 | 4 | fix | 1.4, 2.4 | host unit/property on `_failure_reason` |
| 6 | 4 | preservation | 3.3 | host unit on the 502/SSE bodies |
| 7 | 5 | fix (incl. AC11) | 2.5 | host property on `redact_signed_query` / `redacted_run_view` (task 4.1) |
| 8 | 5 | preservation | 3.4 | host property on the fixed helper (task 4.1) |
| 9 | 3b | fix + preservation + sink guard | 1.6, 2.6, 3.1 | host unit/property on the validation handler's log; host `ast` guard on the download router |

## Fix Implementation

### Design Decisions

- **DD1 — containment idiom and its guard form.** Items 2 and 3 resolve the
  candidate with `os.path.realpath` and require it to lie **strictly below** a
  resolved base directory with the prefix check
  `resolved.startswith(base + os.sep)`, both `realpath` calls wrapped **inside**
  one `try/except ValueError`. The `realpath` calls sit inside the `try`
  because on the device interpreters (3.10 on JP6, 3.11 elsewhere)
  `os.path.realpath` raises `ValueError` on an embedded NUL (confirmed offline
  under 3.11.9 and 3.10.12; the host's 3.9.5 does not raise, so the NUL cases
  run in the container). On `ValueError` the candidate counts as not contained.
  - **Why the prefix form, not `commonpath([resolved, base]) == base`.** Both
    are three lines of a repo idiom, but they differ in two ways that decide the
    choice here, and the choice is locked before the build because changing it
    after task 7 costs another build round per architecture (device code):
    1. **Strictly below for free.** `startswith(base + os.sep)` rejects
       `resolved == base` (a capture-name symlink to the directory itself,
       which would otherwise reach `open()` on a directory), whereas
       `commonpath([resolved, base]) == base` admits it;
       `python_bridge.py:1477` adds an explicit `resolved == root` reject for
       exactly that reason.
    2. **What the path-handling rule models as a check.** The prefix form
       (`realpath` + `startswith(base + os.sep)`) is the normalize-then-prefix
       shape the analyzer recognizes as a containment check, so it is expected
       to clear the `download_file.py:113`, `:318` and `:323` findings on the
       rescan rather than leave them for manual dismissal. The `commonpath`
       equality form is modelled differently and would, by this design's own
       premise, leave those lines open.
    The prefix form is already a backend idiom: `run_retention._within`
    (`:268-272`) is `os.path.realpath(path).startswith(real_root.rstrip(os.sep)
    + os.sep)`. The `realpath`-inside-`try` placement follows
    `python_bridge.py:1470-1484` (`static_image_camera.py:153-158` keeps its
    `realpath` outside the `try`, so it is not cited as the idiom for that
    placement). A plain string check that only rejects `..` ids was considered
    and rejected: it misses a symlink that resolves outward, and resolving the
    path is the repo idiom. Containment failure maps to each route's existing
    404, so no new status is added.
  - **Residual-dismissal fallback.** The prefix form is expected to clear the
    rescan. If a rescan still flags a guarded line, it is dismissed in task 11
    as "contained: realpath + strictly-below prefix guard" — the fallback, not
    the plan. (Whether the rule models the prefix form as a check is confirmed
    only by the post-merge rescan; see Open Questions.)
- **DD2 — anchor the pattern at its definition.** Item 3 anchors
  `CAPTURED_IDS_PATH_PATTERN` with `^...$` in `constants.py` (its one consumer
  is `download_file.py:306`, confirmed). The validator then rejects a
  non-conforming value with the app's request-validation **400** (not 422: the
  app registers `request_validation_exception_handler`, which answers 400 at
  `exception_handlers.py:87`, and the existing suites pin 400). The filesystem
  containment check is defense-in-depth behind it, and also catches a symlink
  inside `DDA_SYSTEM_FOLDER` that resolves outward. Anchoring is chosen over
  "strip to basename" because the value is a full path by contract (the POST
  writer returns an absolute path the GET echoes), so basename would break it.
- **DD3 — fixed strings, keep the logs.** Items 1, 2 and 4 return fixed response
  text and keep (Item 1) or add (Items 2, 4) a log line, so an operator still
  gets the detail from the LocalServer log. For Item 1 the response body carries
  the existing `request_id`, so a log reader can correlate the fixed body with
  its traceback line by that id in the LocalServer log. The device SPA does
  **not** surface `request_id` (no `request_id`/`requestId` use anywhere in
  `src/frontend/src`), so an operator reading the UI sees only the fixed message
  and correlates out-of-band — by reading the response JSON or by a time-window
  search of the LocalServer log; folding the id into the fixed body text is an
  owner option (owner memo D2.2). For Item 1, two alternatives were considered
  and rejected:
  - Keeping the class name and dropping only the value. Class names still
    describe internals, and the SPA shows `data.message` verbatim.
  - Returning a sanitized summary of `str(value)`. There's no bounded rule for
    what is safe to keep.
- **DD4 — Item 4 helper shape (re-verified against this tree).** `_failure_reason`
  becomes:

  ```python
  def _failure_reason(error: BaseException) -> str:
      """The backend failure reason of a generate error (Requirement 5.7)."""
      if isinstance(error, GenerationError) and error.reason:
          return error.reason
      logger.error("Unexpected text generation failure", exc_info=error)
      return "internal error; see the LocalServer log"
  ```

  The delta is from the current body (`:425-426`:
  `reason = getattr(error, "reason", None)`;
  `return reason if reason else str(error)`) to an
  `isinstance(error, GenerationError)` test. Keeping the `getattr` and only
  dropping the `str(error)` fallback was rejected. Unrelated standard-library
  exceptions also carry a `.reason` attribute, and those would still reach
  the caller. Examples are `UnicodeDecodeError` and `urllib.error.URLError`,
  whose `.reason` can be an `OSError` naming a file path.
  `GenerationError` is the only vLLM runtime error carrying `.reason`
  (`manager.py:560-569`); `ConstructionTimeout`
  is raised only during construction (`manager.py:1148`, `:1159`), which
  `generate` can't reach after `_not_ready_response` (`text_generation.py:585`,
  `:694`), so the change is safe. The consumer of these reasons on the workflow
  side is `_default_llm_invoker` (`output_bindings.py:2929`), which POSTs the
  non-streaming `/generate` route (`:2738`) and, on a non-200, raises a
  `RuntimeError` embedding the 502 body (`:3033-3036`), recorded by the
  processor as `{"error": str(e)}` (`:3348`); its shape is unaffected. The
  streaming path logs twice on an unexpected error (the existing `:709-712`
  line plus the helper's); that duplicate is acceptable — both are device-log
  only — and is noted rather than removed. One behavior note on the streaming
  path: `_not_ready_response` (`text_generation.py:694`) runs before the stream
  starts, but `generate_stream` calls `_ready_engine` on the first iteration
  inside `events()` (`manager.py:1755` via `_request`, reached from
  `text_generation.py:704-713`), so a READY→not-READY race raises
  `ModelUnavailableError` **inside** the stream. That error has no `.reason`
  (it carries `.status`), so after the fix the single SSE error event reads the
  fixed string instead of today's `"model '…' is not ready (state: …)"` text.
  This is accepted: it matches Item 4's intent (only a `GenerationError`
  contract reason is surfaced), the pre-stream 409 for an already-not-READY
  model is unchanged, and the state is still in the LocalServer log. Residual: a
  `GenerationError.reason` still derives from the engine's `str(err)`
  (`manager.py:1807`), so the analyzer may keep flagging that path; it is the
  Req 5.7 contract reason, dismissed in task 11, not changed.
- **DD5 — Item 5 masking scope, and the frontend join.** Mask the SigV4
  parameter **values** by name, case-insensitively, preserving every other
  byte, with the mask `***` (the repo's mask, `stream_url.py:101`, so a line
  that also passes through the shared redactor stays single-token). This is
  chosen over (i) adding `credential`/`date` to the shared
  `SECRET_QUERY_PARAMETERS` set — rejected: that set is in the vendored
  `stream_url.py`, which must stay byte-identical to the Portal layer
  (`test_vendored_catalog_mirror.py:82`), so editing it is a Portal change and
  deploy, out of scope; `date` is also a common non-secret name and would
  over-mask other logs — and over (ii) dropping the whole query like
  `payload_fetch._redact` — rejected because it also discards non-signed query
  fields a metadata binding may map. The separate frontend export-link join
  (`ResultCardContent.tsx:302`, a missing `&` before the appended token) is its
  own owner decision (DD8 below, owner memo D5).
- **DD6 — Item 5 layers and the behavior change.** The run reloads the context
  from `trigger_context_json` (`pipeline_executor.py:1559-1561`) and the
  Bedrock reference-image path fetches a URL resolved from `payload_json`
  (`output_bindings.py:2313-2321`). Therefore:
  - **Layer A** masks the **served run-metadata JSON** (at
    `_persist_run_metadata`), the **`/metadata` response** (at the serve
    boundary), the **`tags:` log argument**, and the **`/log` response** — the
    four sinks the run never reads back. Zero change to run behavior. It covers
    runs recorded before the upgrade by masking at serve time, not only at
    persist time.
  - **Layer B** masks the **stored column** (at `serialize_trigger_context`).
    It's the only part that changes what the run reads back. Every SigV4
    value a run reads back from the trigger payload is masked:
    - a presigned-URL fetch (`output_bindings.py:2313-2321`) receives the
      masked URL and fails;
    - a signed value surfaced through a metadata binding
      (`resolve_metadata_binding`, `output_bindings.py:460`, resolved from
      `payload_json`) reads `***`;
    - a signed value surfaced through a `{trigger.…}` placeholder
      (`llm_inference.render_prompt`, `llm_inference.py:65`) reads `***`.

    Non-signed fields are unchanged. This is the "download paths stop
    working" change, routed through owner decision D1.
- **DD7 — one build round, batched with jpeg.** Every item is backend device
  code; one LocalServer build per architecture carries them all, and the design
  recommends batching with the pending jpeg build round. See "Build Rounds".
- **DD8 — Item 3b masks at the validation handler.** That handler is the only
  backend sink for a rejected export query (Bug Details, Item 3b). Chosen: log
  the query through the module's own `_body_for_log`. It withholds any text
  naming a sensitive key as a substring, so it covers the re-encoded and raw
  forms alike. A query naming no sensitive key keeps its exact
  `QueryParams(...)` log text. Rejected alternatives:
  - Teaching the shared `stream_url.redact` to decode `%3D`, or relaxing its
    whole-token rule. The file is the vendored Portal mirror
    (`test_vendored_catalog_mirror.py:82`). The whole-token rule is what keeps
    `stream_key` from matching as `key`.
  - Masking the query in `AccessLogRoute.log_info`. That route class isn't on
    the download router, so the change would alter no outcome for this item,
    while changing the access log of every route built on it.

  The frontend join (`ResultCardContent.tsx:302`) stays a separate owner
  option (owner memo D5). It repairs the auth-enabled download itself, and
  the properly joined `&token=` form is one the shared redactor already masks
  (confirmed offline). Item 3b doesn't depend on it. A frontend fix rides the
  same per-arch component build, because both images are built every round
  (`build-custom.sh:208`). It adds a frontend device check, not a build round.

### Changes Required

#### Change 1 — 500-response body (Item 1)

**File**: `src/backend/exceptions/handlers/exception_handlers.py`,
`unhandled_exception_handler` (`:97-107`).

Keep `:102` and `:105` (the `sys.exc_info()` capture and the
`logger.error("Uncaught Exception", exc_info=(...))` call). Drop the now-unused
`exception_name` at `:103`. Replace the return at `:107`:

```python
return JSONResponse(
    {"message": "Internal Server Error.", "request_id": get_request_id()},
    status_code=500,
)
```

Nothing else in the module changes except Change 3b. `http_exception_handler`
(`:90-95`), the request-validation handler's **body**, and the mapped domain
handlers (`:111-162`) are untouched.

- Behavior change / customer impact: the 500 **body** `message` is the fixed
  string plus the existing `request_id`; it no longer carries the exception
  class name or `str(value)`. Status stays 500. The correlated detail is in the
  LocalServer log by `request_id`. No success path and no deliberately-raised
  `HTTPException` body changes. This is a visible 500-body change for any API
  caller and the SPA. The device SPA shows only `data.message`
  (`live-result/RefreshDisplay.tsx:76`, `ManualDisplay.tsx:83`) and never reads
  `request_id` (it appears nowhere in `src/frontend/src`), so after this change
  an operator sees the fixed message with no in-UI correlation handle;
  correlation is by the response JSON's `request_id` or a time-window log
  search (owner memo D2.2 offers folding the id into the fixed text as an
  option).
- Affected images: all backend images (JP5, JP6, JP7, x86 CPU, x86 NVIDIA);
  backend only.

#### Change 2 — input-image inference-results containment (Item 2)

**File**: `src/backend/endpoints/download_file.py`,
`load_input_image_from_worflow_by_capture_id` (`:98-140`), the jsonl fallback.

Add `INFERENCE_RESULTS_DIR` to the `from utils.constants import ...` line
(`:47`). Replace the f-string build + bare open (`:111-121`) with a contained
resolve whose `realpath` calls are inside the `try`, and the strictly-below
prefix guard put directly in the `if` that guards the sink (DD1):

```python
jsonl_file = None
try:
    base = os.path.realpath(INFERENCE_RESULTS_DIR)
    jsonl_file = os.path.realpath(
        os.path.join(INFERENCE_RESULTS_DIR, workflowId, f"{captureId}.jsonl")
    )
except ValueError:  # embedded NUL: cannot name a file inside
    jsonl_file = None
if jsonl_file is None or not jsonl_file.startswith(base + os.sep):
    raise HTTPException(
        status_code=HTTP_404_NOT_FOUND,
        detail="Server cannot find the input image path.",
    )
try:
    with open(jsonl_file, "r") as f:
        json_str = f.read()
        json_dict = json.loads(json_str)
        image_path = json_dict["deviceGroundTruthData"][0]["source-ref"]
except Exception:
    logger.warning("Could not read the input-image jsonl", exc_info=True)
    raise HTTPException(
        status_code=500,
        detail="Server cannot find the input image path.",
    )
```

(`base` is bound before `jsonl_file` can fail the `ValueError`, so the `if`'s
`base + os.sep` is always defined; `INFERENCE_RESULTS_DIR` is an absolute
constant whose `realpath` can't raise. The prefix form rejects
`jsonl_file == base`, so no "strictly below" extra clause is needed.)

The inner 500 keeps its status but its detail is now the fixed string
`"Server cannot find the input image path."` (no exception interpolation), and
the failure is logged at WARNING with `exc_info`. A legitimate slash-free id
pair with no `..` resolves to exactly the former path and is served unchanged.
The database branch (`:122-123`) and the non-Triton branch (`:124-132`) are
untouched.

- Behavior change / customer impact: with no inference-result row, a request
  whose resolved path leaves `INFERENCE_RESULTS_DIR` (for example
  `workflowId=".."`) returns 404 instead of today's 500 — today it is 200 only
  if a matching `.jsonl` happens to exist one level up. No real workflow id is
  `..`, so there is no impact on real workflows.
- Affected images: all backend images; backend only. The jsonl fallback runs on
  every image (`setup_triton()` sets `is_triton=True` at startup).

#### Change 3 — export anchoring + containment (Item 3)

**File**: `src/backend/utils/constants.py` (`:109`). Anchor the pattern, with
ASCII digit classes (`[0-9]`, not `\d`, which is Unicode and admits non-ASCII
digits — harmless for containment but it keeps the validator and a Python
`re.fullmatch` oracle byte-for-byte agreed, since the companion writer emits
ASCII via `strftime`):

```python
CAPTURED_IDS_PATH_PATTERN = r"^/aws_dda/system/capture-id-path-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}$"
```

**File**: `src/backend/endpoints/download_file.py`,
`get_inference_result_data_for_retraining` (`:298-357`). Replace the
`os.path.exists`/`open` block (`:317-324`) with a contained resolve, the
`realpath` inside the `try` and the strictly-below prefix guard in the `if`
(DD1):

```python
if captureIdPath:
    resolved = None
    try:
        base = os.path.realpath(DDA_SYSTEM_FOLDER)
        resolved = os.path.realpath(captureIdPath)
    except ValueError:  # embedded NUL: cannot name a file inside
        resolved = None
    if (resolved is None
            or not resolved.startswith(base + os.sep)
            or not os.path.exists(resolved)):
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail="The server can't get the capture id file. Check the path and try again.",
        )
    with open(resolved) as json_file:
        inference_result_data_list = json.load(json_file)
```

The 404 detail no longer echoes `captureIdPath`. The anchored pattern already
rejects extra parts with a request-validation 400; the containment check is the
filesystem-layer defense behind it and the symlink guard. (`base` is from the
`DDA_SYSTEM_FOLDER` constant and is bound before the line that can raise, so
`base + os.sep` is always defined. `startswith(base + os.sep)` rejects
`resolved == base`, the capture-name symlink to the directory itself.)

- Behavior change / customer impact: `captureIdPath` values with extra path
  parts, `..` segments, a trailing newline, or an appended `token=…` are
  rejected with 400 at validation; a resolved value outside `DDA_SYSTEM_FOLDER`
  returns 404. The UI flow (POST returns a server-generated path under
  `DDA_SYSTEM_FOLDER`, GET consumes it) is unchanged. The one caller affected is
  the result-history download on a station with authorization or local login
  on: today that mis-joined value is accepted and the route answers 401; after
  this change it is rejected with 400. The owner may also fix the join (owner
  memo D5).
- Affected images: all backend images; backend only.

#### Change 3b — mask the validation handler's query log (Item 3b)

**File**: `src/backend/exceptions/handlers/exception_handlers.py`,
`request_validation_exception_handler` (`:76`, `:80`).

Log the query string through the module's existing `_body_for_log`, which
withholds a string that names a sensitive key (DD8). Keep the `QueryParams`
object when nothing is withheld, so a secret-free query's log text stays
byte-identical (`QueryParams('...')`, not a bare string):

```python
query_params = request.query_params  # (:76, unchanged)
...
query_text = str(query_params)
logged_query = _body_for_log(query_text)
detail = {"errors": safe_errors, "body": _body_for_log(exc.body),
          "query_params": query_params if logged_query == query_text else logged_query}
```

The response body (`:85-87`) is unchanged, so the 400 contract and the
existing tests that pin it hold. A query string naming `token` (re-encoded or
raw) is withheld from the log. One that names no sensitive key is logged
byte-for-byte as before (Property 9 preservation half, task 2). Change 3b
leaves the download router's registration (`download_file.py:56`) and
`access_log_router.py` as they are.

- Behavior change / customer impact: none to any response. The validation
  handler's **log** line for a request whose query names a sensitive key
  becomes `[body withheld: it may contain credentials]` for that field.
- Affected images: all backend images; backend only.

#### Change 4 — text-generation failure-reason (Item 4)

**File**: `src/backend/endpoints/text_generation.py`, `_failure_reason`
(`:423-426`). Apply the DD4 helper. No change to the call sites (`:662`, `:713`)
or the 502/SSE shapes. `GenerationError` is already imported (`:57-58`) and
`logger` is module-level (`:63`).

- Behavior change / customer impact: a caller (and the `llm_inference` binding)
  that previously received raw error text for an unexpected failure now
  receives `"internal error; see the LocalServer log"`, and the error is logged
  with a traceback. `GenerationError` reasons (the normal vLLM backend-error
  path) are unchanged, so the Req 5.7 contract holds and the 502/SSE JSON
  shapes are byte-identical. One extra non-`GenerationError` reaches the helper
  on the **streaming** path: a READY→not-READY race raises
  `ModelUnavailableError` inside `generate_stream` (DD4), so the single SSE
  error event's `reason` changes from `"model '…' is not ready (state: …)"` to
  the fixed string. This is accepted as consistent with Item 4 (only a
  `GenerationError` contract reason is surfaced); the pre-stream 409 for an
  already-not-READY model is unchanged.
- Affected images: vLLM-capable images only (JP6, JP7); backend only. No
  runtime effect on JP5-default, generic amd64, arm64 CPU or x86 NVIDIA (router
  not registered).

#### Change 5 — trigger-payload redaction (Item 5, owner decision D1)

This change ships only if the owner chooses a fix option (b1 or b2). It is two
independently-adoptable, independently-revertable layers.

New helpers, standard-library only, in
`src/backend/workflow_engine/trigger_runtime.py` beside
`serialize_trigger_context`. `pipeline_executor` and `api` import them with a
new `from workflow_engine.trigger_runtime import redact_signed_query,
redacted_run_view`. `trigger_runtime`'s module-level imports are stdlib-only
(`heapq`, `json`, `logging`, `os`, `threading`, `time`,
`concurrent.futures`, `dataclasses`, `typing`), so importing it from
`pipeline_executor` and `api` adds no import cycle (confirmed by reading its
import block):

```python
import re

_SIGV4_PARAM_RE = re.compile(
    r"(?i)(X-Amz-(?:Algorithm|Credential|Date|Expires|SignedHeaders|Signature|Security-Token))"
    r"(=)([^&\"'\s]+)"
)
_MASK = "***"

def redact_signed_query(value):
    """Mask the values of SigV4 query parameters in ``value`` (a URL or any
    text embedding one in the literal name=value form), preserving every
    other byte. Total: a non-string is returned unchanged. A fixed point: a
    second pass re-matches and writes the same mask."""
    if not isinstance(value, str) or not value:
        return value
    return _SIGV4_PARAM_RE.sub(lambda m: m.group(1) + m.group(2) + _MASK, value)

def _redact_leaves(value):
    if isinstance(value, str):
        return redact_signed_query(value)
    if isinstance(value, dict):
        return {key: _redact_leaves(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_leaves(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_leaves(item) for item in value)
    return value

def redacted_run_view(tag_values):
    """A copy of ``tag_values`` whose ``trigger`` section has every SigV4
    value masked. Never mutates ``tag_values``: its ``trigger`` entry is the
    run's live Trigger_Context (``pipeline_executor`` seeds it by reference)."""
    if not isinstance(tag_values, dict) or "trigger" not in tag_values:
        return tag_values
    view = dict(tag_values)
    view["trigger"] = _redact_leaves(tag_values["trigger"])
    return view
```

`_redact_leaves` recurses into every string leaf of a dict/list/tuple and
leaves non-string, non-container leaves (ints, `None`) unchanged; it keeps a
tuple a tuple (so a `tags:` repr is unchanged — a list would alter it). One
function serves every Layer A sink, and `redacted_run_view` never mutates its
input (the seeded `tag_values["trigger"]` is the run's live context,
`pipeline_executor.py:2134-2135`). The vendored `stream_url.py` and the shared
`RedactingFilter` are **not** edited.

**Layer A (served/logged sinks — no behavior change; the recommended fix per
D1 (b1)).** Four edits:
- `_persist_run_metadata` (`pipeline_executor.py:3123-3148`): dump
  `redacted_run_view(tag_values or {})` instead of `tag_values or {}` at
  `:3147`. Covers both call sites (`:2355`, `:2370`).
- The `tags:` log (`pipeline_executor.py:2371-2375`): pass
  `redacted_run_view(tag_values)` as the logged argument.
- `get_workflow_execution_metadata` (`workflow_engine/api.py:543-561`): return
  `redacted_run_view(run_artifacts.read_run_metadata(...))`, so runs recorded
  before the upgrade are also served masked.
- `get_workflow_execution_log` (`workflow_engine/api.py:483-497`): wrap the
  text in `redact_signed_query(run_artifacts.read_run_log(...))`, so the
  `tags:` line in an earlier run's `run.log` is served masked.

**Layer B (stored column — the behavior change; D1 (b2) only).**
- `serialize_trigger_context` (`trigger_runtime.py:498-504`): mask the whole
  context by replacing the current `return json.dumps(context or {},
  default=str)` with

  ```python
  return json.dumps(
      _redact_leaves(context or {}),
      default=lambda v: redact_signed_query(str(v)),
  )
  ```

  This masks **every string leaf** of the stored context — the MQTT `payload`
  string and, for other trigger shapes, any string leaf such as an OPC UA
  string `value` (`trigger_runtime.py:2848-2856`) — so it masks the same values
  Layer A masks, not only the `payload`. It never parses or re-serializes the
  `payload` (there is no parsed `payload` at store time: the stored context's
  `payload` is always a string, written by the MQTT subscribers at
  `trigger_runtime.py:1845-1852` / `:2147-2154`, and `payload_json` is added
  only at load, `pipeline_executor.py:530-561`), so every non-signed byte of
  `payload` is preserved. The redacting `default` keeps the existing
  non-JSON-native degradation (an OPC UA variant still stringifies) while
  masking a signed value inside that stringified form. This is the single
  writer path (used at `:545` and `continuous_runner.py:168`), so one edit
  covers both writers. Because the run reloads this column
  (`pipeline_executor.py:1559-1561`), `load_trigger_context` re-derives
  `payload_json` from the already-masked `payload` text with the same keys, and
  every SigV4 value the run reads back from the payload is masked (DD6). A
  presigned-URL fetch (`output_bindings.py:2313-2321`) fails, and a signed
  value surfaced through a metadata binding or a `{trigger.…}` placeholder
  reads `***`. This is the "download paths stop working" change. (Confirmed
  offline against the tree's own `load_trigger_context`, dummy values only: a
  no-SigV4 context serializes byte-identically to the current writer;
  `load_trigger_context(stored)["payload"] == redact_signed_query(original)`;
  `payload_json` re-derives masked with the same keys; an OPC UA string `value`
  is masked; a tuple leaf stays a tuple and the input is not mutated.)

- Behavior change / customer impact: Layer A changes only served/logged content
  (Req 3.4 preserved; what a run reads back is identical). Layer B also changes
  what the run reads back. Every SigV4 value a run takes from its trigger
  payload is masked:
  - a presigned-URL fetch receives the masked URL and fails;
  - a signed value surfaced through a metadata binding or a `{trigger.…}`
    placeholder reads `***`.

  Non-signed fields, and the metadata bindings and placeholders over them, are
  unchanged under both layers, because only SigV4 values are masked. The
  stored-column mask does not change the continuous-run prefilter
  `'%"source": "continuous"%'` (`run_retention.py:93`): `source` is not a SigV4
  value.
- Affected images: all backend images; backend only.

### Not changed

- `http_exception_handler`, the request-validation handler's **body** and the
  mapped domain handlers (`exception_handlers.py:90-162`), beyond Change 1's
  `:103`/`:107` and Change 3b's log line.
- The input-image route's final "image not found" 404 (`download_file.py:137-140`).
  It names the two requested path segments and carries no error text or
  filesystem path. It's the route's existing message for every missing image.
- The download router's registration (`download_file.py:56`) and the
  access-logging route class (`endpoints/route/access_log_router.py`). Item
  3b relies on the first staying as it is (Property 9 guard).
- The input-image database branch and non-Triton branch; the snapshot-file
  route; the output/overlay/node image routes (they resolve paths only from the
  execution record via `run_artifacts`); the two frontend image-source cases;
  the documentation samples; the test assertion (bugfix "Items reviewed and
  left unchanged"; the optional one-line hardenings are not applied).
- `src/backend/workflow_engine/vendor/workflow_core/stream_url.py` and the
  shared `RedactingFilter` (Item 5 uses a scoped helper, DD5).
- The vLLM runtime companion server (`vllm_runtime/server.py`), loopback only.
- `.kiro/specs/security-scan-remediation-high/requirements.md` (pinned).

## Error Handling

- **Change 1 (500 handler).** The only failure mode is the handler raising
  while building the response. It is a pure dict build + `JSONResponse`;
  `get_request_id()` returns `correlation_id.get()`, which yields `None`
  safely. No new failure mode. The caught exception is already logged with
  `exc_info` (fatal to the request, recoverable for the server). Not re-raised.
- **Changes 2 and 3 (containment).** `os.path.realpath` can raise `ValueError`
  for an embedded NUL. It is caught (realpath inside the `try`), the candidate
  is set to `None`, and the `if` treats `None` or a path that fails
  `startswith(base + os.sep)` as "not contained" → the route's existing 404;
  nothing is opened; the caller gets 404. `base` is resolved from an absolute
  constant before the line that can raise, so it is always bound where the `if`
  reads it. A read/parse failure after a contained path: Item 2 returns 500
  with the fixed detail and logs at WARNING with `exc_info`; Item 3's
  `json.load` of a contained non-JSON file surfaces through the global 500
  handler, now with Change 1's fixed body. No new log level beyond Item 2's
  WARNING line.
- **Change 3b (validation log).** `str(QueryParams)` and `_body_for_log` over
  a `str` are total, so there's no new failure mode. The log stays at ERROR as
  today, and the response is unchanged.
- **Change 4 (`_failure_reason`).** Total: `isinstance` and `.reason` on a
  confirmed `GenerationError` can't raise; the else branch logs at ERROR with
  `exc_info` and returns the fixed string (recoverable per request — the
  502/SSE is still returned). No exception escapes the helper.
- **Change 5 (`redact_signed_query` / `redacted_run_view` / `_redact_leaves`).**
  Total: a non-string or no-match returns unchanged; the regex substitution
  can't raise on a `str`; `_redact_leaves` only recurses into dict/list/tuple
  and returns any other leaf unchanged. The Layer A edits sit inside
  `_persist_run_metadata`'s existing best-effort containment (a failure there is
  already logged and swallowed and never changes run status) and the two serve
  routes' best-effort reads. Layer B keeps `serialize_trigger_context`'s
  existing non-JSON-native-degrades-to-string behavior, with the stringified
  form additionally passed through `redact_signed_query` by the `default`
  callable, so the degrade path masks rather than leaks a signed value.

## Validation Rules

- **`workflowId`, `captureId` (Item 2).** Required path segments; type `str`;
  starlette `[^/]+` (no slash) is enforced by the router. New rule: with no
  inference-result row, the resolved jsonl path must be strictly below
  `realpath(INFERENCE_RESULTS_DIR)`. On failure (containment or an embedded
  NUL): 404, nothing opened.
- **`captureIdPath` (Item 3).** Optional query parameter; type `str`; must
  match the anchored `CAPTURED_IDS_PATH_PATTERN` start-to-end. On pattern
  failure: the app's request-validation **400** (`exception_handlers.py:87`),
  body `{message, request_id}`, unchanged. The handler's log line withholds a
  query that names a sensitive key (Change 3b). New rule: the
  resolved path must be strictly below `realpath(DDA_SYSTEM_FOLDER)` and must
  exist. On failure: 404, nothing opened.
- **`error` (Item 4).** Any `BaseException`; no external input. Classified by
  type; only a `GenerationError`'s truthy `.reason` is returned, else the fixed
  string.
- **trigger `payload` (Item 5).** Any string, decoded `errors="replace"` from
  the message bytes. `redact_signed_query` accepts any value and is total; no
  size or type limit is added (payload size is bounded upstream by the
  broker/IPC path, unchanged).

## Invariant Ownership

- **"A served or opened path stays within its base directory"** — owned at the
  route handler (Items 2, 3), where request input becomes a filesystem path and
  the existing 404 lives; the constants layer owns Item 3's anchored shape so
  the validator rejects early. Chosen there, not in a shared util, because each
  route has one base directory and the check is three lines of the repo's
  idiom.
- **"An uncaught-error response and the two changed download routes carry no
  internal error text"** — owned at the uncaught-exception handler (Item 1) and
  the two download routes' error details (Items 2, 3). Scoped to those; the
  roughly fifteen other `HTTPException(detail=f"…{e}…")` sites are out of scope
  (bugfix Out of Scope).
- **"The failure reason returned to a caller is the backend contract reason or
  a fixed string"** — owned by `_failure_reason` (Item 4), the one helper both
  text-generation sinks call.
- **"A credential in a rejected request's query is not logged"** — owned at the
  request-validation handler (Item 3b), the one place that logs `query_params`
  for the download routes. Its precondition is that the download router
  stays off the access-logging route class. A structural guard test owns that
  (Property 9), because it's a registration choice in `download_file.py`, not
  logic the handler can enforce.
- **"Signed query material is not served, persisted or logged"** — owned, if
  adopted, at the serve/log boundaries (Layer A: `_persist_run_metadata`, the
  `/metadata` and `/log` routes, the `tags:` log) and, under (b2), the persist
  boundary (Layer B: `serialize_trigger_context`). Owned at the sinks because
  the run deliberately keeps reading the raw context unless Layer B is adopted.

## Edge Cases

- Encoded-slash `..` segment in a path position (Item 2): uvicorn decodes
  `%2F` before routing, so the request 404s at the router; the containment
  check is the second layer for the single-`..` case.
- Embedded NUL in `captureId`/`captureIdPath` (Items 2, 3): on the device
  interpreters (3.10 on JP6, 3.11 elsewhere) `os.path.realpath` raises
  `ValueError` inside the `try`, so the path counts as not contained → 404.
  The host interpreter (3.9.5) doesn't raise, which is why the NUL cases run
  in the device container and are skipped on interpreters older than 3.10.
- A rejected export query carrying a mis-joined credential (Item 3b): the
  re-encoded and the raw forms are both withheld from the handler's log by
  `_body_for_log`'s substring match. The 400 body is already masked today
  (`safe_errors` withholds the error `input`, `:78`, and the message is
  rebuilt from them, `:85-86`).
- Symlink inside `DDA_SYSTEM_FOLDER`/`INFERENCE_RESULTS_DIR` pointing outward
  (Items 2, 3): `realpath` resolves the link, so the `startswith(base + os.sep)`
  prefix check sees the real location and rejects it. A capture-name symlink to
  the base directory itself resolves to `base`, which the strictly-below prefix
  check also rejects (`resolved == base` is not below `base + os.sep`). This is
  the only way Item 3's containment branch is reachable after anchoring (any
  non-symlink `..`-segment value is already a 400).
- `GenerationError` with an empty/`None` `.reason` (Item 4): falls to the fixed
  string, logged — intended.
- Trigger payload that is a JSON object embedding a signed URL in one field
  (Item 5): `_redact_leaves` masks the SigV4 values inside that string leaf and
  leaves the surrounding JSON and other fields intact, so a non-signed metadata
  binding still resolves.
- Trigger payload that is a bare signed URL (Item 5): fully covered; no JSON to
  preserve.
- Tuple leaf in a trigger section (Item 5): `_redact_leaves` returns a tuple for
  a tuple, so a `tags:` value that is a tuple keeps its type and repr; only its
  string leaves are masked.
- Non-JSON-native leaf under Layer B (Item 5, b2): a value `_redact_leaves`
  passes through unchanged (e.g. an OPC UA variant object) is stringified by
  the redacting `default=lambda v: redact_signed_query(str(v))`, so a signed
  value inside its string form is still masked rather than stored raw.
- Fixed point (Item 5): a payload already carrying `...=***` is re-matched and
  the same `***` is written, so a second pass is identical.

## Testing Strategy

### Validation approach

Full route behavior on a built image is OWNER-gated on device. In-repo, Items
1-4 are deterministic function edits and Item 5's helpers are pure. The
`download_file.py` routes import device-only modules, so host tests lift the
route function with `ast` and compile it against the real `utils.auth`,
`run_artifacts` and constants — the `test_overlay_image_serving.py` pattern, so
the function under test is byte-for-byte the shipped code. Where a test needs
the device interpreter's `realpath` NUL semantics, a writable `/aws_dda`, or
`gi`/vLLM, it runs in a throwaway container of the local backend image, by
image ID (never the `flask-app:latest` name after a local load — a build or a
`docker load` moves that tag).

Container image for those runs. The authoritative image is the local backend
image whose dependencies match this tree's pins (fastapi 0.109.2 / pydantic
2.6.4). The code under test comes from the mounted worktree. On this
workstation today that's the `flask-app:latest` image (Python 3.11.9). Record
its image ID and `python3.11 --version` in `verification-notes.md` at task
time. The local backend image has **no** `pytest` or `hypothesis` installed
(confirmed offline), so the container run installs them first. Two forms, both
by image ID (never the `flask-app:latest` name after a local load):

- with network for the install (the steering's shape):
  `docker run --rm --pull never -v "$(pwd)":/w/repo:ro -w /w/repo
  -e PYTHONDONTWRITEBYTECODE=1
  -e PYTHONPATH=/w/repo/src/backend:/w/repo/test/backend-test:/w/repo/test/backend-test/workflow_engine
  --entrypoint bash "$IMG" -lc 'PY=$(command -v python3.11 || command -v
  python3.10); $PY -m pip install --no-cache-dir --quiet pytest hypothesis;
  $PY -m pytest <files> -q -p no:cacheprovider --noconftest'`;
- or `--network none` with a mounted wheelhouse of the same two packages.

Record the installed `pytest` and `hypothesis` versions in
`verification-notes.md`. Mount the tree at `/w/repo` (not `/repo`): the repo
root has a tracked `__init__.py`, so a `/repo` mount would put `/` — where the
image bakes its own backend — first on `sys.path` and shadow the tree's
modules.

No local image carries this tree's pinned backend stack on 3.10. The JP6 3.10
NUL semantics are covered two ways: a stdlib-only probe, in which
`os.path.realpath` raised `ValueError` on an embedded NUL under a local
3.10.12, and the Orin device check. If a local JP6 backend image (Python
3.10) with the same pins is available at task time, run the two containment
modules there too and record it. The explicit `a\x00b` NUL member carries
`skipif(sys.version_info < (3, 10))`; the Property 3/4 generators exclude
`\x00` (DD1, Property 3), so a host run (3.9.5, where `realpath` does not raise
on a NUL) skips only the explicit member and never generates a NUL id that
would read as a false failure. The container run (3.11), which doesn't skip, is
the one recorded for the NUL semantics.

### Exploratory bug-condition checking (task 1: FAILS on the unfixed tree, PASSES unchanged after the fix)

Every case below asserts the **fixed** behavior (`not isBugCondition`). Its
failure on the unfixed tree is the recorded counterexample. No case asserts
the bug-present condition, and `¬C` inputs are in task 2.

- **Item 1 (Property 1):** add a `TestUncaughtExceptionBody` class to
  `test/backend-test/stream_ingest/test_log_redaction.py`, which already
  imports `exception_handlers` and builds a `Request`. Raise
  `KeyError("source-ref")`, then call
  `asyncio.run(unhandled_exception_handler(request, exc))` inside the
  `except`. Assert `body["message"] == "Internal Server Error."` and
  `"request_id" in body`. (FAILS unfixed; PASSES after the fix.) Host.
- **Item 2 (Property 3):** lift `load_input_image_from_worflow_by_capture_id`
  with `ast`. In its namespace, stub `validate_token_in_query_param`, make
  `get_is_triton` return `True`, and make
  `inference_result_accessor.get_inference_result` return `None`. Over a temp
  tree, use the recording `open` shim (below) and seed a valid
  `<captureId>.jsonl` one level above the patched `INFERENCE_RESULTS_DIR`.
  Its `source-ref` names an existing temp image.
  - Call with `workflowId=".."`. Assert the call raises `HTTPException` 404 and
    that the shim recorded no `open` call. (FAILS unfixed: it opens the
    one-level-up jsonl and serves it. PASSES after the fix.)
  - For a contained id whose jsonl is malformed, assert 500 with detail
    exactly `"Server cannot find the input image path."`. (FAILS unfixed: the
    detail carries the exception text and path. PASSES after the fix.)

  Device container (the module also holds the NUL cases).
- **Item 3 (Property 4):** use the lifted export function over the seeded
  tree. The tree has a `capture-id-path-<ts>/` directory and a `target.json`
  list outside it, with `DDA_SYSTEM_FOLDER` patched to `<tmp>/aws_dda/system`.
  - (validator) Through the mounted route, which registers the real
    `request_validation_exception_handler`, `GET` with
    `captureIdPath=/aws_dda/system/capture-id-path-2025-01-01-00-00-00/../../../target.json`.
    Assert 400, with the handler body never entered: the stubbed
    `validate_token_in_query_param` records no call. (FAILS unfixed: the
    unanchored pattern accepts. PASSES after the anchor.)
  - (containment) Call the function directly, bypassing the validator, with
    `<tmp>/aws_dda/system/capture-id-path-2025-01-01-00-00-00/../../../target.json`.
    Assert `HTTPException` 404 and no recorded `open` of the target. (FAILS
    unfixed: it opens. PASSES after the fix.)
  - (output encoding) For a missing accepted value, assert the 404 detail is
    exactly `"The server can't get the capture id file. Check the path and try again."`.
    (FAILS unfixed: the detail echoes the value. PASSES after the fix.)

  Device container.
- **Item 3b (Property 9, fix half):** build a `Request` whose raw query string
  is
  `captureIdPath=/aws_dda/system/capture-id-path-2025-01-01-00-00-00token=DUMMYTOKEN`,
  plus a `RequestValidationError` for that value. Call the real
  `request_validation_exception_handler`. Assert that `"DUMMYTOKEN"` is in no
  `caplog` record and not in the 400 body. (FAILS unfixed, on the log
  assertion; the offline probe reproduced the dummy value in the handler's
  record. PASSES after Change 3b.) Host, in the Item 1 module.
- **Item 4 (Property 5):** in `test/backend-test/text_generation/test_failure_reason.py`,
  assert `_failure_reason(ValueError("boom")) == "internal error; see the LocalServer log"`.
  Make the same assertion for a non-`GenerationError` exception carrying a
  `.reason` attribute. (FAILS unfixed: it returns `"boom"` or that attribute.
  PASSES after the fix.) Host.
- **Item 5 (`not isBugCondition_A`, sink level):** in
  `test/backend-test/workflow_engine/test_trigger_payload_redaction.py`, drive
  the real sink, not a hand-built record. The `tags:` line is emitted inline in
  `WorkflowExecutor.execute` (`pipeline_executor.py:2371-2375`), not in
  `_persist_run_metadata`, so use the executor harness in
  `test/backend-test/workflow_engine/test_trigger_metadata_persistence.py`:
  seed a `WorkflowExecution` row whose `trigger_context_json` embeds the dummy
  presigned-URL payload, run `WorkflowExecutor.execute` with the fake pipeline
  manager and a temp capture root, capture the
  `"Workflow execution %s completed; tags: %s"` record with `caplog`, format it
  through `RedactingFilter`, and read the persisted `{output_dir}/{capture_id}.json`.
  Assert that none of the dummy credential, date, signature or token **values**
  appears in the persisted JSON or the formatted line. (FAILS unfixed; PASSES
  after Layer A.) Under D1 (a), mark it `xfail(strict=True)` to record the
  accepted behavior. Host. (This drives Layer A's two production call sites —
  `_persist_run_metadata` and the inline `tags:` log — at the real call site,
  which a hand-built `tags:` record can't; the helper-level masking, fixed point
  and non-mutation are Properties 7/8 in task 4.1.)

Record each counterexample in `verification-notes.md` "Task 1": the failing
assertion, the observed unfixed value, and the opened path. Also record the
`re.search`/`re.fullmatch` observation on the unfixed constant.

### Fix checking (task 5)

The task-1 tests, unchanged, now PASS. The property variants (Properties 1, 3,
4, 5 and 9) run here (authored in task 1), plus the helper-level Properties 7
and 8 and the serve-time masking cases authored in task 4.1/4.2. Under (b2),
the Layer B case added in task 4.3 also runs.

### Preservation checking (task 2: PASSES on the unfixed tree, frozen, still PASSES after the fix)

These are the `¬C` cases, observed on the unfixed tree and frozen.
- Item 1 (Property 2): byte-identical `http_exception_handler` and
  request-validation **response bodies** over a generated exception set. The
  uncaught handler's `caplog` record carries `exc_info` naming the raised
  exception (AC2; kept behavior).
- Item 2 (P-routes): a legitimate slash-free id pair with no `..` serves the
  seeded jsonl's image through the `open` shim (200). With an
  inference-result row, the route serves the row's image.
- Item 3 (P-routes): the exact writer-shape `captureIdPath` passes the mounted
  route's validator. A seeded capture-id-path file under the temp tree yields
  the zip, with the same members and status.
- Item 3b (Property 9, preservation half and guard): for generated query
  strings naming no sensitive key, the handler's log record text equals the
  unfixed expression's text. The oracle is
  `str({"errors": safe_errors, "body": _body_for_log(exc.body), "query_params": request.query_params})`,
  checked on the unfixed tree first. One literal example is also frozen.
  Structural guard: an `ast` read of `src/backend/endpoints/download_file.py`
  finds `unauthenticated_router = APIRouter()` with no `route_class` keyword.
- Item 4 (Property 6): `_failure_reason(GenerationError("m", "oom")) == "oom"`.
  The 502/SSE shapes for a `GenerationError` with `.reason` are
  `{model_name, reason}` and `{"error": {"reason": ...}}` exactly.
- Item 5 (Property 8, observation-first): on the unfixed tree, capture the
  bytes of the metadata JSON and of the formatted `tags:` line for a context
  with no SigV4 value, non-signed query params included. Assert byte-identity
  after the fix. The helper-level Property 8 is checked at the post-fix
  checkpoint, because the helper doesn't exist on the unfixed tree.

### Unit and property tests (added, repo styles)

- Item 1: the `TestUncaughtExceptionBody` class in
  `test/backend-test/stream_ingest/test_log_redaction.py`. Property 1 draws
  exceptions from
  `st.sampled_from([KeyError, ValueError, RuntimeError, OSError, TypeError, AttributeError])`
  × `st.text()`, each raised and handled inside `except`; asserts
  `body["message"] == "Internal Server Error."` and `"request_id" in body`.
  Property 2's `exc_info` record assertion sits beside it. Runs on the host.
- Item 2: a lifted-route module,
  `test/backend-test/workflow_engine/test_input_image_containment.py`. It uses
  a recording `open` shim that maps the literal `/aws_dda/inference-results/`
  prefix to a temp root, with `INFERENCE_RESULTS_DIR` patched to that root.
  So one frozen test serves the seed before and after the fix. Property 3
  draws ids as above (generator excludes `\x00`). The explicit `a\x00b` NUL
  member carries `skipif(sys.version_info < (3, 10))`. Authoritative run: the
  device container, which doesn't skip.
- Item 3: a lifted-route module
  `test/backend-test/workflow_engine/test_export_path_containment.py` following
  `test_overlay_image_serving.py`, mounted on a minimal FastAPI app that
  registers the real `request_validation_exception_handler`, so the 400, 404
  and 200 outcomes are pinned. Property 4's accept/reject half asserts through
  the mounted route, because the engine's anchoring governs, not Python `re`.
  The containment half's explicit NUL member is skipped below 3.10, and the
  authoritative run is the device container. This module also holds the AC5
  **symlink** case: make `<tmp>/aws_dda/system/capture-id-path-<ts>` a symlink
  to a `target.json` outside `<tmp>/aws_dda/system`, call the function directly,
  and assert 404 with no `open` of the target — the only way Item 3's
  containment branch is reachable after anchoring (any non-symlink `..`-segment
  value is a 400). Any additions to the full-app
  `api-endpoints/test_inference_result_api.py` run in the device container only.
- Item 3b: a `TestRejectedQueryLog` class in the Item 1 module (same handler,
  same file). It holds:
  - the fix case, where the dummy credential is in neither the 400 body nor
    any log record;
  - the Property 9 property over generated secret-free query strings, checked
    against the unfixed-expression oracle, plus the frozen literal example;
  - the structural guard, which reads
    `src/backend/endpoints/download_file.py` with `ast` so the device-only
    module is never imported on the host.

  Runs on the host.
- Item 4: `test/backend-test/text_generation/test_failure_reason.py`
  (direct import). Property 5 as above; a unit case pins the 502 and SSE bodies
  for a `GenerationError` via a minimal app + `TestClient`, as in
  `test_generation_metrics_field.py`.
- Item 5: `test/backend-test/workflow_engine/test_trigger_payload_redaction.py`.
  Authored across tasks 1, 4.1 and 4.2:
  - **Property 7 (task 4.1, helper level):** over strings embedding SigV4
    params in random order and casing, `redact_signed_query` masks every SigV4
    value with `***`, preserves every other byte, is a fixed point (second pass
    writes the same mask), and returns a non-string unchanged — this covers
    AC11 (total, fixed point over the literal `name=value` form).
  - **Property 8 and non-mutation (task 4.1):** a no-SigV4 context is returned
    byte-identically by `redacted_run_view` and `redact_signed_query`, and
    `redacted_run_view` does not mutate its input (its `trigger` is the run's
    live context, seeded by reference).
  - **Layer A sink level (task 1):** the executor-harness exploration case
    above (`not isBugCondition_A`), driving `WorkflowExecutor.execute`.
  - **Serve-time masking of a pre-upgrade run (task 4.2, AC10 bullet 3):** seed
    a run directory whose metadata JSON and `run.log` carry unmasked dummy
    values (as a run recorded before the upgrade would), then call
    `get_workflow_execution_metadata` and `get_workflow_execution_log` from
    `workflow_engine.api` mounted the way `test_overlay_image_serving.py` mounts
    routes, and assert every SigV4 value reads `***` with every other byte
    unchanged.
  - **Layer B (task 4.3, b2 only):** `serialize_trigger_context` masks the
    values; `load_trigger_context(stored)["payload"] ==
    redact_signed_query(original_payload)`; `payload_json` re-derives with the
    same keys; and a SigV4 value in a non-`payload` string leaf reads `***`.
  Runs on the host (the helpers and `trigger_runtime` import cleanly;
  `_persist_run_metadata` via a `WorkflowExecutor` is exercised with
  `COMPONENT_WORK_PATH` set by `workflow_engine_test_utils`). Dummy SigV4 values
  use no `AKIA`/`ASIA` access-key prefix and `.invalid` hosts, so the task-6
  hygiene grep and push-time secret scanning stay quiet.

Per-file run location is recorded in tasks.md and `verification-notes.md`
(host with/without conftest, or device container).

### What is unit- vs integration-tested

Unit/property (host or device container): all function-level fixes, the
pattern/containment predicates, the redaction helpers, and the two serve
routes' masking via lifted functions. Integration (OWNER-gated, on device): the
full route round-trips, the vLLM 502/SSE on JP6/JP7, and the trigger-payload
persist/serve/log on a real run. The design is testable at the function level
because each fix is local to one handler/helper; nothing needs a full pipeline
to validate the input-handling decision. A design note: the full-app
`api-endpoints/*` suites don't collect on the development host (they need the
`panorama`/GLIBC stack), so the host-provable outcomes live in the lifted
modules, and the full-app additions run in the device container.

## Affected Images / Components

| Item | JP5 (MIC-730) | JP6 (Orin) | JP7 (thor1) | x86 CPU (the lab Dell) | x86 NVIDIA (the lab Dell) | generic arm64 (no device) | Layer |
|---|---|---|---|---|---|---|---|
| 1 (500 body) | yes | yes | yes | yes | yes | yes | backend |
| 2 (input-image) | yes | yes | yes | yes | yes | yes | backend |
| 3 (export) | yes | yes | yes | yes | yes | yes | backend |
| 3b (validation log) | yes | yes | yes | yes | yes | yes | backend |
| 4 (failure-reason) | no* | yes | yes | no | no | no | backend |
| 5 (trigger payload) | yes | yes | yes | yes | yes | yes | backend |

\* JP5 defaults to `VLLM_ENABLE=0`, so the text-generation router is not
registered and Item 4 has no runtime effect there (a flag-enabled JP5 vLLM
build would include it). The generic arm64 CPU image installs no vLLM wheel. No
frontend (react-webapp) change is required by any item; the two frontend
image-source cases are dismissed (bugfix), and the optional export-link join
(owner memo D5) would ride the same component builds (both images are built every round,
`build-custom.sh:208`), adding a frontend device check, not a build round.

## Build Rounds and Sequencing

All items are backend device code, so they share one LocalServer build per
architecture. The recommendation is to batch this round with the pending jpeg
round, so a single build per arch produces an image carrying both fixes and one
on-device pass per device verifies both.

Current state: `spec/jpeg-folder-source-decode` is pushed at `3a30a1d3`, based
on `48841a0`, waiting for device builds of `amd64`, `amd64Nvidia` and one
`arm64JP7` verification build (its JP5/JP6/JP7 need no release rebuild for that
bug — jpeg design D3). The trunk (`integration/all-specs` = `remediation` =
`main`'s tree) has advanced to `f191d580`; this hardening worktree sits on it.

The trunk delta `48841a0..f191d580` is mostly portal and CI, but not only:
besides `edge-cv-portal/**` and `.github/**` it also changes
`test/backend-test/security/baselines/iam_out_of_scope_baseline.json`,
`iam_post_fix_approved_additions.json`,
`test/backend-test/security/preservation/test_preservation_model_converter.py`,
`README.md` and four `.kiro/specs/portal-usecase-bucket-config/*` files. None of
those is a backend device file either spec edits, so the two feature branches do
not conflict on device code — but the security IAM baselines matter to the build
gate after the merge-up, so the combined ref must carry them.

Both feature branches edit `src/backend/workflow_engine/pipeline_executor.py`:
jpeg adds 93 lines in four hunks (near the imports, near `:318`, a
`_constrain_jpegdec_output` method near `:1167`, and near `:1997`); this spec's
Layer A edits `_persist_run_metadata` and the `tags:` log in
`WorkflowExecutor.execute`. The hunks don't overlap (a read-only `git
merge-tree` of the two branches onto their merge base reports no conflict
markers), but every `pipeline_executor` line this spec cites will shift on the
combined ref, so tasks cite that file by **function name**, not line number.
Both branches also rebaseline the one CSI golden entry for that file to their
own hash (trunk `7b80c297…`, jpeg `103bc023…`); the combined ref's correct value
is neither — it is `sha256sum` of the merged file (task 7). Two more merge
mechanics follow from the two branches both touching `pipeline_executor.py`:

- **The CSI golden conflict is resolved by recompute, never by taking a side.**
  The golden `backend_csi_consumers_sha256.json` rewrites three entries on the
  jpeg branch (`pipeline_builder.py`, `pipeline_executor.py`,
  `catalog/nodes.py`) and one on this branch (`pipeline_executor.py`), so the
  merge conflict hunk spans the adjacent `nodes.py` line too. Resolve it by
  recomputing **every** pinned entry from the merged tree
  (`sha256sum` each path), not by taking either side — taking this spec's side
  would drop jpeg's `nodes.py` value and vice-versa.
- **Layer A's new import is placed so it can't collide with jpeg's.** Layer A
  (and `api.py`) add `from workflow_engine.trigger_runtime import
  redact_signed_query, redacted_run_view`. jpeg inserts its own import block in
  `pipeline_executor.py` right after `from workflow_engine.run_log import
  RunLogCapture` (`:129`). To keep the two import edits non-overlapping, this
  spec adds its `trigger_runtime` import to the **existing**
  `from workflow_engine import …` grouping near `:88` (or imports it lazily
  inside the two functions); it is not inserted at `:129`. The "hunks don't
  overlap" claim covers the function-body hunks and this import placement.

Recommended sequencing (strictly one variant at a time per server, per
steering; the owner triggers builds from the Portal, which builds any
`source_ref`):

1. Bring `spec/jpeg-folder-source-decode` up to the trunk tip (merge trunk in;
   never rebase/force), per that spec's task 6, and re-run its local suites.
2. Land this spec's fix commits on `spec/localserver-input-handling`.
3. Create a named combined ref — `build/jpeg-and-input-handling` — from the
   trunk tip and merge both feature branches into it (merge commits; no rebase,
   no force). Recompute every pinned CSI golden entry from the merged tree and
   re-run the CSI preservation suite plus both specs' suites. Record the ref and
   the recomputed hashes in both specs' `verification-notes.md`.
4. Build each architecture **once** from `build/jpeg-and-input-handling`,
   strictly one variant at a time: `amd64`, `amd64Nvidia`, `arm64` (generic
   CPU), `arm64JP5`, `arm64JP6`, `arm64JP7`. These are jpeg's task-7 builds too:
   jpeg's `arm64JP7` verification build rides along, and jpeg's offline
   built-image check (its task 7) runs on the `amd64`, `amd64Nvidia`, `arm64`
   and `arm64JP7` builds. The hardening fixes add the JP5/JP6 builds and share
   amd64/amd64Nvidia/JP7. Record the build-source change (owner memo D3) in
   jpeg's `verification-notes.md` "Task 6".
5. Verify both fixes in one on-device pass per device (jpeg's checks plus this
   spec's).
6. **Trunk merge, gated on both specs' hardware gates (task 10).** Merge
   `build/jpeg-and-input-handling` to the trunk only after BOTH hold:
   (i) this spec's task 8 has passed (MIC-730, Orin, thor1; amd64/amd64Nvidia a
   device pass or an owner-recorded built-only disposition), AND (ii) the jpeg
   spec's own trunk-merge precondition — its task 8 (lab Dell, amd64) and task
   8b (thor1) have passed. Reason: the combined ref carries jpeg's Ubuntu-22.04
   (x86/arm64-CPU) device fix, whose primary hardware check is jpeg task 8 on
   the lab Dell; merging on this spec's task-8 alone (which accepts a Dell
   built-only disposition) would land that fix on the trunk unverified, against
   jpeg task 11 and the steering's rule that on-device code reaches the trunk
   only after real-hardware verification.
   - **If (ii) can't be met** (for example the lab Dell is still down for its
     GPU upgrade): do **not** merge the combined ref. The owner picks either
     (a) wait, or (b) merge `spec/localserver-input-handling` **alone** to the
     trunk — this spec's fixes were verified on MIC-730/Orin/thor1 in the
     combined images, with amd64/amd64Nvidia left owner-recorded built-only —
     and leave jpeg to its own task 11. Record that the trunk tree then differs
     from the built (combined) tree by jpeg's files.
   - **When both hold**, this one merge also performs jpeg's task 11: record it
     in jpeg's `verification-notes.md`; carry jpeg task 11's PR note (any
     ComputeStack deploy from the trunk now publishes C1 with C1b); use jpeg's
     trunk-merge pattern (merge into `remediation`, then fast-forward
     `integration/all-specs`), then also merge to `main` (the owner's
     instruction for this spec); and fast-forward `remediation` to
     `integration/all-specs` — or record that it is left behind. See task 10 for
     the exact precondition and the failure path.

Keeping the branches separate would need a second build round per architecture.
A change to the D1 answer after task 7 starts would need another build round
too, which the owner must accept explicitly (owner decision D1 is gated before
the build in task 9 for that reason).

Caveat on "exactly the built tree": step 6 lands exactly the built (combined)
tree only if the trunk did not move between task 7 and task 10. If it did, the
trunk receives the built tree **plus** the trunk's interim commits (merge them
into the combined ref first; re-run the CSI recompute if any pinned file moved).

### On-device verification per architecture (OWNER-gated, per build steering)

For every affected architecture: build the component, deploy (hot-patch for
iteration, but "done" requires a built-and-deployed component), exercise the
items end-to-end, then soak at least 30 minutes with at least 10 workflow runs
and at least 10 requests per exercised route, confirming no crash, container
restart or crash-loop. Device mapping:

- **arm64JP5 → MIC-730**: Items 1, 2, 3, 3b, 5, plus the jpeg folder-source
  non-regression check. (Item 4 n/a unless a vLLM-enabled JP5 build.)
- **arm64JP6 → Orin**: Items 1, 2, 3, 3b, 4, 5, plus the jpeg folder-source
  non-regression check (and confirm the PNG staging path still runs).
- **arm64JP7 → thor1**: Items 1, 2, 3, 3b, 4, 5 (shares the jpeg spec's task 8b
  thor1 verification build).
- **amd64 → the lab Dell**: Items 1, 2, 3, 3b, 5. **GATED** on the owner
  confirming the lab Dell is up (offline for a GPU upgrade).
- **amd64Nvidia → the lab Dell (nvidia profile)**: Items 1, 2, 3, 3b, 5.
  **GATED** on the lab Dell being up and switched; otherwise recorded
  built-only. (Item 4 n/a: no vLLM wheel.)
- **arm64 (generic CPU) → no lab device**: built-only; record the offline image
  check. (Item 4 n/a: no vLLM wheel.)

Per-item device check content (dummy values only; record only masked output
for Item 5). Each check names a deterministic inducer so it can't pass
vacuously:
- Item 1: use the deterministic inducer — write a non-JSON scratch file
  `/aws_dda/system/capture-id-path-2000-01-01-00-00-00` (exactly the writer
  shape, so it passes the anchored pattern and the containment check), then
  `GET /workflows/<wf>/results/export?captureIdPath=/aws_dda/system/capture-id-path-2000-01-01-00-00-00`
  (add `&token=<valid>` on an auth-enabled station). The route's `json.load`
  raises into the global handler. Expect 500 with body **exactly**
  `{"message": "Internal Server Error.", "request_id": <id>}`, and an
  application-log traceback line carrying that same `request_id`. Delete the
  scratch file. Backend healthy.
- Item 2: seed a scratch
  `/aws_dda/inference-results/<scratch-wf>/<scratch-cid>.jsonl` whose
  `source-ref` is an existing captured image; with no database row for that
  capture, request the input-image route and expect 200. Then send the
  `..`-segment request **with the segment percent-encoded so a client doesn't
  collapse it** — `GET /workflows/%2E%2E/capture-details/<scratch-cid>/input-image`
  (or `curl --path-as-is` with a literal `/../`) — and expect 404 with body
  `message` **exactly** `"Server cannot find the input image path."`. That body
  (not the framework's `{"detail": "Not Found"}` router-miss body) proves the
  route ran and the containment branch answered. Note: a plain client request
  with a literal `/../` is collapsed before it is sent (curl 7.68, urllib3 1.26
  and httpx 0.28 all rewrite `/../` out of the path), so it would hit the route
  with `workflowId` already resolved and never exercise the guard — use the
  `%2E%2E` form, which reaches the route as `workflowId == ".."` (confirmed
  offline against the backend stack). Remove the seed; backend healthy.
- Item 3: POST then GET export with the server-returned `captureIdPath` → zip; a
  value with an appended `/../` → 400 at validation; backend healthy.
- Item 3b: send one request with a mis-joined dummy credential,
  `GET /workflows/<wf>/results/export?captureIdPath=/aws_dda/system/capture-id-path-2025-01-01-00-00-00token=DUMMYTOKEN`.
  Expect 400 with no dummy value in the body. Then check the backend's logs:
  1. Confirm the request-validation handler's record for that request is
     present in `$COMPONENT_WORK_PATH/logs/application.log` inside the backend
     container, with `[body withheld: it may contain credentials]` for
     `query_params`. This keeps the check from passing vacuously.
  2. Confirm `DUMMYTOKEN` occurs in none of `application.log`, `service.log`
     (same directory) or the container's console output (`docker logs` on the
     device).

  No access-log line is expected for this request, because the download
  routes aren't access-logged (Bug Details, Item 3b). If step 2 finds the
  dummy value in an access-log line, record it and stop: the route has gained
  a sink this spec doesn't cover. Don't narrow the check.
- Item 4 (JP6/JP7): confirm a `GenerationError` still returns its reason in the
  502/SSE, using a **truncated JPEG** (a valid header with cut-off data) as the
  deterministic inducer: it raises `GenerationError("failed to decode the
  supplied image bytes: …")` in the manager (`manager.py:1972-1979`) without
  touching engine state. Do **not** induce an out-of-memory reason: that path
  can mark the model FAILED (`manager.py:1805-1806`) and disturb the soak. The
  reason returned is unchanged by the fix (it is a `GenerationError.reason`), so
  this check pins the preservation half. An unexpected non-`GenerationError`
  may not be inducible on device; if not, record "not inducible on device;
  unit-tested only" and rely on Property 5. Backend healthy.
- Item 5 (b1/b2, if adopted) — exercise the serve-time masking of a
  **pre-upgrade** run, which is the (b1) promise, not only a new run:
  1. Before deploying the new build, run one workflow on the current build with
     a dummy signed-URL-shaped payload and note its execution id.
  2. After deploying the new build, `GET
     /workflows/executions/<id>/metadata` and `.../log` for that id, and run one
     new workflow with the same dummy payload.
  3. Confirm every SigV4 value reads `***` in both the pre-upgrade run's
     `/metadata` and `/log` and the new run's `/metadata`, `/log` and the
     application log. Under (b2), confirm the stored column is masked and record
     whether any signed value the run reads back is affected (the accepted
     change): a fetch, a metadata binding or a `{trigger.…}` placeholder.
     Backend healthy.

## Rollback Considerations

Each change is a self-contained function/constant edit plus its tests. To keep
rollback granular (and to match the preservation rebaseline, which only applies
under Item 5), the commit split in tasks.md is: Changes 1 + 3b (the handler
file); Change 2; Change 3 (the constant + the export route); Change 4; Layer A;
Layer B — each with its own baseline update where one applies. Only Layer A and
Layer B touch the hash-pinned `pipeline_executor.py`, so only their commits
carry the CSI golden rebaseline; a revert of either restores the prior hash. No
schema or data migration is involved (the `trigger_context_json` column shape is
unchanged; Layer B masks only the string content of new rows).

## Open Questions / UNVERIFIED

- Whether any deployed or shipped workflow consumes a signed value from the
  trigger payload. Examples are the Bedrock reference-image-from-trigger-payload
  fetch, or a signed URL read through a metadata binding or a `{trigger.…}`
  placeholder. This decides whether D1 (b2)'s behavior change has real impact.
  UNVERIFIED here (no device/payload access); it is D1's deciding input.
- The lab Dell's availability (offline for a GPU upgrade) — the amd64 and
  amd64Nvidia on-device checks are GATED on the owner confirming it is up.
- Whether a rescan considers the fixes clean. The strictly-below prefix guard
  (DD1) is the normalize-then-prefix shape the path-handling rule is expected to
  model as a check, so Items 2/3's guarded lines are expected to clear; but that
  the rule models the prefix form (and not only `commonpath`) is an expectation
  settled only by the rescan. The engine-wrapped `GenerationError.reason` path
  (Item 4) keeps its contract reason and may still be flagged. Anything still
  flagged is dismissed in task 11 with a recorded reason rather than changed.
  Confirmation needs the post-merge rescan.
- Whether merging the two feature branches is textually clean beyond the
  predicted CSI golden recompute. The read-only `git merge-tree` reports no
  conflict markers, but a real merge is performed in task 7, not here.

## Design Review Responses

The findings of design review iteration 1 are incorporated in the sections
above. The responses below cover iteration 2.

- **M1: the access-log sink and the Item 3b device check.** Addressed, with
  the finding's premise corrected. Re-verified offline in the local backend
  image, with the backend's own `setup_logging` and dummy values.
  - The download routes are registered on a plain `APIRouter()`
    (`download_file.py:56`), and `include_router` keeps them as `APIRoute`.
  - The same mis-joined request produced an `api.access` line, with the value
    unmasked, from an `AccessLogRoute` route, and none from a plain-router
    route.
  - Apart from the test client's own request lines, the anchored-pattern 400
    left exactly one record: the validation handler's.

  So the access-log route class isn't a sink for the export route. Extending
  Item 3b into `log_info` isn't adopted (DD8). It would change no outcome here
  and would alter the access log of every route built on that class. What's
  taken from the finding:
  - `bugfix.md` Item 3b now states the evidence for "no backend log line
    carries the value", replacing "nothing logged".
  - The spec now says the shared redactor masks neither form of the
    mis-joined value: the re-encoded `%3D` form, and the raw form, because of
    its whole-token rule.
  - A structural guard (Property 9) pins the download router's registration,
    so the single-sink premise can't regress silently.
  - The Item 3b device check confirms that the handler's withheld record is
    present and that the dummy value is in no device log. It has a
    stop-and-record rule instead of a narrowed check.
  - The frontend join stays owner option D5.

  Found during this revision: Change 3b now keeps a secret-free query's
  `QueryParams(...)` log text, so the byte-identity it claims holds. The
  previous snippet logged a bare string.
- **M2: exploration-test polarity.** Addressed. The repo convention now
  applies throughout:
  - Every task-1 case asserts the fixed behavior. It FAILS on the unfixed
    tree and PASSES unchanged after the fix.
  - `¬C` cases moved to task 2, and the Bug Details preamble reads
    `not isBugCondition`.
  - Item 3's validator half is asserted through the mounted route (400). The
    `re.search` contrast is an observation only.
  - Item 5 is split into `isBugCondition_A` (task 1) and `isBugCondition_B`
    (task 4.3, (b2) only). Under D1 (a) the case is `xfail(strict=True)`.
- **L1: 3.10 vs 3.11.** Addressed. The Overview, DD1 and Edge Cases say the
  device interpreters (3.10 on JP6, 3.11 elsewhere) raise `ValueError`.
  Testing Strategy names the container image and its interpreter. It records
  that no local image carries this tree's stack on 3.10, and covers 3.10 with
  a stdlib probe plus the Orin check.
- **L2: task 11 enumeration.** Addressed. Task 11 and Acceptance Criterion 15
  list the five findings by location: Items 1, 2, 3, 3 and 4. Items 3b and 5
  have no standalone finding and are verified by their own acceptance
  criteria. Findings are named by file and line rather than finding numbers.
- **L3: Layer B impact.** Addressed. The impact now reads: every SigV4 value
  a run reads back from its trigger payload is masked. That covers
  presigned-URL fetches and signed values surfaced through metadata bindings
  or `{trigger.…}` placeholders; non-signed fields are unchanged. It's
  updated in `bugfix.md`, DD6, Change 5, tasks 4.3 and 9, and owner memo
  D1/D2.1.

The responses below cover iteration 3 (verdict CHANGES_REQUESTED: 1 HIGH, 5
MEDIUM, 10 LOW). Each cited line was re-checked against the source, and the
Layer B and device-request semantics were re-confirmed offline (dummy values).

- **H1 (trunk-merge gating).** Addressed. The combined ref is merged to the
  trunk (task 10 / Build Rounds step 6) only after BOTH this spec's task 8 and
  the jpeg spec's own trunk-merge precondition (jpeg task 8 on the lab Dell and
  task 8b on thor1) pass. If the jpeg precondition can't be met (the Dell is
  down), the owner either waits or merges `spec/localserver-input-handling`
  alone and leaves jpeg to its own task 11, recording that the trunk tree then
  differs from the built tree by jpeg's files. When both hold, the one merge
  also performs jpeg's task 11 (record it in jpeg's notes, carry its PR note,
  use jpeg's `remediation` + fast-forward pattern, then merge to `main`). Task
  7 states that the combined builds are jpeg's task-7 builds and runs jpeg's
  offline built-image check on amd64/amd64Nvidia/arm64/arm64JP7, recording the
  build-source change in jpeg's notes. The failure path is in task 10.
- **M1 (Layer B scope).** Addressed. Layer B now serializes
  `json.dumps(_redact_leaves(context or {}), default=lambda v:
  redact_signed_query(str(v)))`, which masks every string leaf (the MQTT
  `payload` text and any other string leaf, e.g. an OPC UA string `value`),
  never parses or re-serializes the payload (there is no parsed payload at store
  time), and keeps every non-signed byte. AC10 (b2) and task 4.3 now assert
  `load_trigger_context(stored)["payload"] == redact_signed_query(original)`
  and that a SigV4 value in a non-`payload` string leaf reads `***`. Confirmed
  offline against the tree's `load_trigger_context`.
- **M2 (device checks).** Addressed. Item 2's check sends
  `GET /workflows/%2E%2E/capture-details/<cid>/input-image` (so a client
  doesn't collapse `/../`) and asserts the route's own 404 body
  `"Server cannot find the input image path."`, not the router-miss
  `{"detail": "Not Found"}`. Item 1 uses the deterministic non-JSON scratch-file
  inducer through the export route and asserts the exact 500 body + a traceback
  line with the same `request_id`. Item 5 exercises a pre-upgrade run's
  serve-time masking, not only a new run.
- **M3 (test authoring).** Addressed. Task 1 authors the Property 1/3/4/5/9
  hypothesis tests; the Item 5 exploration case is relabelled "sink level" and
  driven through `WorkflowExecutor.execute` via the executor harness (the
  `tags:` line is in `execute`, not `_persist_run_metadata`). Task 4.1 authors
  Property 7 (incl. AC11), Property 8 and the non-mutation case; task 4.2
  authors the pre-upgrade serve-time masking cases; task 1/2 (Item 3) author
  the AC5 symlink case.
- **M4 (build-gate replay).** Addressed. AC13's "build-gate replay" is now
  defined and scheduled: tasks 5 and 7 run `build-custom.sh`'s in-image gate
  body in the local backend image by image ID (a derived copy against a
  writable tree copy, as jpeg task 5 does), compare against the base commit, and
  record known off-device failures (the first block's auth/user-management
  tests needing `libtritonserver.so`, and the stream-component gate) as
  pre-existing.
- **M5 (containment comparison).** Addressed. DD1 (and Changes 2/3) now use
  `realpath` + strictly-below `resolved.startswith(base + os.sep)` — the
  normalize-then-prefix form the rule models as a check and a repo idiom
  (`run_retention._within`) — not `commonpath([resolved, base]) == base`. Task
  11's dismissal is kept only as the fallback if a rescan still flags a line.
- **LOW.** L1 (generators exclude `\x00`; Property 9 filter on
  `str(QueryParams(raw)).lower()`; anchor uses `[0-9]`), L2 (strictly-below is
  the prefix form; only `python_bridge.py:1470-1484` cited for the
  `realpath`-inside-`try` idiom), L3 (streaming `ModelUnavailableError` reaching
  `_failure_reason` is stated and accepted in Change 4), L4 (the SPA shows no
  `request_id`; DD3 and owner memo D2.2), L5 (CSI golden recomputed from the
  merged tree; Layer A import placed to avoid jpeg's; "built tree plus interim
  commits" caveat), L6 (design decisions renamed DD1-DD8; owner decisions keep
  D1-D5), L7 (task 9 placement note, steering's "strictly one variant at a
  time", pre-device commit called an owner-approved deviation, container pytest
  command + recorded versions), L8 (Item 4 inducer is a truncated JPEG, not an
  OOM), L9 (`_redact_leaves` keeps tuples; dummy values avoid `AKIA`/`ASIA` and
  use `.invalid`), L10 (`mqtt.cpp:51` dismissal justification repeated;
  "traversal" reworded to "a `..`-segment value") are all addressed in the
  sections above.
