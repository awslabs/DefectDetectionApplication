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
"""Unit assertions for the JP7 ptxas fix and the reclaim hardening
(spec: vllm-jp7-engine-cuda-init, task 5.2).

Durable regression guards, deliberately overlapping exploration cases
1/3 in ``test_exploration_fork_cuda_init.py`` (that file is the
historical bug-condition record; this one is the long-lived unit
suite):

* The JP7 image contract: ``ENV TRITON_PTXAS_PATH=/opt/dda/cuda-bin/ptxas``
  appears exactly once in ``src/backend/Dockerfile.jp7`` and NOWHERE
  else (compose — shared with JP6 — recipe variants, Dockerfile.jp6,
  Dockerfile.jp5): triton's BUNDLED ptxas (CUDA 12.8) cannot codegen
  for Thor's ``sm_110a``; the env var points triton at the image's
  system CUDA 13.x ptxas and is JP7-image-scoped by construction
  (preservation 3.8).
* ``_reclaim_gpu_memory`` example-based behavior: torch CUDA
  initialized → ``empty_cache()`` called; torch missing → silent
  return; ``empty_cache()`` raising → swallowed and logged.

Follows the sibling convention of
``test/backend-test/vllm_runtime/test_manager_memory_reclaim.py``:
``sys.path`` shim to ``src/backend``, runnable in the flask-app
container, no vllm/torch/GPU dependency.

Validates: Requirements 2.1, 2.2, 3.1, 3.2, 3.6, 3.8
"""
import logging
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

sys.path.insert(0, str(REPO_ROOT / "src" / "backend"))

from vllm_runtime.manager import VllmRuntimeManager  # noqa: E402

DOCKERFILE_JP7 = REPO_ROOT / "src" / "backend" / "Dockerfile.jp7"
DOCKERFILE_JP6 = REPO_ROOT / "src" / "backend" / "Dockerfile.jp6"
DOCKERFILE_JP5 = REPO_ROOT / "src" / "backend" / "Dockerfile.jp5"
DOCKER_COMPOSE = REPO_ROOT / "src" / "docker-compose.yaml"

#: Every Greengrass recipe variant at the repo root (build-custom.sh copies
#: the selected one to greengrass-build/recipes).
RECIPE_VARIANTS = sorted(REPO_ROOT.glob("recipe*.yaml"))

TRITON_PTXAS_ENV_LINE = re.compile(
    r"^\s*ENV\s+TRITON_PTXAS_PATH=/opt/dda/cuda-bin/ptxas\s*$",
    re.MULTILINE,
)


class _FakeCuda:
    """Recording ``torch.cuda`` stand-in for the reclaim unit tests."""

    def __init__(self, initialized, empty_cache_error=None):
        self.calls = []
        self._initialized = initialized
        self._empty_cache_error = empty_cache_error

    def is_initialized(self):
        self.calls.append("is_initialized")
        return self._initialized

    def is_available(self):
        self.calls.append("is_available")
        return self._initialized

    def empty_cache(self):
        self.calls.append("empty_cache")
        if self._empty_cache_error is not None:
            raise self._empty_cache_error


class _FakeTorch:
    """Module-shaped stand-in for the manager's lazy ``import torch``."""

    def __init__(self, cuda):
        self.cuda = cuda


class TestTritonPtxasPathImageContract:
    """The env var is declared exactly once, in the JP7 image only."""

    def test_dockerfile_jp7_declares_the_env_exactly_once(self):
        # Validates: Requirements 2.1, 2.2
        matches = TRITON_PTXAS_ENV_LINE.findall(DOCKERFILE_JP7.read_text())
        assert len(matches) == 1, (
            "Dockerfile.jp7 must declare 'ENV "
            "TRITON_PTXAS_PATH=/opt/dda/cuda-bin/ptxas' exactly once "
            "(found {}): without it triton's bundled CUDA 12.8 ptxas "
            "rejects Thor's sm_110a and any Triton-JIT-compiling vLLM "
            "model dies with PTXASError during the engine's profile "
            "run".format(len(matches))
        )

    def test_no_other_config_source_sets_the_variable(self):
        # Validates: Requirements 3.8
        assert RECIPE_VARIANTS, "no recipe variants found at the repo root"
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for path in (
                [DOCKER_COMPOSE, DOCKERFILE_JP6, DOCKERFILE_JP5]
                + RECIPE_VARIANTS
            )
            if "TRITON_PTXAS_PATH" in path.read_text()
        ]
        assert offenders == [], (
            "TRITON_PTXAS_PATH is JP7-image-scoped by construction and "
            "must not appear in compose (shared with JP6), the recipes, "
            "Dockerfile.jp6, or Dockerfile.jp5 (preservation 3.8); "
            "found in: {}".format(offenders)
        )


class TestReclaimGpuMemoryUnits:
    """Example-based ``_reclaim_gpu_memory`` behavior on the fixed tree."""

    def test_initialized_cuda_empties_cache(self, monkeypatch):
        # Validates: Requirements 3.1, 3.6
        cuda = _FakeCuda(initialized=True)
        monkeypatch.setitem(sys.modules, "torch", _FakeTorch(cuda))

        VllmRuntimeManager._reclaim_gpu_memory("qwen")

        assert cuda.calls.count("empty_cache") == 1, (
            "with torch CUDA initialized in-process (the JP6/V0 engine "
            "case), reclaim must call empty_cache() exactly once; "
            "cuda.* calls recorded: {}".format(cuda.calls)
        )

    def test_missing_torch_returns_silently(self, monkeypatch):
        # Validates: Requirements 3.2
        # sys.modules['torch'] = None makes `import torch` raise
        # ModuleNotFoundError deterministically, independent of the host.
        monkeypatch.setitem(sys.modules, "torch", None)

        assert VllmRuntimeManager._reclaim_gpu_memory("any-model") is None

    def test_empty_cache_error_is_swallowed_and_logged(
            self, monkeypatch, caplog):
        # Validates: Requirements 3.6
        cuda = _FakeCuda(
            initialized=True,
            empty_cache_error=RuntimeError("CUDA error: device lost"),
        )
        monkeypatch.setitem(sys.modules, "torch", _FakeTorch(cuda))

        with caplog.at_level(logging.ERROR, logger="vllm_runtime.manager"):
            result = VllmRuntimeManager._reclaim_gpu_memory("qwen")

        assert result is None, (
            "reclaim is strictly best-effort: an empty_cache() error "
            "must never propagate into unload/fail handling"
        )
        assert any(
            "Error reclaiming CUDA memory" in record.message
            for record in caplog.records
        ), "the swallowed empty_cache() error must be logged"
