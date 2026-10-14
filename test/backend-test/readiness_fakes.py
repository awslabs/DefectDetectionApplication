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
"""Triton readiness fakes — the test primitive this repo did not have.

Bugfix: `.kiro/specs/cold-model-first-run-failure/`, task 1.1.

No test anywhere fakes Triton model readiness on a workflow path
(`get_model_status` appears in no executor test), which is why the cold-model
window went unexercised on both run paths. Shaped after
`test/backend-test/vllm_model_reload/fakes.py`.

Two reasons a fake is mandatory rather than convenient:

* the real `TritonEdgeClient` imports `panorama` at module level, which does
  not exist off-device, so the host cannot import it at all;
* `TritonEdgeClient.get_instance()` CREATES the native server when absent,
  and standing Triton up against an empty repository has a documented hang.

`FakeTritonClient` returns a scripted status sequence and records every
`start_triton_model` call, so "kicked exactly once" and "a LOADING model is
never re-kicked" are directly assertable.
"""


#: Triton's own state vocabulary, as `triton_server.cpp::GetModelStatus`
#: reports it. `UNKNOWN` means "this process has never been asked to load
#: this model" — the state a restarted backend sees — and never converges
#: without a load being kicked.
STATE_READY = "READY"
STATE_LOADING = "LOADING"
STATE_UNKNOWN = "UNKNOWN"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_UNLOADING = "UNLOADING"


class FakeTritonClient:
    """A substitute for `TritonEdgeClient` with a scripted status sequence.

    ``statuses`` is consumed one entry per ``get_model_status`` call; the
    last entry repeats once exhausted, so a test can express "LOADING twice
    then READY" as ``[LOADING, LOADING, READY]`` without pinning the exact
    number of polls the implementation makes.

    ``start_raises`` makes the load kick fail, mirroring the real route's
    403 for a model that is already loading.
    """

    def __init__(self, statuses=None, start_status=None, start_raises=None,
                 reason=None):
        self._statuses = list(statuses or [STATE_READY])
        self._start_status = start_status
        self.start_raises = start_raises
        self.reason = reason
        self.status_calls = []
        self.start_calls = []

    # ------------------------------------------------- TritonEdgeClient API

    def get_model_status(self, model_id):
        self.status_calls.append(model_id)
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    def start_triton_model(self, model_id):
        self.start_calls.append(model_id)
        if self.start_raises is not None:
            raise self.start_raises
        if self._start_status is not None:
            # A kick that moves the model on: replace the remaining script.
            self._statuses = [self._start_status]
        return self._statuses[0]

    def list_triton_models(self):
        return [
            {"model_component": model, "status": self._statuses[0]}
            for model in {*self.status_calls, *self.start_calls}
        ]

    # ------------------------------------------------------------- helpers

    @property
    def kick_count(self):
        return len(self.start_calls)

    @property
    def poll_count(self):
        return len(self.status_calls)


class RecordingSleep:
    """A no-op sleep that records what it was asked to wait for, so a test
    can assert the poll interval and the total budget without real time."""

    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)

    @property
    def total(self):
        return sum(self.calls)


class FakeClock:
    """A monotonic clock advanced only by `RecordingSleep`, so budget
    exhaustion is deterministic."""

    def __init__(self, sleep, start=1_000.0):
        self._sleep = sleep
        self._start = start

    def __call__(self):
        return self._start + self._sleep.total
