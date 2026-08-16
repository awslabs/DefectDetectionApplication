# JP7 Ephemeral Runner Provisioning — Bug Condition Counterexamples

Spec: `.kiro/specs/jp7-ephemeral-runner-provisioning` (task 1)
Test file: `test/backend-test/portal_builds/test_jp7_ephemeral_provisioning_exploration.py`
Run (from the repository root, offline, stubbed `boto3`/`shared_utils`):

```
python3 -m pytest \
    test/backend-test/portal_builds/test_jp7_ephemeral_provisioning_exploration.py \
    --noconftest -q
```

Result on UNFIXED code: **8 failed** (all five exploration cases). Every
failure below is EXPECTED — each one is a counterexample confirming
`isBugCondition(X)` from the design: a JP7 build job is planned/provisioned
as a jammy (Ubuntu 22.04) host in ephemeral mode, no layer screens an
incapable dedicated host, and the shared bootstrap is noble-incompatible.
These same assertions encode the post-fix expected behavior and must PASS
unchanged in task 3.8.

---

## (a) Plan carries no OS release (clause 1.3)

`TestPlanCarriesRequiredOsRelease::test_plan_distinguishes_jp7_host_os_from_jp5`

Shrunk falsifying example: `config_snapshot={}` (Hypothesis noted the test
fails for every generated snapshot).

Observed on unfixed code — `plan_runner` produces a `RunnerPlan` with NO
`os_release` field; a JP7 job and a JP5 job of the same snapshot plan
IDENTICALLY:

```
JP7 plan os_release : None        (expected '24.04')
JP5 plan os_release : None        (expected '22.04')
JP7 plan fields     : {'arch': 'arm64', 'instance_type': 'm6g.4xlarge',
                       'volume_size_gb': 100, 'spot': False,
                       'status': 'provisioning'}
JP5 plan fields     : identical (arch/instance_type/volume/spot/status)
```

The dispatcher therefore has no information with which to select a noble AMI.

## (b) Jammy AMI selected for JP7 (clauses 1.1, 1.2)

`TestJammyAmiSelectedForJp7::test_noble_parameter_requested_for_jp7_plan`

Shrunk falsifying example: `config_snapshot={}` (fails for every snapshot).

Observed on unfixed code — with a recording SSM stub and no env overrides,
resolving the AMI for a JP7 plan reads the Ubuntu **22.04** arm64 canonical
parameter and returns a jammy image:

```
requested SSM parameter path : /aws/service/canonical/ubuntu/server/22.04/
                               stable/current/arm64/hvm/ebs-gp2/ami-id
DescribeImages fallback calls: 0
returned AMI id              : ami-0jammy22040000stub   (the jammy stub)
```

No 24.04 parameter path is ever requested. A JP7 ephemeral runner is
provisioned from the wrong OS release and the build fails mid-run.

## (c) Jammy env override applied to JP7 (clause 1.1)

`TestJammyOverrideAppliedToJp7::test_jammy_pin_not_returned_for_jp7_plan`

Shrunk falsifying example: `config_snapshot={}` (fails for every snapshot).

Observed on unfixed code — with `BUILD_ARM64_AMI_ID` set to a jammy pin,
the pin is returned verbatim for the JP7 plan:

```
BUILD_ARM64_AMI_ID (jammy pin) : ami-0jammypinned0000001
returned AMI id                : ami-0jammypinned0000001   (the pin, verbatim)
SSM parameters consulted       : []   (override short-circuits resolution)
```

The arch-keyed jammy override silently pins a JP7 runner to a 22.04 image.

## (d) No dedicated capability gate (clauses 1.7, 1.8)

`TestNoDedicatedCapabilityGate::test_jp7_dedicated_rejected_on_incapable_host`
(both parametrizations: `ubuntu_version=22.04` and `ubuntu_version-absent`)

Shrunk falsifying examples:
- `recorded_release='22.04'`, `server_id='srv-e3e70682-c209-4cac-629f-6fbed82c07cd'`, `source_ref=None`
- `recorded_release=None` (pre-ec1dc38 record, field absent), same server id

Observed on unfixed code — `validate_build_request` for JP7 + dedicated
against a running arm64 server ACCEPTS both requests:

```
server record (case 1)   : lifecycle running, arch arm64, ubuntu_version=22.04
server record (case 2)   : lifecycle running, arch arm64, ubuntu_version=<field absent>
observed validation      : ValidationResult(valid=True, errors=())   — both cases
observed errors          : <none>
```

No rule inspects the recorded `ubuntu_version`; no missing-capability
diagnostic (Ubuntu 24.04 arm64 vs the server's actual release) is produced.
An accepted validation means a Build_Job record is created and the build
fails mid-run on the jammy host (contrary to jetpack7-support Req 6.4/6.5
fail-closed semantics).

## (e) Noble-incompatible shared bootstrap (clauses 1.4, 1.5, 1.6)

### Unflagged PEP 668 installs

`TestNobleIncompatibleSharedBootstrap::test_pip_installs_carry_pep668_flag`

Observed on unfixed code — the checked-in `setup-build-server.sh` carries
these three command lines, none with `--break-system-packages` (each is
rejected by noble's externally-managed Python):

```
run_cmd "sudo pip3 install awscli" || add_warning "Failed to install AWS CLI"
run_cmd "sudo pip3 install --no-compile 'botocore[crt]'" || add_warning "Failed to install botocore[crt] - 'gdk component publish' may fail to resolve credentials"
run_cmd "pip3 install --user git+https://github.com/aws-greengrass/aws-greengrass-gdk-cli.git@v1.6.2" || add_warning "Failed to install GDK CLI from GitHub"
```

### docker-compose section knows only the snap install

`TestNobleIncompatibleSharedBootstrap::test_docker_compose_section_provides_noble_shim`

```
snap install branch present : True   ("sudo snap install docker-compose")
noble shim present          : False  (no `exec docker compose "$@"` anywhere)
```

The README 4.2 shim delegating `docker-compose` to the noble
`docker compose` plugin is never written, so `build-custom.sh`'s
`docker-compose` invocations cannot resolve on a noble host.

### Release-blind 24.04 launch bootstrap

`TestNobleIncompatibleSharedBootstrap::test_rendered_launch_user_data_reaches_noble_deltas`

Shrunk falsifying example: `repo_url='https://github.com/example-org/
DefectDetectionApplication.git'`, `repo_dir=None`, `source_ref=None`
(fails for every generated repo/dir/ref combination).

```
render_user_data signature accepts ubuntu_version : False
rendered body executes setup-build-server.sh      : True
shared script carries the noble deltas            : False
```

`build_fleet.render_user_data` takes no release input at all, so the
rendered user-data for a 24.04 fleet launch is byte-identical to a 22.04
launch: the same release-blind plain `setup-build-server.sh` run
(`USER_DATA_BODY`), with none of the noble deltas. A portal-launched 24.04
server is registered in the fleet but is NOT JP7-build-capable without
manual operator steps.

---

## Root cause confirmation

All five cases failed exactly as the design's Exploratory Bug Condition
Checking predicted, confirming the hypothesized causes:

1. Missing plan dimension: `RunnerPlan` carries only CPU architecture.
2. Arch-only AMI resolution: `resolve_ami(arch)` knows two 22.04 parameters
   and two jammy env overrides; no noble mapping exists.
3. Missing capability gate: `validate_build_request` checks existence,
   running state, and arch only — never the recorded `ubuntu_version`.
4. Release-blind, noble-incompatible shared bootstrap: one plain
   `setup-build-server.sh` run for every release, with unflagged PEP 668
   installs and a snap-only docker-compose section.
