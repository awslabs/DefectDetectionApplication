# Verification Notes — localserver-input-handling-hardening

**Status:** design authored (revision after design review iteration 3); the
iteration-4 items and the owner's answers of 2026-10-15 (D1 (b1); D2
accepted, D2.2 no fold; D3 batch; D4 dismiss the six now; D5 (b)) were applied
to the spec at the start of implementation. Implementation in progress; the
sections below are filled as each task runs. Nothing has been built, pushed,
deployed or run on a device.

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

## Task 1: exploration on the unfixed tree — PENDING

Every task-1 case asserts the fixed behavior, so each must FAIL here. Per
item, record the test file(s), the failing assertion and the exact unfixed
value it observed (the counterexample):
- Item 1: the 500 body `message` (expected `"Internal Server Error."`; the
  handler was called inside `except`).
- Item 2: for `workflowId=".."`, the path the `open` shim recorded and the
  response instead of 404. For the malformed contained jsonl, the 500 detail
  (device container).
- Item 3: the mounted-route status for the `..`-segment value, instead of 400;
  the target the direct call opened, instead of 404; the 404 detail that
  echoed the value; and the symlink case (AC5). Also record the observation
  that the unfixed constant passes `re.search` and fails `re.fullmatch` for the
  `..`-segment value. It's an observation, not a frozen test.
- Item 3b: the validation handler's log record that carried the dummy `token=`
  value.
- Item 4: the strings returned for `ValueError("boom")` and for a
  non-`GenerationError` exception carrying a `.reason`, instead of the fixed
  string.
- Item 5: the dummy SigV4 values found in the persisted metadata JSON and the
  formatted `tags:` line.

Record the commands (host `--noconftest` or device-container image ID),
pass/fail counts, and confirmation that every task-1 case FAILED. `¬C` cases
are recorded under task 2.

## Task 2: preservation on the unfixed tree — PENDING

Record the frozen baselines captured:
- the two handler **response bodies**, and the uncaught handler's `exc_info`
  record;
- the legitimate-route artifacts and status for Items 2/3, including the
  exact writer-shape acceptance;
- the secret-free validation-log text, the unfixed-expression oracle, and the
  download-router structural guard (Item 3b);
- `_failure_reason(GenerationError(...))` and the 502/SSE shapes;
- the no-SigV4 metadata-JSON and `tags:` bytes for Item 5.

Also record the commands, and that they PASSED on the unfixed tree.

## Task 3: fixes (Changes 1-4, 3b, 6) — PENDING

Record the exact edits applied (file + function), that no hash-pinned file was
touched by Changes 1-4, 3b or 6, and the diff scope. Confirm three things for
Change 3b:
- the 400 response body is unchanged;
- a secret-free query keeps its `QueryParams(...)` log text;
- neither `download_file.py:56` nor `access_log_router.py` was edited.

For Change 6 (task 3.6), record the frontend baseline (suite and build), the
unfixed-tree run of the new jest tests and the shipped template's
counterexample, and the post-change suite and build compared with the
baseline.

## Task 9: Item 5 owner decision (D1) — PENDING

Record the owner's selection (a / b1 / b2), the deciding input (whether any
deployed workflow consumes a signed value from the trigger payload, by fetch,
metadata binding or `{trigger.…}` placeholder), and the
resulting Item 5 code scope. If unanswered at the gate, record that the default
(b1) was taken. Also record the D3 answer (it sets task 7's build source) and
the D2.2 / D5 answers.

## Task 4: Change 5 (Item 5) — PENDING

Record the helpers added (`redact_signed_query`, `_redact_leaves` keeping
tuples, `redacted_run_view` non-mutating), the new
`from workflow_engine.trigger_runtime import …` in `pipeline_executor.py` and
`api.py`, and the Layer A application points (the four sinks). If Layer B:
record that `serialize_trigger_context` became
`json.dumps(_redact_leaves(context or {}), default=lambda v:
redact_signed_query(str(v)))`, that it masks every string leaf (not only
`payload`) and never re-serializes the payload, and that
`load_trigger_context(stored)["payload"] == redact_signed_query(original)` and a
non-`payload` string-leaf SigV4 value reads `***`. Record that the "download
paths stop working" change is in scope: every SigV4 value a run reads back is
masked (presigned-URL fetches and signed values via metadata bindings or
`{trigger.…}` placeholders). Also record the helper-test results (Property 7
incl. AC11, Property 8, non-mutation) and the serve-time masking cases for a
pre-upgrade run. Under (a), record that the task-1 Item 5 case is
`xfail(strict=True)`. Note that `pipeline_executor.py` is hash-pinned and the
CSI golden entry was rebaselined in the Layer A commit.

## Task 5: local checkpoint — PENDING

Record per property (1-10): the test, pass/fail and requirement traceability, and
the run location (host `--noconftest`, device container by image ID, or the
SPA's `npm test` for Property 10).
Confirm that the task-1 tests passed unchanged. Record the container image:
the local backend image whose dependencies match this tree's pins (image ID,
`python3.11 --version`), and the `pytest`/`hypothesis` versions installed into
the container (the image ships neither). Record whether a local 3.10 (JP6)
backend image with the same pins was available for the containment modules. If
not, record "3.10 covered by the stdlib probe and the Orin check". Record the
affected-suite results, the CSI preservation suite and the security guard
result (note the pre-existing `nodes.py` CSI golden failure, unrelated to this
spec), any rebaseline (file + sanctioned path + reason — expected only for
`pipeline_executor.py` under b1/b2), and the **build-gate replay** result: that
it ran as a derived copy against a writable tree copy by image ID, the per-gate
verdicts, the comparison with the base-commit replay, and the recorded
pre-existing off-device failures (first block needs `libtritonserver.so`; stream
gate needs GStreamer/PyAV). Record any pre-existing unrelated failures. Confirm
the no-build/no-device contract was honored.

## Task 6: commit and push — PENDING

Record (after approval): the commit split (Changes 1+3b / 2 / 3 / 4 / Layer A /
Layer B / Change 6, then the docs commit), the named files staged, the hygiene
check result over the diff and over the whole spec dir (no
secrets/identifiers/suppressions), the wording-checklist result (pass/fail
only), the trunk-moved merge if any, the branch pushed, and confirmation that
nothing was merged into or pushed to the trunk and no build was dispatched.

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
