# Copyright 2026 Amazon Web Services, Inc.
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
"""WorkflowExecutor jpegdec native-caps normalization and feed-segment rule
(jpeg-folder-source-decode-ubuntu2204, tasks 1 and 2).

On x86_64, x86_64_nvidia and arm64_cpu (GStreamer 1.20.x), a JPEG file
chain must pin ``jpegdec`` to its native formats, or libjpeg-turbo's direct
RGB output fails ("Improper call to JPEG library in state 205"). Packages
compiled before the fix lack the pin, so the executor inserts it at run
time (design C3, ``_constrain_jpegdec_output``) and moves the fed appsrc's
segment first, so the launch string's first ``caps=`` stays the fed
appsrc's own (design C3b, ``_feed_segment_first``).

Bug-condition cases (task 1; they FAIL on the unfixed tree):
- (d) Properties 1 to 4 of ``_constrain_jpegdec_output``, with Hypothesis;
- (d2) Property 7 of ``_feed_segment_first``, with Hypothesis;
- (e) the vendored compile plus render of ``folder_source -> capture``;
- (f) ``execute()`` renders a pre-fix x86_64 chain with one capsfilter;
- (g) the executor's constants are the vendored ones (identity).

Preservation cases (task 2; they PASS before and after the fix): golden
launch strings for documents the fix must not change, captured from the
unfixed tree under ``goldens/jpegdec_native_caps/`` and frozen afterwards.

New names are looked up with ``getattr(..., None)``, so a missing name
fails per case instead of breaking collection. No GStreamer is needed.
"""
import copy
import os
import time
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import (  # sets COMPONENT_WORK_PATH first
    make_session_factory,
    write_artifact_set,
)

from test_workflow_aravis_executor import (
    FakeCameraManager,
    FakePipelineManager,
    make_aravis_document,
)
from workflow_engine import gst_plugins, pipeline_executor, rendering
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)
from workflow_engine.vendor.workflow_core.catalog import nodes as vendored_nodes
from workflow_engine.vendor.workflow_core.compiler import (
    CompileContext,
    CompiledPipelineDocument,
)
from workflow_engine.vendor.workflow_core.compiler import compile as vendored_compile
from workflow_engine.vendor.workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

#: The exact design string (design C1, D2).
EXPECTED_CAPS = (
    "video/x-raw,format=I420;"
    "video/x-raw,format=Y42B;"
    "video/x-raw,format=Y444;"
    "video/x-raw,format=GRAY8;"
    "video/x-raw,format=RGB"
)
EXPECTED_TOKEN = 'capsfilter caps="{0}"'.format(EXPECTED_CAPS)
CONSTRAINED_ARCHS = ("x86_64", "x86_64_nvidia", "arm64_cpu")
ARCH_VALUES = ("x86_64", "x86_64_nvidia", "arm64_cpu", "arm64_jp5",
               "arm64_jp6", "arm64_jp7", "sim", "unknown", None)
REGISTRATION_ID = "wf-1:3"
CANARY = "/data/images/canary.jpg"

_GOLDEN_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "goldens", "jpegdec_native_caps")


def _capture_or_assert_text(name, current_text):
    """Capture ``current_text`` as the golden ``name`` when it is absent (the
    first run, on the unfixed tree), else assert byte equality with it. The
    ``_cmake_preservation_support.capture_or_assert_text`` pattern."""
    path = os.path.join(_GOLDEN_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(current_text)
        return current_text
    with open(path, encoding="utf-8") as fh:
        recorded = fh.read()
    assert current_text == recorded, (
        "frozen golden '{0}' changed: the launch string differs from the "
        "unfixed-tree capture".format(name))
    return recorded


def _factory(element):
    return element.get("factory") if isinstance(element, dict) else None


# --------------------------------------------------------------------------
# Documents and runs
# --------------------------------------------------------------------------

def catalog_file_elements(node_id="f0", location=CANARY, pinned=False):
    """The catalog JPEG chain (``_jpeg_file_chain``), optionally with the
    native-caps capsfilter the fixed compiler emits after ``jpegdec``."""
    elements = [
        {"nodeId": node_id, "factory": "filesrc",
         "args": {"blocksize": -1, "location": location}},
        {"nodeId": node_id, "factory": "emexifextract", "args": {}},
        {"nodeId": node_id, "factory": "jpegparse", "args": {}},
        {"nodeId": node_id, "factory": "jpegdec", "args": {"idct-method": 2}},
    ]
    if pinned:
        elements.append({"nodeId": node_id, "factory": "capsfilter",
                         "args": {"caps": EXPECTED_CAPS}})
    elements += [
        {"nodeId": node_id, "factory": "videoconvert", "args": {}},
        {"nodeId": node_id, "factory": "videoflip",
         "args": {"method": "automatic"}},
    ]
    return elements


def jp6_png_elements(node_id="f0", location=CANARY):
    """JP6's PNG-staged chain (``_jp6_png_staged_chain``)."""
    return [
        {"nodeId": node_id, "factory": "filesrc",
         "args": {"blocksize": -1, "location": location}},
        {"nodeId": node_id, "factory": "pngdec", "args": {}},
        {"nodeId": node_id, "factory": "videoconvert", "args": {}},
    ]


def fakesink():
    return {"nodeId": None, "factory": "fakesink", "args": {}}


def single_segment_document(arch, elements):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": arch,
        "segments": [{"name": "s0", "from": None, "linkTo": None,
                      "elements": elements}],
        "executorBindings": [],
        "pluginDependencies": [],
    }


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi in these tests."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


def _seed_run(session_factory, artifact_path, arch):
    """A registration with the row's arch plus a pending execution."""
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id=REGISTRATION_ID, workflow_id="wf-1", version="3", arch=arch,
            artifact_path=str(artifact_path), status="registered",
            registered_at=int(time.time())))
        session.add(WorkflowExecution(
            id="exec-1", registration_id=REGISTRATION_ID,
            started_at=int(time.time()), status=EXECUTION_STATUS_PENDING))
        session.commit()
    finally:
        session.close()
    return "exec-1"


def run_document(tmp_path, session_factory, document, arch, grabber=None):
    """Run ``document`` through ``execute()`` with a stubbed pipeline manager
    and return the launch string handed to it. The run must complete."""
    artifact_path = write_artifact_set(tmp_path, compiled=document)
    execution_id = _seed_run(session_factory, artifact_path, arch)
    manager = FakePipelineManager()
    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT",
                      str(tmp_path / "captures")), \
            patch.object(pipeline_executor, "_stage_decoded_png",
                         lambda path: path + ".dda_decoded.png"):
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            frame_grabber=grabber,
        ).execute(execution_id)
    session = session_factory()
    try:
        row = session.get(WorkflowExecution, execution_id)
        assert row.status == EXECUTION_STATUS_COMPLETED, (row.status, row.error)
    finally:
        session.close()
    assert len(manager.calls) == 1
    return manager.calls[0][0]


# --------------------------------------------------------------------------
# (d) Properties 1 to 4: _constrain_jpegdec_output
# --------------------------------------------------------------------------

def _model_constrain(document, arch):
    """Reference model: the document the call must leave, and its count."""
    expected = copy.deepcopy(document)
    if not isinstance(expected, dict) or arch not in CONSTRAINED_ARCHS:
        return expected, 0
    segments = expected.get("segments")
    if not isinstance(segments, list):
        return expected, 0
    count = 0
    for segment in segments:
        elements = segment.get("elements") if isinstance(segment, dict) else None
        if not isinstance(elements, list):
            continue
        rebuilt = []
        for index, element in enumerate(elements):
            rebuilt.append(element)
            if (0 < index < len(elements) - 1
                    and _factory(elements[index - 1]) == "jpegparse"
                    and _factory(element) == "jpegdec"
                    and _factory(elements[index + 1]) == "videoconvert"):
                rebuilt.append({"factory": "capsfilter",
                                "nodeId": element.get("nodeId"),
                                "args": {"caps": EXPECTED_CAPS}})
                count += 1
        elements[:] = rebuilt
    return expected, count


_json_scalars = st.one_of(
    st.none(), st.booleans(), st.integers(),
    st.floats(allow_nan=False, allow_infinity=False), st.text(max_size=8))
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=8), children, max_size=4)),
    max_leaves=12)
_FACTORIES = ("jpegparse", "jpegdec", "videoconvert", "capsfilter", "fakesink",
              "filesrc", "emexifextract", "videoflip", "appsrc")
_NODE_IDS = st.sampled_from(["f0", "f1", None])


def _element(factory, node_id):
    return {"factory": factory, "nodeId": node_id, "args": {}}


_catalog_runs = _NODE_IDS.map(lambda node_id: [
    _element("jpegparse", node_id), _element("jpegdec", node_id),
    _element("videoconvert", node_id)])
_near_miss_runs = st.sampled_from([
    ("jpegdec", "fakesink"),
    ("jpegparse", "jpegdec", "capsfilter"),
    ("videoconvert", "jpegdec", "jpegparse"),
    ("jpegdec", "videoconvert"),
    ("jpegparse", "videoconvert"),
]).map(lambda factories: [_element(f, "f9") for f in factories])
_junk_elements = st.one_of(
    _json_scalars,
    st.builds(dict),
    st.dictionaries(st.sampled_from(["factory", "nodeId", "args", "x"]),
                    _json_values, max_size=3),
    st.sampled_from(_FACTORIES).map(lambda f: {"factory": f}),
)
_element_runs = st.one_of(
    _catalog_runs,
    _near_miss_runs,
    st.lists(_junk_elements, max_size=3),
    st.lists(st.sampled_from(_FACTORIES).map(lambda f: _element(f, "r")),
             max_size=4),
)
_element_lists = st.lists(_element_runs, max_size=5).map(
    lambda runs: [element for run in runs for element in run])
_segments = st.one_of(
    st.fixed_dictionaries({"name": st.just("s0"), "elements": _element_lists}),
    st.fixed_dictionaries(
        {"elements": _element_lists},
        optional={"from": st.sampled_from([None, "t0"]),
                  "linkTo": st.sampled_from([None, "f0"])}),
    st.fixed_dictionaries({"elements": _json_values}),
    _json_values,
)
_documents = st.one_of(
    st.fixed_dictionaries({"segments": st.lists(_segments, max_size=4)}),
    st.fixed_dictionaries({"segments": _json_values}),
    _json_values,
)


@settings(max_examples=400, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(document=_documents, arch=st.sampled_from(ARCH_VALUES))
@example(document={"segments": True}, arch="x86_64")
@example(document={"segments": 1.5}, arch="x86_64")
@example(document={"segments": 5}, arch="x86_64")
@example(document={"segments": "s"}, arch="x86_64")
@example(document={"segments": [1, "s", None, {"elements": 5},
                                {"elements": [None, 2, "x"]}]}, arch="arm64_cpu")
@example(document={"segments": [{"elements": catalog_file_elements()}]},
         arch="x86_64_nvidia")
def test_constrain_jpegdec_output_properties(document, arch):
    """P1: one capsfilter per catalog sequence, carrying jpegdec's nodeId,
    and the return value is the count. P2: a second call returns 0 and
    changes nothing. P3: other arches, or no sequence, leave the document
    deep-equal and return 0. P4: JSON-shaped input never raises."""
    function = getattr(WorkflowExecutor, "_constrain_jpegdec_output", None)
    assert function is not None, "WorkflowExecutor._constrain_jpegdec_output is missing"
    expected, count = _model_constrain(document, arch)

    assert function(document, arch) == count
    assert document == expected

    settled = copy.deepcopy(document)
    assert function(document, arch) == 0
    assert document == settled


# --------------------------------------------------------------------------
# (d2) Property 7: _feed_segment_first (executor)
# --------------------------------------------------------------------------

def _is_fed_segment(segment):
    """The fed appsrc contract (design Validation Rules): a segment with no
    ``from`` whose ``elements[0]`` is ``appsrc`` renamed ``appsrc``."""
    if not isinstance(segment, dict) or segment.get("from"):
        return False
    elements = segment.get("elements")
    if not isinstance(elements, list) or not elements:
        return False
    head = elements[0]
    args = head.get("args") if isinstance(head, dict) else None
    return (_factory(head) == "appsrc" and isinstance(args, dict)
            and args.get("name") == "appsrc")


def _fed_segment():
    return {"name": "s_fed", "from": None, "linkTo": None, "elements": [
        {"nodeId": "n1", "factory": "appsrc",
         "args": {"name": "appsrc", "caps": "video/x-raw,format=GRAY8"}},
        {"nodeId": "n1", "factory": "videoconvert", "args": {}},
        fakesink(),
    ]}


_near_miss_segments = st.sampled_from([
    # the compiled (not renamed) appsrc
    lambda: {"name": "s_c", "elements": [
        {"nodeId": "n1", "factory": "appsrc", "args": {"name": "appsrc_n1"}}]},
    # a bridge appsrc
    lambda: {"name": "s_py", "elements": [
        {"nodeId": "py1", "factory": "appsrc",
         "args": {"name": "py_out_py1", "is-live": True}}, fakesink()]},
    # a fed-looking appsrc that is not elements[0]
    lambda: {"name": "s_mid", "elements": [
        {"nodeId": None, "factory": "funnel", "args": {"name": "f0"}},
        {"nodeId": "n1", "factory": "appsrc",
         "args": {"name": "appsrc", "caps": "video/x-raw"}}]},
    # a fed-looking appsrc under a tee
    lambda: {"name": "s_from", "from": "t0", "elements": [
        {"nodeId": "n1", "factory": "appsrc",
         "args": {"name": "appsrc", "caps": "video/x-raw"}}]},
]).map(lambda make: make())
_other_segments = st.one_of(
    st.just(None).map(lambda _: {
        "name": "s_file", "from": None, "linkTo": None,
        "elements": catalog_file_elements(pinned=True) + [fakesink()]}),
    st.just(None).map(lambda _: {
        "name": "s_branch", "from": "t0", "linkTo": None,
        "elements": [{"nodeId": None, "factory": "queue", "args": {}}, fakesink()]}),
    st.just(None).map(lambda _: {
        "name": "s_link", "from": None, "linkTo": "f0",
        "elements": catalog_file_elements()}),
    _near_miss_segments,
    _json_values,
)


@st.composite
def _feed_documents(draw):
    segments = draw(st.lists(_other_segments, max_size=6))
    if draw(st.booleans()):
        segments.insert(draw(st.integers(0, len(segments))), _fed_segment())
    shape = draw(st.integers(0, 9))
    if shape == 0:
        return draw(_json_values)
    if shape == 1:
        return {"segments": draw(_json_values)}
    return {"segments": segments}


@settings(max_examples=400, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(document=_feed_documents(), arch=st.sampled_from(ARCH_VALUES))
@example(document={"segments": True}, arch="x86_64")
@example(document={"segments": 1.5}, arch="x86_64")
@example(document={"segments": 5}, arch="x86_64")
@example(document={"segments": "s"}, arch="x86_64")
@example(document={"segments": [None, 3, "s", {"elements": "x"},
                                {"elements": [None]}, _fed_segment()]},
         arch="arm64_cpu")
def test_feed_segment_first_properties(document, arch):
    """P7: on a constrained arch the fed segment ends at index 0, the others
    keep their relative order and identity, and the call returns
    ``index > 0``; a second call returns False and changes nothing; any
    other arch, or no fed appsrc, leaves the document deep-equal and returns
    False; JSON-shaped input never raises."""
    function = getattr(WorkflowExecutor, "_feed_segment_first", None)
    assert function is not None, "WorkflowExecutor._feed_segment_first is missing"
    snapshot = copy.deepcopy(document)
    segments = document.get("segments") if isinstance(document, dict) else None
    before = list(segments) if isinstance(segments, list) else None
    index = None
    if before is not None:
        index = next((i for i, s in enumerate(before) if _is_fed_segment(s)), None)

    moved = function(document, arch)

    if arch not in CONSTRAINED_ARCHS or index is None:
        assert moved is False
        assert document == snapshot
    else:
        assert moved is (index > 0)
        after = document["segments"]
        assert after[0] is before[index]
        assert [id(s) for s in after[1:]] == [
            id(s) for i, s in enumerate(before) if i != index]

    settled = copy.deepcopy(document)
    assert function(document, arch) is False
    assert document == settled


# --------------------------------------------------------------------------
# (e) The vendored compile, rendered
# --------------------------------------------------------------------------

def _folder_capture_graph():
    position = Position(0.0, 0.0)
    return WorkflowGraph(
        nodes=[Node(id="src", type="folder_source", position=position,
                    parameters={"location": "/data/images"}),
               Node(id="cap", type="capture", position=position,
                    parameters={"output_path": "/out"})],
        connections=[Connection(id="c1", source=PortEndpoint("src", "out"),
                                target=PortEndpoint("cap", "in"))],
    )


def test_vendored_compile_renders_the_pinned_chain():
    document = vendored_compile(_folder_capture_graph(), "x86_64",
                                CompileContext(workflow_id="wf-1", workflow_version="3"))
    assert isinstance(document, CompiledPipelineDocument), document
    launch = rendering.render_launch_string(document.to_dict())
    assert ("jpegdec idct-method=2 ! {0} ! videoconvert".format(EXPECTED_TOKEN)
            in launch), launch


# --------------------------------------------------------------------------
# (f) execute(): a pre-fix x86_64 document renders one capsfilter
# --------------------------------------------------------------------------

def test_execute_pins_a_pre_fix_x86_64_chain(tmp_path, session_factory):
    document = single_segment_document(
        "x86_64", catalog_file_elements() + [fakesink()])
    launch = run_document(tmp_path, session_factory, document, "x86_64")
    assert launch.count(EXPECTED_TOKEN) == 1, launch
    assert "jpegdec idct-method=2 ! {0} ! videoconvert".format(EXPECTED_TOKEN) in launch


# --------------------------------------------------------------------------
# (g) One constant, one owner (design D5)
# --------------------------------------------------------------------------

def test_executor_constants_are_the_vendored_ones():
    caps = getattr(vendored_nodes, "JPEGDEC_NATIVE_CAPS", None)
    archs = getattr(vendored_nodes, "JPEGDEC_NATIVE_CAPS_ARCHS", None)
    assert caps is not None and archs is not None
    assert getattr(pipeline_executor, "JPEGDEC_NATIVE_CAPS", None) is caps
    assert getattr(pipeline_executor, "JPEGDEC_NATIVE_CAPS_ARCHS", None) is archs


# --------------------------------------------------------------------------
# Task 2: preservation (frozen unfixed-tree launch strings)
# --------------------------------------------------------------------------

PRESERVED_CASES = {
    "arm64_jp5_catalog_chain": (
        "arm64_jp5",
        lambda: single_segment_document(
            "arm64_jp5", catalog_file_elements() + [fakesink()])),
    "arm64_jp7_catalog_chain": (
        "arm64_jp7",
        lambda: single_segment_document(
            "arm64_jp7", catalog_file_elements() + [fakesink()])),
    "arm64_jp6_pngdec_chain": (
        "arm64_jp6",
        lambda: single_segment_document(
            "arm64_jp6", jp6_png_elements() + [fakesink()])),
    "x86_64_filesrc_jpegdec_fakesink": (
        "x86_64",
        lambda: single_segment_document("x86_64", [
            {"nodeId": "f0", "factory": "filesrc", "args": {"location": CANARY}},
            {"nodeId": "f0", "factory": "jpegdec", "args": {}},
            fakesink(),
        ])),
    "x86_64_already_pinned_chain": (
        "x86_64",
        lambda: single_segment_document(
            "x86_64", catalog_file_elements(pinned=True) + [fakesink()])),
    "x86_64_aravis_single_source": (
        "x86_64", lambda: make_aravis_document()),
}


@pytest.mark.parametrize("case_id", sorted(PRESERVED_CASES))
def test_preserved_launch_string_matches_the_frozen_golden(
        tmp_path, session_factory, case_id):
    arch, make_document = PRESERVED_CASES[case_id]
    grabber = FakeCameraManager() if "aravis" in case_id else None
    launch = run_document(tmp_path, session_factory, make_document(), arch,
                          grabber=grabber)
    _capture_or_assert_text(case_id + ".txt", launch + "\n")
