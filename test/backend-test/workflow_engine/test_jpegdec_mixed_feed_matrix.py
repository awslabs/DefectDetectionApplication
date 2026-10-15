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
"""The owner's mixed-flow gate through ``execute()``
(jpeg-folder-source-decode-ubuntu2204, design C5 and D11, Property 8).

The owner's words: "It is important those flows do not break." A mixed
document holds a JPEG file source (``folder_source``, plus a
``csi_camera_source`` subset) and a frame-feed source. Both Frame_Feed
consumers read the launch string's FIRST ``caps=`` clause as the fed
appsrc's caps: ``GstPipelineManager.create_buffer`` for non-bridged runs,
``python_bridge._fed_frame_caps`` for bridged runs (only frames without a
``format``). A file chain that renders its own ``caps=`` ahead of the fed
appsrc hands the appsrc the wrong caps.

The row space: arch {x86_64, x86_64_nvidia, arm64_cpu, arm64_jp5,
arm64_jp6, arm64_jp7} x feed {Aravis 1 byte/pixel, Aravis 3 bytes/pixel,
stream (RGB StreamFrame, on_trigger), Python source (a real producer
returning a 2x2 RGB frame)} x order {file first, feed first} x shape
{pre-fix: no capsfilter; fixed: capsfilter after jpegdec} x run
{non-bridged, bridged through a pass-through custom_python node on the
file branch}. Jetson rows use their real shape only (pre-fix; JP6 reads a
staged PNG through pngdec). Plus a ``csi_camera_source`` subset
(non-bridged, Aravis 1 byte/pixel, both orders and shapes, x86_64 plus
the Jetson rows).

Task 1 (bug condition; FAILS on the unfixed tree):
- (i) the bug rows' consumers yield the frame caps;
- (i2) every constrained file-first row starts with the fed appsrc;
- (i3) every constrained pre-fix-shape row pins each file chain once.

Task 2 (preservation; PASSES before and after the fix):
- the Jetson rows' launch strings equal frozen unfixed-tree goldens;
- every other constrained row's consumer yields the frame caps (after
  the fix this only holds if C3b ships with C3);
- every constrained fixed-shape row pins each file chain exactly once.

Every row asserts that its execution row is ``completed``, so a fixture
problem can't pass as a bug. The fixture modules are imported, never
edited; ``_seed_run`` seeds the registration with the row's arch.
"""
import os
import re
import time
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (  # sets COMPONENT_WORK_PATH first
    make_session_factory,
    write_artifact_set,
)

from test_workflow_aravis_executor import (
    FakeCameraManager,
    FakePipelineManager,
    make_aravis_document,
    make_frame,
)
from test_workflow_python_source_executor import (
    BASIC_HANDLER,
    ECHO_HANDLER,
    make_source_document,
    write_handler,
)
from test_workflow_stream_executor import (
    FakeStreamManager,
    make_frame as make_stream_frame,
    make_stream_document,
)
from workflow_engine import gst_plugins, pipeline_executor, python_bridge
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

#: The exact design string (design C1, D2).
EXPECTED_CAPS = (
    "video/x-raw,format=I420;"
    "video/x-raw,format=Y42B;"
    "video/x-raw,format=Y444;"
    "video/x-raw,format=GRAY8;"
    "video/x-raw,format=RGB"
)
CONSTRAINED_ARCHS = ("x86_64", "x86_64_nvidia", "arm64_cpu")
JETSON_ARCHS = ("arm64_jp5", "arm64_jp6", "arm64_jp7")
FEEDS = ("aravis1", "aravis3", "stream", "python")
ORDERS = ("file_first", "feed_first")
SHAPES = ("prefix", "fixed")
RUNS = ("plain", "bridged")
REGISTRATION_ID = "wf-1:3"
FOLDER_LOCATION = "/data/images/canary.jpg"
CSI_LOCATION = "/aws_dda/nvidia-csi-capture/latest.jpg"
CSI_JP6_LOCATION = "/aws_dda/nvidia-csi-capture/latest.jpg.dda_decoded.png"
STREAM_URL = "rtsp://127.0.0.1:8554/dda"

_GOLDEN_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "goldens", "jpegdec_native_caps",
    "mixed")


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
        "frozen golden '{0}' changed: the Jetson launch string differs from "
        "the unfixed-tree capture".format(name))
    return recorded


# --------------------------------------------------------------------------
# The row space
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    arch: str
    source: str  # "folder" or "csi"
    feed: str
    order: str
    shape: str
    run: str

    @property
    def row_id(self):
        return "-".join((self.arch, self.source, self.feed, self.order,
                         self.shape, self.run))

    @property
    def constrained(self):
        return self.arch in CONSTRAINED_ARCHS

    @property
    def bug(self):
        """Task 1(i): the rows whose consumer reads a clause other than the
        fed appsrc's on the unfixed tree (design D11)."""
        if not self.constrained or self.order != "file_first":
            return False
        if self.run == "plain":
            return self.shape == "fixed"
        return self.feed in ("aravis1", "aravis3")


def _all_rows():
    rows = []
    for arch in CONSTRAINED_ARCHS + JETSON_ARCHS:
        shapes = SHAPES if arch in CONSTRAINED_ARCHS else ("prefix",)
        for feed in FEEDS:
            for order in ORDERS:
                for shape in shapes:
                    for run in RUNS:
                        rows.append(Row(arch, "folder", feed, order, shape, run))
    for order in ORDERS:
        for shape in SHAPES:
            rows.append(Row("x86_64", "csi", "aravis1", order, shape, "plain"))
    for arch in JETSON_ARCHS:
        for order in ORDERS:
            rows.append(Row(arch, "csi", "aravis1", order, "prefix", "plain"))
    return rows


ROWS = _all_rows()


def _params(rows):
    return [pytest.param(row, id=row.row_id) for row in rows]


BUG_ROWS = [row for row in ROWS if row.bug]
PRESERVED_ROWS = [row for row in ROWS if row.constrained and not row.bug]
CONSTRAINED_FILE_FIRST_ROWS = [
    row for row in ROWS if row.constrained and row.order == "file_first"]
CONSTRAINED_PREFIX_ROWS = [
    row for row in ROWS if row.constrained and row.shape == "prefix"]
CONSTRAINED_FIXED_ROWS = [
    row for row in ROWS if row.constrained and row.shape == "fixed"]
JETSON_ROWS = [row for row in ROWS if not row.constrained]


# --------------------------------------------------------------------------
# Building and running one row
# --------------------------------------------------------------------------

def _file_elements(row):
    """The file source's chain for node f0, plus the bridge and the sink."""
    if row.arch == "arm64_jp6":
        location = CSI_JP6_LOCATION if row.source == "csi" else FOLDER_LOCATION
        elements = [
            {"nodeId": "f0", "factory": "filesrc",
             "args": {"blocksize": -1, "location": location}},
            {"nodeId": "f0", "factory": "pngdec", "args": {}},
            {"nodeId": "f0", "factory": "videoconvert", "args": {}},
        ]
    else:
        location = CSI_LOCATION if row.source == "csi" else FOLDER_LOCATION
        elements = [
            {"nodeId": "f0", "factory": "filesrc",
             "args": {"blocksize": -1, "location": location}},
            {"nodeId": "f0", "factory": "emexifextract", "args": {}},
            {"nodeId": "f0", "factory": "jpegparse", "args": {}},
            {"nodeId": "f0", "factory": "jpegdec", "args": {"idct-method": 2}},
        ]
        if row.shape == "fixed":
            elements.append({"nodeId": "f0", "factory": "capsfilter",
                             "args": {"caps": EXPECTED_CAPS}})
        elements += [
            {"nodeId": "f0", "factory": "videoconvert", "args": {}},
            {"nodeId": "f0", "factory": "videoflip",
             "args": {"method": "automatic"}},
        ]
    if row.run == "bridged":
        elements.append({"nodeId": "py1", "factory": "emlpython",
                         "args": {"handler-path": "python/py1/handler.py"}})
    elements.append({"nodeId": None, "factory": "fakesink", "args": {}})
    return elements


def _feed_document(row):
    if row.feed.startswith("aravis"):
        return make_aravis_document(node_id="n1")
    if row.feed == "stream":
        return make_stream_document(node_id="cam", url=STREAM_URL)
    return make_source_document(node_id="src1")


def build_document(row):
    """One mixed compiled document in the row's order and shape."""
    feed = _feed_document(row)
    file_segment = {"from": None, "linkTo": None, "elements": _file_elements(row)}
    feed_segment = feed["segments"][0]
    segments = ([file_segment, feed_segment] if row.order == "file_first"
                else [feed_segment, file_segment])
    for index, segment in enumerate(segments):
        segment["name"] = "s{0}".format(index)
    points = list(feed["bindingPoints"])
    if row.source == "csi":
        points.append({"nodeId": "f0", "nodeType": "csi_camera_source",
                       "parameters": {"gain": 4, "exposure": 5000000},
                       "slots": [], "csiSensorBinding": True})
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": row.arch,
        "segments": segments,
        "bindingPoints": points,
        "executorBindings": [],
        "pluginDependencies": [],
    }


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


@dataclass(frozen=True)
class RowResult:
    status: str
    error: object
    launch: object
    frame_data: object


def execute_row(row, work_dir):
    """Run the row's document through ``execute()`` and record what the
    runner was handed: ``FakePipelineManager.run_pipeline`` for non-bridged
    rows, a recording ``bridged_pipeline_runner`` for bridged rows."""
    session_factory = make_session_factory()
    artifact_path = write_artifact_set(work_dir, compiled=build_document(row))
    if row.feed == "python":
        write_handler(artifact_path, "src1", BASIC_HANDLER)
    if row.run == "bridged":
        write_handler(artifact_path, "py1", ECHO_HANDLER)
    execution_id = _seed_run(session_factory, artifact_path, row.arch)

    manager = FakePipelineManager()
    runner_calls = []

    def runner(launch_string, bridges, latency_metrics=None, frame_data=None):
        runner_calls.append((launch_string, [b.node_id for b in bridges],
                             frame_data))
        return {}

    kwargs = {"session_factory": session_factory,
              "pipeline_manager_factory": lambda: manager}
    if row.feed.startswith("aravis"):
        kwargs["frame_grabber"] = FakeCameraManager(
            frame=make_frame(4, 2, 1 if row.feed == "aravis1" else 3))
    if row.feed == "stream":
        kwargs["stream_ingest_manager"] = FakeStreamManager(
            frame=make_stream_frame(seq=7, width=4, height=2))
    if row.run == "bridged":
        kwargs["bridged_pipeline_runner"] = runner

    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT",
                      os.path.join(str(work_dir), "captures")), \
            patch.object(pipeline_executor, "_stage_decoded_png",
                         lambda path: path + ".dda_decoded.png"), \
            patch.object(WorkflowExecutor, "_prepare_csi_captures",
                         staticmethod(lambda plan, arch: None)):
        WorkflowExecutor(**kwargs).execute(execution_id)

    session = session_factory()
    try:
        execution = session.get(WorkflowExecution, execution_id)
        status, error = execution.status, execution.error
    finally:
        session.close()
    launch = frame_data = None
    if row.run == "bridged" and runner_calls:
        assert len(runner_calls) == 1
        launch, bridge_nodes, frame_data = runner_calls[0]
        assert bridge_nodes == ["py1"]
    elif row.run == "plain" and manager.calls:
        assert len(manager.calls) == 1
        launch, args, _ = manager.calls[0]
        frame_data = args[0] if args else None
    return RowResult(status=status, error=error, launch=launch,
                     frame_data=frame_data)


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi in these tests."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


@pytest.fixture(scope="module")
def row_result(tmp_path_factory):
    """Each row runs once per module; every test reads the same record."""
    cache = {}

    def get(row):
        if row.row_id not in cache:
            cache[row.row_id] = execute_row(row, tmp_path_factory.mktemp("row"))
        result = cache[row.row_id]
        assert result.status == EXECUTION_STATUS_COMPLETED, (
            row.row_id, result.status, result.error)
        assert isinstance(result.launch, str) and result.frame_data is not None
        return result

    return get


def assert_consumer_yields_the_frame_caps(row, result):
    """Property 8: the production consumer of the row's run yields the fed
    frame's caps."""
    frame = result.frame_data
    expected = WorkflowExecutor._frame_caps(frame)
    if row.run == "plain":
        # GstPipelineManager.create_buffer's lookup.
        match = re.search(r"caps=([^!]+)", result.launch)
        assert match is not None, result.launch
        first = match.group(1).strip()
        assert not re.search(r"\s", first), (
            "the first caps= clause is not one token: {0!r}".format(first))
        assert first == expected, result.launch
    else:
        assert python_bridge._fed_frame_caps(result.launch, frame) == (
            "{0},width={1},height={2}".format(
                expected, frame["width"], frame["height"])), result.launch


# --------------------------------------------------------------------------
# Task 1: bug condition (FAIL on the unfixed tree)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("row", _params(BUG_ROWS))
def test_bug_row_consumer_yields_the_frame_caps(row_result, row):
    """1(i): fixed shape file first non-bridged (all feeds), and bridged
    file first with an Aravis feed (both shapes)."""
    assert_consumer_yields_the_frame_caps(row, row_result(row))


@pytest.mark.parametrize("row", _params(CONSTRAINED_FILE_FIRST_ROWS))
def test_constrained_file_first_row_starts_with_the_fed_appsrc(row_result, row):
    """1(i2): the fed appsrc's segment renders first."""
    launch = row_result(row).launch
    assert launch.startswith("appsrc name=appsrc caps="), launch


@pytest.mark.parametrize("row", _params(CONSTRAINED_PREFIX_ROWS))
def test_pre_fix_shape_row_pins_each_file_chain_once(row_result, row):
    """1(i3), AC 12: the executor pins the pre-fix chain exactly once."""
    launch = row_result(row).launch
    assert launch.count(EXPECTED_CAPS) == 1, launch


# --------------------------------------------------------------------------
# Task 2: preservation (PASS before and after the fix)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("row", _params(JETSON_ROWS))
def test_jetson_row_matches_its_frozen_golden(row_result, row):
    """JP5, JP6 and JP7 launch strings stay byte-identical."""
    _capture_or_assert_text(row.row_id + ".txt", row_result(row).launch + "\n")


@pytest.mark.parametrize("row", _params(PRESERVED_ROWS))
def test_preserved_row_consumer_yields_the_frame_caps(row_result, row):
    """Every constrained row that works today keeps working, including the
    pre-fix file-first rows once C3 has pinned their chain (the "C3 never
    without C3b" guard)."""
    assert_consumer_yields_the_frame_caps(row, row_result(row))


@pytest.mark.parametrize("row", _params(CONSTRAINED_FIXED_ROWS))
def test_fixed_shape_row_pins_each_file_chain_once(row_result, row):
    """AC 12: a chain that already carries the capsfilter gains no second
    one."""
    launch = row_result(row).launch
    assert launch.count(EXPECTED_CAPS) == 1, launch
