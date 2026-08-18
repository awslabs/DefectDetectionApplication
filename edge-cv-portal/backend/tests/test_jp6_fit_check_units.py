"""
Unit tests for the SOUND Fit_Check helpers, message composition and the
four worked verdicts (spec: jp6-vllm-kv-cache-oom-regression, task 4.1,
design "Unit Tests" list + Decision 2 "Worked verdicts").

Covered here:
  - ``activation_allowance`` / ``fraction_cap`` / ``images_per_prompt``
    edge cases: zero and tiny weights, ``Decimal`` utilizations, malformed
    ``limit_mm_per_prompt``, unknown architecture.
  - Message composition: every term present with its number, remediation
    ordering (hazard → demand reduction → cap-bounded last resort), the
    never-lower invariant, and the cap sentence appearing only when
    relevant.
  - The four worked verdicts from design Decision 2, computed from repo
    constants and the incident's measured numbers.

Run (from ``edge-cv-portal/backend``, WITH the suite conftest):
    python3 -m pytest tests/test_jp6_fit_check_units.py \
      -q -p no:cacheprovider

# Validates: Requirements 2.1, 2.2, 2.3
"""
import re
from decimal import Decimal

import pytest

from vllm_fit_check import (
    ACTIVATION_FLOOR_BYTES,
    ACTIVATION_WEIGHT_FRACTION,
    CO_TENANCY_RESERVATION_BYTES,
    DEFAULT_GPU_MEMORY_UTILIZATION,
    DEVICE_MEMORY_PROFILE_BYTES,
    GIB,
    MINIMUM_KV_CACHE_BYTES,
    activation_allowance,
    co_tenancy_reservation_bytes,
    evaluate_fit,
    fraction_cap,
    images_per_prompt,
)

_NEVER_LOWER = re.compile(
    r"(lower|decrease|reduce)\w*\s+gpu_memory_utilization", re.IGNORECASE)


def _gib(num_bytes):
    return f"{num_bytes / GIB:.2f} GiB"


# ---------------------------------------------------------------------------
# activation_allowance edge cases
# Validates: Requirements 2.1
# ---------------------------------------------------------------------------

class TestActivationAllowance:
    def test_zero_weights_get_the_floor(self):
        assert activation_allowance(0) == ACTIVATION_FLOOR_BYTES

    def test_tiny_weights_get_the_floor(self):
        # 1 KiB of weights: the fraction term rounds to nothing; the floor
        # keeps small models carrying an allowance.
        assert activation_allowance(1024) == ACTIVATION_FLOOR_BYTES

    def test_fraction_of_weights_above_the_floor(self):
        weights = 4 * GIB  # 0.75 × 4 = 3 GiB > the 2 GiB floor
        assert activation_allowance(weights) == int(
            ACTIVATION_WEIGHT_FRACTION * weights)

    def test_second_image_doubles_the_allowance(self):
        # MULTIMODAL_IMAGE_INCREMENT = 1.0: two images = 2× the one-image
        # allowance (the term defect 1.4's setdefault silently enlarged).
        weights = 8 * GIB
        assert activation_allowance(weights, 2) == 2 * activation_allowance(
            weights, 1)

    def test_images_below_one_clamp_to_one(self):
        weights = 8 * GIB
        one_image = activation_allowance(weights, 1)
        assert activation_allowance(weights, 0) == one_image
        assert activation_allowance(weights, -3) == one_image

    def test_hostile_weights_degrade_to_the_floor_never_raise(self):
        for hostile in (None, "garbage", [], {}):
            assert activation_allowance(hostile) == ACTIVATION_FLOOR_BYTES

    def test_hostile_images_degrade_to_one_never_raise(self):
        weights = 8 * GIB
        one_image = activation_allowance(weights, 1)
        for hostile in (None, "two", []):
            assert activation_allowance(weights, hostile) == one_image


# ---------------------------------------------------------------------------
# fraction_cap edge cases
# Validates: Requirements 2.2
# ---------------------------------------------------------------------------

class TestFractionCap:
    def test_jp6_cap_is_0_80(self):
        # (30 − 6) / 30 = 0.80 — the cap that protects the three ONNX
        # co-tenants on JP6 unified memory.
        assert fraction_cap('arm64_jp6') == pytest.approx(0.80)

    def test_jp5_cap_is_0_80(self):
        assert fraction_cap('arm64_jp5') == pytest.approx(0.80)

    def test_jp7_cap_is_0_9333(self):
        # (120 − 8) / 120 = 0.9333…
        assert fraction_cap('arm64_jp7') == pytest.approx(112 / 120)

    def test_unknown_architecture_returns_none(self):
        assert fraction_cap('x86_64') is None
        assert fraction_cap('arm64_jp4') is None
        assert fraction_cap(None) is None

    def test_reservation_falls_back_to_jp6_for_unknown_arch(self):
        assert co_tenancy_reservation_bytes('unknown-arch') == \
            CO_TENANCY_RESERVATION_BYTES['arm64_jp6']


# ---------------------------------------------------------------------------
# images_per_prompt edge cases (malformed limit_mm_per_prompt)
# Validates: Requirements 2.1
# ---------------------------------------------------------------------------

class TestImagesPerPrompt:
    def test_missing_key_defaults_to_one(self):
        assert images_per_prompt({}) == 1
        assert images_per_prompt(None) == 1

    def test_authored_value_is_used(self):
        assert images_per_prompt(
            {'limit_mm_per_prompt': {'image': 2}}) == 2

    def test_decimal_value_is_accepted(self):
        # DynamoDB round trips yield Decimal.
        assert images_per_prompt(
            {'limit_mm_per_prompt': {'image': Decimal('2')}}) == 2

    @pytest.mark.parametrize("malformed", [
        {'image': 'two'},        # non-numeric
        {'image': None},         # explicit null
        {'image': True},         # boolean is not a count
        {'image': 0},            # below the minimum
        {'image': -3},           # negative
        {'image': ['x']},        # wrong type
        'garbage',               # limit is not a dict and not a number
        ['image'],               # wrong container
    ])
    def test_malformed_limits_fall_back_to_one_never_raise(self, malformed):
        assert images_per_prompt({'limit_mm_per_prompt': malformed}) == 1


# ---------------------------------------------------------------------------
# Decimal utilizations and unknown architectures through evaluate_fit
# Validates: Requirements 2.1, 2.2
# ---------------------------------------------------------------------------

class TestEvaluateFitEdges:
    def test_decimal_utilization_sizes_the_budget(self):
        findings = evaluate_fit(
            {'gpu_memory_utilization': Decimal('0.4')}, 2 * GIB,
            ['arm64_jp6'])
        assert len(findings) == 1
        assert findings[0].budget_bytes == int(
            0.4 * DEVICE_MEMORY_PROFILE_BYTES['arm64_jp6'])

    def test_out_of_range_utilization_falls_back_to_the_default(self):
        for bad in (Decimal('1.5'), 0.0, -0.2, 'garbage'):
            findings = evaluate_fit(
                {'gpu_memory_utilization': bad}, 2 * GIB, ['arm64_jp6'])
            assert findings[0].budget_bytes == int(
                DEFAULT_GPU_MEMORY_UTILIZATION
                * DEVICE_MEMORY_PROFILE_BYTES['arm64_jp6'])

    def test_unknown_architectures_are_skipped_without_findings(self):
        assert evaluate_fit({}, 2 * GIB, ['x86_64', 'arm64_jp4']) == []
        findings = evaluate_fit(
            {}, 2 * GIB, ['x86_64', 'arm64_jp6', 'unknown', 'arm64_jp7'])
        assert [f.arch for f in findings] == ['arm64_jp6', 'arm64_jp7']


# ---------------------------------------------------------------------------
# The four worked verdicts from design Decision 2
# Validates: Requirements 2.1, 2.2, 2.3
# ---------------------------------------------------------------------------

INCIDENT_WEIGHTS = int(6.5 * GIB)     # the incident's 6.5 GiB estimate
INCIDENT_UTIL = 0.4


class TestWorkedVerdicts:
    def test_verdict_1_incident_one_image_fails_by_0_38_gib(self):
        """Incident replay: util=0.4, 6.5 GiB weights, 1 image on JP6.
        budget = 12.00 GiB, activation = max(2, 0.75×6.5) = 4.88 GiB,
        required = 6.5 + 4.88 + 1 = 12.38 GiB → A fails by 0.38 GiB, B
        passes (0.4 ≤ 0.80). The corrected model reproduces the reality
        the shipped one missed by 4.50 GiB of claimed slack."""
        findings = evaluate_fit(
            {'gpu_memory_utilization': INCIDENT_UTIL, 'max_model_len': 4096},
            INCIDENT_WEIGHTS, ['arm64_jp6'])
        assert len(findings) == 1
        finding = findings[0]

        assert finding.fits is False
        assert finding.failed_conditions == ['budget']
        assert _gib(finding.budget_bytes) == "12.00 GiB"
        assert _gib(finding.required_bytes) == "12.38 GiB"
        assert _gib(finding.activation_bytes) == "4.88 GiB"
        assert _gib(finding.required_bytes - finding.budget_bytes) == \
            "0.38 GiB"
        assert "short by 0.38 GiB" in finding.message
        # B passes: 0.4 is under the 0.80 cap.
        assert INCIDENT_UTIL <= fraction_cap('arm64_jp6')

    def test_verdict_2_incident_two_images_fails_by_5_25_gib(self):
        """Same model, 2 images: activation = 9.75 GiB, required =
        17.25 GiB → A fails by 5.25 GiB. The 1.0.61 regression is visible
        at authoring time (defect 1.4)."""
        findings = evaluate_fit(
            {'gpu_memory_utilization': INCIDENT_UTIL,
             'limit_mm_per_prompt': {'image': 2}},
            INCIDENT_WEIGHTS, ['arm64_jp6'])
        finding = findings[0]

        assert finding.fits is False
        assert finding.images_per_prompt == 2
        assert _gib(finding.activation_bytes) == "9.75 GiB"
        assert _gib(finding.required_bytes) == "17.25 GiB"
        assert "short by 5.25 GiB" in finding.message

    def test_verdict_3_jp7_qwen3_vl_fits_unchanged(self):
        """JP7 qwen3-vl: util=0.5, ~16 GiB weights, 1 image: budget =
        60.00 GiB, required = 16 + 12 + 1 = 29.00 GiB → fits;
        0.5 ≤ 0.933. The JP7 verdict is unchanged, with no warnings."""
        findings = evaluate_fit(
            {'gpu_memory_utilization': 0.5}, 16 * GIB, ['arm64_jp7'])
        finding = findings[0]

        assert finding.fits is True
        assert finding.failed_conditions == []
        assert finding.warnings == []
        assert _gib(finding.budget_bytes) == "60.00 GiB"
        assert _gib(finding.required_bytes) == "29.00 GiB"
        assert 0.5 <= fraction_cap('arm64_jp7')

    def test_verdict_4_sibling_incident_still_fails_under_both_models(self):
        """The sibling spec's original incident (Qwen2.5-7B bf16,
        14.25 GiB, util=0.3): the weights alone exceed the 9.00 GiB
        budget, so it fails under the OLD model (14.25 + 1 > 9) AND the
        NEW one — its remediation stays correct."""
        weights = int(14.25 * GIB)
        findings = evaluate_fit(
            {'gpu_memory_utilization': 0.3}, weights, ['arm64_jp6'])
        finding = findings[0]

        assert finding.fits is False
        assert _gib(finding.budget_bytes) == "9.00 GiB"
        # Fails under the shipped (old) arithmetic too.
        assert weights + MINIMUM_KV_CACHE_BYTES > finding.budget_bytes
        # The message states that no tuning can rescue this configuration.
        assert "weights alone exceed the configured budget" in \
            finding.message


# ---------------------------------------------------------------------------
# Message composition
# Validates: Requirements 2.1, 2.2, 2.3
# ---------------------------------------------------------------------------

class TestMessageComposition:
    def _incident_failing_message(self):
        findings = evaluate_fit(
            {'gpu_memory_utilization': INCIDENT_UTIL, 'max_model_len': 4096},
            INCIDENT_WEIGHTS, ['arm64_jp6'])
        return findings[0].message

    def test_every_term_is_present_with_its_number(self):
        message = self._incident_failing_message()
        assert "estimated weights 6.50 GiB" in message
        assert "activation allowance 4.88 GiB" in message
        assert "ESTIMATE" in message
        assert "KV cache floor 1.00 GiB" in message
        assert "12.38 GiB required" in message
        assert "12.00 GiB budget" in message
        assert "6.00 GiB" in message          # co-tenancy reservation
        assert "0.80" in message              # the Fraction_Cap
        assert "arm64_jp6" in message         # the profile entry used
        assert "max_model_len" in message and "4096" in message

    def test_remediation_ordering_hazard_reduce_then_last_resort(self):
        message = self._incident_failing_message()
        hazard = message.index("Hazard first")
        reduce_demand = message.index("Reduce demand first")
        last_resort = message.index("Last resort")
        assert hazard < reduce_demand < last_resort
        # The cap-bounded suggestion quotes the needed fraction, rounded
        # UP (12.38 / 30 = 0.4125 → at least 0.42), within the 0.80 cap.
        assert "raised to at most 0.80" in message
        assert "at least 0.42" in message

    def test_never_lower_invariant_on_passing_and_failing_messages(self):
        failing = self._incident_failing_message()
        passing = evaluate_fit(
            {'gpu_memory_utilization': 0.5}, 2 * GIB,
            ['arm64_jp6', 'arm64_jp7'])
        assert not _NEVER_LOWER.search(failing), failing
        for finding in passing:
            assert finding.fits is True
            assert not _NEVER_LOWER.search(finding.message), finding.message

    def test_cap_sentence_appears_only_when_raising_is_safe(self):
        # util already above the cap: raising is declared unsafe and the
        # "may be raised to at most" offer must NOT appear.
        findings = evaluate_fit(
            {'gpu_memory_utilization': 0.9}, INCIDENT_WEIGHTS,
            ['arm64_jp6'])
        over_cap = findings[0]
        assert over_cap.fits is False
        assert 'co_tenancy' in over_cap.failed_conditions
        assert "Raising the fraction is unsafe" in over_cap.message
        assert "raised to at most" not in over_cap.message

        # util below the cap with a budget shortfall: the bounded offer
        # appears.
        below_cap = self._incident_failing_message()
        assert "raised to at most 0.80" in below_cap
        assert "Raising the fraction is unsafe" not in below_cap

    def test_passing_message_carries_no_remediation(self):
        findings = evaluate_fit(
            {'gpu_memory_utilization': 0.5}, 2 * GIB, ['arm64_jp6'])
        finding = findings[0]
        assert finding.fits is True
        assert "Fit check passed" in finding.message
        for remediation_marker in ("Last resort", "Reduce demand first",
                                   "Hazard first"):
            assert remediation_marker not in finding.message

    def test_needed_fraction_beyond_the_cap_is_called_out(self):
        # Sibling incident: needed fraction 25.94 / 30 = 0.87 > cap 0.80 —
        # the message must say raising cannot make it fit safely.
        findings = evaluate_fit(
            {'gpu_memory_utilization': 0.3}, int(14.25 * GIB),
            ['arm64_jp6'])
        message = findings[0].message
        assert "cannot make this configuration fit safely" in message
