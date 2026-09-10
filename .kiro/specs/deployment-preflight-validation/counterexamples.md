# Counterexamples — task 2 bug-condition exploration

Suite: `edge-cv-portal/backend/tests/test_deployment_preflight_exploration.py`
Tag: `# Feature: deployment-preflight-validation, Property 1: bug condition — pre-submit closure validation`

Command (from the repo root, WITH conftest — it provides the moto `aws_stack`
fixture, the `sys.path` layer wiring and the Hypothesis profiles; `ci` = 100
examples, no hardcoded `max_examples`, no `--noconftest`):

```
HYPOTHESIS_PROFILE=ci ~/.dda-test-venv/bin/python -m pytest \
  edge-cv-portal/backend/tests/test_deployment_preflight_exploration.py \
  -q -p no:cacheprovider
```

Result on the UNFIXED tree (2026-09-10, hypothesis 6.165.10, moto 5.1.22,
pytest 8.4.2, `~/.dda-test-venv` CPython 3.14.4):

```
FF.FF.F.F                                                                [100%]
6 failed, 3 passed in 43.13s
```

| Test | Leg | Unfixed outcome |
|---|---|---|
| `TestLegAPlatformMismatch::test_generic_path_refuses_jp5_jp6_only_component_on_jp7_device` | A (generic submit path) | **FAILED** — expected |
| `TestLegAPlatformMismatch::test_workflow_path_refuses_jp5_jp6_only_component_on_jp7_device` | A (workflow submit path) | **FAILED** — expected |
| `TestLegBUnresolvableDependency::test_dependency_with_zero_published_versions_refused_before_submit` | B (zero published versions) | **FAILED** — expected |
| `TestLegBUnresolvableDependency::test_dependency_with_only_non_satisfying_versions_is_distinguished` | B (only non-satisfying versions) | **FAILED** — expected |
| `TestLegCDeselectedStillRequired::test_first_submit_refused_with_the_effective_outcome_stated` | C | **FAILED** — expected |
| `TestLegDSinglePassReporting::test_all_three_findings_come_back_in_one_response` | D | **FAILED** — expected |
| `TestLegAPlatformMismatch::test_control_variantless_aarch64_component_still_deploys` | control (3.2, 2.8) | PASSED |
| `TestLegBUnresolvableDependency::test_control_jetpack_matched_twin_resolves_and_submits` | control (3.3, 2.4) | PASSED |
| `TestLegCDeselectedStillRequired::test_control_removal_with_no_remaining_dependant_submits` | control (3.11) | PASSED |

**Every leg fails on the same assertion**: the submission was FORWARDED to
`greengrass_client.create_deployment` instead of being refused pre-submit. The
bug condition is confirmed for all three faults, on both submit paths — today
nothing on the submit path resolves the selected components' transitive recipe
`ComponentDependencies` closure, so every one of these submissions is accepted
and forwarded, with HTTP 201 and no finding of any kind.

The three controls passing is the other half of the proof: the fixture is not
refusing everything. A clean variant-less aarch64 component, a JetPack-matched
`-jp7` twin whose dependency resolves, and a removal with no remaining
dependant all submit today — and must keep submitting after the fix (3.2, 3.3,
3.11).

## What is asserted, and what is deliberately not

Per `evidence.md` §4.1 the legs assert the **pre-submit refusal**
(`gg.create_deployment_calls` empty, HTTP 409, a preflight error code), NOT a
reproduced Greengrass error string: `GetDeployment` carries no
`reason`/`statusDetails` member and every superseded revision now reports
`INACTIVE`, so `FAILED_NO_STATE_CHANGE` / `errorStack` are not verifiable from
the allowed read-only set.

Per `evidence.md` §5.4 leg C asserts the **mechanism** — a de-selected
component still HARD-required by a selected one is refused pre-submit — and
encodes no retention window: the depending workflow is absent from revisions
34 and 36-38, so the live retention was not monotonic.

Every fixture component set carries the auto-included
`aws.greengrass.Nucleus` (`{"os":"linux"}`, no architecture),
`aws.greengrass.ShadowManager` and `aws.greengrass.LogManager`
(`{"os":"*"}`), and a variant-less aarch64 `aws.edgeml.dda.LocalServer.arm64JP7`
(27 published versions, seeded under the account namespace while the AWS
components are seeded under `aws`). Each leg asserts that **no finding names
any of them** (`assert_wildcard_components_never_flagged`), so a leg cannot
pass with a matcher that would block every real deployment (bugfix.md 2.8 as
corrected by `evidence.md` §5.1) or with a single-namespace resolvability check
(`evidence.md` §1.1, §5.6 — the wrong namespace returns an EMPTY LIST, never
an error).

Each leg also asserts `assert_no_existing_gate_fired`: no refusal may carry
`VLLM_ARCH_UNSUPPORTED`, `PLUGIN_LIFECYCLE_VIOLATION`,
`PLUGIN_ARCH_UNSUPPORTED`, `INCOMPATIBLE_LOCAL_SERVER`,
`CAMERA_BINDINGS_INVALID`, `CAMERA_WARNINGS_UNCONFIRMED` or
`REGISTRY_UNAVAILABLE`. It held on every example — confirming defect 1.2/1.7
(no gate that exists today covers these sets) rather than assuming it.

---

## Leg A — platform-manifest mismatch (defects 1.1, 1.2, 1.7 -> 2.3, 2.7)

### A.1 generic submit path (`create_deployment`)

Hypothesis falsifying example — the verbatim incident case (an explicit
`@example`; the property fails for every generated case too):

```
Failing explicit example: test_generic_path_refuses_jp5_jp6_only_component_on_jp7_device(
    self=<test_deployment_preflight_exploration.TestLegAPlatformMismatch object at 0x71a40b344690>,
    sm_env=<test_deployment_shadow_manager.ShadowManagerEnv object at 0x71a40695cc20>,
    component_name='dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe',
    component_version='1.0.0',
    thing_name='adlink-dlap-701',
)
```

Assertion:

```
AssertionError: the submission was FORWARDED to greengrass_client.create_deployment
instead of being refused pre-submit: status=201 body={"deployment_id":
"dep-5e4631a3-73b8-4173-8923-8721e5811e23", … "is_revision": false,
"superseded_deployment_id": null, "message": "Deployment created successfully"}
```

The forwarded deployment document (component versions only; the full document
with the LogManager / ShadowManager / Nucleus `configurationUpdate` merges is
in the run log):

```
targetArn: arn:aws:iot:us-east-1:123456789012:thing/adlink-dlap-701
components: {"aws.edgeml.dda.LocalServer.arm64JP7": "1.0.26",
             "dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe": "1.0.0",
             "aws.greengrass.LogManager": "2.3.10",
             "aws.greengrass.ShadowManager": "2.3.9",
             "aws.greengrass.Nucleus": "2.12.0"}
deploymentPolicies: {failureHandlingPolicy: ROLLBACK,
                     componentUpdatePolicy: {NOTIFY_COMPONENTS, 60s}}
```

A jp5/jp6-only workflow component was forwarded to a JP7 device, with
`auto_included` reporting three components and `warnings` absent — the portal
said nothing. This is revision 6 of deployment
`1982ad02-c3eb-4803-8b19-2b41f28bc391` reproduced at the submit path.

The workflow's version item records `has_llm_inference` false, so the existing
vLLM architecture gate collects no manifest for it and does not fire (defect
1.2) — confirmed by `assert_no_existing_gate_fired` passing on every example.

### A.2 workflow submit path (`create_workflow_deployment`)

The same fault on the second submit path (2.1 covers both). All generated cases
fail:

```
Failing test case: test_workflow_path_refuses_jp5_jp6_only_component_on_jp7_device(
    # The test always failed when commented parts were varied together.
    self=<test_deployment_preflight_exploration.TestLegAPlatformMismatch object at 0x71a40b344b90>,
    wf_env=<test_workflow_deploy_subscribe_merge_exploration.WorkflowDeployEnv object at 0x71a40695e7b0>,
    component_version='1.0.0',  # or any other generated value
    thing_name='adlink-dlap-1',  # or any other generated value
)
```

```
AssertionError: the submission was FORWARDED … status=201
forwarded_to_greengrass=[{"targetArn": "arn:aws:iot:us-east-1:123456789012:thing/adlink-dlap-1",
  "deploymentName": "portal-deployment-20260910-110922",
  "components": {"dda.workflow.wf-c55bfa542d8b": {"componentVersion": "1.0.0"}},
  "tags": {"dda-portal:managed": "true", …, "dda-portal:workflow-version": "1"}}]
```

The LocalServer floor, plugin, vLLM and camera-binding gates all passed; only
the missing closure validation let a jp5/jp6-only component through to a JP7
device.

---

## Leg B — dependency with no satisfying published version (1.3, 1.6 -> 2.5)

### B.1 zero published versions in BOTH namespaces

Both live pairs are pinned as explicit examples and both fail; hypothesis
reported the second:

```
Failing explicit example: test_dependency_with_zero_published_versions_refused_before_submit(
    self=<test_deployment_preflight_exploration.TestLegBUnresolvableDependency object at 0x71a40b344a50>,
    sm_env=<test_deployment_shadow_manager.ShadowManagerEnv object at 0x71a40686c910>,
    model_name='model-yolo-test-jetson-xavier',
    model_version='2.0.0',
    dependency_name='aws.edgeml.dda.LocalServer.arm64',
    thing_name='adlink-dlap-701',
)
```

(The other explicit example — `model-cookies-segmentation-seghead-jetson-xavier`
v2.0.0 -> `aws.edgeml.dda.LocalServer.arm64JP4` at `>=1.0.0 <2.0.0` — fails
identically.)

```
AssertionError: the submission was FORWARDED … status=201
targetArn: arn:aws:iot:us-east-1:123456789012:thing/adlink-dlap-701
components: {"aws.edgeml.dda.LocalServer.arm64JP7": "1.0.26",
             "model-yolo-test-jetson-xavier": "2.0.0",
             "aws.greengrass.LogManager": "2.3.10",
             "aws.greengrass.ShadowManager": "2.3.9",
             "aws.greengrass.Nucleus": "2.12.0"}
```

The dependency name is seeded nowhere, so
`list_component_versions` answers `[]` under BOTH the account namespace and
`aws` — the fixture asserts that (`gg.published_versions(dependency_name) == []`)
before submitting. This is revision 9
(`4c0f8f84-32e5-4300-86ee-4ab557f6ec83`) reproduced at the submit path: a
deployment that cannot negotiate, forwarded with no finding.

### B.2 versions exist, none satisfies the requirement

```
Failing test case: test_dependency_with_only_non_satisfying_versions_is_distinguished(
    # The test always failed when commented parts were varied together.
    self=<test_deployment_preflight_exploration.TestLegBUnresolvableDependency object at 0x71a40b344cd0>,
    sm_env=<test_deployment_shadow_manager.ShadowManagerEnv object at 0x71a4067a6c40>,
    model_name='model-cookies-segmentation-onnx-1-jetson-xavier-jp7',  # or any other generated value
    model_version='1.0.0',  # or any other generated value
    requirement='>=2.0.0 <3.0.0',  # or any other generated value
    thing_name='adlink-dlap-1',  # or any other generated value
)
```

```
AssertionError: the submission was FORWARDED … status=201
targetArn: arn:aws:iot:us-east-1:123456789012:thing/adlink-dlap-1
components: {"aws.edgeml.dda.LocalServer.arm64JP7": "1.0.26",
             "model-cookies-segmentation-onnx-1-jetson-xavier-jp7": "1.0.0",
             "aws.greengrass.LogManager": "2.3.10",
             "aws.greengrass.ShadowManager": "2.3.9",
             "aws.greengrass.Nucleus": "2.12.0"}
```

`aws.edgeml.dda.LocalServer.arm64JP7` has all 27 published versions
(1.0.0 .. 1.0.26) and none satisfies `>=2.0.0 <3.0.0`. 2.5 requires the
finding to distinguish this from B.1's zero-versions case; today there is no
finding at all.

---

## Leg C — de-selected but still required (1.8, 1.9, 1.10 -> 2.12-2.15)

```
Failing explicit example: test_first_submit_refused_with_the_effective_outcome_stated(
    self=<test_deployment_preflight_exploration.TestLegCDeselectedStillRequired object at 0x71a40b344e10>,
    sm_env=<test_deployment_shadow_manager.ShadowManagerEnv object at 0x71a3f5431a30>,
    model_name='model-vllm-qwen3-5-9b-jetson-xavier-jp7',
    workflow_name='dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8',
    workflow_version='12.0.0',
    thing_name='jetson-thor1',
)
```

```
AssertionError: the submission was FORWARDED … status=201
targetArn: arn:aws:iot:us-east-1:123456789012:thing/jetson-thor1
components: {"aws.edgeml.dda.LocalServer.arm64JP7": "1.0.26",
             "dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8": "12.0.0",
             "aws.greengrass.LogManager": "2.3.10",
             "aws.greengrass.ShadowManager": "2.3.9",
             "aws.greengrass.Nucleus": "2.12.0"}
```

The target's current deployment carried
`model-vllm-qwen3-5-9b-jetson-xavier-jp7` v1.0.0; the submitted set drops it
and keeps `dda.workflow.421f8233-…` v12.0.0, whose published recipe carries
`{"model-vllm-qwen3-5-9b-jetson-xavier-jp7": {"VersionRequirement": ">=0.0.0",
"DependencyType": "HARD"}}` (confirmed verbatim, `evidence.md` §1.1). The
portal forwarded the removal as a plain revision — HTTP 201, no finding, no
acknowledgement step, nothing in the response about the de-selected component
remaining installed as a resolved dependency. Revision 21
(`5fa4482b-c4d5-4f6b-b35e-3adc9ef6c585`, 2026-08-26T20:39:36.933Z) reproduced
at the submit path.

The leg's remaining assertions (unreachable on the unfixed tree because the
first one fires) pin 2.12 (the requiring component with version +
`VersionRequirement` + `DependencyType`), 2.13 (`remains_installed` /
`removed_by_this_deployment` / an `effective_outcome` statement naming the
component) and 2.14/3.14 (a matching specific
`acknowledged_retained_components` re-submit proceeds and submits the
operator's set unchanged, with the de-selected component NOT re-added).

---

## Leg D — one pass, three classes (defect 1.4 -> 2.6, 2.16)

```
Failing explicit example: test_all_three_findings_come_back_in_one_response(
    self=<test_deployment_preflight_exploration.TestLegDSinglePassReporting object at 0x71a40b345590>,
    sm_env=<test_deployment_shadow_manager.ShadowManagerEnv object at 0x71a404f04d10>,
    workflow_a='dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe',
    legacy_model='model-cookies-segmentation-seghead-jetson-xavier',
    deselected_model='model-vllm-qwen3-5-9b-jetson-xavier-jp7',
    workflow_c='dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8',
    thing_name='jetson-thor1',
)
```

```
AssertionError: the submission was FORWARDED … status=201
targetArn: arn:aws:iot:us-east-1:123456789012:thing/jetson-thor1
components: {"aws.edgeml.dda.LocalServer.arm64JP7": "1.0.26",
             "dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe": "1.0.0",
             "model-cookies-segmentation-seghead-jetson-xavier": "2.0.0",
             "dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8": "12.0.0",
             "aws.greengrass.LogManager": "2.3.10",
             "aws.greengrass.ShadowManager": "2.3.9",
             "aws.greengrass.Nucleus": "2.12.0"}
```

One submission carrying an A fault, a B fault and a C finding at once was
forwarded whole. This is exactly defect 1.4's cost: Greengrass reports one
fault per negotiation, so the operator would have to remove the A workflow,
resubmit, hit the B fault, repair it, resubmit — and would still never learn
about the C finding, because that deployment succeeds.

The leg's remaining assertions pin 2.6/2.16 (all three findings in ONE
response, each classified `blocking-invalid` / `acknowledgement-required` /
`unverified`) and that an acknowledgement of the C finding never clears the A
or B findings.

---

## Harness changes made for this suite

`edge-cv-portal/backend/tests/test_workflow_packaging_deployment_integration.py`
— ADDITIVE extension of the shared `FakeGreengrass`, in the same file so every
existing consumer stays green unmodified:

- `seed_component_version` / `seed_component_versions` / `published_versions`:
  a published-component catalog holding recipes (`Manifests[].Platform` plus a
  verbatim `ComponentDependencies`, `null` included) per
  `(namespace, name, version)`.
- `get_component` (recipe as JSON bytes; `ResourceNotFoundException` for an
  unpublished version), `list_component_versions` (+ paginator pages,
  newest-first, EMPTY LIST for the wrong namespace), `describe_component`
  (platforms mirroring the recipe exactly, and no `ComponentDependencies`).
- `component_arn` / `parse_component_arn` / `default_component_namespace`
  module helpers, so account components resolve under the account id and
  `aws.greengrass.*` under `aws`.
- `register_device(platform=…, architecture=…, runtime=…)` and the matching
  additive fields on `get_core_device` — `platform=linux
  architecture=aarch64 runtime=aws_nucleus_classic` and NO `variant`.

Pre-existing consumers verified unchanged: the 39 test files that reference
`FakeGreengrass` ran **383 passed, 3 skipped** both before and after the edit
(`HYPOTHESIS_PROFILE=ci`, same command shape).
