# Implementation Plan

## Overview

This plan implements the approved `design.md` for the defects in `bugfix.md`:

- a model component's Shutdown deletes the model's Triton repository entries under an in-flight load;
- Triton 2.45.0's python_backend (r24.04) then waits forever for a stub that has exited;
- edgemlsdk's single model-load thread makes every later load queue behind it;
- the Startup still exits 0, so the deployment reports COMPLETED;
- the HARD dependency on `aws.greengrass.Cli` turns nearly every deployment into a restart cascade.

| Order | Tasks | What | Commits |
|---|---|---|---|
| 1 | 1 | Documents: the nine design-review NITs, the D12 follow-up notes, and the documents-only follow-up spec `localserver-config-live-update` | two docs-only commits |
| 2 | 2, 3 | Baseline, exploration tests, then F1 (lock and cleanup guard), the per-run gate, F3 (Startup outcome record) and F5b (content-aware publish) | none yet (device-side) |
| 3 | 4 | F4: wedge detection, self-heal on by default, run activity, surfaces | none yet (device-side) |
| 4 | 5 | F2 (python_backend backport, JP5/JP6/x86 Dockerfiles) and F5a (SOFT CLI dependency in every recipe variant and publish path), with every pin rebaselined | none yet (device-side) |
| 5 | 6 | F6: Portal deployment readiness warning | one commit (Portal code, not deployed) |
| 6 | 7-12 | Pre-build gate, the JP5 build and publish, the MIC-730 verification (R1-R3, self-heal) | — |
| 7 | 13 | Device-side commits, `verification-notes.md` | three code commits, one docs commit |
| 8 | 14 | JP6 and JP7 builds; other devices only with the owner's go | one docs commit |
| 9 | 15 | Final report and teardown | — |

Dependencies: task 3 needs task 2. Task 4 needs task 3's modules (`load_tracker`, `repo_entries`,
`model_status_records`). Tasks 5 and 6 depend on nothing in tasks 2-4. Task 7 needs tasks 3-5. Tasks
8-13 run strictly in order. An independent code review of the whole branch gates task 15.

**Owner decisions** (bugfix.md "Owner decisions", design.md "Owner Decisions"):

- F2 is a backport onto Triton 2.45.0; Triton is not upgraded.
- F5a is SOFT, plus the follow-up spec.
- F4 self-heal is **on by default on every device**, with a per-device opt-out. Any "opt-in" wording
  elsewhere is superseded and is not a finding.
- The MIC-730's state may be changed for verification, including deliberate wedge reproductions on
  unfixed builds.

**F5b and F6 are in scope.** D9 makes a changed publish swap under the exclusive model lock, so F5b
carries part of the lock design. F6 is three Portal files plus tests.

## Ground rules (every task)

**Where.** All edits, builds and tests happen in this worktree,
`/home/ubuntu/github/jp5/main/triton-cleanup-wedge`, addressed by absolute path. A relative path can
land in the parent workspace. Never commit in the main clone. Scratch files and evidence stay outside
the repository.

**Git.**

- Local commits only, in Conventional Commits style (`docs(spec): …`, `fix(triton): …`,
  `feat(portal): …`).
- No push, no rebase, no fast-forward of `remediation` or `integration/all-specs`, and never a force
  push. Each of these is owner-gated.
- Stage files by name. Never `git add -A`, `git add .`, `git stash`, `--amend` or `--no-verify`. To
  compare with the base, `git archive cc35b92a` into a scratch directory.
- Never stage `review.json`, `design-review.*`, the task directory `task-triton-model-cleanup-wedge/`,
  build logs, or the `gdk-config.json` and `recipe.yaml` swaps that a build makes.
- **On-device changes commit only after the JP5 verification on the MIC-730 (task 13).** That covers
  everything under `src/`, the recipes and the publish rewriters. Each commit message states what was
  verified and on which arch, and names the arches still pending.

**Before every commit:**

1. Run the pin sweep in design.md "Build, Baselines and Rollout" ("Before every commit"). Every hit is
   updated in the same commit, or shown to be unaffected.
2. Run the public-repository hygiene check over the added lines of `git diff --cached -U0` and over
   the commit message. The label check (`label_swap.py --check -`) exits 0, and the forbidden-wording,
   twelve-digit-id, IPv4, registry-URI, e-mail, device-name, host/port and bucket-name greps report
   nothing.
   - Allowed: neutral wording, and hardware model names (MIC-730, Jetson AGX Orin, Thor).
   - Not allowed: device thing names or nicknames, account ids, hostnames, ports, bucket names, IoT
     endpoints, e-mail addresses, and any mention of an external report.
3. `git diff --cached --name-only` lists exactly the files the task names.

**Builds** (`.kiro/steering/builds.md`, binding):

- One build at a time, machine-wide. Other worktrees build on this host too. Before every build, both
  `pgrep -af "gdk component build"` and `pgrep -af "build-custom.sh"` must return nothing (also check
  `run_jp_builds.sh`); otherwise wait.
- Rebaseline every preservation-tracked file **before** building, run the guard suite green, and move
  `edge-cv-portal/infrastructure/cdk.out` aside.
- Never run a Portal deploy while a build runs.
- JP5 first. Each build's output goes to its own log in the evidence directory. Restore
  `gdk-config.json` and `recipe.yaml` afterwards.

**Owner gates.** Each one is a pause with a warning, and work waits for the owner's go:

- **G1**: before the first change to the MIC-730's state (any deployment, restart, reproduction, or
  model start or stop). The device hosts the owner's PPE demo, and other work may be using it.
  Read-only inspection is allowed at any time.
- **G2**: before any Portal deploy.
- **G3**: before verifying on any device other than the MIC-730.

**Device identifiers** never enter committed files, including `verification-notes.md`. Write "the
MIC-730 (JP5)".

## Commands

Run from the worktree root unless noted.

- **Host suites:** `PYTHONPATH=src/backend:test/backend-test python3 -m pytest <suite> -q -p no:cacheprovider --noconftest`.
  Task 2.1 records the exact form that runs green today for every suite (for example,
  `workflow_engine` keeps its own conftest and adds `test/backend-test/workflow_engine` to
  `PYTHONPATH`). Later runs reuse those forms.
- **New suite** `test/backend-test/triton_cleanup_wedge/`: host-side, `--noconftest`, with a local
  `fakes.py`. The root conftest's Hypothesis profile is not loaded, so property tests set
  `@settings(max_examples=25, deadline=None)` or lower. `test/backend-test/readiness_fakes.py` is not
  modified.
- **In-image suites:** one suite path per process. Pin the backend image by id for the whole session,
  because other builds retag `flask-app:latest`. Mount at `/w/dda`, never `/repo`: the repository root
  has a tracked `__init__.py`, so a `/repo` mount makes pytest import the image's own backend copy.

  ```
  IMG=$(docker image inspect flask-app:latest --format '{{.Id}}')
  docker run --rm -v /home/ubuntu/github/jp5/main/triton-cleanup-wedge:/w/dda -w /w/dda \
    -e PYTHONPATH=/w/dda/src/backend:/w/dda/test/backend-test \
    -e LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64 \
    -e PYTHONDONTWRITEBYTECODE=1 -e HYPOTHESIS_STORAGE_DIRECTORY=/tmp/.hypothesis \
    "$IMG" bash -lc 'PY=$(for p in python3.11 python3.10; do command -v $p >/dev/null && $p -c "import pydantic" 2>/dev/null && { echo $p; break; }; done); \
      $PY -m pip install --no-cache-dir --quiet pytest sarge testfixtures hypothesis; \
      $PY -m pytest <one suite path> -q -p no:cacheprovider'
  ```

- **Python 3.8** (the JP5 host interpreter for the model-component scripts): a throwaway venv outside
  the repository, built from `/usr/bin/python3.8` (3.8.10), with `pytest==8.3.5 hypothesis==6.113.0
  requests==2.32.4 protobuf==5.29.6`. Run `test_model_locks.py`, `test_cleanup_release.py`,
  `test_startup_outcome_record.py`, `test_content_aware_publish.py`, `test_repo_entries.py` and
  `test_host_script_imports.py` with the host-suite form above.
- **Guard suite** (seconds; before every build):
  ```
  python3 -m pytest \
    test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py \
    test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py \
    -p no:cacheprovider --noconftest -q
  ```
- **Full preservation suite:** the in-image form with suite path `test/backend-test/security/preservation`
  and `--noconftest`.
- **Portal backend:** from `edge-cv-portal/backend`, under Python 3.11 with moto (the vendored shared
  layer needs Python 3.10 or later): `python -m pytest tests/<file> -q -p no:cacheprovider`.
- **Portal frontend:** `cd edge-cv-portal/frontend && npm ci && npx vitest run && npm run build`.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"], "description": "Documents: NITs, follow-up notes, the follow-up spec; two docs-only commits." },
    { "wave": 2, "tasks": ["2"], "description": "Baseline from the base commit, and the exploration tests (they must FAIL on unfixed code)." },
    { "wave": 3, "tasks": ["3"], "description": "F1, the per-run gate, F3 and F5b, with the host-shared modules." },
    { "wave": 4, "tasks": ["4", "5", "6"], "description": "F4 (needs task 3); F2 and F5a; F6 Portal. Executed in this order." },
    { "wave": 5, "tasks": ["7", "8"], "description": "Pre-build gate, then the JP5 build and publish." },
    { "wave": 6, "tasks": ["9", "10", "11", "12"], "description": "MIC-730: G1, deploy, R1-R3, self-heal, restore." },
    { "wave": 7, "tasks": ["13"], "description": "Device-side commits and verification notes." },
    { "wave": 8, "tasks": ["14"], "description": "JP6 and JP7 builds; G3 for other devices." },
    { "wave": 9, "tasks": ["15"], "description": "After the code review approves: final report and teardown." }
  ]
}
```

## Tasks

- [ ] 1. Spec documents (docs only)
  - [ ] 1.1 Apply the nine NITs of the approved design review to `bugfix.md` and `design.md`.
    - Finding 1: D2 "Imports" and the Error Handling row. A failed `repo_entries` import skips the
      sweep and uses the unstamped trash name. In the fallback path, "terminal" means neither `LOADING`
      nor `UNLOADING`. Add the `test_cleanup_release.py` case to the Testing Strategy.
    - Finding 2: D1 "Residual: untracked loads" and D9 step 5. Name digital-input runs as a source of
      untracked loads.
    - Finding 3: D7 API. A refused ticket is neither registered nor bound, and `finish_run`, `bound`
      and `set_details` treat it as a no-op. Add the test case.
    - Finding 4: bugfix.md 2.6 and Property 4. Qualify them with the 10 s interrupt budget.
    - Finding 5: D7 "After the restart" and the Operator notes. Add the digital-input sentence.
    - Finding 6: D4. Replace the `UNKNOWN_LOAD_GRACE_S` rationale.
    - Finding 7: define `self_heal_document(view, records_dir=None)` and
      `InterruptReport(marked, failed, skipped)`.
    - Finding 8: remove the references to documents outside the repository, in iteration-4 response 9
      and in On-Device Verification, with the replacement wording the review gives.
    - Finding 9: cap records on write (attempt `models` at 10 and `evidence` at 5; a load outcome's
      `reason` and `failureReason` at 2,000 characters; prune attempts older than 24 h until the record
      is at most 48 KiB). Add one test per record.
    - Add a revision-history entry to both documents.
    - Files: `.kiro/specs/triton-model-cleanup-wedge/bugfix.md`, `.kiro/specs/triton-model-cleanup-wedge/design.md`
    - Verify: re-read each finding against its edit; the hygiene check over both files reports nothing.
  - [ ] 1.2 Add the D12 notes: the "Follow-up (2026-10-15)" section of
    `triton-model-load-race/bugfix.md`, and the revision-history bullet of
    `cold-model-first-run-failure/bugfix.md`, both verbatim from D12.
    - Files: `.kiro/specs/triton-model-load-race/bugfix.md`, `.kiro/specs/cold-model-first-run-failure/bugfix.md`
    - Verify: `git diff` of each file shows only the appended text.
  - [ ] 1.3 Write the follow-up spec `localserver-config-live-update`. It is documents only and is
    **not implemented in this run**.
    - **requirements.md** (EARS, in the repo's requirements format):
      - an inventory of every LocalServer configuration key, classified as read live or startup-only,
        with its reader. It covers at least `workflowTuning` (`workflow_engine/runtime.py`
        `_configure_sample_export`, around line 280) and the keys served from the per-process cache of
        `DefectDetectionConfig.get_local_server_config()` (`StationName`, `SoftwareVersion`,
        `CameraDiscoveryIntervalSeconds` through `utils/server_setup.py` and
        `camera_discovery/discovery.py`);
      - each startup-only key takes effect without relying on a CLI bounce. Either a configuration-update
        subscription applies it, or the backend restarts explicitly with a logged reason, rate-limited
        and never silent;
      - read failures and invalid values keep the previous configuration, with a WARNING;
      - preservation of the live keys (`LocalLoginEnabled`, `ModelLoadSelfHealEnabled`, `accessControl`);
      - no restart when nothing changed;
      - on-device verification.
    - **design.md:**
      - Greengrass IPC `SubscribeToConfigurationUpdate` on the component's own configuration, and
        whether a component reading its own configuration needs an authorization policy (cite AWS
        documentation);
      - per-key appliers, cache invalidation, and the explicit-restart fallback;
      - how the interim operator procedure in this bugfix's Operator notes is retired.
    - **tasks.md:** the task list, plus a `.config.kiro` with a new spec id,
      `"workflowType": "requirements-first"` and `"specType": "feature"`.
    - Files: `.kiro/specs/localserver-config-live-update/{requirements.md,design.md,tasks.md,.config.kiro}`
    - Verify: every key the inventory names has a file reference that `grep -n` confirms; the hygiene
      check passes.
  - [ ] 1.4 Make two docs-only commits.
    - **Commit A:** `docs(spec): add the triton-model-cleanup-wedge bugfix spec`, with `bugfix.md`,
      `design.md`, `tasks.md` and `.config.kiro` of this spec plus the two task 1.2 files.
    - **Commit B:** `docs(spec): add the localserver-config-live-update spec`, with the four task 1.3
      files only.
    - Files: as listed.
    - Verify: `git show --stat HEAD~1 HEAD` lists exactly those files; the hygiene check passes for
      each commit and message; `git status --short` shows no staged leftovers.

- [ ] 2. Baseline and exploration (before any code change)
  - [ ] 2.1 Record the preservation baseline from the base commit.
    - `git archive cc35b92a` into a scratch directory.
    - In that tree, run every suite named in design.md "Preservation checking", host and in-image.
    - Record per suite: the exact command that runs it, pass/skip/fail counts, failing ids and the
      image id. Known pre-existing failures (for example the two `test_lfv_to_triton.py` cases) are
      compared by id later.
    - Files: none in the repository (evidence directory only).
    - Verify: each suite has a recorded command, counts and a failing-id list.
    - _Requirements: 3.1-3.14_
  - [ ] 2.2 Write the exploration tests. Each **must FAIL on unfixed code**; record the counterexample
    or failure for each.
    - Property 1: `test_exploration_cleanup_under_load.py`
    - Property 4: `test_exploration_run_gate_kick.py`
    - Property 9: `test_exploration_republish_unchanged.py`
    - Property 5: `test_exploration_silent_startup.py`
    - Files: `test/backend-test/triton_cleanup_wedge/{__init__.py,fakes.py,test_exploration_*.py}`
    - Verify: the host-suite run of the four files FAILS, for the reasons the design's exploration
      table gives.
    - _Requirements: 1.1, 1.3, 1.4, 1.9, 2.1, 2.3, 2.7, 2.8_

- [ ] 3. F1, the per-run gate, F3 and F5b
  - [ ] 3.1 Add the host-shared modules. They use the standard library only, and must run on
    Python 3.8.
    - `model_locks.py` (D1 lock contract).
    - `repo_entries.py` (D4):
      - `inspect_entry`/`EntryInspection`, with `ModelGate._file_state`'s body moved verbatim;
      - `boot_stamp`, `staging_prefix`, `trash_name`, `sweep_stale_hidden_dirs`.
    - `model_status_records.py` (D5, with the finding 9 write caps).
    - Files: `src/backend/dda_triton/{model_locks.py,repo_entries.py,model_status_records.py}`;
      tests `test_model_locks.py` (real `flock` across processes), `test_repo_entries.py` (Hypothesis
      oracle against the pre-change `_file_state` body), record cases in `test_startup_outcome_record.py`.
    - Verify: host-suite run of the three test files passes; the Python 3.8 run of them passes.
    - _Requirements: 2.1, 2.2, 2.3, 2.7, 3.14_
  - [ ] 3.2 Add the load tracker and the single load path (D1).
    - `load_tracker.py`: `request_load`, `requested_at`, `inflight`, `ensure_tracker_thread`,
      `ModelReleaseInProgress`. Passes are contained.
    - `TritonEdgeClient`:
      - `self.model_dir`;
      - `_list_models_raw(quiet=False)`;
      - `index_states()`;
      - `start_triton_model` goes through `request_load` with `lock_dir_for(self.model_dir)`.
    - `feature_configs_utils`:
      - `triton_repo_has_models` and `get_features_triton` skip names that start with `.`;
      - `start_model_triton` maps `ModelReleaseInProgress` to 409 (the catalogue's detail text) and an
        invalid lock key to 400.
    - Files: `src/backend/dda_triton/{load_tracker.py,triton_edge_client.py}`,
      `src/backend/utils/feature_configs_utils.py`; test `test_load_tracker.py` (every case in the
      Testing Strategy, including 16 threads, the generation race and the 120 s unconfirmed release).
    - Verify: host-suite run of `test_load_tracker.py` passes; in-image
      `test/backend-test/utils/test_feature_configs_utils.py` passes at its baseline.
    - _Requirements: 2.2, 3.4, 3.10; Properties 2, 3_
  - [ ] 3.3 Change the gates (D1, D4).
    - `ModelGate._file_state` delegates to `repo_entries`, reading `QUIET_PERIOD_S` at call time.
    - `_load` returns False on `ModelReleaseInProgress`, and `_maybe_load_unknown` then discards the
      model from `_load_requested`.
    - `ensure_model_ready`'s `UNKNOWN` branch inspects the entry before its first kick. A refused kick
      sets `kicked` back to False and logs one INFO line. The timeout message names the blocker, and
      adds the remedy for a missing or unreadable path. The inspector is injectable.
    - The `LOADING` stall branch and `gate_wait` come in task 4.6.
    - Files: `src/backend/workflow_engine/model_gate.py`, `src/backend/dda_triton/model_readiness.py`;
      tests `test_run_gate_stable_entries.py` (gate and ModelGate cases), updated `UNKNOWN` cases in
      `test/backend-test/dda_triton/test_model_readiness.py`.
    - Verify: `test_exploration_run_gate_kick.py` now passes;
      `workflow_engine/test_workflow_continuous_model_gate.py` and the cold-model suites pass unchanged.
    - _Requirements: 2.2, 2.3, 3.9; Property 4_
  - [ ] 3.4 Rewrite the cleanup around `release_model` (D2, finding 1).
    - It keeps the CLI and the existing log lines.
    - Every HTTP call has a 10 s timeout.
    - The exclusive lock, the unload of a READY model, rename-to-trash under the lock, and the delete
      outside it.
    - A bounded skip with a WARNING.
    - Optional imports are inside `try/except Exception`.
    - Only argument errors exit 1.
    - Files: `src/backend/dda_triton/convert_model_cleanup.py`; test `test_cleanup_release.py`.
    - Verify: `test_exploration_cleanup_under_load.py` now passes; `test_cleanup_release.py` passes on
      the host and under Python 3.8.
    - _Requirements: 2.1, 2.2, 3.1, 3.14; Property 1_
  - [ ] 3.5 Change the convertor (D5, D9).
    - Install the SIGTERM handler first.
    - Validate `--model_name`.
    - `start_model_detailed` with `StartOutcome`, the `observer` on `_wait_for_model_ready`, and
      `start_model` kept as a wrapper.
    - The outcome record and the pb_stub capture (argument-list subprocess calls only, with the stated
      caps), and the log levels.
    - The three builders.
    - `_stage_model_dir`, `_swap_model_dir`, `_publish_model_dirs` and `_tree_manifest`, with one
      exclusive hold and a 300 s wait.
    - `_atomic_publish_model_dir` kept as a wrapper.
    - Files: `src/backend/dda_triton/model_convertor.py`; tests `test_startup_outcome_record.py`,
      `test_content_aware_publish.py` (including the SIGTERM subprocess case), `test_host_script_imports.py`.
    - Verify:
      - `test_exploration_republish_unchanged.py` and `test_exploration_silent_startup.py` now pass;
      - `dda_triton/test_model_convertor_atomicity_{bug,fix}.py` and `test_start_model_retry.py` pass
        unchanged;
      - the Python 3.8 run passes.
    - _Requirements: 2.7, 2.8, 3.2, 3.3, 3.14; Properties 5, 9_
  - [ ] 3.6 Seed and update the harnesses.
    - Add the three host-shared modules to `files_to_copy_to_dda_triton`.
    - Update `EXPECTED_DDA_TRITON_COPIES` and the three `SRC_AUX_DDA_TRITON` lists.
    - `test_eml_triton.py`, `test_triton_edge_client.py` and any other `dda_triton` test that publishes
      inside the test tree also remove the `locks/` directory they cause.
    - Files: `src/backend/dda_triton/triton_setup.py`,
      `test/backend-test/dda_triton/{test_triton_setup_preservation.py,test_triton_inference_runtimes_bug.py,test_triton_inference_runtimes_fix.py,test_triton_inference_runtimes_preservation.py,test_eml_triton.py,test_triton_edge_client.py}`
    - Verify: in-image `test/backend-test/dda_triton` passes at its baseline; no `locks/` directory is
      left in the tree.
    - _Requirements: 3.2_
  - [ ] 3.7 Checkpoint.
    - Every task-3 test passes. The task 2.2 exploration tests pass.
    - The task 2.1 suites show no new failing id (`comm -13` on the sorted id lists), and the Python 3.8
      run passes.
    - Save `git diff` plus the new files as a snapshot in the evidence directory. No commit.
    - Verify: the comparison output is empty.

- [ ] 4. F4: detection, self-heal on by default, run activity, surfaces
  - [ ] 4.1 Exploration (Property 6): `test_exploration_wedge_report.py` **must FAIL on unfixed code**,
    because `loadHealth` is absent.
    - Files: `test/backend-test/triton_cleanup_wedge/test_exploration_wedge_report.py`
    - Verify: the host run fails for that reason.
    - _Requirements: 2.5_
  - [ ] 4.2 Add `run_activity` (D7).
    - The IDLE/QUIESCING/RESTARTING table, the `QUIESCE_MAX_S` expiry, and the `ContextVar` ticket with
      `bind` and `bound`.
    - Refused tickets are no-ops (finding 3).
    - `gate_wait`, `after_gate`, `commit_restart` (three results), and `interrupt_all` with
      `InterruptReport` (finding 7).
    - `start_run` never waits.
    - Files: `src/backend/utils/run_activity.py`; test `test_run_activity.py`.
    - Verify: `test_run_activity.py` passes (Property 8 cases, the 100 ms run stream, no hold).
    - _Requirements: 2.6, 3.5; Property 8_
  - [ ] 4.3 Wire the registration points (D7).
    - `PipelineExecutor.execute`: `start_run` after the pending check, with `on_interrupt` for
      triggered and manual runs; `set_details` twice; `after_gate`; `finish_run` in the `finally`.
    - The classic run: 503 on a refusal at registration and after the gate; `finish_run` in a `finally`.
    - `gst_pipeline.run_pipeline`.
    - The capture task.
    - The digital-input trigger, with `bind=False` before `_get_image_source`, and `bound` inside
      `execute_workflow`.
    - `ContinuousRunner.step()` skips its tick while quiescing.
    - Rebaseline the `pipeline_executor.py` hash in
      `test/backend-test/csi_nvargus_optional/goldens/backend_csi_consumers_sha256.json`.
    - Files: `src/backend/workflow_engine/{pipeline_executor.py,continuous_runner.py}`,
      `src/backend/endpoints/workflow.py`, `src/backend/gstreamer/gst_pipeline.py`,
      `src/backend/utils/{capture_task_manager.py,digital_input_thread_manager.py}`, that golden; tests
      `test_interrupted_runs.py`, `test_run_registration_points.py`, `test_dio_run_registration.py`.
    - Verify:
      - host runs of the first two pass;
      - in-image `test_dio_run_registration.py` and `test/backend-test/utils/test_digital_input_thread.py`
        pass;
      - `workflow_engine` and `csi_nvargus_optional` show no new failing id;
      - `test_cold_model_classic_path.py` passes.
    - _Requirements: 2.6, 3.5; Properties 8, 15_
  - [ ] 4.4 Add watchdog detection (D6).
    - The tick, steps 1-9. The `since(m)` sightings.
    - The `/proc` stub-CPU sampler, and the Jetson GPU devfreq sampler.
    - The pure `evaluate_wedge`.
    - The immutable `WatchdogView`, and `WATCHDOG_STALE_S`.
    - `stall_for` and `load_health`.
    - Contained ticks.
    - Files: `src/backend/dda_triton/load_watchdog.py`; test `test_load_watchdog_detection.py`.
    - Verify: it passes, including the Property 6 Hypothesis case and the wall-clock-jump case.
    - _Requirements: 2.5, 3.10, 3.11; Property 6_
  - [ ] 4.5 Add self-heal (D7).
    - The switch reader: one module-level reader, the `StreamShadowName` success rule, the 60 s retry
      delay, the kill file.
    - `parse_self_heal_switch` and `decide_self_heal` (rules 1-9).
    - The action, steps 1-11: the guarded block of steps 3-5 with the re-check before the commit; the
      `try` of steps 6-8 with `abort_restart()`; the backstop; the interrupt step; the vLLM refresh.
    - The record, with the finding 9 caps and pruning, and its close-out after a restart.
    - Files: `src/backend/dda_triton/load_watchdog.py`; test `test_self_heal_policy.py`.
    - Verify: it passes, including the Property 7 Hypothesis timeline and every "way out before the
      commit" case.
    - _Requirements: 2.6; Property 7_
  - [ ] 4.6 Wire the surfaces (D8, D4).
    - `model_annotations`, `build_model_status_snapshot`, `self_heal_document`, the size ladder and the
      size-rejection retirement.
    - `get_features_triton` merges the annotations and appends record-backed `UNKNOWN` entries.
    - `__gpu_status_snapshot` delegates; the empty branch reports the empty snapshot.
    - `report()` returns `scheduled`, `unchanged` or `dropped`, and sets the size-rejection flag.
    - `ensure_model_ready`'s `LOADING` stall branch, `gate_wait`, and its refusal.
    - The contained `start_model_load_watchdog()` after `health.set_vllm_server` in `app.py`, plus the
      `app.py` hash and the `note` in `iam_out_of_scope_baseline.json`.
    - Files: `src/backend/dda_triton/{load_watchdog.py,model_readiness.py}`,
      `src/backend/utils/{feature_configs_utils.py,model_status_shadow.py}`,
      `src/backend/endpoints/feature_config.py`, `src/backend/app.py`,
      `test/backend-test/security/baselines/iam_out_of_scope_baseline.json`; test
      `test_self_heal_surfaces.py`.
    - Verify:
      - `test_exploration_wedge_report.py` now passes, and `test_self_heal_surfaces.py` passes;
      - in-image `api-endpoints/test_feature_configuration_api.py`,
        `utils/test_feature_configs_vllm_merge.py` and `model_gpu_fallback_visibility` show no new
        failing id;
      - the guard suite is green;
      - `test_py310_compat.py` passes.
    - _Requirements: 2.5, 2.6, 2.7, 3.4, 3.8, 3.11; Properties 12, 16_
  - [ ] 4.7 Checkpoint. As in 3.7, over every suite, and the snapshot is updated. No commit.
    - Verify: no new failing id against the task 2.1 baseline.

- [ ] 5. F2 and F5a (build inputs)
  - [ ] 5.1 Exploration (Properties 10, 11): `test_exploration_python_backend_patch.py` and
    `test_exploration_cli_dependency.py` **must FAIL on unfixed code**.
    - Files: `test/backend-test/triton_cleanup_wedge/test_exploration_{python_backend_patch,cli_dependency}.py`
    - Verify: both fail, because the patch is absent and the dependency type is missing (HARD).
    - _Requirements: 2.4, 2.9_
  - [ ] 5.2 Generate the patch and edit the Dockerfiles (D3).
    - Regenerate the patch with D3's full-clone recipe: 2 files, 10 hunks, +53/-13.
    - `git apply --check` it on a shallow r24.04 clone with an absolute patch path.
    - Insert only new lines in the three Dockerfiles: the `COPY`, the pre-clone, the absolute-path
      `apply`, the source `grep`, `--no-force-clone`, and the binary sentinel.
    - `Dockerfile.jp7` is untouched.
    - Files: `src/edgemlsdk/patches/edgeml-triton-python-backend.diff`,
      `src/edgemlsdk/{Dockerfile.jp5,Dockerfile.jp6,Dockerfile}`
    - Verify: `test_exploration_python_backend_patch.py` passes; `git diff` of each Dockerfile shows
      added lines only.
    - _Requirements: 2.4, 3.7_
  - [ ] 5.3 Rebaseline the Dockerfile pins, as design.md "Tests and baselines to update" lists.
    - Security: the two masked edgemlsdk baselines (delete each, then capture with
      `test_preservation_docker_masked_bytes.py`), and the `src/edgemlsdk/Dockerfile` hash in
      `docker_baseline_out_of_scope.json`.
    - `edgemlsdk_pythondev`: three baselines. `edgemlsdk_cmake`: three baselines. Delete, capture, then
      re-run to assert.
    - Files: `test/backend-test/security/baselines/{docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt,docker_baseline_edgemlsdk_Dockerfile.jp6_masked.txt,docker_baseline_out_of_scope.json}`,
      `test/backend-test/edgemlsdk_pythondev/baselines/{edgemlsdk_Dockerfile.jp5.sha256.txt,edgemlsdk_Dockerfile.jp6.sha256.txt,edgemlsdk_Dockerfile_pythondev_masked.txt}`,
      `test/backend-test/edgemlsdk_cmake/baselines/{edgemlsdk_Dockerfile.jp6.sha256.txt,edgemlsdk_Dockerfile.jp5_cmake_masked.txt,edgemlsdk_Dockerfile_cmake_masked.txt}`
    - Verify:
      - the `git diff` of every masked baseline shows only the D3 lines;
      - each pinned hash equals `sha256sum` of its Dockerfile;
      - `edgemlsdk_pythondev/` and `edgemlsdk_cmake/` pass.
    - _Requirements: 2.4_
  - [ ] 5.4 Make the CLI dependency SOFT (D10).
    - All seven LocalServer recipes get `DependencyType: SOFT` with its comment, and
      `ModelLoadSelfHealEnabled: "true"` with its comment. `recipe.yaml` stays byte-identical to
      `recipe-arm64-jp6.yaml`.
    - The three ECR rewriters each get the one normalization line.
    - Rebaseline `publish-ecr-only.sh.sha256.txt`.
    - Edit the five golden families surgically (no `--regenerate`).
    - Files: `recipe.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64-jp6.yaml`, `recipe-arm64-jp7.yaml`,
      `recipe-amd64.yaml`, `recipe-amd64-nvidia.yaml`, `recipe-arm64.yaml`,
      `gdk-component-build-and-publish.sh`, `portal-build.sh`, `publish-ecr-only.sh`,
      `test/backend-test/build_save_pkgs/baselines/publish-ecr-only.sh.sha256.txt`,
      `test/backend-test/csi_nvargus_optional/goldens/recipe_sha256.json`,
      `test/backend-test/output_bindings_fixes/goldens/recipe_{arm64_jp6,arm64_jp5,arm64,amd64}_full.golden.json`,
      `test/backend-test/deploy_reliability/goldens/recipe_{amd64,arm64,arm64_jp5,arm64_jp6}_{structure,defect_e_baseline}.golden.json`,
      `test/backend-test/vllm_hf_cache/goldens/code_baseline.sha256.golden.json`; test `test_recipes_soft_cli.py`.
    - Verify:
      - `test_exploration_cli_dependency.py` and `test_recipes_soft_cli.py` pass;
      - `deploy_reliability/`, `output_bindings_fixes/`, `csi_nvargus_optional/`, `build_save_pkgs/` and
        `vllm_hf_cache/` show no new failing id;
      - every golden's `git diff` shows only the two keys or the recipe hashes.
    - _Requirements: 2.9, 3.12, 3.13; Property 10_
  - [ ] 5.5 Checkpoint.
    - The guard suite is green. The full preservation suite in the image is green.
    - No new failing id on any suite.
    - The snapshot is updated. No commit.
    - Verify: as stated.

- [ ] 6. F6: Portal deployment readiness warning
  - [ ] 6.1 Give `get_usecase_client` and `create_boto3_client` an optional `config=None` keyword
    (D11). Without it, the `boto3.client` calls are byte-for-byte today's.
    - Files: `edge-cv-portal/backend/layers/shared/python/shared_utils.py`; test
      `edge-cv-portal/backend/tests/test_shared_utils_client_config.py`.
    - Verify: the Portal backend run of that file passes.
    - _Requirements: 3.8_
  - [ ] 6.2 Add `_read_model_status_shadow` and `_model_readiness_summary` (D11).
    - Only for the latest deployment of its target.
    - At most 20 devices.
    - `SHADOW_READ_CONFIG` (2 s connect and read timeouts, `total_max_attempts` 1), and the 5 s budget.
    - The additive `model_readiness` field, wrapped in `try/except`.
    - Files: `edge-cv-portal/backend/functions/deployments.py`; test
      `edge-cv-portal/backend/tests/test_deployment_model_readiness.py`.
    - Verify: that file passes; `tests/test_deployment_*.py` show no new failing id against task 2.1.
    - _Requirements: 2.10, 3.8; Property 14_
  - [ ] 6.3 Add the optional `model_readiness` field and the Cloudscape warning `Alert` ("current
    state" wording, the shadow time per device, one line per model).
    - Files: `edge-cv-portal/frontend/src/pages/DeploymentDetail.tsx`,
      `edge-cv-portal/frontend/src/pages/DeploymentDetail.modelReadiness.test.tsx`
    - Verify: `npx vitest run src/pages/DeploymentDetail.modelReadiness.test.tsx` passes; the full
      vitest run shows no new failure; `npm run build` exits 0.
    - _Requirements: 2.10_
  - [ ] 6.4 Commit the Portal code: `feat(portal): show models that devices report as not ready on the
    latest deployment`. The message says it was verified by host tests only, and that the Portal is not
    deployed (G2).
    - Files: the six files of tasks 6.1-6.3 (three source files, three test files). Not `tasks.md`.
    - Verify: the hygiene check and the pin sweep pass; `git show --stat HEAD` lists exactly those files.

- [ ] 7. Pre-build gate
  - [ ] 7.1 Run every suite against the task 2.1 baseline: host, in-image, Python 3.8, and
    `test/python_version_audit.py`. No new failing id.
    - Verify: the `comm -13` comparison output is empty.
  - [ ] 7.2 Replay the in-image build gate. Run the body of `build-custom.sh`'s backend-test
    `docker run … -c '…'` block against this worktree with the pinned image, exactly as the build does:
    `repo_audit.py`, the security exploration suites, the full preservation suite and the other audits.
    - Verify: the replay exits 0, and every gate in it prints its "passed" line.
  - [ ] 7.3 Apply builds.md's checklist.
    - Every preservation-tracked file this branch changes is rebaselined.
    - The guard suite is green.
    - `edge-cv-portal/infrastructure/cdk.out` is moved aside if present.
    - No Portal deploy is running or planned during the build.
    - Verify: the guard suite output; `ls edge-cv-portal/infrastructure/cdk.out` is absent.
  - [ ] 7.4 Record the exact source the build will use: `git rev-parse HEAD`, `git diff HEAD --binary`,
    and the untracked source files with their sha256. Task 13 proves the commits equal this source.
    - Verify: the snapshot files exist, with recorded hashes.

- [ ] 8. JP5 build and publish
  - [ ] 8.1 Build JP5.
    - Wait until no build runs (the pgrep checks, re-checked every few minutes).
    - `cp recipe-arm64-jp5.yaml recipe.yaml`.
    - `TARGETS="5" ./run_jp_builds.sh`. It swaps `gdk-config.json`, runs `gdk component build` and
      restores it.
    - Move `.gdk_build_jp5.log` into the evidence directory.
    - Record the `flask-app:latest` and `react-webapp:latest` image ids the moment the build ends.
    - Verify: exit 0; the log shows the backend unit tests and every security gate passed; the
      python_backend layer ran the clone, apply and both `grep` sentinels (any failure fails the build).
  - [ ] 8.2 Publish immediately. Re-run the pgrep checks, and confirm both image ids still equal those
    recorded in 8.1. If either check fails, do not publish and do not retag: pause and report. Then run
    `./publish-ecr-only.sh aws.edgeml.dda.LocalServer.arm64JP5`, with its output in its own log.
    - Verify:
      - the published version is recorded;
      - `aws greengrassv2 get-component` for that version returns a recipe whose `aws.greengrass.Cli`
        dependency is `SOFT` and whose `DefaultConfiguration` carries `ModelLoadSelfHealEnabled: "true"`.
  - [ ] 8.3 Restore with `git checkout -- gdk-config.json recipe.yaml`.
    - Verify: `git status --short` shows only this branch's intended changes; `cmp recipe.yaml
      recipe-arm64-jp6.yaml` exits 0.

- [ ] 9. MIC-730: coordination and the fixed LocalServer
  - [ ] 9.1 Read-only inspection.
    - Open your own SSH control master, with the device's host key verified against the recorded
      known_hosts (`StrictHostKeyChecking=yes`, never `UserKnownHostsFile=/dev/null`). A mismatch
      means stop and report. Use your own port forward.
    - Capture the pre-state:
      - the deployment revision and its JSON;
      - component versions;
      - model states (`/feature-configurations`);
      - the `dda-model-status` shadow;
      - pinned media;
      - deployed workflows;
      - demo processes in the backend container;
      - `docker inspect` start time and restart count;
      - one minute of the GPU load node.
    - Verify: every item is saved in the evidence directory.
  - [ ] 9.2 **OWNER GATE G1.** Pause with a warning that names:
    - every change tasks 9-12 make;
    - the deliberate backend restarts;
    - that self-heal is armed by default once the fixed LocalServer runs;
    - the optional items: a scratch workflow run on the probe (self-heal step 1), and step 5's
      continuous workflow.
    Wait for the go, which confirms the demo work on the device is finished or stopped.
  - [ ] 9.3 On the go, stop the demo's host trigger loop with the demo's own stop command, and confirm
    no run is in flight.
    - Verify: the trigger loop is gone; the device run list shows nothing `pending` or `running`.
  - [ ] 9.4 Deploy the fixed LocalServer.
    - Copy the current deployment and change only the LocalServer version. No model component is added
      or updated, and workflows and triggers are idle (design "Rollout caveat").
    - Wait for COMPLETED.
    - Verify:
      - the backend runs the new image;
      - the host copies of `model_convertor.py`, `convert_model_cleanup.py` and the three
        `/aws_dda/dda_triton/` modules match the worktree by sha256;
      - the watchdog start line reports self-heal on, and names the GPU load node;
      - the shadow carries `modelReadiness`, and `selfHeal` with `enabled: true`;
      - the backend stays healthy for at least 15 min, with no restart.
    - _Requirements: 2.11, 3.11_

- [ ] 10. Reproductions on the fixed build (design "On-Device Verification")
  - [ ] 10.1 **R1.**
    - Restart the backend, and wait until it is healthy.
    - Request `/start` for the yolo-test model, then run its cleanup about 1 s later.
    - Recover with a component restart.
    - Verify:
      - the cleanup logs "a load is in flight", then "load finished (READY); unloading", "unloaded" and
        three `Cleaned directory` lines;
      - the backend log has no pb_stub missing-file line;
      - the other models reach READY in normal time.
    - _Requirements: 2.1, 2.2, 2.11; Property 1_
  - [ ] 10.2 **R3.**
    - Copy the yolo-test entries under a scratch model name, renaming the names inside each
      `config.pbtxt`.
    - Remove `base_<scratch>/<v>/model.py`, then request `/start`.
    - Restore the file, request `/start` again, then remove the scratch entries.
    - Verify:
      - `UNAVAILABLE` with a `failureReason` within 30 s (typically 10-11 s);
      - sibling loads are unaffected;
      - with the file restored, `/start` gives READY.
    - _Requirements: 2.4, 2.11; Property 2_
  - [ ] 10.3 **R2.**
    - Deploy a revision that adds one unused, published JP5 model component, and merges
      `{"ModelLoadSelfHealEnabled": "true"}` into LocalServer, so the shape matches the incident's.
    - Trigger one PPE demo workflow run.
    - Roll back to the 9.4 revision, with a `reset` of that key. The rollback runs the guarded cleanup
      on the removed component.
    - Verify:
      - the Greengrass log has no `Restarting service because dependency aws.greengrass.Cli was in a
        bad state` line for LocalServer;
      - no deletion happens under `LOADING`;
      - the deployment completes with every model READY;
      - the run completes;
      - the rollback removes the entries;
      - the backend stays healthy for at least 15 min after each of the two deployments.
    - _Requirements: 2.9, 2.11, 3.1_

- [ ] 11. Self-heal and F3 on the device (design "On-Device Verification", steps 0-6)
  - [ ] 11.1 **OWNER GATE G2**, only if the F6 live check is wanted.
    - Pause with a warning before any Portal deploy. The warning says that the deploy would publish this
      branch's Portal, and could replace newer live Portal code.
    - Recommend deferring the live F6 check to the trunk merge.
    - Without a go, F6 stays verified by host tests only, and is recorded as pending.
    - With a go: deploy only while no build runs, then move `cdk.out` aside again.
  - [ ] 11.2 Run steps 0-4 and 6 exactly as the design states, using the scratch probe
    `model-selfheal-probe`, whose `initialize()` blocks without using CPU.
    - Every seeding and re-wedge happens while the kill file exists. Step 5 runs only with its go.
    - Run the F3 check between steps 3 and 4: a hand-run Startup of a scratch component.
    - If G2 was approved, also do this between steps 3 and 4: request `/start` for a deployed model,
      so it queues `LOADING` behind the probe, and confirm the Portal deployment view lists it.
    - Verify, with the exact log lines saved:
      - the "is stalled" WARNING with `[stall]` evidence;
      - one "quiescing" line and one `SELF-HEAL:` ERROR line (models, how long LOADING, evidence,
        attempt 1 of 3);
      - the container restart, and "backend restarted after self-heal attempt 1";
      - `lastSelfHeal` and `selfHeal.last` visible afterwards in `/feature-configurations`, in the
        shadow and in the Portal device API;
      - the opt-out reported as `disabled` with `disabledBy: "config"` and no restart;
      - the kill file reported as `disabledBy: "file"`;
      - `cooldown` with the "SELF-HEAL waiting" line;
      - `exhausted` with the "SELF-HEAL EXHAUSTED" line;
      - F3: the WARNING Startup line, the record in `/feature-configurations` and the shadow, and exit
        0;
      - step 6 restores `enabled: true`, `disabledBy: null` and nothing `LOADING`.
    - _Requirements: 2.5, 2.6, 2.7, 2.10, 2.11; Properties 6, 7, 12_

- [ ] 12. Restore and sustained health
  - Remove every scratch model, scratch component and seeded record. No kill file and no switch
    override is left.
  - Restart the demo's clipper and live view with the demo's own start command.
  - Confirm the PPE demo workflow produces detections on the fixed LocalServer.
  - Capture the post-state against the 9.1 pre-state.
  - Verify: the backend is healthy for at least 15 min with no unexpected restart; every difference
    from the pre-state is intended (the LocalServer version), or is reported plainly.
  - _Requirements: 2.11_

- [ ] 13. Device-side commits and verification notes
  - [ ] 13.1 Prove that what you commit is what was built and verified.
    - Verify: `git diff` between the task 7.4 snapshot and the working tree, for every file under
      `src/`, the recipes and the rewriters, is empty. Any difference needs a rebuild and a targeted
      re-verification first.
  - [ ] 13.2 Make three code commits, in this order. Each passes the pin sweep and the hygiene check.
    Check each commit's consistency on a `git archive` of it, with the host suites it touches.
    - **C1** `fix(triton): guard model-repository removals with a per-model load lock, record Startup outcomes, publish only changed entries, and detect and self-heal load wedges`:
      - everything under `src/backend/`;
      - `test/backend-test/triton_cleanup_wedge/` without the two task 5.1 files and
        `test_recipes_soft_cli.py`;
      - every changed file under `test/backend-test/dda_triton/` (tasks 3.3 and 3.6);
      - the CSI consumer golden and the IAM out-of-scope baseline.
    - **C2** `fix(edgemlsdk): bound the python_backend stub start on the Triton 2.45.0 builds`: the
      patch, the three Dockerfiles, the task 5.3 baselines, and
      `test_exploration_python_backend_patch.py`.
    - **C3** `fix(recipes): make LocalServer's Greengrass CLI dependency SOFT and ship ModelLoadSelfHealEnabled`:
      the seven recipes, the three rewriters, the task 5.4 pins and goldens,
      `test_exploration_cli_dependency.py` and `test_recipes_soft_cli.py`.
    - Each message states: verified on the MIC-730 (JP5) with R1-R3 and the self-heal scenario, and
      by the named suites. JP6, JP7 and x86 device verification are pending.
    - Verify: `git log --stat` shows exactly those file sets; `git status --short` shows no
      unintended tracked change.
  - [ ] 13.3 Write `verification-notes.md`. It holds the R1-R3, self-heal, F3 and F6 results, with
    log lines, versions and timings, and no identifiers. It also lists, as unverified items:
    - the x86 Dockerfile's binary sentinel path;
    - opt-out persistence across cloud deployments;
    - SIGTERM delivery to the convertor.
    Tick the completed tasks in this file. Commit C4,
    `docs(spec): record the triton-model-cleanup-wedge JP5 verification`.
    - Files: `.kiro/specs/triton-model-cleanup-wedge/{verification-notes.md,tasks.md}`
    - Verify: the hygiene check passes; `git show --stat HEAD` lists the two files.

- [ ] 14. JP6 and JP7 builds; other devices
  - [ ] 14.1 Build JP6, then JP7, each as in 8.1: one at a time, with the pre-build checks, its own
    log, and the restore. Build only, no publish.
    - Verify: each build exits 0, with every in-image gate passing. JP6 runs the patched python_backend
      layer.
  - [ ] 14.2 **OWNER GATE G3.** Pause with a warning that asks, per device, for the go:
    - a JP6 Jetson AGX Orin: R1 and R3;
    - a JP7 device: R1, as a non-regression check;
    - the x86 workstation: an x86 build on an x86_64 host, then R1 and R3. This host is aarch64 and
      cannot build x86.
    A go covers publishing that arch's version and deploying it there, with the same coordination,
    pre-state and restore as tasks 9-12. Without a go, the device is recorded as pending.
  - [ ] 14.3 Record the builds and any device results in `verification-notes.md`. Commit C5,
    `docs(spec): record the JP6 and JP7 builds`.
    - Verify: the hygiene check passes.

- [ ] 15. Final report and teardown (after the code review approves)
  - Write the final report:
    - files changed, with commit ids;
    - builds, versions and logs;
    - what was verified, on which device;
    - R1-R3 and self-heal results;
    - what is owner-gated or pending: push and trunk merge, JP6/JP7/x86 device verification, the
      Portal deploy, the x86 workstation;
    - follow-ups: `localserver-config-live-update`, F5c, H.
  - Tear down the temporary Portal principal, and run its verify step.
  - Close every SSH master and port forward this work opened.
  - Confirm nothing was pushed.
  - Verify: the principal's verify step shows the deletion; `git status -sb` shows the branch ahead of
    its base by local commits only.
