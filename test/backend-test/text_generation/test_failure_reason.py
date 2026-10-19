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
"""Text-generation failure reason (localserver-input-handling-hardening Item 4).

``_failure_reason`` returns the backend contract reason only for a
``GenerationError`` with a truthy ``.reason``; for any other error it returns a
fixed string and logs a traceback. The task-1 cases assert that fixed behavior
and FAIL on the unfixed tree (which returns the error's ``.reason`` attribute
or ``str(error)``); the task-2 cases pin the preserved ``GenerationError`` path
and the 502/SSE shapes and PASS on the unfixed tree. Host.
"""
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from endpoints import text_generation
from utils.auth import authorize_request
from vllm_runtime.manager import GenerationError, ModelState

_FIXED = "internal error; see the LocalServer log"
_MODEL = "qwen2-vl"


class _HasReason(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _CollectingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _collect:
    def __init__(self, logger):
        self._logger = logger
        self.handler = _CollectingHandler()

    def __enter__(self):
        self._logger.addHandler(self.handler)
        return self.handler

    def __exit__(self, *exc):
        self._logger.removeHandler(self.handler)
        return False


class TestItem4FailureReason:
    """task 1: a non-GenerationError returns the fixed string, logged."""

    def test_a_plain_error_returns_the_fixed_string(self):
        # FAILS unfixed: returns "boom".
        assert text_generation._failure_reason(ValueError("boom")) == _FIXED

    def test_an_error_carrying_a_reason_attribute_returns_the_fixed_string(self):
        # FAILS unfixed: returns the .reason attribute.
        assert text_generation._failure_reason(_HasReason("attr-reason")) == _FIXED

    @settings(max_examples=50, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(payload=st.one_of(
        st.builds(lambda t: GenerationError(_MODEL, t), st.text(min_size=1)),
        st.builds(ValueError, st.text()),
        st.builds(RuntimeError, st.text()),
        st.builds(_HasReason, st.text(min_size=1)),
        st.builds(lambda t: UnicodeDecodeError("utf-8", b"\xff", 0, 1, t),
                  st.text(min_size=1)),
    ))
    @example(payload=ValueError("boom"))
    def test_property_5_reason_contract(self, payload):
        # Returns e.reason iff GenerationError with a truthy reason; else the
        # fixed string and exactly one ERROR record carrying the error.
        with _collect(text_generation.logger) as handler:
            result = text_generation._failure_reason(payload)
        if isinstance(payload, GenerationError) and payload.reason:
            assert result == payload.reason
            assert handler.records == []
        else:
            assert result == _FIXED
            errors = [r for r in handler.records if r.levelno == logging.ERROR]
            assert len(errors) == 1
            assert errors[0].exc_info[1] is payload


class _ReadyStatus:
    state = ModelState.READY
    reason = None


class _FailingRuntime:
    """READY runtime whose generate paths raise a GenerationError(reason)."""

    def __init__(self, reason):
        self._reason = reason

    def state(self, model_name):
        return _ReadyStatus()

    def engine_args(self, model_name):
        return {"max_model_len": 4096}

    async def generate(self, model_name, prompt, sampling_params, **kwargs):
        raise GenerationError(model_name, self._reason)

    async def generate_stream(self, model_name, prompt, sampling_params, **kwargs):
        raise GenerationError(model_name, self._reason)
        yield  # pragma: no cover - makes this an async generator


def _client(runtime):
    app = FastAPI()
    app.include_router(text_generation.router)
    app.dependency_overrides[text_generation.get_runtime] = lambda: runtime
    app.dependency_overrides[authorize_request] = lambda: None
    return TestClient(app)


class TestItem4Preservation:
    """task 2: a GenerationError reason is returned, 502/SSE shapes unchanged."""

    def test_a_generation_error_reason_is_returned(self):
        assert text_generation._failure_reason(
            GenerationError(_MODEL, "oom")) == "oom"

    def test_the_502_body_shape_is_unchanged(self):
        response = _client(_FailingRuntime("oom")).post(
            "/text-generation/{0}/generate".format(_MODEL), json={"prompt": "hi"})
        assert response.status_code == 502
        assert response.json() == {"model_name": _MODEL, "reason": "oom"}

    def test_the_sse_error_event_shape_is_unchanged(self):
        with _client(_FailingRuntime("oom")).stream(
            "POST", "/text-generation/{0}/generate-stream".format(_MODEL),
            json={"prompt": "hi"},
        ) as response:
            body = "".join(chunk for chunk in response.iter_text())
        assert body == 'data: {}\n\n'.format(json.dumps({"error": {"reason": "oom"}}))
