"""Property test for the vLLM preflight fit-check decision
(vllm-sizing-and-packaging-errors, task 1.3).

**Feature: vllm-sizing-and-packaging-errors, Property 4: Fit_Check decision
correctness**

_For any_ engine configuration, weight estimate, and architecture set, a
FitFinding reports `fits = true` if and only if BOTH named conditions hold:

- **A (budget sufficiency)**: `gpu_memory_utilization ×
  DEVICE_MEMORY_PROFILE_BYTES[arch] ≥ weights + activation_allowance +
  MINIMUM_KV_CACHE_BYTES`
- **B (co-tenancy safety)**: `gpu_memory_utilization ≤ (profile[arch] −
  CO_TENANCY_RESERVATION_BYTES[arch]) / profile[arch]`

and every failing finding's message contains the architecture name, the
budget, the estimate, the activation and co-tenancy terms, leads with
demand-reducing remediation, and mentions raising `gpu_memory_utilization`
only with the co-tenancy cap stated — never advice to lower it.

**Validates: Requirements 3.1, 3.8, 3.9**

**REPOINTED** by `jp6-vllm-kv-cache-oom-regression` (spec task 3.2, design
Decision 2 "Sibling-spec items that must be consciously repointed", row S5).
The original assertions were `required_bytes = estimate_bytes +
MINIMUM_KV_CACHE_BYTES` and `expected_fits = budget_bytes >= required_bytes`
with the message required to advise `raise gpu_memory_utilization`. That
model reported 4.50 GiB of slack for the 2026-08-17
`ryanorinagxdevkithomelabjp622` load whose device-measured KV remainder was
−7.83 GiB, because it omitted vLLM's activation/profiling peak (measured
4.92 GiB) and modelled no co-tenancy on shared unified memory. What is
**KEPT unchanged and never weakened** is the negative assertion: no message
may advise *lowering* `gpu_memory_utilization` as a cure for insufficient
KV cache (Requirement 3.9's surviving invariant).

`evaluate_fit` is pure (stdlib-only, no AWS), so this test runs directly
against the module with no fixtures.
"""
import re
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from vllm_fit_check import (
    DEVICE_MEMORY_PROFILE_BYTES,
    DEFAULT_GPU_MEMORY_UTILIZATION,
    GIB,
    MINIMUM_KV_CACHE_BYTES,
    WeightEstimate,
    evaluate_fit,
)

PROFILE_ARCHS = sorted(DEVICE_MEMORY_PROFILE_BYTES)
UNPROFILED_ARCHS = ("arm64_jp4", "x86_64", "unknown-arch")

# ---------------------------------------------------------------------------
# The corrected model's arithmetic, mirrored LOCALLY from design Decision 2
# of jp6-vllm-kv-cache-oom-regression — so this test checks the module
# against the specification rather than against itself.
# ---------------------------------------------------------------------------
ACTIVATION_FLOOR_BYTES = 2 * GIB
# REPOINTED 2026-08-19 (jp6-vllm-kv-cache-oom-regression task 14 / H8).
# SUPERSEDED value, recorded verbatim:  ACTIVATION_WEIGHT_FRACTION = 0.75
# 0.75 was calibrated to one measured point now known to have been a TWO-unit
# (video-unbounded) measurement. The measured per-unit pair on
# `ryanorinagxdevkithomelabjp622` (1.0.62, `gpu_memory_utilization = 0.55`,
# 6.59 GiB of weights) is 2.47 GiB for ONE unit and 4.93 GiB for TWO, i.e.
# 2.47 / 6.59 = 0.375 per unit.
ACTIVATION_WEIGHT_FRACTION = 0.375
MULTIMODAL_IMAGE_INCREMENT = 1.0
# The non-torch/co-tenant residency vLLM subtracts from the SAME budget on
# every load, which the shipped `required` OMITTED entirely. ESTIMATE: median
# of seven measured `non_torch_memory` readings on that device (-0.05, 0.94,
# 0.98, 2.18, 3.67, 4.76, 8.29 GiB; median 2.18), rounded down to a whole GiB.
#
# CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
# jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED name and
# constant, recorded VERBATIM:
#     NON_TORCH_ALLOWANCE_BYTES = 2 * GIB
#     # The HARD KV term in `required` (PROPOSED, task 14 / H9). It replaces
#     # MINIMUM_KV_CACHE_BYTES in the sum: that 1 GiB constant keeps its value
#     # and its name but is the thin-margin WARNING threshold only, which is
#     # what design Decision 2 always said it was ("a serving-margin floor, not
#     # a hard load threshold" — 0.65 GiB of KV demonstrably SERVED at 2.95x
#     # for 4096 tokens, so charging it hard refused a configuration that
#     # works).
#     KV_VIABILITY_FLOOR_BYTES = int(0.25 * GIB)
# Reason: the operator's H9 decision is that `required` charges NO KV term at
# all — hard or soft — because the KV cache is the remainder the budget leaves
# over, exactly how vLLM computes it; and the shipped constant is
# NON_TORCH_MEMORY_BYTES. This oracle keeps its own independent copies so it
# still checks the module against the specification rather than against itself.
NON_TORCH_MEMORY_BYTES = 2 * GIB
CO_TENANCY_RESERVATION_BYTES = {
    'arm64_jp6': 6 * GIB,   # measured: ~5.7 GiB of ONNX Triton stubs + containers
    'arm64_jp5': 6 * GIB,   # same 30 GiB profile class
    'arm64_jp7': 8 * GIB,   # design estimate, unmeasured [HARDWARE H8]
}
DEFAULT_IMAGES_PER_PROMPT = 1
# Videos per prompt when `limit_mm_per_prompt.video` is NOT authored: vLLM's
# own per-modality default of 1, i.e. UNBOUNDED. Repointed for the video
# widening (jp6-vllm-kv-cache-oom-regression, 2026-08-19) — MEASURED on
# `ryanorinagxdevkithomelabjp622` (LocalServer.arm64JP6 1.0.62) at
# `gpu_memory_utilization = 0.55`: `{'image': 1, 'video': 0}` profiled a
# 2.47 GiB activation peak (KV 6.43 GiB, 29.41x, READY) and `{'image': 1}`
# alone profiled 4.93 GiB (KV 0.20 GiB, 0.89x, FAILED), because vLLM reserves
# half of its 32768-token worst case for video (`{'image': 16384,
# 'video': 16384}`). The generators below author no multimodal limit at all,
# so their scope is the TWO-unit (image + unbounded video) shape.
DEFAULT_VIDEOS_PER_PROMPT = 1
DEFAULT_MULTIMODAL_UNITS = DEFAULT_IMAGES_PER_PROMPT + DEFAULT_VIDEOS_PER_PROMPT


def format_gib(num_bytes):
    """Mirror the module's GiB rendering ('14.25 GiB')."""
    return f"{num_bytes / GIB:.2f} GiB"


def expected_activation_allowance(weights_bytes,
                                  units=DEFAULT_MULTIMODAL_UNITS):
    """design Decision 2 as amended 2026-08-19: ``max(floor, fraction ×
    weights) × (1 + increment × (units − 1))`` — an ESTIMATE, deliberately
    erring high. ``units`` is the TOTAL of the authored per-modality limits
    (images + videos), not the image count alone: vLLM reserves its worst-case
    multimodal token budget per modality, so an unauthored video modality
    costs a full extra unit. The default is therefore what a configuration
    authoring NOTHING is sized for."""
    base = max(ACTIVATION_FLOOR_BYTES,
               ACTIVATION_WEIGHT_FRACTION * weights_bytes)
    return int(base * (1.0 + MULTIMODAL_IMAGE_INCREMENT * (max(1, units) - 1)))


def expected_fraction_cap(arch):
    """``(profile − co_tenancy) / profile`` — JP6 ``(30 − 6)/30 = 0.80``."""
    profile = DEVICE_MEMORY_PROFILE_BYTES[arch]
    return (profile - CO_TENANCY_RESERVATION_BYTES[arch]) / profile


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# gpu_memory_utilization in (0, 1]; DynamoDB round trips yield Decimal, so
# the configuration may carry float or Decimal (rounded so Decimal(str(x))
# stays sane), or omit the setting entirely (documented default applies).
_utilizations = st.floats(
    min_value=1e-6, max_value=1.0, allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 6)).filter(lambda x: 0.0 < x <= 1.0)


@st.composite
def engine_configurations(draw):
    """(configuration dict, effective utilization) — the setting present as
    float or Decimal, or absent (default applies).

    NOTE (jp6-vllm-kv-cache-oom-regression): deliberately does NOT emit
    ``limit_mm_per_prompt`` at all, so every example exercises the UNAUTHORED
    multimodal shape — one image plus vLLM's own unbounded video default, i.e.
    :data:`DEFAULT_MULTIMODAL_UNITS` units. The sibling preservation suite
    (`test_property_jp6_fit_preservation.py`) imports this generator and
    computes its corrected-model scope at that same default; the AUTHORED
    shapes (`{"image": N}`, `{"video": 0}`, both) are generated by the
    fix-checking suites that own the multimodal term
    (`test_property_jp6_fit_check_soundness.py`,
    `test_property_jp6_fit_check_video_units.py`)."""
    kind = draw(st.sampled_from(("float", "decimal", "absent")))
    if kind == "absent":
        return {}, DEFAULT_GPU_MEMORY_UTILIZATION
    util = draw(_utilizations)
    value = Decimal(str(util)) if kind == "decimal" else util
    return {"gpu_memory_utilization": value, "dtype": "auto"}, util


# Estimates from tiny to far beyond any budget (~200 GiB), biased around the
# fit boundary so both outcomes are exercised. Passed as a raw int or as a
# WeightEstimate (evaluate_fit accepts both).
_estimate_bytes = st.one_of(
    st.integers(min_value=0, max_value=200 * GIB),
    st.integers(min_value=13 * GIB, max_value=17 * GIB),
)


@st.composite
def estimates(draw):
    """(estimate argument, estimate bytes)."""
    total_bytes = draw(_estimate_bytes)
    if draw(st.booleans()):
        return WeightEstimate(total_bytes=total_bytes, method="param_count",
                              detail="synthetic"), total_bytes
    return total_bytes, total_bytes


# Architecture sets mixing profiled and unprofiled entries (possibly with
# duplicates, possibly empty).
_architecture_sets = st.lists(
    st.sampled_from(PROFILE_ARCHS + list(UNPROFILED_ARCHS)),
    min_size=0, max_size=6,
)


# ---------------------------------------------------------------------------
# Property 4: Fit_Check decision correctness
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(config_case=engine_configurations(), estimate_case=estimates(),
       architectures=_architecture_sets)
def test_fit_decision_correctness(config_case, estimate_case, architectures):
    """**Feature: vllm-sizing-and-packaging-errors, Property 4: Fit_Check
    decision correctness** (repointed by
    jp6-vllm-kv-cache-oom-regression Decision 2)

    fits ⟺ condition A (budget ≥ weights + activation allowance + KV floor)
    AND condition B (utilization ≤ the co-tenancy cap) for every profiled
    architecture, and every failing message names the profile entry, the
    budget, the estimate, the activation and co-tenancy terms, leads with
    demand-reducing remediation, and offers raising
    gpu_memory_utilization only with the cap stated — never advice to lower
    it (Requirements 3.1, 3.8, 3.9)."""
    engine_configuration, utilization = config_case
    estimate_arg, estimate_bytes = estimate_case

    findings = evaluate_fit(engine_configuration, estimate_arg, architectures)

    # Exactly one finding per profiled architecture, in input order;
    # architectures without a Device_Memory_Profile entry are skipped (3.1).
    profiled = [a for a in architectures
                if a in DEVICE_MEMORY_PROFILE_BYTES]
    assert [f.arch for f in findings] == profiled, (
        f"expected findings for {profiled}, got "
        f"{[f.arch for f in findings]}")

    # required = weights + non-torch allowance + activation allowance. NO KV
    # term is charged (task 14 / H9): the KV cache is the remainder the budget
    # leaves over. SUPERSEDED expression, recorded verbatim:
    #     required_bytes = (estimate_bytes + activation_bytes
    #                       + MINIMUM_KV_CACHE_BYTES)
    # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (task 14 / H9). SUPERSEDED
    # expression, recorded VERBATIM — it charged the INTERMEDIATE hard 0.25 GiB
    # viability floor and named the intermediate constant:
    #     required_bytes = (estimate_bytes + NON_TORCH_ALLOWANCE_BYTES
    #                       + activation_bytes + KV_VIABILITY_FLOOR_BYTES)
    activation_bytes = expected_activation_allowance(estimate_bytes)
    required_bytes = (estimate_bytes + NON_TORCH_MEMORY_BYTES
                      + activation_bytes)
    for finding in findings:
        profile_bytes = DEVICE_MEMORY_PROFILE_BYTES[finding.arch]
        budget_bytes = int(float(utilization) * profile_bytes)
        cap = expected_fraction_cap(finding.arch)

        # Budget = gpu_memory_utilization × profile[arch] (unchanged —
        # the profile values and this identity are preserved); required now
        # carries the activation allowance (Requirement 3.1 as revised).
        assert finding.budget_bytes == budget_bytes, (
            f"{finding.arch}: budget {finding.budget_bytes} != "
            f"utilization {utilization} × profile {profile_bytes}")
        assert finding.required_bytes == required_bytes, (
            f"{finding.arch}: required {finding.required_bytes} != "
            f"estimate {estimate_bytes} + non-torch allowance "
            f"{NON_TORCH_MEMORY_BYTES} + activation allowance "
            f"{activation_bytes}")
        # The 1 GiB serving margin is the WARNING threshold, not a term.
        # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (task 14 / H9).
        # SUPERSEDED assertions, recorded VERBATIM:
        #     assert (finding.kv_viability_floor_bytes
        #             == KV_VIABILITY_FLOOR_BYTES)
        #     assert finding.non_torch_bytes == NON_TORCH_ALLOWANCE_BYTES
        # Reason: no KV term is charged, so the finding reports no viability
        # floor. Replaced by a positive pin that NO KV amount is inside
        # `required` — strictly stronger than pinning one term's value.
        assert finding.kv_floor_bytes == MINIMUM_KV_CACHE_BYTES
        assert not hasattr(finding, "kv_viability_floor_bytes")
        assert finding.non_torch_bytes == NON_TORCH_MEMORY_BYTES
        assert (finding.required_bytes
                == estimate_bytes + finding.non_torch_bytes
                + finding.activation_bytes)

        # The decision: fits ⟺ A ∧ B (3.1 as revised by Decision 2).
        condition_a = budget_bytes >= required_bytes
        condition_b = float(utilization) <= cap
        expected_fits = condition_a and condition_b
        assert finding.fits is expected_fits, (
            f"{finding.arch}: fits={finding.fits} but budget "
            f"{budget_bytes} vs required {required_bytes} (A={condition_a}) "
            f"and utilization {utilization} vs cap {cap:.4f} "
            f"(B={condition_b}) imply {expected_fits}")

        # Every message names the profile entry used (Requirement 3.8).
        assert finding.arch in finding.message

        if not finding.fits:
            # Failing message states the budget and the estimate (3.6/3.8
            # message content) ...
            assert format_gib(budget_bytes) in finding.message, (
                f"{finding.arch}: failing message must state the budget "
                f"{format_gib(budget_bytes)}: {finding.message!r}")
            assert format_gib(estimate_bytes) in finding.message, (
                f"{finding.arch}: failing message must state the estimate "
                f"{format_gib(estimate_bytes)}: {finding.message!r}")
            # ... names the activation allowance, labelled an ESTIMATE, and
            # the co-tenancy term (design Decision 2/3: an operator must be
            # able to audit the verdict instead of trusting it) ...
            assert format_gib(activation_bytes) in finding.message, (
                f"{finding.arch}: failing message must state the activation "
                f"allowance {format_gib(activation_bytes)}: "
                f"{finding.message!r}")
            assert re.search(r"activation", finding.message, re.IGNORECASE), (
                f"{finding.arch}: failing message names no activation term: "
                f"{finding.message!r}")
            assert re.search(r"estimate", finding.message, re.IGNORECASE), (
                f"{finding.arch}: the activation allowance must be labelled "
                f"an estimate: {finding.message!r}")
            assert re.search(r"co-tenan|co-resident|other consumers",
                             finding.message, re.IGNORECASE), (
                f"{finding.arch}: failing message states no co-tenancy "
                f"term: {finding.message!r}")
            # ... leads with the remediations that reduce our own demand
            # (Decision 3's ordering — the only ordering that cannot starve
            # the co-resident ONNX GPU models) ...
            demand_reducing = re.search(
                r"max_model_len|smaller.{0,20}model|limit_mm_per_prompt|"
                r"free device memory", finding.message, re.IGNORECASE)
            assert demand_reducing, (
                f"{finding.arch}: failing message offers nothing that "
                f"reduces demand: {finding.message!r}")
            raising = re.search(r"rais(e|ing)\W", finding.message,
                                re.IGNORECASE)
            if raising:
                assert demand_reducing.start() < raising.start(), (
                    f"{finding.arch}: the remediation mentions raising the "
                    f"fraction before the demand-reducing options: "
                    f"{finding.message!r}")
            # ... and offers raising the fraction only below the cap and
            # only with the cap stated (Requirement 3.9 as narrowed by
            # Decision 3 — the hazard is that the fraction is of TOTAL
            # memory on a device whose co-tenants already hold ~6 GiB).
            offers_raise = re.search(r"raise\s+.{0,40}gpu_memory_utilization",
                                     finding.message, re.IGNORECASE)
            if offers_raise:
                assert float(utilization) < cap, (
                    f"{finding.arch}: raising gpu_memory_utilization is "
                    f"offered although {utilization} is already at/above the "
                    f"{cap:.2f} cap: {finding.message!r}")
                assert f"{cap:.2f}" in finding.message, (
                    f"{finding.arch}: raising gpu_memory_utilization is "
                    f"offered without stating the {cap:.2f} cap: "
                    f"{finding.message!r}")
            # KEPT VERBATIM from the sibling spec, never weakened: no
            # message may advise LOWERING the fraction as a cure for
            # insufficient KV cache (Requirement 3.9).
            assert not re.search(r"(lower|decrease|reduce)\w*\s+"
                                 r"gpu_memory_utilization",
                                 finding.message, re.IGNORECASE), (
                f"{finding.arch}: failing message must never advise "
                f"lowering gpu_memory_utilization: {finding.message!r}")
