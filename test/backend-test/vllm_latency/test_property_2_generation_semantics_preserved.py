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
"""Property test for generation semantics under instrumentation (task 2.3).

# Feature: vllm-workflow-latency-optimization, Property 2: Generation semantics preserved under instrumentation

*For any* request against a deterministic fake engine, and *for any* injected
failure in breakdown capture or emission (including none), the instrumented
generate path SHALL return exactly the generated text the pre-feature path
returns, SHALL raise exactly the same error types (ModelUnavailableError,
GenerationError) on the existing failure paths, and SHALL never let a
measurement error escape to the caller.

**Validates: Requirements 1.5, 9.1**

The test drives ``VllmRuntimeManager`` through its injectable
``engine_factory`` / ``sampling_params_factory`` seams (the same fake-engine
harness as ``test/backend-test/vllm_runtime/``), hypothesis-generating:

* request shapes — prompt text, optional system prompt, sampling params,
  model loaded or not, engine yielding N deterministic outputs, raising, or
  yielding nothing;
* injected measurement failures, independently combinable — a final
  ``RequestOutput`` whose measurement-only attributes raise on access, a
  ``build_breakdown`` patched to raise inside the manager module, and a
  hostile monotonic clock (``time.monotonic`` in the manager module raising
  on a drawn pattern of calls).

The oracle is the deterministic fake-engine outcome, computed independently
of any instrumentation: the drawn final text for the success path,
``ModelUnavailableError`` for a never-loaded model, ``GenerationError`` for
an engine failure or an empty output stream. Both ``generate`` (the
delegating, pre-feature-signature path) and ``generate_with_breakdown`` must
match it exactly, fault-injected or not.
"""
import asyncio
import shutil
import tempfile
import time as real_time
from pathlib import Path
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

import vllm_runtime.manager as manager_module
from vllm_runtime.generation_metrics import GenerationPhaseBreakdown
from vllm_runtime.manager import (
    GenerationError,
    ModelState,
    ModelUnavailableError,
    VllmRuntimeManager,
)


# ---------------------------------------------------------------------------
# Deterministic fake engine (adapted from vllm_runtime harness)
# ---------------------------------------------------------------------------

class _RaisingAttrs:
    """Every attribute read raises — hostile measurement introspection."""

    def __getattr__(self, name):
        raise RuntimeError("hostile attribute: {}".format(name))


class _BadLen:
    """``len()`` raises — a token id list that cannot be counted."""

    def __len__(self):
        raise RuntimeError("hostile len")


class _HostileMeasurementOutput:
    """A RequestOutput whose generated text is readable but whose
    measurement-only attributes (``metrics``, ``prompt_token_ids``, …)
    raise on access — a capture-side fault that must stay contained."""

    def __init__(self, text):
        self.outputs = [SimpleNamespace(text=text, token_ids=_BadLen(),
                                        finish_reason=None)]

    def __getattr__(self, name):
        raise RuntimeError("hostile request-output attribute: "
                           "{}".format(name))


class _FakeEngine:
    """Minimal deterministic AsyncLLMEngine surface: records prompts and
    yields the configured outputs (or raises the configured error)."""

    def __init__(self, outputs=None, error=None):
        self._outputs = list(outputs or [])
        self._error = error
        self.calls = []

    def generate(self, prompt, sampling_params, request_id):
        self.calls.append(prompt)
        return self._stream()

    async def _stream(self):
        if self._error is not None:
            raise self._error
        for output in self._outputs:
            yield output


class _FlakyClock:
    """``time.monotonic`` stand-in raising on a drawn call pattern."""

    def __init__(self, fail_pattern):
        self._pattern = list(fail_pattern)
        self._calls = 0

    def monotonic(self):
        fail = self._pattern[self._calls % len(self._pattern)]
        self._calls += 1
        if fail:
            raise RuntimeError("hostile monotonic clock")
        return real_time.monotonic()


def _raising_build_breakdown(**_kwargs):
    raise RuntimeError("injected breakdown-construction failure")


def _stage_repository(model_dir: Path, model_name: str) -> None:
    version_dir = model_dir / model_name / "1"
    version_dir.mkdir(parents=True)
    (model_dir / model_name / "config.pbtxt").write_text('backend: "vllm"\n')
    (version_dir / "model.json").write_text("{}")


def _loaded_manager(model_dir: Path, model_name: str,
                    engine: _FakeEngine) -> VllmRuntimeManager:
    manager = VllmRuntimeManager(
        model_dir=model_dir,
        engine_factory=lambda engine_args: engine,
        sampling_params_factory=dict,
    )
    _stage_repository(model_dir, model_name)
    status = asyncio.run(manager.load(model_name))
    assert status.state is ModelState.READY
    return manager


def _unloaded_manager(model_dir: Path) -> VllmRuntimeManager:
    return VllmRuntimeManager(
        model_dir=model_dir,
        engine_factory=lambda engine_args: _FakeEngine(),
        sampling_params_factory=dict,
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_PROMPT = st.text(min_size=0, max_size=30)
_SYSTEM_PROMPT = st.one_of(st.none(), st.text(min_size=1, max_size=15))
_SAMPLING = st.one_of(
    st.none(),
    st.dictionaries(
        st.sampled_from(["max_tokens", "temperature", "top_p", "seed"]),
        st.one_of(st.integers(min_value=0, max_value=512),
                  st.floats(min_value=0.0, max_value=2.0,
                            allow_nan=False)),
        max_size=3,
    ),
)

_TOKEN_LIST = st.lists(st.integers(min_value=0, max_value=99), max_size=20)


@st.composite
def _final_outputs(draw, text):
    """The final RequestOutput carrying the deterministic text — normal,
    partially hostile fields, or wholly hostile measurement attributes."""
    kind = draw(st.sampled_from(["normal", "normal", "hostile_fields",
                                 "hostile_object"]))
    if kind == "hostile_object":
        return _HostileMeasurementOutput(text)
    if kind == "hostile_fields":
        return SimpleNamespace(
            prompt_token_ids=_BadLen(),
            outputs=[SimpleNamespace(text=text, token_ids=_BadLen(),
                                     finish_reason=123)],
            metrics=_RaisingAttrs(),
        )
    return SimpleNamespace(
        prompt_token_ids=draw(_TOKEN_LIST),
        outputs=[SimpleNamespace(text=text,
                                 token_ids=draw(_TOKEN_LIST),
                                 finish_reason=draw(st.sampled_from(
                                     ["stop", "length", None])))],
        metrics=None,
    )


@st.composite
def _cases(draw):
    scenario = draw(st.sampled_from(
        ["ok", "ok", "ok", "engine_error", "empty_stream", "not_loaded"]))
    final_text = draw(st.text(min_size=1, max_size=40))
    n_outputs = draw(st.integers(min_value=1, max_value=4))
    outputs = []
    if scenario == "ok":
        for i in range(n_outputs - 1):
            partial = final_text[: max(1, (i + 1) * len(final_text)
                                       // n_outputs)]
            outputs.append(SimpleNamespace(
                prompt_token_ids=None,
                outputs=[SimpleNamespace(text=partial, token_ids=None,
                                         finish_reason=None)],
                metrics=None,
            ))
        outputs.append(draw(_final_outputs(final_text)))
    return {
        "scenario": scenario,
        "final_text": final_text,
        "outputs": outputs,
        "engine_error_message": draw(st.text(min_size=1, max_size=20)),
        "prompt": draw(_PROMPT),
        "system_prompt": draw(_SYSTEM_PROMPT),
        "sampling_params": draw(_SAMPLING),
        # Injected measurement faults — independently combinable, and the
        # all-False draw is the "including none" case.
        "patch_build_breakdown": draw(st.booleans()),
        "hostile_clock": draw(st.booleans()),
        "clock_fail_pattern": draw(st.lists(st.booleans(), min_size=1,
                                            max_size=6).map(
            lambda pattern: pattern if any(pattern) else [True])),
    }


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

def _call(coro):
    """Run a manager coroutine; return ('ok', value) or ('err', exc)."""
    try:
        return "ok", asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001 - the property inspects the type
        return "err", exc


# Feature: vllm-workflow-latency-optimization, Property 2: Generation semantics preserved under instrumentation
@settings(max_examples=100, deadline=None)
@given(case=_cases())
def test_property_2_generation_semantics_preserved(case):
    """**Feature: vllm-workflow-latency-optimization, Property 2:
    Generation semantics preserved under instrumentation**

    **Validates: Requirements 1.5, 9.1**
    """
    tmp_a = Path(tempfile.mkdtemp())
    tmp_b = Path(tempfile.mkdtemp())
    saved_build = manager_module.build_breakdown
    saved_time = manager_module.time
    try:
        # Two identically configured deterministic engines/managers: one
        # driven through generate (the delegating pre-feature-signature
        # path), one through generate_with_breakdown.
        if case["scenario"] == "not_loaded":
            manager_a = _unloaded_manager(tmp_a)
            manager_b = _unloaded_manager(tmp_b)
            engine_a = engine_b = None
        else:
            error = (RuntimeError(case["engine_error_message"])
                     if case["scenario"] == "engine_error" else None)
            engine_a = _FakeEngine(outputs=list(case["outputs"]),
                                   error=error)
            engine_b = _FakeEngine(outputs=list(case["outputs"]),
                                   error=error)
            manager_a = _loaded_manager(tmp_a, "model-a", engine_a)
            manager_b = _loaded_manager(tmp_b, "model-b", engine_b)

        # Injected capture/emission failures — applied AFTER load so the
        # faults hit exactly the instrumented generate path.
        if case["patch_build_breakdown"]:
            manager_module.build_breakdown = _raising_build_breakdown
        if case["hostile_clock"]:
            manager_module.time = _FlakyClock(case["clock_fail_pattern"])

        model_a = "nope" if case["scenario"] == "not_loaded" else "model-a"
        model_b = "nope" if case["scenario"] == "not_loaded" else "model-b"
        kwargs = {
            "sampling_params": case["sampling_params"],
            "system_prompt": case["system_prompt"],
        }
        status_a, value_a = _call(
            manager_a.generate(model_a, case["prompt"], **kwargs))
        status_b, value_b = _call(
            manager_b.generate_with_breakdown(model_b, case["prompt"],
                                              **kwargs))
    finally:
        manager_module.build_breakdown = saved_build
        manager_module.time = saved_time
        shutil.rmtree(tmp_a, ignore_errors=True)
        shutil.rmtree(tmp_b, ignore_errors=True)

    if case["scenario"] == "ok":
        # R9.1: identical generated text on both paths, equal to the
        # deterministic fake-engine text; R1.5: no measurement error
        # escaped, and the breakdown is None or a well-typed breakdown.
        assert status_a == "ok", "generate raised: {!r}".format(value_a)
        assert status_b == "ok", (
            "generate_with_breakdown raised: {!r}".format(value_b))
        assert value_a == case["final_text"]
        text_b, breakdown = value_b
        assert text_b == case["final_text"]
        assert value_a == text_b
        assert breakdown is None or isinstance(breakdown,
                                               GenerationPhaseBreakdown)
        # Both engines saw the identical engine prompt.
        assert engine_a.calls == engine_b.calls
        assert len(engine_a.calls) == 1
    elif case["scenario"] == "not_loaded":
        # R9.1: exactly the existing error type on the not-READY path.
        assert status_a == "err" and status_b == "err"
        assert type(value_a) is ModelUnavailableError
        assert type(value_b) is ModelUnavailableError
    else:
        # engine_error / empty_stream — R9.1: exactly the existing
        # GenerationError, naming the failing model; injected measurement
        # faults never replace or mask it (R1.5).
        assert status_a == "err" and status_b == "err"
        assert type(value_a) is GenerationError
        assert type(value_b) is GenerationError
        assert value_a.model_name == model_a
        assert value_b.model_name == model_b
        if case["scenario"] == "engine_error":
            assert case["engine_error_message"] in value_a.reason
            assert case["engine_error_message"] in value_b.reason
