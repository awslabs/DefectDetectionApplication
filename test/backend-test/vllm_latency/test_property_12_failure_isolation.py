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
"""Property test for failure isolation under optimization (task 2.5).

# Feature: vllm-workflow-latency-optimization, Property 12: Failure isolation preserved under optimization

*For any* set of loaded fake models and *for any* generation failure injected
into one model's engine, the failure SHALL surface through the existing error
types with the failing model named, and every other loaded model SHALL remain
READY and continue serving Generation_Calls successfully.

**Validates: Requirements 9.6**

The test drives :class:`vllm_runtime.manager.VllmRuntimeManager` through its
injectable ``engine_factory`` seam (the same fake-engine pattern as
``test/backend-test/vllm_runtime/test_generate_with_breakdown_units.py``):

* 2–5 fake models are loaded, each with its own engine; one engine is rigged
  to raise (either directly in ``generate`` or while streaming outputs, and
  optionally reporting itself ``errored`` — the dead-engine case);
* the failing model is driven through a hypothesis-chosen generate path
  (the instrumented ``generate_with_breakdown`` or the delegating
  ``generate`` — both instrumented code paths of this feature);
* the failure must surface as the existing :class:`GenerationError` naming
  the failing model (``model_name`` attribute), with no new error type;
* every OTHER loaded model must still report READY and must still serve a
  subsequent Generation_Call returning exactly its expected text;
* isolation: only the failing model's state may change (READY for a
  per-request error with a healthy engine, FAILED only when the engine
  reported itself dead).
"""
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vllm_runtime.manager import (
    GenerationError,
    ModelState,
    VllmRuntimeManager,
)


# ---------------------------------------------------------------------------
# Fake engines (AsyncLLMEngine surface used by the manager)
# ---------------------------------------------------------------------------

def _request_output(text):
    return SimpleNamespace(
        prompt_token_ids=[1, 2, 3],
        outputs=[SimpleNamespace(text=text, token_ids=[7, 8],
                                 finish_reason="stop")],
        metrics=None,
    )


class _WorkingEngine:
    """Yields one scripted request output per generate call; reusable
    across any number of calls."""

    def __init__(self, text):
        self._text = text

    def generate(self, prompt, sampling_params, request_id):
        return self._stream()

    async def _stream(self):
        yield _request_output(self._text)


class _FailingEngine:
    """Raises the injected error either synchronously in ``generate`` or
    asynchronously while streaming; optionally reports itself errored
    (the dead-engine case the manager marks FAILED)."""

    def __init__(self, message, raise_in_generate, errored):
        self._message = message
        self._raise_in_generate = raise_in_generate
        self.errored = errored

    def generate(self, prompt, sampling_params, request_id):
        if self._raise_in_generate:
            raise RuntimeError(self._message)
        return self._stream()

    async def _stream(self):
        raise RuntimeError(self._message)
        yield  # pragma: no cover - unreachable


# ---------------------------------------------------------------------------
# Manager harness
# ---------------------------------------------------------------------------

def _stage_repository(model_dir: Path, model_name: str) -> None:
    version_dir = model_dir / model_name / "1"
    version_dir.mkdir(parents=True)
    (model_dir / model_name / "config.pbtxt").write_text('backend: "vllm"\n')
    (version_dir / "model.json").write_text("{}")


def _loaded_manager(model_dir: Path, engines):
    """Load every (name, engine) pair; each load must reach READY."""
    queue = [engine for _, engine in engines]
    manager = VllmRuntimeManager(
        model_dir=model_dir,
        engine_factory=lambda engine_args: queue.pop(0),
        sampling_params_factory=dict,
    )
    for name, _ in engines:
        _stage_repository(model_dir, name)
        status = asyncio.run(manager.load(name))
        assert status.state is ModelState.READY
    return manager


def _generate(manager, path, model_name):
    """Drive one Generation_Call through the hypothesis-chosen path,
    returning the generated text."""
    if path == "generate_with_breakdown":
        text, _ = asyncio.run(
            manager.generate_with_breakdown(model_name, "prompt")
        )
        return text
    return asyncio.run(manager.generate(model_name, "prompt"))


# ---------------------------------------------------------------------------
# Strategy: a multi-model scenario with one rigged engine
# ---------------------------------------------------------------------------

_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1, max_size=12,
)

_PATH = st.sampled_from(["generate", "generate_with_breakdown"])


@st.composite
def _scenarios(draw):
    model_count = draw(st.integers(min_value=2, max_value=5))
    names = ["model-{}".format(index) for index in range(model_count)]
    texts = {name: draw(_TEXT) for name in names}
    return {
        "names": names,
        "texts": texts,
        "failing": draw(st.sampled_from(names)),
        "error_message": draw(_TEXT),
        "raise_in_generate": draw(st.booleans()),
        "dead_engine": draw(st.booleans()),
        "failing_path": draw(_PATH),
        "serving_path": draw(_PATH),
    }


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

# Feature: vllm-workflow-latency-optimization, Property 12: Failure isolation preserved under optimization
@settings(max_examples=100, deadline=None)
@given(scenario=_scenarios())
def test_property_12_failure_isolation_preserved_under_optimization(scenario):
    """**Feature: vllm-workflow-latency-optimization, Property 12: Failure
    isolation preserved under optimization**

    **Validates: Requirements 9.6**
    """
    failing = scenario["failing"]
    engines = []
    for name in scenario["names"]:
        if name == failing:
            engine = _FailingEngine(
                scenario["error_message"],
                raise_in_generate=scenario["raise_in_generate"],
                errored=scenario["dead_engine"],
            )
        else:
            engine = _WorkingEngine(scenario["texts"][name])
        engines.append((name, engine))

    with tempfile.TemporaryDirectory() as tmp:
        manager = _loaded_manager(Path(tmp), engines)

        # The injected generation failure surfaces through the EXISTING
        # error type, naming the failing model (R9.6 — no new error
        # surface under the instrumented/optimized paths).
        with pytest.raises(GenerationError) as excinfo:
            _generate(manager, scenario["failing_path"], failing)
        assert excinfo.value.model_name == failing
        assert scenario["error_message"] in excinfo.value.reason

        # Isolation: only the failing model's state may have changed.
        # A healthy engine with a per-request error stays READY; only an
        # engine that reported itself dead is marked FAILED.
        failing_state = manager.state(failing).state
        if scenario["dead_engine"]:
            assert failing_state is ModelState.FAILED
        else:
            assert failing_state is ModelState.READY

        # Every OTHER loaded model remains READY ...
        others = [name for name in scenario["names"] if name != failing]
        for name in others:
            assert manager.state(name).state is ModelState.READY

        # ... and continues serving Generation_Calls successfully, with
        # each model returning exactly its own expected text.
        for name in others:
            text = _generate(manager, scenario["serving_path"], name)
            assert text == scenario["texts"][name]
            assert manager.state(name).state is ModelState.READY
