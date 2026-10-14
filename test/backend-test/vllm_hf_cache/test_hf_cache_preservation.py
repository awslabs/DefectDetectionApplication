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
"""Preservation property tests (Task 2) for vllm-hf-cache-persistence.

Property 2: Preservation - Everything except the HF_HOME entries is
unchanged.

**Validates: Requirements 3.1, 3.2, 3.3**

Observation-first (observed on the UNFIXED tree and recorded as goldens in
``goldens/``, mirroring ``deploy_reliability/
test_config_structure_preservation.py``):

  * ``hf_home_masked_compose_structure.golden.json`` - the full parsed
    structure of ``src/docker-compose.yaml``, masked of ONLY ``HF_HOME``
    entries in each backend service's ``environment`` list (the one change
    the fix is allowed to make). Everything else - services, volumes,
    healthchecks, restart policies, ``stop_grace_period``, profiles,
    ``runtime: nvidia`` / GPU device reservations, the frontend service,
    and every other environment entry - is pinned (Requirement 3.1).
  * ``code_baseline.sha256.golden.json`` - the sha256 of
    ``src/backend/dda_triton/vllm_model_prep.py`` and of the recipe
    variants. The fix is compose-only: no code or recipe drift is allowed
    (Requirement 3.2).

These tests PASS on the unfixed tree (the goldens were captured from it)
and must still PASS after the fix (task 3.5); any drift outside the masked
HF_HOME entries is a preservation regression.

The cleanup tests (Requirement 3.3) pin the behavioral seam directly:
``vllm_model_prep.cleanup`` removes the staged repo directory (and leftover
staging temp siblings) under ``dda_triton/vllm_model_repo`` and NEVER
touches a sibling ``hf_cache`` tree. The property-based variant (Hypothesis)
generates random ``models--{org}--{name}`` HF cache layouts and asserts the
cache tree is byte-identical across cleanup.

Runs on the host with ``--noconftest`` (like the security guard suite);
requires PYTHONPATH=src/backend for the ``dda_triton`` import (repo
convention).

Regenerating goldens (ONLY for intentional, reviewed changes - task 3.2
does NOT regenerate these; the HF_HOME mask absorbs the fix):

    python3 test/backend-test/vllm_hf_cache/test_hf_cache_preservation.py --regenerate
"""
import copy
import hashlib
import json
import os
import shutil
import tempfile
from argparse import Namespace
from unittest import mock

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

import dda_triton.vllm_model_prep as prep

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
GOLDENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "goldens")

COMPOSE_PATH = os.path.join(REPO_ROOT, "src", "docker-compose.yaml")
COMPOSE_GOLDEN = "hf_home_masked_compose_structure.golden.json"
CODE_BASELINE_GOLDEN = "code_baseline.sha256.golden.json"

#: The three backend services the fix is allowed to touch (and ONLY by
#: adding an HF_HOME environment entry).
BACKEND_SERVICES = (
    "backend_tegra_gpu_enabled",
    "backend_generic",
    "backend_generic_nvidia",
)

#: The single environment key the fix may add to the backend services.
#: Everything else in the compose file is pinned by the golden.
MASKED_ENV_KEY = "HF_HOME"

#: Compose-only fix: these files must be byte-identical to the unfixed
#: baseline (Requirement 3.2). recipe.yaml (the per-target working copy
#: swapped by the build tooling) is deliberately not tracked.
CODE_BASELINE_FILES = (
    "src/backend/dda_triton/vllm_model_prep.py",
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64.yaml",
    "recipe-amd64.yaml",
    "recipe-amd64-nvidia.yaml",
)


def _load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _env_key(entry):
    """The KEY of a compose env list entry ("KEY=value" or bare "KEY")."""
    return str(entry).partition("=")[0]


def mask_hf_home(compose):
    """The compose document minus ONLY HF_HOME entries in the backend
    services' environment lists (the one change the fix may make).

    Handles both the list form (["KEY=value", "KEY"]) and the mapping form
    ({KEY: value}). Non-backend services (the frontend) are NOT masked -
    an HF_HOME appearing there would be an unintended change and must fail
    the golden comparison.
    """
    masked = copy.deepcopy(compose)
    services = masked.get("services") or {}
    for name in BACKEND_SERVICES:
        service = services.get(name)
        if not service:
            continue
        environment = service.get("environment")
        if isinstance(environment, dict):
            environment.pop(MASKED_ENV_KEY, None)
        elif isinstance(environment, list):
            service["environment"] = [
                entry for entry in environment
                if _env_key(entry) != MASKED_ENV_KEY
            ]
    return masked


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _golden_path(name):
    return os.path.join(GOLDENS_DIR, name)


def _load_golden(name):
    path = _golden_path(name)
    assert os.path.isfile(path), (
        "golden fixture missing: {} - regenerate with `python3 {} "
        "--regenerate` on a KNOWN-GOOD (unfixed-baseline) tree".format(
            path, os.path.abspath(__file__)))
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Test 1: masked-structure identity (Requirement 3.1)
# ---------------------------------------------------------------------------

def test_compose_structure_matches_golden_outside_hf_home():
    """Property 2 (Requirement 3.1): after masking ONLY the HF_HOME env
    entries the fix may add to the three backend services, the parsed
    structure of src/docker-compose.yaml equals the golden captured from
    the unfixed tree - services, volumes, healthchecks, restart policies,
    stop_grace_period, profiles, runtime: nvidia / GPU reservations, the
    frontend service, and every other environment entry unchanged.

    Validates: Requirements 3.1
    """
    golden = _load_golden(COMPOSE_GOLDEN)
    masked = mask_hf_home(_load_yaml(COMPOSE_PATH))
    assert masked == golden, (
        "PRESERVATION REGRESSION (Property 2): src/docker-compose.yaml "
        "changed outside the intended edit (adding HF_HOME to the backend "
        "services' environment). Only HF_HOME entries in {} may differ "
        "from the recorded golden.".format(list(BACKEND_SERVICES)))


# ---------------------------------------------------------------------------
# Test 2: property-based env preservation (Requirement 3.1)
# ---------------------------------------------------------------------------

def _golden_env_pairs():
    """All (service, environment-entry) pairs from the unfixed golden."""
    golden = _load_golden(COMPOSE_GOLDEN)
    pairs = []
    for name, service in (golden.get("services") or {}).items():
        environment = service.get("environment") or []
        if isinstance(environment, dict):
            environment = [
                key if value is None else "{}={}".format(key, value)
                for key, value in environment.items()
            ]
        for entry in environment:
            pairs.append((name, entry))
    assert pairs, "golden records no environment entries - regenerate it"
    return pairs


@settings(max_examples=100, deadline=None)
@given(pair=st.deferred(lambda: st.sampled_from(_golden_env_pairs())))
def test_every_golden_env_entry_survives_verbatim(pair):
    """Property 2 (Requirement 3.1): for ALL (service, environment-entry)
    pairs drawn from the unfixed golden, the entry is still present
    VERBATIM in the current compose file's service environment - catching
    reordering-with-loss and entry rewrites that a set-comparison or a
    spot-check would miss.

    Validates: Requirements 3.1
    """
    service_name, entry = pair
    compose = _load_yaml(COMPOSE_PATH)
    service = (compose.get("services") or {}).get(service_name)
    assert service is not None, (
        "PRESERVATION REGRESSION (Property 2): service '{}' from the "
        "unfixed golden is missing from src/docker-compose.yaml"
        .format(service_name))
    environment = service.get("environment") or []
    if isinstance(environment, dict):
        environment = [
            key if value is None else "{}={}".format(key, value)
            for key, value in environment.items()
        ]
    assert entry in [str(e) for e in environment], (
        "PRESERVATION REGRESSION (Property 2): environment entry {!r} of "
        "service '{}' (recorded on the unfixed tree) is no longer present "
        "verbatim in src/docker-compose.yaml".format(entry, service_name))


# ---------------------------------------------------------------------------
# Test 3: cleanup leaves the HF cache alone (Requirement 3.3)
# ---------------------------------------------------------------------------

MODEL_NAME = "qwen25-vl-7b-awq"


def _snapshot(root):
    """{relative path: sha256 or "<dir>"} for every entry under `root`."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for dirname in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, dirname), root)
            entries[rel] = "<dir>"
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            entries[os.path.relpath(path, root)] = _sha256(path)
    return entries


def _populate(root, layout):
    """Create `layout` ({relative file path: bytes}) under `root`."""
    for rel, content in layout.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)


def _run_cleanup_against(aws_dda, cache_layout):
    """Build a fake /aws_dda tree (staged repo + leftover staging sibling +
    sibling hf_cache carrying `cache_layout`), run vllm_model_prep.cleanup
    with the network unload stubbed, and return (exit_code, staged_dir,
    leftover_dir, cache_snapshot_before, cache_snapshot_after)."""
    model_repo = os.path.join(aws_dda, "dda_triton", "vllm_model_repo")
    staged_dir = os.path.join(model_repo, MODEL_NAME)
    leftover_dir = os.path.join(
        model_repo, "{}{}-abc123".format(prep._STAGING_PREFIX, MODEL_NAME))
    hf_cache = os.path.join(aws_dda, "hf_cache")

    _populate(staged_dir, {
        "config.pbtxt": b'backend: "vllm"\n',
        os.path.join("1", "model.json"):
            b'{"model": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ"}\n',
    })
    _populate(leftover_dir, {"partial.bin": b"leftover staging temp"})
    _populate(hf_cache, cache_layout)

    before = _snapshot(hf_cache)
    args = Namespace(model_name=MODEL_NAME,
                     component_name="model-vllm-" + MODEL_NAME,
                     cleanup=True)
    with mock.patch.object(prep, "VLLM_MODEL_DIR", model_repo), \
            mock.patch.object(prep, "request_unload",
                              lambda *a, **k: True):
        exit_code = prep.cleanup(args)
    after = _snapshot(hf_cache)
    return exit_code, staged_dir, leftover_dir, before, after


def test_cleanup_removes_staged_repo_and_leaves_hf_cache_untouched(tmp_path):
    """Property 2 (Requirement 3.3): --cleanup removes the staged repo dir
    (and leftover staging temp siblings) under dda_triton/vllm_model_repo
    and leaves the sibling hf_cache tree byte-identical - the persistent
    HF cache the fix introduces must never be deleted by component
    Shutdown.

    Validates: Requirements 3.3
    """
    cache_layout = {
        os.path.join("hub", "models--Qwen--Qwen2.5-VL-7B-Instruct-AWQ",
                     "snapshots", "d3f0f0f0", "config.json"):
            b'{"architectures": ["Qwen2_5_VLForConditionalGeneration"]}\n',
        os.path.join("hub", "models--Qwen--Qwen2.5-VL-7B-Instruct-AWQ",
                     "snapshots", "d3f0f0f0", "model.safetensors"):
            b"\x00" * 256,
        os.path.join("hub", "models--Qwen--Qwen2.5-VL-7B-Instruct-AWQ",
                     "refs", "main"): b"d3f0f0f0\n",
        os.path.join("hub", "version.txt"): b"1\n",
        "token": b"hf_dummy_token\n",
    }
    exit_code, staged_dir, leftover_dir, before, after = \
        _run_cleanup_against(str(tmp_path / "aws_dda"), cache_layout)

    assert exit_code == 0
    assert not os.path.exists(staged_dir), (
        "cleanup must remove the staged repo directory (golden behavior "
        "recorded on the unfixed tree)")
    assert not os.path.exists(leftover_dir), (
        "cleanup must sweep leftover staging temp siblings (golden "
        "behavior recorded on the unfixed tree)")
    assert after == before, (
        "PRESERVATION REGRESSION (Property 2 / Requirement 3.3): cleanup "
        "modified the sibling hf_cache tree; it must remove ONLY the "
        "staged Triton repo, never the HF cache")


_CACHE_NAME_COMPONENT = st.from_regex(
    r"[A-Za-z0-9]([A-Za-z0-9._-]{0,10}[A-Za-z0-9])?", fullmatch=True)
_FILE_CONTENT = st.binary(min_size=0, max_size=64)


@st.composite
def hf_cache_layouts(draw):
    """A random HF hub cache tree: 1-3 models--{org}--{name} dirs each
    carrying a snapshot file, a ref, and a blob - the layouts
    huggingface_hub actually writes under HF_HOME/hub."""
    layout = {}
    n_models = draw(st.integers(min_value=1, max_value=3))
    for _ in range(n_models):
        org = draw(_CACHE_NAME_COMPONENT)
        name = draw(_CACHE_NAME_COMPONENT)
        model_dir = os.path.join("hub", "models--{}--{}".format(org, name))
        revision = draw(st.from_regex(r"[0-9a-f]{8}", fullmatch=True))
        filename = draw(_CACHE_NAME_COMPONENT)
        layout[os.path.join(model_dir, "snapshots", revision, filename)] = \
            draw(_FILE_CONTENT)
        layout[os.path.join(model_dir, "refs", "main")] = \
            (revision + "\n").encode()
        layout[os.path.join(model_dir, "blobs", revision * 8)] = \
            draw(_FILE_CONTENT)
    return layout


@settings(max_examples=25, deadline=None)
@given(cache_layout=hf_cache_layouts())
def test_cleanup_leaves_any_generated_hf_cache_layout_byte_identical(
        cache_layout):
    """Property 2 (Requirement 3.3), property-based: for ANY generated
    models--{org}--{name} HF cache layout placed beside the staged model
    repo, cleanup removes the staged repo and leaves the cache tree
    byte-identical (same entries, same content hashes).

    Validates: Requirements 3.3
    """
    aws_dda = tempfile.mkdtemp(prefix="hf-cache-preservation-")
    try:
        exit_code, staged_dir, _, before, after = \
            _run_cleanup_against(aws_dda, cache_layout)
    finally:
        shutil.rmtree(aws_dda, ignore_errors=True)

    assert exit_code == 0
    assert not os.path.exists(staged_dir)
    assert after == before, (
        "PRESERVATION REGRESSION (Property 2 / Requirement 3.3): cleanup "
        "modified the sibling hf_cache tree for a generated layout; it "
        "must remove ONLY the staged Triton repo, never the HF cache")


# ---------------------------------------------------------------------------
# Test 4: no code drift (Requirement 3.2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel_path", CODE_BASELINE_FILES)
def test_no_code_drift_from_unfixed_baseline(rel_path):
    """Property 2 (Requirement 3.2): this fix is compose-only -
    vllm_model_prep.py (the S3 --weights_path rewrite, staging, load
    classification, --cleanup) and the recipe variants must be
    byte-identical to the sha256 baseline recorded on the unfixed tree.

    Validates: Requirements 3.2
    """
    baseline = _load_golden(CODE_BASELINE_GOLDEN)
    assert rel_path in baseline, (
        "code baseline golden lacks an entry for {} - regenerate it on a "
        "KNOWN-GOOD tree".format(rel_path))
    actual = _sha256(os.path.join(REPO_ROOT, rel_path))
    assert actual == baseline[rel_path], (
        "PRESERVATION REGRESSION (Property 2 / Requirement 3.2): {} "
        "drifted from the unfixed baseline (sha256 {} != recorded {}). "
        "The vllm-hf-cache-persistence fix is compose-only; no code or "
        "recipe change is allowed.".format(
            rel_path, actual, baseline[rel_path]))


# ---------------------------------------------------------------------------
# Golden regeneration (intentional, reviewed changes only)
# ---------------------------------------------------------------------------

def _regenerate():
    os.makedirs(GOLDENS_DIR, exist_ok=True)
    masked = mask_hf_home(_load_yaml(COMPOSE_PATH))
    with open(_golden_path(COMPOSE_GOLDEN), "w", encoding="utf-8") as f:
        json.dump(masked, f, indent=2, sort_keys=True, ensure_ascii=True)
        f.write("\n")
    print("wrote {}".format(_golden_path(COMPOSE_GOLDEN)))

    baseline = {rel: _sha256(os.path.join(REPO_ROOT, rel))
                for rel in CODE_BASELINE_FILES}
    with open(_golden_path(CODE_BASELINE_GOLDEN), "w",
              encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, sort_keys=True, ensure_ascii=True)
        f.write("\n")
    print("wrote {}".format(_golden_path(CODE_BASELINE_GOLDEN)))


if __name__ == "__main__":
    import sys
    if "--regenerate" in sys.argv:
        _regenerate()
    else:
        print("usage: python3 {} --regenerate".format(sys.argv[0]))
