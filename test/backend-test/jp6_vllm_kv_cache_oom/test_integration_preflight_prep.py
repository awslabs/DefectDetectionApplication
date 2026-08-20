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
import os
import subprocess
import sys
import textwrap

import dda_triton.vllm_model_prep as mp
from jp6_vllm_kv_cache_oom.fakes import (
    DEFAULT_MODEL_NAME,
    INCIDENT_ENGINE_ARGS,
    KV_OOM_REASON,
    build_staged_repo,
)

#: The transient fault that drove the component BROKEN in production (task
#: 11 OUTCOME block 18): three consecutive load attempts at 12:00:47Z /
#: 12:02:09Z / 12:03:22Z failed on DNS, each exiting 1, and after the third
#: ``currentState=BROKEN`` left the two HARD-dependent workflows stuck at
#: INSTALLED and the core device UNHEALTHY. Verbatim.
TRANSIENT_DNS_REASON = (
    "Failed to resolve 'huggingface.co' ([Errno -3] Temporary failure in "
    "name resolution)"
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
    model_repo_dir_default = tmp_path / "vllm_model_repo"
    real_stage = mp.stage_repository

    def staged_into_tmp(model_dir_src, model_name, rewritten_engine_args=None,
                        model_repo_dir=None):
        # ``model_repo_dir`` is accepted (and ignored) so re-applying this
        # redirection in the same test is idempotent.
        return real_stage(model_dir_src, model_name, rewritten_engine_args,
                          model_repo_dir=str(model_repo_dir_default))

    monkeypatch.setattr(mp, "stage_repository", staged_into_tmp)
    return model_repo_dir_default


def _prep_root(monkeypatch, tmp_path):
    """Redirect the WHOLE prep filesystem surface into ``tmp_path``: the
    REAL ``stage_repository`` stages there, and ``VLLM_MODEL_DIR`` — which
    the owner marker, the advisory lock and ``cleanup`` all derive their
    paths from — points at the same root. Nothing under /aws_dda is
    touched. Returns the tmp repository root."""
    model_repo_dir = _real_staging_into(monkeypatch, tmp_path)
    monkeypatch.setattr(mp, "VLLM_MODEL_DIR", str(model_repo_dir))
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
        tmp_path, monkeypatch, caplog):
    """When the recovery's retry fails on the same KV-OOM body, the
    failure is authoritative: exactly ONE recovery cycle (never a second
    unload) and classification ``LOAD_HTTP_ERROR``.

    CONSCIOUS REPOINT (task 14 H11 dispatch; task 11 OUTCOME block 18) —
    the exit code is now **0**, not 1. Verbatim original, recorded so the
    change is auditable and never mistaken for a weakening::

        \"\"\"When the recovery's retry fails on the same KV-OOM body, the
        failure is authoritative: exactly ONE recovery cycle (never a second
        unload), classification ``LOAD_HTTP_ERROR``, exit 1 — the pre-fix
        contract Greengrass' Startup retry behavior depends on (3.8).

        _Requirements: 3.8_\"\"\"
        ...
        assert exit_code == 1
        assert endpoints == ["load", "unload", "load"], endpoints

    Reason: a MODEL that cannot load must not be able to mark the
    COMPONENT broken. Three consecutive transient-DNS load failures drove
    ``currentState=BROKEN``, which left the two workflows that HARD-depend
    on the component stuck at ``INSTALLED`` and the core device UNHEALTHY.
    NOTHING was weakened: the recovery still fires exactly once, the
    request sequence is still asserted, the classification is still
    ``LOAD_HTTP_ERROR``, and this repoint ADDS assertions that the model's
    failure is still reported loudly (the prominent ERROR keeps the reason
    and the staged args, and the terminal line names the reconciler).

    _Requirements: 3.8_"""
    repo = tmp_path / "unarchived"
    build_staged_repo(repo, DEFAULT_MODEL_NAME)
    _real_staging_into(monkeypatch, tmp_path)
    endpoints = _scripted_requests(monkeypatch, [
        _Response(409, json.dumps({"error": KV_OOM_REASON})),  # first load
        _Response(200, ""),                                    # unload
        _Response(409, json.dumps({"error": KV_OOM_REASON})),  # retry load
    ])

    with caplog.at_level(logging.INFO):
        exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                     model_name=DEFAULT_MODEL_NAME))

    assert exit_code == 0
    assert endpoints == ["load", "unload", "load"], endpoints

    errors = [record.getMessage() for record in caplog.records
              if record.levelno >= logging.ERROR]
    # The authoritative failure is still reported with every element 3.8
    # pins: model name, HTTP status, the reason verbatim, staged args.
    failures = [line for line in errors if "FAILED to load" in line]
    assert failures, errors
    assert "(HTTP 409)" in failures[-1]
    assert KV_OOM_REASON in failures[-1]
    assert "staged engine args: gpu_memory_utilization=0.4" in failures[-1]
    assert "max_model_len=4096" in failures[-1]
    # ... and the terminal line says why the component is not failed.
    terminal = [line for line in errors if "reconciler owns the retries" in line]
    assert len(terminal) == 1, errors
    assert "model-status surfaces" in terminal[0]
    assert "deliberately NOT failed" in terminal[0]


# ---------------------------------------------------------------------------
# A model-load failure must not mark the COMPONENT broken (task 11 block 18)
# ---------------------------------------------------------------------------

def test_transient_http_failure_exits_zero_with_the_full_error_preserved(
        tmp_path, monkeypatch, caplog):
    """The production regression, end to end: a TRANSIENT authoritative
    failure (the verbatim DNS resolution error of 12:00:47Z / 12:02:09Z /
    12:03:22Z) is classified ``LOAD_HTTP_ERROR``, keeps every element
    bugfix.md 3.8 pins in the prominent ERROR — model name, HTTP status,
    the reason VERBATIM and the staged
    ``gpu_memory_utilization`` / ``max_model_len`` — and ``prepare()``
    returns **0** so the component is not driven to BROKEN and the two
    workflows that HARD-depend on it are not stranded at INSTALLED.

    _Requirements: 3.8 (exit-code mapping repointed for LOAD_HTTP_ERROR;
    task 14 H11 dispatch, task 11 OUTCOME block 18)_"""
    repo = tmp_path / "unarchived"
    build_staged_repo(repo, DEFAULT_MODEL_NAME)
    _prep_root(monkeypatch, tmp_path)
    endpoints = _scripted_requests(monkeypatch, [
        _Response(400, json.dumps({"error": TRANSIENT_DNS_REASON})),
    ])

    with caplog.at_level(logging.INFO):
        exit_code = mp.prepare(_args(
            unarchived_repo_path=str(repo),
            model_name=DEFAULT_MODEL_NAME,
            component_name="model-vllm-qwen2-5-vl-7b-instruct-awq"))

    assert exit_code == 0
    # A non-KV, non-preflight reason keeps the single-attempt semantics.
    assert endpoints == ["load"], endpoints

    errors = [record.getMessage() for record in caplog.records
              if record.levelno >= logging.ERROR]
    failures = [line for line in errors if "FAILED to load" in line]
    assert len(failures) == 1, errors
    assert DEFAULT_MODEL_NAME in failures[0]
    assert "(HTTP 400)" in failures[0]
    assert TRANSIENT_DNS_REASON in failures[0], failures[0]
    assert " | staged engine args: gpu_memory_utilization=0.4, " \
           "max_model_len=4096" in failures[0], failures[0]

    terminal = [line for line in errors if "reconciler owns the retries" in line]
    assert len(terminal) == 1, errors
    assert "model-status surfaces" in terminal[0]
    assert "deliberately NOT failed" in terminal[0]
    # The historical "exiting non-zero so the component retries" terminal
    # line must NOT appear on this path any more.
    assert not [line for line in errors if "exiting non-zero" in line], errors


def test_unreachable_runtime_still_exits_one_unchanged(
        tmp_path, monkeypatch, caplog):
    """``LOAD_UNREACHABLE`` is UNCHANGED: the runtime was never reachable,
    so the component genuinely started before the backend was ready and a
    component-level retry IS the recovery — exit 1, with its authoritative
    diagnostic byte-identical.

    _Requirements: 3.8_"""
    repo = tmp_path / "unarchived"
    build_staged_repo(repo, DEFAULT_MODEL_NAME)
    _prep_root(monkeypatch, tmp_path)
    monkeypatch.setattr(mp, "wait_for_server", lambda *a, **k: False)
    monkeypatch.setattr(mp.requests, "post", _fail_if_posted)

    with caplog.at_level(logging.INFO):
        exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                     model_name=DEFAULT_MODEL_NAME))

    assert exit_code == 1
    errors = " ".join(record.getMessage() for record in caplog.records
                      if record.levelno >= logging.ERROR)
    assert "was never reachable" in errors, errors
    assert "flask-app" in errors, errors
    assert "docker ps -a" in errors, errors
    assert "Exiting non-zero so the component retries once the backend" in \
        errors, errors


def test_successful_load_still_exits_zero(tmp_path, monkeypatch):
    """``LOAD_OK`` -> 0, unchanged.

    _Requirements: 3.8_"""
    repo = tmp_path / "unarchived"
    build_staged_repo(repo, DEFAULT_MODEL_NAME)
    _prep_root(monkeypatch, tmp_path)
    endpoints = _scripted_requests(monkeypatch, [_Response(200, "")])

    exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                 model_name=DEFAULT_MODEL_NAME,
                                 component_name="model-vllm-owner"))

    assert exit_code == 0
    assert endpoints == ["load"], endpoints


def _fail_if_posted(*args, **kwargs):
    raise AssertionError("no HTTP request may be issued on this path")


# ---------------------------------------------------------------------------
# Owner marker + mutual exclusion (task 14 H11/H12)
# ---------------------------------------------------------------------------

OWNER = "model-vllm-qwen2-5-vl-7b-instruct-awq-jetson-xavier-jp6"
FOREIGN = "model-vllm-qwen2-5-vl-7b-instruct-awq"


def _stage_as(tmp_path, monkeypatch, component, engine_args=None,
              source_name="unarchived"):
    """Run the REAL ``prepare`` as ``component`` against a successful load,
    into the tmp repository root. Returns (exit_code, repo_root)."""
    repo = tmp_path / source_name
    build_staged_repo(repo, DEFAULT_MODEL_NAME, engine_args)
    model_repo_dir = _prep_root(monkeypatch, tmp_path)
    _scripted_requests(monkeypatch, [_Response(200, "")])
    exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                 model_name=DEFAULT_MODEL_NAME,
                                 component_name=component))
    return exit_code, model_repo_dir


def test_owner_marker_is_written_atomically_and_is_readable(
        tmp_path, monkeypatch):
    """A successful stage records the owning component, the model name, the
    source unarchived artifact path and a UTC timestamp INSIDE the staged
    repository, under a name that cannot collide with the Triton contract
    (``config.pbtxt`` + ``1/model.json``) and that the runtime ignores. It
    is written atomically — no temp sibling is left behind — and the
    repository still validates as a vLLM repository afterwards.

    _Requirements: 3.8 (task 14 H11)_"""
    exit_code, model_repo_dir = _stage_as(tmp_path, monkeypatch, OWNER)
    assert exit_code == 0

    staged = model_repo_dir / DEFAULT_MODEL_NAME
    marker_path = staged / mp.OWNER_MARKER_NAME
    assert marker_path.is_file(), sorted(p.name for p in staged.iterdir())
    marker = json.loads(marker_path.read_text())
    assert marker["component_name"] == OWNER
    assert marker["model_name"] == DEFAULT_MODEL_NAME
    assert marker["source_unarchived_path"] == str(tmp_path / "unarchived")
    assert marker["staged_at"].endswith("Z")
    assert marker["staged_at"].startswith("20")

    # The public readers agree with the file.
    assert mp.read_owner_marker(DEFAULT_MODEL_NAME) == marker
    assert mp.marker_owner(mp.read_owner_marker(DEFAULT_MODEL_NAME)) == OWNER

    # Atomic: the temp sibling is gone, and nothing but the marker was added
    # to the Triton contract layout.
    names = sorted(entry.name for entry in staged.iterdir())
    assert names == sorted(["config.pbtxt", "1", mp.OWNER_MARKER_NAME]), names
    assert mp.OWNER_MARKER_NAME.startswith("."), mp.OWNER_MARKER_NAME
    # The runtime's own parser ignores it (this is the real parser).
    from vllm_runtime.repository import parse_repository
    assert parse_repository(str(staged)) == INCIDENT_ENGINE_ARGS

    # The advisory lock lives OUTSIDE the staged tree so it survives cleanup.
    lock_path = mp.stage_lock_path(DEFAULT_MODEL_NAME)
    assert os.path.isfile(lock_path), lock_path
    assert not lock_path.startswith(str(staged)), lock_path
    assert not lock_path.startswith(str(model_repo_dir) + os.sep), lock_path


def test_foreign_cleanup_is_refused_and_leaves_everything_intact(
        tmp_path, monkeypatch, caplog):
    """The production hazard (task 11 OUTCOME blocks 17-18: a non-owning
    Shutdown unloaded and deleted a model 0.6 s after its load succeeded).
    A ``--cleanup`` by a component that is NOT the recorded owner is
    REFUSED: the unload POST is never issued, the staged tree and the marker
    survive, a prominent WARNING names both components, and the exit code is
    still 0 so the teardown is not failed.

    _Requirements: 3.8 (task 14 H11/H12)_"""
    exit_code, model_repo_dir = _stage_as(tmp_path, monkeypatch, OWNER)
    assert exit_code == 0
    staged = model_repo_dir / DEFAULT_MODEL_NAME
    before = staged / "1" / "model.json"
    content_before = before.read_bytes()

    unloads = []
    monkeypatch.setattr(mp, "request_unload",
                        lambda name: unloads.append(name) or True)

    with caplog.at_level(logging.INFO):
        rc = mp.cleanup(_args(model_name=DEFAULT_MODEL_NAME,
                              component_name=FOREIGN, cleanup=True))

    assert rc == 0
    assert unloads == [], "the unload POST must be inside the guard"
    assert staged.is_dir()
    assert before.read_bytes() == content_before
    assert (staged / mp.OWNER_MARKER_NAME).is_file()

    warnings = [record.getMessage() for record in caplog.records
                if record.levelno == logging.WARNING]
    refusals = [line for line in warnings if "REFUSING --cleanup" in line]
    assert len(refusals) == 1, warnings
    assert OWNER in refusals[0] and FOREIGN in refusals[0], refusals[0]
    assert "Directory cleanup finished" not in [
        record.getMessage() for record in caplog.records]


def test_owner_cleanup_still_removes_the_tree_and_is_idempotent(
        tmp_path, monkeypatch, caplog):
    """A ``--cleanup`` by the RECORDED OWNER behaves exactly as before:
    unload, remove the staged tree (marker and all), and a second run is a
    no-op that still exits 0.

    _Requirements: 3.8_"""
    exit_code, model_repo_dir = _stage_as(tmp_path, monkeypatch, OWNER)
    assert exit_code == 0
    staged = model_repo_dir / DEFAULT_MODEL_NAME
    leftover = model_repo_dir / "{}{}-abc".format(mp._STAGING_PREFIX,
                                                 DEFAULT_MODEL_NAME)
    leftover.mkdir(parents=True)

    unloads = []
    monkeypatch.setattr(mp, "request_unload",
                        lambda name: unloads.append(name) or True)

    with caplog.at_level(logging.INFO):
        first = mp.cleanup(_args(model_name=DEFAULT_MODEL_NAME,
                                 component_name=OWNER, cleanup=True))
        second = mp.cleanup(_args(model_name=DEFAULT_MODEL_NAME,
                                  component_name=OWNER, cleanup=True))

    assert (first, second) == (0, 0)
    assert unloads == [DEFAULT_MODEL_NAME, DEFAULT_MODEL_NAME]
    assert not staged.exists()
    assert not leftover.exists()
    # The lock file survived the tree it protects (that is the point of
    # keeping it outside VLLM_MODEL_DIR).
    assert os.path.isfile(mp.stage_lock_path(DEFAULT_MODEL_NAME))


def test_foreign_stage_is_permitted_but_warned(tmp_path, monkeypatch, caplog):
    """The newest deployment legitimately takes the path over, so a stage by
    a NON-owning component proceeds — but a prominent WARNING names both
    components and states that they share one ``--model_name`` and therefore
    one staged path. The marker moves to the new owner.

    _Requirements: 3.8 (task 14 H11)_"""
    exit_code, model_repo_dir = _stage_as(tmp_path, monkeypatch, OWNER)
    assert exit_code == 0

    takeover_args = dict(INCIDENT_ENGINE_ARGS, gpu_memory_utilization=0.55)
    with caplog.at_level(logging.INFO):
        exit_code = _stage_as(tmp_path, monkeypatch, FOREIGN,
                              engine_args=takeover_args,
                              source_name="unarchived-foreign")[0]

    assert exit_code == 0
    warnings = [record.getMessage() for record in caplog.records
                if record.levelno == logging.WARNING]
    collisions = [line for line in warnings if "STAGING COLLISION" in line]
    assert len(collisions) == 1, warnings
    assert OWNER in collisions[0] and FOREIGN in collisions[0], collisions[0]
    assert DEFAULT_MODEL_NAME in collisions[0]
    assert "--model_name" in collisions[0]

    staged_json = json.loads(
        (model_repo_dir / DEFAULT_MODEL_NAME / "1" / "model.json").read_text())
    assert staged_json == takeover_args
    assert mp.marker_owner(mp.read_owner_marker(DEFAULT_MODEL_NAME)) == FOREIGN


def test_malformed_marker_fails_open(tmp_path, monkeypatch, caplog):
    """An unreadable or malformed marker must NOT crash a deployment: it is
    treated as UNOWNED with a WARNING, so both the stage and the cleanup
    behave exactly as they did before markers existed.

    _Requirements: 3.8 (task 14 H11)_"""
    exit_code, model_repo_dir = _stage_as(tmp_path, monkeypatch, OWNER)
    assert exit_code == 0
    staged = model_repo_dir / DEFAULT_MODEL_NAME
    (staged / mp.OWNER_MARKER_NAME).write_text("{not json at all")

    with caplog.at_level(logging.INFO):
        assert mp.read_owner_marker(DEFAULT_MODEL_NAME) is None
        unloads = []
        monkeypatch.setattr(mp, "request_unload",
                            lambda name: unloads.append(name) or True)
        rc = mp.cleanup(_args(model_name=DEFAULT_MODEL_NAME,
                              component_name=FOREIGN, cleanup=True))

    assert rc == 0
    # Fail-OPEN: the foreign cleanup proceeded because ownership is unknown.
    assert unloads == [DEFAULT_MODEL_NAME]
    assert not staged.exists()
    warnings = " ".join(record.getMessage() for record in caplog.records
                        if record.levelno == logging.WARNING)
    assert "unreadable or malformed" in warnings, warnings
    assert "UNOWNED" in warnings, warnings


# ---------------------------------------------------------------------------
# Mutual exclusion across PROCESSES (Greengrass spawns one python3 per
# lifecycle script, so a threading lock would be useless) — task 14 H12
# ---------------------------------------------------------------------------

_LOCK_CHILD = textwrap.dedent(
    """
    import json, os, sys, time
    sys.path.insert(0, sys.argv[1])
    import dda_triton.vllm_model_prep as mp

    root, model, sentinel, out = sys.argv[2:6]
    mp.VLLM_MODEL_DIR = root
    result = {"acquired": None, "overlapped": False}
    with mp.stage_lock(model) as acquired:
        result["acquired"] = bool(acquired)
        try:
            os.close(os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            result["overlapped"] = True
        # Long enough that two unsynchronised children would certainly
        # overlap; the assertion is on the sentinel, never on timing.
        time.sleep(0.5)
        if not result["overlapped"]:
            os.unlink(sentinel)
    with open(out, "w") as handle:
        json.dump(result, handle)
    """
)


def test_stage_lock_serialises_two_real_processes(tmp_path):
    """The lock is an OS-level advisory ``fcntl.flock`` on a file outside the
    staged tree, so it works across the separate ``python3`` processes
    Greengrass spawns per lifecycle script. Two children enter the critical
    section for the same model name; each claims an ``O_EXCL`` sentinel on
    entry and releases it on exit, so an overlap is detected as a FACT and
    not inferred from timing.

    _Requirements: 3.8 (task 14 H12: two components were caught staging the
    same path 2 ms apart, so ordering alone cannot make the stage safe)_"""
    script = tmp_path / "lock_child.py"
    script.write_text(_LOCK_CHILD)
    root = tmp_path / "vllm_model_repo"
    root.mkdir()
    sentinel = tmp_path / "in-critical-section"
    src = os.path.join(os.path.dirname(os.path.dirname(mp.__file__)))

    outputs = [tmp_path / "child-0.json", tmp_path / "child-1.json"]
    children = [
        subprocess.Popen([sys.executable, str(script), src, str(root),
                          DEFAULT_MODEL_NAME, str(sentinel), str(out)])
        for out in outputs
    ]
    for child in children:
        assert child.wait(timeout=60) == 0

    results = [json.loads(out.read_text()) for out in outputs]
    assert all(result["acquired"] for result in results), results
    assert not any(result["overlapped"] for result in results), (
        "MUTUAL EXCLUSION VIOLATED: two processes were inside the "
        "stage-or-cleanup critical section for model '{}' at the same time "
        "({})".format(DEFAULT_MODEL_NAME, results))
    assert not sentinel.exists()


_PREPARE_CHILD = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, sys.argv[1])
    import dda_triton.vllm_model_prep as mp

    root, source, model, component = sys.argv[2:6]
    mp.VLLM_MODEL_DIR = root
    real_stage = mp.stage_repository
    mp.stage_repository = (
        lambda src, name, rewritten=None: real_stage(src, name, rewritten,
                                                     model_repo_dir=root))
    mp.wait_for_server = lambda *a, **k: True


    class _Response:
        status_code = 200
        text = ""


    mp.requests.post = lambda url, timeout=None: _Response()
    args = mp.parser.parse_args(["--unarchived_repo_path", source,
                                 "--model_name", model,
                                 "--component_name", component])
    sys.exit(mp.prepare(args))
    """
)


def test_two_concurrent_prepares_leave_a_consistent_stage_and_marker(
        tmp_path):
    """Two REAL ``prepare`` runs in two REAL processes race for the same
    ``--model_name`` (the 08:53:59.239Z / .241Z production collision). The
    lock makes the stage-plus-marker critical section indivisible, so the
    surviving state is ONE component's entirely: the staged ``model.json``
    is exactly one of the two candidate configurations and the marker names
    the component that wrote THAT configuration — never a mix.

    _Requirements: 3.8 (task 14 H11/H12)_"""
    script = tmp_path / "prepare_child.py"
    script.write_text(_PREPARE_CHILD)
    root = tmp_path / "vllm_model_repo"
    root.mkdir()
    src = os.path.join(os.path.dirname(os.path.dirname(mp.__file__)))

    candidates = {}
    children = []
    for component, utilization in ((OWNER, 0.55), (FOREIGN, 0.4)):
        source = tmp_path / "unarchived-{}".format(utilization)
        engine_args = dict(INCIDENT_ENGINE_ARGS,
                           gpu_memory_utilization=utilization)
        build_staged_repo(source, DEFAULT_MODEL_NAME, engine_args)
        candidates[component] = engine_args
        children.append(subprocess.Popen(
            [sys.executable, str(script), src, str(root), str(source),
             DEFAULT_MODEL_NAME, component]))
    for child in children:
        assert child.wait(timeout=120) == 0

    staged = root / DEFAULT_MODEL_NAME
    staged_json = json.loads((staged / "1" / "model.json").read_text())
    marker = json.loads((staged / mp.OWNER_MARKER_NAME).read_text())
    owner = marker["component_name"]
    assert owner in candidates, marker
    assert staged_json == candidates[owner], (
        "the staged configuration and the recorded owner disagree — the "
        "critical section was not indivisible: marker {!r} but staged "
        "{!r}".format(marker, staged_json))
