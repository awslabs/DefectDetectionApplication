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
Property test for the Build_Job status state machine in
``edge-cv-portal/backend/functions/build_domain.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 4.1, 5.1**

The expected edge set below is transcribed from the DESIGN state machine
(not from the implementation's transition table), so the test independently
checks that every movement the transition function makes follows a designed
edge and that terminal statuses absorb every event.
"""
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Import the pure domain module from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402


# Design state machine, transcribed from design.md (section: job state machine).
# source status -> set of statuses reachable by a single defined edge.
_DESIGN_EDGES = {
    build_domain.STATUS_QUEUED: {
        build_domain.STATUS_PROVISIONING,  # dispatch ephemeral (3.1)
        build_domain.STATUS_BUILDING,      # dispatch dedicated (7.5)
        build_domain.STATUS_CANCELLED,     # cancel while queued (4.5)
        build_domain.STATUS_FAILED,        # dead-server sweep (7.9)
    },
    build_domain.STATUS_PROVISIONING: {
        build_domain.STATUS_BUILDING,      # runner ready
        build_domain.STATUS_FAILED,        # provisioning failed (3.7)
    },
    build_domain.STATUS_BUILDING: {
        build_domain.STATUS_PUBLISHING,    # build succeeded (5.1)
        build_domain.STATUS_FAILED,        # build failed / timeout / serialization
        build_domain.STATUS_INTERRUPTED,   # runner reclaimed (3.5)
        build_domain.STATUS_CANCELLED,     # confirmed cancel (4.6)
    },
    build_domain.STATUS_PUBLISHING: {
        build_domain.STATUS_SUCCEEDED,     # publish succeeded (5.3)
        build_domain.STATUS_FAILED,        # publish failed (5.4) / timeout
        build_domain.STATUS_INTERRUPTED,   # runner reclaimed (3.5)
        build_domain.STATUS_CANCELLED,     # confirmed cancel (4.6)
    },
    # Terminal statuses have no outgoing edges (absorption, Req 4.1).
    build_domain.STATUS_SUCCEEDED: set(),
    build_domain.STATUS_FAILED: set(),
    build_domain.STATUS_INTERRUPTED: set(),
    build_domain.STATUS_CANCELLED: set(),
}

_STATUSES = st.sampled_from(sorted(build_domain.ALL_STATUSES))
_EVENTS = st.sampled_from(sorted(build_domain.ALL_EVENTS))


# Feature: portal-build-fleet-and-workflow-gates, Property 7: Status transitions follow the state machine and terminal states absorb
@settings(max_examples=200)
@given(initial=_STATUSES, events=st.lists(_EVENTS, min_size=1, max_size=12))
def test_status_transitions_follow_state_machine_and_terminal_states_absorb(
    initial, events
):
    """For any starting status and any sequence of events, every transition
    step yields exactly one status from the defined set, every movement
    follows a designed state-machine edge, and any event applied to a
    terminal status leaves it unchanged (terminal absorption)."""
    current = initial
    for event in events:
        nxt = build_domain.next_status(current, event)

        # A Build_Job always holds exactly one status from the defined set.
        assert isinstance(nxt, str)
        assert nxt in build_domain.ALL_STATUSES

        if build_domain.is_terminal(current):
            # Terminal absorption: no event ever moves a terminal job.
            assert nxt == current
        elif nxt != current:
            # Any movement must follow an edge of the designed state machine,
            # and the implementation must report it as a defined transition.
            assert nxt in _DESIGN_EDGES[current]
            assert build_domain.is_valid_transition(current, event)

        # Once terminal, always terminal (absorption persists across the
        # remainder of the event sequence via the loop).
        if build_domain.is_terminal(current):
            assert build_domain.is_terminal(nxt)

        current = nxt
