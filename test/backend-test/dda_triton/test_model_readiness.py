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
"""Units for the Triton model readiness gate.

Bugfix: `.kiro/specs/cold-model-first-run-failure/`, task 2.2. One case per
state in design.md Decision 2's table, plus the traps that make a naive
"poll until READY" loop wrong:

* `UNKNOWN` never converges on its own, so exactly ONE load is kicked;
* a `LOADING` model must never be re-kicked (the real route 403s unless the
  state is `UNKNOWN`/`UNAVAILABLE`);
* `UNAVAILABLE` is terminal and carries Triton's own `reason`;
* an empty model repository must not even construct the client, because
  `get_instance()` creates the native server and standing Triton up against
  an empty repo has a documented hang.

_Requirements: 2.11, 2.12, 2.13, 2.15_
"""
import pytest

from dda_triton import model_readiness
from dda_triton.model_readiness import ensure_model_ready
from readiness_fakes import (
    STATE_LOADING,
    STATE_READY,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    STATE_UNLOADING,
    FakeClock,
    FakeTritonClient,
    RecordingSleep,
)


MODEL = "blue-plate-rfdetr-small"
RESOLVED = "model-blue-plate-rfdetr-small-jetson-xavier-jp7"


@pytest.fixture(autouse=True)
def repo_has_models(monkeypatch):
    """Default: the repository holds models, so the gate runs. The empty-repo
    case overrides this explicitly."""
    monkeypatch.setattr(model_readiness, "_repo_has_models", lambda: True)


def call(client, **kwargs):
    sleep = RecordingSleep()
    outcome = ensure_model_ready(
        MODEL,
        RESOLVED,
        client=client,
        sleep=sleep,
        clock=FakeClock(sleep),
        **kwargs,
    )
    return outcome, sleep


# ------------------------------------------------------------- the warm path

def test_ready_returns_immediately_without_sleeping_or_kicking():
    """The warm path must pay nothing: no wait, no load request.
    _Requirements: 2.11_"""
    client = FakeTritonClient([STATE_READY])

    outcome, sleep = call(client)

    assert outcome.ready is True
    assert outcome.state == STATE_READY
    assert outcome.message is None
    assert sleep.calls == []
    assert client.kick_count == 0
    assert client.poll_count == 1


# ------------------------------------------------------------ LOADING waits

def test_loading_waits_and_never_re_kicks():
    """A load already in flight is waited on, never requested again — the
    real route 403s for exactly this. _Requirements: 2.12_"""
    client = FakeTritonClient([STATE_LOADING, STATE_LOADING, STATE_READY])

    outcome, sleep = call(client)

    assert outcome.ready is True
    assert client.kick_count == 0, (
        "a LOADING model must never be re-kicked"
    )
    assert len(sleep.calls) == 2
    assert outcome.waited_seconds > 0


def test_loading_that_never_becomes_ready_times_out_with_a_named_reason():
    """Budget exhaustion names the model and the state, never the generic
    pipeline error. _Requirements: 2.11, 2.13_"""
    client = FakeTritonClient([STATE_LOADING])

    outcome, sleep = call(client, timeout_s=9.0, poll_interval_s=3.0)

    assert outcome.ready is False
    assert outcome.state == STATE_LOADING
    assert MODEL in outcome.message
    assert RESOLVED in outcome.message
    assert STATE_LOADING in outcome.message
    assert "Pipeline failed to change state to PLAYING" not in outcome.message
    assert sleep.total >= 9.0
    assert client.kick_count == 0


# ------------------------------------------------------------ UNKNOWN kicks

def test_unknown_kicks_exactly_one_load_then_waits():
    """`UNKNOWN` is what a restarted backend sees; a pure wait would never
    converge. _Requirements: 2.12_"""
    client = FakeTritonClient(
        [STATE_UNKNOWN, STATE_UNKNOWN, STATE_LOADING, STATE_READY]
    )

    outcome, _sleep = call(client)

    assert outcome.ready is True
    assert client.kick_count == 1, (
        f"expected exactly one load request, got {client.start_calls}"
    )
    assert client.start_calls == [RESOLVED]


def test_a_refused_load_request_keeps_waiting():
    """The kick races another loader and is refused (the route's 403); the
    other loader is making progress, so keep waiting rather than fail.
    _Requirements: 2.12_"""
    client = FakeTritonClient(
        [STATE_UNKNOWN, STATE_LOADING, STATE_READY],
        start_raises=RuntimeError("403 model is already loading"),
    )

    outcome, _sleep = call(client)

    assert outcome.ready is True
    assert client.kick_count == 1


# --------------------------------------------------------- terminal states

def test_unavailable_fails_fast_and_surfaces_tritons_reason():
    """Terminal: no wait at all, and the `reason` commit 7812407 added is
    carried through. _Requirements: 2.12_"""
    client = FakeTritonClient([STATE_UNAVAILABLE])
    client.list_triton_models = lambda: [
        {"model_component": RESOLVED, "status": STATE_UNAVAILABLE,
         "reason": "failed to load: unsupported opset"}
    ]

    outcome, sleep = call(client)

    assert outcome.ready is False
    assert outcome.state == STATE_UNAVAILABLE
    assert "unsupported opset" in outcome.message
    assert "redeploy" in outcome.message.lower()
    assert sleep.calls == [], "a terminal state must not be waited on"
    assert client.kick_count == 0


def test_unloading_fails_fast():
    """Waiting for a model on its way out is wrong. _Requirements: 2.12_"""
    client = FakeTritonClient([STATE_UNLOADING])

    outcome, sleep = call(client)

    assert outcome.ready is False
    assert outcome.state == STATE_UNLOADING
    assert "unloaded" in outcome.message.lower()
    assert sleep.calls == []


# --------------------------------------------------------- the empty repo

def test_an_empty_repository_no_ops_without_constructing_the_client(
    monkeypatch,
):
    """`get_instance()` creates the native server, and standing Triton up
    against an empty repo hangs — so the guard must run FIRST and the client
    must never be built. _Requirements: 2.15_"""
    monkeypatch.setattr(model_readiness, "_repo_has_models", lambda: False)

    constructed = []

    def exploding_client():
        constructed.append(True)
        raise AssertionError("the client must not be constructed")

    monkeypatch.setattr(model_readiness, "_client", exploding_client)

    outcome = ensure_model_ready(MODEL, RESOLVED)

    assert outcome.ready is True
    assert constructed == []


# ------------------------------------------------------- failing open

def test_an_unreadable_state_fails_open():
    """The gate must never be the sole reason a previously working run stops
    working: if state cannot be READ, proceed and let the pipeline behave
    exactly as it does today."""
    class Exploding:
        def get_model_status(self, _model):
            raise RuntimeError("native call blew up")

    outcome, _sleep = call(Exploding())

    assert outcome.ready is True
    assert outcome.message is None


def test_a_client_that_cannot_be_built_fails_open(monkeypatch):
    def exploding_client():
        raise RuntimeError("no panorama module")

    monkeypatch.setattr(model_readiness, "_client", exploding_client)

    outcome = ensure_model_ready(MODEL, RESOLVED)

    assert outcome.ready is True


# ------------------------------------------------------------ name handling

def test_the_resolved_name_is_what_is_polled_and_kicked():
    """The workflow's registry name appears in the message; the deployed
    Triton name is what Triton is asked about."""
    client = FakeTritonClient([STATE_UNKNOWN, STATE_LOADING])

    outcome, _sleep = call(client, timeout_s=3.0, poll_interval_s=3.0)

    assert client.status_calls[0] == RESOLVED
    assert client.start_calls == [RESOLVED]
    assert MODEL in outcome.message


def test_resolved_name_defaults_to_the_model_name():
    client = FakeTritonClient([STATE_READY])

    ensure_model_ready(MODEL, None, client=client)

    assert client.status_calls == [MODEL]
