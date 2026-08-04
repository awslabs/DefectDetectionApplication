"""Property test for the vLLM preflight fit-check decision
(vllm-sizing-and-packaging-errors, task 1.3).

**Feature: vllm-sizing-and-packaging-errors, Property 4: Fit_Check decision
correctness**

_For any_ engine configuration, weight estimate, and architecture set, a
FitFinding reports `fits = true` if and only if
`gpu_memory_utilization × DEVICE_MEMORY_PROFILE_BYTES[arch] ≥ estimate +
MINIMUM_KV_CACHE_BYTES`, and every failing finding's message contains the
architecture name, the budget, the estimate, and the word "raise" applied to
`gpu_memory_utilization` (never advice to lower it).

**Validates: Requirements 3.1, 3.8, 3.9**

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


def format_gib(num_bytes):
    """Mirror the module's GiB rendering ('14.25 GiB')."""
    return f"{num_bytes / GIB:.2f} GiB"


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
    float or Decimal, or absent (default applies)."""
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
    decision correctness**

    fits ⟺ gpu_memory_utilization × profile[arch] ≥ estimate +
    MINIMUM_KV_CACHE_BYTES for every profiled architecture, and every
    failing message names the profile entry, the budget, the estimate, and
    the raise-gpu_memory_utilization remediation — never advice to lower it
    (Requirements 3.1, 3.8, 3.9)."""
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

    required_bytes = estimate_bytes + MINIMUM_KV_CACHE_BYTES
    for finding in findings:
        profile_bytes = DEVICE_MEMORY_PROFILE_BYTES[finding.arch]
        budget_bytes = int(float(utilization) * profile_bytes)

        # Budget = gpu_memory_utilization × profile[arch]; required =
        # estimate + minimum KV cache (Requirement 3.1).
        assert finding.budget_bytes == budget_bytes, (
            f"{finding.arch}: budget {finding.budget_bytes} != "
            f"utilization {utilization} × profile {profile_bytes}")
        assert finding.required_bytes == required_bytes, (
            f"{finding.arch}: required {finding.required_bytes} != "
            f"estimate {estimate_bytes} + min KV cache "
            f"{MINIMUM_KV_CACHE_BYTES}")

        # The decision: fits ⟺ budget ≥ estimate + minimum KV cache (3.1).
        expected_fits = budget_bytes >= required_bytes
        assert finding.fits is expected_fits, (
            f"{finding.arch}: fits={finding.fits} but budget "
            f"{budget_bytes} vs required {required_bytes} implies "
            f"{expected_fits}")

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
            # ... and the remediation in the correct direction: raise
            # gpu_memory_utilization, never lower it (Requirement 3.9).
            assert re.search(r"raise\s+gpu_memory_utilization",
                             finding.message, re.IGNORECASE), (
                f"{finding.arch}: failing message must advise raising "
                f"gpu_memory_utilization: {finding.message!r}")
            assert not re.search(r"(lower|decrease|reduce)\w*\s+"
                                 r"gpu_memory_utilization",
                                 finding.message, re.IGNORECASE), (
                f"{finding.arch}: failing message must never advise "
                f"lowering gpu_memory_utilization: {finding.message!r}")
