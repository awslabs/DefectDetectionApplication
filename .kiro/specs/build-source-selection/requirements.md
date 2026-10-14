# Requirements Document

## Introduction

Portal build submissions currently offer no control over **which source is
built**. The repository is a single deploy-time environment variable
(`BUILD_REPO_URL = https://github.com/awslabs/DefectDetectionApplication` on
`BuildDispatcherHandler`), and every bootstrap path runs a bare
`git clone <repo_url>` with no ref, so runners always get the remote default
branch (`origin/main`). A user who wants to build a feature branch, or who
works from a fork, has no way to say so.

This feature adds source selection to the build submission surface: an
editable **repository** field defaulting to the DDA repository, and a
**branch dropdown** populated from the branches of whichever repository is
selected. The selected values ride with the Build_Job and reach the runner.

The plumbing is already half-present, which shapes the work:

- `build_jobs.py:103` already defines `'source_ref': None,  # None -> the repo default branch` in the build config, and `build_domain.py:811` / `build_fleet.py:112` carry the same key.
- `build_dispatcher.py:239-241` already appends `SOURCE_REF=<ref>` to the agent command when the snapshot carries one.
- `scripts/portal-build-agent.sh:155-176` already implements the sync: `git fetch --prune origin`, then `git checkout --force -B <ref> origin/<ref>` for a branch or `git checkout --force <ref>` for any other ref, with distinct failure messages for each.

So `source_ref` exists as a **global config value** with no UI and no
per-submission override, and repository selection does not exist at all.

**A blocking defect makes this more than a convenience feature.** Live
evidence from 2026-08-06 proves no portal build can currently run at all,
in either execution mode:

- SSM command `e9281bdc` (job `1ce014a3`, JP5 ephemeral) and `d75f1ea2` (job `a25bb078`, AMD64 dedicated) both ended `Failed` with response code **127** and stderr `bash: /opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh: No such file or directory`.
- `scripts/portal-build-agent.sh` was added in commit `479ab7f`, which exists only on `origin/feature/portal-build-fleet-and-workflow-gates`. It is **absent from `origin/main`**, which is what the runners clone. Inspection of the live runner `i-0b8221f5ed2ebc2a9` confirmed `/opt/dda/DefectDetectionApplication` exists (the clone succeeded) while `scripts/portal-build-agent.sh` does not.
- The agent script is therefore invoked from a tree that cannot contain it. Because the script is what performs the `SOURCE_REF` checkout, the ref-sync logic can never run: the source the build needs is unreachable by the mechanism that was supposed to fetch it.

Source selection is the fix for that bootstrap ordering problem, not just a
UI nicety: the runner must obtain the **selected** repository and ref before
invoking the agent, rather than assuming the default branch already contains
the agent.

Two adjacent defects, proven by the same evidence, must be corrected for a
selected source to actually build:

- **Repo-directory mismatch (dedicated).** `build_dispatcher.py:112-113` defaults `BUILD_REPO_DIR` to `/opt/dda/DefectDetectionApplication` (the deployed Lambda sets no override), while `build_fleet.py:179` bootstraps dedicated servers with `git clone {repo_url} /home/ubuntu/DefectDetectionApplication`. The agent path is never where the repo is.
- **Ephemeral bootstrap race.** The agent command for job `1ce014a3` was requested at 21:36:59Z, while cloud-init on that runner did not finish until 21:38:54Z (`Up 140.42 seconds`). `runner_bootstrap_user_data()` clones and runs `setup-build-server.sh` in user-data, but nothing gates the SSM command on its completion. The dedicated path writes `/var/log/dda-build-server-bootstrap.done`; the ephemeral path has no equivalent and no wait.

**Scope**: the build submission API and UI (repository + branch selection,
branch discovery), the values' propagation into both bootstrap paths and the
agent invocation, and the two defects above that prevent a selected source
from building. 

**Out of scope**: the role-permission matrix and builds authorization
(fixed separately in `build-fleet-rbac-visibility`); SSM/agent outcome
reconciliation, execution diagnostics, and runtime accounting (owned by
`build-fleet-execution-failures`); the missing **cancel control** in the
builds UI (`api.cancelBuild()` and `POST /builds/{id}/cancel` both exist but
no component references them) — a separate defect, noted here because a user
who selects the wrong source needs it, and tracked for its own fix.

## Requirements

### Requirement 1: Repository selection

**User Story:** As a build operator, I want to choose which repository a
build is made from, so that I can build my own fork without redeploying the
portal.

#### Acceptance Criteria

1.1 WHEN the build submission form is displayed THEN the system SHALL present a repository field pre-filled with the configured default repository (`https://github.com/awslabs/DefectDetectionApplication`)

1.2 WHEN the user has not changed the repository field THEN the system SHALL submit the default repository, so existing behavior is the zero-effort path

1.3 WHEN the user enters a different repository URL (e.g. a personal fork) THEN the system SHALL accept it and use it as the clone source for that build

1.4 WHEN a submitted repository URL is not a well-formed HTTPS Git remote THEN the system SHALL reject the submission with the standard validation error envelope naming the offending field, and SHALL NOT create a Build_Job

1.5 WHEN the deployment's default repository is configured (build config setting `build_infrastructure_config`) THEN the system SHALL use that value as the form default, so the default is operator-controlled rather than hard-coded in the frontend

1.6 WHEN a repository URL is submitted THEN the system SHALL persist it on the Build_Job's `config_snapshot`, so the job record shows exactly what was built

### Requirement 2: Branch selection

**User Story:** As a build operator, I want to pick the branch to build from
a list, so that I do not have to remember or type exact branch names.

#### Acceptance Criteria

2.1 WHEN the build submission form is displayed THEN the system SHALL present the branch as a dropdown populated with the branches available in the currently selected repository

2.2 WHEN the user changes the repository THEN the system SHALL re-populate the branch dropdown from the newly selected repository

2.3 WHEN branch discovery is in progress THEN the system SHALL show a loading state, and WHEN it fails THEN the system SHALL show an actionable error while still allowing the user to enter a ref manually (discovery failure SHALL NOT block submission)

2.4 WHEN no branch is explicitly selected THEN the system SHALL default to the selected repository's default branch, preserving today's implicit behavior

2.5 WHEN a branch is selected THEN the system SHALL submit it as the job's source ref and persist it in `config_snapshot.source_ref`, the field that already exists for this purpose

2.6 WHEN the build detail view is shown THEN the system SHALL display the repository and ref the job was built from

2.7 WHEN a ref that is not a branch (a tag or commit SHA) is supplied THEN the system SHALL accept it, since `portal-build-agent.sh` already falls back to `git checkout --force <ref>` for non-branch refs

### Requirement 3: Branch discovery

**User Story:** As a build operator, I want the branch list to come from the
real repository, so the dropdown reflects what actually exists.

#### Acceptance Criteria

3.1 WHEN the frontend requests branches for a repository THEN the system SHALL return the repository's branches and identify which one is the default

3.2 WHEN the repository is a public GitHub repository THEN the system SHALL discover branches without requiring credentials (both the DDA repository and typical forks are public)

3.3 WHEN the upstream discovery call fails, times out, is rate-limited, or the repository does not exist or is not accessible THEN the system SHALL return a distinct, actionable error for each case rather than an empty list presented as success

3.4 WHEN branch discovery is requested THEN the system SHALL require the same builds read authorization as the rest of the builds surface, and SHALL record denials in the existing audit structure

3.5 WHEN a repository URL is supplied for discovery THEN the system SHALL validate and normalize it before any outbound call, and SHALL NOT allow the value to be used to reach non-repository endpoints

### Requirement 4: The selected source reaches the runner

**User Story:** As a build operator, I want the build to actually be made
from the source I selected, so the resulting component contains my changes.

#### Acceptance Criteria

4.1 WHEN a runner is bootstrapped for a job THEN the system SHALL obtain the job's selected repository and ref, so that the tree on disk contains that ref's content — including `scripts/portal-build-agent.sh` itself

4.2 WHEN the agent is invoked THEN the system SHALL NOT depend on the agent script being present in the repository's default branch, since the live 127 failures prove that assumption is false whenever the script lives on a non-default branch

4.3 WHEN the selected ref is synced THEN the system SHALL preserve the existing `SOURCE_REF` contract that `portal-build-agent.sh` implements (`git fetch --prune origin`, then branch or detached-ref checkout) rather than introducing a second, divergent sync mechanism

4.4 WHEN a selected repository or ref cannot be obtained on the runner THEN the system SHALL fail the Build_Job with an error that names the repository and ref and distinguishes "ref not found" from "repository unreachable", instead of surfacing a bare exit 127

4.5 WHEN a build completes THEN the system SHALL record the resolved commit SHA that was built, so a job is traceable to an exact source state even if the branch moves

### Requirement 5: Repo directory alignment (blocking defect)

**User Story:** As a build operator, I want dedicated-server builds to find
the agent, so that dedicated mode works at all.

#### Acceptance Criteria

5.1 WHEN the dispatcher composes the agent command for a dedicated server THEN the system SHALL target the directory where that server's bootstrap actually placed the repository, eliminating the `/opt/dda/DefectDetectionApplication` versus `/home/ubuntu/DefectDetectionApplication` mismatch

5.2 WHEN the repository location is determined THEN the system SHALL use one authoritative value shared by the bootstrap that creates the clone and the dispatcher that invokes the agent, so the two cannot drift again

5.3 WHEN existing dedicated servers (already bootstrapped with the repository at `/home/ubuntu/DefectDetectionApplication`) receive a build THEN the system SHALL work against them without requiring them to be rebuilt or re-bootstrapped

5.4 WHEN the agent path is resolved for an ephemeral runner THEN the system SHALL resolve to the same location its user-data cloned into, for both the default and any overridden repository directory

### Requirement 6: Bootstrap completion gating (blocking defect)

**User Story:** As a build operator, I want the build to start only once the
runner is ready, so builds do not fail 3 seconds in on a half-built machine.

#### Acceptance Criteria

6.1 WHEN an ephemeral runner is launched THEN the system SHALL NOT send the agent command until the runner's bootstrap has completed, closing the observed 21:36:59Z-command versus 21:38:54Z-bootstrap-finish race

6.2 WHEN a runner's bootstrap signals completion THEN the system SHALL detect that signal explicitly, mirroring the dedicated path's existing `/var/log/dda-build-server-bootstrap.done` marker rather than relying on a fixed sleep

6.3 WHEN a runner's bootstrap does not complete within a bounded interval THEN the system SHALL fail the Build_Job with an error identifying bootstrap as the failing stage, and SHALL NOT leave the job waiting indefinitely

6.4 WHEN a runner's bootstrap fails partway (the live runner logged `Failed: sudo chmod 666 /var/run/docker.sock` and `Failed to set Python 3.11 as default` while still reporting overall success) THEN the system SHALL treat the readiness signal as authoritative and record the bootstrap log location for diagnosis

6.5 WHEN a job fails during provisioning or bootstrap THEN the system SHALL release the runner rather than leaving it running, addressing the observed orphan (`i-0b8221f5ed2ebc2a9`, idle over an hour after its agent command failed at 3 seconds) — noting that the reconciliation half of this behavior is owned by `build-fleet-execution-failures`

### Requirement 7: Unchanged behavior (regression prevention)

#### Acceptance Criteria

7.1 WHEN a build is submitted without any repository or branch selection THEN the system SHALL behave exactly as today: default repository, default branch, same request shape, same `201` response, same one queued Build_Job

7.2 WHEN the builds authorization boundary is evaluated THEN the system SHALL CONTINUE TO apply the existing role-permission matrix and 403 envelope unchanged (`builds:submit` for PortalAdmin, DataScientist, UseCaseAdmin), including the `build-fleet-rbac-visibility` fixes

7.3 WHEN a Build_Job is persisted THEN the system SHALL CONTINUE TO omit `None`-valued indexed key attributes, preserving the `server-index` GSI fix that made ephemeral submission possible

7.4 WHEN the existing build APIs are called THEN the system SHALL CONTINUE TO return the same list/detail/logs shapes, pagination tokens, ordering, cancel semantics, and error envelopes

7.5 WHEN `config_snapshot` is written THEN the system SHALL CONTINUE TO carry its existing keys and snapshot semantics, extending rather than restructuring it

7.6 WHEN `portal-build-agent.sh` runs THEN the system SHALL CONTINUE TO honor its existing argument contract and phase-event emissions, so the events pipeline is unaffected

7.7 WHEN the `portal_builds` and builds-related backend suites run THEN the system SHALL CONTINUE TO pass them unchanged
