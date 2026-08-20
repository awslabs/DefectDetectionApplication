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
        # REPOINTED 2026-08-19 (task 14 / H8). SUPERSEDED case, recorded
        # verbatim:
        #     weights = 4 * GIB  # 0.75 × 4 = 3 GiB > the 2 GiB floor
        # At the recalibrated 0.375 the floor binds until ~5.33 GiB of
        # weights, so 4 GiB no longer exercises the fraction term at all.
        weights = 8 * GIB  # 0.375 × 8 = 3 GiB > the 2 GiB floor
        assert activation_allowance(weights) == int(
            ACTIVATION_WEIGHT_FRACTION * weights)

    def test_the_floor_binds_below_about_5_33_gib_of_weights(self):
        """Stated consequence of the recalibration (task 14 / H8): with
        ``ACTIVATION_WEIGHT_FRACTION = 0.375`` the 2 GiB ACTIVATION_FLOOR
        binds for every model under 2 / 0.375 ≈ 5.33 GiB of weights."""
        assert activation_allowance(5 * GIB) == ACTIVATION_FLOOR_BYTES
        assert activation_allowance(int(5.33 * GIB)) == ACTIVATION_FLOOR_BYTES
        assert activation_allowance(6 * GIB) > ACTIVATION_FLOOR_BYTES

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
    def test_verdict_1_incident_one_unit_now_fits_after_h8_and_h9(self):
        """Incident replay, REPOINTED by task 14 / H8 + H9: util=0.4,
        6.5 GiB weights, ONE multimodal unit on JP6
        (`{'image': 1, 'video': 0}` — the authored default).
        budget = 12.00 GiB, activation = max(2, 0.375×6.5) = 2.44 GiB,
        required = 6.5 + 2.00 (non-torch) + 2.44 + 0.25 (KV viability) =
        11.19 GiB → **A now PASSES**, B passes (0.4 ≤ 0.80).

        SUPERSEDED test, recorded verbatim — it asserted the OPPOSITE verdict::

            def test_verdict_1_incident_one_image_fails_by_0_38_gib(self):
                \"\"\"Incident replay: util=0.4, 6.5 GiB weights, ONE
                multimodal unit on JP6 (`{'image': 1, 'video': 0}` — the
                authored default). budget = 12.00 GiB, activation =
                max(2, 0.75×6.5) = 4.88 GiB, required = 6.5 + 4.88 + 1 =
                12.38 GiB → A fails by 0.38 GiB, B passes (0.4 ≤ 0.80). The
                corrected model reproduces the reality the shipped one missed
                by 4.50 GiB of claimed slack.\"\"\"
                findings = evaluate_fit(
                    {'gpu_memory_utilization': INCIDENT_UTIL,
                     'max_model_len': 4096,
                     'limit_mm_per_prompt': {'image': 1, 'video': 0}},
                    INCIDENT_WEIGHTS, ['arm64_jp6'])
                assert len(findings) == 1
                finding = findings[0]
                assert finding.fits is False
                assert finding.failed_conditions == ['budget']
                assert _gib(finding.budget_bytes) == "12.00 GiB"
                assert _gib(finding.required_bytes) == "12.38 GiB"
                assert _gib(finding.activation_bytes) == "4.88 GiB"
                assert _gib(finding.required_bytes
                            - finding.budget_bytes) == "0.38 GiB"
                assert "short by 0.38 GiB" in finding.message
                assert INCIDENT_UTIL <= fraction_cap('arm64_jp6')

        WHY the verdict flipped, stated plainly: the model is more ACCURATE,
        not more permissive by intent. The superseded numbers were wrong in
        two directions at once — the activation term was ~2x too high
        (0.75 against a MEASURED 0.375 per unit) and the 1 GiB KV floor was
        charged as HARD, which refused the configuration LocalServer 1.0.59
        demonstrably SERVED (0.65 GiB of KV at 2.95x concurrency for 4096
        tokens). Meanwhile `required` gained the non-torch term it always
        omitted. This configuration is exactly the one H9 exists to admit."""
        findings = evaluate_fit(
            {'gpu_memory_utilization': INCIDENT_UTIL, 'max_model_len': 4096,
             'limit_mm_per_prompt': {'image': 1, 'video': 0}},
            INCIDENT_WEIGHTS, ['arm64_jp6'])
        assert len(findings) == 1
        finding = findings[0]

        assert finding.fits is True, finding.message
        assert finding.failed_conditions == []
        assert _gib(finding.budget_bytes) == "12.00 GiB"
        # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
        # jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED
        # assertions, recorded VERBATIM:
        #     assert _gib(finding.required_bytes) == "11.19 GiB"
        #     assert _gib(finding.kv_viability_floor_bytes) == "0.25 GiB"
        # Reason: H9's final decision charges NO KV term in `required`, so the
        # requirement is 6.5 + 2.00 + 2.44 = 10.94 GiB and there is no
        # viability-floor term to report. The verdict asserted (ADMITTED, no
        # warning) is unchanged.
        assert _gib(finding.required_bytes) == "10.94 GiB"
        assert _gib(finding.activation_bytes) == "2.44 GiB"
        assert _gib(finding.non_torch_bytes) == "2.00 GiB"
        assert not hasattr(finding, 'kv_viability_floor_bytes')
        # The 1 GiB serving margin is the WARNING threshold, and the predicted
        # KV headroom clears it here, so not even a warning fires.
        assert _gib(finding.kv_floor_bytes) == "1.00 GiB"
        assert finding.kv_headroom_bytes >= MINIMUM_KV_CACHE_BYTES
        assert finding.warnings == []
        # B passes: 0.4 is under the 0.80 cap.
        assert INCIDENT_UTIL <= fraction_cap('arm64_jp6')

    def test_verdict_2_incident_two_units_fails_by_1_38_gib(self):
        """Same model, 2 images with video bounded (2 units): activation =
        4.88 GiB, required = 6.5 + 2.00 + 4.88 = 13.38 GiB → A fails
        by 1.38 GiB. The 1.0.61 regression is still visible at authoring time
        (defect 1.4).

        CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
        jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED name,
        docstring line and assertions, recorded VERBATIM::

            def test_verdict_2_incident_two_units_fails_by_1_62_gib(self):
            4.88 GiB, required = 6.5 + 2.00 + 4.88 + 0.25 = 13.62 GiB → A fails
            by 1.62 GiB.

            assert _gib(finding.required_bytes) == "13.62 GiB"
            assert "short by 1.62 GiB" in finding.message

        Reason: H9's final decision charges NO KV term, so the requirement and
        the shortfall each drop by the intermediate 0.25 GiB. The verdict —
        A FAILS — is unchanged, which is what this test exists to pin.

        REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED assertions,
        recorded verbatim::

            assert _gib(finding.activation_bytes) == "9.75 GiB"
            assert _gib(finding.required_bytes) == "17.25 GiB"
            assert "short by 5.25 GiB" in finding.message
        """
        findings = evaluate_fit(
            {'gpu_memory_utilization': INCIDENT_UTIL,
             'limit_mm_per_prompt': {'image': 2, 'video': 0}},
            INCIDENT_WEIGHTS, ['arm64_jp6'])
        finding = findings[0]

        assert finding.fits is False
        assert finding.images_per_prompt == 2
        assert finding.multimodal_units == 2
        assert _gib(finding.activation_bytes) == "4.88 GiB"
        assert _gib(finding.required_bytes) == "13.38 GiB"
        assert "short by 1.38 GiB" in finding.message

    def test_unauthored_video_is_a_second_unit(self):
        """MEASURED 2026-08-19 on `ryanorinagxdevkithomelabjp622`
        (LocalServer.arm64JP6 1.0.62), same model, same
        `gpu_memory_utilization = 0.55`: `{'image': 1, 'video': 0}` profiled
        an activation peak of 2.47 GiB (KV 6.43 GiB, 29.41x, READY) while
        `{'image': 1}` alone profiled 4.93 GiB (KV 0.20 GiB, 0.89x, FAILED)
        — vLLM reserves half of its 32768-token worst case for video
        (`{'image': 16384, 'video': 16384}`).

        So the same authored image count with video LEFT UNBOUNDED must be
        sized as two units: activation 4.88 GiB, required 13.38 GiB (superseded
        2026-08-19 second pass, task 14 / H9: "13.62 GiB", which charged the
        intermediate 0.25 GiB KV viability floor), the
        same numbers as an explicitly two-unit configuration.

        REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED assertions,
        recorded verbatim::

            assert _gib(unbounded.activation_bytes) == "9.75 GiB"
            assert _gib(unbounded.required_bytes) == "17.25 GiB"
        """
        unbounded = evaluate_fit(
            {'gpu_memory_utilization': INCIDENT_UTIL,
             'limit_mm_per_prompt': {'image': 1}},
            INCIDENT_WEIGHTS, ['arm64_jp6'])[0]
        bounded = evaluate_fit(
            {'gpu_memory_utilization': INCIDENT_UTIL,
             'limit_mm_per_prompt': {'image': 1, 'video': 0}},
            INCIDENT_WEIGHTS, ['arm64_jp6'])[0]

        assert unbounded.images_per_prompt == bounded.images_per_prompt == 1
        assert unbounded.videos_per_prompt == 1   # vLLM's own default
        assert bounded.videos_per_prompt == 0     # authored bound
        assert unbounded.multimodal_units == 2
        assert bounded.multimodal_units == 1
        assert _gib(unbounded.activation_bytes) == "4.88 GiB"
        assert _gib(unbounded.required_bytes) == "13.38 GiB"
        assert unbounded.activation_bytes == 2 * bounded.activation_bytes
        # And the message says what the omission costs and how to fix it.
        assert 'limit_mm_per_prompt.video' in unbounded.message
        assert '"video": 0' in unbounded.message

    def test_verdict_3_jp7_qwen3_vl_fits_unchanged(self):
        """JP7 qwen3-vl: util=0.5, ~16 GiB weights, one unit: budget =
        60.00 GiB, required = 16 + 2.00 (non-torch) + 6.00 (activation) =
        24.00 GiB → fits; 0.5 ≤ 0.933. The JP7 verdict is unchanged, with no
        warnings.

        CONSCIOUS REPOINT 2026-08-19, SECOND PASS (task 14 / H9). SUPERSEDED
        docstring line and assertion, recorded VERBATIM::

            0.25 (KV viability) = 24.25 GiB → fits; 0.5 ≤ 0.933.
            assert _gib(finding.required_bytes) == "24.25 GiB"

        REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED assertion, recorded
        verbatim:  ``assert _gib(finding.required_bytes) == "29.00 GiB"``.
        The requirement moved DOWN, so preservation 3.4 cannot be at risk."""
        findings = evaluate_fit(
            {'gpu_memory_utilization': 0.5,
             'limit_mm_per_prompt': {'image': 1, 'video': 0}},
            16 * GIB, ['arm64_jp7'])
        finding = findings[0]

        assert finding.fits is True
        assert finding.failed_conditions == []
        assert finding.warnings == []
        assert _gib(finding.budget_bytes) == "60.00 GiB"
        assert _gib(finding.required_bytes) == "24.00 GiB"
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
        """A FAILING incident-shaped verdict, for the message contract.

        REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED helper, recorded
        verbatim::

            def _incident_failing_message(self):
                findings = evaluate_fit(
                    {'gpu_memory_utilization': INCIDENT_UTIL,
                     'max_model_len': 4096,
                     'limit_mm_per_prompt': {'image': 1, 'video': 0}},
                    INCIDENT_WEIGHTS, ['arm64_jp6'])
                return findings[0].message

        The one-unit incident configuration now FITS (that is H9's point — see
        `test_verdict_1_incident_one_unit_now_fits_after_h8_and_h9`), so the
        failing shape used here is the same model with the video modality left
        UNBOUNDED: two units, required 13.38 GiB against a 12.00 GiB budget
        (superseded 2026-08-19 second pass, task 14 / H9: "13.62 GiB", which
        charged the intermediate 0.25 GiB KV viability floor).
        Nothing is weakened — every message assertion below still runs against
        a genuinely failing verdict."""
        findings = evaluate_fit(
            {'gpu_memory_utilization': INCIDENT_UTIL, 'max_model_len': 4096,
             'limit_mm_per_prompt': {'image': 1}},
            INCIDENT_WEIGHTS, ['arm64_jp6'])
        assert findings[0].fits is False, findings[0].message
        return findings[0].message

    def test_every_term_is_present_with_its_number(self):
        message = self._incident_failing_message()
        assert "estimated weights 6.50 GiB" in message
        assert "non-torch allowance 2.00 GiB (an ESTIMATE" in message
        assert "activation allowance 4.88 GiB (an ESTIMATE" in message
        assert "ESTIMATE" in message
        # REPOINTED (task 14 / H9). SUPERSEDED assertions, recorded verbatim:
        #     assert "KV cache floor 1.00 GiB" in message
        #     assert "12.38 GiB required" in message
        # The term `required` charges is the KV VIABILITY floor; the 1 GiB
        # serving margin is quoted by the thin-margin warning instead.
        #
        # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
        # jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED
        # assertions, recorded VERBATIM:
        #     assert "KV cache viability floor 0.25 GiB" in message
        #     assert "13.62 GiB required" in message
        # Reason: H9 charges no KV term, so there is no viability floor in the
        # message. What replaces it is strictly more: the PREDICTED KV
        # REMAINDER this configuration leaves, stated against the 1 GiB
        # serving-margin floor — the H9 surface itself, in one verbatim
        # sentence (12.00 - 13.38 = -1.38 GiB).
        assert ("leaving a predicted KV cache remainder of -1.38 GiB against "
                "the 1.00 GiB serving-margin floor") in message
        assert "viability floor" not in message
        assert "13.38 GiB required" in message
        assert "12.00 GiB budget" in message
        assert "6.00 GiB" in message          # co-tenancy reservation
        assert "0.80" in message              # the Fraction_Cap
        assert "arm64_jp6" in message         # the profile entry used
        assert "max_model_len" in message and "4096" in message
        # The multimodal units the allowance assumed are named, per modality.
        assert "2 multimodal unit(s) per prompt" in message
        assert "1 image(s) + 1 video(s)" in message

    def test_remediation_ordering_hazard_reduce_then_last_resort(self):
        message = self._incident_failing_message()
        hazard = message.index("Hazard first")
        reduce_demand = message.index("Reduce demand first")
        last_resort = message.index("Last resort")
        assert hazard < reduce_demand < last_resort
        # The cap-bounded suggestion quotes the needed fraction, rounded
        # UP (13.38 / 30 = 0.4458 → at least 0.45), within the 0.80 cap.
        # SUPERSEDED (task 14 / H8+H9), recorded verbatim:
        #     assert "at least 0.42" in message  # 12.38 / 30 = 0.4125
        # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (task 14 / H9).
        # SUPERSEDED comment and assertion, recorded VERBATIM:
        #     # UP (13.62 / 30 = 0.4542 → at least 0.46), within the 0.80 cap.
        #     assert "at least 0.46" in message
        assert "raised to at most 0.80" in message
        assert "at least 0.45" in message

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
