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
Property test for resolved-commit recording in the pure
event-application function ``apply_phase_event`` of
``edge-cv-portal/backend/functions/build_events.py``.

Spec: .kiro/specs/build-source-selection (task 12)

**Validates: Requirements 4.5, 7.6**

For any agent phase event applied to any Build_Job status:

- WITH a ``source_commit`` on the event detail, the applied field
  updates persist it (``updates['source_commit']``) so the job is
  traceable to an exact source state even if the branch moves
  (Req 4.5) — and every OTHER part of the application (transition
  chain, remaining updates, audit action and details) is identical to
  the legacy application without the field;
- WITHOUT a ``source_commit`` (a legacy agent), the application is
  byte-identical to today's: no ``source_commit`` key ever appears,
  and no other field changes (Req 7.6 — the existing agent contract
  and phase emissions keep working).

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

#: 40-hex resolved commit SHAs, as `git rev-parse HEAD` produces.
_SHAS = st.from_regex(r"[0-9a-f]{40}", fullmatch=True)

_AGENT_TEXT = st.text(min_size=1, max_size=30)
_ARTIFACT_LISTS = st.lists(_AGENT_TEXT, max_size=4)


@st.composite
def _phase_details(draw):
    """A random agent phase event detail across the whole emission
    domain: building, publishing, succeeded, failed (build-stage and
    publishing-stage), plus an unknown phase (ignored delivery)."""
    phase = draw(st.sampled_from(sorted(build_events.KNOWN_PHASES)
                                 + ["bogus_phase"]))
    detail = {
        "phase": phase,
        "build_job_id": "job-property-13",
        "build_target": draw(st.sampled_from(
            ["JP5", "JP6", "AMD64", "AMD64_NVIDIA"])),
    }
    if phase == build_events.PHASE_BUILDING:
        detail["source_ref"] = draw(st.one_of(
            st.just(""), _AGENT_TEXT))
    elif phase == build_events.PHASE_SUCCEEDED:
        detail["result"] = {
            "component_name": draw(_AGENT_TEXT),
            "published_version": draw(st.from_regex(
                r"[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3}", fullmatch=True)),
            "pushed_image_refs": draw(_ARTIFACT_LISTS),
        }
    elif phase == build_events.PHASE_FAILED:
        error_kind = draw(st.sampled_from(
            [build_events.ERROR_KIND_PUBLISHING, "building", None]))
        if error_kind is not None:
            detail["error_kind"] = error_kind
        if error_kind == build_events.ERROR_KIND_PUBLISHING:
            detail["published_artifacts"] = draw(_ARTIFACT_LISTS)
            detail["unpublished_artifacts"] = draw(_ARTIFACT_LISTS)
        message = draw(st.one_of(st.none(), st.text(max_size=40)))
        if message is not None:
            detail["error_message"] = message
    return detail


# Feature: build-source-selection, Property 13: Resolved commit recording
# Validates: Requirements 4.5, 7.6
@settings(max_examples=300, deadline=None)
@given(current_status=st.sampled_from(_STATUSES),
       detail=_phase_details(),
       source_commit=st.one_of(st.none(), _SHAS),
       now=st.integers(min_value=1, max_value=2 ** 41))
def test_resolved_commit_recording(current_status, detail, source_commit,
                                   now):
    """A present ``source_commit`` is persisted with the applied phase
    transition (Req 4.5); its absence leaves the application identical
    to today's in every field, so legacy agents keep working
    (Req 7.6)."""
    legacy = build_events.apply_phase_event(current_status, dict(detail),
                                            now)

    payload = dict(detail)
    if source_commit is not None:
        payload["source_commit"] = source_commit
    application = build_events.apply_phase_event(current_status, payload,
                                                 now)

    if source_commit is None:
        # Legacy payload: byte-identical application, and the new key
        # never appears.
        assert application == legacy
        assert "source_commit" not in application.updates
        return

    # The transition chain and audit output are untouched by the new
    # field — it is purely additive.
    assert application.steps == legacy.steps
    assert application.audit_action == legacy.audit_action
    assert application.audit_details == legacy.audit_details

    if application.is_noop:
        # A no-op delivery (duplicate/stale/terminal/unknown phase)
        # writes nothing, commit or not.
        assert application.updates == {}
    else:
        # Persisted when present (Req 4.5) ...
        assert application.updates["source_commit"] == source_commit
        # ... and the record is otherwise unchanged in every field.
        remaining = {k: v for k, v in application.updates.items()
                     if k != "source_commit"}
        assert remaining == legacy.updates


# ---------------------------------------------------------------------------
# Unit examples
# ---------------------------------------------------------------------------

_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_building_event_with_commit_persists_it():
    """provisioning + phase=building carrying source_commit records the
    SHA alongside started_at (Req 4.5)."""
    application = build_events.apply_phase_event(
        build_domain.STATUS_PROVISIONING,
        {"phase": build_events.PHASE_BUILDING, "source_ref": "main",
         "source_commit": _SHA},
        1000,
    )
    assert not application.is_noop
    assert application.updates["source_commit"] == _SHA
    assert application.updates["started_at"] == 1000


def test_building_event_without_commit_unchanged():
    """A legacy building event (no source_commit) produces exactly
    today's updates: started_at only (Req 7.6)."""
    application = build_events.apply_phase_event(
        build_domain.STATUS_PROVISIONING,
        {"phase": build_events.PHASE_BUILDING, "source_ref": "main"},
        1000,
    )
    assert not application.is_noop
    assert application.updates == {"started_at": 1000}


def test_empty_commit_is_treated_as_absent():
    """An empty source_commit (rev-parse failed on the runner) is
    ignored — never persisted as an empty string."""
    application = build_events.apply_phase_event(
        build_domain.STATUS_PROVISIONING,
        {"phase": build_events.PHASE_BUILDING, "source_commit": ""},
        1000,
    )
    assert "source_commit" not in application.updates


def test_agent_building_emission_carries_source_commit():
    """The agent's phase=building emission includes the additive
    source_commit field from `git rev-parse HEAD`, with the existing
    fields untouched (Req 4.5, 7.6)."""
    script = os.path.join(_REPO_ROOT, "scripts", "portal-build-agent.sh")
    with open(script) as f:
        text = f.read()
    building_lines = [line for line in text.splitlines()
                      if '"phase":"building"' in line
                      and "build_job_id" in line]
    assert building_lines, "phase=building emission not found in the agent"
    emission = building_lines[0]
    # Existing fields untouched, new field appended.
    assert '"build_job_id":"%s"' in emission
    assert '"build_target":"%s"' in emission
    assert '"source_ref":"%s"' in emission
    assert '"source_commit":"%s"' in emission
    assert "git rev-parse HEAD" in text
