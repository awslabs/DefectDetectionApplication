#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""Byte parity of five Portal test-sandbox copies with their ``src/backend``
sources.

The test sandbox image ships copies of LocalServer modules under
``edge-cv-portal/test-sandbox/dda_triton_resources/``. These five had
drifted: each copy lacked a ``src/backend`` commit (the GPU provider record,
the manifest ``score_threshold`` fix, the detections sidecar and the opt-in
letterbox input). They were copied again from ``src/backend``, which is
canonical, and each must stay byte-identical to its source: a change to one
copy has to be copied to the other.
"""
import hashlib
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SRC = os.path.join(_ROOT, "src", "backend")
_SANDBOX = os.path.join(_ROOT, "edge-cv-portal", "test-sandbox", "dda_triton_resources")

#: (source under src/backend, copy under the sandbox's dda_triton_resources/)
COPIES = (
    ("dda_triton/resources_for_copy/inference_runtimes.py", "inference_runtimes.py"),
    ("dda_triton/resources_for_copy/lfv_model_template.py", "lfv_model_template.py"),
    (
        "dda_triton/resources_for_copy/marshal_for_capture_template.py",
        "marshal_for_capture_template.py",
    ),
    (
        "lyra_science_processing_utils/model_processors/basic_preprocessor.py",
        "lyra_science_processing_utils/model_processors/basic_preprocessor.py",
    ),
    (
        "lyra_science_processing_utils/model_processors/yolo_detection_postprocessor.py",
        "lyra_science_processing_utils/model_processors/yolo_detection_postprocessor.py",
    ),
)


def _sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


@pytest.mark.parametrize(
    "source,copy", COPIES, ids=[os.path.basename(copy) for _, copy in COPIES]
)
def test_sandbox_copy_is_byte_identical_to_its_source(source, copy):
    canonical = os.path.join(_SRC, source)
    sandbox = os.path.join(_SANDBOX, copy)
    assert os.path.isfile(canonical), canonical
    assert os.path.isfile(sandbox), sandbox
    assert _sha256(canonical) == _sha256(sandbox), (
        "edge-cv-portal/test-sandbox/dda_triton_resources/{} differs from "
        "src/backend/{}; copy the changed file (src/backend is canonical)".format(copy, source)
    )
