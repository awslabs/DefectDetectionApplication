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
"""Property test for watcher registration re-evaluation (task 14.5).

**Feature: camera-registry-sync, Property 21: Registration re-evaluation
on camera appearance**

*For any* document and binding whose resolution is invalid against an
inventory missing the referenced Camera_Source, re-resolving after adding
that source to the inventory yields a resolved document, and
re-evaluation flips the registration from invalid to registered.

**Validates: Requirements 10.4**

The generated scenarios drive the real ``WorkflowWatcher`` +
``CameraBindingStore`` integration: 1-2 artifact sets on disk, each with
1-3 binding points bound via the bindings shadow to Camera_Source ids
drawn from a small universe, and a mutable local inventory that changes
across a sequence of discovery ``on_change`` re-evaluations. Every
scenario starts with at least one bound id missing (so at least one
registration is invalid) and contains at least one step where every
bound id is present (so the invalid-to-registered flip is always
exercised); steps that remove cameras exercise the reverse flip. After
every step each registration must be ``registered`` exactly when all of
its bound ids resolve, and ``invalid`` with a ``missing camera source
{csid}`` reason per unresolved binding point otherwise.

Runs with the hypothesis profiles registered in this directory's conftest
(``engine-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import copy
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from test_workflow_binding_store import THING, FakeShadowAccessor
from workflow_engine_test_utils import (
    VALID_COMPILED,
    make_session_factory,
    make_watcher,
    write_artifact_set,
)

from workflow_engine.camera_binding_store import CameraBindingStore, binding_key
from workflow_engine.models import WorkflowRegistration

VERSION = "1"

# --- generators --------------------------------------------------------------


@st.composite
def _scenarios(draw):
    """A re-evaluation scenario: workflows with bound binding points, an
    initial inventory missing at least one bound id, and a sequence of
    inventory snapshots (cameras appearing and disappearing) with at
    least one snapshot resolving everything."""
    universe = [
        "cfg-cam-{}".format(i)
        for i in range(draw(st.integers(min_value=2, max_value=4)))
    ]

    # 1-2 artifact sets, each binding 1-3 nodes to ids from the universe
    # (ids may repeat across nodes and workflows).
    workflows = []
    for w in range(draw(st.integers(min_value=1, max_value=2))):
        bound_ids = draw(
            st.lists(st.sampled_from(universe), min_size=1, max_size=3)
        )
        workflows.append(("wf-{}".format(w), bound_ids))

    all_bound = {csid for _, bound_ids in workflows for csid in bound_ids}

    # Initial inventory: any subset, forced to miss at least one bound id
    # so the scenario starts from an invalid registration (10.2 precondition).
    initial = draw(st.sets(st.sampled_from(universe)))
    if all_bound <= initial:
        initial.discard(draw(st.sampled_from(sorted(all_bound))))

    # 1-4 steps of arbitrary appearance/disappearance; one guaranteed
    # step where every bound id is present, so the invalid registration
    # must flip to registered (10.4).
    steps = [
        draw(st.sets(st.sampled_from(universe)))
        for _ in range(draw(st.integers(min_value=1, max_value=3)))
    ]
    steps.insert(draw(st.integers(min_value=0, max_value=len(steps))),
                 set(all_bound))

    return universe, workflows, initial, steps


# --- harness -----------------------------------------------------------------


def _compiled_for(workflow_id, bound_ids):
    """A compiled document with one element and one device slot per
    bound binding point."""
    document = copy.deepcopy(VALID_COMPILED)
    document["workflowId"] = workflow_id
    document["workflowVersion"] = VERSION
    document["segments"] = [{
        "name": "s0",
        "elements": [
            {
                "nodeId": "n{}".format(i),
                "factory": "videotestsrc",
                "args": {"device": "/dev/default-{}".format(i)},
            }
            for i in range(len(bound_ids))
        ],
    }]
    document["bindingPoints"] = [
        {
            "nodeId": "n{}".format(i),
            "nodeType": "camera_source",
            "parameters": {"device": "/dev/default-{}".format(i)},
            "slots": [
                {"param": "device", "segment": 0, "element": i, "arg": "device"}
            ],
        }
        for i in range(len(bound_ids))
    ]
    return document


def _device_path(universe, csid):
    return "/dev/video{}".format(universe.index(csid))


def _statuses(session_factory):
    session = session_factory()
    try:
        return {
            row.id: row.status
            for row in session.query(WorkflowRegistration).all()
        }
    finally:
        session.close()


def _assert_step(universe, workflows, present, watcher, session_factory):
    """Requirement 10.4 invariant: after a re-evaluation each registration
    is registered exactly when every bound id resolves, and invalid with
    one ``missing camera source {csid}`` reason per unresolved binding
    point otherwise."""
    statuses = _statuses(session_factory)
    for workflow_id, bound_ids in workflows:
        registration_id = "{}:{}".format(workflow_id, VERSION)
        missing = [csid for csid in bound_ids if csid not in present]

        if missing:
            assert statuses[registration_id] == "invalid"
            assert watcher.invalid_reason(registration_id) == "; ".join(
                "missing camera source {}".format(csid) for csid in missing
            )
            assert watcher.binding_resolution(registration_id) is None
        else:
            assert statuses[registration_id] == "registered"
            assert watcher.invalid_reason(registration_id) is None
            resolution = watcher.binding_resolution(registration_id)
            assert resolution is not None
            elements = resolution.document["segments"][0]["elements"]
            for i, csid in enumerate(bound_ids):
                assert elements[i]["args"]["device"] == _device_path(
                    universe, csid
                )


# --- property ----------------------------------------------------------------


@settings(deadline=None)
@given(scenario=_scenarios())
def test_registration_reevaluation_on_camera_appearance(scenario):
    """**Feature: camera-registry-sync, Property 21: Registration
    re-evaluation on camera appearance**

    **Validates: Requirements 10.4**
    """
    universe, workflows, initial, steps = scenario
    root = tempfile.mkdtemp(prefix="workflow_reeval_")
    try:
        session_factory = make_session_factory()

        bindings = {}
        for workflow_id, bound_ids in workflows:
            write_artifact_set(
                root, workflow_id, VERSION,
                compiled=_compiled_for(workflow_id, bound_ids),
            )
            bindings[binding_key(workflow_id, VERSION)] = {
                "n{}".format(i): {"cameraSourceId": csid}
                for i, csid in enumerate(bound_ids)
            }
        store = CameraBindingStore(
            FakeShadowAccessor({"desired": {"bindings": bindings}}),
            thing_name=THING,
        )

        inventory = {}

        def set_present(present):
            inventory.clear()
            for csid in present:
                inventory[csid] = {
                    "params": {"devicePath": _device_path(universe, csid)}
                }

        watcher = make_watcher(
            root, session_factory,
            binding_store=store,
            inventory_provider=lambda: dict(inventory),
        )

        set_present(initial)
        watcher.sync_once()
        _assert_step(universe, workflows, initial, watcher, session_factory)

        for present in steps:
            set_present(present)
            watcher.on_discovery_change()
            _assert_step(universe, workflows, present, watcher,
                         session_factory)
    finally:
        shutil.rmtree(root, ignore_errors=True)
