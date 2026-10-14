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
"""Preservation property tests (Task 2) for workflow-output-bindings-fixes.

Property 6: Preservation — Triton capture routing unchanged: for any
document containing ``emltriton``, ``_inject_inference_metadata`` and
``_route_capture_outputs`` produce exactly today's output (correlation-id
injection, ``buffer-message-id``/``meta`` targets), and the future
post-run artifact repair is the IDENTITY on any run directory containing
only correctly-named ``{capture_id}.*`` files.

**Validates: Requirements 3.6**

Observation-first: the reference transform below (``_expected_document``)
re-encodes the routing/injection behavior OBSERVED on the current
(unfixed) tree — deliberately WITHOUT calling the production functions, so
a behavior change in the fixed code cannot silently move the expectation
with it:

* ``_inject_inference_metadata`` touches only ``emltriton`` elements whose
  deployed model declares a ``METADATA`` input: it fills ``metadata`` (the
  capture_id / capture-data-disk-path / fleet-name JSON) and
  ``correlation-id`` (= ``capture_id``) ONLY where those args are absent
  (an explicitly compiled value wins); models without the input — and
  models with no readable config — are left untouched;
* ``_route_capture_outputs`` mutates only terminal ``emlcapture`` elements:
  ``buffer-message-id`` becomes ``file-target_{output_dir}-jpg``, and
  ``meta`` is set to the ordered ``triton_inference_output_*`` targets of
  the union of every emltriton model's declared outputs — only when there
  is something to declare and the compiled ``meta`` is empty/absent/the
  ``{capture_meta}`` placeholder (an explicit compiled meta wins).

The repair-identity half runs the full executor over a Triton-shaped
capture document while the fake pipeline plays the broker writing
correctly-named ``{capture_id}.*`` artifacts; every seeded file must
survive the run byte-identical (no rename, no deletion). It PASSES today
(no repair step exists) and MUST keep passing after the fix (the repair
only renames empty-basename files). ``{capture_id}.json`` is deliberately
not seeded: the fix legitimately writes that file (new metadata JSON), so
it is not part of the preserved surface.

Runs with the hypothesis profiles registered in ``test/backend-test/
conftest.py`` (``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci``
= 100).
"""
import copy
import itertools
import json
import os
import shutil
import tempfile
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import DEVICE_ARCH, write_artifact_set

from executor_harness import (
    CAPTURE_ID,
    EXECUTION_ID,
    FakePipelineManager,
    WORKFLOW_ID,
    make_doc,
    seed_run,
)

from workflow_engine import pipeline_executor
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    WorkflowExecutor,
)
from workflow_engine.models import WorkflowExecution
from workflow_engine_test_utils import make_session_factory

_ALL_OUTPUTS = (
    "output_overlay",
    "output_mask",
    "output_capture",
    "output_anomalous",
    "output_confidence",
)
_MODEL_NAME = "model-widget"
_CAPTURE_META_PLACEHOLDER = "{capture_meta}"

OUTPUT_DIR = "/aws_dda/captures/wf-1/exec-1"


# ---------------------------------------------------------------------------
# Model repo fixture writer (config.pbtxt with declared outputs and an
# optional METADATA input)
# ---------------------------------------------------------------------------

def _write_repo(root, outputs, declares_metadata):
    model_dir = os.path.join(root, _MODEL_NAME)
    os.makedirs(model_dir, exist_ok=True)
    lines = ['name: "{0}"'.format(_MODEL_NAME), 'platform: "ensemble"']
    if declares_metadata:
        lines += ['input {', '  name: "METADATA"',
                  '  data_type: TYPE_STRING', '  dims: [ -1 ]', '}']
    for name in outputs:
        lines += ['output {', '  name: "{0}"'.format(name),
                  '  data_type: TYPE_UINT8', '  dims: [ -1 ]', '}']
    with open(os.path.join(model_dir, "config.pbtxt"), "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Generators: documents WITH emltriton and a terminal emlcapture
# ---------------------------------------------------------------------------

_MIDDLE_FACTORIES = st.sampled_from(
    ["videoconvert", "videoscale", "queue", "jpegenc"])

#: The emltriton element's compiled args: an explicitly compiled
#: metadata/correlation-id must win over injection (observed).
_TRITON_ARG_VARIANTS = st.sampled_from(
    ["plain", "has_metadata", "has_correlation_id", "has_both"])

#: The terminal emlcapture's compiled meta: absent, the placeholder the
#: routing fills, or an explicit value the routing must not overwrite.
_CAPTURE_META_VARIANTS = st.sampled_from(
    ["absent", "placeholder", "explicit"])


@st.composite
def _shapes(draw):
    return {
        "middles": draw(st.lists(_MIDDLE_FACTORIES, min_size=0, max_size=2)),
        "triton_args": draw(_TRITON_ARG_VARIANTS),
        "capture_meta": draw(_CAPTURE_META_VARIANTS),
        "declared": draw(st.sets(st.sampled_from(_ALL_OUTPUTS))),
        "declares_metadata": draw(st.booleans()),
    }


def _build_document(shape):
    triton_args = {"model": _MODEL_NAME}
    if shape["triton_args"] in ("has_metadata", "has_both"):
        triton_args["metadata"] = '{"compiled": "wins"}'
    if shape["triton_args"] in ("has_correlation_id", "has_both"):
        triton_args["correlation-id"] = "compiled-correlation-id"
    capture_args = {}
    if shape["capture_meta"] == "placeholder":
        capture_args["meta"] = _CAPTURE_META_PLACEHOLDER
    elif shape["capture_meta"] == "explicit":
        capture_args["meta"] = "already:routed"
    elements = [{"nodeId": "n1", "factory": "videotestsrc",
                 "args": {"num-buffers": 1}}]
    for index, factory in enumerate(shape["middles"]):
        elements.append(
            {"nodeId": "m{0}".format(index), "factory": factory, "args": {}})
    elements.append(
        {"nodeId": "triton_1", "factory": "emltriton", "args": triton_args})
    elements.append(
        {"nodeId": "capture_1", "factory": "emlcapture",
         "args": capture_args})
    return {
        "schemaVersion": 1,
        "workflowId": WORKFLOW_ID,
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [{"name": "s0", "elements": elements}],
        "executorBindings": [],
        "pluginDependencies": [],
    }


# ---------------------------------------------------------------------------
# Reference transform: today's injection + routing, re-encoded independently
# ---------------------------------------------------------------------------

def _expected_document(document, shape, output_dir, capture_id):
    expected = copy.deepcopy(document)
    elements = expected["segments"][0]["elements"]

    # _inject_inference_metadata (observed): only METADATA-declaring models,
    # only absent args are filled.
    if shape["declares_metadata"]:
        metadata_json = json.dumps({
            "capture_id": capture_id,
            "sagemaker_edge_core_capture_data_disk_path": output_dir,
            "sagemaker_edge_core_device_fleet_name": "",
        })
        for element in elements:
            if element["factory"] != "emltriton":
                continue
            if "metadata" not in element["args"]:
                element["args"]["metadata"] = metadata_json
            if "correlation-id" not in element["args"]:
                element["args"]["correlation-id"] = capture_id

    # _route_capture_outputs (observed): terminal emlcapture only.
    terminal = elements[-1]
    terminal["args"]["buffer-message-id"] = (
        "file-target_{0}-jpg".format(output_dir))
    meta = ",".join(
        pipeline_executor._CAPTURE_OUTPUT_TARGETS[name].format(p=output_dir)
        for name in pipeline_executor._CAPTURE_OUTPUT_ORDER
        if name in shape["declared"]
    )
    if meta:
        existing = terminal["args"].get("meta")
        if not existing or existing == _CAPTURE_META_PLACEHOLDER:
            terminal["args"]["meta"] = meta
    return expected


# ---------------------------------------------------------------------------
# Property 6, part 1: injection + routing identity for Triton documents
# ---------------------------------------------------------------------------

@given(shape=_shapes())
@settings(deadline=None)
def test_triton_injection_and_routing_identity(shape):
    """**Property 6: Preservation — Triton routing identity.** For any
    document with an ``emltriton`` element and a terminal ``emlcapture``,
    injection + routing produce exactly the reference document (today's
    behavior): correlation-id/metadata filled only for METADATA-declaring
    models where absent, buffer-message-id rewritten, ordered declared-
    output meta targets, explicit compiled values always winning.

    **Validates: Requirements 3.6**
    """
    document = _build_document(shape)
    repo = tempfile.mkdtemp(prefix="triton-routing-preservation-")
    try:
        _write_repo(repo, shape["declared"], shape["declares_metadata"])
        expected = _expected_document(
            document, shape, OUTPUT_DIR, CAPTURE_ID)
        with patch.object(pipeline_executor, "_TRITON_MODEL_REPO", repo):
            WorkflowExecutor._inject_inference_metadata(
                document, WORKFLOW_ID, EXECUTION_ID, OUTPUT_DIR)
            routed = WorkflowExecutor._route_capture_outputs(
                document, OUTPUT_DIR, CAPTURE_ID)
    finally:
        shutil.rmtree(repo, ignore_errors=True)

    assert routed is True, (
        "PRESERVATION REGRESSION (Property 6): a terminal-emlcapture "
        "Triton document was no longer detected as a File_Output_Node run")
    assert document == expected, (
        "PRESERVATION REGRESSION (Property 6): injection/routing output "
        "changed for shape {0!r}:\n  actual:   {1!r}\n  expected: {2!r}"
        .format(shape, document, expected))


# ---------------------------------------------------------------------------
# Property 6, part 2: the post-run repair is the identity on
# correctly-named {capture_id}.* artifacts
# ---------------------------------------------------------------------------

#: The correctly-named artifacts a Triton capture run leaves today (the
#: broker's ``{c_id}.{ext}`` products with c_id == capture_id, plus the
#: declared overlay/mask/jsonl targets). ``{capture_id}.json`` is NOT
#: seeded — the fix legitimately writes that file.
_CORRECT_ARTIFACTS = (
    CAPTURE_ID + ".jpg",
    CAPTURE_ID + "-overlay.jpg",
    CAPTURE_ID + "-mask.png",
    CAPTURE_ID + "-jsonl",
)

#: A Triton-shaped capture document (emltriton present; the default model
#: repo is unreadable in tests, so injection/routing no-op harmlessly).
_TRITON_CAPTURE_SEGMENTS = [
    {
        "name": "s0",
        "elements": [
            {"nodeId": "n1", "factory": "videotestsrc",
             "args": {"num-buffers": 1}},
            {"nodeId": "triton_1", "factory": "emltriton",
             "args": {"model": _MODEL_NAME}},
            {"nodeId": "capture_1", "factory": "emlcapture", "args": {}},
        ],
    }
]


@given(
    seeded=st.sets(st.sampled_from(_CORRECT_ARTIFACTS), min_size=1),
    filler=st.binary(min_size=1, max_size=64),
)
@settings(deadline=None)
def test_repair_is_identity_on_correctly_named_artifacts(seeded, filler):
    """**Property 6: Preservation — repair identity.** A run whose
    directory holds only correctly-named ``{capture_id}.*`` artifacts must
    leave every one of them in place, byte-identical, after the executor's
    post-run processing (no rename, no deletion). Passes today (no repair
    exists) and must keep passing after the fix (the repair touches only
    empty-basename files).

    **Validates: Requirements 3.6**
    """
    contents = {
        name: b"\xff\xd8" + filler + name.encode("utf-8")
        for name in seeded
    }
    session_factory = make_session_factory()
    root = tempfile.mkdtemp(prefix="repair-identity-")
    try:
        capture_root = os.path.join(root, "captures")
        output_dir = os.path.join(capture_root, WORKFLOW_ID, EXECUTION_ID)
        document = make_doc(segments=_TRITON_CAPTURE_SEGMENTS, bindings=[])
        artifact_path = write_artifact_set(root, compiled=document)
        execution_id = seed_run(session_factory, artifact_path)

        def broker_writes_correctly_named_artifacts():
            os.makedirs(output_dir, exist_ok=True)
            for name, data in contents.items():
                with open(os.path.join(output_dir, name), "wb") as f:
                    f.write(data)

        with patch.object(
                pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root):
            WorkflowExecutor(
                session_factory=session_factory,
                pipeline_manager_factory=lambda: FakePipelineManager(
                    tag_values={"is_anomalous": False},
                    on_run=broker_writes_correctly_named_artifacts),
            ).execute(execution_id)

        session = session_factory()
        try:
            row = session.get(WorkflowExecution, execution_id)
            assert row.status == EXECUTION_STATUS_COMPLETED
        finally:
            session.close()

        entries = sorted(os.listdir(output_dir))
        for name, data in contents.items():
            assert name in entries, (
                "PRESERVATION REGRESSION (Property 6): correctly-named "
                "artifact {0!r} disappeared from the run directory "
                "(entries: {1!r})".format(name, entries))
            with open(os.path.join(output_dir, name), "rb") as f:
                assert f.read() == data, (
                    "PRESERVATION REGRESSION (Property 6): correctly-named "
                    "artifact {0!r} was modified by post-run processing"
                    .format(name))
    finally:
        shutil.rmtree(root, ignore_errors=True)
