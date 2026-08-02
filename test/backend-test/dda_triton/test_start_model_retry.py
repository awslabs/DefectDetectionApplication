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
"""Hardening tests for start_model: readiness verification, bounded retry, and
per-model failure isolation."""
import json

import pytest

import dda_triton.model_convertor as mc


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


def _make_fake_requests(start_status, states):
    """Build a fake requests.get.

    ``start_status`` is the HTTP status returned by the /start endpoint.
    ``states`` is a mutable list; each /feature-configurations poll pops the next
    state (the last value repeats once exhausted).
    """
    calls = {"start": 0, "list": 0}

    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/start"):
            calls["start"] += 1
            return _Resp(status_code=start_status, text="ok")
        # /feature-configurations
        calls["list"] += 1
        state = states.pop(0) if len(states) > 1 else states[0]
        payload = [{"type": "TritonModel", "modelName": "model-x", "status": state}]
        return _Resp(status_code=200, payload=payload)

    return fake_get, calls


@pytest.fixture(autouse=True)
def _no_sleep_no_wait(monkeypatch):
    monkeypatch.setattr(mc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mc, "wait_for_server", lambda *a, **k: True)


def test_start_model_succeeds_after_loading_then_ready(monkeypatch):
    # Poll sees LOADING, LOADING, then READY.
    fake_get, calls = _make_fake_requests(200, ["LOADING", "LOADING", "READY"])
    monkeypatch.setattr(mc.requests, "get", fake_get)

    ok = mc.start_model("model-x", ready_timeout=30, backoff=0)

    assert ok is True
    assert calls["start"] == 1  # one start was enough; readiness confirmed by polling


def test_start_model_already_ready_short_circuits(monkeypatch):
    fake_get, calls = _make_fake_requests(200, ["READY"])
    monkeypatch.setattr(mc.requests, "get", fake_get)

    ok = mc.start_model("model-x", ready_timeout=30, backoff=0)

    assert ok is True
    assert calls["start"] == 0  # already READY => never issued /start


def test_start_model_retries_then_gives_up_without_raising(monkeypatch):
    # Never reaches READY: bounded retries, returns False, no exception.
    fake_get, calls = _make_fake_requests(200, ["LOADING"])
    monkeypatch.setattr(mc.requests, "get", fake_get)

    ok = mc.start_model("model-x", max_attempts=3, ready_timeout=0, backoff=0)

    assert ok is False
    assert calls["start"] == 3  # exhausted the attempt budget


def test_start_model_terminal_failure_short_circuits_wait(monkeypatch):
    # UNAVAILABLE is terminal => each attempt's wait returns fast, still retried.
    fake_get, calls = _make_fake_requests(200, ["UNAVAILABLE"])
    monkeypatch.setattr(mc.requests, "get", fake_get)

    ok = mc.start_model("model-x", max_attempts=2, ready_timeout=30, backoff=0)

    assert ok is False
    assert calls["start"] == 2


def test_start_model_handles_403_as_in_flight(monkeypatch):
    # 403 (not startable) but model is loading and becomes READY => success.
    fake_get, calls = _make_fake_requests(403, ["LOADING", "READY"])
    monkeypatch.setattr(mc.requests, "get", fake_get)

    ok = mc.start_model("model-x", ready_timeout=30, backoff=0)

    assert ok is True


def test_start_model_connection_error_is_contained(monkeypatch):
    def boom_get(url, headers=None, timeout=None):
        raise ConnectionError("simulated connection reset")

    monkeypatch.setattr(mc.requests, "get", boom_get)

    # Must not raise; returns False after bounded retries.
    ok = mc.start_model("model-x", max_attempts=2, ready_timeout=0, backoff=0)
    assert ok is False


def test_one_model_failure_does_not_affect_sibling(monkeypatch):
    """A failed start for one model does not prevent a sibling from succeeding."""
    def fake_get(url, headers=None, timeout=None):
        # /feature-configurations reports model-good READY, model-bad LOADING.
        if url.endswith("/start"):
            return _Resp(status_code=200, text="ok")
        payload = [
            {"type": "TritonModel", "modelName": "model-good", "status": "READY"},
            {"type": "TritonModel", "modelName": "model-bad", "status": "LOADING"},
        ]
        return _Resp(status_code=200, payload=payload)

    monkeypatch.setattr(mc.requests, "get", fake_get)

    bad = mc.start_model("model-bad", max_attempts=2, ready_timeout=0, backoff=0)
    good = mc.start_model("model-good", max_attempts=2, ready_timeout=5, backoff=0)

    assert bad is False
    assert good is True
