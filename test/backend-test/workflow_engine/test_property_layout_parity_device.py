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
"""The device half of the Layout Check parity test (car-assembly-check
task 9.2 - Requirements 5.7, 5.9; the rtsp-rtmp-stream-cameras Property 27
rule applied to ``layout.<nodeId>``).

The corpus is the one the sandbox half replays
(``edge-cv-portal/test-sandbox/tests/fixtures/layout_check_parity_cases.json``,
``tests/test_layout_check_bindings.py``): pass, one misplaced, one missing,
duplicate, a detection without a mask, an unexpected class, and an
instance outside the car outline. Each case runs through the real device
path: a capture record whose detections block carries the case's raw
entries and whose input image has the case's frame size, the run's
Detection_List (``tag_values["detections"]``) set to the built entries,
then ``scene_analytics.apply_scene_analytics`` with the capture location.
The merged section must equal the pinned one, which the sandbox produces
too.
"""
import base64
import copy
import json
import os

import pytest

from workflow_engine.scene_analytics import LAYOUT_KEY, apply_scene_analytics

FIXTURES = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "edge-cv-portal", "test-sandbox", "tests", "fixtures",
    "layout_check_parity_cases.json"))
CAPTURE_ID = "parity-capture"


def _cases():
    if not os.path.isfile(FIXTURES):
        return []
    with open(FIXTURES, "r", encoding="utf-8") as handle:
        return json.load(handle)["cases"]


CASES = _cases()


def _write_capture_record(directory, case):
    from PIL import Image
    image_path = directory / "{0}.png".format(CAPTURE_ID)
    Image.new("L", (case["frame"]["width"], case["frame"]["height"])).save(str(image_path))
    payload = {"detections": {str(index): entry for index, entry in enumerate(case["raw_entries"])}}
    record = {
        "deviceFleetAuxiliaryInputs": [{"data-ref": "file://{0}".format(image_path)}],
        "deviceFleetAuxiliaryOutputs": [{
            "observedContentType": "json_with_base64_encoding",
            "data": base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"),
        }],
    }
    (directory / "{0}.jsonl".format(CAPTURE_ID)).write_text(json.dumps(record) + "\n")
    return str(directory)


def test_the_shared_corpus_is_present():
    if not CASES:
        pytest.skip("the Portal test sandbox fixtures are not in this checkout")
    assert len(CASES) == 7


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_device_reproduces_the_sandbox_section(case, tmp_path):
    parameters = dict(case["parameters"], expected_regions=case["regions"])
    document = {"workflowId": "wf-parity", "workflowVersion": "1", "executorBindings": [
        {"nodeId": "lay", "binding": "layout_check", "parameters": parameters,
         "upstreamNodeIds": ["det"], "downstreamNodeIds": []}]}
    tag_values = {"detections": copy.deepcopy(case["built_entries"])}
    output_dir = _write_capture_record(tmp_path, case)
    apply_scene_analytics(document, tag_values, output_dir=output_dir, capture_id=CAPTURE_ID)
    assert tag_values[LAYOUT_KEY]["lay"] == case["expected_section"]
