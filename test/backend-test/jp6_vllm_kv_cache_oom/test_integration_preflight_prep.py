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
"""Host integration tests for the device half of
jp6-vllm-kv-cache-oom-regression (task 4.8, design "Integration Tests").

End to end through the REAL prep pipeline: ``prepare()`` validates the
unarchived Triton_vLLM_Repository, stages it with the REAL
``vllm_model_prep.stage_repository`` (the atomic temp-sibling copy +
rename, redirected into a tmp directory because the production
``/aws_dda`` root is not writable on this host — the function itself is
untouched), then drives ``request_load`` against a monkeypatched
``requests`` layer serving crafted Triton model-control 409 bodies:

- a preflight-refusal body (reason carrying ``preflight-refused:`` — and,
  deliberately, the string ``gpu_memory_utilization``, which the KV
  markers would otherwise match) is classified ``LOAD_PREFLIGHT_REFUSED``
  after exactly ONE load request, the prominent ERROR line carries the
  full diagnostic, and ``prepare()`` exits **0** (2.9) — while the staged
  repository is byte-identical to the source (verbatim staging, 3.8);
- a genuine KV-OOM body (the device's verbatim reason, WITHOUT the
  marker) still fires the single unload -> reload recovery — ``load,
  unload, load`` — exactly as before the fix, whether the retry succeeds
  (exit 0) or fails authoritatively (exit 1, never a second recovery)
  (3.8).

HONESTY GUARD (design "Honesty Guard", binding): this file proves the
prep's classification, log lines and exit codes over a monkeypatched HTTP
layer and a tmp-dir staging root only. Nothing here contacts a runtime,
loads a vLLM engine, allocates GPU memory, or reproduces Jetson
unified-memory accounting — the REAL integration tier is ON HARDWARE
(**H1-H3**, task 11: the fixed component loading and serving on
`ryanorinagxdevkithomelabjp622`, the co-resident ONNX models unchanged,
the refusal fast and the deployment surviving it), and no assertion here
claims any of it.

Run (host-side, from the repo root):
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
      test/backend-test/jp6_vllm_kv_cache_oom/test_integration_preflight_prep.py \
      -q -p no:cacheprovider --noconftest

_Requirements: 2.9, 3.8_
"""
import argparse
import json
import logging

import dda_triton.vllm_model_prep as mp
from jp6_vllm_kv_cache_oom.fakes import (
    DEFAULT_MODEL_NAME,
    INCIDENT_ENGINE_ARGS,
    KV_OOM_REASON,
    build_staged_repo,
)

#: A realistic runtime preflight-refusal diagnostic: the marker first, then
#: the measured/computed terms — including the string
#: 'gpu_memory_utilization' (every real refusal spells the device budget
#: out as util x MemTotal), the very string that would trigger the KV-OOM
#: unload -> reload recovery if the classification order were wrong.
PREFLIGHT_REFUSAL_REASON = (
    "{} vLLM model '{}' cannot be loaded on this device now: measured "
    "available memory 3.00 GiB (MemAvailable) is below the computed "
    "requirement 12.00 GiB (weights 6.50 GiB + activation allowance "
    "4.50 GiB, an ESTIMATE, + KV cache floor 1.00 GiB), within the device "
    "budget 11.98 GiB (gpu_memory_utilization=0.4 x MemTotal 29.95 GiB)"
).format(mp.PREFLIGHT_REFUSED_MARKER, DEFAULT_MODEL_NAME)


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _args(**kwargs):
    namespace = argparse.Namespace(
        unarchived_repo_path=None,
        weights_path=None,
        model_name=None,
        component_name=None,
        cleanup=False,
    )
    for key, value in kwargs.items():
        setattr(namespace, key, value)
    return namespace


def _real_staging_into(monkeypatch, tmp_path):
    """Redirect the REAL ``stage_repository`` into ``tmp_path`` (the
    production default root is /aws_dda, not writable host-side; the
    staging logic itself — temp sibling copy, atomic rename, optional
    model.json rewrite — runs unmodified). Returns the tmp staging root."""
    model_repo_dir = tmp_path / "vllm_model_repo"
    real_stage = mp.stage_repository

    def staged_into_tmp(model_dir_src, model_name, rewritten_engine_args=None):
        return real_stage(model_dir_src, model_name, rewritten_engine_args,
                          model_repo_dir=str(model_repo_dir))

    monkeypatch.setattr(mp, "stage_repository", staged_into_tmp)
    return model_repo_dir


def _scripted_requests(monkeypatch, responses):
    """Monkeypatch the prep's HTTP layer: ``wait_for_server`` always True,
    ``requests.post`` serves ``responses`` in order (the last one repeats).
    Returns the list of endpoint suffixes hit (``load`` / ``unload``)."""
    endpoints = []

    def scripted_post(url, timeout=None):
        endpoints.append(url.rsplit("/", 1)[-1])
        index = min(len(endpoints) - 1, len(responses) - 1)
        return responses[index]

    monkeypatch.setattr(mp, "wait_for_server", lambda *a, **k: True)
    monkeypatch.setattr(mp.requests, "post", scripted_post)
    return endpoints


# ---------------------------------------------------------------------------
# Preflight refusal: classification, ERROR line, exit 0, verbatim staging
# ---------------------------------------------------------------------------

def test_preflight_refusal_classification_error_line_and_exit_zero(
        tmp_path, monkeypatch, caplog):
    """The refused-load end-to-end (2.9, 3.8): REAL validation + REAL
    staging into tmp, one load request answered by a crafted 409
    preflight-refusal body -> ``LOAD_PREFLIGHT_REFUSED`` (never the
    unload -> reload recovery, although the body contains
    'gpu_memory_utilization'), the prominent ERROR carries the full
    diagnostic, and ``prepare()`` returns **0** so the one deterministic
    pre-allocation refusal cannot take the Greengrass deployment
    BROKEN -> rolled back (defect 1.9).

    _Requirements: 2.9, 3.8_"""
    repo = tmp_path / "unarchived"
    build_staged_repo(repo, DEFAULT_MODEL_NAME)
    model_repo_dir = _real_staging_into(monkeypatch, tmp_path)
    endpoints = _scripted_requests(monkeypatch, [
        _Response(409, json.dumps({"error": PREFLIGHT_REFUSAL_REASON})),
    ])

    with caplog.at_level(logging.INFO):
        exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                     model_name=DEFAULT_MODEL_NAME))

    # Exit 0: deterministic, pre-allocation refusal — a component retry
    # cannot change it (2.9).
    assert exit_code == 0

    # Classified BEFORE the KV markers: exactly ONE load request, no
    # unload -> reload recovery for a load that never allocated anything.
    assert endpoints == ["load"], endpoints

    # The prominent ERROR line carries the full diagnostic verbatim.
    errors = [record.getMessage() for record in caplog.records
              if record.levelno >= logging.ERROR]
    refused_lines = [line for line in errors
                     if "REFUSED by the device memory preflight" in line]
    assert len(refused_lines) == 1, errors
    assert DEFAULT_MODEL_NAME in refused_lines[0]
    assert PREFLIGHT_REFUSAL_REASON in refused_lines[0], refused_lines[0]

    # The REAL stage_repository staged the repository VERBATIM (3.8): the
    # staged model.json is byte-identical to the unarchived source's.
    staged_model_json = (model_repo_dir / DEFAULT_MODEL_NAME / "1"
                         / "model.json")
    source_model_json = repo / DEFAULT_MODEL_NAME / "1" / "model.json"
    assert staged_model_json.read_bytes() == source_model_json.read_bytes()
    assert json.loads(staged_model_json.read_text()) == INCIDENT_ENGINE_ARGS
    staged_config = model_repo_dir / DEFAULT_MODEL_NAME / "config.pbtxt"
    assert staged_config.read_bytes() == \
        (repo / DEFAULT_MODEL_NAME / "config.pbtxt").read_bytes()


# ---------------------------------------------------------------------------
# Genuine KV-OOM: the single unload -> reload recovery still fires (3.8)
# ---------------------------------------------------------------------------

def test_genuine_kv_oom_still_fires_the_single_recovery_and_succeeds(
        tmp_path, monkeypatch, caplog):
    """A genuine KV-OOM 409 body (the device's verbatim reason, no
    preflight marker) still drives the validated single unload -> reload
    recovery — ``load, unload, load`` — and when the retry succeeds the
    prep exits 0 exactly as before the fix (3.8).

    _Requirements: 3.8_"""
    repo = tmp_path / "unarchived"
    build_staged_repo(repo, DEFAULT_MODEL_NAME)
    _real_staging_into(monkeypatch, tmp_path)
    endpoints = _scripted_requests(monkeypatch, [
        _Response(409, json.dumps({"error": KV_OOM_REASON})),  # first load
        _Response(200, ""),                                    # unload
        _Response(200, ""),                                    # retry load
    ])

    with caplog.at_level(logging.INFO):
        exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                     model_name=DEFAULT_MODEL_NAME))

    assert exit_code == 0
    assert endpoints == ["load", "unload", "load"], endpoints

    # The recovery's ERROR line for the first failure carried the staged
    # engine args (the sizing diagnostic contract, 3.8).
    errors = [record.getMessage() for record in caplog.records
              if record.levelno >= logging.ERROR]
    failure_lines = [line for line in errors if "FAILED to load" in line]
    assert len(failure_lines) == 1, errors
    assert "gpu_memory_utilization=0.4" in failure_lines[0]
    assert "max_model_len=4096" in failure_lines[0]


def test_genuine_kv_oom_recovery_fires_exactly_once_when_the_retry_fails(
        tmp_path, monkeypatch):
    """When the recovery's retry fails on the same KV-OOM body, the
    failure is authoritative: exactly ONE recovery cycle (never a second
    unload), classification ``LOAD_HTTP_ERROR``, exit 1 — the pre-fix
    contract Greengrass' Startup retry behavior depends on (3.8).

    _Requirements: 3.8_"""
    repo = tmp_path / "unarchived"
    build_staged_repo(repo, DEFAULT_MODEL_NAME)
    _real_staging_into(monkeypatch, tmp_path)
    endpoints = _scripted_requests(monkeypatch, [
        _Response(409, json.dumps({"error": KV_OOM_REASON})),  # first load
        _Response(200, ""),                                    # unload
        _Response(409, json.dumps({"error": KV_OOM_REASON})),  # retry load
    ])

    exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                 model_name=DEFAULT_MODEL_NAME))

    assert exit_code == 1
    assert endpoints == ["load", "unload", "load"], endpoints
