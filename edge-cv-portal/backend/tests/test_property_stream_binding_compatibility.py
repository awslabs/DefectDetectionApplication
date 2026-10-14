"""
Property-based test for deploy-time Stream_Camera binding validation —
``validate_camera_bindings`` (functions/deployments.py), task 7.3.

**Feature: rtsp-rtmp-stream-cameras, Property 10: Stream binding compatibility and override validation**

*For any* version item, registry, and binding set, for a stream node:

- ``CAMERA_TYPE_INCOMPATIBLE`` SHALL be reported exactly when the bound
  entry's type differs from the node's protocol type.
- ``CAMERA_OVERRIDE_INVALID`` SHALL be reported exactly when an override
  ``url`` violates the descriptor constraints or ``check_stream_url``.

**Validates: Requirements 9.3, 9.4**

The layer under test is the pure pre-submit validator: it is called
directly, with no AWS. ``deployments`` is imported through the shared
moto-backed session fixture only so its module-level boto3 clients bind
to the mock (the re-import pattern of
test_property_aravis_type_compatibility.py).

Reference models, restated here from the requirements rather than
imported from the implementation, so that a change on either side is a
test failure and not a silent agreement:

- **Protocol types** (clause 1, Requirement 9.3): an
  ``rtsp_camera_source`` node binds to an ``RTSP`` Camera_Source and an
  ``rtmp_stream_source`` node to an ``RTMP`` one — nothing else can
  decode the node's transport, so every other registry type, including
  the crossed stream protocol and a differently-cased spelling of the
  right one, is incompatible.
- **Stream_URL rules** (clause 2, Requirement 9.4 via 2.1/2.2): a
  Stream_URL parses, has a scheme allowed for *that* node type, has a
  host, carries no user information and no Secret_Query_Parameter. The
  expected verdict of every generated override value is decided by
  construction — values are drawn from a clean pool or from a pool of
  single-defect values, each tagged with the defect the requirement
  names — never by re-running the checker the test is pinning.
- **Descriptor constraints** (clause 2, the other half): the node type's
  declared ``url`` constraints, of which the length bound is the one the
  Stream_URL rules do not also cover; the over-long case therefore pins
  that a descriptor-only violation is reported on its own, and the
  trailing-newline case pins the converse — the catalog ``regex``
  constraint accepts it (Python's ``$`` matches before a trailing
  newline, and the validator applies the pattern with ``re.search``)
  while ``check_stream_url`` rejects it, so only the added rule catches
  it.

What is deliberately *not* generated here:

- **A failed Stream_Health state.** Clause 1's "exactly" is about the
  error set, so entries are generated healthy (synced, present, fresh,
  and reporting a non-failed stream state) and the properties assert
  that no warning at all is raised. The degraded-source warning for
  ``state: failed`` is Requirement 9.5's, pinned by
  test_camera_binding_degraded_warning_properties.py and task 7.4.
- **The unified spelling.** Stream nodes appear in their direct
  spelling (``rtsp_camera_source`` / ``rtmp_stream_source``), which is
  what the packager records in ``camera_input_nodes``; a unified
  ``Input Source`` record would reach neither the compatibility map nor
  the Stream_URL rule, the pre-existing ``aravis_camera`` parity and out
  of scope for this property.

Controls that make the two clauses discriminating rather than merely
true: an ``icam_source`` node is mixed into both properties (its
compatible set restated from csi-icam-input-nodes Requirement 6.1), and
its override values are checked to carry no Stream_URL problem code — a
``url`` key on a non-stream node is an undeclared parameter, not a
Stream_URL. The same holds for the differently-cased key ``URL`` on a
stream node.
"""
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments inside the moto mock so its module-level boto3
    clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


# ---------------------------------------------------------------------------
# Reference models (independent of the implementation)
# ---------------------------------------------------------------------------

#: Requirement 9.3: the Camera_Source type a Stream_Camera_Source_Node
#: binds to, one protocol each.
_STREAM_PROTOCOL_TYPE = {
    "rtsp_camera_source": "RTSP",
    "rtmp_stream_source": "RTMP",
}

#: Requirement 2.1: the schemes each stream node type accepts.
_STREAM_SCHEMES = {
    "rtsp_camera_source": ("rtsp", "rtsps"),
    "rtmp_stream_source": ("rtmp", "rtmps"),
}

#: The non-stream neighbour carried through both properties as a control.
#: Its compatible set is restated from csi-icam-input-nodes Requirement
#: 6.1 (a V4L2 smart camera captured directly).
_NEIGHBOUR_NODE_TYPE = "icam_source"
_NEIGHBOUR_COMPATIBLE = frozenset({"ICam", "V4L2Discovered", "Camera"})

_STREAM_NODE_TYPES = sorted(_STREAM_PROTOCOL_TYPE)
_NODE_TYPES = _STREAM_NODE_TYPES + [_NEIGHBOUR_NODE_TYPE]


def _type_compatible(node_type, source_type):
    """The Requirement 9.3 expectation for a stream node (and the
    pre-existing Requirement 9.4 expectation for the neighbour)."""
    if node_type in _STREAM_PROTOCOL_TYPE:
        return source_type == _STREAM_PROTOCOL_TYPE[node_type]
    return source_type in _NEIGHBOUR_COMPATIBLE


#: Registry ``type`` values a target may report: both stream protocols,
#: every camera-backed type, the categorically incompatible Folder, a
#: foreign network type, a lowercase spelling of a stream protocol (a
#: different string, hence a different type), an unknown one, and a
#: missing type (a malformed entry — the validator must stay total).
_SOURCE_TYPES = ["RTSP", "RTMP", "Camera", "ICam", "NvidiaCSI",
                 "V4L2Discovered", "AravisDiscovered", "StaticImage",
                 "Folder", "HTTPPull", "rtsp", "SomethingElse", None]

#: Stream_Health states that are *not* degraded (Requirement 9.5 names
#: only ``failed``), plus "no capability section reported at all".
_HEALTHY_STREAM_STATES = ["streaming", "reconnecting", "idle", None]

#: The Stream_URL problem codes (design: check_stream_url's ``code``).
_STREAM_URL_CODES = frozenset({"invalid_url", "scheme_not_allowed",
                               "no_host", "user_info",
                               "secret_query_parameter"})

_NODE_IDS = ["n1", "n2", "n3"]
_DEVICES = ["line-a", "line-b", "line-c"]

#: Hosts a Stream_URL may carry: name, FQDN, IPv4, bracketed IPv6.
_HOSTS = ["cam.local", "media.plant.example.com", "192.168.1.64",
          "10.0.4.21", "[2001:db8::1]"]


def _registry_entry(source_type, stream_state):
    """A healthy registry entry: synced, present, fresh, and (for a
    stream type) reporting a non-degraded Stream_Health state."""
    entry = {
        "type": source_type,
        "params": {"url": "rtsp://cam.local/live/1"},
        "sync_status": "synced",
        "absent": False,
        "stale": False,
    }
    if stream_state is not None:
        entry["capabilities"] = {"stream": {"state": stream_state}}
    return entry


# ---------------------------------------------------------------------------
# Clause 1: type compatibility
# ---------------------------------------------------------------------------


@st.composite
def _type_cases(draw):
    """A version item whose Camera_Input_Nodes mix both stream types and
    the non-stream neighbour, per-device registries whose entries carry
    arbitrary source types, and a binding set referencing them. Every
    referenced source exists and is healthy, so type compatibility is
    the only possible signal."""
    node_ids = draw(st.lists(st.sampled_from(_NODE_IDS),
                             unique=True, min_size=1, max_size=3))
    node_types = {node_id: draw(st.sampled_from(_NODE_TYPES))
                  for node_id in node_ids}
    # At least one stream node, so every example exercises the new rule.
    node_types[node_ids[0]] = draw(st.sampled_from(_STREAM_NODE_TYPES))
    targets = draw(st.lists(st.sampled_from(_DEVICES),
                            unique=True, min_size=1, max_size=3))

    registry_snapshot = {}
    bindings = {}
    # (device, node_id) -> (camera_source_id, source_type)
    source_refs = {}

    for device in targets:
        cameras = {}
        device_bindings = {}
        for node_id in node_ids:
            source_type = draw(st.sampled_from(_SOURCE_TYPES))
            stream_state = draw(st.sampled_from(_HEALTHY_STREAM_STATES))
            camera_source_id = f"src-{device}-{node_id}"
            cameras[camera_source_id] = _registry_entry(source_type,
                                                        stream_state)
            device_bindings[node_id] = {"cameraSourceId": camera_source_id}
            source_refs[(device, node_id)] = (camera_source_id, source_type)
        registry_snapshot[device] = {"never_synced": False,
                                     "cameras": cameras}
        bindings[device] = device_bindings

    version = {
        "has_binding_points": True,
        "camera_input_nodes": [
            {"node_id": node_id, "node_type": node_types[node_id]}
            for node_id in node_ids
        ],
    }
    return version, targets, registry_snapshot, bindings, node_types, \
        source_refs


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_type_cases())
def test_stream_type_incompatibility_is_reported_exactly(deployments, case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 10: Stream binding
    compatibility and override validation** (clause 1)

    **Validates: Requirements 9.3, 9.4**

    A CAMERA_TYPE_INCOMPATIBLE error is produced exactly for the stream
    bindings whose Camera_Source type differs from the node's protocol
    type — ``RTSP`` for ``rtsp_camera_source`` and ``RTMP`` for
    ``rtmp_stream_source``, so the crossed protocol is rejected like any
    foreign type — and the neighbour node type's pre-existing set is
    unchanged. Nothing else is reported for these healthy, fully bound
    cases.
    """
    (version, targets, registry_snapshot, bindings, node_types,
     source_refs) = case

    errors, warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings, [])

    expected_mismatches = {
        (device, node_id, camera_source_id, source_type)
        for (device, node_id), (camera_source_id, source_type)
        in source_refs.items()
        if not _type_compatible(node_types[node_id], source_type)
    }

    type_errors = [
        e for e in errors
        if e["code"] == deployments.CAMERA_ERROR_TYPE_INCOMPATIBLE]

    # Exactly one type error per incompatible binding; every compatible
    # binding passes the type check.
    assert {(e["device"], e["nodeId"], e["cameraSourceId"], e["sourceType"])
            for e in type_errors} == expected_mismatches
    assert len(type_errors) == len(expected_mismatches)

    # Each error identifies both sides of the mismatch.
    for error in type_errors:
        assert error["nodeType"] == node_types[error["nodeId"]]
        assert str(error["sourceType"]) in error["message"]
        assert error["nodeType"] in error["message"]

    # Healthy, present, fully bound sources: type compatibility is the
    # only signal — no other error, and no warning (a non-failed stream
    # state is not degraded).
    assert len(errors) == len(type_errors)
    assert warnings == []


def test_matching_protocol_binds_and_crossed_protocol_does_not(deployments):
    """Directed companion to clause 1: the two accepting pairs and the
    two crossed ones, so the property above cannot pass by rejecting
    every stream binding."""
    for node_type, good, bad in (
            ("rtsp_camera_source", "RTSP", "RTMP"),
            ("rtmp_stream_source", "RTMP", "RTSP")):
        version = {"has_binding_points": True,
                   "camera_input_nodes": [{"node_id": "n1",
                                           "node_type": node_type}]}
        for source_type, expect_error in ((good, False), (bad, True)):
            snapshot = {"line-a": {
                "never_synced": False,
                "cameras": {"s1": _registry_entry(source_type, "streaming")}}}
            errors, warnings = deployments.validate_camera_bindings(
                version, ["line-a"], snapshot,
                {"line-a": {"n1": {"cameraSourceId": "s1"}}}, [])
            assert warnings == []
            if expect_error:
                assert [e["code"] for e in errors] == [
                    deployments.CAMERA_ERROR_TYPE_INCOMPATIBLE]
            else:
                assert errors == []


# ---------------------------------------------------------------------------
# Clause 2: override validation
# ---------------------------------------------------------------------------


@st.composite
def _valid_stream_url(draw, node_type):
    """A Stream_URL that satisfies every rule for ``node_type``: an
    allowed lowercase scheme, a host, an optional numeric port, an
    optional path, and a query with no Secret_Query_Parameter. No user
    information, no whitespace, no fragment."""
    scheme = draw(st.sampled_from(_STREAM_SCHEMES[node_type]))
    host = draw(st.sampled_from(_HOSTS))
    port = draw(st.sampled_from(["", ":554", ":322", ":1935", ":443",
                                 ":8554", ":10000"]))
    path = draw(st.sampled_from(["", "/", "/Streaming/Channels/101",
                                 "/live/line1", "/cam/a-b_c.1"]))
    query = draw(st.sampled_from(["", "?channel=1",
                                  "?subtype=0&profile=main",
                                  "?transport=tcp"]))
    return f"{scheme}://{host}{port}{path}{query}"


#: A secret literal embedded in the two credential-carrying defects; no
#: message may echo it (design: problem messages name the offending part,
#: never its value).
_SECRET = "hunter2s3cret"


def _invalid_url_items(node_type):
    """Single-defect ``url`` values for ``node_type``, each tagged with
    the Stream_URL problem the requirement names (or None when the
    Stream_URL rules accept the value and only a declared descriptor
    constraint rejects it), the descriptor violation code to pin when it
    is the discriminating one, and any secret literal the value carries.

    Every entry is invalid *by construction*, from the wording of
    Requirements 2.1, 2.2 and 1.3 — the checker is never consulted.
    """
    own = _STREAM_SCHEMES[node_type][0]
    other = _STREAM_SCHEMES[
        "rtmp_stream_source" if node_type == "rtsp_camera_source"
        else "rtsp_camera_source"][0]
    return [
        # Scheme outside the node type's set: the crossed stream
        # protocol (which the shared catalog regex accepts, so only the
        # per-type scheme rule rejects it) and a foreign scheme.
        (f"{other}://cam.local/live/1", "scheme_not_allowed", None, ()),
        ("http://cam.local/live/1", "scheme_not_allowed", None, ()),
        # Embedded user information (Requirement 2.2).
        (f"{own}://viewer:{_SECRET}@cam.local/live/1", "user_info", None,
         (_SECRET,)),
        # Secret_Query_Parameter, matched case-insensitively.
        (f"{own}://cam.local/live/1?password={_SECRET}",
         "secret_query_parameter", None, (_SECRET,)),
        (f"{own}://cam.local/live/1?TOKEN={_SECRET}",
         "secret_query_parameter", None, (_SECRET,)),
        # No host.
        (f"{own}:///live/1", "no_host", None, ()),
        # Unparseable, empty, non-lowercase scheme, whitespace, fragment.
        ("not a url", "invalid_url", None, ()),
        ("", "invalid_url", None, ()),
        (f"{own.upper()}://cam.local/live/1", "invalid_url", None, ()),
        (f"{own}://cam.local/live 1", "invalid_url", None, ()),
        (f"{own}://cam.local/live/1#frag", "invalid_url", None, ()),
        # Trailing newline: the catalog regex constraint accepts this
        # (anchored pattern applied with re.search, and Python's '$'
        # matches before a trailing newline), so the Stream_URL rule is
        # the only thing that rejects it.
        (f"{own}://cam.local/live/1\n", "invalid_url", None, ()),
        # Not a string at all.
        (5, "invalid_url", None, ()),
        (True, "invalid_url", None, ()),
        (1.5, "invalid_url", None, ()),
        (None, "invalid_url", None, ()),
        ((f"{own}://cam.local/live/1",), "invalid_url", None, ()),
        # Over the declared length bound, and otherwise a valid
        # Stream_URL: the descriptor constraint is the only rejecter.
        (f"{own}://cam.local/" + "a" * 2100, None, "PARAM_MAX_LENGTH", ()),
    ]


#: Values satisfying other declared stream parameters (they must add no
#: error), and, per node type, parameter names that node type does not
#: declare (they must be rejected whatever the value). ``URL`` is in
#: every list because the Stream_URL rule keys on the exact name ``url``;
#: ``device`` is undeclared on the stream types and ``processing_mode``
#: on the neighbour, which declares ``device`` alone.
_OTHER_VALID_PARAMETERS = {
    "processing_mode": st.sampled_from(["continuous", "on_trigger"]),
    "max_frame_age_ms": st.integers(min_value=100, max_value=60000),
    "frames_per_second": st.floats(min_value=0.05, max_value=10.0,
                                   allow_nan=False, allow_infinity=False),
}
_UNDECLARED_NAMES = {
    "rtsp_camera_source": ["URL", "device", "bogus"],
    "rtmp_stream_source": ["URL", "device", "bogus"],
    _NEIGHBOUR_NODE_TYPE: ["URL", "processing_mode", "bogus"],
}


@st.composite
def _override_cases(draw):
    """Per (device, node) a manual-override binding on a stream node or
    on the neighbour node type, carrying a ``url`` drawn either from the
    clean pool or from the single-defect pool, optionally a valid other
    declared parameter, and optionally an undeclared name. Returns the
    per-value expected verdicts alongside the inputs."""
    node_ids = draw(st.lists(st.sampled_from(_NODE_IDS),
                             unique=True, min_size=1, max_size=3))
    node_types = {node_id: draw(st.sampled_from(_NODE_TYPES))
                  for node_id in node_ids}
    node_types[node_ids[0]] = draw(st.sampled_from(_STREAM_NODE_TYPES))
    targets = draw(st.lists(st.sampled_from(_DEVICES),
                            unique=True, min_size=1, max_size=2))

    bindings = {}
    # (device, node_id, parameter) -> verdict dict
    verdicts = {}
    secrets = set()

    for device in targets:
        device_bindings = {}
        for node_id in node_ids:
            node_type = node_types[node_id]
            stream = node_type in _STREAM_SCHEMES
            override = {}

            if draw(st.booleans()):
                # A clean Stream_URL for the node's own protocol. On a
                # stream node it is valid; on the neighbour, 'url' is an
                # undeclared parameter whatever its value.
                url_type = node_type if stream else \
                    draw(st.sampled_from(_STREAM_NODE_TYPES))
                override["url"] = draw(_valid_stream_url(url_type))
                verdicts[(device, node_id, "url")] = {
                    "valid": stream,
                    "stream_code": None,
                    "descriptor_code": None,
                    "no_stream_code": not stream,
                }
            else:
                url_type = node_type if stream else \
                    draw(st.sampled_from(_STREAM_NODE_TYPES))
                value, stream_code, descriptor_code, value_secrets = draw(
                    st.sampled_from(_invalid_url_items(url_type)))
                override["url"] = value
                secrets.update(value_secrets)
                verdicts[(device, node_id, "url")] = {
                    "valid": False,
                    # On the neighbour the value is rejected as an
                    # undeclared parameter, never as a Stream_URL.
                    "stream_code": stream_code if stream else None,
                    "descriptor_code": descriptor_code if stream else None,
                    "no_stream_code": not stream,
                }

            if stream and draw(st.booleans()):
                name = draw(st.sampled_from(sorted(_OTHER_VALID_PARAMETERS)))
                override[name] = draw(_OTHER_VALID_PARAMETERS[name])
                verdicts[(device, node_id, name)] = {
                    "valid": True, "stream_code": None,
                    "descriptor_code": None, "no_stream_code": True,
                }

            if draw(st.booleans()):
                name = draw(st.sampled_from(_UNDECLARED_NAMES[node_type]))
                override[name] = draw(st.one_of(
                    st.text(max_size=8), st.integers(-5, 5)))
                verdicts[(device, node_id, name)] = {
                    "valid": False, "stream_code": None,
                    "descriptor_code": None, "no_stream_code": True,
                }

            device_bindings[node_id] = {"override": override}
        bindings[device] = device_bindings

    version = {
        "has_binding_points": True,
        "camera_input_nodes": [
            {"node_id": node_id, "node_type": node_types[node_id]}
            for node_id in node_ids
        ],
    }
    registry_snapshot = {device: {"never_synced": False, "cameras": {}}
                         for device in targets}
    return version, targets, registry_snapshot, bindings, node_types, \
        verdicts, sorted(secrets)


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_override_cases())
def test_stream_override_url_rejected_exactly_when_invalid(deployments, case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 10: Stream binding
    compatibility and override validation** (clause 2)

    **Validates: Requirements 9.3, 9.4**

    A stream node's override is accepted exactly when its ``url``
    satisfies both the declared descriptor constraints and the
    Stream_URL rules for that node type: a wrong scheme for the type,
    embedded user information, a Secret_Query_Parameter, a missing host,
    an unparseable value and a non-string all produce
    CAMERA_OVERRIDE_INVALID carrying the Stream_URL problem code, an
    over-long but otherwise valid URL produces the declared length
    violation on its own, other declared parameters with satisfying
    values produce nothing, and an undeclared name — including the
    differently-cased ``URL`` and any ``url`` on a non-stream node type
    — is rejected without any Stream_URL problem code. No message
    echoes a secret value.
    """
    (version, targets, registry_snapshot, bindings, node_types, verdicts,
     secrets) = case

    errors, warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings, [])

    override_errors = [
        e for e in errors
        if e["code"] == deployments.CAMERA_ERROR_OVERRIDE_INVALID]

    # Override validation is the only signal, and an overridden node on
    # a synced target raises no warning.
    assert len(errors) == len(override_errors)
    assert warnings == []

    expected_rejected = {key for key, verdict in verdicts.items()
                         if not verdict["valid"]}
    reported = {(e["device"], e["nodeId"], e["parameter"])
                for e in override_errors}
    assert reported == expected_rejected

    for key, verdict in verdicts.items():
        codes = {e.get("violation") for e in override_errors
                 if (e["device"], e["nodeId"], e["parameter"]) == key}
        if verdict["valid"]:
            assert codes == set()
            continue
        if verdict["stream_code"] is not None:
            assert verdict["stream_code"] in codes
        if verdict["descriptor_code"] is not None:
            assert verdict["descriptor_code"] in codes
            assert not (codes & _STREAM_URL_CODES)
        if verdict["no_stream_code"]:
            assert not (codes & _STREAM_URL_CODES)

    for error in override_errors:
        # Every error identifies the offending parameter, the node and
        # the device. A Stream_URL problem names the field in operator
        # terms ("Stream URL ...") rather than repeating the parameter
        # name, which the structured ``parameter`` field carries.
        assert error["parameter"]
        if error.get("violation") in _STREAM_URL_CODES:
            assert "Stream URL" in error["message"]
        else:
            assert error["parameter"] in error["message"]
        assert error["nodeId"] in error["message"]
        assert error["device"] in error["message"]
        for secret in secrets:
            assert secret not in error["message"]
