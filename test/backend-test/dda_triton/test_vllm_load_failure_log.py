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
"""Unit tests for the vLLM load-failure log (spec:
vllm-sizing-and-packaging-errors, task 7.1, Requirements 4.1-4.4, 6.2):
reason extraction from the Triton ``{"error": "..."}`` body, the single
prominent ERROR line, the KV-cache remediation hint, and the staged
engine args flowing from ``prepare`` into the failure log."""
import argparse
import json
import logging

import pytest
import requests

import dda_triton.vllm_model_prep as mp

# A real-world Triton 409 body for the motivating failure (negative
# KV-cache budget on model-vllm-qwen-2-5-7b).
KV_CACHE_409_BODY = json.dumps(
    {
        "error": "load failed for model 'model-vllm-qwen-2-5-7b': version 1 "
        "is at UNAVAILABLE state: Internal: ValueError: No available memory "
        "for the cache blocks. Try increasing `gpu_memory_utilization` when "
        "initializing the engine.;"
    }
)


# --- extract_load_failure_reason ---------------------------------------------


def test_extracts_error_field_from_json_object():
    assert (
        mp.extract_load_failure_reason('{"error": "engine exploded"}')
        == "engine exploded"
    )


def test_falls_back_to_raw_text_for_non_json():
    assert mp.extract_load_failure_reason("  plain text failure \n") == (
        "plain text failure"
    )


def test_falls_back_to_raw_text_for_json_without_error():
    body = '{"status": "FAILED"}'
    assert mp.extract_load_failure_reason(body) == body


def test_falls_back_to_raw_text_for_json_non_object():
    assert mp.extract_load_failure_reason('["error"]') == '["error"]'


# --- log_load_failure ---------------------------------------------------------


def _error_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


class TestLogLoadFailure:
    """Class-based (the repo conftest's caplog override binds to request.cls)."""

    def test_prominent_line_carries_model_status_and_reason(self, caplog):
        caplog = self.caplog
        with caplog.at_level(logging.ERROR):
            mp.log_load_failure("model-x", 409, '{"error": "boom"}')
        errors = _error_lines(caplog)
        assert len(errors) == 1
        line = errors[0]
        assert "model 'model-x'" in line
        assert "HTTP 409" in line
        assert "boom" in line

    def test_kv_cache_reason_appends_raise_remediation(self, caplog):
        caplog = self.caplog
        with caplog.at_level(logging.ERROR):
            mp.log_load_failure(
                "model-vllm-qwen-2-5-7b",
                409,
                KV_CACHE_409_BODY,
                {"gpu_memory_utilization": 0.3, "max_model_len": 8192},
            )
        line = _error_lines(caplog)[0]
        assert "RAISE" in line
        assert "gpu_memory_utilization" in line
        assert "max_model_len" in line
        # Requirement 4.4: the staged values themselves are in the output.
        assert "gpu_memory_utilization=0.3" in line
        assert "max_model_len=8192" in line

    def test_non_matching_reason_gets_no_remediation_hint(self, caplog):
        caplog = self.caplog
        with caplog.at_level(logging.ERROR):
            mp.log_load_failure("model-x", 400, '{"error": "unknown model"}')
        line = _error_lines(caplog)[0]
        assert "RAISE" not in line
        assert "Remediation" not in line

    def test_gpu_memory_utilization_marker_alone_triggers_hint(self, caplog):
        # Requirement 4.2: the second KV-cache marker matches even without
        # the "No available memory for the cache blocks" phrasing.
        caplog = self.caplog
        with caplog.at_level(logging.ERROR):
            mp.log_load_failure(
                "model-x",
                409,
                '{"error": "engine init failed: try increasing '
                "`gpu_memory_utilization` when initializing the engine\"}",
            )
        line = _error_lines(caplog)[0]
        assert "RAISE" in line
        assert "Remediation" in line

    def test_engine_args_logged_even_without_remediation_hint(self, caplog):
        # Requirement 4.4: the staged engine args appear in the failure log
        # for ANY authoritative HTTP error, not only KV-cache failures.
        caplog = self.caplog
        with caplog.at_level(logging.ERROR):
            mp.log_load_failure(
                "model-x",
                400,
                '{"error": "unknown model"}',
                {"gpu_memory_utilization": 0.5, "max_model_len": 4096},
            )
        line = _error_lines(caplog)[0]
        assert "RAISE" not in line
        assert "gpu_memory_utilization=0.5" in line
        assert "max_model_len=4096" in line

    def test_raw_body_stays_available_at_debug(self, caplog):
        caplog = self.caplog
        with caplog.at_level(logging.DEBUG):
            mp.log_load_failure("model-x", 409, KV_CACHE_409_BODY)
        debugs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert any(KV_CACHE_409_BODY in m for m in debugs)


# --- request_load wiring --------------------------------------------------------


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class TestRequestLoadHttpError:
    # INTENTIONAL GOLDEN UPDATE (vllm-restart-model-recovery): a KV-cache
    # OOM failure no longer stays single-attempt. The failed load leaves
    # its GPU allocations pinned in the runtime, so every plain retry
    # keeps OOMing; the validated on-device recovery (ryan-orin-nano/JP6)
    # is unload -> reload, run exactly once. Non-KV HTTP errors keep the
    # original single-attempt semantics (asserted below and in the
    # deploy_reliability preservation suite).

    def test_kv_cache_failure_triggers_one_unload_reload_cycle(
            self, monkeypatch, caplog):
        caplog = self.caplog
        monkeypatch.setattr(mp, "wait_for_server", lambda *a, **k: True)
        urls = []

        def scripted_post(url, timeout=None):
            urls.append(url)
            return _Resp(409, KV_CACHE_409_BODY)

        monkeypatch.setattr(mp.requests, "post", scripted_post)
        with caplog.at_level(logging.ERROR):
            outcome = mp.request_load(
                "model-vllm-qwen-2-5-7b",
                {"gpu_memory_utilization": 0.3, "max_model_len": 4096},
            )
        # The retry failing too is authoritative: LOAD_HTTP_ERROR, and the
        # cycle ran exactly once (load, unload, load - no further posts).
        assert outcome == mp.LOAD_HTTP_ERROR
        assert [u.rsplit("/", 1)[-1] for u in urls] == [
            "load", "unload", "load"]
        # The prominent failure line (format unchanged) appears for each
        # load attempt, still carrying the remediation hint + staged args.
        lines = [line for line in _error_lines(caplog)
                 if "FAILED to load" in line]
        assert len(lines) == 2
        for line in lines:
            assert "model 'model-vllm-qwen-2-5-7b'" in line
            assert "HTTP 409" in line
            assert "No available memory for the cache blocks" in line
            assert "RAISE" in line
            assert "gpu_memory_utilization=0.3" in line

    def test_kv_cache_recovery_reload_success_returns_ok(
            self, monkeypatch, caplog):
        caplog = self.caplog
        monkeypatch.setattr(mp, "wait_for_server", lambda *a, **k: True)
        urls = []

        def scripted_post(url, timeout=None):
            urls.append(url)
            if url.endswith("/unload"):
                return _Resp(200)
            if len([u for u in urls if u.endswith("/load")]) == 1:
                return _Resp(409, KV_CACHE_409_BODY)
            return _Resp(200)

        monkeypatch.setattr(mp.requests, "post", scripted_post)
        with caplog.at_level(logging.ERROR):
            outcome = mp.request_load(
                "model-vllm-qwen-2-5-7b",
                {"gpu_memory_utilization": 0.3, "max_model_len": 4096},
            )
        assert outcome == mp.LOAD_OK
        assert [u.rsplit("/", 1)[-1] for u in urls] == [
            "load", "unload", "load"]

    def test_non_kv_http_error_stays_single_attempt(
            self, monkeypatch, caplog):
        caplog = self.caplog
        monkeypatch.setattr(mp, "wait_for_server", lambda *a, **k: True)
        urls = []

        def scripted_post(url, timeout=None):
            urls.append(url)
            return _Resp(400, '{"error": "unknown model"}')

        monkeypatch.setattr(mp.requests, "post", scripted_post)
        with caplog.at_level(logging.ERROR):
            outcome = mp.request_load("model-x")
        assert outcome == mp.LOAD_HTTP_ERROR
        assert [u.rsplit("/", 1)[-1] for u in urls] == ["load"]
        errors = _error_lines(caplog)
        assert len(errors) == 1
        assert "model 'model-x'" in errors[0]
        assert "HTTP 400" in errors[0]


def test_request_load_200_unchanged(monkeypatch):
    monkeypatch.setattr(mp, "wait_for_server", lambda *a, **k: True)
    monkeypatch.setattr(mp.requests, "post", lambda url, timeout=None: _Resp(200))
    assert mp.request_load("model-x") == mp.LOAD_OK


def test_request_load_unreachable_unchanged(monkeypatch):
    monkeypatch.setattr(mp, "wait_for_server", lambda *a, **k: False)
    assert mp.request_load("model-x") == mp.LOAD_UNREACHABLE


# --- prepare passes the staged engine args ---------------------------------------


def _make_repo(tmp_path, model_name, engine_args):
    model_dir = tmp_path / model_name
    (model_dir / "1").mkdir(parents=True)
    (model_dir / "config.pbtxt").write_text('backend: "vllm"\n', encoding="utf-8")
    (model_dir / "1" / "model.json").write_text(
        json.dumps(engine_args), encoding="utf-8"
    )
    return tmp_path


def _args(**kwargs):
    ns = argparse.Namespace(
        unarchived_repo_path=None,
        weights_path=None,
        model_name=None,
        component_name=None,
        cleanup=False,
    )
    for key, value in kwargs.items():
        setattr(ns, key, value)
    return ns


def test_prepare_passes_staged_engine_args_hf(monkeypatch, tmp_path):
    engine_args = {
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "gpu_memory_utilization": 0.3,
        "max_model_len": 8192,
    }
    repo = _make_repo(tmp_path, "model-x", engine_args)
    monkeypatch.setattr(
        mp, "stage_repository", lambda *a, **k: str(tmp_path / "staged")
    )
    seen = {}

    def fake_request_load(model_name, engine_args=None):
        seen["model_name"] = model_name
        seen["engine_args"] = engine_args
        return mp.LOAD_OK

    monkeypatch.setattr(mp, "request_load", fake_request_load)

    rc = mp.prepare(_args(unarchived_repo_path=str(repo), model_name="model-x"))

    assert rc == 0
    assert seen["model_name"] == "model-x"
    assert seen["engine_args"] == engine_args


def test_prepare_passes_rewritten_engine_args_s3(monkeypatch, tmp_path):
    engine_args = {
        "model": mp.WEIGHTS_SENTINEL,
        "gpu_memory_utilization": 0.5,
        "max_model_len": 4096,
    }
    repo = _make_repo(tmp_path, "model-y", engine_args)
    weights = tmp_path / "weights"
    weights.mkdir()
    monkeypatch.setattr(
        mp, "stage_repository", lambda *a, **k: str(tmp_path / "staged")
    )
    seen = {}

    def fake_request_load(model_name, engine_args=None):
        seen["engine_args"] = engine_args
        return mp.LOAD_OK

    monkeypatch.setattr(mp, "request_load", fake_request_load)

    rc = mp.prepare(
        _args(
            unarchived_repo_path=str(repo),
            model_name="model-y",
            weights_path=str(weights),
        )
    )

    assert rc == 0
    # The rewritten (staged) args carry the sizing settings and the
    # resolved weights path.
    assert seen["engine_args"]["gpu_memory_utilization"] == 0.5
    assert seen["engine_args"]["max_model_len"] == 4096
    assert seen["engine_args"]["model"] == str(weights)
