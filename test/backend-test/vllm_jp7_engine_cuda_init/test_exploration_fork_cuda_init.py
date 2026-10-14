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
"""Bug condition exploration suite (spec: vllm-jp7-engine-cuda-init).

FILENAME NOTE (historical): this file keeps its original name from the
REFUTED fork-after-CUDA-init hypothesis — the on-device chronology in
bugfix.md's "Re-hypothesis (task 3 outcome)" chain proved the original
``cudaErrorDevicesUnavailable`` failure ENVIRONMENTAL (nvargus/Argus
driver defect, cleared by a daemon restart) and surfaced the REAL image
defect this suite now pins: triton's BUNDLED ptxas (CUDA 12.8, V12.8.93)
cannot codegen for Thor's ``sm_110a`` (``ptxas fatal : Value 'sm_110a'
is not defined for option 'gpu-name'`` → PTXASError during the engine's
profile run → model FAILED → 409 → component BROKEN → deployment
rollback). The file is NOT renamed so the task 1 record stays intact
(tasks.md task 4.4).

Re-scoped cases (design.md "Exploratory Bug Condition Checking",
reworked 2026-08-15):

* Case 1 — the causal configuration of the validated fix:
  ``src/backend/Dockerfile.jp7`` declares
  ``ENV TRITON_PTXAS_PATH=/opt/dda/cuda-bin/ptxas`` exactly once,
  pointing triton at the image's system CUDA 13.x ptxas (which accepts
  ``sm_110a``); no other config source (compose — shared with JP6 — or
  the recipe variants) sets the variable. FAILED on unfixed code (the
  line was absent, which IS the bug condition); passes on the fixed
  tree. The mooted spawn ENV is intentionally NOT asserted present.
* Case 2 — reclaim hygiene (defect 1.3, optional hardening, unchanged):
  the manager's failure handler (``_fail`` → ``_reclaim_gpu_memory``)
  must not probe ``torch.cuda.is_available()`` — a driver-INITIALIZING
  call — in a process whose torch CUDA was never initialized. FAILED on
  unfixed code; passes once reclaim gates on the pure state read
  ``torch.cuda.is_initialized()``.
* Case 3 — F(X) guard, PASSES on unfixed code and must NEVER be
  inverted: JP6 pins the V0 in-process engine (``ENV VLLM_USE_V1=0``,
  no multiproc env var) and JP5 ships vLLM-disabled
  (``ARG VLLM_ENABLE=0``); NEITHER image gains ``TRITON_PTXAS_PATH``
  (preservation 3.8 — the env var is JP7-image-scoped by construction).

Honesty note (tasks.md): no GPU-free test executes ptxas or CUDA. The
behavioral leg (system ptxas → profile run completes → READY) is
validated on hardware: the hot-patch validation (DONE 2026-08-15,
bugfix.md) and the built-component acceptance (task 10).

Follows the sibling convention of
``test/backend-test/vllm_runtime/test_manager_memory_reclaim.py``:
``sys.path`` shim to ``src/backend``, runnable in the flask-app
container, no vllm/torch/GPU dependency.

Validates: Requirements 2.1, 2.2, 2.3, 3.8
"""
import asyncio
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

sys.path.insert(0, str(REPO_ROOT / "src" / "backend"))

from vllm_runtime.manager import (  # noqa: E402
    ModelState,
    VllmRuntimeManager,
)

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


def _stage_repository(model_dir: Path, model_name: str) -> None:
    """Minimal valid Triton_vLLM_Repository (sibling helper shape)."""
    version_dir = model_dir / model_name / "1"
    version_dir.mkdir(parents=True)
    (model_dir / model_name / "config.pbtxt").write_text('backend: "vllm"\n')
    (version_dir / "model.json").write_text("{}")


class _RecordingCuda:
    """Fake ``torch.cuda`` for a process whose torch CUDA was NEVER
    initialized. Records every ``cuda.*`` attribute call so the test can
    prove which probes the failure handler made."""

    def __init__(self):
        self.calls = []

    def is_initialized(self):
        # Pure state read (never initializes CUDA): this process has not
        # touched CUDA, so torch would report False.
        self.calls.append("is_initialized")
        return False

    def is_available(self):
        # THE poisoning probe: on real torch this performs driver-level
        # CUDA initialization in the calling (parent) process.
        self.calls.append("is_available")
        return False

    def __getattr__(self, name):
        # Any other cuda.* attribute: record the call, do nothing.
        def _record(*args, **kwargs):
            self.calls.append(name)
            return None

        return _record


class _FakeTorch:
    """Module-shaped stand-in injected as ``sys.modules['torch']`` so the
    manager's lazy ``import torch`` sees a CUDA-uninitialized process."""

    def __init__(self):
        self.cuda = _RecordingCuda()


class TestCase1TritonPtxasPathDeclaredForJp7:
    """Case 1 — the causal configuration of the validated ptxas fix.

    FAILED ON UNFIXED CODE: Dockerfile.jp7 did not declare
    ``ENV TRITON_PTXAS_PATH=/opt/dda/cuda-bin/ptxas``, so triton fell
    back to its BUNDLED CUDA 12.8 ptxas, which cannot codegen for Thor's
    ``sm_110a`` — any Triton-JIT-compiling vLLM model died with
    PTXASError during profile_run. Passes on the fixed tree.

    Validates: Requirements 2.1, 2.2
    """

    def test_jp7_image_declares_triton_ptxas_path_exactly_once(self):
        content = DOCKERFILE_JP7.read_text()
        matches = TRITON_PTXAS_ENV_LINE.findall(content)
        assert len(matches) == 1, (
            "Dockerfile.jp7 must declare 'ENV "
            "TRITON_PTXAS_PATH=/opt/dda/cuda-bin/ptxas' exactly once "
            "(found {}): without it triton uses its BUNDLED CUDA 12.8 "
            "ptxas, which rejects Thor's sm_110a ('ptxas fatal : Value "
            "'sm_110a' is not defined for option 'gpu-name''), so any "
            "vLLM model whose execution path JIT-compiles a Triton "
            "kernel dies with PTXASError during the engine's profile "
            "run".format(len(matches))
        )

    def test_no_other_config_source_sets_the_variable(self):
        """Documents the input surface: the Dockerfile ENV is the ONLY
        intended declaration point (JP7-image-scoped, mirroring how
        Dockerfile.jp6 scopes VLLM_USE_V1=0). Compose is shared across
        targets and the recipes must stay untouched (preservation 3.8)."""
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for path in [DOCKER_COMPOSE] + RECIPE_VARIANTS
            if "TRITON_PTXAS_PATH" in path.read_text()
        ]
        assert RECIPE_VARIANTS, "no recipe variants found at the repo root"
        assert offenders == [], (
            "TRITON_PTXAS_PATH must not be set outside Dockerfile.jp7 "
            "(would leak onto JP6/JP5 targets); found in: "
            "{}".format(offenders)
        )


class TestCase2FailureHandlerCudaHygiene:
    """Case 2 — reclaim hygiene (defect 1.3, the optional hardening),
    behavioral and GPU-free. Unchanged by the 2026-08-15 re-scope.

    FAILED ON UNFIXED CODE: driving a failed load through ``_fail()``
    made ``_reclaim_gpu_memory`` call ``torch.cuda.is_available()`` — a
    driver-initializing probe — in a process whose torch CUDA was never
    initialized. The fixed reclaim gates on the pure state read
    ``torch.cuda.is_initialized()``, so a re-attempt after failure
    starts from an uncontaminated parent.

    Validates: Requirements 2.3
    """

    def test_failed_load_makes_no_cuda_initializing_call(
            self, tmp_path, monkeypatch):
        fake_torch = _FakeTorch()
        monkeypatch.setitem(sys.modules, "torch", fake_torch)

        def exploding_factory(engine_args):
            raise RuntimeError(
                "Engine core initialization failed. See root cause above. "
                "Failed core proc(s): {}")

        manager = VllmRuntimeManager(
            model_dir=tmp_path, engine_factory=exploding_factory)
        _stage_repository(tmp_path, "qwen3-vl-8b-instruct")

        status = asyncio.run(manager.load("qwen3-vl-8b-instruct"))

        # Sanity (not the assertion under exploration): the failure is
        # isolated to this model with its reason retained (3.4/3.7 shape).
        assert status.state is ModelState.FAILED
        assert "Engine core initialization failed" in status.reason

        # The exploration assertion: the failure handler must not be the
        # first CUDA touch in the process. is_initialized() is a pure
        # state read and is allowed; is_available() driver-initializes
        # CUDA in the parent and must never be called.
        assert "is_available" not in fake_torch.cuda.calls, (
            "defect 1.3 counterexample: _fail() -> _reclaim_gpu_memory() "
            "called torch.cuda.is_available() in a process whose torch "
            "CUDA was never initialized (cuda.* calls recorded: {}). "
            "This driver-initializes CUDA in the backend parent process "
            "on every failure — the hygiene defect the hardening "
            "removes".format(fake_torch.cuda.calls)
        )


class TestCase3Jp6Jp5EngineContractsUnchanged:
    """Case 3 — F(X) guard. PASSES on unfixed code and must NEVER be
    inverted: the fix is JP7-image-scoped by construction. Extended at
    the 2026-08-15 re-scope: neither JP6 nor JP5 gains
    ``TRITON_PTXAS_PATH`` (preservation 3.8).

    Validates: Requirements 3.8 (JP7-only scoping of the fix)
    """

    def test_jp6_pins_v0_in_process_engine(self):
        content = DOCKERFILE_JP6.read_text()
        assert re.search(
            r"^\s*ENV\s+VLLM_USE_V1=0\s*$", content, re.MULTILINE
        ), (
            "Dockerfile.jp6 must keep ENV VLLM_USE_V1=0: JP6's V0 engine "
            "is constructed in-process (vllm 0.9.3, no CUDA subprocess)"
        )

    def test_jp6_declares_no_multiproc_method(self):
        assert "VLLM_WORKER_MULTIPROC_METHOD" not in DOCKERFILE_JP6.read_text(), (
            "Dockerfile.jp6 must NOT gain VLLM_WORKER_MULTIPROC_METHOD: "
            "the JP6 image is untouched by this fix (preservation 3.1)"
        )

    def test_jp6_declares_no_triton_ptxas_path(self):
        assert "TRITON_PTXAS_PATH" not in DOCKERFILE_JP6.read_text(), (
            "Dockerfile.jp6 must NOT gain TRITON_PTXAS_PATH: the env var "
            "is JP7-image-scoped by construction — JP6's vllm 0.9.3 / V0 "
            "cu122 stack takes no analogous env var (preservation 3.8)"
        )

    def test_jp5_keeps_vllm_disabled(self):
        content = DOCKERFILE_JP5.read_text()
        assert re.search(
            r"^\s*ARG\s+VLLM_ENABLE=0\s*$", content, re.MULTILINE
        ), (
            "Dockerfile.jp5 must keep ARG VLLM_ENABLE=0: JP5 ships "
            "without vLLM (preservation 3.2)"
        )
        assert "VLLM_WORKER_MULTIPROC_METHOD" not in content, (
            "Dockerfile.jp5 must NOT gain VLLM_WORKER_MULTIPROC_METHOD"
        )

    def test_jp5_declares_no_triton_ptxas_path(self):
        assert "TRITON_PTXAS_PATH" not in DOCKERFILE_JP5.read_text(), (
            "Dockerfile.jp5 must NOT gain TRITON_PTXAS_PATH: JP5 ships "
            "without vLLM (preservation 3.8)"
        )
