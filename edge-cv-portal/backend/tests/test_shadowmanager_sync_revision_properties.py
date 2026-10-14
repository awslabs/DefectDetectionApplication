"""Fix-checking property tests — shadowmanager-sync-config-on-revision.

Bugfix spec: .kiro/specs/shadowmanager-sync-config-on-revision/

This file hosts TWO property suites (per the task plan):

- **Property 3 (task 4.1, THIS section): Fix Checking — Union-Not-Replace
  Merge Semantics (helper level).**
  **Validates: Requirements 2.1, 2.2, 3.2, 3.3, 3.5**
  _For any_ generated submitted-entry shape — bare (no
  configurationUpdate), stale merge (any strict subset of the portal
  names, with/without extra caller names, with/without explicit
  direction/classic values, with unknown extra keys), corrupt merge
  (non-JSON string, non-dict synchronize/coreThing nodes, non-list
  namedShadows), and compliant merge — ``ensure_shadow_manager_sync``
  produces an entry whose parsed merge contains all portal names AND
  every parseable caller-supplied name AND every caller-supplied field
  value (union, setdefault-only defaults), respects an explicit
  componentVersion without calling the resolver, resolves a missing one
  exactly once, and returns 'unchanged' with a byte-identical merge
  string for compliant input.

- **Property 4 (task 4.2): Fix Checking — Both Call Sites End-to-End** —
  APPENDED BELOW the Property 3 section by task 4.2 (do not interleave).

Property 3 exercises the helper DIRECTLY (no endpoint, no fakes): the
resolver is a recording stub, so the laziness contract (design Decision
2 — zero-arg closure, called at most once, only for a version-less
entry) is asserted exactly. Example counts come from the conftest
Hypothesis profiles (portal-fast locally, ci with HYPOTHESIS_PROFILE=ci)
— never hardcoded here.
"""
import copy
import json
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import WorkflowStoreEnv
from test_deployment_shadow_manager import (
    LOCAL_SERVER_COMPONENT, ShadowManagerEnv)
from test_workflow_deploy_subscribe_merge_exploration import (
    LOCAL_SERVER_ARM64JP6, WorkflowDeployEnv)

SHADOW_MANAGER = "aws.greengrass.ShadowManager"

# The portal shadow names (deployments.py module constants; hardcoded here
# on purpose so the tests pin the CONTRACT, not whatever the constants say).
PORTAL_SHADOW_NAMES = ["dda-camera-registry", "dda-camera-bindings",
                       "dda-model-status"]

# Caller-extra shadow names beyond the portal set (3.2 — must survive).
EXTRA_SHADOW_POOL = ["custom-shadow", "site-telemetry", "ops-overrides"]

# Non-portal direction values a caller may have set (3.3 — survive as-is).
DIRECTION_POOL = ["betweenDeviceAndCloud", "deviceToCloud", "cloudToDevice"]

# Explicit component versions callers/copies carry.
VERSION_POOL = ["2.3.15", "2.3.5", "2.4.0", "1.0.7"]

# What the recording stub resolver returns — deliberately NOT a real
# ShadowManager version, so a resolved version is unmistakable in failures.
RESOLVED_VERSION = "9.9.9-resolved-by-stub"

# Sentinel for "this key was absent from the generated document".
_MISSING = object()


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments (and its workflow_guards binding) inside the
    moto mock so module-level boto3 clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


def run_helper(deployments, entry):
    """Run ensure_shadow_manager_sync on a single-entry components map with
    a recording stub resolver. Returns (result, submitted_entry, calls)."""
    calls = []

    def resolve_version():
        calls.append(1)
        return RESOLVED_VERSION

    components_map = {SHADOW_MANAGER: copy.deepcopy(entry)}
    result = deployments.ensure_shadow_manager_sync(
        components_map, resolve_version)
    return result, components_map[SHADOW_MANAGER], calls


# ==========================================================================
# ==========================================================================
# Property 3 (task 4.1) — Union-Not-Replace Merge Semantics, helper level
# **Validates: Requirements 2.1, 2.2, 3.2, 3.3, 3.5**
# ==========================================================================
# ==========================================================================

@st.composite
def submitted_entry_cases(draw):
    """A generated submitted ShadowManager entry in ANY shape either call
    site can hand the helper, plus a meta record of what was generated so
    the test can assert preservation/union without re-implementing the
    helper. Shapes: bare (no configurationUpdate / empty configurationUpdate
    / empty merge string), corrupt (non-JSON or non-dict document), and
    parseable documents with independently absent/corrupt/valid
    synchronize -> coreThing -> namedShadows nodes (valid named lists range
    from the empty list through strict stale subsets to fully compliant,
    with optional caller extras, direction/classic values, and unknown keys
    at every level)."""
    explicit_version = draw(
        st.one_of(st.none(), st.sampled_from(VERSION_POOL)))

    meta = {
        "explicit_version": explicit_version,
        "merge_string": None,     # the generated merge string, if any
        "parseable": False,       # merge parses to a JSON object
        "sync_is_dict": False,
        "core_is_dict": False,
        "named": None,            # the original namedShadows LIST, if a list
        "direction": _MISSING,
        "classic": _MISSING,
        "doc_unknown": {},
        "sync_unknown": {},
        "core_unknown": {},
        "compliant": False,       # named list already covers portal names
        "cu_siblings": {},        # sibling configurationUpdate keys
    }

    entry = {}
    if explicit_version is not None:
        entry["componentVersion"] = explicit_version

    shape = draw(st.sampled_from(
        ["bare_no_cu", "bare_empty_cu", "bare_empty_merge", "non_json"]
        + ["doc"] * 6))

    if shape == "bare_no_cu":
        return entry, meta

    config_update = {}
    if draw(st.booleans()):
        # A sibling configurationUpdate key (the API also supports `reset`);
        # the helper must only ever touch `merge`.
        config_update["reset"] = [""]
        meta["cu_siblings"] = {"reset": [""]}
    entry["configurationUpdate"] = config_update

    if shape == "bare_empty_cu":
        return entry, meta
    if shape == "bare_empty_merge":
        config_update["merge"] = ""
        return entry, meta
    if shape == "non_json":
        # Unparseable, or parseable but not a JSON object — both corrupt.
        config_update["merge"] = draw(st.sampled_from(
            ["not-json{", "[1, 2, 3]", "42", "\"hello\"", "null"]))
        return entry, meta

    # shape == "doc": a parseable JSON object document.
    meta["parseable"] = True
    doc = {}
    if draw(st.booleans()):
        doc["strategy"] = {"type": "realTime"}
        meta["doc_unknown"] = {"strategy": {"type": "realTime"}}

    sync_kind = draw(st.sampled_from(["dict"] * 5 + ["absent", "corrupt"]))
    if sync_kind == "corrupt":
        doc["synchronize"] = draw(st.sampled_from(["on", 7, [1], True]))
    elif sync_kind == "dict":
        meta["sync_is_dict"] = True
        sync = {}
        if draw(st.booleans()):
            sync["direction"] = draw(st.sampled_from(DIRECTION_POOL))
            meta["direction"] = sync["direction"]
        if draw(st.booleans()):
            sync["shadowDocumentsMap"] = {
                "custom": {"maxDepth": draw(st.integers(1, 8))}}
            meta["sync_unknown"] = {
                "shadowDocumentsMap": sync["shadowDocumentsMap"]}

        core_kind = draw(st.sampled_from(["dict"] * 5 + ["absent", "corrupt"]))
        if core_kind == "corrupt":
            sync["coreThing"] = draw(st.sampled_from(["nope", 5, True]))
        elif core_kind == "dict":
            meta["core_is_dict"] = True
            core = {}
            if draw(st.booleans()):
                core["classic"] = draw(st.booleans())
                meta["classic"] = core["classic"]
            if draw(st.booleans()):
                core["maxOutboundSyncUpdatesPerSecond"] = draw(
                    st.integers(1, 200))
                meta["core_unknown"] = {
                    "maxOutboundSyncUpdatesPerSecond":
                        core["maxOutboundSyncUpdatesPerSecond"]}

            named_kind = draw(st.sampled_from(
                ["list"] * 5 + ["absent", "corrupt"]))
            if named_kind == "corrupt":
                core["namedShadows"] = draw(st.sampled_from(
                    ["dda-camera-registry", 5, {"a": 1}, True]))
            elif named_kind == "list":
                # From the empty list through strict stale subsets up to
                # the full portal set, interleaved with caller extras in
                # any order.
                subset = draw(st.lists(
                    st.sampled_from(PORTAL_SHADOW_NAMES), unique=True))
                extras = draw(st.lists(
                    st.sampled_from(EXTRA_SHADOW_POOL), unique=True,
                    max_size=3))
                named = list(draw(st.permutations(subset + extras)))
                core["namedShadows"] = list(named)
                meta["named"] = named
                meta["compliant"] = all(
                    p in named for p in PORTAL_SHADOW_NAMES)
            sync["coreThing"] = core
        doc["synchronize"] = sync

    # Serialization variance: a re-serialization inside the helper would
    # normalize whitespace/key order and betray itself byte-wise on the
    # compliant no-op path.
    merge_string = json.dumps(
        doc, indent=draw(st.sampled_from([None, 1, 2])),
        sort_keys=draw(st.booleans()))
    config_update["merge"] = merge_string
    meta["merge_string"] = merge_string
    return entry, meta


@st.composite
def compliant_entry_merge_strings(draw):
    """A merge document already carrying ALL THREE portal shadow names (any
    order, optional caller extras and unknown keys, direction/classic each
    independently PRESENT OR ABSENT — the no-op gate keys on the names
    alone and must not default-fill a compliant document), serialized with
    arbitrary whitespace/key order."""
    extras = draw(st.lists(st.sampled_from(EXTRA_SHADOW_POOL),
                           unique=True, max_size=3))
    named = list(draw(st.permutations(PORTAL_SHADOW_NAMES + extras)))

    core = {"namedShadows": named}
    if draw(st.booleans()):
        core["classic"] = draw(st.booleans())
    if draw(st.booleans()):
        core["maxOutboundSyncUpdatesPerSecond"] = draw(st.integers(1, 200))

    sync = {"coreThing": core}
    if draw(st.booleans()):
        sync["direction"] = draw(st.sampled_from(DIRECTION_POOL))
    if draw(st.booleans()):
        sync["shadowDocumentsMap"] = {
            "custom": {"maxDepth": draw(st.integers(1, 8))}}

    doc = {"synchronize": sync}
    if draw(st.booleans()):
        doc["strategy"] = {"type": "realTime"}

    return json.dumps(doc, indent=draw(st.sampled_from([None, 1, 2])),
                      sort_keys=draw(st.booleans()))


# Example counts come from the conftest Hypothesis profiles (portal-fast
# locally, ci with HYPOTHESIS_PROFILE=ci) — never hardcoded here.
@settings(deadline=None)
@given(case=submitted_entry_cases())
def test_union_setdefault_and_field_preservation_across_entry_shapes(
        deployments, case):
    """**Property 3: Fix Checking — union-not-replace merge semantics.**
    # Validates: Requirements 2.1, 2.2, 3.2, 3.3

    _For any_ generated submitted-entry shape, the helper's output entry
    parses to a merge whose namedShadows contains all portal names (2.1)
    AND every parseable caller-supplied name — the original list object's
    order kept as a prefix, missing portal names appended in
    portal-constant order (union, never replace — 2.2/3.2); every
    caller-supplied field value (direction, classic, unknown keys at every
    surviving level, sibling configurationUpdate keys) is byte-preserved,
    with portal defaults filling ABSENT keys only (setdefault-only — 3.3);
    a compliant input comes back 'unchanged' (explicit version) with the
    merge string byte-identical."""
    entry, meta = case
    result, out, _calls = run_helper(deployments, entry)

    # Return-value contract (design Decision 1).
    if meta["compliant"] and meta["explicit_version"] is not None:
        assert result == "unchanged"
    else:
        assert result == "merged"

    merge = out["configurationUpdate"]["merge"]
    doc = json.loads(merge)
    named = doc["synchronize"]["coreThing"]["namedShadows"]

    # All portal names always present (2.1).
    assert all(p in named for p in PORTAL_SHADOW_NAMES), named

    if meta["named"] is not None:
        # Union semantics (2.2/3.2): the caller's list survives in order as
        # a prefix; the missing portal names are appended in
        # portal-constant order — nothing dropped, nothing reordered.
        assert named[:len(meta["named"])] == meta["named"]
        assert named[len(meta["named"]):] == [
            p for p in PORTAL_SHADOW_NAMES if p not in meta["named"]]
    else:
        # No parseable caller list existed (bare/corrupt/absent chain):
        # exactly the portal names, in portal-constant order.
        assert named == PORTAL_SHADOW_NAMES

    if meta["compliant"]:
        # Compliant input: the merge string is byte-identical whether the
        # version was explicit ('unchanged') or filled ('merged').
        assert merge == meta["merge_string"]

    # Caller field values byte-preserved wherever the node survived (3.3).
    if meta["parseable"]:
        for key, value in meta["doc_unknown"].items():
            assert doc[key] == value
        if meta["sync_is_dict"]:
            sync = doc["synchronize"]
            for key, value in meta["sync_unknown"].items():
                assert sync[key] == value
            if meta["direction"] is not _MISSING:
                assert sync["direction"] == meta["direction"]
            if meta["core_is_dict"]:
                core = sync["coreThing"]
                for key, value in meta["core_unknown"].items():
                    assert core[key] == value
                if meta["classic"] is not _MISSING:
                    assert core["classic"] == meta["classic"]

    # Setdefault-only defaults (3.3): portal defaults fill ABSENT keys on
    # every touched document; a compliant document is never default-filled
    # (byte-identity above already proves it).
    if not meta["compliant"]:
        if not (meta["sync_is_dict"] and meta["direction"] is not _MISSING):
            assert doc["synchronize"]["direction"] == "betweenDeviceAndCloud"
        if not (meta["sync_is_dict"] and meta["core_is_dict"]
                and meta["classic"] is not _MISSING):
            assert doc["synchronize"]["coreThing"]["classic"] is True

    # Sibling configurationUpdate keys survive untouched.
    for key, value in meta["cu_siblings"].items():
        assert out["configurationUpdate"][key] == value


@settings(deadline=None)
@given(case=submitted_entry_cases())
def test_explicit_version_respected_missing_version_resolved_exactly_once(
        deployments, case):
    """**Property 3: Fix Checking — lazy version resolution.**
    # Validates: Requirements 3.5

    _For any_ generated submitted-entry shape: an explicit componentVersion
    is submitted verbatim and the resolver is NEVER called; a missing one
    is resolved by calling the zero-arg resolver EXACTLY once."""
    entry, meta = case
    _result, out, calls = run_helper(deployments, entry)

    if meta["explicit_version"] is not None:
        assert out["componentVersion"] == meta["explicit_version"]
        assert len(calls) == 0
    else:
        assert out["componentVersion"] == RESOLVED_VERSION
        assert len(calls) == 1


@settings(deadline=None)
@given(merge_string=compliant_entry_merge_strings(),
       explicit_version=st.one_of(st.none(), st.sampled_from(VERSION_POOL)))
def test_compliant_input_unchanged_with_byte_identical_merge_string(
        deployments, merge_string, explicit_version):
    """**Property 3: Fix Checking — compliant no-op.**
    # Validates: Requirements 3.2, 3.3, 3.5

    _For any_ merge document already carrying all three portal shadow
    names (direction/classic each independently present or absent): with
    an explicit componentVersion the helper returns 'unchanged' and the
    ENTIRE entry is untouched — the merge string byte-identical, no
    default-filling, resolver never called; with a missing version the
    helper returns 'merged' having ONLY filled the version (exactly one
    resolver call), the merge string still byte-identical."""
    entry = {"configurationUpdate": {"merge": merge_string}}
    if explicit_version is not None:
        entry["componentVersion"] = explicit_version

    result, out, calls = run_helper(deployments, entry)

    # Byte-identity: never re-serialized (whitespace/key-order variance in
    # the generated string would betray any round-trip).
    assert out["configurationUpdate"]["merge"] == merge_string

    if explicit_version is not None:
        assert result == "unchanged"
        assert out == entry          # the whole entry untouched
        assert calls == []
    else:
        assert result == "merged"
        assert out["componentVersion"] == RESOLVED_VERSION
        assert len(calls) == 1
        # Nothing but the version was touched.
        assert out == {"componentVersion": RESOLVED_VERSION,
                       "configurationUpdate": {"merge": merge_string}}


# ==========================================================================
# ==========================================================================
# Property 4 (task 4.2) — Both Call Sites End-to-End
# **Validates: Requirements 2.1, 2.2, 2.3, 2.5, 3.6**
#
# APPENDED BY TASK 4.2 BELOW THIS BANNER — end-to-end PBT through the real
# endpoints (ShadowManagerEnv for create_deployment; the workflow-deploy
# harness with a seeded previous revision for create_workflow_deployment)
# plus the thor1-shape integration replay.
# ==========================================================================
# ==========================================================================

# Benign non-ShadowManager component names for the workflow carry-over:
# no dda.plugin.* / model-vllm-* / dda.workflow.* prefixes, so the
# plugin/vLLM/subscribe gates all no-op (3.6).
EXTRA_COMPONENT_POOL = ["com.example.telemetry", "com.example.vision",
                        "com.thirdparty.agent"]

# The exact thor1 revision-10 caller shape (bugfix.md Incident Record).
THOR1_BARE_SHADOW_MANAGER = {
    "component_name": SHADOW_MANAGER,
    "component_version": "2.3.15",
}


def submitted_named_shadows(entry):
    """synchronize.coreThing.namedShadows of a SUBMITTED entry's parsed
    merge; [] when the entry is bare."""
    merge = (entry.get("configurationUpdate") or {}).get("merge")
    if not merge:
        return []
    doc = json.loads(merge)
    return (doc.get("synchronize", {})
               .get("coreThing", {})
               .get("namedShadows", []))


def parseable_caller_names(entry):
    """The caller namedShadows list of an INPUT entry when every node on
    the merge path is well-formed (JSON object -> dict synchronize ->
    dict coreThing -> list namedShadows) — exactly the names the fix must
    union in, never drop. [] for bare/corrupt shapes."""
    config_update = entry.get("configurationUpdate")
    merge = (config_update.get("merge")
             if isinstance(config_update, dict) else None)
    if not merge:
        return []
    try:
        doc = json.loads(merge)
    except (TypeError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []
    sync = doc.get("synchronize")
    if not isinstance(sync, dict):
        return []
    core = sync.get("coreThing")
    if not isinstance(core, dict):
        return []
    named = core.get("namedShadows")
    return named if isinstance(named, list) else []


@st.composite
def merge_documents_for_state(draw, state):
    """A synchronize merge document for a named entry state: 'stale' (a
    strict subset of the portal names), 'extra' (caller extras beyond the
    portal set), 'compliant' (all three portal names, optional extras)."""
    if state == "compliant":
        base = list(PORTAL_SHADOW_NAMES)
        extras = draw(st.lists(st.sampled_from(EXTRA_SHADOW_POOL),
                               unique=True, max_size=2))
    else:
        base = draw(st.lists(st.sampled_from(PORTAL_SHADOW_NAMES),
                             unique=True, max_size=2))
        extras = draw(st.lists(
            st.sampled_from(EXTRA_SHADOW_POOL), unique=True,
            min_size=1 if state == "extra" else 0, max_size=2))
    named = list(draw(st.permutations(base + extras)))
    return {"synchronize": {
        "direction": draw(st.sampled_from(DIRECTION_POOL)),
        "coreThing": {"classic": draw(st.booleans()),
                      "namedShadows": named},
    }}


@st.composite
def revision_shaped_requests(draw):
    """A create_deployment request component list: LocalServer (so
    needs_nucleus fires) + a caller ShadowManager entry in any generated
    state — bare / stale / extra caller names / compliant. (Observed at
    task 1: the endpoint's request parse keeps name+version only, so
    every caller state collapses to a bare components-map entry — the
    property must hold regardless of what the caller sent.)"""
    sm = {"component_name": SHADOW_MANAGER,
          "component_version": draw(st.sampled_from(VERSION_POOL))}
    state = draw(st.sampled_from(["bare", "stale", "extra", "compliant"]))
    if state != "bare":
        sm["configurationUpdate"] = {"merge": json.dumps(
            draw(merge_documents_for_state(state)))}
    return [dict(LOCAL_SERVER_COMPONENT), sm]


@st.composite
def previous_shadow_manager_entries(draw):
    """A previous-revision ShadowManager entry in any state — bare /
    stale / extra caller names / compliant — always with an explicit
    componentVersion (get_deployment always returns one)."""
    entry = {"componentVersion": draw(st.sampled_from(VERSION_POOL))}
    state = draw(st.sampled_from(["bare", "stale", "extra", "compliant"]))
    if state != "bare":
        entry["configurationUpdate"] = {"merge": json.dumps(
            draw(merge_documents_for_state(state)))}
    return entry


@st.composite
def carried_over_component_maps(draw):
    """Non-ShadowManager previous-revision components the workflow path
    must carry over verbatim (3.6)."""
    components = {LOCAL_SERVER_ARM64JP6: {"componentVersion": "1.0.51"}}
    for name in draw(st.lists(st.sampled_from(EXTRA_COMPONENT_POOL),
                              unique=True, max_size=2)):
        components[name] = {
            "componentVersion": draw(st.sampled_from(VERSION_POOL))}
    return components


# --------------------------------------------------------------------------
# Per-example environment builders (fresh fakes + fresh Use_Case per
# Hypothesis example; only session/module-scoped fixtures enter @given —
# the task-2 preservation suite's established pattern)
# --------------------------------------------------------------------------

def build_sm_env(aws_stack, deployments, mp):
    return ShadowManagerEnv(WorkflowStoreEnv(aws_stack), deployments, mp)


def build_wf_env(aws_stack, deployments, mp):
    return WorkflowDeployEnv(WorkflowStoreEnv(aws_stack), deployments, mp)


def seed_workflow_revision(wf_env, previous_components,
                           thing_name="ryan-orin-nano"):
    """Seed a v2 non-subscribing workflow and a previous latest-for-target
    deployment carrying `previous_components` + the old workflow entry at
    1.0.0 (so the revision must REPLACE it at 2.0.0 — 3.6). Returns
    (workflow_id, deepcopied expected carry-over map)."""
    workflow_id = wf_env.seed_subscribing_workflow(version=2, topics=None)
    wf_env.register_device(thing_name)
    seeded = copy.deepcopy(previous_components)
    seeded[f"dda.workflow.{workflow_id}"] = {"componentVersion": "1.0.0"}
    # deepcopy again for seeding: FakeGreengrass stores the entry dicts by
    # reference and the handler mutates its (shallow) copy in place, so
    # the expectation must be an independent snapshot.
    wf_env.gg.seed_deployment(wf_env.thing_arn(thing_name),
                              copy.deepcopy(seeded))
    return workflow_id, seeded


@settings(deadline=None)
@given(components=revision_shaped_requests())
def test_create_deployment_submits_compliant_shadow_manager(
        aws_stack, deployments, components):
    """**Property 4: Fix Checking — create_deployment end-to-end.**
    # Validates: Requirements 2.1, 2.2, 2.5

    _For any_ revision-shaped request (LocalServer + a caller
    ShadowManager entry in any generated state — bare / stale / extra
    names / compliant), the SUBMITTED deployment document's ShadowManager
    entry satisfies portalShadowNames ⊆ namedShadows with a concrete
    componentVersion (the caller's explicit pin, verbatim)."""
    with pytest.MonkeyPatch.context() as mp:
        sm_env = build_sm_env(aws_stack, deployments, mp)
        status, payload = sm_env.deploy_components(
            copy.deepcopy(components), target_devices=["jetson-thor1"])

        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        entry = call["components"][SHADOW_MANAGER]

    # A concrete componentVersion — the caller's explicit pin, verbatim
    # (2.5).
    assert entry.get("componentVersion") == components[1]["component_version"]
    # The submitted merge covers every portal shadow name (2.1/2.2).
    assert set(PORTAL_SHADOW_NAMES) <= set(submitted_named_shadows(entry))


@settings(deadline=None)
@given(sm_entry=previous_shadow_manager_entries(),
       extras=carried_over_component_maps())
def test_workflow_revision_submits_compliant_and_carries_over(
        aws_stack, deployments, sm_entry, extras):
    """**Property 4: Fix Checking — create_workflow_deployment
    end-to-end.**
    # Validates: Requirements 2.1, 2.2, 2.3, 2.5, 3.6

    _For any_ previous-revision ShadowManager entry state (bare / stale /
    extra names / compliant), the workflow revision's SUBMITTED
    ShadowManager entry satisfies portalShadowNames ⊆ namedShadows (with
    parseable caller names unioned in, never dropped) at the copied
    concrete componentVersion — while every other copied component rides
    verbatim and the workflow entry is (re)placed at the resolved
    registered version (3.6)."""
    with pytest.MonkeyPatch.context() as mp:
        wf_env = build_wf_env(aws_stack, deployments, mp)
        previous = copy.deepcopy(extras)
        previous[SHADOW_MANAGER] = copy.deepcopy(sm_entry)
        workflow_id, seeded = seed_workflow_revision(wf_env, previous)

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        assert payload["is_revision"] is True
        [call] = wf_env.gg.create_deployment_calls
        submitted = call["components"]

    entry = submitted[SHADOW_MANAGER]
    # The copied explicit version survives verbatim (2.5).
    assert entry["componentVersion"] == sm_entry["componentVersion"]
    # Portal names always covered; parseable caller names unioned in,
    # never dropped (2.1/2.2/2.3).
    shadows = submitted_named_shadows(entry)
    assert set(PORTAL_SHADOW_NAMES) <= set(shadows)
    assert set(parseable_caller_names(sm_entry)) <= set(shadows)

    # Carry-over identity (3.6): every other copied component verbatim;
    # ONLY the workflow entry (re)placed, at the resolved registered
    # version 2.0.0.
    submitted_others = {name: comp for name, comp in submitted.items()
                        if name != SHADOW_MANAGER}
    expected = {name: comp for name, comp in seeded.items()
                if name != SHADOW_MANAGER}
    expected[f"dda.workflow.{workflow_id}"] = {"componentVersion": "2.0.0"}
    assert submitted_others == expected


# ==========================================================================
# Integration replay (task 4.2) — the exact thor1 revision-10 shape, the
# two fixed paths composed
# **Validates: Requirements 2.1, 2.3, 2.5, 3.6**
# ==========================================================================

class TestThor1RevisionReplayComposition:
    """**Property 4: Fix Checking — integration replay.**
    # Validates: Requirements 2.1, 2.3, 2.5, 3.6

    The exact thor1 revision-10 shape — LocalServer + the bare
    ShadowManager 2.3.15 entry — through create_deployment, then a
    workflow revision over the RESULT through create_workflow_deployment.
    Both SUBMITTED documents must be compliant, and the composed second
    submission must be an 'unchanged' pass-through: the first submission
    already carries the full portal merge, so the workflow leg's ensure
    step must leave the merge string byte-identical (never
    re-serialized)."""

    def test_both_paths_compose_and_second_pass_is_byte_identical(
            self, aws_stack, deployments):
        thing = "jetson-thor1"

        # --- Leg 1: create_deployment with the thor1 revision-10 shape.
        with pytest.MonkeyPatch.context() as mp:
            sm_env = build_sm_env(aws_stack, deployments, mp)
            status, payload = sm_env.deploy_components(
                [dict(LOCAL_SERVER_COMPONENT),
                 dict(THOR1_BARE_SHADOW_MANAGER)],
                target_devices=[thing])

            assert status == 201, payload
            [call] = sm_env.gg.create_deployment_calls
            first_components = copy.deepcopy(call["components"])

        first_entry = first_components[SHADOW_MANAGER]
        # First submission compliant: the bare caller entry gained the
        # full portal merge, the explicit 2.3.15 pin verbatim (2.1/2.5).
        assert first_entry["componentVersion"] == "2.3.15"
        first_merge = first_entry["configurationUpdate"]["merge"]
        assert set(PORTAL_SHADOW_NAMES) <= set(
            submitted_named_shadows(first_entry))

        # --- Leg 2: a workflow revision over the leg-1 RESULT (the
        # composed fleet state after the fixed create_deployment ships).
        with pytest.MonkeyPatch.context() as mp:
            wf_env = build_wf_env(aws_stack, deployments, mp)
            workflow_id, seeded = seed_workflow_revision(
                wf_env, first_components, thing_name=thing)

            status, payload = wf_env.deploy(
                workflow_id, target_devices=[thing])

            assert status == 201, payload
            assert payload["is_revision"] is True
            [call] = wf_env.gg.create_deployment_calls
            submitted = call["components"]

        entry = submitted[SHADOW_MANAGER]
        # Second submission compliant too (2.3)...
        assert set(PORTAL_SHADOW_NAMES) <= set(
            submitted_named_shadows(entry))
        # ...and an 'unchanged' pass-through: the copied version AND the
        # ENTIRE entry verbatim — the merge string byte-identical, never
        # re-serialized.
        assert entry["componentVersion"] == "2.3.15"
        assert entry["configurationUpdate"]["merge"] == first_merge
        assert entry == seeded[SHADOW_MANAGER]

        # Carry-over identity across the composition (3.6): every other
        # leg-1 component (LocalServer + the LogManager/Nucleus
        # auto-includes) rides verbatim, ONLY the workflow entry replaced
        # at the resolved registered version.
        submitted_others = {name: comp for name, comp in submitted.items()
                            if name != SHADOW_MANAGER}
        expected = {name: comp for name, comp in seeded.items()
                    if name != SHADOW_MANAGER}
        expected[f"dda.workflow.{workflow_id}"] = {
            "componentVersion": "2.0.0"}
        assert submitted_others == expected
