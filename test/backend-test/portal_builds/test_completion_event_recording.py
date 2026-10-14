# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Property test for the pure event-application function
``apply_phase_event`` of
``edge-cv-portal/backend/functions/build_events.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates (task 8.4)

**Validates: Requirements 5.3, 5.4**

For any agent completion event (phase=succeeded, or phase=failed with a
publishing or build error kind) applied to any Build_Job status:

- a succeeded event applied to a job occupying build compute drives the
  job to ``succeeded`` and records the agent-reported result metadata
  (component version, image references) VERBATIM on the job, with the
  ``build_published`` Audit_Log action and the terminal end time
  (Req 5.3);
- a failed event with ``error_kind=publishing`` marks the job failed
  with the PUBLISHING_FAILED error kind — DISTINCT from the
  BUILD_FAILED kind of a build-stage failure — and preserves the
  published/unpublished artifact lists exactly as the agent reported
  them (``publish_partial``), in both the job updates and the audit
  details (Req 5.4);
- every transition in the returned chain is an edge of the
  build_domain state machine starting at the current status, and a
  terminal current status absorbs every completion event as a no-op
  (duplicate/stale delivery), so completion recording can never
  resurrect a finished Build_Job.

``apply_phase_event`` is pure (event payload -> field updates), so this
test needs no AWS clients; the shared_utils Lambda-layer import of
build_events is satisfied with a stub module (the standalone-suite
pattern used across test/backend-test/portal_builds/).
"""
import os
import sys
import types

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap + fake shared_utils BEFORE build_events is imported
# (build_events imports log_audit_event from the Lambda layer at import
# time; other suites in the session may have installed their own copies).
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

for _module in ("build_events", "build_domain", "shared_utils"):
    sys.modules.pop(_module, None)

_shared_utils = types.ModuleType("shared_utils")
_shared_utils.log_audit_event = lambda *args, **kwargs: None
sys.modules["shared_utils"] = _shared_utils

import build_domain  # noqa: E402
import build_events  # noqa: E402

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_STATUSES = sorted(build_domain.ALL_STATUSES)
_EVENTS = sorted(build_domain.ALL_EVENTS)

#: Statuses in which the job occupies build compute, so an agent
#: completion event is live (a queued job has no agent yet; the
#: chain-healing in apply_phase_event covers lost intermediates).
_ACTIVE_STATUSES = frozenset({
    build_domain.STATUS_PROVISIONING,
    build_domain.STATUS_BUILDING,
    build_domain.STATUS_PUBLISHING,
})

#: Agent-reported strings (artifact ids, image refs, versions...).
_AGENT_TEXT = st.text(min_size=1, max_size=30)
_ARTIFACT_LISTS = st.lists(_AGENT_TEXT, max_size=4)

#: Result metadata as the agent reports it on success: component
#: version identifier + pushed image references plus arbitrary extra
#: keys — recording must be VERBATIM whatever the exact shape (Req 5.3).
_JSON_LEAF = st.one_of(_AGENT_TEXT, st.integers(), st.booleans())
_RESULTS = st.fixed_dictionaries(
    {
        "component_name": _AGENT_TEXT,
        "published_version": st.from_regex(
            r"[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3}", fullmatch=True),
        "pushed_image_refs": _ARTIFACT_LISTS,
    },
    optional={
        "extra": st.dictionaries(_AGENT_TEXT,
                                 st.one_of(_JSON_LEAF,
                                           st.lists(_JSON_LEAF, max_size=3)),
                                 max_size=3),
    },
)

_KIND_SUCCEEDED = "succeeded"
_KIND_PUBLISHING_FAILURE = "publishing_failure"
_KIND_BUILD_FAILURE = "build_failure"


@st.composite
def _completion_events(draw):
    """A random agent completion event: (kind, EventBridge detail)."""
    kind = draw(st.sampled_from(
        [_KIND_SUCCEEDED, _KIND_PUBLISHING_FAILURE, _KIND_BUILD_FAILURE]))
    if kind == _KIND_SUCCEEDED:
        return kind, {
            "phase": build_events.PHASE_SUCCEEDED,
            "result": draw(_RESULTS),
        }
    if kind == _KIND_PUBLISHING_FAILURE:
        detail = {
            "phase": build_events.PHASE_FAILED,
            "error_kind": build_events.ERROR_KIND_PUBLISHING,
            "published_artifacts": draw(_ARTIFACT_LISTS),
            "unpublished_artifacts": draw(_ARTIFACT_LISTS),
        }
    else:
        # Build-stage failure: the agent reports error_kind=building or
        # omits the kind entirely (both mean "not publishing").
        detail = {"phase": build_events.PHASE_FAILED}
        error_kind = draw(st.sampled_from(["building", None]))
        if error_kind is not None:
            detail["error_kind"] = error_kind
    message = draw(st.one_of(st.none(), st.text(max_size=40)))
    if message is not None:
        detail["error_message"] = message
    return kind, detail


def _assert_chain_follows_state_machine(current_status, steps):
    """Every returned transition is a defined build_domain edge, and the
    chain is contiguous from the delivery-time status."""
    assert steps[0][0] == current_status
    for (_, reached), (expected, _) in zip(steps, steps[1:]):
        assert reached == expected, "transition chain is not contiguous"
    for expected, next_ in steps:
        assert next_ != expected
        assert any(
            build_domain.next_status(expected, event) == next_
            for event in _EVENTS
        ), f"({expected} -> {next_}) is not a state-machine edge"


# Feature: portal-build-fleet-and-workflow-gates, Property 10: Result and failure recording on completion events
# Validates: Requirements 5.3, 5.4
@settings(max_examples=200, deadline=None)
@given(current_status=st.sampled_from(_STATUSES),
       completion=_completion_events(),
       now=st.integers(min_value=1, max_value=2 ** 41))
def test_result_and_failure_recording_on_completion_events(
        current_status, completion, now):
    """For any agent completion event: a succeeded event's component
    version and image references are recorded verbatim on the Build_Job
    which reaches ``succeeded`` (Req 5.3); a publishing-stage failure
    marks the job failed with an error kind distinct from a build
    failure and preserves the published/unpublished artifact lists
    exactly as reported (Req 5.4); transitions follow the state machine
    with terminal absorption."""
    kind, detail = completion

    application = build_events.apply_phase_event(
        current_status, dict(detail), now)

    # -- terminal absorption: a finished job is never touched again.
    if build_domain.is_terminal(current_status):
        assert application.is_noop
        assert application.updates == {}
        assert application.audit_action is None
        return

    # -- every transition follows the build_domain state machine.
    if application.steps:
        _assert_chain_follows_state_machine(current_status,
                                            application.steps)

    active = current_status in _ACTIVE_STATUSES
    expected_message = detail.get("error_message") or "The build failed."

    if kind == _KIND_SUCCEEDED:
        if not active:
            # queued: the agent cannot have finished a job that was
            # never dispatched — stale delivery, no-op.
            assert application.is_noop
            return
        # Req 5.3: the job reaches succeeded and the agent-reported
        # result metadata (component version, image references, and
        # anything else it reported) is recorded VERBATIM.
        assert application.final_status == build_domain.STATUS_SUCCEEDED
        assert application.updates["result"] == detail["result"]
        assert application.updates["ended_at"] == now
        assert application.audit_action == build_events.AUDIT_BUILD_PUBLISHED
        assert "error" not in application.updates
        assert "publish_partial" not in application.updates

    elif kind == _KIND_PUBLISHING_FAILURE:
        if not active:
            assert application.is_noop
            return
        # Req 5.4: failed with the PUBLISHING_FAILED kind — distinct
        # from a build failure — and the per-artifact lists exactly as
        # the agent reported them.
        assert application.final_status == build_domain.STATUS_FAILED
        error = application.updates["error"]
        assert error["code"] == build_events.ERROR_PUBLISHING_FAILED
        assert error["code"] != build_events.ERROR_BUILD_FAILED
        assert error["message"] == expected_message
        partial = application.updates["publish_partial"]
        assert partial["published"] == detail["published_artifacts"]
        assert partial["unpublished"] == detail["unpublished_artifacts"]
        assert application.updates["ended_at"] == now
        assert application.audit_action == \
            build_events.AUDIT_BUILD_PUBLISHING_FAILED
        assert application.audit_details["published"] == \
            detail["published_artifacts"]
        assert application.audit_details["unpublished"] == \
            detail["unpublished_artifacts"]
        assert application.audit_details["error_code"] == \
            build_events.ERROR_PUBLISHING_FAILED

    else:  # build-stage failure
        if current_status in (build_domain.STATUS_PROVISIONING,
                              build_domain.STATUS_BUILDING):
            # Failed with the build-failure kind, distinct from the
            # publishing kind, and no publish_partial lists (Req 5.4
            # distinctness).
            assert application.final_status == build_domain.STATUS_FAILED
            error = application.updates["error"]
            assert error["code"] == build_events.ERROR_BUILD_FAILED
            assert error["code"] != build_events.ERROR_PUBLISHING_FAILED
            assert error["message"] == expected_message
            assert "publish_partial" not in application.updates
            assert application.updates["ended_at"] == now
            assert application.audit_action == build_events.AUDIT_BUILD_FAILED
        else:
            # queued (never dispatched) or publishing (a build-stage
            # failure after the build step succeeded is out of order):
            # stale delivery, no-op.
            assert application.is_noop
