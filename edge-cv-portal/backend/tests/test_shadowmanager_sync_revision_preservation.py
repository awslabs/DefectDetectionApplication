"""Preservation property tests — shadowmanager-sync-config-on-revision
task 2 (written BEFORE the fix, observation-first).

Bugfix spec: .kiro/specs/shadowmanager-sync-config-on-revision/

Property 2: Preservation — Everything Outside the Bug Condition Is
Unchanged.
**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

Every assertion here encodes behavior OBSERVED on the UNFIXED tree and
must keep passing after task 3 lands ``ensure_shadow_manager_sync`` at
both call sites:

- **Fresh-deploy auto-include identity (3.1)**: the fresh-add submitted
  ShadowManager entry (pinned version, byte-exact merge string,
  ``auto_included`` entry incl. the verbatim reason string) is pinned as
  the reference the fixed tree must reproduce byte-identically.
- **Compliant-merge no-op PBT (3.2, 3.3)**: _for any_ ShadowManager entry
  already carrying all three portal shadow names (plus generated extra
  names, direction/classic values, unknown keys, and arbitrary JSON
  serialization whitespace), the submitted merge STRING is byte-identical
  to the input. End-to-end through ``create_workflow_deployment`` — the
  one real submission path where a pre-existing merge string rides into
  the submitted document. (Observed on the unfixed tree:
  ``create_deployment``'s request parse keeps ``component_name`` +
  ``component_version`` ONLY, so a caller merge cannot enter through the
  HTTP body at all; that parse is explicitly out of the fix's scope —
  design "Explicitly NOT changed". On the unfixed tree this passes via
  the verbatim components copy; on the fixed tree 'unchanged' must not
  re-serialize.)
- **Non-ShadowManager map identity (3.4)**: _for any_ generated component
  set, the submitted components map MINUS the ShadowManager key
  deep-equals the unfixed capture — encoded as a deterministic oracle of
  the observed unfixed behavior (caller entries verbatim, the
  LogManager/Nucleus auto-includes byte-exact) — through BOTH endpoints.
- **Explicit-version identity (3.5)**: caller/copied ShadowManager
  entries with explicit versions keep them verbatim — both endpoints.
- **Workflow carry-over identity (3.6)**: carried-over components
  verbatim, workflow entry (re)placed at the resolved registered version,
  deployment name reused.

Honesty guard (design Decision 7): every assertion is about the SUBMITTED
deployment document (the FakeGreengrass ``create_deployment_calls``
capture) — nothing here touches a real device or account.

--------------------------------------------------------------------------
RECORDED BASELINE SUITE COUNTS (UNFIXED tree, branch
spec/jetpack7-support) — all green, recorded from actual runs; these must
be reproduced at task 3.4 / task 5 (the ONLY intended diff anywhere = the
task-3.1 recorded repoint below):

  Fresh-deploy auto-include identity (3.1), backend WITH conftest:
    tests/test_deployment_shadow_manager.py ................ 4 passed
    tests/test_model_status_shadow_sync.py ................. 2 passed

  Workflow carry-over identity (3.6), backend WITH conftest:
    tests/test_workflow_deploy_subscribe_merge_exploration.py  3 passed
    tests/test_workflow_deploy_subscribe_merge_preservation.py 3 passed
    tests/test_workflow_deploy_component_version_exploration.py 5 passed
    tests/test_workflow_deploy_component_version_preservation.py 6 passed
    tests/test_workflow_packaging_deployment_integration.py . 11 passed
    tests/test_camera_binding_submission.py ................ 10 passed
  (none of these carry ShadowManager fixtures — they must stay green
  UNMODIFIED through the whole spec)

  Frontend preload identity (3.4 UI leg), from edge-cv-portal/frontend:
    npx vitest run src/pages/CreateDeployment.archFilter.test.tsx
      ......................................... 11 passed (1 file)
  (the archFilter revise fixture preloads existing components WITHOUT
  ShadowManager — the without-ShadowManager preload-unchanged identity;
  the WITH-ShadowManager preload case lives in the task-1 vitest file)

  This suite (on the UNFIXED tree): 5 passed.

--------------------------------------------------------------------------
VERBATIM RECORD (design Decision 6 — the ONE conscious pinned-test
repoint target). The current UNFIXED assertions of
test_deployment_shadow_manager.py::TestShadowManagerAutoInclude::
test_caller_supplied_shadow_manager_is_not_overridden are, verbatim:

    def test_caller_supplied_shadow_manager_is_not_overridden(self, sm_env):
        \"\"\"When the caller already includes aws.greengrass.ShadowManager the
        auto-include is skipped: their pinned version/config is submitted
        untouched and no auto_included entry is reported.\"\"\"
        status, payload = sm_env.deploy_components(
            [LOCAL_SERVER_COMPONENT,
             {"component_name": "aws.greengrass.ShadowManager",
              "component_version": "2.3.5"}],
            target_devices=["line-a-camera-01"])

        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        assert call["components"]["aws.greengrass.ShadowManager"] == {
            "componentVersion": "2.3.5"}
        assert [e for e in payload["auto_included"]
                if e["component_name"] == "aws.greengrass.ShadowManager"] == []

That test pins the exact defect (requirement 1.1: a caller-supplied entry
is submitted untouched — bare). Task 3.1 repoints EXACTLY this test to
the 2.1/3.5 contract (caller's ``2.3.5`` version submitted verbatim, the
entry now carrying the full portal synchronize merge, still no
``auto_included`` entry) and NOTHING else in that file. Recording the old
assertions here makes the 3.1 diff auditable.
--------------------------------------------------------------------------
"""
import copy
import json
import sys
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import WorkflowStoreEnv
from test_deployment_shadow_manager import (
    LOCAL_SERVER_COMPONENT, ShadowManagerEnv)
from test_workflow_deploy_subscribe_merge_exploration import (
    LOCAL_SERVER_ARM64JP6, WorkflowDeployEnv)

SHADOW_MANAGER = "aws.greengrass.ShadowManager"

# The portal shadow names (deployments.py module constants).
PORTAL_SHADOW_NAMES = ["dda-camera-registry", "dda-camera-bindings",
                       "dda-model-status"]


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments (and its workflow_guards binding) inside the
    moto mock so module-level boto3 clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


@pytest.fixture
def sm_env(env, deployments, monkeypatch):
    return ShadowManagerEnv(env, deployments, monkeypatch)


@pytest.fixture
def wf_env(env, deployments, monkeypatch):
    return WorkflowDeployEnv(env, deployments, monkeypatch)


# ==========================================================================
# Observed fresh-add reference (UNFIXED capture, pinned byte-exact)
# ==========================================================================

# The exact dict the unfixed auto-include block serializes (same key
# order), so json.dumps() reproduces the submitted merge string
# byte-for-byte.
FRESH_ADD_MERGE_DOC = {
    "synchronize": {
        "direction": "betweenDeviceAndCloud",
        "coreThing": {
            "classic": True,
            "namedShadows": ["dda-camera-registry", "dda-camera-bindings",
                             "dda-model-status"],
        },
    }
}

# The auto_included reason string, verbatim from the unfixed tree.
FRESH_ADD_REASON = (
    "Syncs the dda-camera-registry, dda-camera-bindings and "
    "dda-model-status named shadows with IoT Core for camera registry "
    "synchronization and model GPU-fallback status visibility")


class TestFreshAddIdentityPin:
    """**Validates: Requirements 3.1**

    Fresh deployment (component set WITHOUT ShadowManager): the submitted
    entry and the ``auto_included`` report, captured on the UNFIXED tree
    and pinned byte-exact. The fixed 'added' path must reproduce this
    verbatim."""

    def test_fresh_add_submitted_entry_and_report_pinned_byte_exact(
            self, sm_env):
        # Validates: Requirements 3.1
        status, payload = sm_env.deploy_components(
            [LOCAL_SERVER_COMPONENT], target_devices=["line-a-camera-01"])

        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        entry = call["components"][SHADOW_MANAGER]

        # No resolvable running Nucleus in this harness -> the static pin.
        assert entry == {
            "componentVersion": sm_env.deployments.SHADOW_MANAGER_VERSION,
            "configurationUpdate": {
                # Byte-exact merge string, not just parsed equality.
                "merge": json.dumps(FRESH_ADD_MERGE_DOC),
            },
        }
        assert sm_env.deployments.SHADOW_MANAGER_VERSION == "2.3.15"

        # The auto_included entry, pinned whole (reason string verbatim).
        [reported] = [e for e in payload["auto_included"]
                      if e["component_name"] == SHADOW_MANAGER]
        assert reported == {
            "component_name": SHADOW_MANAGER,
            "component_version": sm_env.deployments.SHADOW_MANAGER_VERSION,
            "reason": FRESH_ADD_REASON,
        }


# ==========================================================================
# Generators
# ==========================================================================

# Caller-extra shadow names beyond the portal set (3.2).
EXTRA_SHADOW_POOL = ["custom-shadow", "site-telemetry", "ops-overrides"]

# Non-portal direction values a caller may have set (3.3 — survive as-is).
DIRECTION_POOL = ["betweenDeviceAndCloud", "deviceToCloud", "cloudToDevice"]

# Explicit component versions (valid per the create_deployment parse's
# skip list AND the FakeGreengrass componentVersion requirement).
VERSION_POOL = ["2.3.15", "2.3.5", "2.4.0", "1.0.7"]

# Benign non-ShadowManager component names: no dda.plugin.* / model-vllm-*
# / dda.workflow.* prefixes, so the plugin/vLLM/subscribe gates all no-op.
EXTRA_COMPONENT_POOL = ["com.example.telemetry", "com.example.vision",
                        "com.thirdparty.agent"]

# Pre-existing configurationUpdate merges on carried-over non-ShadowManager
# entries (3.4): parseable and non-parseable alike must survive verbatim.
NON_SM_MERGE_POOL = [
    json.dumps({"logging": {"level": "DEBUG"}}),
    json.dumps({"limits": {"cpu": 2}}, indent=2),
    "not-json{",
]


@st.composite
def compliant_merge_strings(draw):
    """A synchronize merge document already carrying ALL THREE portal
    shadow names (in any order, with optional caller extras), an explicit
    direction and classic value, optional unknown keys at every level, and
    arbitrary JSON serialization whitespace/key-order — i.e. NOT the bug
    condition. The submitted string must come back byte-identical."""
    extras = draw(st.lists(st.sampled_from(EXTRA_SHADOW_POOL),
                           unique=True, max_size=3))
    named_shadows = list(draw(st.permutations(
        PORTAL_SHADOW_NAMES + extras)))

    core_thing = {
        "classic": draw(st.booleans()),
        "namedShadows": named_shadows,
    }
    if draw(st.booleans()):
        core_thing["maxOutboundSyncUpdatesPerSecond"] = draw(
            st.integers(1, 200))

    synchronize = {
        "direction": draw(st.sampled_from(DIRECTION_POOL)),
        "coreThing": core_thing,
    }
    if draw(st.booleans()):
        synchronize["shadowDocumentsMap"] = {
            "custom": {"maxDepth": draw(st.integers(1, 8))}}

    doc = {"synchronize": synchronize}
    if draw(st.booleans()):
        doc["strategy"] = {"type": "realTime"}

    # Serialization variance: a re-serialization inside the pipeline would
    # normalize whitespace/key order and betray itself byte-wise.
    indent = draw(st.sampled_from([None, 1, 2]))
    sort_keys = draw(st.booleans())
    return json.dumps(doc, indent=indent, sort_keys=sort_keys)


@st.composite
def shadow_manager_entries(draw):
    """A previous-revision ShadowManager entry in ANY observed state:
    bare (the thor1 shape), stale (a strict subset of the portal names),
    or compliant — always with an explicit componentVersion."""
    version = draw(st.sampled_from(VERSION_POOL))
    kind = draw(st.sampled_from(["bare", "stale", "compliant"]))
    if kind == "bare":
        return {"componentVersion": version}
    if kind == "stale":
        subset = draw(st.lists(st.sampled_from(PORTAL_SHADOW_NAMES),
                               unique=True, max_size=2))
        doc = {"synchronize": {
            "direction": "betweenDeviceAndCloud",
            "coreThing": {"classic": True, "namedShadows": subset},
        }}
        return {"componentVersion": version,
                "configurationUpdate": {"merge": json.dumps(doc)}}
    return {"componentVersion": version,
            "configurationUpdate": {
                "merge": draw(compliant_merge_strings())}}


@st.composite
def caller_component_sets(draw):
    """A create_deployment request component list: LocalServer (so
    needs_nucleus fires) plus generated benign extras, plus optionally a
    bare caller-supplied ShadowManager entry (the request parse keeps
    name+version only, so bare is the ONLY caller shape that path
    admits)."""
    components = [dict(LOCAL_SERVER_COMPONENT)]
    extra_names = draw(st.lists(st.sampled_from(EXTRA_COMPONENT_POOL),
                                unique=True, max_size=3))
    for name in extra_names:
        components.append({"component_name": name,
                           "component_version": draw(
                               st.sampled_from(VERSION_POOL))})
    include_sm = draw(st.booleans())
    if include_sm:
        components.append({
            "component_name": SHADOW_MANAGER,
            "component_version": draw(st.sampled_from(VERSION_POOL))})
    return components


@st.composite
def previous_revision_components(draw):
    """A seeded previous-revision components map for the workflow path:
    LocalServer (optionally with its own configurationUpdate), generated
    extras (optionally with parseable/corrupt merges), optionally a
    ShadowManager entry in any state, plus the old workflow entry (added
    by the test, which knows the workflow_id)."""
    components = {}
    local_server = {"componentVersion": "1.0.51"}
    if draw(st.booleans()):
        local_server["configurationUpdate"] = {
            "merge": draw(st.sampled_from(NON_SM_MERGE_POOL))}
    components[LOCAL_SERVER_ARM64JP6] = local_server

    for name in draw(st.lists(st.sampled_from(EXTRA_COMPONENT_POOL),
                              unique=True, max_size=3)):
        entry = {"componentVersion": draw(st.sampled_from(VERSION_POOL))}
        if draw(st.booleans()):
            entry["configurationUpdate"] = {
                "merge": draw(st.sampled_from(NON_SM_MERGE_POOL))}
        components[name] = entry

    if draw(st.booleans()):
        components[SHADOW_MANAGER] = draw(shadow_manager_entries())
    return components


# ==========================================================================
# Per-example environment builders (fresh fakes + fresh Use_Case per
# Hypothesis example; only session/module-scoped fixtures enter @given)
# ==========================================================================

def build_sm_env(aws_stack, deployments, mp):
    return ShadowManagerEnv(WorkflowStoreEnv(aws_stack), deployments, mp)


def build_wf_env(aws_stack, deployments, mp):
    return WorkflowDeployEnv(WorkflowStoreEnv(aws_stack), deployments, mp)


def seed_workflow_revision(wf_env, previous_components):
    """Seed a v2 non-subscribing workflow and a previous latest-for-target
    deployment carrying `previous_components` + the old workflow entry.
    Returns (workflow_id, deepcopied expected carry-over map)."""
    workflow_id = wf_env.seed_subscribing_workflow(version=2, topics=None)
    wf_env.register_device()
    seeded = copy.deepcopy(previous_components)
    seeded[f"dda.workflow.{workflow_id}"] = {"componentVersion": "1.0.0"}
    # deepcopy again for seeding: FakeGreengrass stores the entry dicts by
    # reference and the handler mutates its (shallow) copy in place, so
    # the expectation must be an independent snapshot.
    wf_env.gg.seed_deployment(wf_env.thing_arn(), copy.deepcopy(seeded),
                              name="fleet-alpha")
    return workflow_id, seeded


# ==========================================================================
# Compliant-merge no-op PBT (3.2, 3.3)
# ==========================================================================

# Example counts come from the conftest Hypothesis profiles (portal-fast
# locally, ci with HYPOTHESIS_PROFILE=ci) — never hardcoded here.
@settings(deadline=None)
@given(merge_string=compliant_merge_strings(),
       version=st.sampled_from(VERSION_POOL))
def test_compliant_merge_passes_through_byte_identical(
        aws_stack, deployments, merge_string, version):
    """**Property 2: Preservation — compliant-merge no-op.**
    # Validates: Requirements 3.2, 3.3

    _For any_ previous-revision ShadowManager entry already carrying all
    three portal shadow names — with generated extra caller names,
    direction/classic values, unknown keys, and arbitrary serialization
    whitespace — a workflow revision submits the merge STRING byte-
    identical to the input (and the whole entry untouched). Observed on
    the unfixed tree (verbatim components copy); the fixed tree's
    'unchanged' outcome must not re-serialize."""
    with pytest.MonkeyPatch.context() as mp:
        wf_env = build_wf_env(aws_stack, deployments, mp)
        workflow_id, seeded = seed_workflow_revision(wf_env, {
            LOCAL_SERVER_ARM64JP6: {"componentVersion": "1.0.51"},
            SHADOW_MANAGER: {
                "componentVersion": version,
                "configurationUpdate": {"merge": merge_string},
            },
        })

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        submitted = wf_env.submitted_components()[SHADOW_MANAGER]
        # Byte-identity: the exact merge string, not just parsed equality
        # (caller extras, field values, unknown keys, whitespace — 3.2/3.3).
        assert submitted["configurationUpdate"]["merge"] == merge_string
        assert submitted == seeded[SHADOW_MANAGER]


# ==========================================================================
# Non-ShadowManager map identity — create_deployment endpoint (3.4)
# ==========================================================================

def expected_log_manager_entry(deployments, submitted_component_names):
    """The LogManager entry the UNFIXED create_deployment builds, as a
    deterministic oracle: com.aws.greengrass plus one per-component entry
    for every caller component (insertion order), pinned to the static
    LOG_MANAGER_VERSION fallback (no resolvable running Nucleus in this
    harness). Byte-exact via identical dict construction order."""
    per_component = {
        "minimumLogLevel": "INFO",
        "diskSpaceLimit": 10,
        "diskSpaceLimitUnit": "MB",
        "deleteLogFileAfterCloudUpload": False,
    }
    component_log_config_map = {"com.aws.greengrass": dict(per_component)}
    for name in submitted_component_names:
        if name not in {"aws.greengrass.Nucleus", "aws.greengrass.LogManager"}:
            component_log_config_map[name] = dict(per_component)
    log_manager_config = {
        "logsUploaderConfiguration": {
            "systemLogsConfiguration": {
                "uploadToCloudWatch": True,
                "minimumLogLevel": "INFO",
                "diskSpaceLimit": 25,
                "diskSpaceLimitUnit": "MB",
                "deleteLogFileAfterCloudUpload": False,
            },
            "componentLogsConfigurationMap": component_log_config_map,
            "periodicUploadIntervalSec": 300,
        }
    }
    return {
        "componentVersion": deployments.LOG_MANAGER_VERSION,
        "configurationUpdate": {"merge": json.dumps(log_manager_config)},
    }


@settings(deadline=None)
@given(components=caller_component_sets())
def test_create_deployment_non_shadowmanager_map_identity(
        aws_stack, deployments, components):
    """**Property 2: Preservation — non-ShadowManager map identity
    (create_deployment endpoint).**
    # Validates: Requirements 3.4

    _For any_ generated caller component set, the submitted components
    map MINUS the ShadowManager key deep-equals the unfixed capture:
    caller entries verbatim ({componentVersion} only — the request parse
    keeps name+version), the LogManager auto-include byte-exact per the
    oracle, the Nucleus auto-include the unpinned store-limit fallback.
    The auto_included report carries a ShadowManager entry exactly when
    the caller set omitted it (Decision 4 keeps 'merged' silent)."""
    with pytest.MonkeyPatch.context() as mp:
        sm_env = build_sm_env(aws_stack, deployments, mp)
        status, payload = sm_env.deploy_components(
            copy.deepcopy(components), target_devices=["line-a-camera-01"])

        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        submitted = dict(call["components"])
        submitted.pop(SHADOW_MANAGER, None)

        caller_names = [c["component_name"] for c in components]
        expected = {
            c["component_name"]: {"componentVersion": c["component_version"]}
            for c in components if c["component_name"] != SHADOW_MANAGER}
        expected["aws.greengrass.LogManager"] = expected_log_manager_entry(
            deployments, caller_names)
        # No resolvable running Nucleus -> the unpinned fallback entry
        # carrying only the store-limit merge.
        expected["aws.greengrass.Nucleus"] = {
            "configurationUpdate":
                deployments._nucleus_store_configuration_update()}

        assert submitted == expected

        # auto_included: LogManager + Nucleus always; ShadowManager exactly
        # when the caller set omitted it (unfixed gate skip == fixed
        # 'merged' silence, Decision 4).
        reported_names = [e["component_name"] for e in payload["auto_included"]]
        assert reported_names.count("aws.greengrass.LogManager") == 1
        assert reported_names.count("aws.greengrass.Nucleus") == 1
        expected_sm_reports = 0 if SHADOW_MANAGER in caller_names else 1
        assert reported_names.count(SHADOW_MANAGER) == expected_sm_reports


# ==========================================================================
# Non-ShadowManager map identity + carry-over — workflow endpoint (3.4, 3.6)
# ==========================================================================

@settings(deadline=None)
@given(previous=previous_revision_components())
def test_workflow_revision_non_shadowmanager_carry_over_identity(
        aws_stack, deployments, previous):
    """**Property 2: Preservation — non-ShadowManager map identity and
    carry-over (create_workflow_deployment endpoint).**
    # Validates: Requirements 3.4, 3.6

    _For any_ generated previous-revision component map (extras with
    parseable AND non-parseable configurationUpdate merges, ShadowManager
    optionally present in any state), a workflow revision submits every
    non-ShadowManager entry verbatim, (re)places ONLY the workflow entry
    at the resolved registered version, and reuses the deployment name.
    Observed on the unfixed tree (verbatim copy); the fixed ensure step
    may touch nothing but the ShadowManager entry."""
    with pytest.MonkeyPatch.context() as mp:
        wf_env = build_wf_env(aws_stack, deployments, mp)
        workflow_id, seeded = seed_workflow_revision(wf_env, previous)

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        assert payload["is_revision"] is True
        assert payload["component_version"] == "2.0.0"

        [call] = wf_env.gg.create_deployment_calls
        submitted = dict(call["components"])
        submitted.pop(SHADOW_MANAGER, None)

        expected = {name: entry for name, entry in seeded.items()
                    if name != SHADOW_MANAGER}
        # The workflow entry is (re)placed at the registered version (3.6).
        expected[f"dda.workflow.{workflow_id}"] = {
            "componentVersion": "2.0.0"}

        assert submitted == expected
        # ShadowManager never APPEARS on the workflow path unless the
        # previous revision carried it (presence-gated by design; a fresh
        # workflow map must not start auto-including it).
        assert (SHADOW_MANAGER in call["components"]) == (
            SHADOW_MANAGER in seeded)

        # Revision identity preserved: name reused, same target (3.6).
        assert call["deploymentName"] == "fleet-alpha"
        assert call["targetArn"] == wf_env.thing_arn()


# ==========================================================================
# Explicit-version identity — both endpoints (3.5)
# ==========================================================================

@settings(deadline=None)
@given(version=st.sampled_from(VERSION_POOL),
       sm_entry=shadow_manager_entries())
def test_explicit_shadow_manager_version_kept_verbatim_both_endpoints(
        aws_stack, deployments, version, sm_entry):
    """**Property 2: Preservation — explicit-version identity.**
    # Validates: Requirements 3.5

    A caller-supplied (create_deployment) or carried-over
    (create_workflow_deployment) ShadowManager entry with an explicit
    componentVersion keeps that exact version in the submitted document —
    in EVERY entry state. Observed on the unfixed tree (entry untouched);
    the fixed helper must respect it rather than re-resolving."""
    with pytest.MonkeyPatch.context() as mp:
        sm_env = build_sm_env(aws_stack, deployments, mp)
        status, payload = sm_env.deploy_components(
            [dict(LOCAL_SERVER_COMPONENT),
             {"component_name": SHADOW_MANAGER,
              "component_version": version}],
            target_devices=["line-a-camera-01"])
        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        assert (call["components"][SHADOW_MANAGER]["componentVersion"]
                == version)

    with pytest.MonkeyPatch.context() as mp:
        wf_env = build_wf_env(aws_stack, deployments, mp)
        workflow_id, seeded = seed_workflow_revision(wf_env, {
            LOCAL_SERVER_ARM64JP6: {"componentVersion": "1.0.51"},
            SHADOW_MANAGER: sm_entry,
        })
        status, payload = wf_env.deploy(workflow_id)
        assert status == 201, payload
        submitted = wf_env.submitted_components()[SHADOW_MANAGER]
        assert (submitted["componentVersion"]
                == seeded[SHADOW_MANAGER]["componentVersion"])
