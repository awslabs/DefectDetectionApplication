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
"""Fix-checking PROPERTY: the portal and device sizing models agree (spec:
jp6-vllm-kv-cache-oom-regression, task 4.7).

# Validates: Requirements 2.1, 2.9

**Property 8: Preservation — Portal and device sizing models agree**
(design "Correctness Properties"). _For any_ point on a grid of
(architecture, weights, utilization, multimodal units), the portal's
``functions.vllm_fit_check`` budget model (File 1, the SINGLE SOURCE OF
TRUTH) and the device's ``vllm_runtime.memory_budget`` model (File 5, the
mirror — its constants block points HERE as the keep-in-sync guard) SHALL
compute the **same required bytes** and the **same budget-sufficiency
verdict**, and every mirrored constant SHALL be equal — so a configuration
accepted at publish time is never refused by the device for a reason the
portal could have predicted. Any drift between the two copies fails this
suite loudly.

Properties in this file (all facets of Property 8):
  P8-A **Every mirrored constant is equal** [2.1, 2.9] — the seven sizing
       constants plus the profile/reservation tables and every reader
       default, each also pinned to its design Decision 2 value (as amended
       by task 14 / H8+H9) so a change to EITHER side — or to both, away
       from the design — fails.
  P8-B **Same required bytes and same budget-sufficiency verdict** [2.1,
       2.9] — over the generated grid, the portal's ``evaluate_fit``
       finding and the device's ``evaluate_device_fit`` verdict (fed a
       reading whose ``MemTotal`` equals the architecture's profile entry)
       agree on required bytes, the non-torch allowance, the activation
       allowance, budget bytes, the KV serving margin (a WARNING
       threshold on both legs, a term in NEITHER `required`), predicted KV
       headroom, images/videos/units per prompt,
       co-tenancy reservation, fraction cap AND on condition A itself
       (budget >= required) — with NO unit-count exception.
  P8-C **Accepted at publish time is never refused by the device** [2.1,
       2.9] — for a device in exactly the state the portal models
       (``MemTotal`` = profile entry, ``MemAvailable`` = total minus the
       co-tenancy reservation), every configuration the portal accepts
       (conditions A AND B) passes the device preflight.
  P8-D **The tolerant readers agree on hostile configurations** [2.1,
       2.9] — malformed / missing / ``Decimal`` / boolean / out-of-range
       ``limit_mm_per_prompt`` (including its ``video`` sub-key) and
       ``gpu_memory_utilization`` values degrade to the SAME effective
       images, videos, units, required bytes and budget on both sides, so
       the two models cannot drift apart through their input parsing
       either.

THE DIVERGENCE RECORDED IN TASK 11's NINTH OUTCOME BLOCK IS NOW **CLOSED**
(2026-08-19, spec task 14 / task 4.7). The device mirror has adopted the
portal's UNITS model — ``videos_per_prompt``, ``video_is_authored``,
``multimodal_units``, ``DEFAULT_VIDEOS_PER_PROMPT = 1``,
``DEFAULT_MULTIMODAL_UNITS = 2`` and ``activation_allowance(weights, units)``,
same names, same semantics, same tolerant-reader behaviour — in the SAME change
that added ``NON_TORCH_MEMORY_BYTES`` to ``required``, recalibrated
``ACTIVATION_WEIGHT_FRACTION`` (0.75 -> 0.375, H8) and REMOVED the KV floor
from ``required`` (``MINIMUM_KV_CACHE_BYTES`` is the thin-margin warning
threshold applied to the predicted remainder, and no KV term is charged — H9).
Both legs moved together because this property pins them together.

So parity is **EXACT again in BOTH authoring shapes**: every mirrored constant
is equal, and the required-bytes arithmetic and the budget verdict agree at
every generated point, whether or not the configuration authors
``limit_mm_per_prompt.video``.

**SUPERSEDED STOPGAP, recorded verbatim before deletion** (it was the
one-directional assertion this file carried between the video widening and the
device build, per task 11's ninth OUTCOME block and design Property 8's
amendment)::

    else:
        # The authored-video dimension the device mirror does not know yet
        # (it ships with the JP6 component build; H8 recalibration, task 14).
        # The portal must be STRICTLY MORE conservative — never less — and its
        # verdict must therefore imply the device's.
        assert portal_units > device_units, (
            "the portal must never count FEWER multimodal units than the "
            "device mirror: portal={} device={} config={}".format(
                portal_units, device_units, config))
        assert finding.required_bytes >= verdict.terms["required_bytes"], (
            "the portal became LESS conservative than the device mirror: "
            "portal required={} device required={} config={}".format(
                finding.required_bytes, verdict.terms["required_bytes"],
                config))
        if portal_condition_a:
            assert verdict.ok, (
                "portal accepted (condition A) but the device refused, in "
                "the very state the portal models: finding={} terms={} "
                "refusal={}".format(finding, verdict.terms,
                                    verdict.refusal_reason))

    # ... and in P8-D:
    assert portal_units >= device_images, (raw_limit, portal_units,
                                           device_images)
    assert finding.required_bytes >= mb.required_bytes(weights_bytes,
                                                       device_images), (
        raw_limit, raw_util, finding)

Nothing is weakened by removing it: exact equality is STRICTLY STRONGER than
"portal >= device", and it now holds on the whole generated grid rather than
only on the configurations that author ``video``. The MEASURED justification
for the units model is unchanged and still binding: vLLM reserves its
worst-case token budget per modality — its own warning on
`ryanorinagxdevkithomelabjp622`: "worst-case total number of multimodal tokens
(32768) ... out of which {'image': 16384, 'video': 16384} are reserved for
multi-modal embeddings" — and at ``gpu_memory_utilization = 0.55``
``{"image": 1, "video": 0}`` profiled a 2.47 GiB activation peak (KV 6.43 GiB,
29.41x, READY) while ``{"image": 1}`` alone profiled 4.93 GiB (KV 0.20 GiB,
0.89x, FAILED).

STILL [HARDWARE]: the device leg of this change ships ONLY inside an
`aws.edgeml.dda.LocalServer.arm64JP6` component build, which has NOT run for
it. This file proves the two models agree; it cannot prove either matches the
device (tasks 11/14).

NOTE on scope (design Decision 4): the device preflight enforces the
portal's condition A (budget sufficiency) against the device's REAL
``MemTotal``, plus its own starvation arm against ``MemAvailable``. The
portal's condition B (the co-tenancy Fraction_Cap) is a PUBLISH-TIME-ONLY
gate — the device does not refuse on it — so parity is asserted for what
is mirrored: the required-bytes arithmetic, condition A, and the constants
(including ``fraction_cap`` as a computed value). P8-C proves the safe
direction of that asymmetry: portal-accepted implies device-accepted.

HONESTY GUARD (binding, design "Honesty Guard"). This file proves PURE
MATH PARITY between two stdlib-only modules — nothing else. No vLLM
engine, no GPU allocation, no Jetson unified-memory claim; the "device" is
a constructed ``MemoryReading``. That the shared model reflects hardware
reality is [HARDWARE] territory (tasks 11/14, H8 calibration).

Hypothesis conventions for the device suites (``--noconftest``, so no
profile is registered): ``@settings(deadline=None)`` with **no hardcoded
``max_examples``**, matching the sibling device suites.

Run (host-side, from the repo root, portal backend on the path):
    PYTHONPATH=src/backend:test/backend-test:edge-cv-portal/backend \
      python3 -m pytest \
      test/backend-test/jp6_vllm_kv_cache_oom/test_property_portal_device_parity.py \
      -q -p no:cacheprovider --noconftest

_Requirements: 2.1, 2.9_
"""
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

import functions.vllm_fit_check as fit
import vllm_runtime.memory_budget as mb

GIB = 1024 ** 3
MIB = 1024 ** 2


# ---------------------------------------------------------------------------
# P8-A — every mirrored constant is equal  [2.1, 2.9]
# ---------------------------------------------------------------------------

def test_every_mirrored_constant_is_equal():
    """P8-A. The device module duplicates the portal's constants (it cannot
    import a Lambda function module); this is the pin its constants block
    points at. Each value is ALSO pinned to design Decision 2's number, so
    editing either copy — or both, away from the design — fails loudly here
    rather than silently skewing a verdict.

    # Validates: Requirements 2.1, 2.9
    """
    assert fit.GIB == mb.GIB == GIB

    # The per-architecture tables, key set and values.
    assert fit.DEVICE_MEMORY_PROFILE_BYTES == mb.DEVICE_MEMORY_PROFILE_BYTES
    assert fit.DEVICE_MEMORY_PROFILE_BYTES == {
        'arm64_jp6': 30 * GIB,
        'arm64_jp5': 30 * GIB,
        'arm64_jp7': 120 * GIB,
    }
    assert fit.CO_TENANCY_RESERVATION_BYTES == mb.CO_TENANCY_RESERVATION_BYTES
    assert fit.CO_TENANCY_RESERVATION_BYTES == {
        'arm64_jp6': 6 * GIB,
        'arm64_jp5': 6 * GIB,
        'arm64_jp7': 8 * GIB,
    }

    # The scalar sizing constants (design Decision 2's table, as amended by
    # task 14 / H8 + H9).
    #
    # SUPERSEDED PINS, recorded verbatim before the change:
    #     assert (fit.ACTIVATION_WEIGHT_FRACTION
    #             == mb.ACTIVATION_WEIGHT_FRACTION == 0.75)
    # 0.75 was calibrated to one point that is now known to have been a
    # TWO-unit (video-unbounded) measurement; the measured per-unit pair
    # (2.47 GiB against 6.59 GiB of weights) puts it at 0.375. The pin is
    # repointed, not removed — a silent drift on either side still fails.
    assert fit.ACTIVATION_FLOOR_BYTES == mb.ACTIVATION_FLOOR_BYTES == 2 * GIB
    assert (fit.ACTIVATION_WEIGHT_FRACTION
            == mb.ACTIVATION_WEIGHT_FRACTION == 0.375)
    assert (fit.MULTIMODAL_IMAGE_INCREMENT
            == mb.MULTIMODAL_IMAGE_INCREMENT == 1.0)

    # The non-torch allowance: a term the shipped `required` omitted entirely
    # (task 11's ninth OUTCOME block, defect (a)). ESTIMATE, median of seven
    # measured readings (-0.05 .. 8.29 GiB, median 2.18) rounded down.
    #
    # SUPERSEDED PIN, recorded verbatim before the change (the intermediate
    # version of this change named the constant ...ALLOWANCE...):
    #     assert (fit.NON_TORCH_ALLOWANCE_BYTES
    #             == mb.NON_TORCH_ALLOWANCE_BYTES == 2 * GIB)
    assert (fit.NON_TORCH_MEMORY_BYTES
            == mb.NON_TORCH_MEMORY_BYTES == 2 * GIB)

    # The KV floor is a WARNING THRESHOLD ON BOTH LEGS AND A TERM IN NEITHER
    # `required` (task 14 / H9). Value and name unchanged at 1 GiB.
    #
    # SUPERSEDED PINS, recorded verbatim before the change (an intermediate
    # version of this change kept a HARD 0.25 GiB viability floor in
    # `required`; the operator's decision is that NO KV term is charged):
    #     assert (fit.KV_VIABILITY_FLOOR_BYTES
    #             == mb.KV_VIABILITY_FLOOR_BYTES == int(0.25 * GIB))
    #     assert 0 < fit.KV_VIABILITY_FLOOR_BYTES < fit.MINIMUM_KV_CACHE_BYTES
    #     assert fit.KV_VIABILITY_FLOOR_BYTES < int(0.65 * GIB)
    # Nothing is weakened: the replacement pins the WHOLE composition of
    # `required` on both legs, which is strictly stronger than pinning one of
    # its terms.
    assert fit.MINIMUM_KV_CACHE_BYTES == mb.MINIMUM_KV_CACHE_BYTES == 1 * GIB
    assert not hasattr(fit, "KV_VIABILITY_FLOOR_BYTES")
    assert not hasattr(mb, "KV_VIABILITY_FLOOR_BYTES")
    for weights_bytes in (0, int(6.45 * GIB), 16 * GIB):
        for units in (1, 2, 5):
            composed = (weights_bytes + fit.NON_TORCH_MEMORY_BYTES
                        + fit.activation_allowance(weights_bytes, units))
            assert (fit.required_bytes(weights_bytes, units)
                    == mb.required_bytes(weights_bytes, units)
                    == composed), (weights_bytes, units)
            # No KV term, hard or soft, is charged by either leg.
            assert (fit.required_bytes(weights_bytes, units)
                    < composed + fit.MINIMUM_KV_CACHE_BYTES)
    # The 1 GiB floor's role: the configuration that demonstrably SERVED
    # (0.65 GiB of KV at 2.95x for 4096 tokens) is ADMITTED and merely warned
    # about, on both legs.
    assert int(0.65 * GIB) < fit.MINIMUM_KV_CACHE_BYTES

    # The reader defaults both modules resolve omitted settings to. An
    # unauthored `limit_mm_per_prompt.video` costs a full extra unit on BOTH
    # sides now (the divergence of task 11's ninth block, CLOSED).
    assert (fit.DEFAULT_IMAGES_PER_PROMPT
            == mb.DEFAULT_IMAGES_PER_PROMPT == 1)
    assert (fit.DEFAULT_VIDEOS_PER_PROMPT
            == mb.DEFAULT_VIDEOS_PER_PROMPT == 1)
    assert (fit.DEFAULT_MULTIMODAL_UNITS
            == mb.DEFAULT_MULTIMODAL_UNITS == 2)
    assert (tuple(fit.MULTIMODAL_MODALITY_KEYS)
            == tuple(mb.MULTIMODAL_MODALITY_KEYS) == ('image', 'video'))
    assert (fit.DEFAULT_GPU_MEMORY_UTILIZATION
            == mb.DEFAULT_GPU_MEMORY_UTILIZATION == 0.5)


def test_fraction_cap_agrees_for_every_profiled_architecture():
    """P8-A (computed constant). The Fraction_Cap the two modules derive
    from the mirrored tables is identical for every profiled architecture,
    whether the device computes it from the architecture or from a total
    equal to the profile entry (JP6: (30-6)/30 = 0.80).

    # Validates: Requirements 2.1, 2.9
    """
    for arch, profile_bytes in fit.DEVICE_MEMORY_PROFILE_BYTES.items():
        portal_cap = fit.fraction_cap(arch)
        assert portal_cap == mb.fraction_cap(arch), arch
        assert portal_cap == mb.fraction_cap(arch, profile_bytes), arch
        # The device's inferred-total route (no arch, a total equal to the
        # profile entry) lands on the same cap for every profiled arch.
        assert portal_cap == mb.fraction_cap(total_bytes=profile_bytes), arch
    # The JP6 cap is the design's worked number.
    assert fit.fraction_cap('arm64_jp6') == (30 - 6) / 30.0


# ---------------------------------------------------------------------------
# The grid: (architecture, weights, utilization, images)
# ---------------------------------------------------------------------------

_architectures = st.sampled_from(sorted(mb.DEVICE_MEMORY_PROFILE_BYTES))

#: Whole-MiB weights from zero (weightless edge) to 128 GiB (dwarfs every
#: profile), so BOTH verdicts are exercised on every architecture.
_weights = st.integers(min_value=0, max_value=128 * 1024).map(
    lambda mib: mib * MIB)

_utilizations = st.floats(min_value=0.01, max_value=1.0,
                          allow_nan=False, allow_infinity=False
                          ).map(lambda x: round(x, 3))


@st.composite
def grid_points(draw):
    """One grid point: an architecture, weights, and an engine
    configuration whose utilization / multimodal limit / max_model_len are
    each independently present or omitted (omission exercises the mirrored
    defaults).

    The multimodal limit is drawn over BOTH authoring shapes: with and
    without the ``video`` sub-key (the 2026-08-19 widening). Authoring
    ``video`` is what makes the portal's unit count equal the device
    mirror's image count, so both the exact-parity arm and the
    safe-direction arm of P8-B are exercised."""
    arch = draw(_architectures)
    weights_bytes = draw(_weights)
    config = {}
    if draw(st.booleans()):
        config["gpu_memory_utilization"] = draw(_utilizations)
    if draw(st.booleans()):
        limit = {"image": draw(st.integers(min_value=1, max_value=8))}
        if draw(st.booleans()):
            limit["video"] = draw(st.integers(min_value=0, max_value=2))
        config["limit_mm_per_prompt"] = limit
    if draw(st.booleans()):
        config["max_model_len"] = draw(st.integers(min_value=256,
                                                   max_value=32768))
    return arch, weights_bytes, config


def _unit_counts(config):
    """``(portal_units, device_units)`` for ``config``. BOTH now count
    images + videos, an unauthored ``video`` costing vLLM's own default of 1
    (the device mirror adopted the portal's units model — task 14 / H8+H9), so
    these must be EQUAL at every point."""
    return fit.multimodal_units(config), mb.multimodal_units(config)


def _portal_finding(config, weights_bytes, arch):
    findings = fit.evaluate_fit(config, weights_bytes, [arch])
    assert len(findings) == 1, (
        "expected exactly one finding for profiled arch {}: {}".format(
            arch, findings))
    return findings[0]


# ---------------------------------------------------------------------------
# P8-B — same required bytes, same budget-sufficiency verdict  [2.1, 2.9]
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(point=grid_points())
def test_property_same_required_bytes_and_budget_verdict(point):
    """P8-B. For any grid point, the portal finding and the device verdict
    (over a reading whose MemTotal equals the architecture's profile entry,
    with nothing else resident) agree on EVERY shared term — required
    bytes, activation allowance, budget bytes, KV floor, effective images,
    co-tenancy reservation, fraction cap — and on the budget-sufficiency
    verdict itself: the device preflight's budget arm passes exactly when
    the portal's condition A holds.

    # Validates: Requirements 2.1, 2.9
    """
    arch, weights_bytes, config = point
    finding = _portal_finding(config, weights_bytes, arch)
    portal_units, device_units = _unit_counts(config)

    total = mb.DEVICE_MEMORY_PROFILE_BYTES[arch]
    reading = mb.MemoryReading(total_bytes=total, available_bytes=total)
    verdict = mb.evaluate_device_fit(config, reading,
                                     weights_bytes=weights_bytes, arch=arch)

    # The UNIT COUNT itself agrees now — this is the assertion that was
    # relaxed to `portal_units >= device_units` between the video widening and
    # the device mirror adopting the units model (task 11's ninth OUTCOME
    # block); it is back to equality.
    assert portal_units == device_units, (
        "multimodal-unit reader drift: portal={} device={} config={}".format(
            portal_units, device_units, config))

    # The mirrored FORMULA itself never drifts.
    assert verdict.unverified is False
    assert mb.required_bytes(weights_bytes, portal_units) \
        == finding.required_bytes
    assert mb.activation_allowance(weights_bytes, portal_units) \
        == finding.activation_bytes
    assert fit.required_bytes(weights_bytes, portal_units) \
        == mb.required_bytes(weights_bytes, portal_units)
    assert fit.kv_headroom_bytes(finding.budget_bytes, weights_bytes,
                                 portal_units) \
        == mb.kv_headroom_bytes(finding.budget_bytes, weights_bytes,
                                portal_units)

    # EXACT parity on every shared term, unconditionally.
    assert verdict.terms["budget_bytes"] == finding.budget_bytes, (
        verdict.terms, finding)
    assert verdict.terms["required_bytes"] == finding.required_bytes, (
        verdict.terms, finding)
    assert verdict.terms["activation_bytes"] == finding.activation_bytes, (
        verdict.terms, finding)
    assert (verdict.terms["non_torch_bytes"] == finding.non_torch_bytes
            == mb.NON_TORCH_MEMORY_BYTES)
    assert "kv_viability_floor_bytes" not in verdict.terms
    # The serving margin is the WARNING threshold on both sides and is NOT a
    # term in either `required_bytes`.
    assert (verdict.terms["kv_floor_bytes"] == finding.kv_floor_bytes
            == mb.MINIMUM_KV_CACHE_BYTES)
    assert verdict.terms["kv_headroom_bytes"] == finding.kv_headroom_bytes, (
        verdict.terms, finding)
    assert verdict.terms["images_per_prompt"] == finding.images_per_prompt, (
        verdict.terms, finding)
    assert verdict.terms["videos_per_prompt"] == finding.videos_per_prompt, (
        verdict.terms, finding)
    assert verdict.terms["multimodal_units"] == finding.multimodal_units, (
        verdict.terms, finding)
    assert verdict.terms["co_tenancy_bytes"] == finding.co_tenancy_bytes, (
        verdict.terms, finding)
    assert verdict.terms["fraction_cap"] == finding.fraction_cap, (
        verdict.terms, finding)

    portal_condition_a = 'budget' not in finding.failed_conditions
    assert portal_condition_a == (
        finding.budget_bytes >= finding.required_bytes)  # self-consistency

    # The verdict itself. With MemAvailable == MemTotal the preflight's
    # starvation arm cannot refuse anything its budget arm (util x MemTotal,
    # identical to the portal's budget here) does not, so verdict.ok IS
    # condition A.
    assert verdict.ok == portal_condition_a, (
        "budget-sufficiency drift: portal condition A={} "
        "(budget={} required={}) but device verdict.ok={} "
        "terms={}".format(portal_condition_a, finding.budget_bytes,
                          finding.required_bytes, verdict.ok,
                          verdict.terms))

    # The thin-margin WARNING fires on the same side of the same threshold on
    # both legs — a passing verdict whose predicted KV headroom is under the
    # 1 GiB serving margin (task 14 / H9: a caution, never a refusal).
    if verdict.ok:
        device_thin = "thin_margin" in (verdict.terms["warnings"] or [])
        assert device_thin == (
            finding.kv_headroom_bytes < mb.MINIMUM_KV_CACHE_BYTES), (
            verdict.terms, finding)
        if finding.fits:
            assert device_thin == ('thin_margin' in finding.warnings), (
                verdict.terms, finding)


# ---------------------------------------------------------------------------
# P8-C — accepted at publish time is never refused by the device  [2.1, 2.9]
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(point=grid_points())
def test_property_publish_accepted_is_never_device_refused(point):
    """P8-C. Put the device in exactly the state the portal models —
    MemTotal equal to the profile entry and the co-tenancy reservation
    (and nothing else) resident, so MemAvailable = total − reservation.
    Every configuration the portal accepts (conditions A AND B) then
    passes the device preflight: a configuration accepted at publish time
    is never refused by the device for a reason the portal could have
    predicted.

    # Validates: Requirements 2.1, 2.9
    """
    arch, weights_bytes, config = point
    finding = _portal_finding(config, weights_bytes, arch)

    total = mb.DEVICE_MEMORY_PROFILE_BYTES[arch]
    available = total - mb.CO_TENANCY_RESERVATION_BYTES[arch]
    reading = mb.MemoryReading(total_bytes=total, available_bytes=available)
    verdict = mb.evaluate_device_fit(config, reading,
                                     weights_bytes=weights_bytes, arch=arch)

    if finding.fits:
        assert verdict.ok, (
            "the portal accepted this configuration but the device refused "
            "it in the very state the portal models: finding={} "
            "verdict.terms={} refusal={}".format(
                finding, verdict.terms, verdict.refusal_reason))


# ---------------------------------------------------------------------------
# P8-D — the tolerant readers agree on hostile configurations  [2.1, 2.9]
# ---------------------------------------------------------------------------

_hostile_image_values = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-5, max_value=12),
    st.floats(min_value=-2, max_value=12,
              allow_nan=False, allow_infinity=False),
    st.text(max_size=4),
    st.decimals(min_value=Decimal("-2"), max_value=Decimal("9"),
                allow_nan=False, allow_infinity=False),
)

_hostile_limits = st.one_of(
    st.none(),
    _hostile_image_values,
    st.fixed_dictionaries({"image": _hostile_image_values}),
    # The `video` sub-key on the hostile side too, now that BOTH modules read
    # it (the divergence of task 11's ninth block is closed).
    st.fixed_dictionaries({"video": _hostile_image_values}),
    st.fixed_dictionaries({"image": _hostile_image_values,
                           "video": _hostile_image_values}),
    st.lists(st.integers(), max_size=2),
)

_hostile_utilizations = st.one_of(
    st.none(),
    st.booleans(),
    st.floats(min_value=-2.0, max_value=2.0,
              allow_nan=False, allow_infinity=False),
    st.text(max_size=4),
    st.decimals(min_value=Decimal("-1"), max_value=Decimal("2"),
                allow_nan=False, allow_infinity=False),
)


@settings(deadline=None)
@given(arch=_architectures, weights_bytes=_weights,
       raw_limit=_hostile_limits, raw_util=_hostile_utilizations)
def test_property_tolerant_readers_agree_on_hostile_configs(
        arch, weights_bytes, raw_limit, raw_util):
    """P8-D. Missing, malformed, boolean, ``Decimal``, negative or
    out-of-range ``limit_mm_per_prompt`` / ``gpu_memory_utilization``
    values degrade identically on both sides: the portal finding's
    effective images, required bytes and budget bytes equal what the
    device's own readers and formula produce for the same configuration —
    so input parsing cannot become a second source of model drift.

    # Validates: Requirements 2.1, 2.9
    """
    config = {"limit_mm_per_prompt": raw_limit,
              "gpu_memory_utilization": raw_util}
    finding = _portal_finding(config, weights_bytes, arch)

    device_images = mb.images_per_prompt(config)
    device_videos = mb.videos_per_prompt(config)
    device_units = mb.multimodal_units(config)
    device_util = mb.gpu_memory_utilization(config)
    portal_units = fit.multimodal_units(config)

    assert finding.images_per_prompt == device_images, (
        "images-per-prompt reader drift on {!r}: portal={} device={}".format(
            raw_limit, finding.images_per_prompt, device_images))
    assert finding.videos_per_prompt == device_videos, (
        "videos-per-prompt reader drift on {!r}: portal={} device={}".format(
            raw_limit, finding.videos_per_prompt, device_videos))
    assert finding.multimodal_units == device_units == portal_units, (
        "multimodal-unit reader drift on {!r}: portal={} device={}".format(
            raw_limit, portal_units, device_units))
    assert mb.video_is_authored(config) == fit.video_is_authored(config), (
        raw_limit,)
    # The shared formula reproduces the finding exactly — so hostile input
    # cannot make the two models diverge through their parsing either.
    assert finding.required_bytes == mb.required_bytes(weights_bytes,
                                                       device_units), (
        raw_limit, raw_util, finding)
    assert finding.budget_bytes == int(
        device_util * mb.DEVICE_MEMORY_PROFILE_BYTES[arch]), (
        "utilization reader drift on {!r}: portal budget={} device "
        "util={}".format(raw_util, finding.budget_bytes, device_util))
