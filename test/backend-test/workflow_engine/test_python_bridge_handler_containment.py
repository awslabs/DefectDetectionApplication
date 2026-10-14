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
"""Handler-path containment for Custom_Python_Node bridges
(security-scan-remediation-high, Requirement 7.2).

``build_bridges`` and ``build_producer_bridge`` join the compiled
document's handler path onto the component artifact directory. The real
path of the result must lie strictly inside the artifact directory's
real path; a valid document keeps today's joined path string. The check
runs when a bridge is built, so no handler subprocess starts here.
"""
import os
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from workflow_engine.python_bridge import (
    BridgeSpec,
    CustomPythonNodeError,
    build_bridges,
    build_producer_bridge,
)

NODE_ID = "n1"
VALID = "python/n1/handler.py"
ESCAPING = ["../../etc/x.py", "/usr/lib/x.py", "python/../../x.py"]
OUTSIDE = "resolves outside the component artifact directory"


def _per_frame(artifact, relative):
    spec = BridgeSpec(
        node_id=NODE_ID, handler_path=relative,
        sink_name="py_in_n1", src_name="py_out_n1",
    )
    (bridge,) = build_bridges([spec], str(artifact))
    return bridge._handler_path


def _producer(artifact, relative):
    feed = SimpleNamespace(node_id=NODE_ID, handler_path=relative)
    return build_producer_bridge(feed, str(artifact))._handler_path


BUILDERS = pytest.mark.parametrize(
    "build", [_per_frame, _producer],
    ids=["build_bridges", "build_producer_bridge"],
)


@pytest.fixture
def artifact(tmp_path):
    root = tmp_path / "components" / "artifact"
    (root / "python" / "n1").mkdir(parents=True)
    (root / "python" / "n1" / "handler.py").write_text("")
    return root


@BUILDERS
def test_valid_handler_path_is_accepted_unchanged(artifact, build):
    assert build(artifact, VALID) == os.path.join(str(artifact), VALID)


@BUILDERS
@pytest.mark.parametrize("relative", ESCAPING)
def test_escaping_handler_paths_are_rejected_naming_the_node(
    artifact, build, relative
):
    with pytest.raises(CustomPythonNodeError) as excinfo:
        build(artifact, relative)
    assert excinfo.value.node_id == NODE_ID
    message = str(excinfo.value)
    assert "Custom Python node 'n1'" in message
    assert "handler path '{0}' {1}".format(relative, OUTSIDE) in message


@BUILDERS
@pytest.mark.parametrize("relative", [".", "python/.."])
def test_the_artifact_directory_itself_is_not_a_handler(
    artifact, build, relative
):
    with pytest.raises(CustomPythonNodeError, match=OUTSIDE):
        build(artifact, relative)


@BUILDERS
def test_symlink_inside_the_artifact_pointing_outside_is_rejected(
    artifact, tmp_path, build
):
    outside = tmp_path / "elsewhere.py"
    outside.write_text("")
    os.symlink(str(outside), str(artifact / "python" / "n1" / "link.py"))
    with pytest.raises(CustomPythonNodeError, match=OUTSIDE) as excinfo:
        build(artifact, "python/n1/link.py")
    assert excinfo.value.node_id == NODE_ID


@BUILDERS
def test_symlink_inside_the_artifact_pointing_inside_is_accepted(
    artifact, build
):
    (artifact / "lib").mkdir()
    (artifact / "lib" / "real.py").write_text("")
    os.symlink(
        str(artifact / "lib" / "real.py"),
        str(artifact / "python" / "n1" / "link.py"),
    )
    relative = "python/n1/link.py"
    assert build(artifact, relative) == os.path.join(str(artifact), relative)


@BUILDERS
def test_artifact_directory_reached_through_a_symlink_still_accepts(
    artifact, tmp_path, build
):
    alias = tmp_path / "current"
    os.symlink(str(artifact), str(alias))
    assert build(alias, VALID) == os.path.join(str(alias), VALID)


@BUILDERS
def test_unknown_artifact_directory_is_rejected_naming_the_node(build):
    with pytest.raises(CustomPythonNodeError) as excinfo:
        build("", VALID)
    assert excinfo.value.node_id == NODE_ID
    assert "component artifact directory is unknown" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Property 2 (security-scan-remediation-high)
# ---------------------------------------------------------------------------

SEGMENTS = ["..", ".", "python", "n1", "handler.py", "/"]


@pytest.fixture(scope="module")
def property_artifact(tmp_path_factory):
    """A nested artifact whose ``n1`` entry is a symlink that leads out,
    so the generated paths also cross a symlink boundary."""
    base = tmp_path_factory.mktemp("containment")
    root = base / "components" / "artifact"
    (root / "python" / "n1").mkdir(parents=True)
    (root / "python" / "n1" / "handler.py").write_text("")
    (base / "outside").mkdir()
    os.symlink(str(base / "outside"), str(root / "n1"))
    return str(root)


# Feature: security-scan-remediation-high, Property 2: Custom Python
# handlers stay inside the artifact. Validates: Requirements 7.2
# Runs at Hypothesis's own default example count, taken from its built-in
# "default" profile, because the conftests' fast profiles lower it to 25.
@pytest.mark.parametrize(
    "build", [_per_frame, _producer],
    ids=["build_bridges", "build_producer_bridge"],
)
@settings(max_examples=settings.get_profile("default").max_examples, deadline=None)
@given(relative=st.lists(st.sampled_from(SEGMENTS), max_size=8).map("".join))
@example(relative=VALID)
@example(relative="n1/handler.py")
def test_property_handlers_stay_inside_the_artifact(
    property_artifact, build, relative
):
    try:
        returned = build(property_artifact, relative)
    except CustomPythonNodeError as error:
        assert error.node_id == NODE_ID
        return
    root = os.path.realpath(property_artifact)
    resolved = os.path.realpath(returned)
    assert resolved != root
    assert os.path.commonpath([root, resolved]) == root
    assert returned == os.path.join(property_artifact, relative)
