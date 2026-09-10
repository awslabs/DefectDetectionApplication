"""Fix-checking property suites — deployment-preflight-validation task 5.

Bugfix spec:   .kiro/specs/deployment-preflight-validation/
Live evidence: .kiro/specs/deployment-preflight-validation/evidence.md
Implementation: edge-cv-portal/backend/functions/deployment_preflight.py
                plus its two call sites in deployments.py
                (``check_deployment_preflight``).

Four properties of the FIXED tree, one per test, run at ``HYPOTHESIS_PROFILE=ci``
(100 examples; the profile supplies ``max_examples`` — nothing here hardcodes
it):

* **Property 3 — fail-open under any greengrassv2 exception** (task 5.1,
  requirements 2.9/2.10). No exception from any call in the validation path,
  and no malformed recipe body or broken paginator, can turn a submission that
  would otherwise be permitted into a refusal or a 5xx. This is the difference
  between a validation that helps and one that takes deployment submission down
  the moment Greengrass throttles.
* **Property 4 — every finding classified exactly once in one pass** (task 5.2,
  requirements 2.6/2.9/2.10/2.16).
* **Property 5 — a specific acknowledgement can never authorize a different
  finding** (task 5.3, requirement 2.14).
* **Property 6 — each recipe is fetched at most once per validation** (task
  5.4, requirements 2.1/2.11).

Plus one property added by the task-5 dispatch to close a real coverage gap in
the (immutable) preservation oracle:

* **Property 7 — the wildcard matcher has teeth on a fully reported device
  platform** (requirements 2.2, 2.3, 2.8 as corrected by evidence.md §5.1).
  **This closes a gap the task-4 implementer found in
  ``test_deployment_preflight_preservation.py``, which is immutable and must
  not be edited**: every ``register_device`` call in that oracle omits
  ``platform=``/``architecture=``, so ``FakeGreengrass.get_core_device`` raises
  ``ResourceNotFoundException`` on every endpoint-level preservation test. The
  device platform therefore resolves to ``{}`` (or to ``{'variant': …}`` alone
  when a Devices-table record exists), every manifest that constrains ``os`` or
  ``architecture`` is UNDECIDABLE, and several of that oracle's platform claims
  — including the variant-less-aarch64-stays-universal claim (3.2) and the
  unconstrained-manifest claim (2.8) — pass via **FAIL-OPEN** rather than via
  the wildcard matcher actually deciding. Property 7 pins the matcher's teeth
  independently: with a FULLY reported device platform (``platform=linux``,
  ``architecture=aarch64`` from ``get_core_device`` plus
  ``DEVICES_TABLE.target_architecture=arm64_jp7`` supplying the ``variant``),
  all four wildcard forms the account really publishes are SATISFIED, and a
  jp5/jp6-only manifest is REFUSED for that jp7 device with the device's os,
  architecture AND variant all present in the finding — which is only possible
  if the matcher decided rather than failed open.

Harnesses are reused, not rebuilt: ``ShadowManagerEnv``
(test_deployment_shadow_manager.py) for ``create_deployment``,
``WorkflowDeployEnv`` (test_workflow_deploy_subscribe_merge_exploration.py) for
``create_workflow_deployment``, and the additive published-component catalog on
``FakeGreengrass`` (test_workflow_packaging_deployment_integration.py:
``seed_component_version`` / ``seed_component_versions`` / ``get_component`` /
``list_component_versions`` / ``describe_component`` /
``register_device(platform=, architecture=, runtime=)``).

Two levels are exercised deliberately:

* the **pure module** (``deployment_preflight`` over injected callables), where
  the finding list and its classification are directly observable; and
* the **endpoint** (the real ``deployments.handler``), where only the refusal
  or the forwarded deployment document is observable.

Where a claim is only observable at one level it is asserted there and not
faked at the other — in particular the 201 response body carries NO findings
field (unverified findings are logged, not returned), so "the affected
component is reported unverified" is asserted against ``outcome.findings`` at
the module level while the endpoint asserts the submission is permitted and
produces no 5xx.
"""
import copy
import json
import sys

import pytest
from botocore.exceptions import ClientError
from hypothesis import (HealthCheck, assume, given, settings,
                        strategies as st)

import deployment_preflight as preflight
from test_deployment_shadow_manager import ShadowManagerEnv
from test_workflow_deploy_subscribe_merge_exploration import WorkflowDeployEnv
from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, REGION, FakeGreengrass, parse_component_arn)

# --------------------------------------------------------------------------
# The response contract (deployment_preflight owns it; re-stated locally so a
# rename of a public constant is a visible failure rather than a silent skip)
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
FINDING_CLASSES = (CLASS_BLOCKING, CLASS_ACK, CLASS_UNVERIFIED)

#: Every pre-submit gate that exists today (3.4). None of them may fire on any
#: component set in this suite — a refusal carrying one of these codes would
#: mean the property is measuring the wrong gate.
EXISTING_GATE_CODES = {
    'VLLM_ARCH_UNSUPPORTED', 'PLUGIN_LIFECYCLE_VIOLATION',
    'PLUGIN_ARCH_UNSUPPORTED', 'INCOMPATIBLE_LOCAL_SERVER',
    'CAMERA_BINDINGS_INVALID', 'CAMERA_WARNINGS_UNCONFIRMED',
    'REGISTRY_UNAVAILABLE',
}


def test_contract_constants_match_the_implementation():
    """Guard for this file, not for the fix: the codes, kinds, classes and the
    acknowledgement request field asserted below are the ones the module
    publishes. A rename must fail loudly here rather than quietly turning the
    properties into tautologies."""
    assert preflight.CODE_VALIDATION_FAILED == CODE_BLOCKING
    assert preflight.CODE_ACKNOWLEDGEMENT_REQUIRED == CODE_ACK_REQUIRED
    assert preflight.ACKNOWLEDGEMENT_FIELD == ACK_FIELD
    assert (preflight.KIND_PLATFORM, preflight.KIND_DEPENDENCY,
            preflight.KIND_DESELECTED) == (KIND_PLATFORM, KIND_DEPENDENCY,
                                           KIND_DESELECTED)
    assert (preflight.CLASS_BLOCKING, preflight.CLASS_ACKNOWLEDGEMENT,
            preflight.CLASS_UNVERIFIED) == FINDING_CLASSES


# --------------------------------------------------------------------------
# The account's real platform-manifest shapes (evidence.md §1.1)
# --------------------------------------------------------------------------

PLATFORM_LINUX_ONLY = {'os': 'linux'}                  # Nucleus, Cli
PLATFORM_ANY_OS = {'os': '*'}                          # ShadowManager, LogManager
PLATFORM_AARCH64 = {'os': 'linux', 'architecture': 'aarch64'}  # variant-less
PLATFORM_JP5 = {'os': 'linux', 'variant': 'arm64_jp5',
                'architecture': 'aarch64'}
PLATFORM_JP6 = {'os': 'linux', 'variant': 'arm64_jp6',
                'architecture': 'aarch64'}

#: The four wildcard forms the account actually publishes (evidence.md §1.1 /
#: §5.1). Every one of them MUST be satisfied by a fully reported device.
WILDCARD_MANIFEST_FORMS = {
    'absent-attribute-key': [PLATFORM_LINUX_ONLY],
    'literal-star-value': [PLATFORM_ANY_OS],
    'null-platform-block': [None],
    'empty-platform-block': [{}],
    'variantless-aarch64': [PLATFORM_AARCH64],
}

NUCLEUS = 'aws.greengrass.Nucleus'
SHADOW_MANAGER = 'aws.greengrass.ShadowManager'
LOG_MANAGER = 'aws.greengrass.LogManager'
DEVICE_NUCLEUS_VERSION = '2.12.0'
NUCLEUS_VERSIONS = ['2.12.0', '2.14.3']
SHADOW_MANAGER_VERSIONS = ['2.3.9']
LOG_MANAGER_VERSIONS = ['2.3.10']
PUBLIC_NUCLEUS_DEPENDENCY = {
    NUCLEUS: {'VersionRequirement': '>=2.0.0 <2.15.0',
              'DependencyType': 'SOFT'},
}

LOCAL_SERVER_JP7 = 'aws.edgeml.dda.LocalServer.arm64JP7'
LOCAL_SERVER_JP7_VERSIONS = [f'1.0.{patch}' for patch in range(27)]
LOCAL_SERVER_JP7_LATEST = '1.0.26'
LOCAL_SERVER_DEPENDENCIES = {
    NUCLEUS: {'VersionRequirement': '>=2.4.0', 'DependencyType': 'HARD'},
    SHADOW_MANAGER: {'VersionRequirement': '>=2.2.0',
                     'DependencyType': 'HARD'},
}

#: Wildcard-bearing, auto-included or carried components that must NEVER
#: produce a finding: a matcher that treated an absent attribute key or a
#: literal "*" as a constraint would report these incompatible with every
#: device and refuse EVERY submission (2.8, evidence.md §5.1).
NEVER_A_FINDING = frozenset({NUCLEUS, SHADOW_MANAGER, LOG_MANAGER,
                             LOCAL_SERVER_JP7})

TARGET_ARCHITECTURE_JP7 = 'arm64_jp7'
#: What a FULLY reported JP7 device resolves to: os/architecture from
#: get_core_device, variant from DEVICES_TABLE.target_architecture by identity
#: map (evidence.md §1.3).
DEVICE_PLATFORM_FULL = {'os': 'linux', 'architecture': 'aarch64',
                        'variant': TARGET_ARCHITECTURE_JP7}

#: The reference JP7 device the module-level runs judge against
#: (evidence.md §1.3).
CE_C_DEVICE = 'jetson-thor1'


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


# --------------------------------------------------------------------------
# Catalog seeding (FakeGreengrass, endpoint level)
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
    """All 27 published JP7 LocalServer versions, variant-less aarch64 — the
    JetPack lives in the component NAME, never in the manifest."""
    gg.seed_component_versions(
        LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_VERSIONS,
        platforms=[PLATFORM_AARCH64], dependencies=LOCAL_SERVER_DEPENDENCIES)


def seed_device(gg, tables, thing_name, fully_reported=True,
                target_architecture=TARGET_ARCHITECTURE_JP7):
    """A JP7 core device.

    ``fully_reported=True`` makes ``get_core_device`` report exactly what the
    live API reports — ``platform=linux architecture=aarch64
    runtime=aws_nucleus_classic`` and NO variant — and records the variant in
    the portal's own Devices table, so the platform matcher has every attribute
    it needs and DECIDES. ``fully_reported=False`` reproduces the preservation
    oracle's shape, where ``get_core_device`` raises and the judgement falls
    back to UNVERIFIED.
    """
    if fully_reported:
        gg.register_device(thing_name,
                           local_server_version=LOCAL_SERVER_JP7_LATEST,
                           arch='arm64JP7',
                           nucleus_version=DEVICE_NUCLEUS_VERSION,
                           platform='linux', architecture='aarch64',
                           runtime='aws_nucleus_classic')
    else:
        gg.register_device(thing_name,
                           local_server_version=LOCAL_SERVER_JP7_LATEST,
                           arch='arm64JP7')
    item = {'device_id': thing_name}
    if target_architecture:
        item['target_architecture'] = target_architecture
    tables.devices.put_item(Item=item)


def fresh_catalog(harness, thing_names, gg=None, fully_reported=True):
    """A per-example FakeGreengrass with the public catalog, LocalServer and
    the target devices seeded, wired into the harness in place of its own (the
    harness's client factory resolves ``self.gg`` per call)."""
    gg = gg if gg is not None else FakeGreengrass()
    harness.gg = gg
    seed_public_catalog(gg)
    seed_local_server(gg)
    for thing_name in thing_names:
        seed_device(gg, harness.env.stack.tables, thing_name,
                    fully_reported=fully_reported)
    return gg


def thing_arn(thing_name):
    return f'arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}'


def component_entry(name, version):
    return {'component_name': name, 'component_version': version}


def component_map(pairs):
    """``{name: {'componentVersion': version}}`` from ``[(name, version)]``."""
    return {name: {'componentVersion': version} for name, version in pairs}


def versions_only(components_map):
    """``{name: version}`` — the shape the validators take, which ``evaluate``
    normalizes a Greengrass components map into before resolving."""
    return {name: (entry or {}).get('componentVersion')
            for name, entry in (components_map or {}).items()}


# --------------------------------------------------------------------------
# Assertion helpers
# --------------------------------------------------------------------------

def error_code(body):
    return ((body or {}).get('error') or {}).get('code')


def body_findings(body):
    details = ((body or {}).get('error') or {}).get('details') or {}
    return details.get('findings') or []


def describe(status, body, gg):
    return (f'status={status} body={json.dumps(body, default=str)} '
            f'forwarded={json.dumps(gg.create_deployment_calls, default=str)}')


def assert_no_existing_gate_fired(status, body, gg):
    code = error_code(body)
    assert code not in EXISTING_GATE_CODES, (
        f'an EXISTING pre-submit gate ({code}) refused this submission, so the '
        f'property is not measuring the preflight validation: '
        f'{describe(status, body, gg)}')


def assert_wildcard_components_never_flagged(findings):
    flagged = sorted({finding.get('component_name') for finding in findings}
                     & NEVER_A_FINDING)
    assert not flagged, (
        f'wildcard-bearing components were reported as findings ({flagged}) — '
        f'a matcher that does this refuses every real deployment: '
        f'{json.dumps(findings, default=str)}')


def finding_identity(finding):
    """A finding's identity for uniqueness/equality checks: the kind, the
    component it names and the requirement/version it names it at."""
    return (finding.get('kind'), finding.get('component_name'),
            str(finding.get('component_version') or ''),
            str(finding.get('version_requirement') or ''))


def assert_each_finding_classified_exactly_once(findings):
    """2.16: every finding carries exactly one of the three classes, and no
    finding identity appears twice (which is what 'classified in exactly one
    class' means operationally — the same fact cannot ride in two classes)."""
    identities = []
    for finding in findings:
        assert finding.get('finding_class') in FINDING_CLASSES, (
            f'finding carries no valid finding_class: {finding!r}')
        identities.append(finding_identity(finding))
    duplicates = sorted({identity for identity in identities
                         if identities.count(identity) > 1})
    assert not duplicates, (
        f'the same finding was reported more than once ({duplicates}), so it '
        f'is not classified in exactly one class: '
        f'{json.dumps(findings, default=str)}')


def names_of(findings, kind=None, finding_class=None):
    return {finding.get('component_name') for finding in findings
            if (kind is None or finding.get('kind') == kind)
            and (finding_class is None
                 or finding.get('finding_class') == finding_class)}


# --------------------------------------------------------------------------
# A pure in-memory catalog for the module-level runs
# --------------------------------------------------------------------------

class Catalog:
    """The injected ``fetch_recipe`` / ``list_versions`` pair backed by a plain
    dict, with per-call fault injection and full call recording.

    Recipes come back as a JSON **bytes** body, exactly as
    ``GetComponent(recipeOutputFormat='JSON')`` returns them, so the decode and
    parse path is exercised rather than bypassed.
    """

    def __init__(self):
        self.recipes = {}          # (name, version) -> recipe dict
        self.versions = {}         # name -> [versions]
        self.recipe_calls = []     # every (name, version) asked for, in order
        self.version_calls = []    # every name asked for, in order
        self.recipe_faults = {}    # (name, version) -> Exception
        self.recipe_bodies = {}    # (name, version) -> raw body override
        self.version_faults = {}   # name -> Exception

    # ------------------------------------------------------------- seeding
    def publish(self, name, version, platforms=None, dependencies=None):
        manifests = [{'Platform': platform, 'Lifecycle': {}}
                     for platform in ([None] if platforms is None
                                      else platforms)]
        self.recipes[(name, str(version))] = {
            'RecipeFormatVersion': '2020-01-25',
            'ComponentName': name,
            'ComponentVersion': str(version),
            'ComponentDependencies': dependencies,
            'Manifests': manifests,
        }
        self.versions.setdefault(name, [])
        if str(version) not in self.versions[name]:
            self.versions[name].append(str(version))

    def publish_many(self, name, versions, platforms=None, dependencies=None):
        for version in versions:
            self.publish(name, version, platforms=platforms,
                         dependencies=dependencies)

    def seed_public_and_local_server(self):
        self.publish_many(NUCLEUS, NUCLEUS_VERSIONS,
                          platforms=[PLATFORM_LINUX_ONLY])
        self.publish_many(SHADOW_MANAGER, SHADOW_MANAGER_VERSIONS,
                          platforms=[PLATFORM_ANY_OS],
                          dependencies=PUBLIC_NUCLEUS_DEPENDENCY)
        self.publish_many(LOG_MANAGER, LOG_MANAGER_VERSIONS,
                          platforms=[PLATFORM_ANY_OS],
                          dependencies=PUBLIC_NUCLEUS_DEPENDENCY)
        self.publish_many(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_VERSIONS,
                          platforms=[PLATFORM_AARCH64],
                          dependencies=LOCAL_SERVER_DEPENDENCIES)

    # ------------------------------------------------------- injected pair
    def fetch_recipe(self, name, version):
        key = (name, str(version))
        self.recipe_calls.append(key)
        if key in self.recipe_faults:
            raise self.recipe_faults[key]
        if key in self.recipe_bodies:
            return self.recipe_bodies[key]
        recipe = self.recipes.get(key)
        if recipe is None:
            raise ClientError(
                {'Error': {'Code': 'ResourceNotFoundException',
                           'Message': f'Component ({key}) does not exist'}},
                'GetComponent')
        return json.dumps(recipe).encode('utf-8')

    def list_versions(self, name):
        self.version_calls.append(name)
        if name in self.version_faults:
            raise self.version_faults[name]
        return list(self.versions.get(name, []))


# --------------------------------------------------------------------------
# Fault injection (Property 3)
# --------------------------------------------------------------------------

AWS_ERROR_CODES = ('ThrottlingException', 'AccessDeniedException',
                   'ResourceNotFoundException', 'ValidationException',
                   'InternalServerException')
EXCEPTION_TOKENS = AWS_ERROR_CODES + ('__raw_exception__', '__runtime_error__')

#: Recipe bodies that are not a JSON object — the shapes a throttled or
#: proxied GetComponent can hand back (2.9 names "a malformed/non-JSON recipe
#: body" explicitly).
MALFORMED_RECIPE_BODIES = (
    b'<html><body>503 Service Unavailable</body></html>',
    'not json at all',
    b'',
    json.dumps([1, 2, 3]).encode('utf-8'),
    json.dumps('a bare string').encode('utf-8'),
    b'\xff\xfe\x00 not utf-8',
    None,
)


def build_exception(token):
    if token == '__raw_exception__':
        return Exception('unclassified greengrassv2 failure')
    if token == '__runtime_error__':
        return RuntimeError('connection reset by peer')
    return ClientError(
        {'Error': {'Code': token, 'Message': f'{token} injected by the test'}},
        'GreengrassOperation')


def exception_tokens():
    return st.sampled_from(EXCEPTION_TOKENS)


class _BrokenPaginator:
    """A paginator whose ``paginate`` raises — the "broken paginator" case."""

    def __init__(self, error):
        self._error = error

    def paginate(self, **_kwargs):
        raise self._error


class _HalfBrokenPaginator:
    """A paginator that yields one usable page and then raises, so the failure
    lands mid-iteration rather than at the call."""

    def __init__(self, error, first_page):
        self._error = error
        self._first_page = first_page

    def paginate(self, **_kwargs):
        yield self._first_page
        raise self._error


#: Every greengrassv2 call reachable from the validation path, plus the two
#: non-exception failure shapes 2.9 names.
BREAKABLE_CALLS = (
    'get_component',
    'list_component_versions',
    'describe_component',
    'get_core_device',
    'get_deployment',
    'get_paginator',
    'paginate',
    'paginate_midway',
    'malformed_recipe_body',
)


class BrokenGreengrass(FakeGreengrass):
    """``FakeGreengrass`` with exactly ONE call broken.

    Only the named call fails; everything else behaves normally, so a permitted
    submission proves the failure was absorbed rather than that the whole
    harness went quiet. ``get_paginator``/``paginate`` are broken only for the
    ``list_component_versions`` operation — the installed-component and
    thing-group paginators the pre-existing gates use stay intact, so the
    property measures the preflight path and not a collapsed harness.
    """

    def __init__(self, broken_call, error, malformed_body=None):
        super().__init__()
        self.broken_call = broken_call
        self.error = error
        self.malformed_body = malformed_body

    # -- the closure walk's two reads ----------------------------------
    def get_component(self, arn=None, recipeOutputFormat=None, **kwargs):
        if self.broken_call == 'get_component':
            self.get_component_calls.append({'arn': arn, 'broken': True})
            raise self.error
        if self.broken_call == 'malformed_recipe_body':
            self.get_component_calls.append({'arn': arn, 'malformed': True})
            return {'recipeOutputFormat': recipeOutputFormat or 'JSON',
                    'recipe': self.malformed_body}
        return super().get_component(arn=arn,
                                     recipeOutputFormat=recipeOutputFormat,
                                     **kwargs)

    def list_component_versions(self, arn=None, **kwargs):
        if self.broken_call == 'list_component_versions':
            self.list_component_versions_calls.append(arn)
            raise self.error
        return super().list_component_versions(arn=arn, **kwargs)

    def get_paginator(self, operation):
        if operation == 'list_component_versions':
            if self.broken_call == 'get_paginator':
                raise self.error
            if self.broken_call == 'paginate':
                return _BrokenPaginator(self.error)
            if self.broken_call == 'paginate_midway':
                return _HalfBrokenPaginator(
                    self.error, {'componentVersions': []})
        return super().get_paginator(operation)

    # -- the reads the two call sites make around the walk -------------
    def describe_component(self, arn=None, **kwargs):
        if self.broken_call == 'describe_component':
            self.describe_component_calls.append(arn)
            raise self.error
        return super().describe_component(arn=arn, **kwargs)

    def get_core_device(self, coreDeviceThingName=None, **kwargs):
        if self.broken_call == 'get_core_device':
            raise self.error
        return super().get_core_device(
            coreDeviceThingName=coreDeviceThingName, **kwargs)

    def get_deployment(self, deploymentId=None, **kwargs):
        if self.broken_call == 'get_deployment':
            raise self.error
        return super().get_deployment(deploymentId=deploymentId, **kwargs)


# --------------------------------------------------------------------------
# Shared strategies
# --------------------------------------------------------------------------

def jp7_thing_names():
    return st.builds(
        lambda stem, index: f'{stem}-{index}',
        st.sampled_from(['adlink-dlap', 'jetson-thor', 'jp7-orin', 'orin-agx']),
        st.integers(min_value=1, max_value=999))


def clean_component_names():
    """Component names carrying NO prefix any existing gate keys off
    (``dda.plugin.``, ``model-vllm-``, ``dda.workflow.``), so every property
    below measures the preflight validation alone."""
    return st.builds(
        lambda slug, index: f'model-{slug}-{index}-jetson-xavier-jp7',
        st.sampled_from(['yolo-probe', 'blue-plate', 'widget-defect',
                         'cookies-onnx']),
        st.integers(min_value=1, max_value=9999))


#: The only variant values the account's 41 variant-bearing manifests use, and
#: the only Jetson `Target_Architecture` values the portal records
#: (evidence.md §1.1, §1.3).
JETPACK_VARIANTS = ('arm64_jp4', 'arm64_jp5', 'arm64_jp6', 'arm64_jp7')
DEVICE_RUNTIMES = (None, 'aws_nucleus_classic', 'nvidia')
#: Attributes a device may report that no manifest constrains — a matcher must
#: ignore them rather than treat them as a mismatch.
UNCONSTRAINED_DEVICE_ATTRIBUTES = (None, {'kernel': '5.15.148-tegra'},
                                   {'gpu': 'orin'}, {'thing_group': 'line-a'})


@st.composite
def fully_reported_device_platforms(draw):
    """A Jetson device that reports EVERY attribute the matcher needs: os and
    architecture from ``get_core_device`` and the variant from
    ``DEVICES_TABLE.target_architecture`` (evidence.md §1.3). Optionally also a
    runtime and attributes no manifest constrains."""
    platform = {'os': 'linux', 'architecture': 'aarch64',
                'variant': draw(st.sampled_from(JETPACK_VARIANTS))}
    runtime = draw(st.sampled_from(DEVICE_RUNTIMES))
    if runtime:
        platform['runtime'] = runtime
    extra = draw(st.sampled_from(UNCONSTRAINED_DEVICE_ATTRIBUTES))
    if extra:
        platform.update(extra)
    return platform


# ==========================================================================
# Property 3 — fail-open under any greengrassv2 exception (task 5.1)
# ==========================================================================

#: The model component the fault is aimed at, and a second-level dependency
#: whose version list must be READ (not pinned by the submitted set), so the
#: `list_component_versions` fault has something to break.
PROBE_MODEL = 'model-preflight-probe-jetson-xavier-jp7'
PROBE_MODEL_VERSION = '3.0.0'
PROBE_HELPER = 'aws.edgeml.dda.probe.Helper'
PROBE_HELPER_VERSION = '1.4.0'
PROBE_MODEL_DEPENDENCIES = {
    LOCAL_SERVER_JP7: {'VersionRequirement': '>=1.0.0',
                       'DependencyType': 'HARD'},
    PROBE_HELPER: {'VersionRequirement': '>=1.0.0',
                   'DependencyType': 'HARD'},
}

MODULE_FAULT_TARGETS = ('recipe_fault', 'malformed_recipe_body',
                        'versions_fault', 'core_device_fault')

#: Breaks that remove the COMPONENT's own claim (its recipe or the version
#: list that resolves it), plus the device-side read. Kept apart from
#: `get_core_device` deliberately — see the docstring of
#: `test_a_check_that_could_not_be_performed_never_becomes_a_fault`.
RECIPE_SIDE_BREAKS = ('get_component', 'malformed_recipe_body',
                      'get_paginator', 'paginate', 'paginate_midway',
                      'list_component_versions')


def clean_probe_catalog():
    """A catalog and submitted set that produce ZERO findings when every call
    works — the control every fail-open assertion is measured against."""
    catalog = Catalog()
    catalog.seed_public_and_local_server()
    catalog.publish(PROBE_HELPER, PROBE_HELPER_VERSION,
                    platforms=[PLATFORM_AARCH64])
    catalog.publish(PROBE_MODEL, PROBE_MODEL_VERSION,
                    platforms=[PLATFORM_AARCH64],
                    dependencies=PROBE_MODEL_DEPENDENCIES)
    submitted = component_map([
        (LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
        (PROBE_MODEL, PROBE_MODEL_VERSION),
        (NUCLEUS, DEVICE_NUCLEUS_VERSION),
        (SHADOW_MANAGER, SHADOW_MANAGER_VERSIONS[0]),
        (LOG_MANAGER, LOG_MANAGER_VERSIONS[0]),
    ])
    return catalog, submitted


class TestProperty3FailOpen:
    """**Validates: Requirements 2.9, 2.10.**

    2.9 is a hard contract and deliberately the OPPOSITE of the existing plugin
    architecture gate's fail-CLOSED behaviour: this validation runs on EVERY
    submission, so a fail-closed default would take deployment submission down
    whenever an account read is unavailable. Greengrass throttling is not
    hypothetical — evidence.md §9 records repeated near-identical revisions
    seconds apart on both incident devices.
    """

    # Feature: deployment-preflight-validation, Property 3: fail-open under any greengrassv2 exception
    @given(target=st.sampled_from(MODULE_FAULT_TARGETS),
           token=exception_tokens(),
           malformed_body=st.sampled_from(MALFORMED_RECIPE_BODIES))
    @settings(deadline=None)
    def test_module_fails_open_for_any_failing_call(self, target, token,
                                                   malformed_body):
        """Pure module: a submitted set that validates CLEAN when every call
        works must still be PERMITTED when any one call fails with any
        exception, or hands back a body that is not a JSON object — and the
        component the failure touched must come back UNVERIFIED rather than
        silently dropped."""
        control_catalog, submitted = clean_probe_catalog()
        control = preflight.evaluate(
            submitted, {CE_C_DEVICE: dict(DEVICE_PLATFORM_FULL)},
            control_catalog.fetch_recipe, control_catalog.list_versions,
            previous_components=copy.deepcopy(submitted))
        assert control.findings == [], (
            'the control run is not clean, so this property would pass '
            f'vacuously: {json.dumps(control.findings, default=str)}')
        assert control.refusal is None

        catalog, submitted = clean_probe_catalog()
        error = build_exception(token)
        device_platforms = {CE_C_DEVICE: dict(DEVICE_PLATFORM_FULL)}
        if target == 'recipe_fault':
            catalog.recipe_faults[(PROBE_MODEL, PROBE_MODEL_VERSION)] = error
            expected_unverified = PROBE_MODEL
        elif target == 'malformed_recipe_body':
            catalog.recipe_bodies[(PROBE_MODEL, PROBE_MODEL_VERSION)] = \
                malformed_body
            expected_unverified = PROBE_MODEL
        elif target == 'versions_fault':
            catalog.version_faults[PROBE_HELPER] = error
            expected_unverified = PROBE_HELPER
        else:                                  # core_device_fault
            def read_core_device(_thing_name):
                raise error
            device_platforms = preflight.resolve_device_platforms(
                [CE_C_DEVICE], read_core_device, {})
            assert device_platforms == {CE_C_DEVICE: {}}, (
                'resolve_device_platforms must absorb the failure and report '
                f'no attributes: {device_platforms!r}')
            expected_unverified = PROBE_MODEL

        outcome = preflight.evaluate(
            submitted, device_platforms, catalog.fetch_recipe,
            catalog.list_versions,
            previous_components=copy.deepcopy(submitted))

        assert outcome.refusal is None, (
            'a failing account read refused the submission: '
            f'{json.dumps(outcome.refusal, default=str)}')
        assert outcome.of_class(CLASS_BLOCKING) == [], (
            'a check that could not be performed produced a blocking finding: '
            f'{json.dumps(outcome.findings, default=str)}')
        assert outcome.of_class(CLASS_ACK) == [], (
            'a failing account read invented an acknowledgement-required '
            f'finding: {json.dumps(outcome.findings, default=str)}')
        unverified = outcome.of_class(CLASS_UNVERIFIED)
        assert unverified, (
            'the failure was swallowed without reporting the component as '
            f'unverified: {json.dumps(outcome.findings, default=str)}')
        assert expected_unverified in names_of(unverified), (
            f'{expected_unverified} was not reported unverified: '
            f'{json.dumps(unverified, default=str)}')
        assert_each_finding_classified_exactly_once(outcome.findings)
        assert_wildcard_components_never_flagged(
            outcome.of_class(CLASS_BLOCKING))

    # Feature: deployment-preflight-validation, Property 3: fail-open under any greengrassv2 exception
    @given(broken=st.sampled_from(BREAKABLE_CALLS), token=exception_tokens(),
           malformed_body=st.sampled_from(MALFORMED_RECIPE_BODIES))
    @settings(deadline=None)
    def test_production_fetchers_fail_open_for_any_broken_call(
            self, broken, token, malformed_body):
        """The PRODUCTION fetchers (`greengrass_fetchers`) over a greengrassv2
        client with exactly one call broken — including a `get_paginator` that
        raises, a `paginate` that raises, and a paginator that raises mid
        iteration. Nothing propagates out of `resolve_closure` or `evaluate`,
        and the outcome is still PERMITTED."""
        error = build_exception(token)
        gg = BrokenGreengrass(broken, error, malformed_body=malformed_body)
        seed_public_catalog(gg)
        seed_local_server(gg)
        gg.seed_component_version(PROBE_HELPER, PROBE_HELPER_VERSION,
                                  platforms=[PLATFORM_AARCH64])
        gg.seed_component_version(PROBE_MODEL, PROBE_MODEL_VERSION,
                                  platforms=[PLATFORM_AARCH64],
                                  dependencies=PROBE_MODEL_DEPENDENCIES)
        _catalog, submitted = clean_probe_catalog()

        fetch_recipe, list_versions = preflight.greengrass_fetchers(
            gg, REGION, ACCOUNT_ID)
        # resolve_closure must never raise, whatever the client does.
        closure = preflight.resolve_closure(
            {name: entry['componentVersion']
             for name, entry in submitted.items()},
            fetch_recipe, list_versions)
        assert isinstance(closure, preflight.ResolvedClosure)

        fetch_recipe, list_versions = preflight.greengrass_fetchers(
            gg, REGION, ACCOUNT_ID)
        outcome = preflight.evaluate(
            submitted, {CE_C_DEVICE: dict(DEVICE_PLATFORM_FULL)},
            fetch_recipe, list_versions,
            previous_components=copy.deepcopy(submitted))

        assert outcome.refusal is None, (
            f'{broken} raising {token} refused the submission: '
            f'{json.dumps(outcome.refusal, default=str)}')
        assert outcome.of_class(CLASS_BLOCKING) == [], (
            f'{broken} raising {token} produced a blocking finding: '
            f'{json.dumps(outcome.findings, default=str)}')

    # Feature: deployment-preflight-validation, Property 3: fail-open under any greengrassv2 exception
    @given(token=exception_tokens(), thing_a=jp7_thing_names(),
           thing_b=jp7_thing_names(), record_architecture=st.booleans())
    @settings(deadline=None)
    def test_the_two_reads_around_the_walk_fail_open(
            self, deployments, token, thing_a, thing_b, record_architecture):
        """The two greengrassv2 reads `deployments.py` makes AROUND the closure
        walk — `get_deployment` for the previous component set and
        `get_core_device` for the device platform — absorb any exception and
        return the fail-open value (no previous components, no known platform)
        rather than propagating.

        `get_deployment` is exercised here rather than through the endpoint
        because `find_latest_deployment_for_target` already carries the
        previous deployment's `components`, so the endpoint never reaches the
        `get_deployment` fallback; asserting it at the endpoint would be a
        test that proves nothing."""
        error = build_exception(token)
        gg = BrokenGreengrass('get_deployment', error)
        assert deployments._preflight_previous_components(
            gg, {'deploymentId': 'dep-unreadable'}) == {}
        # No previous deployment at all is the same fail-open value.
        assert deployments._preflight_previous_components(gg, None) == {}

        gg = BrokenGreengrass('get_core_device', error)
        recorded = ({thing_a: TARGET_ARCHITECTURE_JP7}
                    if record_architecture else {})
        platforms = preflight.resolve_device_platforms(
            [thing_a, thing_b],
            lambda thing_name: gg.get_core_device(
                coreDeviceThingName=thing_name),
            recorded)
        # os/architecture are unknown; the portal's own record still supplies
        # the variant for a device that has one (evidence.md §1.3).
        expected = {thing_a: {}, thing_b: {}}
        if record_architecture:
            expected[thing_a] = {'variant': TARGET_ARCHITECTURE_JP7}
        if thing_a == thing_b and record_architecture:
            expected = {thing_a: {'variant': TARGET_ARCHITECTURE_JP7}}
        assert platforms == expected, platforms

    # Feature: deployment-preflight-validation, Property 3: fail-open under any greengrassv2 exception
    @given(broken=st.sampled_from(BREAKABLE_CALLS), token=exception_tokens(),
           malformed_body=st.sampled_from(MALFORMED_RECIPE_BODIES),
           thing_name=jp7_thing_names())
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_endpoint_still_submits_when_any_greengrass_call_is_broken(
            self, sm_env, broken, token, malformed_body, thing_name):
        """Endpoint level, `create_deployment`: a clean submission is still
        forwarded to Greengrass with a 201 and no 5xx when any one greengrassv2
        call in the validation path is broken. This is the property that
        decides whether the validation helps or takes submission down."""
        error = build_exception(token)
        gg = fresh_catalog(sm_env, [thing_name],
                           gg=BrokenGreengrass(broken, error,
                                               malformed_body=malformed_body))
        gg.seed_component_version(PROBE_HELPER, PROBE_HELPER_VERSION,
                                  platforms=[PLATFORM_AARCH64])
        gg.seed_component_version(PROBE_MODEL, PROBE_MODEL_VERSION,
                                  platforms=[PLATFORM_AARCH64],
                                  dependencies=PROBE_MODEL_DEPENDENCIES)

        status, body = sm_env.deploy_components(
            [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
             component_entry(PROBE_MODEL, PROBE_MODEL_VERSION)],
            target_devices=[thing_name])

        assert status < 500, (
            f'{broken} raising {token} produced a server error: '
            f'{describe(status, body, gg)}')
        assert error_code(body) not in (CODE_BLOCKING, CODE_ACK_REQUIRED), (
            f'{broken} raising {token} refused the submission: '
            f'{describe(status, body, gg)}')
        assert_no_existing_gate_fired(status, body, gg)
        assert status == 201, describe(status, body, gg)
        [call] = gg.create_deployment_calls
        assert call['components'][PROBE_MODEL]['componentVersion'] == \
            PROBE_MODEL_VERSION

    # Feature: deployment-preflight-validation, Property 3: fail-open under any greengrassv2 exception
    @given(broken=st.sampled_from(RECIPE_SIDE_BREAKS + ('get_core_device',)),
           token=exception_tokens(), thing_name=jp7_thing_names())
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_a_check_that_could_not_be_performed_never_becomes_a_fault(
            self, sm_env, broken, token, thing_name):
        """The sharp edge of 2.9: a submission that a WORKING validation would
        refuse (a jp5/jp6-only component on a jp7 device) is PERMITTED when the
        read that would have proved the fault is broken. The portal never
        blocks on the basis of a check it could not perform; Greengrass stays
        the authoritative enforcement layer (2.10).

        DELIBERATE ASYMMETRY, encoded rather than glossed: for the
        `get_core_device` case the device's Devices-table record is omitted.
        The device `variant` comes ONLY from
        `DEVICES_TABLE.target_architecture` (evidence.md §1.3), so with a
        record present a jp5/jp6-only manifest is still decidably
        contradicted — `variant: arm64_jp5` vs the recorded `arm64_jp7` —
        even when `get_core_device` is down, and the refusal is CORRECT. It is
        only when neither source knows an attribute the manifest constrains
        that the judgement becomes unverified.
        """
        error = build_exception(token)
        record_architecture = broken != 'get_core_device'
        gg = BrokenGreengrass(broken, error,
                              malformed_body=MALFORMED_RECIPE_BODIES[0])
        sm_env.gg = gg
        seed_public_catalog(gg)
        seed_local_server(gg)
        seed_device(gg, sm_env.env.stack.tables, thing_name,
                    target_architecture=(TARGET_ARCHITECTURE_JP7
                                         if record_architecture else None))
        offender = f'model-jp5-jp6-only-{thing_name}-jetson-xavier'
        gg.seed_component_version(offender, '1.0.0',
                                  platforms=[PLATFORM_JP5, PLATFORM_JP6])

        status, body = sm_env.deploy_components(
            [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
             component_entry(offender, '1.0.0')],
            target_devices=[thing_name])

        assert status < 500, describe(status, body, gg)
        assert status == 201, (
            f'{broken} raising {token} still blocked the submission on a '
            f'check it could not perform: {describe(status, body, gg)}')
        [call] = gg.create_deployment_calls
        assert offender in call['components']


# ==========================================================================
# Property 4 — every finding classified exactly once in one pass (task 5.2)
# ==========================================================================

#: Bounded requirements stay blocking-invalid under 2.5; an UNPINNED one on a
#: name with no published version is unverified under 2.5a, which is a
#: different property and is not what this one measures.
BOUNDED_DEAD_REQUIREMENT = '>=1.0.0 <2.0.0'
UNPINNED_REQUIREMENT = '>=0.0.0'


def fault_names(prefix, count, salt=''):
    return [f'model-{prefix}{index}{salt}-jetson-xavier-jp7'
            for index in range(count)]


def dead_dependency_names(count, salt=''):
    """Names with ZERO published versions in BOTH namespaces — the shape of
    the two live Counterexample B edges (`…LocalServer.arm64JP4`,
    `…LocalServer.arm64`)."""
    return [f'aws.edgeml.dda.LocalServer.dead{index}{salt}'
            for index in range(count)]


class MixedFindingScenario:
    """A submission generated to carry `a` A-shaped platform faults, `b`
    B-shaped resolvability faults, `c` C-shaped de-selections and `u`
    unverifiable nodes at once, seeded identically into the pure Catalog and
    into FakeGreengrass so the two levels see the same account."""

    def __init__(self, a_count, b_count, c_count, u_count, salt=''):
        self.a_names = fault_names('a', a_count, salt)
        self.b_names = fault_names('b', b_count, salt)
        self.dead_names = dead_dependency_names(b_count, salt)
        self.keeper_names = fault_names('keep', c_count, salt)
        self.deselected_names = fault_names('gone', c_count, salt)
        self.u_names = fault_names('u', u_count, salt)
        self.version = '1.0.0'
        self.b_version = '2.0.0'
        self.keeper_version = '5.0.0'

    # ------------------------------------------------------------- shapes
    @property
    def operator_components(self):
        """The set the operator selects (roots, before auto-includes)."""
        pairs = [(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST)]
        pairs += [(name, self.version) for name in self.a_names]
        pairs += [(name, self.b_version) for name in self.b_names]
        pairs += [(name, self.keeper_version) for name in self.keeper_names]
        pairs += [(name, self.version) for name in self.u_names]
        return pairs

    @property
    def previous_pairs(self):
        """The target's current deployment: the operator's set plus the
        components this submission de-selects."""
        return (self.operator_components
                + [(name, self.version) for name in self.deselected_names])

    def expected(self):
        return {
            CLASS_BLOCKING: (set(self.a_names) | set(self.dead_names)),
            CLASS_ACK: set(self.deselected_names),
            CLASS_UNVERIFIED: set(self.u_names),
        }

    # ------------------------------------------------------------ seeding
    def seed(self, publish, fault_recipe):
        """`publish(name, version, platforms, dependencies)` and
        `fault_recipe(name, version)` adapt this to either backend."""
        for name in self.a_names:
            # A: every published manifest names another JetPack (2.3).
            publish(name, self.version, [PLATFORM_JP5, PLATFORM_JP6], None)
        for name, dead in zip(self.b_names, self.dead_names):
            # B: a HARD, BOUNDED requirement on a name with no published
            # version in either namespace (2.5). `dead` is never published.
            publish(name, self.b_version, [PLATFORM_AARCH64],
                    {dead: {'VersionRequirement': BOUNDED_DEAD_REQUIREMENT,
                            'DependencyType': 'HARD'}})
        for keeper, gone in zip(self.keeper_names, self.deselected_names):
            # C: the deliberately unpinned >=0.0.0 HARD edge
            # workflow_packaging.model_component_dependencies emits by design
            # (3.12) — the mechanism behind Counterexample C.
            publish(keeper, self.keeper_version, [PLATFORM_AARCH64],
                    {gone: {'VersionRequirement': UNPINNED_REQUIREMENT,
                            'DependencyType': 'HARD'},
                     LOCAL_SERVER_JP7: {'VersionRequirement': '>=1.0.0',
                                        'DependencyType': 'HARD'}})
            publish(gone, self.version, [PLATFORM_AARCH64], None)
        for name in self.u_names:
            # Unverified: published, but its recipe cannot be read (2.9).
            publish(name, self.version, [PLATFORM_AARCH64], None)
            fault_recipe(name, self.version)


def mixed_scenarios(max_each=2):
    return st.builds(
        MixedFindingScenario,
        st.integers(min_value=0, max_value=max_each),
        st.integers(min_value=0, max_value=max_each),
        st.integers(min_value=0, max_value=max_each),
        st.integers(min_value=0, max_value=max_each))


class TestProperty4SinglePassThreeClasses:
    """**Validates: Requirements 2.6, 2.9, 2.10, 2.16.**

    Greengrass reports exactly ONE fault per negotiation, which is what forced
    the incident's blind serial iteration (defect 1.4). The portal holds the
    whole closure, so every finding must come back in ONE response, each in
    exactly one class, with the outcome decided by the strongest class present.
    """

    # Feature: deployment-preflight-validation, Property 4: every finding classified exactly once in one pass
    @given(scenario=mixed_scenarios(max_each=3))
    @settings(deadline=None)
    def test_one_pass_classifies_every_finding_exactly_once(self, deployments,
                                                            scenario):
        """Pure module, where the whole finding list is observable: for any mix
        of A-shaped faults, B-shaped faults, C-shaped findings and unverifiable
        nodes, ONE outcome carries ALL of them, each in exactly one class, and
        the refusal is decided by class — blocking refuses whatever the
        acknowledgement says, acknowledgement-required clears only on an exact
        match, unverified-only never blocks, no findings at all submits
        unchanged."""
        catalog = Catalog()
        catalog.seed_public_and_local_server()
        scenario.seed(
            lambda name, version, platforms, dependencies: catalog.publish(
                name, version, platforms=platforms, dependencies=dependencies),
            lambda name, version: catalog.recipe_faults.__setitem__(
                (name, str(version)),
                build_exception('ThrottlingException')))

        submitted = component_map(scenario.operator_components + [
            (NUCLEUS, DEVICE_NUCLEUS_VERSION),
            (SHADOW_MANAGER, SHADOW_MANAGER_VERSIONS[0]),
            (LOG_MANAGER, LOG_MANAGER_VERSIONS[0])])
        previous = component_map(scenario.previous_pairs + [
            (NUCLEUS, DEVICE_NUCLEUS_VERSION),
            (SHADOW_MANAGER, SHADOW_MANAGER_VERSIONS[0]),
            (LOG_MANAGER, LOG_MANAGER_VERSIONS[0])])
        expected = scenario.expected()

        outcome = preflight.evaluate(
            submitted, {CE_C_DEVICE: dict(DEVICE_PLATFORM_FULL)},
            catalog.fetch_recipe, catalog.list_versions,
            previous_components=previous,
            excluded_names=deployments.PORTAL_AUTO_INCLUDED_COMPONENTS)
        findings = outcome.findings

        # 2.6 / 2.16: ONE pass, every finding classified exactly once.
        assert_each_finding_classified_exactly_once(findings)
        assert_wildcard_components_never_flagged(
            outcome.of_class(CLASS_BLOCKING))

        # Each generated fault appears, in the class its shape dictates, and
        # in no other class.
        assert names_of(findings, kind=KIND_PLATFORM,
                        finding_class=CLASS_BLOCKING) == set(scenario.a_names)
        assert names_of(findings, kind=KIND_DEPENDENCY,
                        finding_class=CLASS_BLOCKING) == set(
                            scenario.dead_names)
        assert names_of(findings, kind=KIND_DESELECTED,
                        finding_class=CLASS_ACK) == set(
                            scenario.deselected_names)
        assert set(scenario.u_names) <= names_of(
            findings, finding_class=CLASS_UNVERIFIED)
        # Nothing that is a fault of one shape leaks into another class.
        for finding_class, expected_names in expected.items():
            other = set()
            for name in FINDING_CLASSES:
                if name != finding_class:
                    other |= names_of(findings, finding_class=name)
            assert not (expected_names & other), (
                f'{sorted(expected_names & other)} was classified as more than '
                f'one class: {json.dumps(findings, default=str)}')

        # The outcome is decided by the strongest class present.
        all_ack_names = sorted(scenario.deselected_names)
        if expected[CLASS_BLOCKING]:
            assert outcome.refusal, json.dumps(findings, default=str)
            assert outcome.refusal['code'] == CODE_BLOCKING
            assert outcome.refusal['details']['findings'] == findings
            # 2.16: no acknowledgement bypasses a blocking finding.
            acknowledged = preflight.evaluate(
                submitted, {CE_C_DEVICE: dict(DEVICE_PLATFORM_FULL)},
                catalog.fetch_recipe, catalog.list_versions,
                previous_components=previous, acknowledged=all_ack_names,
                excluded_names=deployments.PORTAL_AUTO_INCLUDED_COMPONENTS)
            assert acknowledged.refusal['code'] == CODE_BLOCKING
            assert names_of(acknowledged.findings, kind=KIND_PLATFORM,
                            finding_class=CLASS_BLOCKING) == set(
                                scenario.a_names)
            assert names_of(acknowledged.findings, kind=KIND_DEPENDENCY,
                            finding_class=CLASS_BLOCKING) == set(
                                scenario.dead_names)
        elif expected[CLASS_ACK]:
            assert outcome.refusal['code'] == CODE_ACK_REQUIRED
            assert outcome.refusal['details'][
                'acknowledgement_required_for'] == all_ack_names
            acknowledged = preflight.evaluate(
                submitted, {CE_C_DEVICE: dict(DEVICE_PLATFORM_FULL)},
                catalog.fetch_recipe, catalog.list_versions,
                previous_components=previous, acknowledged=all_ack_names,
                excluded_names=deployments.PORTAL_AUTO_INCLUDED_COMPONENTS)
            assert acknowledged.refusal is None, json.dumps(
                acknowledged.refusal, default=str)
        else:
            # 2.9 / 2.10: unverified findings alone never change the outcome,
            # and no finding at all submits unchanged.
            assert outcome.refusal is None, json.dumps(
                outcome.refusal, default=str)

    # Feature: deployment-preflight-validation, Property 4: every finding classified exactly once in one pass
    @given(scenario=mixed_scenarios(max_each=1), thing_name=jp7_thing_names())
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_endpoint_reports_the_whole_mix_in_one_response(
            self, sm_env, scenario, thing_name):
        """The same mix through the real `create_deployment`: ONE response
        carries every finding, the code reflects the strongest class, nothing
        reaches Greengrass while refused, and an unverified-only or clean
        submission is forwarded with the operator's set intact (3.14)."""
        gg = fresh_catalog(sm_env, [thing_name])
        scenario.seed(
            lambda name, version, platforms, dependencies:
                gg.seed_component_version(name, version, platforms=platforms,
                                          dependencies=dependencies),
            lambda name, version: gg.recipes.pop(
                (ACCOUNT_ID, name, str(version))))
        gg.seed_deployment(
            thing_arn(thing_name),
            {name: {'componentVersion': version}
             for name, version in scenario.previous_pairs},
            name=f'ssh-tunnel-on-{thing_name}')

        operator_set = [component_entry(name, version)
                        for name, version in scenario.operator_components]
        status, body = sm_env.deploy_components(operator_set,
                                               target_devices=[thing_name])
        expected = scenario.expected()

        assert_no_existing_gate_fired(status, body, gg)
        if expected[CLASS_BLOCKING]:
            assert status == 409, describe(status, body, gg)
            assert error_code(body) == CODE_BLOCKING, describe(status, body, gg)
            assert gg.create_deployment_calls == [], describe(status, body, gg)
            findings = body_findings(body)
            assert_each_finding_classified_exactly_once(findings)
            # 2.6: A, B and C all in ONE response — no serial rediscovery.
            assert names_of(findings, kind=KIND_PLATFORM,
                            finding_class=CLASS_BLOCKING) == set(
                                scenario.a_names)
            assert names_of(findings, kind=KIND_DEPENDENCY,
                            finding_class=CLASS_BLOCKING) == set(
                                scenario.dead_names)
            assert names_of(findings, kind=KIND_DESELECTED,
                            finding_class=CLASS_ACK) == set(
                                scenario.deselected_names)
            assert_wildcard_components_never_flagged(
                [f for f in findings
                 if f.get('finding_class') == CLASS_BLOCKING])
            # An acknowledgement cannot clear a blocking finding (2.16).
            ack_status, ack_body = sm_env.deploy_components(
                operator_set, target_devices=[thing_name],
                **{ACK_FIELD: sorted(scenario.deselected_names)})
            assert ack_status == 409, describe(ack_status, ack_body, gg)
            assert error_code(ack_body) == CODE_BLOCKING
            assert gg.create_deployment_calls == []
        elif expected[CLASS_ACK]:
            assert status == 409, describe(status, body, gg)
            assert error_code(body) == CODE_ACK_REQUIRED, describe(
                status, body, gg)
            assert gg.create_deployment_calls == []
            findings = body_findings(body)
            assert_each_finding_classified_exactly_once(findings)
            assert names_of(findings, kind=KIND_DESELECTED,
                            finding_class=CLASS_ACK) == set(
                                scenario.deselected_names)
            ack_status, ack_body = sm_env.deploy_components(
                operator_set, target_devices=[thing_name],
                **{ACK_FIELD: sorted(scenario.deselected_names)})
            assert ack_status == 201, describe(ack_status, ack_body, gg)
            [call] = gg.create_deployment_calls
            # 3.14: the de-selected component is never silently re-added.
            for gone in scenario.deselected_names:
                assert gone not in call['components'], sorted(
                    call['components'])
        else:
            assert status == 201, describe(status, body, gg)
            [call] = gg.create_deployment_calls
            for name, version in scenario.operator_components:
                assert call['components'][name]['componentVersion'] == version


# ==========================================================================
# Property 5 — acknowledgement specificity (task 5.3)
# ==========================================================================

ACKNOWLEDGEMENT_MUTATIONS = (
    'exact', 'exact_reordered_with_duplicates', 'superset', 'subset',
    'renamed', 'stale', 'empty', 'blanket_star', 'blanket_all', 'none')

STALE_ACK_NAME = 'model-acknowledged-last-time-jetson-xavier-jp7'
FOREIGN_ACK_NAME = 'model-not-in-this-submission-jetson-xavier-jp7'


def mutate_acknowledgement(kind, computed):
    """The acknowledgement an operator (or a stale client) might send for a
    computed de-selection set. Only `exact` — as a SET, so order and duplicates
    are irrelevant — may proceed (2.14)."""
    computed = sorted(computed)
    if kind == 'exact':
        return list(computed)
    if kind == 'exact_reordered_with_duplicates':
        return list(reversed(computed)) + list(computed)
    if kind == 'superset':
        return list(computed) + [FOREIGN_ACK_NAME]
    if kind == 'subset':
        return list(computed)[:-1]
    if kind == 'renamed':
        return [f'{name}-renamed' for name in computed]
    if kind == 'stale':
        return [STALE_ACK_NAME]
    if kind == 'empty':
        return []
    if kind == 'blanket_star':
        return ['*']
    if kind == 'blanket_all':
        return ['ALL']
    return None                                   # 'none' — no field at all


def deselected_finding(component_name):
    """The finding shape validator C produces, as `classify_findings` sees
    it."""
    return {
        'kind': KIND_DESELECTED,
        'finding_class': CLASS_ACK,
        'component_name': component_name,
        'component_version': '1.0.0',
        'required_by': [{'component_name': f'{component_name}-dependant',
                         'component_version': '5.0.0',
                         'version_requirement': UNPINNED_REQUIREMENT,
                         'dependency_type': 'HARD'}],
        'remains_installed': True,
        'removed_by_this_deployment': False,
        'effective_outcome': f'{component_name} will REMAIN installed',
        'remediation': 'de-select the dependant or acknowledge',
    }


def seed_deselection_case(gg, thing_name, count, salt=''):
    """A submission whose ONLY findings are `count` de-selected-but-still-
    required components: one keeper per de-selected component, each carrying
    the unpinned `>=0.0.0` HARD edge (3.12). Returns
    ``(operator_set, deselected_names)``."""
    keepers = [f'model-keeper{index}{salt}-jetson-xavier-jp7'
               for index in range(count)]
    deselected = [f'model-retained{index}{salt}-jetson-xavier-jp7'
                  for index in range(count)]
    for keeper, gone in zip(keepers, deselected):
        gg.seed_component_version(gone, '1.0.0', platforms=[PLATFORM_AARCH64])
        gg.seed_component_version(
            keeper, '5.0.0', platforms=[PLATFORM_AARCH64],
            dependencies={gone: {'VersionRequirement': UNPINNED_REQUIREMENT,
                                 'DependencyType': 'HARD'},
                          LOCAL_SERVER_JP7: {'VersionRequirement': '>=1.0.0',
                                             'DependencyType': 'HARD'}})
    operator_set = [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST)]
    operator_set += [component_entry(keeper, '5.0.0') for keeper in keepers]
    previous = {name: {'componentVersion': version}
                for name, version in
                [(entry['component_name'], entry['component_version'])
                 for entry in operator_set]}
    previous.update({gone: {'componentVersion': '1.0.0'}
                     for gone in deselected})
    gg.seed_deployment(thing_arn(thing_name), previous,
                       name=f'ssh-tunnel-on-{thing_name}')
    return operator_set, deselected


class TestProperty5AcknowledgementSpecificity:
    """**Validates: Requirement 2.14.**

    The acknowledgement is what separates "the portal refuses this deployment"
    from "the portal refuses to accept it SILENTLY". It must therefore be
    specific: it names the de-selected component(s) whose retention it
    authorizes, so it can never silently authorize a different or a later
    finding.
    """

    # Feature: deployment-preflight-validation, Property 5: a specific acknowledgement can never authorize a different finding
    @given(computed=st.lists(clean_component_names(), min_size=1, max_size=3,
                             unique=True),
           mutation=st.sampled_from(ACKNOWLEDGEMENT_MUTATIONS))
    @settings(deadline=None)
    def test_classification_proceeds_only_on_exact_set_equality(
            self, computed, mutation):
        """Pure classifier: for any computed de-selection set and any
        acknowledgement, the submission proceeds IFF the two match exactly as
        sets. Every other shape — superset, subset, renamed, stale, blanket and
        absent — refuses AND re-reports the finding."""
        findings = [deselected_finding(name) for name in computed]
        acknowledged = mutate_acknowledgement(mutation, computed)
        should_proceed = set(acknowledged or []) == set(computed)

        outcome = preflight.classify_findings([], [], copy.deepcopy(findings),
                                             acknowledged=acknowledged)

        assert (outcome.refusal is None) == should_proceed, (
            f'{mutation} acknowledgement {acknowledged!r} for computed '
            f'{sorted(computed)}: refusal='
            f'{json.dumps(outcome.refusal, default=str)}')
        assert_each_finding_classified_exactly_once(outcome.findings)
        assert names_of(outcome.findings, finding_class=CLASS_ACK) == set(
            computed)
        for finding in outcome.of_class(CLASS_ACK):
            assert finding['acknowledged'] is should_proceed, finding
        if should_proceed:
            return
        # 2.14: refused submissions RE-REPORT the finding rather than dropping
        # it, and name exactly what an acknowledgement would have to carry.
        assert outcome.refusal['code'] == CODE_ACK_REQUIRED
        details = outcome.refusal['details']
        assert details['acknowledgement_field'] == ACK_FIELD
        assert details['acknowledgement_required_for'] == sorted(computed)
        assert names_of(details['findings'], finding_class=CLASS_ACK) == set(
            computed)

    # Feature: deployment-preflight-validation, Property 5: a specific acknowledgement can never authorize a different finding
    @given(count=st.integers(min_value=1, max_value=2),
           mutation=st.sampled_from(ACKNOWLEDGEMENT_MUTATIONS),
           thing_name=jp7_thing_names())
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_endpoint_submits_only_on_an_exactly_matching_acknowledgement(
            self, sm_env, count, mutation, thing_name):
        """Endpoint level: the same specificity through `create_deployment`.
        Only an exactly matching acknowledgement reaches Greengrass; every
        other shape is refused with the finding re-reported and NOTHING
        forwarded."""
        gg = fresh_catalog(sm_env, [thing_name])
        operator_set, deselected = seed_deselection_case(
            gg, thing_name, count)
        acknowledged = mutate_acknowledgement(mutation, deselected)
        should_proceed = set(acknowledged or []) == set(deselected)

        body_kwargs = ({} if acknowledged is None
                       else {ACK_FIELD: acknowledged})
        status, body = sm_env.deploy_components(
            operator_set, target_devices=[thing_name], **body_kwargs)

        assert_no_existing_gate_fired(status, body, gg)
        if should_proceed:
            assert status == 201, describe(status, body, gg)
            [call] = gg.create_deployment_calls
            for gone in deselected:
                assert gone not in call['components'], sorted(
                    call['components'])
            return
        assert status == 409, describe(status, body, gg)
        assert error_code(body) == CODE_ACK_REQUIRED, describe(
            status, body, gg)
        assert gg.create_deployment_calls == [], (
            f'a {mutation} acknowledgement reached Greengrass: '
            f'{describe(status, body, gg)}')
        findings = body_findings(body)
        assert names_of(findings, kind=KIND_DESELECTED,
                        finding_class=CLASS_ACK) == set(deselected)
        details = ((body or {}).get('error') or {}).get('details') or {}
        assert details.get('acknowledgement_required_for') == sorted(deselected)

    # Feature: deployment-preflight-validation, Property 5: a specific acknowledgement can never authorize a different finding
    @given(thing_name=jp7_thing_names())
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_a_component_set_edit_invalidates_the_earlier_acknowledgement(
            self, sm_env, thing_name):
        """The clause 2.14 adds after the acknowledgement itself: WHERE the
        component set changes between the acknowledged submit and the re-submit
        so the acknowledgement no longer matches the finding computed for the
        submitted set, the finding is RE-REPORTED and that submission is
        refused again.

        Three submissions against one target: de-select the first model with a
        matching acknowledgement (proceeds); then de-select a SECOND model
        while still sending the first acknowledgement (refused, re-reporting
        the second); then acknowledge the second (proceeds)."""
        gg = fresh_catalog(sm_env, [thing_name])
        keeper = f'model-keeper-both-{thing_name}-jetson-xavier-jp7'
        first = f'model-retained-first-{thing_name}-jetson-xavier-jp7'
        second = f'model-retained-second-{thing_name}-jetson-xavier-jp7'
        for gone in (first, second):
            gg.seed_component_version(gone, '1.0.0',
                                      platforms=[PLATFORM_AARCH64])
        gg.seed_component_version(
            keeper, '5.0.0', platforms=[PLATFORM_AARCH64],
            dependencies={
                first: {'VersionRequirement': UNPINNED_REQUIREMENT,
                        'DependencyType': 'HARD'},
                second: {'VersionRequirement': UNPINNED_REQUIREMENT,
                         'DependencyType': 'HARD'},
                LOCAL_SERVER_JP7: {'VersionRequirement': '>=1.0.0',
                                   'DependencyType': 'HARD'}})
        gg.seed_deployment(thing_arn(thing_name), {
            LOCAL_SERVER_JP7: {'componentVersion': LOCAL_SERVER_JP7_LATEST},
            keeper: {'componentVersion': '5.0.0'},
            first: {'componentVersion': '1.0.0'},
            second: {'componentVersion': '1.0.0'},
        }, name=f'ssh-tunnel-on-{thing_name}')

        keep_second = [
            component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
            component_entry(keeper, '5.0.0'),
            component_entry(second, '1.0.0'),
        ]
        drop_both = keep_second[:2]

        # 1. de-select `first`, acknowledging exactly `first` — proceeds.
        status, body = sm_env.deploy_components(
            keep_second, target_devices=[thing_name],
            **{ACK_FIELD: [first]})
        assert status == 201, describe(status, body, gg)
        [call] = gg.create_deployment_calls
        assert first not in call['components']
        assert second in call['components']

        # 2. the operator EDITS the set (now dropping `second` too) but the
        # client still carries the earlier acknowledgement.
        gg.create_deployment_calls.clear()
        status, body = sm_env.deploy_components(
            drop_both, target_devices=[thing_name],
            **{ACK_FIELD: [first]})
        assert status == 409, describe(status, body, gg)
        assert error_code(body) == CODE_ACK_REQUIRED, describe(
            status, body, gg)
        assert gg.create_deployment_calls == [], describe(status, body, gg)
        findings = body_findings(body)
        assert names_of(findings, kind=KIND_DESELECTED,
                        finding_class=CLASS_ACK) == {second}, json.dumps(
                            findings, default=str)
        details = ((body or {}).get('error') or {}).get('details') or {}
        assert details.get('acknowledgement_required_for') == [second]

        # 3. acknowledging the finding computed for the SUBMITTED set proceeds.
        status, body = sm_env.deploy_components(
            drop_both, target_devices=[thing_name],
            **{ACK_FIELD: [second]})
        assert status == 201, describe(status, body, gg)
        [call] = gg.create_deployment_calls
        assert second not in call['components']
        assert first not in call['components']


# ==========================================================================
# Property 6 — one closure, three validators (task 5.4)
# ==========================================================================

GRAPH_SHAPES = ('chain', 'diamond', 'shared_dependency', 'repeated_versions',
                'cycle', 'fan_out')

#: D1 publishes three versions so a BOUNDED requirement and an open one
#: resolve to DIFFERENT versions of the SAME name — the "repeated names at
#: different versions" case, which must still list the name's versions once.
D1_VERSIONS = ('1.0.0', '1.5.0', '2.0.0')
DEAD_GRAPH_DEPENDENCY = 'aws.edgeml.dda.LocalServer.arm64JP4'


def hard(requirement):
    return {'VersionRequirement': requirement, 'DependencyType': 'HARD'}


class ComponentGraph:
    """A generated recipe graph carrying diamonds, shared dependencies,
    repeated names at different versions and cycles.

    ``nodes``  {(name, version): (platforms, dependencies)} — what to publish
    ``roots``  {name: version} — the operator's selected set (LocalServer aside)
    """

    def __init__(self, shape, platform_fault, dead_dependency, deselection,
                 salt):
        self.shape = shape
        self.platform_fault = platform_fault
        self.dead_dependency = dead_dependency
        self.deselection = deselection
        self.r1 = f'model-r1-{salt}-jetson-xavier-jp7'
        self.r2 = f'model-r2-{salt}-jetson-xavier-jp7'
        self.d1 = f'model-d1-{salt}-jetson-xavier-jp7'
        self.d2 = f'model-d2-{salt}-jetson-xavier-jp7'
        self.d3 = f'model-d3-{salt}-jetson-xavier-jp7'
        self.gone = f'model-gone-{salt}-jetson-xavier-jp7'
        self.nodes = {}
        self.roots = {}
        self._build()

    # ------------------------------------------------------------- build
    def _publish(self, name, version, dependencies=None, platforms=None):
        self.nodes[(name, version)] = (
            platforms if platforms is not None else [PLATFORM_AARCH64],
            dependencies)

    def _build(self):
        r1_deps = {}
        if self.shape == 'chain':
            r1_deps[self.d1] = hard('>=1.0.0 <2.0.0')
            for version in D1_VERSIONS:
                self._publish(self.d1, version, {self.d2: hard('>=1.0.0')})
            self._publish(self.d2, '1.0.0')
            self.roots = {self.r1: '1.0.0'}
        elif self.shape == 'diamond':
            r1_deps[self.d1] = hard('>=1.0.0 <2.0.0')
            r1_deps[self.d2] = hard('>=1.0.0')
            for version in D1_VERSIONS:
                self._publish(self.d1, version, {self.d3: hard('>=1.0.0')})
            self._publish(self.d2, '1.0.0', {self.d3: hard('>=1.0.0')})
            self._publish(self.d3, '1.0.0')
            self.roots = {self.r1: '1.0.0'}
        elif self.shape == 'shared_dependency':
            r1_deps[self.d1] = hard('>=1.0.0 <2.0.0')
            for version in D1_VERSIONS:
                self._publish(self.d1, version)
            self._publish(self.r2, '1.0.0', {self.d1: hard('>=1.0.0 <2.0.0')})
            self.roots = {self.r1: '1.0.0', self.r2: '1.0.0'}
        elif self.shape == 'repeated_versions':
            # The SAME dependency name at two different resolved versions.
            r1_deps[self.d1] = hard('>=1.0.0 <2.0.0')
            for version in D1_VERSIONS:
                self._publish(self.d1, version)
            self._publish(self.r2, '1.0.0', {self.d1: hard('>=2.0.0')})
            self.roots = {self.r1: '1.0.0', self.r2: '1.0.0'}
        elif self.shape == 'cycle':
            r1_deps[self.d1] = hard('>=1.0.0')
            self._publish(self.d1, '1.0.0', {self.r1: hard('>=1.0.0')})
            self.roots = {self.r1: '1.0.0'}
        else:                                        # fan_out
            r1_deps[self.d1] = hard('>=1.0.0 <2.0.0')
            r1_deps[self.d2] = hard('>=1.0.0')
            r1_deps[self.d3] = hard('>=1.0.0')
            for version in D1_VERSIONS:
                self._publish(self.d1, version)
            self._publish(self.d2, '1.0.0')
            self._publish(self.d3, '1.0.0')
            self.roots = {self.r1: '1.0.0'}

        if self.dead_dependency:
            # A bounded requirement on a name with ZERO published versions in
            # either namespace — blocking-invalid under 2.5.
            r1_deps[DEAD_GRAPH_DEPENDENCY] = hard(BOUNDED_DEAD_REQUIREMENT)
        if self.deselection:
            r1_deps[self.gone] = hard(UNPINNED_REQUIREMENT)
            self._publish(self.gone, '1.0.0')
        self._publish(
            self.r1, '1.0.0', r1_deps or None,
            platforms=([PLATFORM_JP5, PLATFORM_JP6] if self.platform_fault
                       else [PLATFORM_AARCH64]))

    # ------------------------------------------------------------ helpers
    @property
    def names(self):
        names = {name for name, _version in self.nodes}
        names.add(DEAD_GRAPH_DEPENDENCY)
        return names

    @property
    def operator_pairs(self):
        return ([(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST)]
                + sorted(self.roots.items()))

    @property
    def previous_pairs(self):
        pairs = list(self.operator_pairs)
        if self.deselection:
            pairs.append((self.gone, '1.0.0'))
        return pairs

    def seed_catalog(self, catalog):
        for (name, version), (platforms, dependencies) in self.nodes.items():
            catalog.publish(name, version, platforms=platforms,
                            dependencies=dependencies)

    def seed_fake(self, gg):
        for (name, version), (platforms, dependencies) in self.nodes.items():
            gg.seed_component_version(name, version, platforms=platforms,
                                      dependencies=dependencies)


def component_graphs():
    return st.builds(ComponentGraph, st.sampled_from(GRAPH_SHAPES),
                     st.booleans(), st.booleans(), st.booleans(),
                     st.integers(min_value=1, max_value=9999).map(str))


def projected(findings, names):
    """Findings restricted to the generated graph's component names, as an
    order-insensitive set of (kind, class, name, version, requirement)."""
    return {finding_identity(finding) + (finding.get('finding_class'),)
            for finding in findings
            if finding.get('component_name') in names}


def graph_arn_triples(arns, names):
    """The `(namespace, name, version)` triples of a recorded ARN list,
    restricted to the graph's names — so the auto-included public components'
    own version resolution (`resolve_public_component_version`, which also
    calls GetComponent and ListComponentVersions) cannot pollute the count."""
    triples = []
    for arn in arns:
        namespace, name, version = parse_component_arn(arn)
        if name in names:
            triples.append((namespace, name, version))
    return triples


class TestProperty6OneClosureThreeValidators:
    """**Validates: Requirements 2.1, 2.11.**

    The design claim is "one closure, three validators — not three walks". That
    is only true if each `(name, version)` recipe is read at most ONCE and each
    name's published-version list at most once for the whole validation, and if
    the three validators are pure functions over the already-resolved object.
    """

    # Feature: deployment-preflight-validation, Property 6: each recipe is fetched at most once per validation
    @given(graph=component_graphs())
    @settings(deadline=None)
    def test_each_recipe_and_version_list_is_read_at_most_once(
            self, deployments, graph):
        """For any generated component graph — diamonds, shared dependencies,
        the same name at two different resolved versions, and cycles — the
        closure walk reads each `(name, version)` recipe at most once and each
        name's published-version list at most once, and running the three
        validators over the resolved closure afterwards adds ZERO further
        reads."""
        catalog = Catalog()
        catalog.seed_public_and_local_server()
        graph.seed_catalog(catalog)
        submitted = component_map(graph.operator_pairs + [
            (NUCLEUS, DEVICE_NUCLEUS_VERSION),
            (SHADOW_MANAGER, SHADOW_MANAGER_VERSIONS[0]),
            (LOG_MANAGER, LOG_MANAGER_VERSIONS[0])])
        previous = component_map(graph.previous_pairs)
        roots = {name: entry['componentVersion']
                 for name, entry in submitted.items()}

        closure = preflight.resolve_closure(roots, catalog.fetch_recipe,
                                           catalog.list_versions)

        # Fetched once per DISTINCT (name, version) reached.
        assert len(catalog.recipe_calls) == len(set(catalog.recipe_calls)), (
            f'a recipe was fetched more than once for {graph.shape}: '
            f'{catalog.recipe_calls}')
        reachable = set(catalog.recipes) | set(roots.items())
        assert set(catalog.recipe_calls) <= reachable, (
            f'the walk asked for a pair outside the catalog and the roots: '
            f'{sorted(set(catalog.recipe_calls) - reachable)}')
        assert len(catalog.recipe_calls) <= len(reachable)
        assert closure.recipe_reads == len(catalog.recipe_calls)

        # One published-version listing per NAME, at most.
        assert len(catalog.version_calls) == len(set(catalog.version_calls)), (
            f"a name's published versions were listed more than once for "
            f'{graph.shape}: {catalog.version_calls}')
        known_names = set(catalog.versions) | set(roots) | graph.names
        assert set(catalog.version_calls) <= known_names
        assert len(catalog.version_calls) <= len(known_names)

        # The 'repeated_versions' shape must genuinely exercise the case it
        # names: the same dependency name resolved at two different versions,
        # each recipe read once, its version list read once.
        if graph.shape == 'repeated_versions':
            d1_pairs = {pair for pair in catalog.recipe_calls
                        if pair[0] == graph.d1}
            assert len(d1_pairs) == 2, (
                f'expected {graph.d1} at two distinct versions: {d1_pairs}')
            assert catalog.version_calls.count(graph.d1) == 1

        # Three validators over ONE closure: none of them fetches anything.
        recipe_calls_after_walk = list(catalog.recipe_calls)
        version_calls_after_walk = list(catalog.version_calls)
        preflight.validate_platforms(
            closure, {CE_C_DEVICE: dict(DEVICE_PLATFORM_FULL)})
        preflight.validate_resolvability(closure)
        preflight.validate_deselected_still_required(
            closure, versions_only(previous), versions_only(submitted),
            deployments.PORTAL_AUTO_INCLUDED_COMPONENTS)
        assert catalog.recipe_calls == recipe_calls_after_walk, (
            'a validator re-walked the closure instead of reading it: '
            f'{catalog.recipe_calls[len(recipe_calls_after_walk):]}')
        assert catalog.version_calls == version_calls_after_walk, (
            'a validator re-listed published versions: '
            f'{catalog.version_calls[len(version_calls_after_walk):]}')

    # Feature: deployment-preflight-validation, Property 6: each recipe is fetched at most once per validation
    @given(graph=component_graphs(), thing_name=jp7_thing_names())
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_validators_over_the_preresolved_closure_match_the_endpoint(
            self, deployments, sm_env, graph, thing_name):
        """The three validators run over a closure resolved ONCE outside the
        endpoint produce exactly the findings the endpoint reports for the same
        account and the same submitted set — which is what "validators over one
        closure" means as opposed to three independent walks. Findings are
        compared restricted to the generated graph's component names, because
        the endpoint additionally carries the portal's auto-included entries.

        The endpoint's own GetComponent / ListComponentVersions calls for the
        graph's names are counted too: at most one per `(name, version)` and per
        name on the real path, not only in the pure module."""
        catalog = Catalog()
        catalog.seed_public_and_local_server()
        graph.seed_catalog(catalog)
        submitted = component_map(graph.operator_pairs + [
            (NUCLEUS, DEVICE_NUCLEUS_VERSION),
            (SHADOW_MANAGER, SHADOW_MANAGER_VERSIONS[0]),
            (LOG_MANAGER, LOG_MANAGER_VERSIONS[0])])
        previous = component_map(graph.previous_pairs)
        closure = preflight.resolve_closure(
            {name: entry['componentVersion']
             for name, entry in submitted.items()},
            catalog.fetch_recipe, catalog.list_versions)
        module_findings = (
            preflight.validate_platforms(
                closure, {CE_C_DEVICE: dict(DEVICE_PLATFORM_FULL)})
            + preflight.validate_resolvability(closure)
            + preflight.validate_deselected_still_required(
                closure, versions_only(previous), versions_only(submitted),
                deployments.PORTAL_AUTO_INCLUDED_COMPONENTS))

        gg = fresh_catalog(sm_env, [thing_name])
        graph.seed_fake(gg)
        gg.seed_deployment(
            thing_arn(thing_name),
            {name: {'componentVersion': version}
             for name, version in graph.previous_pairs},
            name=f'ssh-tunnel-on-{thing_name}')
        status, body = sm_env.deploy_components(
            [component_entry(name, version)
             for name, version in graph.operator_pairs],
            target_devices=[thing_name])

        assert_no_existing_gate_fired(status, body, gg)
        assert status in (201, 409), describe(status, body, gg)
        assert projected(body_findings(body), graph.names) == projected(
            module_findings, graph.names), (
            'the endpoint and the pre-resolved closure disagree: endpoint='
            f'{json.dumps(body_findings(body), default=str)} module='
            f'{json.dumps(module_findings, default=str)}')

        recipe_triples = graph_arn_triples(
            [call['arn'] for call in gg.get_component_calls], graph.names)
        assert len(recipe_triples) == len(set(recipe_triples)), (
            f'the endpoint fetched a graph recipe more than once: '
            f'{recipe_triples}')
        # DELIBERATE, not an obvious invariant: on the real path a name is
        # looked up in at most ONE namespace round, but a name that resolves in
        # NEITHER namespace legitimately costs one ListComponentVersions call in
        # each — that is the dual-namespace guard 2.4 mandates (the wrong
        # namespace answers with an EMPTY LIST rather than raising, evidence.md
        # §1.1/§5.6). So the bound on the real path is "at most one call per
        # (namespace, name)", and "at most one per name" holds at the resolver
        # level, which the pure-module test above asserts.
        version_triples = graph_arn_triples(gg.list_component_versions_calls,
                                            graph.names)
        assert len(version_triples) == len(set(version_triples)), (
            f'the endpoint listed the same (namespace, name) more than once: '
            f'{version_triples}')
        for name in graph.names:
            rounds = [triple for triple in version_triples
                      if triple[1] == name]
            assert len(rounds) <= 2, (
                f'{name} was looked up in more than the two namespaces: '
                f'{rounds}')
            if len(rounds) == 2:
                assert not gg.published_versions(name), (
                    f'{name} resolves in a namespace yet was looked up in '
                    f'both: {rounds}')


# ==========================================================================
# Property 7 — the wildcard matcher has teeth on a fully reported device
# (added by task 5's dispatch to close the preservation oracle's gap; see the
#  module docstring)
# ==========================================================================

class TestProperty7WildcardMatcherOnAFullyReportedDevice:
    """**Validates: Requirements 2.2, 2.3, 2.8** (as corrected by evidence.md
    §5.1).

    2.8 is load-bearing for correctness, not a convenience: Nucleus,
    ShadowManager and LogManager are auto-included on EVERY portal deployment
    and publish `{"os": "linux"}` (no architecture) and `{"os": "*"}`, so a
    matcher that required every attribute to be present and literal would
    report them incompatible with every device and refuse every submission.

    The immutable preservation oracle cannot pin this: every `register_device`
    call there omits `platform=`/`architecture=`, so `get_core_device` raises,
    the device platform resolves with no `os`/`architecture`, and its platform
    claims are satisfied by FAIL-OPEN (an undecidable verdict) rather than by
    the matcher deciding. These tests supply a device that reports EVERYTHING
    the matcher needs, so satisfaction and refusal are both decisions.
    """

    # Feature: deployment-preflight-validation, Property 7: the wildcard matcher has teeth on a fully reported device platform
    @given(form=st.sampled_from(sorted(WILDCARD_MANIFEST_FORMS)),
           device_platform=fully_reported_device_platforms())
    @settings(deadline=None)
    def test_every_wildcard_form_is_satisfied_by_a_fully_reported_device(
            self, form, device_platform):
        """All four wildcard forms the account publishes — an ABSENT attribute
        key, a literal `"*"` value, an empty/null `Platform` block, and a
        variant-less aarch64 manifest — are SATISFIED (True, not the
        undecidable None) by ANY device that reports os, architecture and
        variant."""
        platforms = WILDCARD_MANIFEST_FORMS[form]
        assert {'os', 'architecture', 'variant'} <= set(device_platform), (
            'the device platform under test is not fully reported, so a True '
            'verdict would not prove the matcher decided')
        for platform in platforms:
            verdict = preflight.manifest_satisfied(platform, device_platform)
            assert verdict is True, (
                f'{form} manifest {platform!r} was not satisfied by fully '
                f'reported device {device_platform!r}: verdict={verdict!r}')
        assert preflight.platforms_satisfied(
            platforms, device_platform) is True, (
            f'{form} was not satisfied by {device_platform!r}')

    # Feature: deployment-preflight-validation, Property 7: the wildcard matcher has teeth on a fully reported device platform
    @given(device_platform=fully_reported_device_platforms(),
           claimed_variants=st.lists(st.sampled_from(JETPACK_VARIANTS),
                                     min_size=1, max_size=3, unique=True))
    @settings(deadline=None)
    def test_a_variant_mismatched_manifest_is_contradicted_not_undecidable(
            self, device_platform, claimed_variants):
        """The other half of the same matcher: for any fully reported device, a
        manifest set whose every entry names a DIFFERENT variant is
        CONTRADICTED (False), not undecidable — so Counterexample A's refusal is
        a decision and not a fail-open accident. The same manifests stay
        undecidable against a device that reports nothing, which is the
        preservation oracle's shape (2.9)."""
        assume(device_platform['variant'] not in claimed_variants)
        claimed = [{'os': 'linux', 'variant': variant,
                    'architecture': 'aarch64'}
                   for variant in claimed_variants]

        assert preflight.platforms_satisfied(
            claimed, device_platform) is False, (
            f'{claimed!r} was not contradicted by {device_platform!r}')
        assert preflight.platforms_satisfied(claimed, {}) is None
        # Positive control: adding a manifest for the device's OWN variant
        # flips the verdict, so the False above is the variant mismatch and
        # nothing else about these manifests.
        matching = claimed + [{'os': 'linux', 'architecture': 'aarch64',
                               'variant': device_platform['variant']}]
        assert preflight.platforms_satisfied(
            matching, device_platform) is True, matching
        # The verbatim Counterexample A pair against the verbatim JP7 report.
        assert preflight.platforms_satisfied(
            [PLATFORM_JP5, PLATFORM_JP6], DEVICE_PLATFORM_FULL) is False

    # Feature: deployment-preflight-validation, Property 7: the wildcard matcher has teeth on a fully reported device platform
    @given(form=st.sampled_from(sorted(WILDCARD_MANIFEST_FORMS)),
           thing_name=jp7_thing_names())
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_endpoint_deploys_every_wildcard_form_to_a_fully_reported_device(
            self, sm_env, form, thing_name):
        """Endpoint level with a device that reports `platform=linux`,
        `architecture=aarch64` and carries `DEVICES_TABLE.target_architecture
        = arm64_jp7`: a component publishing any of the four wildcard forms is
        submitted, with no finding of any class naming it."""
        gg = fresh_catalog(sm_env, [thing_name], fully_reported=True)
        wildcard = f'model-wildcard-{form}-jetson-xavier-jp7'
        gg.seed_component_version(wildcard, '1.0.0',
                                  platforms=WILDCARD_MANIFEST_FORMS[form])

        status, body = sm_env.deploy_components(
            [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
             component_entry(wildcard, '1.0.0')],
            target_devices=[thing_name])

        assert_no_existing_gate_fired(status, body, gg)
        assert status == 201, describe(status, body, gg)
        assert wildcard not in names_of(body_findings(body))
        [call] = gg.create_deployment_calls
        assert call['components'][wildcard]['componentVersion'] == '1.0.0'
        # The wildcard-bearing auto-includes rode along untouched (2.8).
        assert set(call['components']) >= {NUCLEUS, SHADOW_MANAGER,
                                           LOG_MANAGER, LOCAL_SERVER_JP7}

    # Feature: deployment-preflight-validation, Property 7: the wildcard matcher has teeth on a fully reported device platform
    @given(thing_name=jp7_thing_names(),
           claimed=st.sampled_from([[PLATFORM_JP5], [PLATFORM_JP6],
                                    [PLATFORM_JP5, PLATFORM_JP6]]))
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_endpoint_refuses_a_jp5_jp6_manifest_for_a_fully_reported_jp7_device(
            self, sm_env, thing_name, claimed):
        """The matcher's teeth, on the same fully reported device: a jp5/jp6-
        only manifest is refused as blocking-invalid, the finding carries the
        component's ACTUAL claim and the device's os, architecture AND variant
        as distinct facts (2.3, 2.7), the device is listed as incompatible
        rather than unverified, and no wildcard-bearing auto-include is
        flagged."""
        gg = fresh_catalog(sm_env, [thing_name], fully_reported=True)
        offender = f'model-jp56-only-{thing_name}-jetson-xavier'
        gg.seed_component_version(offender, '1.0.0', platforms=claimed)

        status, body = sm_env.deploy_components(
            [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
             component_entry(offender, '1.0.0')],
            target_devices=[thing_name])

        assert_no_existing_gate_fired(status, body, gg)
        assert status == 409, describe(status, body, gg)
        assert error_code(body) == CODE_BLOCKING, describe(status, body, gg)
        assert gg.create_deployment_calls == [], describe(status, body, gg)
        findings = body_findings(body)
        assert_each_finding_classified_exactly_once(findings)
        assert_wildcard_components_never_flagged(
            [f for f in findings if f.get('finding_class') == CLASS_BLOCKING])
        [finding] = [f for f in findings
                     if f.get('component_name') == offender
                     and f.get('kind') == KIND_PLATFORM]
        assert finding['finding_class'] == CLASS_BLOCKING, finding
        # The device platform was FULLY reported, so the refusal is a decision
        # and not the fail-open path the preservation oracle exercises.
        [device] = finding['devices']
        assert device['thing_name'] == thing_name, finding
        assert device['platform'] == DEVICE_PLATFORM_FULL, finding
        assert (finding.get('unverified_devices') or []) == [], finding
        # 2.7: the device's platform is never presented as the component's
        # claim — the Greengrass clause that misdirected the incident.
        assert TARGET_ARCHITECTURE_JP7 not in json.dumps(
            finding['claimed_platforms'], default=str), finding

    def test_the_preservation_oracle_shape_passes_only_by_fail_open(
            self, sm_env):
        """The gap this property closes, stated as an executable fact rather
        than a claim in a docstring.

        With the preservation oracle's device shape — `register_device` called
        with no `platform=`/`architecture=`, so `get_core_device` raises, and no
        `DEVICES_TABLE.target_architecture` — the SAME jp5/jp6-only component
        that the fully reported device refuses above is PERMITTED, because the
        judgement is undecidable and 2.9 forbids blocking on a check that could
        not be performed. That is why the oracle's platform claims cannot pin
        the matcher, and why Property 7 exists."""
        gg = FakeGreengrass()
        sm_env.gg = gg
        seed_public_catalog(gg)
        seed_local_server(gg)
        # The oracle's shape, verbatim: no platform, no architecture, and no
        # portal device record to supply the variant.
        gg.register_device('preservation-shaped-device',
                           local_server_version=LOCAL_SERVER_JP7_LATEST,
                           arch='arm64JP7')
        offender = 'model-jp56-only-oracle-shape-jetson-xavier'
        gg.seed_component_version(offender, '1.0.0',
                                  platforms=[PLATFORM_JP5, PLATFORM_JP6])

        status, body = sm_env.deploy_components(
            [component_entry(LOCAL_SERVER_JP7, LOCAL_SERVER_JP7_LATEST),
             component_entry(offender, '1.0.0')],
            target_devices=['preservation-shaped-device'])

        assert status == 201, describe(status, body, gg)
        [call] = gg.create_deployment_calls
        assert offender in call['components']
