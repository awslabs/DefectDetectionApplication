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
"""JP7 vLLM layer static convention checks (jp7-vllm-enablement).

Text-only structural checks (the ``test_jp7_digest_equality.py`` convention:
no docker, no subprocess) over the two artifacts of the JP7 from-source vLLM
enablement:

* ``src/backend/Dockerfile.jp7`` -- vLLM enabled by default (``VLLM_ENABLE=1``)
  with ``VLLM_ENABLE`` as the SOLE gate (legacy ``VLLM_SPEC``/``VLLM_INDEX_URL``
  ARGs removed, no ``VLLM_USE_V1`` ENV), the layer order
  onnxruntime -> torch -> vLLM -> verification gates, the exact cu130 torch
  pins, and one named import-gate check per Classic_Engine_API symbol.
* ``src/backend/edge_ml1_p_camera_management/install_vllm_gpu.sh`` -- exists
  and is executable, pins ``VLLM_VERSION=v0.11.2`` / ``CUDA_ARCHITECTURES=11.0``
  with env-var overrides, caps parallel jobs at min(nproc, 6), stages the
  built wheel to ``/opt/vllm-wheels`` BEFORE the work-dir cleanup, runs the
  ``import vllm`` check BEFORE the per-symbol checks, builds against the
  installed torch via ``use_existing_torch.py``, and is referenced by no
  non-JP7 Dockerfile.

Also asserts the script's build-baseline treatment: UNTRACKED by the sha256
baseline suite (the ``install_onnxruntime_gpu.sh`` treatment recorded in the
design), so no ``install_vllm_gpu.sh.sha256.txt`` baseline may exist.

Import-light so it runs under
``pytest test/backend-test/backend_jammy_pkgs/ --noconftest``.

**Validates: Requirements 1.1, 1.2, 1.4, 1.7, 1.9, 2.1, 2.6, 3.1, 3.2, 3.4,
3.7, 3.9, 5.3, 5.9**

Run (finite, non-watch):
    python3 -m pytest test/backend-test/backend_jammy_pkgs/test_jp7_vllm_layer.py \
        -p no:cacheprovider --noconftest -v
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))

DOCKERFILE_JP7_REL = os.path.join("src", "backend", "Dockerfile.jp7")
SCRIPT_REL = os.path.join(
    "src", "backend", "edge_ml1_p_camera_management", "install_vllm_gpu.sh"
)
BASELINES_DIR = os.path.join(HERE, "baselines")


def _read(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _noncomment_lines(text):
    """The lines of ``text`` that are not comment lines (Dockerfile comments
    and shell comments both start the line with ``#``)."""
    return [
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    ]


def _logical_instructions(text):
    """Join backslash-continued physical lines into logical Dockerfile
    instructions (comment lines dropped), so a multi-line RUN can be
    inspected as one string."""
    logical = []
    buf = None
    for line in text.splitlines():
        if buf is None and line.lstrip().startswith("#"):
            continue
        buf = line if buf is None else buf + "\n" + line
        if line.rstrip().endswith("\\"):
            continue
        logical.append(buf)
        buf = None
    if buf is not None:
        logical.append(buf)
    return logical


def _vllm_layer_run(dockerfile_text):
    """The single logical RUN instruction that invokes install_vllm_gpu.sh."""
    runs = [
        ins for ins in _logical_instructions(dockerfile_text)
        if ins.lstrip().startswith("RUN") and "install_vllm_gpu.sh" in ins
    ]
    assert len(runs) == 1, (
        f"{DOCKERFILE_JP7_REL}: expected exactly one RUN invoking "
        f"install_vllm_gpu.sh, found {len(runs)}"
    )
    return runs[0]


# ── Dockerfile.jp7 checks ────────────────────────────────────────────────────

# Validates: Requirements 3.1 -- vLLM enabled by default.
def test_vllm_enable_defaults_to_1():
    """Dockerfile.jp7 declares ``ARG VLLM_ENABLE=1`` (enabled-by-default)."""
    text = _read(DOCKERFILE_JP7_REL)
    assert re.search(r"(?m)^ARG VLLM_ENABLE=1\s*$", text), (
        f"{DOCKERFILE_JP7_REL}: missing 'ARG VLLM_ENABLE=1' -- the vLLM "
        f"layer must default to enabled (Requirement 3.1)"
    )


# Validates: Requirements 3.2 -- legacy build args removed.
def test_legacy_vllm_spec_and_index_url_args_removed():
    """No non-comment line declares or uses VLLM_SPEC / VLLM_INDEX_URL: the
    legacy prebuilt-wheel args are removed, so an empty/absent value cannot
    alter the vLLM layer's gating."""
    for line in _noncomment_lines(_read(DOCKERFILE_JP7_REL)):
        assert "VLLM_SPEC" not in line and "VLLM_INDEX_URL" not in line, (
            f"{DOCKERFILE_JP7_REL}: legacy VLLM_SPEC/VLLM_INDEX_URL must be "
            f"removed (Requirement 3.2), found: {line!r}"
        )


# Validates: Requirements 3.2 -- no V0-era env var (design Research #5).
def test_no_vllm_use_v1_env():
    """No non-comment line sets VLLM_USE_V1: v0.11.2 removed the V0 engine
    and its selection env var (the JP6 ``ENV VLLM_USE_V1=0`` is a deliberate
    per-JetPack difference that must NOT be copied here)."""
    for line in _noncomment_lines(_read(DOCKERFILE_JP7_REL)):
        assert "VLLM_USE_V1" not in line, (
            f"{DOCKERFILE_JP7_REL}: VLLM_USE_V1 must not be set on JP7 "
            f"(v0.11.2 has no V0 engine), found: {line!r}"
        )


# Validates: Requirements 3.2, 3.7 -- sole gate with skip echo.
def test_vllm_layer_gates_solely_on_vllm_enable_with_skip_echo():
    """The vLLM layer RUN gates solely on ``[ "$VLLM_ENABLE" = "1" ]``,
    passes VLLM_VERSION through to the script, and its else-branch echoes
    the skip with the VLLM_ENABLE value."""
    run = _vllm_layer_run(_read(DOCKERFILE_JP7_REL))
    conds = re.findall(r"if\s+\[\s*(.*?)\s*\]\s*;", run)
    assert conds == ['"$VLLM_ENABLE" = "1"'], (
        f"{DOCKERFILE_JP7_REL}: the vLLM layer RUN must gate on exactly one "
        f"condition, '\"$VLLM_ENABLE\" = \"1\"' (the SOLE gate, Requirement "
        f"3.2), got conditions: {conds!r}"
    )
    assert (
        "VLLM_VERSION=${VLLM_VERSION} "
        "./edge_ml1_p_camera_management/install_vllm_gpu.sh" in run
    ), (
        f"{DOCKERFILE_JP7_REL}: the vLLM layer must invoke "
        f"install_vllm_gpu.sh with VLLM_VERSION passthrough, got:\n{run}"
    )
    assert 'echo "vLLM layer skipped (VLLM_ENABLE=${VLLM_ENABLE})"' in run, (
        f"{DOCKERFILE_JP7_REL}: the vLLM layer's else-branch must echo the "
        f"skip with the VLLM_ENABLE value (Requirement 3.7), got:\n{run}"
    )


# Validates: Requirements 2.1, 3.9 -- layer order.
def test_layer_order_onnxruntime_then_torch_then_vllm_then_gates():
    """Layer order in Dockerfile.jp7: onnxruntime GPU build -> torch cu130
    layer -> vLLM layer -> vLLM import gate -> dependency-consistency gate,
    so the two hours-long compiles stay independently cacheable and vLLM
    compiles against the already-installed torch."""
    text = _read(DOCKERFILE_JP7_REL)
    markers = [
        ("onnxruntime GPU layer",
         "./edge_ml1_p_camera_management/install_onnxruntime_gpu.sh"),
        ("torch cu130 install layer",
         "pip install --no-cache-dir --index-url ${TORCH_INDEX_URL}"),
        ("vLLM layer",
         "./edge_ml1_p_camera_management/install_vllm_gpu.sh"),
        ("vLLM import gate",
         "ERROR: GPU-dependent package failed to import: vllm"),
        ("dependency-consistency gate",
         "dependency consistency gate skipped"),
    ]
    indices = []
    for name, marker in markers:
        idx = text.find(marker)
        assert idx >= 0, (
            f"{DOCKERFILE_JP7_REL}: missing the {name} (marker {marker!r})"
        )
        indices.append((name, idx))
    for (prev_name, prev_idx), (name, idx) in zip(indices, indices[1:]):
        assert prev_idx < idx, (
            f"{DOCKERFILE_JP7_REL}: layer order violated -- the {prev_name} "
            f"must precede the {name} (Requirements 2.1, 3.9)"
        )


# Validates: Requirements 2.6 -- exact CUDA-dependent wheel pins.
def test_exact_torch_pins_and_cu130_index_url():
    """The torch layer pins the exact verified cu130 stack and installs it
    from the official PyTorch cu130 index (no unpinned transitive installs
    from default PyPI for the CUDA-dependent chain)."""
    text = _read(DOCKERFILE_JP7_REL)
    for arg_line in (
        'ARG TORCH_SPEC="torch==2.9.0+cu130"',
        'ARG TORCHVISION_SPEC="torchvision==0.24.0"',
        'ARG TORCHAUDIO_SPEC="torchaudio==2.9.0"',
        'ARG TRITON_SPEC="triton==3.5.0"',
        'ARG TORCH_INDEX_URL="https://download.pytorch.org/whl/cu130"',
    ):
        assert arg_line in text, (
            f"{DOCKERFILE_JP7_REL}: missing exact pin line (Requirement "
            f"2.6): {arg_line!r}"
        )


# Validates: Requirements 3.4 -- per-symbol named gate checks.
def test_import_gate_names_each_classic_engine_api_symbol():
    """The extended GPU import gate carries one named check per
    Classic_Engine_API symbol the Companion_Runtime uses, so a failure names
    the specific symbol in the build log."""
    text = _read(DOCKERFILE_JP7_REL)
    for symbol in (
        "AsyncEngineArgs",
        "SamplingParams",
        "AsyncLLMEngine.from_engine_args",
        "AsyncLLMEngine.shutdown_background_loop",
        "AsyncLLMEngine.errored",
    ):
        marker = f"ERROR: missing vLLM symbol: {symbol}"
        assert marker in text, (
            f"{DOCKERFILE_JP7_REL}: import gate missing the named check for "
            f"{symbol} (Requirement 3.4): expected {marker!r}"
        )
    assert "ERROR: GPU-dependent package failed to import: vllm" in text
    assert "ERROR: GPU-dependent package failed to import: torch" in text


# ── install_vllm_gpu.sh checks ──────────────────────────────────────────────

# Validates: Requirements 1.1 -- the vLLM_Build_Script exists, executable.
def test_build_script_exists_and_is_executable():
    """install_vllm_gpu.sh exists under src/backend/ and carries the
    executable bit (the Dockerfile invokes it directly)."""
    path = os.path.join(REPO_ROOT, SCRIPT_REL)
    assert os.path.isfile(path), f"{SCRIPT_REL}: script does not exist"
    assert os.access(path, os.X_OK), f"{SCRIPT_REL}: script is not executable"


# Validates: Requirements 1.2 -- pinned defaults, env-var overridable.
def test_script_env_defaults():
    """The script defaults VLLM_VERSION to v0.11.2 and CUDA_ARCHITECTURES to
    11.0 (Thor sm_110), each via the ``${VAR:-default}`` env-override form."""
    text = _read(SCRIPT_REL)
    assert 'VLLM_VERSION="${VLLM_VERSION:-v0.11.2}"' in text, (
        f"{SCRIPT_REL}: missing the env-overridable v0.11.2 default "
        f"(Requirement 1.2)"
    )
    assert 'CUDA_ARCHITECTURES="${CUDA_ARCHITECTURES:-11.0}"' in text, (
        f"{SCRIPT_REL}: missing the env-overridable 11.0 (Thor sm_110) "
        f"default (Requirement 1.2)"
    )


# Validates: Requirements 1.4 -- min(nproc, 6) memory-safety job cap.
def test_script_caps_build_jobs_at_min_nproc_6():
    """The default parallel job count is min(nproc, 6) -- the
    install_onnxruntime_gpu.sh memory-safety cap -- overridable via
    VLLM_BUILD_JOBS."""
    text = _read(SCRIPT_REL)
    assert (
        'VLLM_BUILD_JOBS="${VLLM_BUILD_JOBS:-$(( NPROC > 6 ? 6 : NPROC ))}"'
        in text
    ), (
        f"{SCRIPT_REL}: missing the env-overridable min(nproc, 6) job cap "
        f"(Requirement 1.4)"
    )
    assert re.search(r"(?m)^NPROC=\$\(nproc\)\s*$", text), (
        f"{SCRIPT_REL}: NPROC must come from nproc (Requirement 1.4)"
    )


# Validates: Requirements 1.9 -- staging precedes the work-dir cleanup.
def test_script_stages_wheel_to_opt_vllm_wheels_before_workdir_cleanup():
    """The built wheel is staged at /opt/vllm-wheels (fixed directory
    outside the build tree) BEFORE the final ``rm -rf "${WORK_DIR}"``
    cleanup, preserving the hours-long build output in the image."""
    text = _read(SCRIPT_REL)
    idx_mkdir = text.find("mkdir -p /opt/vllm-wheels")
    idx_cp = text.find("/opt/vllm-wheels/")
    assert idx_mkdir >= 0, (
        f"{SCRIPT_REL}: missing 'mkdir -p /opt/vllm-wheels' staging "
        f"(Requirement 1.9)"
    )
    assert idx_cp >= 0, (
        f"{SCRIPT_REL}: missing the wheel copy into /opt/vllm-wheels/ "
        f"(Requirement 1.9)"
    )
    # The script also clears WORK_DIR before cloning; the cleanup that must
    # come AFTER staging is the LAST rm -rf of the work dir.
    idx_cleanup = text.rfind('rm -rf "${WORK_DIR}"')
    assert idx_cleanup >= 0, (
        f"{SCRIPT_REL}: missing the work-dir cleanup 'rm -rf \"${{WORK_DIR}}\"'"
    )
    assert max(idx_mkdir, idx_cp) < idx_cleanup, (
        f"{SCRIPT_REL}: the wheel must be staged to /opt/vllm-wheels BEFORE "
        f"the final work-dir rm -rf (Requirement 1.9)"
    )


# Validates: Requirements 1.7 -- import check first, then symbol checks.
def test_script_import_vllm_check_precedes_per_symbol_checks():
    """Post-install verification runs ``import vllm`` ALONE first (an import
    failure exits with the import error before any symbol check), then one
    named check per Classic_Engine_API symbol."""
    text = _read(SCRIPT_REL)
    idx_import = text.find(
        "ERROR: post-install verification failed: import vllm"
    )
    assert idx_import >= 0, (
        f"{SCRIPT_REL}: missing the standalone 'import vllm' verification "
        f"(Requirement 1.7)"
    )
    symbol_indices = []
    for symbol in (
        "AsyncEngineArgs",
        "SamplingParams",
        "AsyncLLMEngine.from_engine_args",
        "AsyncLLMEngine.generate",
        "AsyncLLMEngine.shutdown_background_loop",
    ):
        marker = f"ERROR: missing vLLM symbol: {symbol}"
        idx = text.find(marker)
        assert idx >= 0, (
            f"{SCRIPT_REL}: missing the named per-symbol check for {symbol} "
            f"(Requirement 1.6): expected {marker!r}"
        )
        symbol_indices.append(idx)
    assert idx_import < min(symbol_indices), (
        f"{SCRIPT_REL}: the 'import vllm' check must PRECEDE every "
        f"per-symbol check (Requirement 1.7)"
    )


# Validates: Requirements 2.1 -- build against the installed Torch_Pin.
def test_script_invokes_use_existing_torch():
    """The script runs vLLM's use_existing_torch.py so the build compiles
    against -- and the wheel metadata never demands -- any torch other than
    the installed Torch_Pin."""
    text = _read(SCRIPT_REL)
    assert re.search(r"(?m)^\$\{PYBIN\} use_existing_torch\.py\s*$", text), (
        f"{SCRIPT_REL}: missing the '${{PYBIN}} use_existing_torch.py' "
        f"invocation (Requirement 2.1)"
    )


# Validates: Requirements 5.3 -- no non-JP7 Dockerfile references the script.
def test_no_non_jp7_dockerfile_references_build_script():
    """Repo-wide over src/: the ONLY Dockerfile referencing
    install_vllm_gpu.sh is src/backend/Dockerfile.jp7 -- JP5/JP6/JP4/x86 and
    every other Dockerfile stay vLLM-build-free."""
    allowed = os.path.join(REPO_ROOT, DOCKERFILE_JP7_REL)
    offenders = []
    src_root = os.path.join(REPO_ROOT, "src")
    for dirpath, dirnames, filenames in os.walk(src_root):
        # Vendored JS trees carry unrelated third-party Dockerfiles.
        dirnames[:] = [d for d in dirnames if d != "node_modules"]
        for name in filenames:
            if not name.startswith("Dockerfile"):
                continue
            path = os.path.join(dirpath, name)
            if os.path.normpath(path) == os.path.normpath(allowed):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                if "install_vllm_gpu" in fh.read():
                    offenders.append(os.path.relpath(path, REPO_ROOT))
    assert not offenders, (
        f"install_vllm_gpu.sh must be referenced ONLY by "
        f"{DOCKERFILE_JP7_REL} (Requirement 5.3); also referenced by: "
        f"{offenders}"
    )


# Validates: Requirements 5.9 -- untracked baseline treatment.
def test_no_sha256_baseline_registered_for_build_script():
    """install_vllm_gpu.sh is UNTRACKED by the sha256 baseline suite (the
    install_onnxruntime_gpu.sh treatment recorded in the design) -- no
    baseline file for it may exist in this suite's baselines/."""
    offenders = [
        name for name in os.listdir(BASELINES_DIR)
        if "install_vllm_gpu" in name
    ]
    assert not offenders, (
        f"install_vllm_gpu.sh must carry NO sha256 baseline (the untracked "
        f"install_onnxruntime_gpu.sh treatment, Requirement 5.9); found: "
        f"{offenders}"
    )
