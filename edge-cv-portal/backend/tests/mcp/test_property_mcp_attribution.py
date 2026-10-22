"""Property test: marker attribution round trip and context binding.

**Feature: mcp-servers, Property 7: Marker attribution round trip and context binding**

Spec: .kiro/specs/mcp-servers/ (design.md "3. Shared-layer and audit-log
changes", D25, Correctness Property 7; tasks.md 2.2).
Validates: Requirements 4.1, 4.2, 4.3.

Two clauses:

* For any event with or without an `mcp` block, `attribution_from` returns
  exactly the five Attribution_Fields when the block is absent (equal to the
  pre-MCP computation, reproduced below as the oracle) and the five fields
  plus the five MCP_MARKER_FIELDS when it is present (`channel='mcp'`,
  `'unknown'` for missing values).
* For any sequence of `get_user_from_event(e_i)` calls interleaved with
  `_X_AMZN_TRACE_ID` changes, `_attribution_attributes(None, None)` is marked
  iff an event carrying an `mcp` block was remembered under the current
  trace id (a later marker-less event under the same trace id does not
  replace it, one under a new trace id does); otherwise it is the five
  `'unknown'` fields with `channel='api'`.
"""
import os
import sys
from contextlib import contextmanager

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ATTRIBUTION_FIELDS = ("username", "email", "source_ip", "user_agent",
                      "identity_source")
MCP_MARKER_FIELDS = ("channel", "mcp_tool", "mcp_server", "mcp_client",
                     "mcp_request_id")
BLOCK_KEYS = (("mcp_tool", "tool"), ("mcp_server", "server"),
              ("mcp_client", "client"), ("mcp_request_id", "request_id"))
TRACE = "_X_AMZN_TRACE_ID"
REGISTRY_STUB = "absent"


@pytest.fixture(scope="module")
def shared(aws_stack):
    return sys.modules["shared_utils"]


@contextmanager
def isolated(shared):
    """Fresh audit context, no trace id, and a registry-free identity source."""
    saved_context = shared._REQUEST_AUDIT_CONTEXT
    saved_lookup = shared._identity_source_for
    saved_trace = os.environ.pop(TRACE, None)
    shared._REQUEST_AUDIT_CONTEXT = None
    shared._identity_source_for = lambda *_a, **_k: REGISTRY_STUB
    try:
        yield
    finally:
        shared._REQUEST_AUDIT_CONTEXT = saved_context
        shared._identity_source_for = saved_lookup
        os.environ.pop(TRACE, None)
        if saved_trace is not None:
            os.environ[TRACE] = saved_trace


# ------------------------------------------------------------- strategies

# Values the request may carry: absent, empty, the literal 'unknown', or text.
values = st.one_of(st.none(), st.just(""), st.just("unknown"), st.just("  "),
                   st.text(min_size=1, max_size=12))


def _put(mapping, key, value):
    if value is not None:
        mapping[key] = value


@st.composite
def claims_dicts(draw):
    claims = {}
    for key in ("sub", "cognito:username", "username", "email",
                "custom:role", "client_id"):
        _put(claims, key, draw(values))
    return claims


@st.composite
def mcp_blocks(draw):
    block = {"channel": "mcp"}
    for key in ("tool", "server", "client", "request_id", "principal_email",
                "identity_source"):
        _put(block, key, draw(values))
    return block


@st.composite
def events(draw, block=st.one_of(st.none(), mcp_blocks())):
    request_context = {}
    shape = draw(st.sampled_from(("claims", "context", "none")))
    if shape == "claims":
        request_context["authorizer"] = {"claims": draw(claims_dicts())}
    elif shape == "context":
        authorizer = {}
        for key in ("userId", "username", "email", "role"):
            _put(authorizer, key, draw(values))
        request_context["authorizer"] = authorizer
    identity = {}
    _put(identity, "sourceIp", draw(values))
    _put(identity, "userAgent", draw(values))
    if identity or draw(st.booleans()):
        request_context["identity"] = identity
    event = {"httpMethod": "GET", "resource": "/devices",
             "requestContext": request_context}
    drawn_block = draw(block)
    if drawn_block is not None:
        event["mcp"] = drawn_block
    return event


# ------------------------------------------------------------- the oracle

def user_of(shared, event):
    """`get_user_from_event(event)` without touching the audit context the
    property observes."""
    saved = shared._REQUEST_AUDIT_CONTEXT
    try:
        return shared.get_user_from_event(event)
    finally:
        shared._REQUEST_AUDIT_CONTEXT = saved


def pre_mcp_attribution(shared, event, identity_source=None):
    """`attribution_from` as it computed the five fields before the marker.

    Reproduced from the shared layer as of the trunk this branch is based on
    (the email candidates without `principal_email`, the identity source
    from the registry lookup), so a change to the no-block path shows up
    here as a mismatch.
    """
    claims, authorizer, identity_block = shared._authorizer_identity_inputs(event)
    user = user_of(shared, event) if event is not None else {}
    user_id = user.get("user_id")
    username = shared._identifying_value(
        user.get("username"), claims.get("cognito:username"),
        claims.get("username"), authorizer.get("username"))
    email = shared._identifying_value(
        claims.get("email"), authorizer.get("email"),
        shared._claimed_email(user))
    if identity_source is None:
        identity_source = shared._identity_source_for(user_id, None)
    return {
        "username": username,
        "email": email,
        "source_ip": shared._identifying_value(identity_block.get("sourceIp")),
        "user_agent": shared._identifying_value(identity_block.get("userAgent")),
        "identity_source": shared._identifying_value(identity_source),
    }


def expected_marked(shared, event):
    """The ten attributes an event with an `mcp` block must yield."""
    block = event["mcp"]
    claims, authorizer, _ = shared._authorizer_identity_inputs(event)
    user = user_of(shared, event)
    base = pre_mcp_attribution(
        shared, event, identity_source=block.get("identity_source") or None)
    base["email"] = shared._identifying_value(
        claims.get("email"), block.get("principal_email"),
        authorizer.get("email"), shared._claimed_email(user))
    base["channel"] = "mcp"
    for field, key in BLOCK_KEYS:
        base[field] = shared._identifying_value(block.get(key))
    return base


# ------------------------------------------------------- clause 1: round trip

@settings(max_examples=100, deadline=None)
@given(event=events(), identity_source=st.one_of(
    st.none(), st.sampled_from(("registry", "absent", "unknown"))))
def test_attribution_round_trip(shared, event, identity_source):
    with isolated(shared):
        result = shared.attribution_from(event, identity_source=identity_source)
        if "mcp" not in event:
            assert tuple(result) == ATTRIBUTION_FIELDS
            assert result == pre_mcp_attribution(shared, event, identity_source)
        else:
            assert set(result) == set(ATTRIBUTION_FIELDS) | set(MCP_MARKER_FIELDS)
            expected = expected_marked(shared, event)
            if identity_source is not None:
                expected["identity_source"] = identity_source
            assert result == expected
            assert result["channel"] == "mcp"
        for value in result.values():
            assert isinstance(value, str) and value.strip()


# --------------------------------------------- clause 2: context binding

trace_ids = st.one_of(st.none(), st.sampled_from(("Root=1-a", "Root=1-b",
                                                  "Root=1-c")))
steps = st.lists(
    st.one_of(st.tuples(st.just("trace"), trace_ids),
              st.tuples(st.just("call"), events())),
    min_size=1, max_size=12)


def _set_trace(value):
    if value is None:
        os.environ.pop(TRACE, None)
    else:
        os.environ[TRACE] = value


@settings(max_examples=100, deadline=None)
@given(sequence=steps)
def test_context_binding(shared, sequence):
    with isolated(shared):
        # Model of D25: (remembered event, trace id it was remembered under).
        remembered, remembered_trace = None, None
        current = None
        for kind, value in sequence:
            if kind == "trace":
                current = value
                _set_trace(value)
            else:
                keep = (remembered is not None and "mcp" in remembered
                        and "mcp" not in value and remembered_trace == current)
                if not keep:
                    remembered, remembered_trace = value, current
                shared.get_user_from_event(value)

            attributes = shared._attribution_attributes(None, None)
            marked = (remembered is not None and "mcp" in remembered
                      and remembered_trace == current)
            if marked:
                expected = expected_marked(shared, remembered)
                assert attributes == expected
            else:
                assert attributes == dict(
                    {field: "unknown" for field in ATTRIBUTION_FIELDS},
                    channel="api")
