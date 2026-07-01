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
"""Preservation baselines that are DEFERRED to the runtime / build gate.

Spec: python-3-11-security-upgrade — Property 2: Preservation — No functional
regression for non-3.9 artifacts.

Three preservation requirements are not expressible as pure-logic tests in a bare
checkout (nor even as unit tests inside the image): they assert the *runtime
output* of native pipelines and the *packaged build artifact* on each target.
Their baselines are captured and verified by the integration gates:

* **Req 3.2 — GStreamer pipeline output.** Building/executing a streaming +
  snapshot pipeline and comparing output to the 3.9 baseline needs the
  ``gi``/GStreamer system stack and a source. Verified by the runtime smoke tests
  (task 13, design "Preservation Checking" case 2).
* **Req 3.3 — Triton Python-backend inference.** Loading a model through the
  rebuilt Python backend and comparing inference results to the 3.9 baseline needs
  the Triton server + the ``libpython3.11``-linked backend + a model. Verified by
  the runtime smoke tests (task 13, case 3).
* **Req 3.7 — per-target packaged artifact.** Producing a working packaged
  deployment artifact for JP5 / JP6 / amd64 needs a full ``build-custom.sh`` run per
  target. Verified by the per-target build gate (task 12, case 7).

These are recorded here as ``skip``-marked placeholders so the deferred baselines
are explicit and traceable. The actual verification is performed by tasks 12/13,
not by an importable test. They are NOT a pass/fail gate for task 2 (which only
captures the baselines runnable on the unfixed bare tree).
"""
import pytest


# Spec: python-3-11-security-upgrade — Property 2: Preservation
# Validates: Requirements 3.2
@pytest.mark.skip(
    reason="Req 3.2 GStreamer pipeline-output preservation is verified by the runtime "
           "smoke tests (task 13); requires the gi/GStreamer stack + a source."
)
def test_gstreamer_pipeline_output_preserved():
    """DEFERRED (task 13): streaming + snapshot pipeline output matches the 3.9 baseline."""
    raise AssertionError("runtime gate — not executed in the bare/unit environment")


# Spec: python-3-11-security-upgrade — Property 2: Preservation
# Validates: Requirements 3.3
@pytest.mark.skip(
    reason="Req 3.3 Triton inference preservation is verified by the runtime smoke tests "
           "(task 13); requires the Triton server + the libpython3.11-linked backend + a model."
)
def test_triton_inference_results_preserved():
    """DEFERRED (task 13): model load + inference results match the 3.9 baseline."""
    raise AssertionError("runtime gate — not executed in the bare/unit environment")


# Spec: python-3-11-security-upgrade — Property 2: Preservation
# Validates: Requirements 3.7
@pytest.mark.skip(
    reason="Req 3.7 per-target packaging preservation is verified by the per-target build "
           "gate (task 12); requires a full build-custom.sh run for JP5/JP6/amd64."
)
def test_per_target_packaged_artifact_preserved():
    """DEFERRED (task 12): JP5/JP6/amd64 each still produce a working packaged artifact."""
    raise AssertionError("build gate — not executed in the bare/unit environment")
