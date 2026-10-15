# Verification Notes — localserver-input-handling-hardening

**Status:** tasks 1-5 complete locally; the owner's answers of 2026-10-15 are
applied (D1 (b1), D2 accepted / D2.2 no fold, D3 batch, D4 the six dismissed
2026-10-15, D5 (b)); the task-9 gate is recorded with D1 (b1). The
feature-branch commits are local and unpushed. Tasks 6 (push), 7 (build), 8
(on-device), 10 (trunk merge) and 11 (post-rescan reconciliation) are
owner-gated and not run here. Nothing has been built, pushed, deployed or run
on a device.

Design-time offline checks (local backend image, `--network none`, dummy
values only; no test files added; scratch probes deleted):
- An embedded NUL makes `os.path.realpath` raise `ValueError` under 3.11.9
  and 3.10.12, and not under the host's 3.9.5.
- The mis-joined export query is masked by the shared redactor in neither its
  re-encoded nor its raw form, and is masked in the `&token=` form.
- Under the backend's own logging setup, the plain-router route the download
  routes use emits no access-log line. The anchored-pattern 400 leaves only
  the validation handler's record, which carries the dummy value today.
- Iteration 3 (re-confirmed against the backend stack, dummy values):
  - The uncaught-500 handler's `"Uncaught Exception"` record carries the same
    `request_id` the response returns, and the traceback names the raised
    exception.
  - `GET /workflows/%2E%2E/capture-details/<cid>/input-image` reaches the route
    with `workflowId == ".."` (so the containment branch runs), while a
    router-miss answers `{"detail": "Not Found"}`; a non-encoded `/../` would be
    collapsed by the client before sending. This is why Item 2's device check
    uses `%2E%2E` and asserts the route's own 404 body.
  - The anchored `[0-9]`-digit pattern accepts the writer shape and rejects a
    non-ASCII (Arabic-Indic) digit timestamp, a trailing newline, a `..`-segment
    value and an appended `token=`.
  - Layer B as `json.dumps(_redact_leaves(context or {}), default=lambda v:
    redact_signed_query(str(v)))`, against the tree's own
    `load_trigger_context`: a no-SigV4 MQTT and a no-SigV4 OPC UA context
    serialize byte-identically to the current writer;
    `load_trigger_context(stored)["payload"] == redact_signed_query(original)`;
    `payload_json` re-derives masked with the same keys; an OPC UA string
    `value` and a non-JSON-native variant carrying a signed URL are both masked;
    `_redact_leaves` keeps a tuple a tuple and does not mutate its input; the
    masker is a fixed point.
  - `build-custom.sh`'s in-image gate body extracts to 141 lines (`bash -n`
    clean, 7 `Running …` gates), unchanged by the trunk delta — the shape task 5
    / task 7 replay against a writable tree copy.

Redaction policy for this file (public-repo spec dir): no device thing names,
hostnames, LAN addresses, account ids, ARNs, registry URIs, bucket names,
credentials, personal names or analyzer/tool names. Lab devices are named only
"MIC-730", "Orin", "thor1" and "the lab Dell". Image references are redacted
labels (repository basename + tag) and local image IDs only. Component artifacts
are named by component name and version only.

## Task 1: exploration on the unfixed tree — DONE

Authored before any production edit and run on the unfixed tree (HEAD at the
time, the committed spec on `296c230c`). Every task-1 case asserts the fixed
behavior and FAILED, each matching its documented counterexample. The five test
files are frozen in the state directory (`.agents/localserver-input-handling/frozen/`,
with `SHA256SUMS`). Image IDs and versions: host CPython 3.9.5, pytest 8.4.2,
hypothesis 6.141.1; device container `flask-app:latest` =
`sha256:f2e221c3e2d1…` (Python 3.11.9, fastapi 0.109.2 / pydantic 2.6.4 /
starlette 0.36.3), pytest 8.4.2 + hypothesis 6.141.1 installed into the
throwaway container; 3.10 stdlib probe under `flask-app:arm64-1.1.0` =
`sha256:6d5ce25b9e2d…` (3.10.12).

New properties carry `@settings(max_examples=50, deadline=None)`; inside
`@given` tests no function-scoped fixture is used (a local collecting
`logging.Handler` replaces `caplog`, and `tmp_path_factory` gives a per-example
directory). Each task-1 property pins its counterexample with `@example(...)`.

- **Item 1** (`stream_ingest/test_log_redaction.py::TestUncaughtExceptionBody`,
  host). FAILED: for `KeyError("source-ref")` raised and handled inside
  `except`, the 500 body `message` was
  `"Internal Server Error. Error: 'KeyError: 'source-ref'.'"`, not the fixed
  `"Internal Server Error."`. Property 1 falsified immediately
  (`KeyError, "source-ref"`).
- **Item 2** (`workflow_engine/test_input_image_containment.py`, device
  container authoritative; host skips NUL). FAILED: `workflowId=".."` opened
  the one-level-up jsonl (the `open` shim recorded the mapped
  `/aws_dda/inference-results/../escape.jsonl`) and served it (200), instead of
  404 with no open; the malformed contained jsonl answered 500 with detail
  `"Server cannot find the input image path, Error Expecting property name …"`,
  not the fixed detail; the `a\x00b` member (container, 3.11) answered 500 with
  `"… Error embedded null byte."` and 1 open, not 404 with none; Property 3
  falsified at `workflow_id="..", capture_id="cap-1", seed_mode="valid"`.
- **Item 3** (`workflow_engine/test_export_path_containment.py`, device
  container authoritative; host skips NUL + the zip). FAILED: the mounted route
  accepted the `…/capture-id-path-…/../../../target.json` value (status != 400,
  token check reached), not 400; the direct call opened the escaped
  `<tmp>/target.json` (142 → clean 442) instead of 404 with no open; the
  missing-value 404 detail echoed the value; the capture-name symlink resolved
  outward and opened the target; the NUL member answered 404 but with the
  **echoed** detail, not the fixed string; Property 4 (validator) falsified at
  the `/../../../target.json` suffix, Property 4 (containment) at
  `capture-id-path-…/../../..` → `<tmp>/target.json`. Observation (one-off
  `python3 -c`, not a frozen test): for the `..`-segment value the unfixed
  constant `re.search(P, v)` is truthy and `re.fullmatch(P, v)` is `None`.
- **Item 3b** (`stream_ingest/test_log_redaction.py::TestRejectedQueryLog`,
  host). FAILED: the validation handler's record carried the dummy credential
  in the re-encoded `…token%3DLOCALSESSIONTOKENdummy1234` form; Property 9's
  fix half falsified on the first generated credential.
- **Item 4** (`text_generation/test_failure_reason.py`, host). FAILED:
  `_failure_reason(ValueError("boom"))` returned `"boom"`;
  `_failure_reason(_HasReason("attr-reason"))` returned `"attr-reason"`;
  Property 5 falsified at `ValueError("boom")`.
- **Item 5** (`workflow_engine/test_trigger_payload_redaction.py`, host).
  FAILED: the persisted `{capture_id}.json` and the `tags:` line (formatted
  through `RedactingFilter`) both carried `DUMMYKEYID1234567890`
  (`X-Amz-Credential`) and `20250101T000000Z` (`X-Amz-Date`). The existing
  shared redactor already masks `X-Amz-Security-Token` and `X-Amz-Signature`
  (the `-` before the name makes them whole tokens), so only Credential and
  Date leak — exactly the masking boundary the design records.

## Task 2: preservation on the unfixed tree — DONE

Authored in the same files and run on the unfixed tree; every `¬C` case PASSED
and is frozen.
- Item 1 (Property 2): `http_exception_handler` and the request-validation
  handler produced byte-identical bodies to the unfixed-expression oracle over
  the generated exception set; the uncaught handler's `"Uncaught Exception"`
  record carried `exc_info` naming `KeyError` (AC2).
- Item 2 (P-routes): the legitimate `(wf, cap-1)` pair served the seeded
  image (`FileResponse.path`); a database row served the row's image; a
  contained id with a missing image answered 404.
- Item 3 (P-routes): the exact writer-shape value entered the handler (the
  token check was reached); a seeded capture-id-path list yielded the zip with
  members `manifest`, `a.jpg`, `b.jpg` (device container only; the host skips
  it — `inference_results_utils` needs `awsiot`).
- Item 3b (Property 9 preservation half + guard): a query naming no sensitive
  key logged byte-identically to the oracle
  `str({"errors": safe_errors, "body": _body_for_log(exc.body), "query_params": request.query_params})`;
  one frozen literal (`captureIdPath=not-a-capture-path&limit=5`); the `ast`
  guard confirms `unauthenticated_router = APIRouter()` has no `route_class`.
- Item 4 (Property 6): `_failure_reason(GenerationError("qwen2-vl", "oom"))`
  returned `"oom"`; the 502 body was `{"model_name": "qwen2-vl", "reason": "oom"}`
  and the SSE event `data: {"error": {"reason": "oom"}}\n\n` exactly.
- Item 5 (observation-first): the no-SigV4 context (`image_url` with
  `?part=42&lane=3`) persisted and logged byte-identically, with no `***`
  introduced; the frozen literal check asserts byte-identity after the fix.

Commands (wrapped in `timeout`): host runs via
`PYTHONPATH=src/backend:test/backend-test:test/backend-test/workflow_engine
python3 -m pytest <file> --noconftest -p no:cacheprovider`; container runs by
image ID with pytest/hypothesis installed first, `--noconftest`. Unfixed-tree
counts: Item 1/3b host 4 failed (task 1) / 43 passed; Item 2 container 4 failed
/ 3 passed; Item 3 container 7 failed / 2 passed; Item 4 host 3 failed / 3
passed; Item 5 host 1 failed / 1 passed.

## Task 3: fixes (Changes 1-4, 3b, 6) — DONE

Edits, by file + function:
- **Change 1** `exception_handlers.unhandled_exception_handler`: dropped the
  `exception_name` line; the return is
  `JSONResponse({"message": "Internal Server Error.", "request_id": get_request_id()}, status_code=500)`;
  the `sys.exc_info()` capture and the `logger.error("Uncaught Exception", exc_info=…)`
  line are kept.
- **Change 3b** `exception_handlers.request_validation_exception_handler`: added
  `query_text = str(query_params)` and `logged_query = _body_for_log(query_text)`;
  the detail's `query_params` is `query_params if logged_query == query_text else logged_query`.
  Confirmed: the 400 response body is unchanged (Property 2 and the existing
  suites); a secret-free query keeps its `QueryParams(...)` text (Property 9
  preservation half); neither `download_file.py:56` nor `access_log_router.py`
  was edited.
- **Change 2** `download_file.load_input_image_from_worflow_by_capture_id`:
  added `INFERENCE_RESULTS_DIR` to the constants import; the jsonl fallback
  resolves `base` and `jsonl_file` with `os.path.realpath` inside one
  `try/except ValueError`, guards the sink with
  `jsonl_file.startswith(base + os.sep)` (404 fixed detail otherwise), and the
  inner read's `except` logs at WARNING with `exc_info` and 500s with the fixed
  detail.
- **Change 3** `constants.CAPTURED_IDS_PATH_PATTERN` anchored
  `^/aws_dda/system/capture-id-path-[0-9]{4}-…-[0-9]{2}$`;
  `download_file.get_inference_result_data_for_retraining` resolves
  `base`/`resolved` with `realpath` inside `try/except ValueError` and opens
  only when `resolved.startswith(base + os.sep)` and `os.path.exists(resolved)`;
  the 404 detail no longer echoes the value.
- **Change 4** `text_generation._failure_reason`: the DD4 helper
  (`isinstance(error, GenerationError) and error.reason` → `error.reason`; else
  `logger.error("Unexpected text generation failure", exc_info=error)` and the
  fixed string). No call-site or shape change.
- **Change 6** (task 3.6, Item 3c) is recorded under Task 6 (frontend); it is
  not a backend change.

No hash-pinned file was touched by Changes 1-4, 3b or 6: after commit (iv) the
CSI surface suite showed only the pre-existing
`vendor/workflow_core/catalog/nodes.py` failure (same as the 296c230c
baseline), the two guard suites equalled the baseline, and
`git diff 296c230c -- src/backend/endpoints/route/access_log_router.py` was
empty. Only Change 5 (Layer A) touches the hash-pinned `pipeline_executor.py`
(Task 4).

### Change 6 — frontend export link (task 3.6)

Frontend baseline on the unfixed tree (`src/frontend`, Node 20.20.2, after
`npm ci --ignore-scripts`): `CI=true npm test -- --watchAll=false` → 35 suites
/ 308 tests passed; `npm run build` → compiled with the project's standing
warnings (rc=0). The new `result-history/utils.test.ts`, run on the unfixed
tree, failed 5/5: `buildExportUrl` did not exist
(`TypeError: (0 , _utils.buildExportUrl) is not a function`) and the call site
still held the mis-joined `token=${encodeURIComponent(token)}` template — the
shipped counterexample (for the writer shape with auth on, the parsed query
holds one `captureIdPath` ending `token=<credential>` and no `token`). After
Change 6: the suite passed 5/5 (unit auth-on/off, the reserved-character path,
Property 10 at 200 runs, and the call-site check); the full suite is unchanged
except the pre-existing flaky `DeployedWorkflowDetails.exploration` property
suite (see Task 5); `npm run build` compiled with the same warning set as the
baseline (rc=0).

## Task 9: Item 5 owner decision (D1) — DONE (owner answered 2026-10-15)

The owner answered on 2026-10-15 (`.agents/localserver-input-handling/owner-answers-2026-10-15.md`):
- **D1 = (b1):** Item 5 Layer A only (helper + the four served/logged sinks),
  no Layer B. The stored `trigger_context_json` column is unchanged, so what a
  run reads back is unchanged. Task 4.3 is not implemented; the task-1 Item 5
  sink case is a real pass (not `xfail`).
- **D2 accepted; D2.2 no fold:** the request_id is not folded into the 500
  body; any fold is a follow-up round.
- **D3 = batch:** task 7 builds from `build/jpeg-and-input-handling`.
- **D5 = (b):** the frontend export link is fixed in this round (Item 3c /
  Change 6 / task 3.6), with a frontend device check added to task 8.

Deciding input for D1 (whether a deployed workflow consumes a signed value
from the trigger payload) stays UNVERIFIED here; the owner chose (b1)
regardless, so Layer B's behavior change is not adopted. The task-9 box is
checked because the owner answered; tasks 7/8/10/11 remain owner-gated.

## Task 4: Change 5 (Item 5) — DONE (Layer A only, D1 = b1)

Helpers added beside `serialize_trigger_context` in `trigger_runtime.py`
(stdlib only; `import re` added to the module import block):
- `redact_signed_query(value)` — masks the value of each SigV4 parameter in
  the literal `name=value` form with `***`; value class
  `[^&;,)\]}#"'\s]+` (the shared redactor's terminators plus `#`); total (a
  non-string or empty string returns unchanged); a fixed point;
- `_redact_leaves(value)` — recurses into string leaves of dict/list/tuple,
  keeps a tuple a tuple, returns other leaves unchanged;
- `redacted_run_view(tag_values)` — a shallow copy whose `trigger` section is
  `_redact_leaves`-masked; returns the input unchanged when it is not a mapping
  or has no `trigger` key; never mutates its input.

Imports (each module imports only what it uses):
`pipeline_executor.py` adds `from workflow_engine.trigger_runtime import
redacted_run_view` in the existing `from workflow_engine import …` grouping
near `:88` (not jpeg's `:129` block); `api.py` adds
`from workflow_engine.trigger_runtime import redact_signed_query, redacted_run_view`.

Layer A sinks (no behavior change):
- `_persist_run_metadata` dumps `redacted_run_view(tag_values or {})`, computed
  before the file is opened;
- the completed-run `tags:` log computes
  `logged_tags = redacted_run_view(tag_values)` inside `try/except RecursionError`
  (placeholder `"<tags withheld: too deeply nested to mask>"`), after the run
  is committed COMPLETED, so the run status is never affected;
- `get_workflow_execution_metadata` returns
  `redacted_run_view(read_run_metadata(...))`;
- `get_workflow_execution_log` wraps the text in `redact_signed_query(...)`.

Layer B not implemented (D1 = b1): `serialize_trigger_context` is unchanged, so
the stored column keeps its bytes and what a run reads back is unchanged
(presigned-URL fetches and `{trigger.…}` / metadata-binding resolution are
unaffected).

Helper and serve-time test results (host, `test_trigger_payload_redaction.py`,
8 passed): Property 7 (masking, byte-for-byte preservation, fixed point,
non-string passthrough — covers AC11); Property 8 and the `redacted_run_view`
non-mutation / tuple-preservation case; the sink-level task-1 case through the
executor harness; the pre-upgrade serve-time masking of `/metadata` and `/log`
(every SigV4 value reads `***`, `is_anomalous` and the non-signed payload
fields preserved); and the RecursionError guard (the run stays COMPLETED and
the `tags:` record carries the placeholder).

`pipeline_executor.py` is hash-pinned by the CSI consumer golden; its entry was
rebaselined in the Layer A commit (95c58736):
`7b80c297aaa3a3fab07f36a70185476ac9b928a1a7a0d7c0bbb490e3e36b03dc` →
`8edf43bfcd39aed25e3d9a29817923c3599e2b8f34427bf3a7259550a93d186a`. The stale
`vendor/workflow_core/catalog/nodes.py` entry is left as-is (pre-existing; the
jpeg merge recomputes every entry at task 7).

## Task 5: local checkpoint — DONE

The five test files are byte-identical to their `frozen/` copies (the four
backend files whole, `test_trigger_payload_redaction.py` through its frozen
head of 185 lines; the task-4 helpers/serve-time/guard cases are appended
below the marker). The task-1 tests pass unchanged after the fixes; the task-2
tests still pass.

Per property:
- **1** (Item 1 fix) PASS, host — Req 1.1, 2.1.
- **2** (Item 1 preservation) PASS, host — Req 3.1.
- **3** (Item 2 fix + containment) PASS, device container (host skips the NUL
  member) — Req 1.2, 2.2, 3.2.
- **4** (Item 3 anchor + containment) PASS, device container (host skips the
  NUL member) — Req 1.3, 2.3, 3.2.
- **5** (Item 4 fix) PASS, host — Req 1.4, 2.4.
- **6** (Item 4 preservation) PASS, host — Req 3.3.
- **7** (Item 5 helper, incl. AC11) PASS, host — Req 2.5.
- **8** (Item 5 preservation) PASS, host — Req 3.4.
- **9** (Item 3b fix + preservation + guard) PASS, host — Req 1.6, 2.6, 3.1.
- **10** (Item 3c export link) PASS, SPA `npm test` — Req 1.7, 2.7, 3.7.

Container image: the local backend image whose pins match this tree,
`flask-app:latest` = `sha256:f2e221c3e2d1…`, `python3.11 --version` = Python
3.11.9, fastapi 0.109.2 / pydantic 2.6.4 / starlette 0.36.3; the image ships
neither pytest nor hypothesis, so the throwaway container installs
pytest 8.4.2 + hypothesis 6.141.1 (pinned to the host versions). The two
full-app files run against a writable `git archive HEAD` copy, because
`LocalServerBaseTestCase.setUp` writes into the tree, with
`LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64` so
`libtritonserver.so` resolves. HEAD container runs: the five new/modified files
plus `test_trigger_metadata_persistence.py` → 78 passed (NUL members run);
`test_inference_result_api.py` + `test_workflows_api.py` → 42 passed, matching
the baseline with no failing id.

No local image carries this tree's pinned stack on 3.10; 3.10 is covered by the
stdlib probe (bare `realpath` and the two lifted route bodies under 3.10.12,
`flask-app:arm64-1.1.0` = `sha256:6d5ce25b9e2d…`) and the Orin device check
(task 8, Item 2's `a%00b`). NUL probe on HEAD: under 3.10.12 and 3.11.9 both
routes answer 404 with their fixed details and 0 open calls; under the host
3.9.5 the input-image route answers 500 with the fixed detail (realpath does
not raise on a NUL there — the WARNING line is logged with the traceback — which
is why the explicit NUL member skips below 3.10) and the export route answers
404 with the fixed detail.

Affected-suite comparison, HEAD vs the 296c230c baseline (host, `--noconftest`;
`comm -13` on the sorted failing/erroring id sets): no new failing id in any of
`stream_ingest/` (2 pre-existing collection errors), `text_generation/` (0),
`workflow_engine/` (7 pre-existing), `output_bindings_fixes/` (7 pre-existing),
the text-generation consumers (1 pre-existing: the `vllm_model_reload`
hash-pin), the download-route source checks (1 pre-existing collection error),
the CSI surface (only `nodes.py`), and the two guard suites (0). HEAD passing
counts rose only by the new tests (e.g. `workflow_engine/` 1898 → 1919).

CSI preservation: after the Layer A rebaseline the `pipeline_executor.py` entry
passes; only the pre-existing `vendor/workflow_core/catalog/nodes.py` entry
fails (unrelated to this spec; the jpeg merge recomputes it at task 7). Guard
suites: 4 passed / 3 skipped, equal to the baseline. The one rebaseline is
`backend_csi_consumers_sha256.json`'s `pipeline_executor.py` entry, in the
Layer A commit, for the intended Layer A edit (hashes in Task 4).

Build-gate replay (AC13): `build-custom.sh`'s in-image gate body, extracted
verbatim (141 lines, `bash -n` clean) and run as a derived `set +e` + ERR-trap
copy against a writable `git archive` tree copy by image ID, on both 296c230c
and HEAD. HEAD's per-gate summary is identical to the base run (modulo line
numbers and timings): the six security audit gates pass in both
(injection/deserialization, secrets/credentials/JWT, IAM, S3, Docker,
dependency), and the only two `GATE-CMD-FAILED` are the pre-existing off-device
ones — the first block (auth / user-management unit tests needing
`libtritonserver.so`) and the stream-component gate (needs GStreamer/PyAV the
local image lacks). `git diff 296c230c --stat -- test/backend-test/security/baselines/`
is empty. Logs in `impl/gate-base.*` and `impl/gate-head.*`.

Property budget note: new properties use `@settings(max_examples=50,
deadline=None)` as the step prompt requires; the `workflow_engine` and
`text_generation` conftests register a 25-example "fast" profile, but the host
and container runs use `--noconftest`, which drops those profiles, so the
per-property 50 applies and there is no conflict in these runs.

Frontend flaky note: with Change 6 applied, the SPA suite is unchanged against
the pre-change baseline except `DeployedWorkflowDetails.exploration.test.tsx`,
a fast-check property that uses no fixed seed and falsifies on
whitespace-collapsing counterexamples (e.g. `" !"`). It is unrelated to this
change (it imports none of `result-history/`): run against a copy of the
committed tree (HEAD, no frontend edits) it failed once and passed twice over
three runs, so it is pre-existing seed-dependent flakiness, not a regression.

The no-build/no-device contract was honored: no component or image was built,
nothing was deployed, pushed or run on a device. `npm run build` was a local
compile check only; its `build/` output was removed.

## Task 6: commit (local part) — DONE; push PENDING (owner-gated)

The fix commits are made locally on `spec/localserver-input-handling`; the push
is not done (it needs the owner's go at task 6). Commit split and hashes
(oldest first), each carrying its own item's tests:
- `e3aebb09` docs(spec): design review iteration 4 items + owner answers (d0)
- `a1022f18` fix(backend): fixed 500 body; withhold credential-bearing query logs (i, Changes 1 + 3b)
- `d953fabe` fix(backend): contain the input-image jsonl fallback path (ii, Change 2)
- `73fec9f4` fix(backend): anchor captureIdPath and contain the export path (iii, Change 3)
- `db451906` fix(backend): fixed reason for unexpected text-generation errors (iv, Change 4)
- `95c58736` fix(workflow-engine): mask signed query values in run metadata and logs (v, Layer A + CSI rebaseline)
- `f280cc02` fix(frontend): build the result-history export link with encoded parameters (vii, Change 6 / Item 3c)
- (d1) docs(spec): this verification-notes + tasks.md record

Each commit staged named files only (no `git add .`); the staged file list was
checked to equal the commit's list before committing. The hygiene gate ran on
every commit's staged added lines and its message, and the wording checklist's
four checks (label swap → 0 originals; the identifier grep; the neutral-wording
term grep; the identifier-class grep) passed for every commit (recorded
pass/fail only). The whole committed spec directory also passes all four
checks. Messages are framed as input validation / path containment / output
encoding / error-response hardening, and each code-commit body states what was
verified locally and that on-device verification is pending.

Scope (`git diff 296c230c`): the four spec files; the seven source files
(`exception_handlers.py`, `download_file.py`, `constants.py`,
`text_generation.py`, `trigger_runtime.py`, `pipeline_executor.py`, `api.py`);
the CSI golden; the five backend test files; and the three frontend files
(`result-history/utils.ts`, `utils.test.ts`, `ResultCardContent.tsx`). Empty:
`.kiro/specs/security-scan-remediation-high/requirements.md`,
`src/backend/endpoints/route/access_log_router.py`, and any
`src/frontend`/`hmi`/`edge-cv-portal`/`vendor` path outside `result-history`.
Nothing was merged into or pushed to the trunk, and no build was dispatched.

## Owner-gated — PENDING (not executed in this run)

### Task 7: build round — PENDING
Record: the jpeg-branch merge-up result, the combined ref
`build/jpeg-and-input-handling`, the recomputed CSI golden hashes for **every**
pinned entry from the merged tree (resolved by recompute, not by taking a
branch's side), the re-run CSI + both specs' suites on it, the build-gate replay
on the combined ref, the two pushes and their hygiene result, the pre-build
checks, and each architecture's build
(component name + version) — strictly one variant at a time. These are jpeg's
task-7 builds too: record jpeg's offline built-image check on the
amd64/amd64Nvidia/arm64/arm64JP7 builds and the build-source change (owner memo
D3) in jpeg's `verification-notes.md` "Task 6". Combined with the jpeg round.

### Task 8: on-device verification — PENDING
Per architecture (MIC-730 / Orin / thor1 / the lab Dell for amd64 and
amd64Nvidia / built-only for generic arm64): the build version, the per-item
check results (Items 1, 2, 3, 3b, 3c, 5 everywhere; Item 4 on JP6/JP7; the jpeg
folder-source non-regression check on MIC-730 and Orin (its four sub-check
results, also recorded in jpeg's notes; it doesn't replace jpeg task 8b); the
Item 3c download on at least one auth-enabled station, recording the link's
shape only), and the sustained-health
observation (>= 30 min, >= 10 runs, >= 10 requests per route). Record each
deterministic inducer used: Item 1 the non-JSON scratch file + exact 500 body +
`request_id` in the traceback; Item 2 the `%2E%2E` request + the route's own
404 body `"Server cannot find the input image path."`, and the `a%00b`
request's 404 with the same body (or, if the device rejected the NUL before
the route, that outcome and the AC4 rewording); Item 4 the truncated JPEG with
a READY multimodal model (not an OOM). For Item 5, record the pre-upgrade run's masked `/metadata`
and `/log` as well as a new run's. For Item 3b, record two things: that the
validation handler's withheld record was present, and that the dummy value was
in none of `application.log`, `service.log` or the container console output. If
it turned up in an access-log line, record that and stop rather than narrowing
the check. The lab Dell checks are GATED on the owner confirming the device is
up (offline for a GPU upgrade). Record built-only dispositions where no device
is available. Item 5 uses dummy values only, masked output recorded. Item 4's
unexpected-error path: record "not inducible on device; unit-tested only" if it
cannot be induced. No thing names / hostnames / LAN addresses / bucket names.

### Task 10: trunk merge — PENDING
Record which precondition path was taken: (both gates met) the combined-ref
merge, which also performs jpeg's task 11 — merge into `remediation`, fast-forward
`integration/all-specs`, then merge to `main`, and the jpeg task-11 PR note; or
(jpeg's lab Dell/thor1 gate not met) that the owner waited, or merged
`spec/localserver-input-handling` alone and recorded that the trunk tree differs
from the built tree by jpeg's files. Record the devices verified on which arch
for both specs, and (if the trunk moved since task 7) that the interim trunk
commits were merged into the combined ref first. Merge only, no force.

### Task 11: reconciliation after the main rescan — PENDING
Record the outcome for each of the five changed findings, by location:
- `exception_handlers.py:107` (Item 1);
- `download_file.py:113` (Item 2);
- `download_file.py:318` and `:323` (Item 3);
- `text_generation.py:660`/`:713` (Item 4).

Record any residual dismissed with its reason (Item 4 contract reason;
containment-guarded Item 2/3 lines, with "realpath + strictly-below prefix
guard" as the fallback reason — the prefix form is expected to clear these).
Items 3b, 3c and 5 have no standalone finding; they're verified under tasks 5
and 8. The six dismissals are not part of this task (see below).

## Static-analysis dismissals (owner decision D4) — DONE 2026-10-15

The owner chose to dismiss the six findings in bugfix "Items reviewed and left
unchanged" without waiting for this spec's build or rescan (this spec changes
none of their files). They were dismissed on the default branch at
2026-10-15 13:37Z with the platform's fixed reasons, each checked first to be
open at the expected location:
- false positive ×3: `download_file.py:90`, `CapturedCards.tsx:158`,
  `InteractableImage.tsx:114`;
- won't fix ×2: `mqtt.cpp:28` and `:51`;
- used in tests ×1: `test_docker_base_image_bug_condition_exploration.py:120`.

The five findings this spec changes stay open until task 11.

## UNVERIFIED / open

- Whether any deployed or shipped workflow consumes a signed value from the
  trigger payload, by fetch, metadata binding or `{trigger.…}` placeholder.
  This decides Item 5 Layer B / D1 (b2) impact — owner memo D1.
- The lab Dell's availability (amd64 / amd64Nvidia on-device checks GATED).
- Whether a rescan considers the fixes clean. The strictly-below prefix guard
  (design DD1) is the normalize-then-prefix shape the path-handling rule is
  expected to model as a check, so Items 2/3's guarded lines are expected to
  clear; the engine-wrapped Item 4 path keeps its contract reason and may still
  be flagged. Anything still flagged is dismissed in task 11 with a recorded
  reason. Settled only by the post-merge rescan.
- Whether merging the two feature branches is textually clean beyond the
  predicted CSI golden recompute (the golden conflict spans jpeg's adjacent
  `nodes.py` line; Layer A's `trigger_runtime` import is placed to avoid jpeg's
  `:129` import block). A read-only `git merge-tree` showed no conflict markers
  in the function bodies; the real merge is task 7.
- Whether `%00` in a path segment reaches the input-image route on device
  (Item 2's `a%00b` request). Task 8 records the outcome either way.
- Whether a multimodal model is READY on the Orin and thor1 for Item 4's
  check.
- Which station runs with authorization or local login on for Item 3c's
  auth-enabled download check (task 8).
