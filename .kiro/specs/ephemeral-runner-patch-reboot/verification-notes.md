# Verification Notes — ephemeral-runner-patch-reboot

Worktree branch `spec/ephemeral-runner-patch-reboot`, based on
`origin/integration/all-specs` @ `86ddf49`. Implementation iteration 1,
2026-10-07. No build host was touched; no component build was started.

## Commands

Portal build suites. The brief's command runs as root in the container,
which trips `test_run_as_ubuntu_unit.py`'s own "must not run as root"
assertion, so the command that worked adds `-u 1000:1000 -e HOME=/tmp`
(and, after the baseline, `-e PYTHONDONTWRITEBYTECODE=1`):

```
docker run --rm -u 1000:1000 -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -v <worktree>:/w/dda -w /w/dda -e HYPOTHESIS_PROFILE=ci \
  -e PYTHONPATH=src/backend:test/backend-test dda-portal-test:py311 \
  python -m pytest test/backend-test/portal_builds --noconftest -q \
  -p no:cacheprovider
```

Infrastructure (`edge-cv-portal/infrastructure`): `node_modules` symlinked
from the main clone (its `package-lock.json` is byte-identical), then
`npm run build` before `npx jest test/build-fleet-stack.test.ts` and
`npm test -- --ci`. The full-suite baseline ran on a clean
`git archive HEAD` copy with the same `node_modules`.

Security gates, from the worktree root, on the host:

```
python3 -m pytest \
  test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py \
  test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py \
  -p no:cacheprovider --noconftest -q
python3 -m pytest -q -p no:cacheprovider --noconftest \
  test/backend-test/security/preservation/test_preservation_iam_cdk_synth.py
```

## Results

| Suite | Baseline (untouched) | After the change |
|---|---|---|
| portal_builds, as root (brief's command) | 7 failed, 1221 passed, 1 skipped | not re-run as root |
| portal_builds, uid 1000 | 6 failed, 1222 passed, 1 skipped (3m00s) | 6 failed, 1263 passed, 1 skipped (3m09s) |
| `test_runner_settle_unit.py` (new) | — | 31 passed |
| jest `build-fleet-stack.test.ts` | 22 passed, 1 snapshot | 23 passed, 1 snapshot |
| jest full `npm test` | 28 suites, 305 passed | 28 suites, 306 passed |
| guard pair (builds.md step 5) | 4 passed, 3 skipped | 4 passed, 3 skipped |
| IAM CDK synth gate | 17 passed | 17 passed |
| IAM README prose gate | — | 3 passed |

The 6 failures are the same tests before and after, all pre-existing and
unrelated: 4 in `test_build_diagnostic_api.py` (the log-events projection
returns no events under this image's moto) and 2 parametrizations of
`test_ref_aware_bootstrap_property.py::TestPreambleIsTheOneSyncGenerator::test_preamble_then_agent_sync_leaves_the_same_head_as_agent_alone`
(`REPO_DIR: unbound variable` in the agent re-sync). The extra root-run
failure is `test_run_as_ubuntu_unit.py::...::test_unprivileged_execution_takes_the_direct_arm`,
which asserts it is not run as root. +41 tests: 31 unit, 5 tick
integration (2 P0-A, 3 P1), 4 gate/per-boot, 1 setup-script lock wait.

Baselines: no file this change edits is pinned under
`test/backend-test/security/baselines/` (only README prose mentions
`./setup-build-server.sh`). No pin or IAM gate tripped; nothing
owner-approved was edited.

## Shell checks

- `bash -n`: `setup-build-server.sh`, the generated runner user data (no
  ref, `main`, `feature/x`) and the fleet user data — all clean. Runner
  user data is 2.8 KB (EC2 limit 16 KB).
- shellcheck 0.10.0 (container `koalaman/shellcheck:v0.10.0`): runner user
  data 0 findings; `setup-build-server.sh` the same 4 findings before and
  after (SC2124, SC1091, SC2016 x2); fleet user data the same 1 finding
  before and after (SC2164 on `cd <repo_dir>`).

## Evidence for P0-B

- cloud-init module order, read from the `cloud-init 26.1-0ubuntu1~22.04.1`
  and `~24.04.1` packages (`apt-get download` in `ubuntu:22.04` /
  `ubuntu:24.04` containers; no build host read): `cloud_final_modules` =
  ... `scripts_vendor`, `scripts_per_once`, `scripts_per_boot`,
  `scripts_per_instance`, `scripts_user` ...; `cc_scripts_per_boot`
  frequency `PER_ALWAYS`; `cc_scripts_user` frequency `PER_INSTANCE`;
  `helpers.FileSemaphores._acquire` writes the semaphore before the module
  runs, so a reboot during `scripts_user` never re-runs the user data.
- Re-run idempotency, executed in a sandbox
  (`TestPerBootReentryIsIdempotent`): a completed bootstrap re-run from the
  per-boot copy and from the user data is a no-op (no apt-get or setup
  call, log untouched, no output); a run killed after the source sync
  (simulated reboot) is completed by the per-boot copy (marker written,
  setup run once, tree on the ref), and the next boot is a no-op. The
  per-boot copy is mode 0700 and byte-identical to the script.
- apt lock semantics, `ubuntu:22.04` (apt 2.4.14) and `ubuntu:24.04`
  (apt 2.8.3) with a concurrent apt-get holding the lock:
  `apt-get -o DPkg::Lock::Timeout=120 install` waited ("Waiting for cache
  lock ... lock-frontend") and succeeded; `apt-get -o
  DPkg::Lock::Timeout=120 update` failed at once (rc 100, "Could not get
  lock /var/lib/apt/lists/lock"). The option covers the dpkg lock only.

## Test changes (17 Python test files, 1 jest file)

moto does not implement `DescribeInstanceAssociationsStatus`, so every
suite that drives a dedicated dispatch or an ephemeral agent start through
the real dispatcher needed an answer for the new read. They answer
"settled" (no association), so their scenarios are unchanged: the three
tick-integration suites patch the client method; the other moto suites
patch `runner_association_statuses`; `test_bootstrap_gate_property.py`'s
recording SSM fake gained the method; the no-live-validation contract
allow-lists it as a read (never as a costly action); the preflight matrix
replaces it with a recorder and now also asserts an invalid contract never
reads it. The byte oracles in `test_bootstrap_zip_preservation.py`,
`test_jp7_ephemeral_preservation.py` and
`test_jp7_mixed_batch_tick_integration.py` are consciously re-recorded
(the re-entry lines and the lock option only), and
`test_run_as_ubuntu_unit.py` now counts the marker WRITE (once) and the
two re-entry guards. No assertion was removed or loosened.

## Public-repo check

Before each commit, the brief's public-repo grep (account id, internal
program and alias tokens; the pattern itself is kept out of the repo) over
`git diff origin/integration/all-specs` printed nothing (exit 1).

## Commits

- `58524e5` fix(builds): hold ephemeral dispatch until SSM associations
  settle — code, IAM and tests.
- The spec commit that adds this directory.

## Deploy evidence

Ship step, 2026-10-07 UTC, us-east-1, umask 0002. Full record in the
workflow's `ship-evidence.md` (outside the repo).

- Rebase: `origin/integration/all-specs` was still `86ddf49` at 05:21Z,
  the branch's base, so no rebase or re-test was needed.
- Build gate: `dda-portal-build-jobs` held 0 jobs outside the terminal set
  (succeeded, failed, interrupted, cancelled) of 188 at 05:21Z and again
  at 05:36Z, right before the deploy. `ed2bb0c2…` had succeeded. No build,
  SSM command or build server was touched.
- Deployed code before: the build handlers' bundle differed from this
  branch's `backend/functions` in exactly `build_dispatcher.py`,
  `build_fleet.py` and `build_planner.py`. No other undeployed trunk
  commit touches a Portal asset.
- `cdk diff --all` (cloudFrontDomain from FrontendStack,
  `portalRegistryEnforced=true`): BuildFleetStack adds
  `ssm:DescribeInstanceAssociationsStatus` (`*`) to the BuildDispatcher
  and BuildJobs default policies (2 `+` rows, no `-` row); every function
  on the shared `backend/functions` asset gets new Code. Otherwise noise
  only: the two `Timestamp` custom resources, the ApiGateway nested
  TemplateURL, the two imaging layer versions (built `python/` equals
  deployed v18 byte for byte) and the Quick Setup bucket deployment's
  source key (all 5 files equal the live objects). No data store
  replacement, no IAM removal.
- Quick Setup: synth and deploy printed
  `setup-bundle.tar.gz (sha256=b367e915…c078)`, the live value.
  JwtLayer not built; it stays as deployed.
- Push: `spec/ephemeral-runner-patch-reboot` pushed (new branch), then
  fast-forward `86ddf49..df80db4` to `integration/all-specs`. Public-repo
  grep empty before each push. `remediation` not pushed.
- Deploy: `./deploy-infrastructure.sh` 05:37:27 to 05:52:13Z, rc 0, 8/8
  stacks `✅` (Auth, Storage, Frontend no changes). 0
  `CREATE_FAILED|UPDATE_FAILED|UPDATE_ROLLBACK` lines. All 16 portal
  stacks `UPDATE_COMPLETE`; BuildFleetStack updated 05:48:32Z.
- After: the deployed functions tree equals this branch's
  (`diff -rq --exclude=__pycache__` clean); sha256 of
  `build_dispatcher.py` `26cf986d…`, `build_fleet.py` `ec395d74…`,
  `build_planner.py` `8020436e…`, each equal to the branch. The
  dispatcher role's default policy carries
  `ssm:DescribeInstanceAssociationsStatus`. `PORTAL_REGISTRY_ENFORCED`
  is `true` on all 54 handlers that set it; `QuickSetupHandler`'s
  environment is unchanged (`QUICK_SETUP_BUNDLE_SHA256` `b367e915…c078`),
  and the live bundle object hashes to the same value.
- Dispatcher watch 05:48:59 to 06:04:59Z: 17 ticks, each "Dispatcher tick
  complete: {jobs: 188, servers: 9}"; no ERROR, Traceback, AccessDenied or
  timeout; CloudWatch Errors 0. No job was queued in the window, so the
  new association read has not yet run live (review finding 4, task 11).
- `cdk.out` moved to `cdk.out.bak-20261007T060058Z`; guard pair: 4 passed,
  3 skipped (no cdk.out copies present). The main clone's `cdk.out*` set
  is unchanged.

