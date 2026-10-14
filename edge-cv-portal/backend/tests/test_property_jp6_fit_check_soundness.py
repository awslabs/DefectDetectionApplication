"""
Fix-checking properties for the SOUND Fit_Check (spec:
jp6-vllm-kv-cache-oom-regression, task 4.1).

**Property 1: Bug Condition — Unsound Fit_Check verdict** (design
"Correctness Properties"). _For any_ (utilization, weights, architecture
set, images):

  - the verdict equals **A ∧ B** exactly, where
      A (budget sufficiency): ``util × profile[arch] >= weights +
        NON_TORCH_MEMORY_BYTES +
        activation_allowance(weights, multimodal_units)``  (amended
        2026-08-19, task 14 / H8+H9 — the superseded form charged
        ``+ MINIMUM_KV_CACHE_BYTES`` and no non-torch term at all; an
        INTERMEDIATE form of the same amendment, also superseded, charged
        ``+ KV_VIABILITY_FLOOR_BYTES`` — H9's final decision charges NO KV
        term, so the KV cache is the remainder the budget leaves over)
      B (co-tenancy safety):  ``util <= (profile[arch] −
        CO_TENANCY_RESERVATION_BYTES[arch]) / profile[arch]``
  - _for every_ generated failing finding the message names the weights,
    the non-torch allowance (labelled an ESTIMATE), the activation allowance
    (labelled an ESTIMATE), the predicted KV remainder against the
    serving-margin floor (superseded 2026-08-19 second pass: "the KV
    viability floor" — H9 charges no KV term, so the remainder is what the
    message owes), the budget, the
    co-tenancy reservation and the Fraction_Cap **with their numbers**;
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
    NON_TORCH_MEMORY_BYTES,
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

# The VIDEO dimension (widened schema, 2026-08-19): ``None`` means the key is
# not authored at all, which is NOT free — vLLM applies its own per-modality
# default of 1, so the modality costs a full extra unit. MEASURED on
# `ryanorinagxdevkithomelabjp622` at `gpu_memory_utilization = 0.55`:
# ``{"image": 1, "video": 0}`` profiled a 2.47 GiB activation peak (KV
# 6.43 GiB, 29.41x, READY); ``{"image": 1}`` profiled 4.93 GiB (KV 0.20 GiB,
# 0.89x, FAILED). An explicit ``0`` is therefore strictly cheaper than an
# absent key, and both arms are generated here.
_videos_cases = st.one_of(st.none(), st.integers(min_value=0, max_value=2))

#: Videos vLLM assumes when the key is not authored (its own default).
UNAUTHORED_VIDEOS = 1


def _with_multimodal_limit(engine_configuration, images, videos):
    """Overlay the authored multimodal limit; return
    ``(config, effective_images, effective_videos, effective_units)``.

    ``images is None`` / ``videos is None`` mean the sub-key is not authored,
    so vLLM's own per-modality default of 1 applies to it."""
    config = dict(engine_configuration)
    limit = {}
    if images is not None:
        limit["image"] = images
    if videos is not None:
        limit["video"] = videos
    if limit:
        config["limit_mm_per_prompt"] = limit
    effective_images = 1 if images is None else images
    effective_videos = UNAUTHORED_VIDEOS if videos is None else videos
    return (config, effective_images, effective_videos,
            effective_images + effective_videos)


_NEVER_LOWER = re.compile(
    r"(lower|decrease|reduce)\w*\s+gpu_memory_utilization", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Property 1, verdict leg: fits equals A ∧ B exactly
# ---------------------------------------------------------------------------

# Validates: Requirements 2.1, 2.2
@settings(deadline=None)
@given(config_case=engine_configurations(), estimate_case=estimates(),
       architectures=_architecture_sets, images=_images_cases,
       videos=_videos_cases)
def test_verdict_equals_conjunction_of_budget_and_co_tenancy(
        config_case, estimate_case, architectures, images, videos):
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
    (config, effective_images, effective_videos,
     effective_units) = _with_multimodal_limit(engine_configuration, images,
                                               videos)

    findings = evaluate_fit(config, estimate_arg, architectures)

    profiled = [a for a in architectures if a in DEVICE_MEMORY_PROFILE_BYTES]
    assert [f.arch for f in findings] == profiled

    # REPOINTED 2026-08-19 (task 14 / H8 + H9). SUPERSEDED expression,
    # recorded verbatim:
    #     required_bytes = (estimate_bytes + activation_bytes
    #                       + MINIMUM_KV_CACHE_BYTES)
    # `required` now charges the non-torch residency vLLM always consumes and
    # the small HARD KV VIABILITY floor; the 1 GiB serving margin became the
    # thin-margin WARNING threshold it was always documented to be.
    #
    # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
    # jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED expression,
    # recorded VERBATIM:
    #     required_bytes = (estimate_bytes + NON_TORCH_ALLOWANCE_BYTES
    #                       + activation_bytes + KV_VIABILITY_FLOOR_BYTES)
    # Reason: the operator's H9 decision charges NO KV term in `required`, hard
    # or soft — the KV cache is the remainder the budget leaves over.
    activation_bytes = expected_activation_allowance(
        estimate_bytes, effective_units)
    required_bytes = (estimate_bytes + NON_TORCH_MEMORY_BYTES
                      + activation_bytes)

    for finding in findings:
        profile_bytes = DEVICE_MEMORY_PROFILE_BYTES[finding.arch]
        budget_bytes = int(float(utilization) * profile_bytes)
        cap = expected_fraction_cap(finding.arch)

        assert finding.budget_bytes == budget_bytes, (
            f"{finding.arch}: budget {finding.budget_bytes} != "
            f"int({utilization} × {profile_bytes})")
        assert finding.required_bytes == required_bytes, (
            f"{finding.arch}: required {finding.required_bytes} != weights "
            f"{estimate_bytes} + non-torch {NON_TORCH_MEMORY_BYTES} + "
            f"activation {activation_bytes} (at "
            f"{effective_units} multimodal unit(s): "
            f"{effective_images} image(s) + {effective_videos} video(s))")

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
        assert finding.videos_per_prompt == effective_videos
        assert finding.multimodal_units == effective_units
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
       architectures=_architecture_sets, images=_images_cases,
       videos=_videos_cases)
def test_failing_message_names_every_term_with_its_number(
        config_case, estimate_case, architectures, images, videos):
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
    (config, effective_images, effective_videos,
     effective_units) = _with_multimodal_limit(engine_configuration, images,
                                               videos)

    findings = evaluate_fit(config, estimate_arg, architectures)

    activation_bytes = expected_activation_allowance(
        estimate_bytes, effective_units)

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
        # REPOINTED 2026-08-19 (task 14 / H9). SUPERSEDED assertion, recorded
        # verbatim:
        #     assert format_gib(MINIMUM_KV_CACHE_BYTES) in message, (
        #         f"{finding.arch}: failing message misses the KV floor "
        #         f"{format_gib(MINIMUM_KV_CACHE_BYTES)}: {message!r}")
        # The term `required` charges — and therefore the one the message must
        # quote — is the KV VIABILITY floor. The 1 GiB serving margin is
        # quoted by the thin-margin warning, on PASSING verdicts.
        #
        # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
        # jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED
        # assertion, recorded VERBATIM:
        #     assert format_gib(KV_VIABILITY_FLOOR_BYTES) in message, (
        #         f"{finding.arch}: failing message misses the KV viability "
        #         f"floor {format_gib(KV_VIABILITY_FLOOR_BYTES)}: {message!r}")
        # Reason: `required` charges no KV term, so there is no viability floor
        # for the message to quote. What the message owes instead — and what is
        # asserted here in its place, strictly more than a bare constant — is
        # the PREDICTED KV REMAINDER the budget leaves over, stated against the
        # 1 GiB serving-margin floor (the H9 surface itself).
        assert (f"leaving a predicted KV cache remainder of "
                f"{format_gib(finding.kv_headroom_bytes)} against the "
                f"{format_gib(MINIMUM_KV_CACHE_BYTES)} serving-margin floor"
                in message), (
            f"{finding.arch}: failing message does not state the predicted KV "
            f"remainder against the serving-margin floor: {message!r}")
        assert "viability floor" not in message, (
            f"{finding.arch}: the message still claims a KV viability floor "
            f"that `required` does not charge: {message!r}")
        # ... and the non-torch allowance, labelled an ESTIMATE like the
        # activation allowance (it is the term the shipped model omitted).
        assert format_gib(NON_TORCH_MEMORY_BYTES) in message, (
            f"{finding.arch}: failing message misses the non-torch allowance "
            f"{format_gib(NON_TORCH_MEMORY_BYTES)}: {message!r}")
        assert (f"non-torch allowance "
                f"{format_gib(NON_TORCH_MEMORY_BYTES)} (an ESTIMATE"
                in message), (
            f"{finding.arch}: the non-torch allowance is not labelled an "
            f"ESTIMATE: {message!r}")
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
        # ... and the multimodal units the allowance assumed, per modality
        # (the widened schema, 2026-08-19: an unauthored `video` is a full
        # extra unit, so the message must say which units it sized for).
        assert f"{effective_units} multimodal unit(s) per prompt" in message, (
            f"{finding.arch}: failing message does not name the "
            f"{effective_units} multimodal unit(s) the allowance assumed: "
            f"{message!r}")
        assert (f"{effective_images} image(s) + {effective_videos} video(s)"
                in message), (
            f"{finding.arch}: failing message does not break the units down "
            f"per modality: {message!r}")
        if videos is None:
            assert 'limit_mm_per_prompt.video is NOT authored' in message, (
                f"{finding.arch}: an unauthored video modality must be "
                f"called out as the extra unit it is: {message!r}")


# ---------------------------------------------------------------------------
# Property 1, remediation leg: demand-reduction first, the fraction only
# within the cap, never lower
# ---------------------------------------------------------------------------

# Validates: Requirements 2.3
@settings(deadline=None)
@given(config_case=engine_configurations(), estimate_case=estimates(),
       architectures=_architecture_sets, images=_images_cases,
       videos=_videos_cases)
def test_remediation_orders_demand_reduction_and_respects_the_cap(
        config_case, estimate_case, architectures, images, videos):
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
    config = _with_multimodal_limit(engine_configuration, images,
                                    videos)[0]

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
