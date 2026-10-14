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
"""Property tests for the device-leg guards added by the task 14 H11/H12
dispatch of jp6-vllm-kv-cache-oom-regression — staged-repository ownership,
mutual exclusion, and the rule that a MODEL-load failure may not mark the
COMPONENT broken.

Properties (each one is universal over the generated input space, not a
worked example):

  PO-A **An authoritative runtime answer never fails the component** — for
       ANY non-200 HTTP status and ANY failure reason that is not a
       preflight refusal, ``prepare()`` exits **0**, the prominent ERROR
       still carries the model name, the HTTP status, the reason VERBATIM
       and the staged ``gpu_memory_utilization`` / ``max_model_len``, and
       the historical "exiting non-zero" terminal line is gone. Evidence
       for the change: three consecutive transient-DNS load failures each
       exited 1, the third drove ``currentState=BROKEN``, and the two
       workflows that HARD-depend on the component were stranded at
       ``INSTALLED`` with the core device UNHEALTHY (task 11 OUTCOME
       block 18).
  PO-B **Only a never-reachable runtime fails the component** — over the
       classification set, exit 1 iff ``LOAD_UNREACHABLE``.
  PO-C **The owner marker round-trips, atomically** — for ANY component
       name, model name and source path, the marker written by a successful
       stage reads back with those fields, leaves NO temp sibling behind,
       and never shadows the Triton contract layout.
  PO-D **A foreign teardown is refused; the owner's is not** — for ANY pair
       of distinct component names, a ``--cleanup`` by the non-owner skips
       BOTH the unload and the removal and still exits 0, while the owner's
       removes the tree.
  PO-E **A corrupt marker fails OPEN** — for ANY byte string that is not a
       JSON object, ``read_owner_marker`` returns ``None`` instead of
       raising, so a corrupt marker degrades to the pre-marker behaviour.

HONESTY GUARD (design "Honesty Guard", binding). Nothing here contacts a
runtime, loads a vLLM engine, allocates GPU memory or reproduces Jetson
unified-memory accounting: the HTTP layer is a monkeypatched ``requests``
and the "device" filesystem is a temp directory. The production evidence
these properties encode was collected on hardware and is cited, not
re-measured. The cross-process mutual-exclusion leg is driven with REAL
subprocesses in ``test_integration_preflight_prep.py``.

Run (host-side, from the repo root):
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
      test/backend-test/jp6_vllm_kv_cache_oom/test_property_stage_ownership.py \
      -q -p no:cacheprovider --noconftest

_Requirements: 3.8 (prep lifecycle semantics; the LOAD_HTTP_ERROR exit-code
mapping is a conscious, recorded repoint), 2.9_
"""
import argparse
import json
import logging
import os
import shutil
import tempfile

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import dda_triton.vllm_model_prep as mp
from jp6_vllm_kv_cache_oom.fakes import (
    DEFAULT_MODEL_NAME,
    INCIDENT_ENGINE_ARGS,
    build_staged_repo,
)

#: Component names in the shape the portal publishes them.
component_names = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"),
                           whitelist_characters="-"),
    min_size=1, max_size=40,
).map(lambda suffix: "model-vllm-{}".format(suffix))

#: Model names are DIRECTORY names on the device, so the alphabet is the
#: safe subset the portal actually emits.
model_names = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"),
                           whitelist_characters="-"),
    min_size=1, max_size=30,
).filter(lambda name: not name.startswith(".") and name not in (".", ".."))

#: Every authoritative non-200 answer the runtime can give.
http_statuses = st.sampled_from([400, 403, 404, 409, 422, 500, 502, 503])

#: Failure reasons that are NOT preflight refusals (the refusal path has its
#: own classification and its own properties in
#: ``test_property_device_preflight.py``).
failure_reasons = st.sampled_from([
    "Failed to resolve 'huggingface.co' ([Errno -3] Temporary failure in "
    "name resolution)",
    "No available memory for the cache blocks. Try increasing "
    "`gpu_memory_utilization` when initializing the engine.",
    'NVML_SUCCESS == r INTERNAL ASSERT FAILED at '
    '"/opt/pytorch/c10/cuda/CUDACachingAllocator.cpp":1131',
    "repository-invalid: model repository directory does not exist",
    "unknown model",
]).filter(lambda reason: mp.PREFLIGHT_REFUSED_MARKER not in reason)


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


class _Device:
    """A throwaway "device": a temp ``VLLM_MODEL_DIR``, the REAL
    ``stage_repository`` redirected into it, and a scripted HTTP layer."""

    def __init__(self, patch, model_name=DEFAULT_MODEL_NAME):
        self.root = tempfile.mkdtemp(prefix="jp6-ownership-")
        self.model_name = model_name
        self.model_repo_dir = os.path.join(self.root, "vllm_model_repo")
        self.endpoints = []
        real_stage = mp.stage_repository

        def staged_into_tmp(model_dir_src, name, rewritten=None):
            return real_stage(model_dir_src, name, rewritten,
                              model_repo_dir=self.model_repo_dir)

        patch.setattr(mp, "stage_repository", staged_into_tmp)
        patch.setattr(mp, "VLLM_MODEL_DIR", self.model_repo_dir)
        patch.setattr(mp, "wait_for_server", lambda *a, **k: True)

    def script(self, patch, responses):
        def scripted_post(url, timeout=None):
            self.endpoints.append(url.rsplit("/", 1)[-1])
            index = min(len(self.endpoints) - 1, len(responses) - 1)
            return responses[index]

        patch.setattr(mp.requests, "post", scripted_post)

    def source(self, name="unarchived", engine_args=None):
        path = os.path.join(self.root, name)
        build_staged_repo(path, self.model_name, engine_args)
        return path

    @property
    def staged_dir(self):
        return os.path.join(self.model_repo_dir, self.model_name)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------------------
# PO-A — an authoritative runtime answer never fails the component
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(status=http_statuses, reason=failure_reasons,
       component=component_names)
def test_property_authoritative_http_failure_exits_zero_and_stays_loud(
        status, reason, component):
    """PO-A. Whatever the status and whatever the reason, the COMPONENT
    survives (exit 0) and the MODEL's failure is still reported in full.

    # Validates: Requirements 3.8, 2.9
    """
    with pytest.MonkeyPatch.context() as patch:
        device = _Device(patch)
        try:
            source = device.source()
            # A KV-OOM reason legitimately drives the single recovery cycle
            # (unload -> reload); the last scripted response repeats, so the
            # retry fails the same way and the failure stays authoritative.
            device.script(patch, [_Response(status,
                                           json.dumps({"error": reason}))])
            records = _capture(logging.ERROR)
            exit_code = mp.prepare(_args(unarchived_repo_path=source,
                                         model_name=device.model_name,
                                         component_name=component))
            errors = records()
        finally:
            device.close()

    assert exit_code == 0, (status, reason)
    failures = [line for line in errors if "FAILED to load" in line]
    assert failures, errors
    assert device.model_name in failures[0]
    assert "(HTTP {})".format(status) in failures[0]
    assert reason in failures[0], failures[0]
    assert " | staged engine args: gpu_memory_utilization=0.4, " \
           "max_model_len=4096" in failures[0], failures[0]
    terminal = [line for line in errors if "reconciler owns the retries" in line]
    assert len(terminal) == 1, errors
    assert not [line for line in errors if "exiting non-zero" in line], errors


def _capture(level):
    """Attach a recording handler to the root logger and return a callable
    yielding the messages at or above ``level``. (Hypothesis-driven tests
    cannot reuse a function-scoped ``caplog`` across examples.)"""
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            if record.levelno >= level:
                records.append(record.getMessage())

    handler = _Handler(level=logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.DEBUG)

    def messages():
        root.removeHandler(handler)
        root.setLevel(previous)
        return list(records)

    return messages


# ---------------------------------------------------------------------------
# PO-B — only a never-reachable runtime fails the component
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(classification=st.sampled_from((mp.LOAD_OK, mp.LOAD_HTTP_ERROR,
                                       mp.LOAD_UNREACHABLE,
                                       mp.LOAD_PREFLIGHT_REFUSED)),
       component=component_names)
def test_property_only_unreachable_runtime_exits_non_zero(classification,
                                                          component):
    """PO-B. ``LOAD_UNREACHABLE`` -> 1 (a component retry IS the recovery:
    the component started before the backend was ready); every other
    classification -> 0, because the runtime answered and the answer is the
    MODEL's state, not the component's health.

    # Validates: Requirements 3.8
    """
    with pytest.MonkeyPatch.context() as patch:
        device = _Device(patch)
        try:
            source = device.source()
            patch.setattr(mp, "request_load",
                          lambda name, engine_args=None: classification)
            exit_code = mp.prepare(_args(unarchived_repo_path=source,
                                         model_name=device.model_name,
                                         component_name=component))
        finally:
            device.close()

    expected = 1 if classification == mp.LOAD_UNREACHABLE else 0
    assert exit_code == expected, (classification, exit_code)


# ---------------------------------------------------------------------------
# PO-C — the owner marker round-trips, atomically
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(component=component_names, model_name=model_names)
def test_property_owner_marker_round_trips_and_leaves_no_litter(
        component, model_name):
    """PO-C. Whatever the component and model name, a successful stage
    records the owner, the model, the source artifact path and a UTC
    timestamp; the marker reads back through the public helpers; no temp
    sibling survives; and the staged repository still contains exactly the
    Triton contract entries plus the dot-prefixed marker.

    # Validates: Requirements 3.8
    """
    with pytest.MonkeyPatch.context() as patch:
        device = _Device(patch, model_name=model_name)
        try:
            source = device.source()
            device.script(patch, [_Response(200, "")])
            exit_code = mp.prepare(_args(unarchived_repo_path=source,
                                         model_name=model_name,
                                         component_name=component))
            marker = mp.read_owner_marker(model_name)
            entries = sorted(os.listdir(device.staged_dir))
        finally:
            device.close()

    assert exit_code == 0
    assert marker is not None
    assert marker["component_name"] == component
    assert marker["model_name"] == model_name
    assert marker["source_unarchived_path"] == source
    assert marker["staged_at"].endswith("Z")
    assert mp.marker_owner(marker) == component
    assert entries == sorted(["config.pbtxt", "1", mp.OWNER_MARKER_NAME]), \
        entries


# ---------------------------------------------------------------------------
# PO-D — a foreign teardown is refused; the owner's is not
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(pair=st.tuples(component_names, component_names)
       .filter(lambda pair: pair[0] != pair[1]))
def test_property_foreign_cleanup_is_refused_and_owner_cleanup_proceeds(pair):
    """PO-D. For ANY two distinct components sharing one ``--model_name``:
    the non-owner's ``--cleanup`` issues NO unload and removes NOTHING yet
    still exits 0 (a failed teardown would fail the deployment), while the
    recorded owner's ``--cleanup`` unloads and removes the tree.

    This is the production hazard: a non-owning Shutdown ran ``POST
    /unload`` 200 -> ``unloaded successfully`` -> ``Cleaned directory: ...``
    0.6 s after the owner's load succeeded (task 11 OUTCOME blocks 17-18),
    which is why the guard covers the unload as well as the removal.

    # Validates: Requirements 3.8
    """
    owner, foreign = pair
    with pytest.MonkeyPatch.context() as patch:
        device = _Device(patch)
        try:
            source = device.source()
            device.script(patch, [_Response(200, "")])
            assert mp.prepare(_args(unarchived_repo_path=source,
                                    model_name=device.model_name,
                                    component_name=owner)) == 0
            unloads = []
            patch.setattr(mp, "request_unload",
                          lambda name: unloads.append(name) or True)

            foreign_rc = mp.cleanup(_args(model_name=device.model_name,
                                          component_name=foreign,
                                          cleanup=True))
            survived = os.path.isdir(device.staged_dir)
            marker_survived = os.path.isfile(
                os.path.join(device.staged_dir, mp.OWNER_MARKER_NAME))
            unloads_after_foreign = list(unloads)

            owner_rc = mp.cleanup(_args(model_name=device.model_name,
                                        component_name=owner, cleanup=True))
            removed = not os.path.exists(device.staged_dir)
            unloads_after_owner = list(unloads)
        finally:
            device.close()

    assert foreign_rc == 0, "a refused teardown must not fail the deployment"
    assert unloads_after_foreign == [], (
        "the non-owner's cleanup issued an unload: {}".format(
            unloads_after_foreign))
    assert survived and marker_survived, (survived, marker_survived)
    assert owner_rc == 0
    assert unloads_after_owner == [device.model_name], unloads_after_owner
    assert removed


# ---------------------------------------------------------------------------
# PO-E — a corrupt marker fails OPEN
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(payload=st.one_of(
    st.text(max_size=200),
    st.just(""),
    st.just("[]"),
    st.just("null"),
    st.just("42"),
    st.just('"a string, not an object"'),
    st.binary(max_size=64).map(lambda raw: raw.decode("latin-1")),
))
def test_property_corrupt_marker_fails_open(payload):
    """PO-E. ANY marker payload that is not a JSON object yields ``None``
    (never an exception), so ownership degrades to "unknown" and the
    pre-marker behaviour applies. A guard that could crash a lifecycle
    script would be worse than no guard.

    # Validates: Requirements 3.8
    """
    with pytest.MonkeyPatch.context() as patch:
        device = _Device(patch)
        try:
            build_staged_repo(device.model_repo_dir, device.model_name,
                              INCIDENT_ENGINE_ARGS)
            with open(os.path.join(device.staged_dir, mp.OWNER_MARKER_NAME),
                      "w", encoding="utf-8") as handle:
                handle.write(payload)
            marker = mp.read_owner_marker(device.model_name)
        finally:
            device.close()

    try:
        parsed = json.loads(payload)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        assert marker == parsed
    else:
        assert marker is None, (payload, marker)
    assert mp.marker_owner(marker) == mp.marker_owner(marker)  # never raises
