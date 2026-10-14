"""Bug condition exploration suite — deployment-preflight-validation task 2.

Bugfix spec: .kiro/specs/deployment-preflight-validation/
Live evidence:  .kiro/specs/deployment-preflight-validation/evidence.md

# Feature: deployment-preflight-validation, Property 1: bug condition — pre-submit closure validation

Reproduces C(X) at the submit-path level: neither ``create_deployment`` nor
``create_workflow_deployment`` resolves the selected components' transitive
recipe ``ComponentDependencies`` closure, so three faults reach Greengrass
(or reach the device) that the portal already holds enough information to
refuse:

* **Leg A — platform-manifest mismatch** (defects 1.1, 1.2, 1.7 -> 2.3, 2.7).
  ``dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe`` v1.0.0 publishes
  exactly ``{"os":"linux","variant":"arm64_jp5","architecture":"aarch64"}``
  and ``{…"arm64_jp6"…}`` (confirmed first-hand, evidence.md §1.1) and was
  offered for, and accepted onto, the JP7 device ``adlink-dlap-701``
  (deployment ``1982ad02-c3eb-4803-8b19-2b41f28bc391`` revision 6).
* **Leg B — dependency with no satisfying published version** (1.3, 1.6 ->
  2.5). ``model-cookies-segmentation-seghead-jetson-xavier`` v2.0.0 HARD-
  depends on ``aws.edgeml.dda.LocalServer.arm64JP4 >=1.0.0 <2.0.0`` and
  ``model-yolo-test-jetson-xavier`` v2.0.0 on
  ``aws.edgeml.dda.LocalServer.arm64 >=1.0.0 <2.0.0``; both names have ZERO
  published versions in BOTH namespaces (evidence.md §1.1).
* **Leg C — de-selected but still required** (1.8, 1.9, 1.10 -> 2.13, 2.14).
  ``dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8`` v12.0.0 publishes
  ``{"model-vllm-qwen3-5-9b-jetson-xavier-jp7": {"VersionRequirement":
  ">=0.0.0", "DependencyType": "HARD"}, "aws.edgeml.dda.LocalServer.arm64JP7":
  {"VersionRequirement": ">=1.0.0", "DependencyType": "HARD"}}`` (confirmed
  verbatim, evidence.md §1.1), so removing the model at ``jetson-thor1``
  revision 21 (``5fa4482b-c4d5-4f6b-b35e-3adc9ef6c585``) did not remove it.
* **Leg D — one pass** (1.4 -> 2.6, 2.16). A submission carrying an A fault,
  a B fault and a C finding must report all of them in ONE response, each
  classified, and an acknowledgement must not clear the blocking ones.

**THESE TESTS ASSERT THE FIXED BEHAVIOUR AND ARE EXPECTED TO FAIL ON THE
UNFIXED TREE.** Failure IS the bug-condition proof: today every one of these
submissions is accepted and forwarded to Greengrass. Do not weaken the
assertions; the same file, unmodified, validates the fix at task 4.8.

What this suite deliberately does NOT assert (evidence.md §4.1, §5.4):

* No Greengrass error string, no ``FAILED_NO_STATE_CHANGE`` and no
  ``errorStack``. ``GetDeployment`` has no ``reason``/``statusDetails``
  member and every superseded revision now reports ``INACTIVE``, so the
  cloud-side failure text is not verifiable from the allowed read-only set.
  The assertion is the PRE-SUBMIT refusal: nothing reaches
  ``greengrass_client.create_deployment`` (``gg.create_deployment_calls``
  stays empty).
* No six-day retention for Counterexample C. The depending workflow is
  absent from revisions 34 and 36-38, so retention was NOT monotonic. What
  is asserted is the mechanism — a de-selected component still HARD-required
  by a selected one is refused pre-submit — never a device outcome.

Fixture fidelity the legs depend on:

* Every component set carries the auto-included ``aws.greengrass.Nucleus``
  (``{"os":"linux"}``, NO architecture) and ``aws.greengrass.ShadowManager``
  (``{"os":"*"}``), plus ``aws.greengrass.LogManager`` (``{"os":"*"}``) and a
  variant-less aarch64 LocalServer. A matcher that treated an absent
  attribute key or a literal ``"*"`` as a constraint would report those as
  incompatible with every device and refuse EVERY submission (bugfix.md 2.8
  as corrected by evidence.md §5.1), so no leg can pass by accident: each
  asserts that no finding names them.
* ``list_component_versions`` returns an EMPTY LIST for the wrong namespace
  rather than raising (evidence.md §1.1, §5.6): account components are seeded
  under the account namespace and ``aws.greengrass.*`` under ``aws``, so a
  single-namespace resolvability check would flag Nucleus/ShadowManager/
  LogManager — which the legs assert never happens.
* ``get_core_device`` reports ``platform=linux architecture=aarch64`` with NO
  variant for JP5/JP6/JP7 alike (evidence.md §1.3); the device's variant comes
  only from ``DEVICES_TABLE.target_architecture`` by identity map.

Response contract these legs pin (implemented by tasks 4.4/4.5):

    HTTP 409 in the ``_workflow_error`` envelope
    {"error": {"code": <CODE>, "message": str, "details": {"findings": [...]}}}

    code PREFLIGHT_VALIDATION_FAILED        any blocking-invalid finding
    code PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED only acknowledgement-required

    finding: {"kind": "platform-mismatch" | "dependency-unresolvable"
                      | "deselected-still-required",
              "finding_class": "blocking-invalid" | "acknowledgement-required"
                      | "unverified",
              "remediation": str, ...per-kind fields asserted below}

    request field ``acknowledged_retained_components``: [component names]

Harnesses are reused, not rebuilt: ``ShadowManagerEnv``
(test_deployment_shadow_manager.py) for the generic path, ``WorkflowDeployEnv``
(test_workflow_deploy_subscribe_merge_exploration.py) for the workflow path,
and the additive published-component catalog on ``FakeGreengrass``
(test_workflow_packaging_deployment_integration.py).
"""
import json
import sys

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from test_deployment_shadow_manager import ShadowManagerEnv
from test_workflow_deploy_subscribe_merge_exploration import WorkflowDeployEnv
from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, REGION, FakeGreengrass)

# --------------------------------------------------------------------------
# The response contract (task 4.4 / 4.5 implements it; this suite pins it)
# --------------------------------------------------------------------------

CODE_BLOCKING = 'PREFLIGHT_VALIDATION_FAILED'
CODE_ACK_REQUIRED = 'PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED'
ACK_FIELD = 'acknowledged_retained_components'

KIND_PLATFORM = 'platform-mismatch'
KIND_DEPENDENCY = 'dependency-unresolvable'
KIND_DESELECTED = 'deselected-still-required'

CLASS_BLOCKING = 'blocking-invalid'
CLASS_ACK = 'acknowledgement-required'
CLASS_UNVERIFIED = 'unverified'
FINDING_CLASSES = {CLASS_BLOCKING, CLASS_ACK, CLASS_UNVERIFIED}

#: Every pre-submit gate that exists TODAY (3.4). None of them fires on any
#: of these component sets — which is exactly defects 1.1/1.2/1.3: the
#: submission is accepted. A refusal carrying one of these codes would mean
#: an existing gate covers the case and the root-cause analysis is wrong.
EXISTING_GATE_CODES = {
    'VLLM_ARCH_UNSUPPORTED', 'PLUGIN_LIFECYCLE_VIOLATION',
    'PLUGIN_ARCH_UNSUPPORTED', 'INCOMPATIBLE_LOCAL_SERVER',
    'CAMERA_BINDINGS_INVALID', 'CAMERA_WARNINGS_UNCONFIRMED',
    'REGISTRY_UNAVAILABLE',
}

# --------------------------------------------------------------------------
# The account's real platform-manifest shapes (evidence.md §1.1)
# --------------------------------------------------------------------------

PLATFORM_LINUX_ONLY = {'os': 'linux'}                    # Nucleus, Cli
PLATFORM_ANY_OS = {'os': '*'}                            # ShadowManager, LogManager
PLATFORM_AARCH64 = {'os': 'linux', 'architecture': 'aarch64'}   # variant-less
PLATFORM_JP5 = {'os': 'linux', 'variant': 'arm64_jp5',
                'architecture': 'aarch64'}
PLATFORM_JP6 = {'os': 'linux', 'variant': 'arm64_jp6',
                'architecture': 'aarch64'}
PLATFORM_JP7 = {'os': 'linux', 'variant': 'arm64_jp7',
                'architecture': 'aarch64'}

NUCLEUS = 'aws.greengrass.Nucleus'
SHADOW_MANAGER = 'aws.greengrass.ShadowManager'
LOG_MANAGER = 'aws.greengrass.LogManager'
DEVICE_NUCLEUS_VERSION = '2.12.0'
NUCLEUS_VERSIONS = ['2.12.0', '2.14.3']
SHADOW_MANAGER_VERSIONS = ['2.3.9']
LOG_MANAGER_VERSIONS = ['2.3.10']
#: Public components declare a Nucleus VersionRequirement; the device's
#: running 2.12.0 satisfies this one, so version resolution is deterministic.
PUBLIC_NUCLEUS_DEPENDENCY = {
    NUCLEUS: {'VersionRequirement': '>=2.0.0 <2.15.0',
              'DependencyType': 'SOFT'},
}

LOCAL_SERVER_JP7 = 'aws.edgeml.dda.LocalServer.arm64JP7'
#: 27 versions live in the account, 1.0.0 .. 1.0.26 (evidence.md §1.1).
LOCAL_SERVER_JP7_VERSIONS = [f'1.0.{patch}' for patch in range(27)]
LOCAL_SERVER_JP7_LATEST = '1.0.26'
#: LocalServer HARD-depends on Nucleus and ShadowManager, both of which live
#: ONLY in the `aws` namespace — the dual-namespace guard is load-bearing.
LOCAL_SERVER_DEPENDENCIES = {
    NUCLEUS: {'VersionRequirement': '>=2.4.0', 'DependencyType': 'HARD'},
    SHADOW_MANAGER: {'VersionRequirement': '>=2.2.0',
                     'DependencyType': 'HARD'},
}
#: Auto-included / carried components that must NEVER produce a finding.
NEVER_A_FINDING = frozenset({NUCLEUS, SHADOW_MANAGER, LOG_MANAGER,
                             LOCAL_SERVER_JP7})

# Counterexample A, verbatim (evidence.md §1.1)
CE_A_WORKFLOW = 'dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe'
CE_A_VERSION = '1.0.0'
CE_A_DEVICE = 'adlink-dlap-701'

# Counterexample B, verbatim (evidence.md §1.1)
CE_B_SEGHEAD = 'model-cookies-segmentation-seghead-jetson-xavier'
CE_B_SEGHEAD_VERSION = '2.0.0'
CE_B_SEGHEAD_DEPENDENCY = 'aws.edgeml.dda.LocalServer.arm64JP4'
CE_B_YOLO = 'model-yolo-test-jetson-xavier'
CE_B_YOLO_VERSION = '2.0.0'
CE_B_YOLO_DEPENDENCY = 'aws.edgeml.dda.LocalServer.arm64'
CE_B_REQUIREMENT = '>=1.0.0 <2.0.0'
# The resolvable JP7 twin — the negative control (3.3)
CE_B_TWIN = 'model-yolo-test-jetson-xavier-jp7'
CE_B_TWIN_VERSION = '8.0.0'

# Counterexample C, verbatim (evidence.md §1.1, §1.2)
CE_C_MODEL = 'model-vllm-qwen3-5-9b-jetson-xavier-jp7'
CE_C_MODEL_VERSION = '1.0.0'
CE_C_WORKFLOW = 'dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8'
CE_C_WORKFLOW_VERSION = '12.0.0'
CE_C_DEVICE = 'jetson-thor1'
CE_C_DEPENDENCIES = {
    CE_C_MODEL: {'VersionRequirement': '>=0.0.0', 'DependencyType': 'HARD'},
    LOCAL_SERVER_JP7: {'VersionRequirement': '>=1.0.0',
                       'DependencyType': 'HARD'},
}

TARGET_ARCHITECTURE_JP7 = 'arm64_jp7'


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments (and its workflow_guards binding) inside the moto
    mock so module-level boto3 clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


@pytest.fixture
def sm_env(env, deployments, monkeypatch):
    """Generic submit path (create_deployment) through the real handler."""
    return ShadowManagerEnv(env, deployments, monkeypatch)


@pytest.fixture
def wf_env(env, deployments, monkeypatch):
    """Workflow submit path (create_workflow_deployment)."""
    return WorkflowDeployEnv(env, deployments, monkeypatch)


#: Devices-table rows written by ``seed_jp7_device`` during the CURRENT test,
#: as (table, device_id) pairs, so ``_devices_table_isolation`` can delete
#: exactly those rows again — never any row another module seeded.
#:
#: TEARDOWN HYGIENE, NOT A CHANGE OF EXPECTATIONS. ``conftest.aws_stack`` is
#: SESSION-scoped and the devices table is keyed on ``device_id`` ALONE, so a
#: row this suite writes for one of the verbatim incident device names
#: outlives the test and is read by every later module that resolves the same
#: id. ``test_model_status_devices_read.py`` (the model-gpu-fallback-visibility
#: oracle) pins the NO-record rendering of ``jetson-thor1``
#: (``target_architecture: None``) and neither seeds nor cleans that row, so a
#: leaked ``target_architecture: arm64_jp7`` from this suite's Leg C/D
#: ``@example(thing_name=CE_C_DEVICE)`` made that oracle read this suite's
#: state. The incident names (``jetson-thor1``, ``adlink-dlap-701``) are
#: deliberate fidelity and stay exactly as they are — what is fixed is that
#: the rows do not survive the test that wrote them.
_SEEDED_DEVICE_ROWS = []


@pytest.fixture(autouse=True)
def _devices_table_isolation():
    """Delete exactly the devices-table rows this test seeded.

    Tolerant by construction: a teardown must never fail a run, and the row
    may legitimately be gone already (a later example re-seeds the same id).
    No assertion in this file — or in any other — is affected: the deletes
    happen after the test body has finished asserting.
    """
    _SEEDED_DEVICE_ROWS.clear()
    yield
    while _SEEDED_DEVICE_ROWS:
        table, device_id = _SEEDED_DEVICE_ROWS.pop()
        try:
            table.delete_item(Key={'device_id': device_id})
        except Exception:      # pragma: no cover - teardown is best-effort
            pass


# --------------------------------------------------------------------------
# Catalog seeding
# --------------------------------------------------------------------------

def seed_public_catalog(gg):
    """The AWS-managed components every portal deployment auto-includes,
    published under the `aws` namespace with the wildcard-bearing manifests
    the account really publishes (evidence.md §1.1)."""
    gg.seed_component_versions(
        NUCLEUS, NUCLEUS_VERSIONS,
        platforms=[PLATFORM_LINUX_ONLY, {'os': 'darwin'}, {'os': 'windows'}],
        dependencies=None)
    gg.seed_component_versions(
        SHADOW_MANAGER, SHADOW_MANAGER_VERSIONS,
        platforms=[PLATFORM_ANY_OS], dependencies=PUBLIC_NUCLEUS_DEPENDENCY)
    gg.seed_component_versions(
        LOG_MANAGER, LOG_MANAGER_VERSIONS,
        platforms=[PLATFORM_ANY_OS], dependencies=PUBLIC_NUCLEUS_DEPENDENCY)


def seed_local_server(gg):
    """All 27 published JP7 LocalServer versions, variant-less aarch64 —
    JetPack lives in the component NAME, never in the manifest."""
    gg.seed_component_versions(
        LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_VERSIONS,
        platforms=[PLATFORM_AARCH64], dependencies=LOCAL_SERVER_DEPENDENCIES)


def seed_jp7_device(gg, tables, thing_name,
                    target_architecture=TARGET_ARCHITECTURE_JP7):
    """A JP7 core device reporting exactly what the live API reports — no
    variant — with the portal's own record carrying the variant."""
    gg.register_device(thing_name, local_server_version=LOCAL_SERVER_JP7_LATEST,
                       arch='arm64JP7', nucleus_version=DEVICE_NUCLEUS_VERSION,
                       platform='linux', architecture='aarch64',
                       runtime='aws_nucleus_classic')
    item = {'device_id': thing_name}
    if target_architecture:
        item['target_architecture'] = target_architecture
    tables.devices.put_item(Item=item)
    # Track the row so `_devices_table_isolation` removes it on teardown: the
    # devices table is SESSION-scoped and `device_id` is its only key, so a
    # leaked row is read by every later module resolving the same id.
    _SEEDED_DEVICE_ROWS.append((tables.devices, thing_name))


def fresh_catalog(harness, thing_names):
    """A per-example FakeGreengrass with the public catalog, LocalServer and
    the target devices seeded, wired into the harness in place of its own
    (the harness's client factory resolves ``self.gg`` per call)."""
    gg = FakeGreengrass()
    harness.gg = gg
    seed_public_catalog(gg)
    seed_local_server(gg)
    for thing_name in thing_names:
        seed_jp7_device(gg, harness.env.stack.tables, thing_name)
    return gg


def seed_workflow_version_item(harness, component_name, component_version,
                               has_llm_inference=False,
                               packaged_architectures=None,
                               workflow_name='exploration workflow'):
    """The portal-side records of a packaged workflow component: the
    Workflows metadata item and the WorkflowVersions item the deployment
    path resolves (component_version scan-first, D2)."""
    workflow_id = component_name.split('dda.workflow.', 1)[-1]
    major = int(str(component_version).split('.')[0])
    tables = harness.env.stack.tables
    tables.workflows.put_item(Item={
        'workflow_id': workflow_id,
        'usecase_id': harness.usecase_id,
        'name': workflow_name,
        'latest_version': major,
        'created_at': 1,
    })
    item = {
        'workflow_id': workflow_id,
        'version': major,
        'validation_status': {'status': 'passed'},
        'component_arn': (f'arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:'
                          f'components:{component_name}:versions:'
                          f'{component_version}'),
        'component_version': component_version,
    }
    if has_llm_inference:
        item['has_llm_inference'] = True
        item['packaged_architectures'] = list(
            packaged_architectures or [TARGET_ARCHITECTURE_JP7])
    tables.versions.put_item(Item=item)
    return workflow_id


def thing_arn(thing_name):
    return f'arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}'


def component_entry(name, version):
    return {'component_name': name, 'component_version': version}


# --------------------------------------------------------------------------
# Assertion helpers
# --------------------------------------------------------------------------

def error_code(body):
    return ((body or {}).get('error') or {}).get('code')


def findings(body):
    details = ((body or {}).get('error') or {}).get('details') or {}
    return details.get('findings') or []


def findings_of_kind(body, kind):
    return [f for f in findings(body) if f.get('kind') == kind]


def platform_set(platforms):
    """Platform blocks as an order-insensitive comparable set."""
    return {frozenset((p or {}).items()) for p in (platforms or [])}


def describe(status, body, gg):
    """Failure context: the status, the response body and the deployment
    document that WAS forwarded (the counterexample on the unfixed tree)."""
    return (f'status={status} body={json.dumps(body, default=str)} '
            f'forwarded_to_greengrass={json.dumps(gg.create_deployment_calls, default=str)}')


def assert_no_existing_gate_fired(status, body, gg):
    """Defect 1.2 / 1.7: no gate that exists today covers these component
    sets. A refusal must come from the NEW preflight validation, never from
    the vLLM, plugin, LocalServer-floor or camera gates."""
    code = error_code(body)
    assert code not in EXISTING_GATE_CODES, (
        f'an EXISTING pre-submit gate ({code}) refused this submission — the '
        f'root-cause analysis needs revisiting: {describe(status, body, gg)}')


def assert_refused_before_submit(status, body, gg, expected_code):
    """The load-bearing assertion (evidence.md §4.1): nothing reaches
    Greengrass, and the refusal carries the expected preflight code."""
    assert gg.create_deployment_calls == [], (
        'the submission was FORWARDED to greengrass_client.create_deployment '
        'instead of being refused pre-submit: '
        f'{describe(status, body, gg)}')
    assert status == 409, (
        f'expected a 409 pre-submit refusal: {describe(status, body, gg)}')
    assert error_code(body) == expected_code, (
        f'expected error code {expected_code}: {describe(status, body, gg)}')


def assert_every_finding_classified(body):
    """2.16: every finding carries exactly one of the three classes."""
    for finding in findings(body):
        assert finding.get('finding_class') in FINDING_CLASSES, (
            f'finding carries no valid finding_class: {finding!r}')


def assert_wildcard_components_never_flagged(body):
    """2.8 as corrected by evidence.md §5.1: an absent attribute key, a
    literal "*" value and a variant-less aarch64 manifest are wildcards, so
    the auto-included AWS components and LocalServer are never findings. A
    matcher that got this wrong would refuse EVERY submission."""
    flagged = sorted({f.get('component_name') for f in findings(body)}
                     & NEVER_A_FINDING)
    assert not flagged, (
        'wildcard-bearing components were reported as findings '
        f'({flagged}) — the matcher would refuse every real deployment: '
        f'{json.dumps(findings(body), default=str)}')


def assert_device_platform_reported(entry, thing_name):
    """2.3/2.7: the DEVICE's platform is reported as the device's, os and
    architecture from get_core_device and variant from the portal record."""
    assert entry.get('thing_name') == thing_name, entry
    platform = entry.get('platform') or {}
    assert platform.get('os') == 'linux', entry
    assert platform.get('architecture') == 'aarch64', entry
    assert platform.get('variant') == TARGET_ARCHITECTURE_JP7, entry


# --------------------------------------------------------------------------
# Strategies (scoped to the incident shapes)
# --------------------------------------------------------------------------

def jp7_thing_names():
    return st.builds(
        lambda stem, index: f'{stem}-{index}',
        st.sampled_from(['adlink-dlap', 'jetson-thor', 'jp7-orin', 'orin-agx',
                         'dlap']),
        st.integers(min_value=1, max_value=999))


def workflow_component_names():
    return st.uuids().map(lambda value: f'dda.workflow.{value}')


def component_majors():
    return st.integers(min_value=1, max_value=24).map(
        lambda major: f'{major}.0.0')


def legacy_model_component_names():
    return st.builds(
        lambda slug, index: f'model-{slug}-{index}-jetson-xavier',
        st.sampled_from(['cookies-segmentation-seghead', 'yolo-test',
                         'blue-plate', 'widget-defect']),
        st.integers(min_value=1, max_value=999))


def jp7_model_component_names():
    return st.builds(
        lambda slug, index: f'model-{slug}-{index}-jetson-xavier-jp7',
        st.sampled_from(['cookies-segmentation-onnx', 'yolo-test',
                         'blue-plate']),
        st.integers(min_value=1, max_value=999))


def dead_dependency_names():
    """Component names with ZERO published versions in BOTH namespaces —
    the two live ones plus the other retired LocalServer names."""
    return st.sampled_from([CE_B_SEGHEAD_DEPENDENCY, CE_B_YOLO_DEPENDENCY,
                            'aws.edgeml.dda.LocalServer',
                            'aws.edgeml.dda.LocalServer.arm64JP3'])


def unsatisfiable_requirements():
    """Requirements no published JP7 LocalServer version (1.0.0 .. 1.0.26)
    satisfies."""
    return st.sampled_from(['>=2.0.0 <3.0.0', '>=1.1.0', '>=1.0.27 <2.0.0',
                            '=2.5.0', '>=1.0.27'])


# ==========================================================================
# Leg A — platform-manifest mismatch (defects 1.1, 1.2, 1.7 -> 2.3, 2.7)
# ==========================================================================

class TestLegAPlatformMismatch:
    """**Validates: Requirements 2.2, 2.3, 2.7, 2.8** (defects 1.1, 1.2, 1.7).

    A component version whose every published manifest names a JetPack other
    than the target device's cannot deploy there. The portal holds both
    facts before submit — the recipe manifests and the device's platform —
    and must refuse, naming the component, its version, the platforms it
    ACTUALLY claims, the device and the device's platform."""

    @given(component_name=workflow_component_names(),
           component_version=component_majors(),
           thing_name=jp7_thing_names())
    @example(component_name=CE_A_WORKFLOW, component_version=CE_A_VERSION,
             thing_name=CE_A_DEVICE)
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_generic_path_refuses_jp5_jp6_only_component_on_jp7_device(
            self, sm_env, component_name, component_version, thing_name):
        """Counterexample A's shape on ``create_deployment``: the submission
        is refused before Greengrass, with a blocking-invalid finding naming
        the component, its jp5/jp6-only claim and the JP7 device."""
        gg = fresh_catalog(sm_env, [thing_name])
        gg.seed_component_version(component_name, component_version,
                                  platforms=[PLATFORM_JP5, PLATFORM_JP6],
                                  dependencies=None)
        # has_llm_inference false — the existing vLLM gate collects no
        # manifest for this component and demonstrably does not fire (1.2).
        seed_workflow_version_item(sm_env, component_name, component_version,
                                   has_llm_inference=False)

        status, body = sm_env.deploy_components(
            [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
             component_entry(component_name, component_version)],
            target_devices=[thing_name],
            rollout_config={'auto_rollback': True, 'timeout_seconds': 60})

        assert_no_existing_gate_fired(status, body, gg)
        assert_refused_before_submit(status, body, gg, CODE_BLOCKING)
        assert_every_finding_classified(body)
        assert_wildcard_components_never_flagged(body)

        matches = [f for f in findings_of_kind(body, KIND_PLATFORM)
                   if f.get('component_name') == component_name]
        assert len(matches) == 1, (
            f'expected exactly one platform-mismatch finding for '
            f'{component_name}: {json.dumps(findings(body), default=str)}')
        finding = matches[0]
        assert finding['finding_class'] == CLASS_BLOCKING, finding
        assert finding.get('component_version') == component_version, finding
        # 2.3: the platforms the component version ACTUALLY claims.
        assert platform_set(finding.get('claimed_platforms')) == platform_set(
            [PLATFORM_JP5, PLATFORM_JP6]), finding
        # 2.7: the device's platform is never presented as the component's
        # claim — the clause that misdirected the incident's first diagnosis.
        assert TARGET_ARCHITECTURE_JP7 not in json.dumps(
            finding.get('claimed_platforms'), default=str), finding
        devices = finding.get('devices') or []
        assert len(devices) == 1, finding
        assert_device_platform_reported(devices[0], thing_name)
        assert finding.get('remediation'), finding

    @given(component_version=component_majors(),
           thing_name=jp7_thing_names())
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_workflow_path_refuses_jp5_jp6_only_component_on_jp7_device(
            self, wf_env, component_version, thing_name):
        """The same fault on ``create_workflow_deployment`` (2.1 covers BOTH
        submit paths). The LocalServer floor, plugin, vLLM and camera gates
        all pass; only the missing closure validation lets it through."""
        gg = fresh_catalog(wf_env, [thing_name])
        workflow_id = wf_env.seed_subscribing_workflow(
            version=int(component_version.split('.')[0]), topics=None)
        component_name = f'dda.workflow.{workflow_id}'
        gg.seed_component_version(component_name, component_version,
                                  platforms=[PLATFORM_JP5, PLATFORM_JP6],
                                  dependencies=None)

        status, body = wf_env.deploy(workflow_id, target_devices=[thing_name])

        assert_no_existing_gate_fired(status, body, gg)
        assert_refused_before_submit(status, body, gg, CODE_BLOCKING)
        assert_wildcard_components_never_flagged(body)
        matches = [f for f in findings_of_kind(body, KIND_PLATFORM)
                   if f.get('component_name') == component_name]
        assert len(matches) == 1, (
            'expected a platform-mismatch finding on the workflow submit '
            f'path: {json.dumps(findings(body), default=str)}')
        assert platform_set(matches[0].get('claimed_platforms')) == platform_set(
            [PLATFORM_JP5, PLATFORM_JP6]), matches[0]
        assert_device_platform_reported(
            (matches[0].get('devices') or [{}])[0], thing_name)

    def test_control_variantless_aarch64_component_still_deploys(self, sm_env):
        """CONTROL (3.2, 2.8) — passes on the unfixed tree AND must keep
        passing after the fix: a variant-less aarch64 manifest is universal
        (107 of the account's 148 aarch64 manifests), so a JP7 device takes
        it with no finding and no extra step."""
        gg = fresh_catalog(sm_env, [CE_C_DEVICE])
        gg.seed_component_version(CE_B_TWIN, CE_B_TWIN_VERSION,
                                  platforms=[PLATFORM_AARCH64],
                                  dependencies={LOCAL_SERVER_JP7: {
                                      'VersionRequirement': CE_B_REQUIREMENT,
                                      'DependencyType': 'HARD'}})

        status, body = sm_env.deploy_components(
            [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
             component_entry(CE_B_TWIN, CE_B_TWIN_VERSION)],
            target_devices=[CE_C_DEVICE])

        assert status == 201, describe(status, body, gg)
        [call] = gg.create_deployment_calls
        assert CE_B_TWIN in call['components']


# ==========================================================================
# Leg B — unresolvable dependency (defects 1.3, 1.6 -> 2.4, 2.5)
# ==========================================================================

class TestLegBUnresolvableDependency:
    """**Validates: Requirements 2.4, 2.5** (defects 1.3, 1.6).

    A selected component whose transitive closure names a component with no
    published version satisfying its ``VersionRequirement`` cannot deploy
    anywhere. Greengrass reports this with a reason naming NO component
    (revision 9, ``4c0f8f84-32e5-4300-86ee-4ab557f6ec83``); the portal must
    name the depended-on component, the requirement, and who requires it."""

    @given(model_name=legacy_model_component_names(),
           model_version=component_majors(),
           dependency_name=dead_dependency_names(),
           thing_name=jp7_thing_names())
    @example(model_name=CE_B_SEGHEAD, model_version=CE_B_SEGHEAD_VERSION,
             dependency_name=CE_B_SEGHEAD_DEPENDENCY, thing_name=CE_A_DEVICE)
    @example(model_name=CE_B_YOLO, model_version=CE_B_YOLO_VERSION,
             dependency_name=CE_B_YOLO_DEPENDENCY, thing_name=CE_A_DEVICE)
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_dependency_with_zero_published_versions_refused_before_submit(
            self, sm_env, model_name, model_version, dependency_name,
            thing_name):
        """Counterexample B's shape: a HARD dependency on a name with an
        EMPTY published-version list in BOTH namespaces is refused pre-submit
        and reported as zero-versions rather than non-satisfying (2.5)."""
        gg = fresh_catalog(sm_env, [thing_name])
        gg.seed_component_version(
            model_name, model_version, platforms=[PLATFORM_AARCH64],
            dependencies={dependency_name: {
                'VersionRequirement': CE_B_REQUIREMENT,
                'DependencyType': 'HARD'}})
        # The depended-on name is deliberately NOT seeded: zero published
        # versions under the account namespace AND under `aws`.
        assert gg.published_versions(dependency_name) == []

        status, body = sm_env.deploy_components(
            [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
             component_entry(model_name, model_version)],
            target_devices=[thing_name])

        assert_no_existing_gate_fired(status, body, gg)
        assert_refused_before_submit(status, body, gg, CODE_BLOCKING)
        assert_every_finding_classified(body)
        assert_wildcard_components_never_flagged(body)

        matches = [f for f in findings_of_kind(body, KIND_DEPENDENCY)
                   if f.get('component_name') == dependency_name]
        assert len(matches) == 1, (
            f'expected exactly one resolvability finding for '
            f'{dependency_name}: {json.dumps(findings(body), default=str)}')
        finding = matches[0]
        assert finding['finding_class'] == CLASS_BLOCKING, finding
        assert finding.get('version_requirement') == CE_B_REQUIREMENT, finding
        # 2.5: which selected component and version requires it.
        required_by = finding.get('required_by') or []
        assert any(entry.get('component_name') == model_name
                   and entry.get('component_version') == model_version
                   for entry in required_by), finding
        # 2.5: zero published versions, not merely non-satisfying ones.
        assert finding.get('has_published_versions') is False, finding
        assert (finding.get('published_versions') or []) == [], finding
        assert finding.get('remediation'), finding

    @given(model_name=jp7_model_component_names(),
           model_version=component_majors(),
           requirement=unsatisfiable_requirements(),
           thing_name=jp7_thing_names())
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_dependency_with_only_non_satisfying_versions_is_distinguished(
            self, sm_env, model_name, model_version, requirement, thing_name):
        """The second shape 2.5 requires distinguishing: the depended-on name
        HAS published versions (all 27 JP7 LocalServer versions) but none
        satisfies the requirement."""
        gg = fresh_catalog(sm_env, [thing_name])
        gg.seed_component_version(
            model_name, model_version, platforms=[PLATFORM_AARCH64],
            dependencies={LOCAL_SERVER_JP7: {
                'VersionRequirement': requirement,
                'DependencyType': 'HARD'}})
        assert gg.published_versions(LOCAL_SERVER_JP7)

        status, body = sm_env.deploy_components(
            [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
             component_entry(model_name, model_version)],
            target_devices=[thing_name])

        assert_no_existing_gate_fired(status, body, gg)
        assert_refused_before_submit(status, body, gg, CODE_BLOCKING)
        assert_every_finding_classified(body)

        matches = [f for f in findings_of_kind(body, KIND_DEPENDENCY)
                   if f.get('component_name') == LOCAL_SERVER_JP7
                   and f.get('version_requirement') == requirement]
        assert len(matches) == 1, (
            'expected a resolvability finding for the non-satisfying '
            f'requirement {requirement}: '
            f'{json.dumps(findings(body), default=str)}')
        finding = matches[0]
        assert finding['finding_class'] == CLASS_BLOCKING, finding
        # The distinction 2.5 demands: versions exist, none satisfy.
        assert finding.get('has_published_versions') is True, finding
        assert finding.get('published_versions'), finding
        assert any(entry.get('component_name') == model_name
                   for entry in finding.get('required_by') or []), finding

    def test_control_jetpack_matched_twin_resolves_and_submits(self, sm_env):
        """CONTROL (3.3, 2.4) — passes on the unfixed tree AND must keep
        passing after the fix: ``model-yolo-test-jetson-xavier-jp7`` v8.0.0
        HARD-depends on ``…LocalServer.arm64JP7 >=1.0.0 <2.0.0``, which has
        27 published versions, so the submission goes through untouched. The
        AWS-managed names in the closure resolve under the `aws` namespace
        only — a single-namespace check would refuse this."""
        gg = fresh_catalog(sm_env, [CE_A_DEVICE])
        gg.seed_component_version(
            CE_B_TWIN, CE_B_TWIN_VERSION, platforms=[PLATFORM_AARCH64],
            dependencies={LOCAL_SERVER_JP7: {
                'VersionRequirement': CE_B_REQUIREMENT,
                'DependencyType': 'HARD'}})

        status, body = sm_env.deploy_components(
            [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
             component_entry(CE_B_TWIN, CE_B_TWIN_VERSION)],
            target_devices=[CE_A_DEVICE])

        assert status == 201, describe(status, body, gg)
        [call] = gg.create_deployment_calls
        assert set(call['components']) >= {
            LOCAL_SERVER_JP7, CE_B_TWIN, NUCLEUS, SHADOW_MANAGER, LOG_MANAGER}


# ==========================================================================
# Leg C — de-selected but still required (1.8-1.10 -> 2.12, 2.13, 2.14, 2.15)
# ==========================================================================

class TestLegCDeselectedStillRequired:
    """**Validates: Requirements 2.11, 2.12, 2.13, 2.14, 2.15** (defects 1.8,
    1.9, 1.10).

    Greengrass ACCEPTS this deployment and it does not do what the component
    list says: the de-selected component stays installed as a resolved
    non-root dependency of a component that remained selected. The mechanism
    — not any device outcome — is what is asserted (evidence.md §5.4: the
    depending workflow was absent from revisions 34 and 36-38, so the live
    retention was NOT monotonic)."""

    @given(model_name=jp7_model_component_names(),
           workflow_name=workflow_component_names(),
           workflow_version=component_majors(),
           thing_name=jp7_thing_names())
    @example(model_name=CE_C_MODEL, workflow_name=CE_C_WORKFLOW,
             workflow_version=CE_C_WORKFLOW_VERSION, thing_name=CE_C_DEVICE)
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_first_submit_refused_with_the_effective_outcome_stated(
            self, sm_env, model_name, workflow_name, workflow_version,
            thing_name):
        """Counterexample C's shape: the operator drops the model and keeps
        the workflow whose published recipe HARD-requires it. (a) the FIRST
        submit is refused with nothing sent to Greengrass, (b) the finding
        names the de-selected component and every selected component
        requiring it with version + VersionRequirement + DependencyType,
        (c) the effective outcome is stated explicitly, (d) a matching
        specific acknowledgement submits the operator's set UNCHANGED."""
        gg = fresh_catalog(sm_env, [thing_name])
        gg.seed_component_version(
            model_name, CE_C_MODEL_VERSION, platforms=[PLATFORM_AARCH64],
            dependencies={LOCAL_SERVER_JP7: {
                'VersionRequirement': '>=1.0.0', 'DependencyType': 'HARD'}})
        # The workflow's recipe carries the deliberately unpinned >=0.0.0
        # HARD entry workflow_packaging.model_component_dependencies emits
        # by design (3.12), plus the LocalServer edge.
        gg.seed_component_version(
            workflow_name, workflow_version, platforms=[PLATFORM_AARCH64],
            dependencies={
                model_name: {'VersionRequirement': '>=0.0.0',
                             'DependencyType': 'HARD'},
                LOCAL_SERVER_JP7: {'VersionRequirement': '>=1.0.0',
                                   'DependencyType': 'HARD'}})
        seed_workflow_version_item(sm_env, workflow_name, workflow_version,
                                   has_llm_inference=True,
                                   packaged_architectures=[
                                       TARGET_ARCHITECTURE_JP7])
        # The target's current deployment carries BOTH (revision 20's shape).
        gg.seed_deployment(thing_arn(thing_name), {
            model_name: {'componentVersion': CE_C_MODEL_VERSION},
            workflow_name: {'componentVersion': workflow_version},
            LOCAL_SERVER_JP7: {'componentVersion': LOCAL_SERVER_JP7_LATEST},
            NUCLEUS: {'componentVersion': DEVICE_NUCLEUS_VERSION},
            SHADOW_MANAGER: {'componentVersion': SHADOW_MANAGER_VERSIONS[0]},
            LOG_MANAGER: {'componentVersion': LOG_MANAGER_VERSIONS[0]},
        }, name=f'ssh-tunnel-on-{thing_name}')

        operator_set = [
            component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
            component_entry(workflow_name, workflow_version),
        ]

        status, body = sm_env.deploy_components(
            operator_set, target_devices=[thing_name])

        # (a) refused on FIRST submit, nothing forwarded (2.14)
        assert_no_existing_gate_fired(status, body, gg)
        assert_refused_before_submit(status, body, gg, CODE_ACK_REQUIRED)
        assert_every_finding_classified(body)
        assert_wildcard_components_never_flagged(body)

        matches = [f for f in findings_of_kind(body, KIND_DESELECTED)
                   if f.get('component_name') == model_name]
        assert len(matches) == 1, (
            f'expected exactly one de-selected-still-required finding for '
            f'{model_name}: {json.dumps(findings(body), default=str)}')
        finding = matches[0]
        assert finding['finding_class'] == CLASS_ACK, finding

        # (b) every selected component requiring it, named precisely enough
        # to de-select in the same edit (2.12, 2.15)
        required_by = finding.get('required_by') or []
        depender = [entry for entry in required_by
                    if entry.get('component_name') == workflow_name]
        assert len(depender) == 1, finding
        assert depender[0].get('component_version') == workflow_version, finding
        assert depender[0].get('version_requirement') == '>=0.0.0', finding
        assert depender[0].get('dependency_type') == 'HARD', finding

        # (c) the effective outcome, stated before submit (2.13)
        assert finding.get('remains_installed') is True, finding
        assert finding.get('removed_by_this_deployment') is False, finding
        outcome = finding.get('effective_outcome') or ''
        assert isinstance(outcome, str) and model_name in outcome, finding

        # (d) a matching SPECIFIC acknowledgement proceeds, submitting the
        # operator's component set unchanged (2.14, 3.14)
        ack_status, ack_body = sm_env.deploy_components(
            operator_set, target_devices=[thing_name],
            **{ACK_FIELD: [model_name]})

        assert ack_status == 201, describe(ack_status, ack_body, gg)
        [call] = gg.create_deployment_calls
        submitted = call['components']
        assert model_name not in submitted, (
            'the validation re-added the de-selected component (3.14): '
            f'{sorted(submitted)}')
        assert submitted[workflow_name]['componentVersion'] == workflow_version
        assert (submitted[LOCAL_SERVER_JP7]['componentVersion']
                == LOCAL_SERVER_JP7_LATEST)

    def test_control_removal_with_no_remaining_dependant_submits(self, sm_env):
        """CONTROL (3.11) — passes on the unfixed tree AND must keep passing
        after the fix: revision 53's shape, where the model and the depending
        workflow are removed together, needs no acknowledgement and produces
        no finding."""
        gg = fresh_catalog(sm_env, [CE_C_DEVICE])
        gg.seed_component_version(
            CE_C_MODEL, CE_C_MODEL_VERSION, platforms=[PLATFORM_AARCH64],
            dependencies={LOCAL_SERVER_JP7: {
                'VersionRequirement': '>=1.0.0', 'DependencyType': 'HARD'}})
        gg.seed_component_version(
            CE_C_WORKFLOW, CE_C_WORKFLOW_VERSION,
            platforms=[PLATFORM_AARCH64], dependencies=CE_C_DEPENDENCIES)
        gg.seed_deployment(thing_arn(CE_C_DEVICE), {
            CE_C_MODEL: {'componentVersion': CE_C_MODEL_VERSION},
            CE_C_WORKFLOW: {'componentVersion': CE_C_WORKFLOW_VERSION},
            LOCAL_SERVER_JP7: {'componentVersion': LOCAL_SERVER_JP7_LATEST},
        }, name='thor1-remove-qwen-folder-test')

        status, body = sm_env.deploy_components(
            [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST)],
            target_devices=[CE_C_DEVICE])

        assert status == 201, describe(status, body, gg)
        [call] = gg.create_deployment_calls
        assert CE_C_MODEL not in call['components']
        assert CE_C_WORKFLOW not in call['components']


# ==========================================================================
# Leg D — one pass, three classes (defect 1.4 -> 2.6, 2.16)
# ==========================================================================

class TestLegDSinglePassReporting:
    """**Validates: Requirements 2.6, 2.16** (defect 1.4).

    Greengrass reports exactly ONE fault per negotiation, so a deployment
    carrying several must be fixed by blind serial iteration. The portal
    holds the whole closure and must report every finding in ONE response,
    each classified — and an acknowledgement must never clear a
    blocking-invalid finding."""

    @given(workflow_a=workflow_component_names(),
           legacy_model=legacy_model_component_names(),
           deselected_model=jp7_model_component_names(),
           workflow_c=workflow_component_names(),
           thing_name=jp7_thing_names())
    @example(workflow_a=CE_A_WORKFLOW, legacy_model=CE_B_SEGHEAD,
             deselected_model=CE_C_MODEL, workflow_c=CE_C_WORKFLOW,
             thing_name=CE_C_DEVICE)
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_all_three_findings_come_back_in_one_response(
            self, sm_env, workflow_a, legacy_model, deselected_model,
            workflow_c, thing_name):
        """One submission carrying an A fault, a B fault and a C finding: all
        three come back in ONE response, each classified, and an
        acknowledgement of the C finding does NOT clear A or B."""
        gg = fresh_catalog(sm_env, [thing_name])
        # A: jp5/jp6-only manifests on a JP7 device
        gg.seed_component_version(workflow_a, CE_A_VERSION,
                                  platforms=[PLATFORM_JP5, PLATFORM_JP6],
                                  dependencies=None)
        seed_workflow_version_item(sm_env, workflow_a, CE_A_VERSION,
                                   has_llm_inference=False)
        # B: HARD dependency on a name with zero published versions
        gg.seed_component_version(
            legacy_model, CE_B_SEGHEAD_VERSION, platforms=[PLATFORM_AARCH64],
            dependencies={CE_B_SEGHEAD_DEPENDENCY: {
                'VersionRequirement': CE_B_REQUIREMENT,
                'DependencyType': 'HARD'}})
        # C: de-selected model still HARD-required by a selected workflow
        gg.seed_component_version(
            deselected_model, CE_C_MODEL_VERSION,
            platforms=[PLATFORM_AARCH64],
            dependencies={LOCAL_SERVER_JP7: {
                'VersionRequirement': '>=1.0.0', 'DependencyType': 'HARD'}})
        gg.seed_component_version(
            workflow_c, CE_C_WORKFLOW_VERSION, platforms=[PLATFORM_AARCH64],
            dependencies={
                deselected_model: {'VersionRequirement': '>=0.0.0',
                                   'DependencyType': 'HARD'},
                LOCAL_SERVER_JP7: {'VersionRequirement': '>=1.0.0',
                                   'DependencyType': 'HARD'}})
        seed_workflow_version_item(sm_env, workflow_c, CE_C_WORKFLOW_VERSION,
                                   has_llm_inference=True,
                                   packaged_architectures=[
                                       TARGET_ARCHITECTURE_JP7])
        gg.seed_deployment(thing_arn(thing_name), {
            deselected_model: {'componentVersion': CE_C_MODEL_VERSION},
            workflow_c: {'componentVersion': CE_C_WORKFLOW_VERSION},
            LOCAL_SERVER_JP7: {'componentVersion': LOCAL_SERVER_JP7_LATEST},
        }, name=f'ssh-tunnel-on-{thing_name}')

        operator_set = [
            component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
            component_entry(workflow_a, CE_A_VERSION),
            component_entry(legacy_model, CE_B_SEGHEAD_VERSION),
            component_entry(workflow_c, CE_C_WORKFLOW_VERSION),
        ]

        status, body = sm_env.deploy_components(
            operator_set, target_devices=[thing_name])

        assert_no_existing_gate_fired(status, body, gg)
        # A blocking-invalid finding is present, so the response is a
        # blocking refusal regardless of the C finding (2.16).
        assert_refused_before_submit(status, body, gg, CODE_BLOCKING)
        assert_every_finding_classified(body)
        assert_wildcard_components_never_flagged(body)

        # 2.6: every fault in ONE response — no serial rediscovery.
        platform_findings = [f for f in findings_of_kind(body, KIND_PLATFORM)
                             if f.get('component_name') == workflow_a]
        dependency_findings = [
            f for f in findings_of_kind(body, KIND_DEPENDENCY)
            if f.get('component_name') == CE_B_SEGHEAD_DEPENDENCY]
        deselected_findings = [
            f for f in findings_of_kind(body, KIND_DESELECTED)
            if f.get('component_name') == deselected_model]
        assert platform_findings and dependency_findings and deselected_findings, (
            'the response does not carry all three findings in one pass: '
            f'{json.dumps(findings(body), default=str)}')
        # 2.16: each classified as exactly one of the three classes.
        assert platform_findings[0]['finding_class'] == CLASS_BLOCKING
        assert dependency_findings[0]['finding_class'] == CLASS_BLOCKING
        assert deselected_findings[0]['finding_class'] == CLASS_ACK

        # An acknowledgement can never bypass a blocking-invalid finding
        # (2.14, 2.16): the same submission with the C acknowledgement is
        # refused again, still reporting A and B, still sending nothing.
        ack_status, ack_body = sm_env.deploy_components(
            operator_set, target_devices=[thing_name],
            **{ACK_FIELD: [deselected_model]})

        assert_refused_before_submit(ack_status, ack_body, gg, CODE_BLOCKING)
        assert [f for f in findings_of_kind(ack_body, KIND_PLATFORM)
                if f.get('component_name') == workflow_a], (
            'the acknowledgement cleared the platform finding: '
            f'{json.dumps(findings(ack_body), default=str)}')
        assert [f for f in findings_of_kind(ack_body, KIND_DEPENDENCY)
                if f.get('component_name') == CE_B_SEGHEAD_DEPENDENCY], (
            'the acknowledgement cleared the resolvability finding: '
            f'{json.dumps(findings(ack_body), default=str)}')
