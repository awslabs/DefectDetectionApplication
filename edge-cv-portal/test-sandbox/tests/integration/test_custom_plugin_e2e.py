"""Containerized workflow test-run integration test with a real custom
x86_64 Plugin_Artifact (custom-node-designer task 13.3, Requirements
12.1, 12.2).

Runs the real workflow test harness (``python3 -m harness``) inside the
sandbox container image against moto-served S3, reusing the session
fixtures from ``conftest.py`` (Docker/moto/Pillow prerequisites; each
test skips cleanly when one is missing), following the task 8.5 pattern
from ``test_simulate_e2e.py``: the staged Plugin_Artifact is a real
loadable GStreamer plugin ``.so`` extracted from the sandbox image
itself.

Unlike the simulate-mode tests, the plugin's system copy is deleted
inside the container before the harness starts, so the ``videoflip``
element used by the workflow's custom node exists *only* through the
custom-plugin staging path: ``custom_plugins.json`` manifest discovery,
S3 download into the task's plugin scan directory, ``GST_PLUGIN_PATH``
extension, and registry load (12.1). A passing pipeline therefore
proves the staged custom x86_64 artifact was actually loaded and
executed within the simulated pipeline.

The workflow also contains a stubbed Custom_Node_Type (the compile
step's ``custom_stub_<nodeId>`` identity substitution for a node without
an x86_64 build); the flushed test run report must identify it as
stubbed with the limitation note that it was simulated because no
x86_64 build exists (12.2).

Documented limitation (same shape as test_simulate_e2e.py): no custom
plugin can be compiled in CI, so the staged ``.so`` is the image's stock
videofilter plugin rather than a portal-built artifact; only the
plugin's provenance differs.
"""

import json
import subprocess
import uuid

import pytest

pytestmark = pytest.mark.integration

#: Path (arch-globbed) of the real loadable GStreamer plugin inside the
#: image that is staged as the run's custom Plugin_Artifact. It provides
#: the ``videoflip`` element the custom node maps to.
_IMAGE_PLUGIN_GLOB = "/usr/lib/*/gstreamer-1.0/libgstvideofilter.so"

#: Harness entrypoint that first removes the plugin's system copy, so
#: the custom element resolves only through the staged artifact (12.1).
_REMOVE_SYSTEM_COPY_AND_RUN = [
    "sh", "-c",
    "rm -f {0} && python3 -m harness".format(_IMAGE_PLUGIN_GLOB),
]

#: The limitation note the harness records on stubbed Custom_Node_Types
#: (harness.py CUSTOM_NODE_STUB_NOTE; duplicated here so the assertion
#: checks the exact report wording, not a shared constant).
CUSTOM_NODE_STUB_NOTE = (
    "Simulated: this custom node has no x86_64 build; a pass-through "
    "stub recorded the frames the node would have consumed and passed "
    "them through unchanged"
)


@pytest.fixture(scope="session")
def custom_plugin_so_bytes(sandbox_image):
    """A real loadable plugin ``.so`` extracted from the sandbox image
    itself, so its architecture always matches the image under test
    (see module docstring limitation)."""
    completed = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", sandbox_image,
         "-c", "cat {0}".format(_IMAGE_PLUGIN_GLOB)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    if completed.returncode != 0 or not completed.stdout:
        pytest.skip("could not extract a plugin .so from the sandbox image")
    return completed.stdout


def make_custom_node_document():
    """A compiled document exercising both custom-node paths (12.1, 12.2):

    - ``custom1``: a Custom_Node_Type with an x86_64 Plugin_Artifact —
      its ``videoflip`` element must come from the staged custom plugin.
    - ``custom2``: a Custom_Node_Type without an x86_64 build — the
      compile step substituted the ``custom_stub_<nodeId>`` pass-through
      recording stub.
    """
    return {
        "schemaVersion": 1,
        "workflowId": "wf-custom-it",
        "workflowVersion": "1",
        "targetArch": "x86_64",
        "segments": [{
            "name": "s0",
            "from": None,
            "linkTo": None,
            "elements": [
                {"nodeId": "cam", "factory": "multifilesrc",
                 "args": {"location": "{dataset_location}"}},
                {"nodeId": "cam", "factory": "jpegparse", "args": {}},
                {"nodeId": "cam", "factory": "jpegdec", "args": {}},
                {"nodeId": "cam", "factory": "videoconvert", "args": {}},
                {"nodeId": "custom1", "factory": "videoflip",
                 "args": {"method": "rotate-180"}},
                {"nodeId": "custom2", "factory": "identity",
                 "args": {"name": "custom_stub_custom2"}},
                {"nodeId": "sink", "factory": "fakesink",
                 "args": {"sync": False}},
            ],
        }],
        "executorBindings": [],
        "pluginDependencies": [],
    }


def records_by_node(results):
    return {record["nodeId"]: record for record in results["nodes"]}


def test_e2e_test_run_executes_staged_custom_plugin_and_reports_stub(
        sandbox_run, custom_plugin_so_bytes):
    """End-to-end workflow test run with a real custom x86_64 artifact:
    the harness downloads the manifest-listed Plugin_Artifact into its
    plugin scan path and executes its element within the simulated
    pipeline (12.1 — the system copy is removed, so only the staged
    artifact provides the element), while the Custom_Node_Type without
    an x86_64 build is reported as stubbed with the limitation note
    (12.2)."""
    # Stage the custom Plugin_Artifact under the run's prefix and write
    # the custom_plugins.json manifest next to the results document —
    # exactly the layout the workflow_test_steps.py compile step stages.
    run_prefix = sandbox_run.results_key.rsplit("/", 1)[0]
    # The staged file keeps the plugin's own filename: GStreamer derives
    # the plugin entry symbol (gst_plugin_<name>_get_desc) from the
    # libgst<name>.so filename, so a renamed .so does not load.
    plugin_key = run_prefix + "/plugins/libgstvideofilter.so"
    sandbox_run.s3.put_object(Bucket=sandbox_run.bucket, Key=plugin_key,
                              Body=custom_plugin_so_bytes)
    manifest = {
        "plugins": [{
            "nodeTypeId": "custom.uc-integration.videoflip",
            "fileName": "libgstvideofilter.so",
            "s3Key": plugin_key,
        }],
        "stubbedNodeTypeIds": ["custom.uc-integration.unbuilt"],
    }
    sandbox_run.s3.put_object(
        Bucket=sandbox_run.bucket, Key=run_prefix + "/custom_plugins.json",
        Body=json.dumps(manifest).encode("utf-8"))

    sandbox_run.upload_document(make_custom_node_document())
    sandbox_run.upload_dataset_jpegs(count=3)

    exit_code, logs = sandbox_run.run_harness(
        command=_REMOVE_SYSTEM_COPY_AND_RUN)
    assert exit_code == 0, "harness exited {0}; logs:\n{1}".format(
        exit_code, logs)

    # The staged artifact was downloaded into the task's scan directory
    # before GStreamer initialized (12.1).
    assert "Staged 1 custom plugin(s)" in logs
    assert "libgstvideofilter.so" in logs

    results = sandbox_run.fetch_results()
    records = records_by_node(results)
    assert set(records) == {"cam", "custom1", "custom2", "sink"}

    # The custom node backed by the staged x86_64 artifact executed for
    # real: completed, no error, and no custom_node_stub activity —
    # with the system copy removed, videoflip could only have loaded
    # from the staged custom plugin (12.1).
    assert records["custom1"]["status"] == "completed"
    assert records["custom1"]["error"] is None
    assert not [entry for entry in records["custom1"]["stubActivity"]
                if entry.get("type") == "custom_node_stub"]

    # The Custom_Node_Type without an x86_64 build is identified as
    # stubbed, with the limitation note describing that it was simulated
    # because no x86_64 build exists (12.2).
    assert records["custom2"]["status"] == "completed"
    stub_entries = [entry for entry in records["custom2"]["stubActivity"]
                    if entry.get("type") == "custom_node_stub"]
    assert len(stub_entries) == 1
    assert stub_entries[0]["element"] == "custom_stub_custom2"
    assert stub_entries[0]["frameCount"] == 3
    assert stub_entries[0]["note"] == CUSTOM_NODE_STUB_NOTE

    # The dataset fed the source and the sink completed: the full
    # pipeline (including the custom element) processed the frames.
    assert records["cam"]["status"] == "completed"
    assert records["sink"]["status"] == "completed"
