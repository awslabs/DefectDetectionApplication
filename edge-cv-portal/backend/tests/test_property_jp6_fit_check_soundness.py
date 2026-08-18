"""
Fix-checking properties for the SOUND Fit_Check (spec:
jp6-vllm-kv-cache-oom-regression, task 4.1).

**Property 1: Bug Condition — Unsound Fit_Check verdict** (design
"Correctness Properties"). _For any_ (utilization, weights, architecture
set, images):

  - the verdict equals **A ∧ B** exactly, where
      A (budget sufficiency): ``util × profile[arch] >= weights +
        activation_allowance(weights, images) + MINIMUM_KV_CACHE_BYTES``
      B (co-tenancy safety):  ``util <= (profile[arch] −
        CO_TENANCY_RESERVATION_BYTES[arch]) / profile[arch]``
  - _for every_ generated failing finding the message names the weights,
    the activation allowance (labelled an ESTIMATE), the KV floor, the
    budget, the co-tenancy reservation and the Fraction_Cap **with their
    numbers**;
  - the remediation orders demand-reduction **before** raising the
    fraction;
  - the message never suggests a ``gpu_memory_utilization`` above
    ``fraction_cap(arch)``;
  - the never-lower invariant holds on EVERY message
    (`vllm-sizing-and-packaging-errors` Requirement 3.9, kept verbatim).

The expected arithmetic is mirrored from design Decision 2 via the sibling
spec's helpers (`expected_activation_allowance`, `expected_fraction_cap`),
so this suite checks `vllm_fit_check` against the SPECIFICATION rather than
against itself. Generators are the sibling's, reused verbatim
(`engine_configurations()`, `estimates()`, `_architecture_sets`), extended
here with a generated images-per-prompt dimension — the term the shipped
model never sized (defect 1.4).

Hypothesis budget comes from the conftest-registered profiles
(`portal-fast` / `ci`); NO ``max_examples`` is hardcoded anywhere in this
file.

Run (from ``edge-cv-portal/backend``, WITH the suite conftest):
    python3 -m pytest tests/test_property_jp6_fit_check_soundness.py \
      -q -p no:cacheprovider

# Validates: Requirements 2.1, 2.2, 2.3
"""
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from vllm_fit_check import (
    DEVICE_MEMORY_PROFILE_BYTES,
    MINIMUM_KV_CACHE_BYTES,
    evaluate_fit,
)

# The sibling spec's generators and Decision-2 arithmetic mirrors — reused
# verbatim so the two specs' guarantees stay directly comparable (task 4.1).
from test_property_fit_check_decision import (  # noqa: E402
    _architecture_sets,
    engine_configurations,
    estimates,
    expected_activation_allowance,
    expected_fraction_cap,
    format_gib,
)

# ---------------------------------------------------------------------------
# The images-per-prompt dimension: absent (defaults to 1) or an authored
# ``limit_mm_per_prompt = {"image": N}``. The sibling generator deliberately
# never emits the key (its own preservation scope is one image), so the
# overlay happens here — the fix-checking suite is precisely the place the
# multimodal term must be exercised (defect 1.4, Requirement 2.4's portal
# visibility half).
# ---------------------------------------------------------------------------
_images_cases = st.one_of(st.none(), st.integers(min_value=1, max_value=4))


def _with_images(engine_configuration, images):
    """Overlay the authored multimodal limit; return (config, effective)."""
    config = dict(engine_configuration)
    if images is None:
        return config, 1
    config["limit_mm_per_prompt"] = {"image": images}
    return config, images


_NEVER_LOWER = re.compile(
    r"(lower|decrease|reduce)\w*\s+gpu_memory_utilization", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Property 1, verdict leg: fits equals A ∧ B exactly
# ---------------------------------------------------------------------------

# Validates: Requirements 2.1, 2.2
@settings(deadline=None)
@given(config_case=engine_configurations(), estimate_case=estimates(),
       architectures=_architecture_sets, images=_images_cases)
def test_verdict_equals_conjunction_of_budget_and_co_tenancy(
        config_case, estimate_case, architectures, images):
    """**Property 1: Bug Condition — Unsound Fit_Check verdict** (verdict
    leg): for any (utilization, weights, arch set, images) the fixed
    ``evaluate_fit`` reports ``fits = A ∧ B`` EXACTLY — the activation
    allowance (scaled by the authored images) and the KV floor are inside
    ``required``, the budget keeps its ``int(util × profile)`` identity,
    and the Fraction_Cap gates the fraction — so a configuration whose
    device-side KV remainder would be negative can never again be reported
    as fitting with slack (defects 1.1/1.2).

    # Validates: Requirements 2.1, 2.2
    """
    engine_configuration, utilization = config_case
    estimate_arg, estimate_bytes = estimate_case
    config, effective_images = _with_images(engine_configuration, images)

    findings = evaluate_fit(config, estimate_arg, architectures)

    profiled = [a for a in architectures if a in DEVICE_MEMORY_PROFILE_BYTES]
    assert [f.arch for f in findings] == profiled

    activation_bytes = expected_activation_allowance(
        estimate_bytes, effective_images)
    required_bytes = (estimate_bytes + activation_bytes
                      + MINIMUM_KV_CACHE_BYTES)

    for finding in findings:
        profile_bytes = DEVICE_MEMORY_PROFILE_BYTES[finding.arch]
        budget_bytes = int(float(utilization) * profile_bytes)
        cap = expected_fraction_cap(finding.arch)

        assert finding.budget_bytes == budget_bytes, (
            f"{finding.arch}: budget {finding.budget_bytes} != "
            f"int({utilization} × {profile_bytes})")
        assert finding.required_bytes == required_bytes, (
            f"{finding.arch}: required {finding.required_bytes} != weights "
            f"{estimate_bytes} + activation {activation_bytes} (at "
            f"{effective_images} image(s)) + KV floor "
            f"{MINIMUM_KV_CACHE_BYTES}")

        condition_a = budget_bytes >= required_bytes
        condition_b = float(utilization) <= cap
        assert finding.fits is (condition_a and condition_b), (
            f"{finding.arch}: fits={finding.fits} but A={condition_a} "
            f"(budget {format_gib(budget_bytes)} vs required "
            f"{format_gib(required_bytes)}) and B={condition_b} "
            f"(util {utilization} vs cap {cap:.4f}) imply "
            f"{condition_a and condition_b}")

        # The additive audit-trail fields agree with the verdict's terms.
        assert finding.weights_bytes == estimate_bytes
        assert finding.activation_bytes == activation_bytes
        assert finding.kv_floor_bytes == MINIMUM_KV_CACHE_BYTES
        assert finding.images_per_prompt == effective_images
        expected_failed = []
        if not condition_a:
            expected_failed.append('budget')
        if not condition_b:
            expected_failed.append('co_tenancy')
        assert finding.failed_conditions == expected_failed, (
            f"{finding.arch}: failed_conditions "
            f"{finding.failed_conditions} != {expected_failed}")


# ---------------------------------------------------------------------------
# Property 1, message leg: every failing finding names every term with its
# number
# ---------------------------------------------------------------------------

# Validates: Requirements 2.1, 2.2
@settings(deadline=None)
@given(config_case=engine_configurations(), estimate_case=estimates(),
       architectures=_architecture_sets, images=_images_cases)
def test_failing_message_names_every_term_with_its_number(
        config_case, estimate_case, architectures, images):
    """**Property 1: Bug Condition — Unsound Fit_Check verdict** (message
    leg): every generated FAILING finding's message names the weights, the
    activation allowance (labelled an ESTIMATE), the KV floor, the budget,
    the co-tenancy reservation and the Fraction_Cap — each WITH its number
    — so an operator can audit the verdict instead of trusting it (design
    Decision 2/3).

    # Validates: Requirements 2.1, 2.2
    """
    engine_configuration, utilization = config_case
    estimate_arg, estimate_bytes = estimate_case
    config, effective_images = _with_images(engine_configuration, images)

    findings = evaluate_fit(config, estimate_arg, architectures)

    activation_bytes = expected_activation_allowance(
        estimate_bytes, effective_images)

    for finding in findings:
        if finding.fits:
            continue
        message = finding.message
        cap = expected_fraction_cap(finding.arch)

        assert format_gib(estimate_bytes) in message, (
            f"{finding.arch}: failing message misses the weights "
            f"{format_gib(estimate_bytes)}: {message!r}")
        assert format_gib(activation_bytes) in message, (
            f"{finding.arch}: failing message misses the activation "
            f"allowance {format_gib(activation_bytes)}: {message!r}")
        assert re.search(r"\bESTIMATE\b", message), (
            f"{finding.arch}: the activation allowance is not labelled an "
            f"ESTIMATE: {message!r}")
        assert format_gib(MINIMUM_KV_CACHE_BYTES) in message, (
            f"{finding.arch}: failing message misses the KV floor "
            f"{format_gib(MINIMUM_KV_CACHE_BYTES)}: {message!r}")
        assert format_gib(finding.budget_bytes) in message, (
            f"{finding.arch}: failing message misses the budget "
            f"{format_gib(finding.budget_bytes)}: {message!r}")
        assert format_gib(finding.co_tenancy_bytes) in message, (
            f"{finding.arch}: failing message misses the co-tenancy "
            f"reservation {format_gib(finding.co_tenancy_bytes)}: "
            f"{message!r}")
        assert f"{cap:.2f}" in message, (
            f"{finding.arch}: failing message misses the {cap:.2f} "
            f"Fraction_Cap: {message!r}")


# ---------------------------------------------------------------------------
# Property 1, remediation leg: demand-reduction first, the fraction only
# within the cap, never lower
# ---------------------------------------------------------------------------

# Validates: Requirements 2.3
@settings(deadline=None)
@given(config_case=engine_configurations(), estimate_case=estimates(),
       architectures=_architecture_sets, images=_images_cases)
def test_remediation_orders_demand_reduction_and_respects_the_cap(
        config_case, estimate_case, architectures, images):
    """**Property 1: Bug Condition — Unsound Fit_Check verdict**
    (remediation leg): every generated failing finding's remediation lists
    the demand-reducing options BEFORE any mention of raising the fraction;
    no message ever suggests a ``gpu_memory_utilization`` above
    ``fraction_cap(arch)``; and the never-lower invariant holds on EVERY
    message, passing or failing (defect 1.3, design Decision 3).

    # Validates: Requirements 2.3
    """
    engine_configuration, utilization = config_case
    estimate_arg, estimate_bytes = estimate_case
    config, _effective_images = _with_images(engine_configuration, images)

    findings = evaluate_fit(config, estimate_arg, architectures)

    for finding in findings:
        message = finding.message
        cap = expected_fraction_cap(finding.arch)

        # The never-lower invariant holds on EVERY message.
        assert not _NEVER_LOWER.search(message), (
            f"{finding.arch}: a message advises lowering "
            f"gpu_memory_utilization: {message!r}")

        if finding.fits:
            continue

        # Demand-reduction is offered, and BEFORE any raising talk.
        demand_reducing = re.search(
            r"max_model_len|limit_mm_per_prompt|smaller.{0,30}model|"
            r"free device memory", message, re.IGNORECASE)
        assert demand_reducing, (
            f"{finding.arch}: failing message offers nothing that reduces "
            f"demand: {message!r}")
        raising = re.search(r"rais(e|ing)\W", message, re.IGNORECASE)
        if raising:
            assert demand_reducing.start() < raising.start(), (
                f"{finding.arch}: raising the fraction is mentioned before "
                f"the demand-reducing options: {message!r}")

        # Raising is offered only below the cap, with the cap stated, and
        # the suggested target fraction never exceeds the cap.
        offers_raise = re.search(
            r"raise\s+.{0,40}gpu_memory_utilization", message, re.IGNORECASE)
        if offers_raise:
            assert float(utilization) < cap, (
                f"{finding.arch}: raising is offered although "
                f"{utilization} is already at/above the {cap:.4f} cap: "
                f"{message!r}")
            assert f"{cap:.2f}" in message, (
                f"{finding.arch}: raising is offered without stating the "
                f"{cap:.2f} cap: {message!r}")
            at_most = re.search(r"raised to at most (\d+\.\d+)", message)
            assert at_most, (
                f"{finding.arch}: raising is offered without bounding the "
                f"target fraction: {message!r}")
            assert float(at_most.group(1)) <= cap + 1e-9, (
                f"{finding.arch}: the message suggests a fraction "
                f"{at_most.group(1)} above the {cap:.4f} cap: {message!r}")
            # When the needed fraction exceeds the cap, the message must
            # say so rather than leave the suggestion actionable.
            at_least = re.search(r"at least (\d+\.\d+)", message)
            if at_least and float(at_least.group(1)) > cap + 1e-9:
                assert ("cannot make this configuration fit safely"
                        in message), (
                    f"{finding.arch}: the needed fraction "
                    f"{at_least.group(1)} exceeds the {cap:.2f} cap but the "
                    f"message does not say raising cannot fit safely: "
                    f"{message!r}")
