"""Property test for the Plugin_Record lifecycle state machine (task 3.2).

**Feature: custom-node-designer, Property 10: Lifecycle state machine conformance**

For all random sequences of operations (create record, create new
version, record build success/failure per arch, promote, demote,
approve review, reject review) applied to Plugin_Records, the
implementation agrees with a reference model: every new record and
every new version starts in dev with review pending regardless of
prior versions; dev->test succeeds if and only if at least one
successfully built Plugin_Artifact exists (otherwise rejected
identifying the missing build); test->prod succeeds if and only if
the security review is approved (otherwise rejected identifying the
missing approval); demotion always succeeds and only changes the
state.

**Validates: Requirements 9.1, 9.4, 9.5, 9.9, 9.10, 9.13, 10.1, 10.5**

The lifecycle guards under test (`new_version_item`,
`evaluate_promotion`, `evaluate_demotion`, `successful_build_archs`)
are pure over the Plugin_Record version-item dicts, so the state
machine is exercised directly with no AWS involvement. The module is
imported through the shared moto-backed session fixture only so the
real `shared_utils` layer (not a test fake) backs the import.
"""

from __future__ import annotations

import copy

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="session")
def records(aws_stack):
    """The real plugin_records module, imported via the session stack."""
    return aws_stack.plugin_records


# ---------------------------------------------------------------------------
# Reference model: the lifecycle state machine restated from the
# requirements (9.1, 9.4, 9.5, 9.9, 9.10, 9.13, 10.1, 10.5) rather than
# imported, so the test cannot silently agree with a wrong implementation.
# ---------------------------------------------------------------------------

class ModelVersion:
    """One Plugin_Record version in the reference model."""

    def __init__(self):
        # Every new record and new version starts in dev with the
        # security review pending, independently of prior versions.
        self.state = "dev"
        self.review = "pending"
        self.succeeded_archs = set()

    def build(self, arch, ok):
        if ok:
            self.succeeded_archs.add(arch)
        else:
            self.succeeded_archs.discard(arch)

    def promote(self):
        """Returns (next_state, None) or (None, expected_error_code)."""
        if self.state == "dev":
            if self.succeeded_archs:
                return "test", None
            return None, "PLUGIN_BUILD_REQUIRED"
        if self.state == "test":
            if self.review == "approved":
                return "prod", None
            return None, "SECURITY_REVIEW_REQUIRED"
        # No transition forward from prod (or any other value).
        return None, "INVALID_LIFECYCLE_TRANSITION"

    def demote(self):
        """Demotion always succeeds one step back; none exists from dev."""
        if self.state == "prod":
            return "test", None
        if self.state == "test":
            return "dev", None
        return None, "INVALID_LIFECYCLE_TRANSITION"


# ---------------------------------------------------------------------------
# Operation sequences
# ---------------------------------------------------------------------------

ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")

#: Each operation carries a selector integer used to pick the target
#: version (mod the number of versions existing when it executes).
_selector = st.integers(min_value=0, max_value=999)

operations = st.lists(
    st.one_of(
        st.tuples(st.just("new_version"), _selector),
        st.tuples(st.just("build"), _selector,
                  st.sampled_from(ARCHS), st.booleans()),
        st.tuples(st.just("promote"), _selector),
        st.tuples(st.just("demote"), _selector),
        st.tuples(st.just("review"), _selector,
                  st.sampled_from(("approved", "rejected"))),
    ),
    max_size=40,
)


def _artifact_entry(arch, ok):
    """Per-arch artifact entry as the build result handler records it."""
    return {
        "s3Key": f"workflow-plugins/custom/uc/{arch}/p.so",
        "checksum": "ab" * 32,
        "signature": "sig-bytes",
        "buildStatus": "succeeded" if ok else "failed",
        "logTail": "" if ok else "error: boom",
    }


def _assert_fresh(mod, item):
    """New records and new versions start dev + pending (9.1, 9.13,
    10.1, 10.5), with no artifacts carried over."""
    assert item["lifecycle_state"] == mod.STATE_DEV
    assert item["review"]["decision"] == mod.REVIEW_PENDING
    assert item["artifacts"] == {}


def _assert_agrees(mod, item, model):
    """The implementation item matches the reference model state."""
    assert item["lifecycle_state"] == model.state
    assert item["review"]["decision"] == model.review
    assert mod.successful_build_archs(item) == sorted(model.succeeded_archs)


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(ops=operations)
def test_lifecycle_state_machine_conformance(records, ops):
    """**Feature: custom-node-designer, Property 10: Lifecycle state machine conformance**

    For all random sequences of lifecycle operations applied to
    Plugin_Record version items, the implementation agrees with the
    reference state machine, every rejection carries the identifying
    error, and guard evaluation never mutates the record.

    **Validates: Requirements 9.1, 9.4, 9.5, 9.9, 9.10, 9.13, 10.1, 10.5**
    """
    mod = records

    def new_version(version):
        item = mod.new_version_item(
            plugin_id="plugin-p10", version=version, usecase_id="uc-p10",
            name="p10", kind="scaffold", user_id="user-p10",
            timestamp=1_700_000_000_000 + version,
        )
        _assert_fresh(mod, item)
        return item

    # Create the record (version 1, dev + pending: 9.1, 10.1).
    items = [new_version(1)]
    model = [ModelVersion()]

    for op in ops:
        kind, selector = op[0], op[1]
        idx = selector % len(items)
        item, mv = items[idx], model[idx]

        if kind == "new_version":
            # A new version starts dev + pending regardless of the
            # states and approvals prior versions reached (9.13, 10.5).
            items.append(new_version(len(items) + 1))
            model.append(ModelVersion())

        elif kind == "build":
            arch, ok = op[2], op[3]
            item["artifacts"][arch] = _artifact_entry(arch, ok)
            mv.build(arch, ok)

        elif kind == "promote":
            before = copy.deepcopy(item)
            next_state, error = mod.evaluate_promotion(item)
            # Guard evaluation only decides; it never mutates (9.12
            # analogue for promotion: only the state may change, and
            # only when the handler applies the returned next state).
            assert item == before

            expected_state, expected_code = mv.promote()
            if expected_state is not None:
                # dev->test with a build present, or test->prod with an
                # approved review, succeeds one step forward (9.4, 9.9).
                assert error is None
                assert next_state == expected_state
                item["lifecycle_state"] = next_state
                mv.state = expected_state
            else:
                # Rejected: no state change, identifying the missing
                # build (9.5) or the missing approval (9.10).
                assert next_state is None
                assert error["code"] == expected_code
                if expected_code == "PLUGIN_BUILD_REQUIRED":
                    assert "Plugin_Artifact" in error["details"]["missing"]
                elif expected_code == "SECURITY_REVIEW_REQUIRED":
                    assert "security review" in error["details"]["missing"]

        elif kind == "demote":
            before = copy.deepcopy(item)
            next_state, error = mod.evaluate_demotion(item)
            assert item == before

            expected_state, expected_code = mv.demote()
            if expected_state is not None:
                # Demotion always succeeds one step back and only
                # changes the state: review and artifacts untouched.
                assert error is None
                assert next_state == expected_state
                item["lifecycle_state"] = next_state
                mv.state = expected_state
            else:
                # No transition below dev exists.
                assert next_state is None
                assert error["code"] == expected_code

        elif kind == "review":
            decision = op[2]
            item["review"] = {"decision": decision, "reviewer": "admin-p10",
                              "reviewedAt": 1_700_000_000_000}
            mv.review = decision

        _assert_agrees(mod, item, mv)

    # Final sweep: every version still agrees with its model twin, so
    # operations on one version never leaked into another.
    for item, mv in zip(items, model):
        _assert_agrees(mod, item, mv)
