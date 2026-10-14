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
"""Unit tests for the device-side memory budget module (spec:
jp6-vllm-kv-cache-oom-regression, task 3.5 / design File 5).

HONESTY GUARD (binding). Nothing here loads a vLLM engine, allocates GPU
memory or reproduces Jetson unified-memory accounting. Every memory number is
INJECTED through ``read_memory(reader=...)`` or a sparse file tree; what these
tests prove is parsing, sizing math, degradation-to-unverified and message
content — nothing about a GPU. The GPU-only claims live in the [HARDWARE]
H1-H8 tasks.

Covers the four groups task 3.5 names: ``read_memory`` parsing (normal
`/proc/meminfo`, missing keys, garbage, empty), ``estimate_weights_on_disk``
(local dir, HF cache layout, absent, unreadable), ``activation_allowance``
edge cases (zero/tiny weights, ``Decimal`` utilizations, malformed
``limit_mm_per_prompt``, unknown architecture), plus the module's binding
CUDA-free invariant and the refusal-reason contract the prep matches.
"""
import os
import re
from decimal import Decimal
from pathlib import Path

import pytest

from vllm_runtime import memory_budget as mb

from fakes import (
    DEVICE_TOTAL_BYTES,
    GIB,
    INCIDENT_ENGINE_ARGS,
    FakeMeminfoReader,
    disk_usage_of,
    hf_cache_tree,
    meminfo_text,
    weight_tree,
)


# ---------------------------------------------------------------------------
# The binding invariant: pure, stdlib-only, no CUDA by any route
# ---------------------------------------------------------------------------

def test_module_imports_no_torch_no_cuda_no_vllm():
    """A CUDA-initializing probe in the parent backend process poisons every
    subsequently forked child (`vllm-jp7-engine-cuda-init` defect 1.3), so the
    preflight reads the kernel, never the driver.

    _Requirements: 1.10, 2.9_"""
    source = Path(mb.__file__).read_text()
    for forbidden in ("import torch", "import vllm", "from vllm",
                      "cuda.is_available", "pynvml", "nvidia_smi"):
        assert forbidden not in source, (
            "memory_budget acquired a CUDA/vLLM dependency: {!r}".format(
                forbidden))


def test_preflight_refused_marker_is_the_documented_literal():
    """The stable token `vllm_model_prep` matches BEFORE `KV_CACHE_HINT_MARKERS`.

    _Requirements: 2.9_"""
    assert mb.PREFLIGHT_REFUSED_MARKER == "preflight-refused:"


def test_proposed_thresholds_carry_the_design_values():
    """`RECLAIM_TOLERANCE_BYTES` and `THIN_MARGIN_CONCURRENCY` are PROPOSED
    thresholds (design Open question 5), pinned so a silent drift is visible.

    _Requirements: 2.5, 2.7_"""
    assert mb.RECLAIM_TOLERANCE_BYTES == int(0.5 * GIB)
    assert mb.THIN_MARGIN_CONCURRENCY == 2.0
    # CONSCIOUS REPOINT 2026-08-19 (spec jp6-vllm-kv-cache-oom-regression,
    # task 14 / H9). SUPERSEDED assertions, recorded VERBATIM — they pinned the
    # INTERMEDIATE version of this change, which kept a HARD
    # `KV_VIABILITY_FLOOR_BYTES = 0.25 GiB` term in `required` and named the
    # non-torch constant `NON_TORCH_ALLOWANCE_BYTES`::
    #
    #     # Added 2026-08-19 (task 14 / H8 + H9): two more constants under the
    #     # same PROPOSED/ESTIMATE discipline, pinned so a silent drift is
    #     # visible. The ORDER of the two KV terms is the H9 invariant: the
    #     # hard viability floor sits strictly between zero and the 1 GiB
    #     # serving margin, so the configuration that served with 0.65 GiB of
    #     # KV is admitted while a non-positive remainder is still refused.
    #     assert mb.KV_VIABILITY_FLOOR_BYTES == int(0.25 * GIB)
    #     assert mb.NON_TORCH_ALLOWANCE_BYTES == 2 * GIB
    #     assert 0 < mb.KV_VIABILITY_FLOOR_BYTES < mb.MINIMUM_KV_CACHE_BYTES
    #     assert mb.KV_VIABILITY_FLOOR_BYTES < int(0.65 * GIB)
    #
    # REASON: the operator took the simpler H9 decision — `required` charges NO
    # KV term at all, hard or soft, because the KV cache is what the budget
    # LEAVES OVER (exactly how vLLM computes it), and the constant is
    # `NON_TORCH_MEMORY_BYTES`. Nothing is weakened: the replacement pins the
    # WHOLE composition of `required` (strictly stronger than pinning one of
    # its terms), pins the absence of the intermediate constant, and still
    # pins the H9 outcome the superseded block existed to protect — the
    # configuration that demonstrably SERVED with 0.65 GiB of KV is ADMITTED
    # and merely warned about.
    assert mb.NON_TORCH_MEMORY_BYTES == 2 * GIB
    assert mb.MINIMUM_KV_CACHE_BYTES == 1 * GIB
    assert mb.ACTIVATION_FLOOR_BYTES == 2 * GIB
    assert mb.ACTIVATION_WEIGHT_FRACTION == 0.375
    assert mb.MULTIMODAL_IMAGE_INCREMENT == 1.0
    # H9: no KV term is charged, so `required` is exactly the three terms and
    # is STRICTLY BELOW what it would be with the serving margin added.
    for weights in (0, int(6.45 * GIB), 16 * GIB):
        for units in (1, 2, 3):
            assert mb.required_bytes(weights, units) == (
                weights + mb.NON_TORCH_MEMORY_BYTES
                + mb.activation_allowance(weights, units))
            assert (mb.required_bytes(weights, units)
                    < mb.required_bytes(weights, units)
                    + mb.MINIMUM_KV_CACHE_BYTES)
    # The intermediate hard-floor constant is GONE, not merely unused.
    assert not hasattr(mb, "KV_VIABILITY_FLOOR_BYTES")
    assert not hasattr(mb, "NON_TORCH_ALLOWANCE_BYTES")
    # The 1 GiB floor's surviving role: 0.65 GiB of KV served this model at
    # 2.95x concurrency for 4096 tokens, so it is under the floor and is a
    # WARNING threshold, never a refusal.
    assert int(0.65 * GIB) < mb.MINIMUM_KV_CACHE_BYTES


# ---------------------------------------------------------------------------
# read_memory parsing
# ---------------------------------------------------------------------------

def test_read_memory_parses_a_normal_proc_meminfo():
    """_Requirements: 1.10, 2.9_"""
    reader = FakeMeminfoReader([(DEVICE_TOTAL_BYTES, 3 * GIB)])
    reading = mb.read_memory(reader=reader)

    assert reading is not None
    # kB granularity: the parse is exact to the kB the kernel prints.
    assert reading.total_bytes == (DEVICE_TOTAL_BYTES // 1024) * 1024
    assert reading.available_bytes == 3 * GIB
    assert reader.call_count == 1


def test_read_memory_reads_the_real_proc_meminfo_by_default(tmp_path,
                                                            monkeypatch):
    """The default reader resolves :data:`PROC_MEMINFO_PATH` at CALL time, so
    the module-level path is a usable seam (and on a real device it is the
    kernel's own file).

    _Requirements: 1.10_"""
    crafted = tmp_path / "meminfo"
    crafted.write_text(meminfo_text(29 * GIB, 7 * GIB))
    monkeypatch.setattr(mb, "PROC_MEMINFO_PATH", str(crafted))

    reading = mb.read_memory()

    assert reading is not None
    assert reading.available_bytes == 7 * GIB


@pytest.mark.parametrize("body,label", [
    ("MemTotal:       31266816 kB\n", "MemAvailable missing"),
    ("MemAvailable:    3145728 kB\n", "MemTotal missing"),
    ("MemTotal:            kB\nMemAvailable:   not-a-number kB\n", "garbage"),
    ("MemTotal: -5 kB\nMemAvailable: 10 kB\n", "non-positive total"),
    ("", "empty"),
    ("\n\n### not meminfo at all ###\n", "unrelated text"),
])
def test_read_memory_degrades_to_none_and_never_raises(body, label):
    """Unparseable device state is "unverified", never an exception on the
    load path.

    _Requirements: 2.9_"""
    assert mb.read_memory(reader=lambda: body) is None, label


def test_read_memory_degrades_to_none_when_the_reader_explodes():
    """_Requirements: 2.9_"""
    def exploding_reader():
        raise OSError("device is on fire")

    assert mb.read_memory(reader=exploding_reader) is None


def test_read_memory_scales_recognised_units_and_refuses_unit_less_lines():
    """`/proc/meminfo` prints kB. A recognised unit is scaled; a line with NO
    unit is refused rather than assumed — guessing a memory figure's scale by
    a factor of 1024 is exactly the invented number this module never
    produces.

    _Requirements: 1.10, 2.9_"""
    kib = mb.read_memory(reader=lambda: "MemTotal: 1024 KiB\n"
                                        "MemAvailable: 512 KiB\n")
    unit_less = mb.read_memory(reader=lambda: "MemTotal: 1048576\n"
                                             "MemAvailable: 524288\n")

    assert kib is not None and kib.total_bytes == 1024 * 1024
    assert unit_less is None


# ---------------------------------------------------------------------------
# estimate_weights_on_disk
# ---------------------------------------------------------------------------

def test_estimate_weights_on_disk_sums_a_local_directory(tmp_path):
    """The S3-sourced rewritten-path shape: a local directory of shards.

    _Requirements: 1.1, 2.9_"""
    directory = weight_tree(tmp_path / "qwen-local", int(6.5 * GIB), shards=3)

    total = mb.estimate_weights_on_disk({"model": str(directory)})

    assert total == int(6.5 * GIB)
    # config.json is NOT weights: the sum is strictly the weight files.
    assert total < disk_usage_of(directory)


@pytest.mark.parametrize("suffix", [".safetensors", ".bin", ".gguf"])
def test_estimate_weights_on_disk_counts_every_weight_suffix(tmp_path, suffix):
    """_Requirements: 1.1_"""
    directory = weight_tree(tmp_path / suffix.lstrip("."), 2 * GIB,
                            shards=1, suffix=suffix)

    assert mb.estimate_weights_on_disk({"model": str(directory)}) == 2 * GIB


def test_estimate_weights_on_disk_finds_the_hf_cache_snapshot(tmp_path):
    """The repo-id shape: `models--{org}--{name}/snapshots/{revision}` under a
    cache root (the device sets HF_HOME=/aws_dda/hf_cache).

    _Requirements: 1.1, 2.9_"""
    cache_root = tmp_path / "hf_cache" / "hub"
    hf_cache_tree(cache_root, "Qwen/Qwen2.5-VL-7B-Instruct-AWQ",
                  int(6.47 * GIB))

    total = mb.estimate_weights_on_disk(INCIDENT_ENGINE_ARGS,
                                        hf_cache_roots=[str(cache_root)])

    assert total == int(6.47 * GIB)


def test_estimate_weights_on_disk_does_not_double_count_snapshots(tmp_path):
    """Two snapshots of the same repo must not be summed (they share blobs);
    the pinned `refs/main` revision is the one that counts.

    _Requirements: 1.1_"""
    cache_root = tmp_path / "hub"
    hf_cache_tree(cache_root, "org/model", 4 * GIB, revision="rev-a")
    hf_cache_tree(cache_root, "org/model", 3 * GIB, revision="rev-b")
    # hf_cache_tree wrote refs/main last as rev-b.
    total = mb.estimate_weights_on_disk({"model": "org/model"},
                                        hf_cache_roots=[str(cache_root)])

    assert total == 3 * GIB


def test_estimate_weights_on_disk_uses_the_environment_cache_roots(tmp_path,
                                                                   monkeypatch):
    """_Requirements: 1.1_"""
    hf_home = tmp_path / "aws_dda" / "hf_cache"
    hf_cache_tree(hf_home / "hub", "org/model", 5 * GIB)
    monkeypatch.setenv("HF_HOME", str(hf_home))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)

    assert str(hf_home / "hub") in mb.default_hf_cache_roots()
    assert mb.estimate_weights_on_disk({"model": "org/model"}) == 5 * GIB


@pytest.mark.parametrize("engine_args,label", [
    ({}, "no model key"),
    ({"model": ""}, "blank model"),
    ({"model": "   "}, "whitespace model"),
    ({"model": None}, "None model"),
    ({"model": 17}, "non-string model"),
    ({"model": "org/name/extra"}, "not a repo id"),
    (None, "no engine args at all"),
])
def test_estimate_weights_on_disk_returns_none_when_undeterminable(
        engine_args, label, tmp_path):
    """Undeterminable weights are ``None`` — the caller degrades to the
    documented lower bound and marks the verdict unverified, never a guess.

    _Requirements: 2.9_"""
    assert mb.estimate_weights_on_disk(
        engine_args, hf_cache_roots=[str(tmp_path)]) is None, label


def test_estimate_weights_on_disk_returns_none_for_an_absent_model(tmp_path):
    """A model that has not been pulled yet: nothing on disk to size.

    _Requirements: 2.9_"""
    assert mb.estimate_weights_on_disk(
        {"model": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ"},
        hf_cache_roots=[str(tmp_path / "empty-cache")]) is None


def test_estimate_weights_on_disk_returns_none_for_a_weightless_directory(
        tmp_path):
    """A directory that exists but holds no weight files (a bare
    `config.json`) is undeterminable, not zero bytes.

    _Requirements: 2.9_"""
    directory = tmp_path / "config-only"
    directory.mkdir()
    (directory / "config.json").write_text("{}")

    assert mb.estimate_weights_on_disk({"model": str(directory)}) is None


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root bypasses directory permissions")
def test_estimate_weights_on_disk_survives_an_unreadable_tree(tmp_path):
    """An unreadable tree degrades (partial sum or ``None``) and NEVER raises
    — a preflight must not be able to break the load it checks.

    _Requirements: 2.9_"""
    directory = weight_tree(tmp_path / "locked", 2 * GIB, shards=1)
    nested = directory / "shards"
    weight_tree(nested, 1 * GIB, shards=1)
    os.chmod(str(nested), 0o000)
    try:
        total = mb.estimate_weights_on_disk({"model": str(directory)})
    finally:
        os.chmod(str(nested), 0o755)

    assert total == 2 * GIB  # the readable shard only


# ---------------------------------------------------------------------------
# activation_allowance / required_bytes / fraction_cap edge cases
# ---------------------------------------------------------------------------

def test_activation_allowance_reproduces_the_recalibrated_arithmetic():
    """The MEASURED per-unit arithmetic (task 14 / H8): 6.5 GiB of weights,
    ONE multimodal unit → ``max(2, 0.375 x 6.5) = 2.44 GiB``, and
    ``required = 6.5 + 2.00 (non-torch) + 2.44 = 10.94 GiB``.

    CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
    jp6-vllm-kv-cache-oom-regression, task 14 / H9). The INTERMEDIATE version
    of this change kept a hard 0.25 GiB KV viability floor in ``required``.
    SUPERSEDED assertions and docstring line, recorded VERBATIM::

        ``required = 6.5 + 2.00 (non-torch) + 2.44 + 0.25 (KV viability) =
        11.19 GiB``.

        assert mb.format_gib(required) == "11.19 GiB"
        # The four terms, and nothing else, make up the requirement.
        assert required == (weights + mb.NON_TORCH_ALLOWANCE_BYTES + allowance
                            + mb.KV_VIABILITY_FLOOR_BYTES)
        # The 1 GiB serving margin is NOT charged (task 14 / H9).
        assert mb.MINIMUM_KV_CACHE_BYTES not in (
            required - weights - allowance - mb.NON_TORCH_ALLOWANCE_BYTES,)

    Reason: the operator's H9 decision charges NO KV term at all — the KV cache
    is the remainder the budget leaves over, which is how vLLM computes it — so
    the requirement is exactly THREE terms and the constant is
    ``NON_TORCH_MEMORY_BYTES``. Same strength: every term is still pinned to a
    literal GiB string and the composition is still pinned exactly, now with
    the additional assertion that NO KV amount of any size is inside it.

    REPOINTED 2026-08-19. SUPERSEDED test, recorded verbatim::

        def test_activation_allowance_reproduces_the_incident_arithmetic():
            \"\"\"design Decision 2's worked verdict: 6.5 GiB of weights, one
            image → ``max(2, 0.75 x 6.5) = 4.88 GiB``,
            ``required = 12.38 GiB``.\"\"\"
            weights = int(6.5 * GIB)
            allowance = mb.activation_allowance(weights, 1)
            required = mb.required_bytes(weights, 1)
            assert allowance == pytest.approx(0.75 * weights, rel=1e-9)
            assert mb.format_gib(allowance) == "4.88 GiB"
            assert mb.format_gib(required) == "12.38 GiB"

    Why: 0.75 was calibrated to one point now known to have been a TWO-unit
    (video-unbounded) measurement, and the measured one-unit peak is 2.47 GiB
    against 6.59 GiB of weights = 0.375. `required` also gained the non-torch
    term it always omitted and traded the 1 GiB hard KV floor for the 0.25 GiB
    viability floor (H9). Same strength: every term is still pinned to a
    literal GiB string.

    _Requirements: 1.1, 2.1_"""
    weights = int(6.5 * GIB)

    allowance = mb.activation_allowance(weights, 1)
    required = mb.required_bytes(weights, 1)

    assert allowance == pytest.approx(0.375 * weights, rel=1e-9)
    assert mb.format_gib(allowance) == "2.44 GiB"
    assert mb.format_gib(required) == "10.94 GiB"
    # The three terms, and nothing else, make up the requirement.
    assert required == (weights + mb.NON_TORCH_MEMORY_BYTES + allowance)
    # NO KV term is charged, of any size (task 14 / H9): the residue after the
    # three terms is exactly zero, so neither the 1 GiB serving margin nor the
    # intermediate 0.25 GiB viability floor is hiding in it.
    assert required - weights - allowance - mb.NON_TORCH_MEMORY_BYTES == 0


def test_activation_allowance_two_units_doubles_the_peak():
    """`MULTIMODAL_IMAGE_INCREMENT = 1.0` is MEASURED-CONFIRMED for the
    image<->video UNIT step (2.47 -> 4.93 GiB, a 2:1 ratio within 0.01 GiB)
    and still UNMEASURED per additional IMAGE, so a multi-unit configuration
    must be sized explicitly ([HARDWARE] H8).

    REPOINTED 2026-08-19. SUPERSEDED test, recorded verbatim::

        def test_activation_allowance_two_images_doubles_the_peak():
            \"\"\"`MULTIMODAL_IMAGE_INCREMENT = 1.0` is UNMEASURED and
            deliberately high, so a two-image configuration must be sized
            explicitly ([HARDWARE] H8).\"\"\"
            weights = int(6.5 * GIB)
            assert mb.activation_allowance(weights, 2) == \\
                2 * mb.activation_allowance(weights, 1)
            assert mb.format_gib(mb.required_bytes(weights, 2)) == "17.25 GiB"

    CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
    jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED assertion,
    recorded VERBATIM — it priced the INTERMEDIATE hard 0.25 GiB KV viability
    floor into ``required`` (6.50 + 2.00 + 4.88 + 0.25)::

        assert mb.format_gib(mb.required_bytes(weights, 2)) == "13.62 GiB"

    Reason: H9 charges no KV term, so ``required`` is 6.50 + 2.00 + 4.88 =
    13.38 GiB. Same strength: the 2:1 unit ratio and the literal GiB string are
    both still pinned.

    _Requirements: 2.1, 2.4_"""
    weights = int(6.5 * GIB)

    assert mb.activation_allowance(weights, 2) == \
        2 * mb.activation_allowance(weights, 1)
    assert mb.format_gib(mb.required_bytes(weights, 2)) == "13.38 GiB"


@pytest.mark.parametrize("weights", [None, 0, 1, 1024, int(0.1 * GIB)])
def test_activation_allowance_floors_zero_and_tiny_weights(weights):
    """A fraction-of-weights term rounds to nothing for small models; the
    floor is what keeps the estimate conservative.

    _Requirements: 2.1_"""
    assert mb.activation_allowance(weights, 1) == mb.ACTIVATION_FLOOR_BYTES


def test_required_bytes_degrades_to_the_documented_lower_bound():
    """Undeterminable weights → ``NON_TORCH + ACTIVATION_FLOOR``, never a
    guessed weight — the lower bound the module's own docstring and the
    ``UNVERIFIED`` refusal note both name.

    REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED assertion, recorded
    verbatim::

        assert mb.required_bytes(None, 1) == \\
            mb.ACTIVATION_FLOOR_BYTES + mb.MINIMUM_KV_CACHE_BYTES

    CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
    jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED assertion,
    recorded VERBATIM — the INTERMEDIATE bound carried a hard KV viability
    floor and named the constant ``NON_TORCH_ALLOWANCE_BYTES``::

        assert mb.required_bytes(None, 1) == (mb.NON_TORCH_ALLOWANCE_BYTES
                                             + mb.ACTIVATION_FLOOR_BYTES
                                             + mb.KV_VIABILITY_FLOOR_BYTES)

    Reason: H9 charges no KV term, so the documented bound is exactly
    ``NON_TORCH + ACTIVATION_FLOOR`` = 4 GiB — which is what
    :func:`evaluate_device_fit`'s ``UNVERIFIED`` note states in words, and the
    two are now pinned to agree here.

    _Requirements: 2.9_"""
    assert mb.required_bytes(None, 1) == (mb.NON_TORCH_MEMORY_BYTES
                                          + mb.ACTIVATION_FLOOR_BYTES)
    # The bound is a LOWER bound: no weight was invented, and it is strictly
    # below the requirement of any model whose weights ARE sizable.
    assert mb.required_bytes(None, 1) < mb.required_bytes(int(6.45 * GIB), 1)


@pytest.mark.parametrize("images", [None, 0, -3, "two", 1.9, True, object()])
def test_activation_allowance_tolerates_malformed_image_counts(images):
    """Never raises out of the public API; a malformed count is one image.

    _Requirements: 2.9_"""
    assert mb.activation_allowance(int(1 * GIB), images) \
        >= mb.ACTIVATION_FLOOR_BYTES


@pytest.mark.parametrize("limit,expected", [
    ({"image": 1}, 1),
    ({"image": 2}, 2),
    ({"image": Decimal("3")}, 3),
    (None, 1),
    ({}, 1),
    ({"image": None}, 1),
    ({"image": True}, 1),
    ({"image": 0}, 1),
    ({"image": -4}, 1),
    ({"image": "banana"}, 1),
    ({"video": 2}, 1),
    ("image=2", 1),
    ([2], 1),
])
def test_images_per_prompt_tolerates_malformed_limit_mm_per_prompt(limit,
                                                                   expected):
    """The device NEVER invents a larger multimodal limit than the authored
    configuration states (defect 1.4) — malformed input falls back to 1.

    _Requirements: 2.4, 2.9_"""
    assert mb.images_per_prompt({"limit_mm_per_prompt": limit}) == expected


@pytest.mark.parametrize("raw,expected", [
    (Decimal("0.4"), 0.4),
    (0.4, 0.4),
    (1, 1.0),
    ("0.25", 0.25),
    (None, mb.DEFAULT_GPU_MEMORY_UTILIZATION),
    (0, mb.DEFAULT_GPU_MEMORY_UTILIZATION),
    (-0.5, mb.DEFAULT_GPU_MEMORY_UTILIZATION),
    (1.5, mb.DEFAULT_GPU_MEMORY_UTILIZATION),
    ("banana", mb.DEFAULT_GPU_MEMORY_UTILIZATION),
])
def test_gpu_memory_utilization_accepts_decimal_and_degrades_safely(raw,
                                                                    expected):
    """`Decimal` is the portal's storage type; nonsense falls back to the
    documented default rather than refusing on a guess.

    _Requirements: 2.9_"""
    args = dict(INCIDENT_ENGINE_ARGS)
    args["gpu_memory_utilization"] = raw

    assert mb.gpu_memory_utilization(args) == pytest.approx(expected)


def test_fraction_cap_matches_the_documented_jp6_and_jp7_values():
    """JP6: ``(30-6)/30 = 0.80``. JP7: ``(120-8)/120 = 0.9333``.

    _Requirements: 2.2_"""
    assert mb.fraction_cap("arm64_jp6") == pytest.approx(0.80)
    assert mb.fraction_cap("arm64_jp7") == pytest.approx(112 / 120)


@pytest.mark.parametrize("arch", [None, "", "x86_64", "arm64_jp9", 7])
def test_fraction_cap_is_none_for_an_unknown_architecture(arch):
    """No profile entry and no measured total → no cap sentence is invented.

    _Requirements: 2.2, 2.9_"""
    assert mb.fraction_cap(arch) is None


def test_fraction_cap_prefers_the_devices_measured_total():
    """On the device the fraction applies to the REAL MemTotal, so the cap is
    computed from it (29.95 GiB − 6 GiB ≈ 0.80).

    _Requirements: 2.2_"""
    cap = mb.fraction_cap(None, DEVICE_TOTAL_BYTES)

    assert cap == pytest.approx((DEVICE_TOTAL_BYTES - 6 * GIB)
                                / DEVICE_TOTAL_BYTES)
    assert 0.79 < cap < 0.81


# ---------------------------------------------------------------------------
# evaluate_device_fit — the incident, and the honesty rules
# ---------------------------------------------------------------------------

def test_evaluate_device_fit_admits_the_1_0_59_equivalent_configuration():
    """H9, the point of the floor split: the configuration LocalServer 1.0.59
    demonstrably SERVED — the incident's staged args with the multimodal limit
    authored as the product's default one unit, at
    ``gpu_memory_utilization = 0.4`` — is ADMITTED again.

    ``0.4 x 29.95 GiB = 11.98 GiB`` of budget against a
    ``6.45 + 2.00 + 2.42 = 10.87 GiB`` requirement, leaving 1.11 GiB of
    predicted KV cache — above the 1 GiB serving margin.

    CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
    jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED docstring line
    and assertions, recorded VERBATIM — they priced the INTERMEDIATE hard
    0.25 GiB KV viability floor and named its ``terms`` key::

        ``6.45 + 2.00 + 2.42 + 0.25 = 11.12 GiB`` requirement

        assert mb.format_gib(verdict.terms["required_bytes"]) == "11.12 GiB"
        assert verdict.terms["non_torch_bytes"] == mb.NON_TORCH_ALLOWANCE_BYTES
        assert (verdict.terms["kv_viability_floor_bytes"]
                == mb.KV_VIABILITY_FLOOR_BYTES)

    Reason: H9 charges no KV term at all, so there is no viability floor and no
    ``kv_viability_floor_bytes`` term to report. Nothing is weakened — the
    verdict asserted (ADMITTED, no warning, headroom above the serving margin)
    is unchanged, and the absence of the intermediate term is now pinned
    positively.

    The shipped
    preflight refused it because it charged the 1 GiB serving-margin floor as a
    HARD term, converting "serves, with a thin margin, sometimes after one
    retry" into "never loads" (0.65 GiB of KV served this model at 2.95x
    concurrency for 4096 tokens).

    REPOINTED 2026-08-19 (task 14 / H9). SUPERSEDED test, recorded verbatim —
    it asserted the OPPOSITE verdict for this configuration, which is exactly
    the contradiction H9 records::

        def test_evaluate_device_fit_refuses_the_incident_configuration():
            \"\"\"The staged args verbatim from bugfix.md against the device's
            own total: ``0.4 x 29.95 GiB = 11.98 GiB`` budget vs a 12.38 GiB
            requirement — the refusal names measured available, every term,
            and the setting to change.\"\"\"
            reading = mb.MemoryReading(total_bytes=DEVICE_TOTAL_BYTES,
                                       available_bytes=int(23 * GIB))
            verdict = mb.evaluate_device_fit(INCIDENT_ENGINE_ARGS, reading,
                                             int(6.5 * GIB))
            assert verdict.ok is False
            assert verdict.unverified is False
            assert verdict.terms["failed_conditions"] == ["budget"]
            reason = verdict.refusal_reason
            assert reason.startswith(mb.PREFLIGHT_REFUSED_MARKER)
            assert "\\n" not in reason
            for fragment in ("23.00 GiB", "11.98 GiB", "12.38 GiB",
                             "6.50 GiB", "4.88 GiB", "1.00 GiB", "ESTIMATE",
                             "limit_mm_per_prompt.image", "max_model_len"):
                assert fragment in reason, fragment
            assert not re.search(r"(lower|decrease|reduce)\\w*\\s+"
                                 r"gpu_memory_utilization", reason,
                                 re.IGNORECASE)

    The refusal-diagnostic coverage that test carried is NOT lost: it is
    asserted in full by
    :func:`test_evaluate_device_fit_refuses_a_genuinely_short_budget` below,
    over a configuration that genuinely does not fit.

    _Requirements: 1.1, 1.10, 2.1, 2.9_"""
    args = dict(INCIDENT_ENGINE_ARGS,
                limit_mm_per_prompt={"image": 1, "video": 0})
    reading = mb.MemoryReading(total_bytes=DEVICE_TOTAL_BYTES,
                               available_bytes=int(23 * GIB))

    verdict = mb.evaluate_device_fit(args, reading, int(6.45 * GIB),
                                     arch="arm64_jp6")

    assert verdict.ok is True, verdict.refusal_reason
    assert verdict.refusal_reason is None
    assert verdict.terms["failed_conditions"] == []
    assert mb.format_gib(verdict.terms["required_bytes"]) == "10.87 GiB"
    assert mb.format_gib(verdict.terms["budget_bytes"]) == "11.98 GiB"
    assert mb.format_gib(verdict.terms["activation_bytes"]) == "2.42 GiB"
    assert verdict.terms["non_torch_bytes"] == mb.NON_TORCH_MEMORY_BYTES
    # No KV term is charged, so no viability floor is reported (H9); the
    # serving margin is still reported, as the WARNING threshold it now is.
    assert "kv_viability_floor_bytes" not in verdict.terms
    assert verdict.terms["kv_floor_bytes"] == mb.MINIMUM_KV_CACHE_BYTES
    # The predicted KV headroom clears the 1 GiB serving margin here, so not
    # even a thin-margin warning fires (the device measured 0.65 GiB at this
    # utilization when non_torch happened to be high).
    assert verdict.terms["warnings"] == []
    assert verdict.terms["kv_headroom_bytes"] >= mb.MINIMUM_KV_CACHE_BYTES


def test_evaluate_device_fit_warns_thin_margin_without_refusing():
    """A configuration that leaves a POSITIVE predicted KV remainder but less
    than the 1 GiB serving margin PASSES with a ``thin_margin`` warning —
    design Decision 2's "serving-margin floor, not a hard load threshold" and
    Decision 6's Thin_Margin WARNING, which the shipped preflight contradicted
    by refusing outright (task 14 / H9).

    CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
    jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED assertion,
    recorded VERBATIM — it bracketed the headroom by the INTERMEDIATE hard
    0.25 GiB viability floor::

        assert (mb.KV_VIABILITY_FLOOR_BYTES
                <= verdict.terms["kv_headroom_bytes"]
                < mb.MINIMUM_KV_CACHE_BYTES)

    Reason: with no hard KV term the lower bracket is what the budget arm
    actually enforces, namely a POSITIVE remainder (``budget >= required`` is
    ``headroom >= 0``). Not a loosening of what is tested: the same two-sided
    bracket is asserted, against the bound the code now enforces, and the
    ``0.51 GiB`` figure is pinned to a literal as well.

    _Requirements: 2.1, 2.7_"""
    args = dict(INCIDENT_ENGINE_ARGS,
                gpu_memory_utilization=0.38,
                limit_mm_per_prompt={"image": 1, "video": 0})
    reading = mb.MemoryReading(total_bytes=DEVICE_TOTAL_BYTES,
                               available_bytes=int(23 * GIB))

    verdict = mb.evaluate_device_fit(args, reading, int(6.45 * GIB),
                                     arch="arm64_jp6")

    # 0.38 x 29.95 = 11.38 GiB of budget against 10.87 GiB required: it fits,
    # with only 0.51 GiB of predicted KV — positive, so the budget arm admits
    # it, but under the 1 GiB serving margin.
    assert verdict.ok is True, verdict.refusal_reason
    assert verdict.terms["failed_conditions"] == []
    assert verdict.terms["warnings"] == ["thin_margin"]
    assert (0 < verdict.terms["kv_headroom_bytes"]
            < mb.MINIMUM_KV_CACHE_BYTES)
    assert mb.format_gib(verdict.terms["kv_headroom_bytes"]) == "0.51 GiB"


def test_evaluate_device_fit_refuses_a_genuinely_short_budget():
    """The refusal diagnostic, in full, over a configuration that genuinely
    does not fit: ``0.3 x 29.95 GiB = 8.98 GiB`` of budget against the same
    10.87 GiB requirement. Carries every assertion the superseded
    incident-refusal test made (see
    :func:`test_evaluate_device_fit_admits_the_1_0_59_equivalent_configuration`),
    repointed to the new term set — including the non-torch allowance, and the
    PREDICTED KV REMAINDER stated against the serving-margin floor, all named
    with their numbers.

    CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
    jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED fragments,
    recorded VERBATIM — they asserted the INTERMEDIATE requirement total and
    the hard viability floor's own sentence::

        for fragment in ("23.00 GiB", "8.98 GiB", "11.12 GiB", "6.45 GiB",
                         "non-torch allowance 2.00 GiB (ESTIMATE)",
                         "activation allowance 2.42 GiB (ESTIMATE",
                         "KV-cache viability floor 0.25 GiB",
                         "1 multimodal unit(s): 1 image(s) + 0 video(s)",
                         "limit_mm_per_prompt.image", "max_model_len"):
            assert fragment in reason, fragment

    Reason: with no KV term charged there is no viability-floor sentence to
    name. What replaces it is STRICTLY MORE than a constant: the diagnostic must
    state the PREDICTED KV REMAINDER this configuration leaves (-1.88 GiB) AND
    the serving-margin floor it is measured against (1.00 GiB) — i.e. the H9
    surface itself, asserted as one verbatim sentence. Every other fragment,
    including both ESTIMATE labels, is unchanged.

    _Requirements: 1.1, 1.10, 2.1, 2.9_"""
    args = dict(INCIDENT_ENGINE_ARGS,
                gpu_memory_utilization=0.3,
                limit_mm_per_prompt={"image": 1, "video": 0})
    reading = mb.MemoryReading(total_bytes=DEVICE_TOTAL_BYTES,
                               available_bytes=int(23 * GIB))

    verdict = mb.evaluate_device_fit(args, reading, int(6.45 * GIB),
                                     arch="arm64_jp6")

    assert verdict.ok is False
    assert verdict.unverified is False
    assert verdict.terms["failed_conditions"] == ["budget"]
    reason = verdict.refusal_reason
    assert reason.startswith(mb.PREFLIGHT_REFUSED_MARKER)
    assert "\n" not in reason
    for fragment in ("23.00 GiB", "8.98 GiB", "10.87 GiB", "6.45 GiB",
                     "non-torch allowance 2.00 GiB (ESTIMATE)",
                     "activation allowance 2.42 GiB (ESTIMATE",
                     "leaving a predicted KV cache remainder of -1.88 GiB "
                     "against the 1.00 GiB serving-margin floor",
                     "1 multimodal unit(s): 1 image(s) + 0 video(s)",
                     "limit_mm_per_prompt.image", "max_model_len"):
        assert fragment in reason, fragment
    # The requirement names every term it CHARGES and no term it does not:
    # nothing in the diagnostic claims a KV amount is part of the requirement.
    assert "viability floor" not in reason, reason
    # Never advise LOWERING the fraction as a cure for insufficient KV.
    assert not re.search(r"(lower|decrease|reduce)\w*\s+"
                         r"gpu_memory_utilization", reason, re.IGNORECASE)


def test_evaluate_device_fit_refuses_a_starved_device_before_the_budget_arm():
    """P1: whatever the configured fraction says, 3 GB of available memory
    cannot hold a 12.38 GiB requirement.

    _Requirements: 1.10, 2.9_"""
    reading = mb.MemoryReading(total_bytes=DEVICE_TOTAL_BYTES,
                               available_bytes=3 * GIB)

    verdict = mb.evaluate_device_fit(INCIDENT_ENGINE_ARGS, reading,
                                     int(6.5 * GIB))

    assert verdict.ok is False
    assert verdict.terms["failed_conditions"] == ["starvation", "budget"]
    assert "3.00 GiB" in verdict.refusal_reason


def test_evaluate_device_fit_accepts_a_healthy_configuration():
    """The inverse: an ample device and a modest model are not refused, and no
    diagnostic is composed.

    _Requirements: 2.9_"""
    args = dict(INCIDENT_ENGINE_ARGS, gpu_memory_utilization=0.5)
    reading = mb.MemoryReading(total_bytes=120 * GIB,
                               available_bytes=100 * GIB)

    verdict = mb.evaluate_device_fit(args, reading, 16 * GIB)

    assert verdict.ok is True
    assert verdict.refusal_reason is None
    assert verdict.terms["failed_conditions"] == []
    assert verdict.unverified is False


def test_evaluate_device_fit_marks_undeterminable_weights_unverified():
    """Weights-dependent arms degrade to the NON_TORCH + ACTIVATION_FLOOR lower
    bound, the verdict says UNVERIFIED, and no weight number is invented.

    REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED assertion, recorded
    verbatim::

        assert verdict.terms["required_bytes"] == \\
            mb.ACTIVATION_FLOOR_BYTES + mb.MINIMUM_KV_CACHE_BYTES

    CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
    jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED assertion,
    recorded VERBATIM — the INTERMEDIATE bound added a hard viability floor and
    named the constant ``NON_TORCH_ALLOWANCE_BYTES``::

        assert verdict.terms["required_bytes"] == (
            mb.NON_TORCH_ALLOWANCE_BYTES
            + mb.activation_allowance(0, mb.DEFAULT_MULTIMODAL_UNITS)
            + mb.KV_VIABILITY_FLOOR_BYTES)

    Reason: H9 charges no KV term, so the degradation path's bound is exactly
    ``NON_TORCH + activation_allowance(0, units)`` — coherent with
    :func:`required_bytes`' own docstring and with the words the ``UNVERIFIED``
    note puts in the refusal ("the NON_TORCH + ACTIVATION_FLOOR lower bound"),
    which this test now also pins verbatim.

    Note the staged incident args author NO ``limit_mm_per_prompt``, so they
    are sized for TWO multimodal units (vLLM's own unbounded video default) —
    the same conservative reading the portal makes.

    _Requirements: 2.9_"""
    reading = mb.MemoryReading(total_bytes=DEVICE_TOTAL_BYTES,
                               available_bytes=int(2.5 * GIB))

    verdict = mb.evaluate_device_fit(INCIDENT_ENGINE_ARGS, reading, None)

    assert verdict.unverified is True
    assert verdict.ok is False
    assert verdict.terms["weights_bytes"] is None
    assert verdict.terms["multimodal_units"] == mb.DEFAULT_MULTIMODAL_UNITS
    assert verdict.terms["required_bytes"] == (
        mb.NON_TORCH_MEMORY_BYTES
        + mb.activation_allowance(0, mb.DEFAULT_MULTIMODAL_UNITS))
    assert "UNVERIFIED" in verdict.refusal_reason
    assert "undeterminable" in verdict.refusal_reason
    # The words the refusal uses for the bound match the bound it used.
    assert ("the NON_TORCH + ACTIVATION_FLOOR lower bound"
            in verdict.refusal_reason), verdict.refusal_reason


def test_evaluate_device_fit_declines_to_judge_without_a_reading():
    """An unreadable `/proc/meminfo` must not refuse a load that might well
    succeed: ok, but unverified.

    _Requirements: 2.9_"""
    verdict = mb.evaluate_device_fit(INCIDENT_ENGINE_ARGS, None,
                                     int(6.5 * GIB))

    assert verdict.ok is True
    assert verdict.unverified is True
    assert verdict.refusal_reason is None


def test_evaluate_device_fit_refuses_while_the_starvation_latch_is_set():
    """P3: a previous failed attempt's memory did not come back, so the retry
    is refused with BOTH readings named rather than starving the device
    further (defect 1.5).

    _Requirements: 2.5_"""
    latch = mb.StarvationLatch(model_name="qwen2-5-vl-7b-instruct-awq",
                               available_before_bytes=int(23 * GIB),
                               available_after_bytes=int(3 * GIB))
    reading = mb.MemoryReading(total_bytes=DEVICE_TOTAL_BYTES,
                               available_bytes=int(3 * GIB))

    verdict = mb.evaluate_device_fit(INCIDENT_ENGINE_ARGS, reading,
                                     int(6.5 * GIB), latch)

    assert verdict.ok is False
    assert verdict.terms["failed_conditions"] == ["latch"]
    assert verdict.refusal_reason.startswith(mb.PREFLIGHT_REFUSED_MARKER)
    for fragment in ("23.00 GiB", "3.00 GiB", "20.00 GiB",
                     "backend container restart"):
        assert fragment in verdict.refusal_reason, fragment


def test_evaluate_device_fit_states_the_co_tenancy_hazard_above_the_cap():
    """At or above the cap the surfaces say raising the fraction is unsafe
    here and stop (defect 1.3 is a guidance hazard, not a wording nit).

    _Requirements: 2.2, 2.3_"""
    args = dict(INCIDENT_ENGINE_ARGS, gpu_memory_utilization=0.9)
    reading = mb.MemoryReading(total_bytes=DEVICE_TOTAL_BYTES,
                               available_bytes=int(4 * GIB))

    verdict = mb.evaluate_device_fit(args, reading, int(20 * GIB))

    assert verdict.ok is False
    reason = verdict.refusal_reason
    assert "unsafe here" in reason
    assert "co-tenancy cap" in reason
    assert "unified memory" in reason
    assert not re.search(r"(lower|decrease|reduce)\w*\s+"
                         r"gpu_memory_utilization", reason, re.IGNORECASE)


def test_evaluate_device_fit_quantifies_a_safe_fraction_below_the_cap():
    """Below the cap, raising the fraction is offered LAST and bounded: the
    exact minimum fraction and the cap, both quantified.

    _Requirements: 2.3_"""
    args = dict(INCIDENT_ENGINE_ARGS, gpu_memory_utilization=0.2)
    reading = mb.MemoryReading(total_bytes=DEVICE_TOTAL_BYTES,
                               available_bytes=int(23 * GIB))

    verdict = mb.evaluate_device_fit(args, reading, int(6.5 * GIB))
    reason = verdict.refusal_reason

    assert verdict.ok is False
    assert "may be raised to at most 0.80" in reason
    # REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED assertion, recorded
    # verbatim:  assert "at least 0.41" in reason  # 12.38 / 29.95
    # The staged incident args author no `limit_mm_per_prompt`, so they are
    # sized for TWO units: required = 6.50 + 2.00 + 4.88 + 0.25 = 13.62 GiB,
    # and 13.62 / 29.95 = 0.45.
    assert "at least 0.45" in reason  # 13.62 / 29.95
    # Demand-reducing remediation comes BEFORE the fraction sentence.
    assert reason.index("Reduce demand first") < \
        reason.index("may be raised to at most")


def test_evaluate_device_fit_sizes_the_authored_two_image_limit():
    """The multimodal term is the AUTHORED limit — a two-image model is sized
    for two images, and the diagnostic says so. With the video modality left
    UNAUTHORED that is THREE units (vLLM's own default of 1 video), which is
    the portal's reading too (task 14 / H8+H9 closed the divergence).

    REPOINTED 2026-08-19. SUPERSEDED assertions, recorded verbatim::

        assert mb.format_gib(verdict.terms["required_bytes"]) == "17.25 GiB"
        assert "2 image(s)" in verdict.refusal_reason

    _Requirements: 2.4_"""
    args = dict(INCIDENT_ENGINE_ARGS, limit_mm_per_prompt={"image": 2})
    reading = mb.MemoryReading(total_bytes=DEVICE_TOTAL_BYTES,
                               available_bytes=int(23 * GIB))

    verdict = mb.evaluate_device_fit(args, reading, int(6.5 * GIB))

    assert verdict.terms["images_per_prompt"] == 2
    assert verdict.terms["videos_per_prompt"] == mb.DEFAULT_VIDEOS_PER_PROMPT
    assert verdict.terms["multimodal_units"] == 3
    # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
    # jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED assertion,
    # recorded VERBATIM — it priced the INTERMEDIATE hard 0.25 GiB KV viability
    # floor into `required`:
    #     # 6.50 + 2.00 + (0.375 x 6.50 x 3 = 7.31) + 0.25
    #     assert mb.format_gib(verdict.terms["required_bytes"]) == "16.06 GiB"
    # Reason: H9 charges no KV term, so the total is 15.81 GiB. Same strength —
    # the literal GiB string is still pinned.
    # 6.50 + 2.00 + (0.375 x 6.50 x 3 = 7.31)
    assert mb.format_gib(verdict.terms["required_bytes"]) == "15.81 GiB"
    assert "3 multimodal unit(s): 2 image(s) + 1 video(s)" \
        in verdict.refusal_reason
    # The omission is named, with the fix and its measured effect.
    assert "limit_mm_per_prompt.video (effective 1, NOT authored)" \
        in verdict.refusal_reason
    assert "set limit_mm_per_prompt.video = 0" in verdict.refusal_reason

    # Bounding the video modality drops it back to two units, exactly as the
    # portal prices it (measured: activation peak 4.93 -> 2.47 GiB).
    bounded = dict(args, limit_mm_per_prompt={"image": 2, "video": 0})
    bounded_verdict = mb.evaluate_device_fit(bounded, reading, int(6.5 * GIB))

    assert bounded_verdict.terms["multimodal_units"] == 2
    assert (bounded_verdict.terms["required_bytes"]
            < verdict.terms["required_bytes"])


def test_preflight_composes_the_reads_and_never_raises(tmp_path):
    """The manager's call site: read memory + size weights + evaluate, both
    reads injected, no GPU anywhere.

    _Requirements: 1.10, 2.9_"""
    cache_root = tmp_path / "hub"
    hf_cache_tree(cache_root, "Qwen/Qwen2.5-VL-7B-Instruct-AWQ",
                  int(6.47 * GIB))
    reader = FakeMeminfoReader([(DEVICE_TOTAL_BYTES, int(23 * GIB))])

    verdict = mb.preflight(INCIDENT_ENGINE_ARGS, reader=reader,
                           hf_cache_roots=[str(cache_root)],
                           model_name="qwen2-5-vl-7b-instruct-awq")

    assert verdict.ok is False
    assert verdict.unverified is False
    assert verdict.terms["weights_bytes"] == int(6.47 * GIB)
    assert "qwen2-5-vl-7b-instruct-awq" in verdict.refusal_reason
    assert reader.call_count == 1


def test_preflight_is_unverified_when_nothing_can_be_measured():
    """_Requirements: 2.9_"""
    def exploding_reader():
        raise OSError("no /proc")

    verdict = mb.preflight({"model": "org/never-pulled"},
                           reader=exploding_reader, hf_cache_roots=[])

    assert verdict.ok is True
    assert verdict.unverified is True
