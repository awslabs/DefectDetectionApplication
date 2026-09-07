# Bugfix Requirements Document

## Introduction

Portal-launched build servers are born without the `zip` binary, so every
first build on a fresh server dies at the very last packaging step after
~1.5 hours of successful work — the most expensive possible failure point.

**Incident (observed live, 2026-09-07):** Build_Job
`53312133-1ce1-4c09-a1c8-3fa01d0e9d1a` (target JP6, component
`aws.edgeml.dda.LocalServer.arm64JP6`, dedicated server
`srv-aac90870-033e-4e9c-9994-29ee895da421`, EC2 instance
`i-07c2ca92f3a526c93`, m6g.4xlarge arm64) failed with:

```
build-custom.sh: line 447: zip: command not found
ERROR: packaging zip failed (exit 127).
```

Everything before that point had succeeded: edgemlsdk + flask-app +
react-webapp Docker images built, in-image backend tests and the security
gate passed, image tars staged (flask-app.tar 9.9G, react-webapp.tar 30M).
The build died only when `build-custom.sh` invoked `zip` to package the
component archive (the explicit `ZIP_MEMBERS` invocation at line ~447 plus
the `zip -T` integrity check — see the build-docker-save-stdout-failure
spec for that packaging block's history).

**Root cause (verified in code):** the Dedicated_Build_Server bootstrap is
`USER_DATA_BODY` in `edge-cv-portal/backend/functions/build_fleet.py`
(~line 296). Its root-run package installation is exactly `apt-get update`
+ `apt-get install -y git` — `zip` is never installed there, and Ubuntu
server cloud images (22.04 and 24.04, standard and Pro) do not ship it.
(The docstring comment near line 278 saying something "is preinstalled on
Ubuntu 22.04" refers to the SSM agent; `zip` is NOT preinstalled.)

**Why the setup script does not save us:** the bootstrap does run
`setup-build-server.sh` afterwards, and that script's apt line has listed
`zip` since 2026-02-15 (commit d430cf6). But that is a weak, indirect
guarantee: the script executed is the copy in the SYNCED SOURCE REF's tree
(a server bootstrapped onto an older ref runs an older script without
`zip`), and its apt install is deliberately failure-tolerant
(`run_cmd ... || add_warning`), so transient apt failures right after boot
(e.g. lock contention with unattended-upgrades) silently skip the package.
The observed live server completed its bootstrap and still had no `zip`.
The fix must make the guarantee direct: the portal-owned, root-run apt
line in the rendered user-data itself.

**Single-source-of-truth verification (done during exploration of this
report):** within `build_fleet.py` the claim holds — `USER_DATA_TEMPLATE`
and `render_user_data()` both flow through `_user_data_body()`, and the
launch site (`run_fleet_instance`, used by POST /build-servers →
`launch_build_server`) renders via `render_user_data()`, so one edit to
`USER_DATA_BODY` covers every dedicated-server render path. The
ubuntu-pro-build-servers `ubuntu_flavor` selection (pro/standard) alters
ONLY AMI resolution (`resolve_ubuntu_ami`); both flavors share the same
`render_user_data` path, so both get the fix from the same edit. HOWEVER,
there is a second, parallel bootstrap the claim does not cover:
`runner_bootstrap_user_data()` in
`edge-cv-portal/backend/functions/build_dispatcher.py` (~line 1176)
generates the EPHEMERAL runner user-data with its own
`apt-get update -y && apt-get install -y git` line. Ephemeral runners
execute the same `build-custom.sh` and carry the identical latent failure,
so both bootstrap generators are in scope.

**Manual mitigation already applied (NOT the fix):** `zip`/`unzip` were
installed via SSM on the one live server `i-07c2ca92f3a526c93`, after
which resubmitted job `ec55e6fa-07d5-489e-a5ce-ba10c6c2535c` succeeded and
published 1.0.64. Every FUTURE server launched through POST /build-servers
is still born without `zip`.

**Out of scope:**
- The edgemlsdk cached-debs cache not being arch-aware (an amd64
  `openssl.deb` poisoning arm64 local builds) — a separate local-build
  issue.
- Retrofitting `zip` onto already-running servers — handled operationally
  via SSM, already done for the one live server.

**Deployment context:** the fix lands in a Lambda-managed asset
(BuildFleetHandler in EdgeCVPortalBuildFleetStack; the dispatcher handler
for the ephemeral path). Making it live requires a portal infrastructure
deploy (`edge-cv-portal/deploy-infrastructure.sh`), honoring
`.kiro/steering/builds.md`: never deploy while a component build runs, and
move `cdk.out` aside afterwards for the security-gate drift guard.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a Dedicated_Build_Server is launched through POST /build-servers
(`launch_build_server` → `run_fleet_instance` → `render_user_data` →
`USER_DATA_BODY`) THEN the rendered user-data's root package installation
installs only `git`, and the instance boots without the `zip` binary
(neither `zip` nor `unzip` appears anywhere in the rendered bootstrap
text).

1.2 WHEN a Build_Job runs `build-custom.sh` on such a server and reaches
the packaging step (~line 447) THEN the build fails with
`zip: command not found` / `ERROR: packaging zip failed (exit 127)` after
~1.5 hours of otherwise-successful work, and the job is recorded failed.

1.3 WHEN an ephemeral runner is provisioned
(`build_dispatcher.runner_bootstrap_user_data`) THEN its generated
user-data likewise installs only `git` at the root prologue, leaving the
same latent packaging failure for ephemeral builds.

1.4 WHEN the synced source ref's `setup-build-server.sh` is old (pre-zip)
or its failure-tolerant apt line silently fails THEN nothing else in the
bootstrap installs `zip`, so bootstrap completion (Bootstrap_Marker
written, readiness gate open) does NOT imply the build toolchain is
complete.

### Expected Behavior (Correct)

2.1 WHEN a Dedicated_Build_Server is launched through POST /build-servers
THEN the rendered user-data (every render path: `render_user_data(...)`
with any repo_dir/source_ref combination, and the module-level
`USER_DATA_TEMPLATE`) SHALL install `zip` and `unzip` in its root-run
apt-get install line, so the binaries exist regardless of which source ref
is synced and before any build can be dispatched.

2.2 WHEN a Build_Job runs `build-custom.sh` on a freshly bootstrapped
server and reaches the packaging step THEN the `zip` invocation SHALL find
the binary and package the archive (no exit 127 at line ~447).

2.3 WHEN an ephemeral runner is provisioned via
`runner_bootstrap_user_data` THEN its generated user-data SHALL likewise
install `zip` and `unzip` in the root apt-get install line.

2.4 WHEN either bootstrap text is rendered THEN it SHALL remain valid bash
(`bash -n` clean, matching the existing
`test_bootstrap_gate_property.py` syntax discipline).

### Unchanged Behavior (Regression Prevention)

3.1 WHEN user-data is rendered for any (repo_url, repo_dir, source_ref)
THEN the system SHALL CONTINUE TO produce the same bootstrap sequence in
the same order — log redirect, apt install, clone, Source_Sync block,
`setup-build-server.sh` run, Bootstrap_Marker write last — with the apt
package list being the only changed text.

3.2 WHEN user-data is rendered THEN the system SHALL CONTINUE TO leave
`{repo_url}` as the only unbound placeholder in `USER_DATA_TEMPLATE`
(`.format(repo_url=...)` stays valid for every existing caller), keep the
Source_Sync block byte-identical to `build_source.source_sync_commands`
output, and keep the non-fatal log-redirect and marker-write semantics.

3.3 WHEN existing portal_builds tests that parse or pin the bootstrap text
run (`test/backend-test/portal_builds/`: git-clone-line parsing in
`test_source_selection_preservation.py`, directory alignment in
`test_source_dir_alignment_property.py`, marker-last/`bash -n` in
`test_bootstrap_gate_property.py`, run-as-ubuntu structure in
`test_run_as_ubuntu_unit.py`) THEN they SHALL CONTINUE TO pass, except the
FROZEN byte-level ephemeral oracle in
`test_jp7_mixed_batch_tick_integration.py` (`frozen_runner_bootstrap`),
which pins the dispatcher's exact apt line and MUST be consciously
re-recorded to the new line as part of this fix — not weakened or deleted.

3.4 WHEN a launch request selects either Ubuntu_Flavor (`pro` or
`standard`) or either supported Ubuntu release THEN the system SHALL
CONTINUE TO resolve AMIs and validate requests exactly as before — the
fix touches only the user-data text shared by both flavors.

3.5 WHEN the readiness gate evaluates a bootstrap (marker written, budget
semantics, classified sync exits 65/66) THEN the system SHALL CONTINUE TO
behave identically.

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type RenderedBootstrapUserData
  // X is the text produced by build_fleet.render_user_data(...) /
  // USER_DATA_TEMPLATE, or by
  // build_dispatcher.runner_bootstrap_user_data(job, ...)
  OUTPUT: boolean

  // The bug condition holds for every rendered bootstrap today:
  // the root apt-get install line does not install zip.
  RETURN NOT ("zip" IN packages(root_apt_install_line(X)))
END FUNCTION
```

**Property (Fix Checking):**

```pascal
// For every render path and every input combination, the fixed
// generators emit a bootstrap whose root apt line installs zip + unzip.
FOR ALL (repo_url, repo_dir, source_ref) DO
  text ← render_user_data'(repo_url, repo_dir, source_ref)
  ASSERT "zip" IN packages(root_apt_install_line(text))
     AND "unzip" IN packages(root_apt_install_line(text))
END FOR
// and likewise for runner_bootstrap_user_data'(job, repo_dir) whenever
// it generates non-empty text.
```

**Preservation Goal:**

```pascal
// Everything about the rendered bootstrap other than the apt package
// list is byte-identical between F and F'.
FOR ALL (repo_url, repo_dir, source_ref) DO
  ASSERT normalize(F(inputs)) = normalize(F'(inputs))
  // where normalize() maps the root apt-get install line to a
  // canonical token, leaving every other line untouched
END FOR
```
