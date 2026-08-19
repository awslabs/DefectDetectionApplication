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
(architecture, weights, utilization, images), the portal's
``functions.vllm_fit_check`` budget model (File 1, the SINGLE SOURCE OF
TRUTH) and the device's ``vllm_runtime.memory_budget`` model (File 5, the
mirror — its constants block points HERE as the keep-in-sync guard) SHALL
compute the **same required bytes** and the **same budget-sufficiency
verdict**, and every mirrored constant SHALL be equal — so a configuration
accepted at publish time is never refused by the device for a reason the
portal could have predicted. Any drift between the two copies fails this
suite loudly.

Properties in this file (all facets of Property 8):
  P8-A **Every mirrored constant is equal** [2.1, 2.9] — the five sizing
       constants plus the profile/reservation tables and both reader
       defaults, each also pinned to its design Decision 2 value so a
       change to EITHER side (or to both, away from the design) fails.
  P8-B **Same required bytes and same budget-sufficiency verdict** [2.1,
       2.9] — over the generated grid, the portal's ``evaluate_fit``
       finding and the device's ``evaluate_device_fit`` verdict (fed a
       reading whose ``MemTotal`` equals the architecture's profile entry)
       agree on required bytes, activation allowance, budget bytes, KV
       floor, images per prompt, co-tenancy reservation, fraction cap AND
       on condition A itself (budget >= required).
  P8-C **Accepted at publish time is never refused by the device** [2.1,
       2.9] — for a device in exactly the state the portal models
       (``MemTotal`` = profile entry, ``MemAvailable`` = total minus the
       co-tenancy reservation), every configuration the portal accepts
       (conditions A AND B) passes the device preflight.
  P8-D **The tolerant readers agree on hostile configurations** [2.1,
       2.9] — malformed / missing / ``Decimal`` / boolean / out-of-range
       ``limit_mm_per_prompt`` and ``gpu_memory_utilization`` values
       degrade to the SAME effective images and the SAME budget on both
       sides, so the two models cannot drift apart through their input
       parsing either.

ASYMMETRY, recorded deliberately (video widening, 2026-08-19). The portal
now sizes the activation allowance from the TOTAL authored multimodal UNITS
(``image`` + ``video``), because vLLM reserves its worst-case token budget per
modality — its own warning on `ryanorinagxdevkithomelabjp622`: "worst-case
total number of multimodal tokens (32768) ... out of which {'image': 16384,
'video': 16384} are reserved for multi-modal embeddings". MEASURED there at
``gpu_memory_utilization = 0.55``: ``{"image": 1, "video": 0}`` profiled a
2.47 GiB activation peak (KV 6.43 GiB, 29.41x, READY) while ``{"image": 1}``
alone profiled 4.93 GiB (KV 0.20 GiB, 0.89x, FAILED). The DEVICE mirror
(`vllm_runtime.memory_budget`) still counts images only, and it must: it ships
ONLY inside an `aws.edgeml.dda.LocalServer.arm64JP6` component build, which is
task 10/11's leg, and the ≈0.375-per-unit recalibration those measurements
imply lands with it in task 14 / H8. So this suite pins:

  * EXACT parity — every mirrored constant, and the whole required-bytes
    arithmetic — wherever the two modules see the same number of multimodal
    units (which includes every configuration that authors ``video``, i.e.
    everything the portal now writes by default), and
  * the SAFE DIRECTION where they do not: the portal is never LESS
    conservative than the device, and portal-accepted still implies
    device-accepted (P8-C).

Neither leg is weakened: a drift in the shared formula, in any constant, or in
the safe direction still fails loudly here.

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

    # The scalar sizing constants (design Decision 2's table).
    assert fit.MINIMUM_KV_CACHE_BYTES == mb.MINIMUM_KV_CACHE_BYTES == 1 * GIB
    assert fit.ACTIVATION_FLOOR_BYTES == mb.ACTIVATION_FLOOR_BYTES == 2 * GIB
    assert (fit.ACTIVATION_WEIGHT_FRACTION
            == mb.ACTIVATION_WEIGHT_FRACTION == 0.75)
    assert (fit.MULTIMODAL_IMAGE_INCREMENT
            == mb.MULTIMODAL_IMAGE_INCREMENT == 1.0)

    # The reader defaults both modules resolve omitted settings to.
    assert (fit.DEFAULT_IMAGES_PER_PROMPT
            == mb.DEFAULT_IMAGES_PER_PROMPT == 1)
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
    """``(portal_units, device_units)`` for ``config``: the portal counts
    images + videos (an unauthored ``video`` costing vLLM's own default of
    1), the device mirror counts images only until its component build
    lands (task 14 / H8)."""
    return fit.multimodal_units(config), mb.images_per_prompt(config)


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

    # The mirrored FORMULA itself never drifts: fed the same unit count, both
    # modules compute the same requirement. (This is the invariant the H8
    # recalibration must preserve when the device leg's build lands.)
    assert verdict.unverified is False
    assert mb.required_bytes(weights_bytes, portal_units) \
        == finding.required_bytes
    assert mb.activation_allowance(weights_bytes, portal_units) \
        == finding.activation_bytes

    # Terms that do not depend on the unit count agree unconditionally.
    assert verdict.terms["budget_bytes"] == finding.budget_bytes, (
        verdict.terms, finding)
    assert (verdict.terms["kv_floor_bytes"] == finding.kv_floor_bytes
            == mb.MINIMUM_KV_CACHE_BYTES)
    assert verdict.terms["images_per_prompt"] == finding.images_per_prompt, (
        verdict.terms, finding)
    assert verdict.terms["co_tenancy_bytes"] == finding.co_tenancy_bytes, (
        verdict.terms, finding)
    assert verdict.terms["fraction_cap"] == finding.fraction_cap, (
        verdict.terms, finding)

    portal_condition_a = 'budget' not in finding.failed_conditions
    assert portal_condition_a == (
        finding.budget_bytes >= finding.required_bytes)  # self-consistency

    if portal_units == device_units:
        # EXACT parity: every remaining term, and the verdict itself. With
        # MemAvailable == MemTotal the preflight's starvation arm cannot
        # refuse anything its budget arm (util x MemTotal, identical to the
        # portal's budget here) does not, so verdict.ok IS condition A.
        assert verdict.terms["required_bytes"] == finding.required_bytes, (
            verdict.terms, finding)
        assert verdict.terms["activation_bytes"] == finding.activation_bytes, (
            verdict.terms, finding)
        assert verdict.ok == portal_condition_a, (
            "budget-sufficiency drift: portal condition A={} "
            "(budget={} required={}) but device verdict.ok={} "
            "terms={}".format(portal_condition_a, finding.budget_bytes,
                              finding.required_bytes, verdict.ok,
                              verdict.terms))
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
    device_util = mb.gpu_memory_utilization(config)
    portal_units = fit.multimodal_units(config)

    assert finding.images_per_prompt == device_images, (
        "images-per-prompt reader drift on {!r}: portal={} device={}".format(
            raw_limit, finding.images_per_prompt, device_images))
    # The shared formula, fed the portal's unit count, reproduces the
    # finding exactly — so hostile input cannot make the two ARITHMETICS
    # diverge, only the (deliberately more conservative) portal unit count.
    assert finding.required_bytes == mb.required_bytes(weights_bytes,
                                                       portal_units), (
        raw_limit, raw_util, finding)
    # And a hostile value never makes the portal cheaper than the device:
    # every degraded reading still counts at least the image units the
    # device counts (the authored-video leg lands with task 14 / H8).
    assert portal_units >= device_images, (raw_limit, portal_units,
                                           device_images)
    assert finding.required_bytes >= mb.required_bytes(weights_bytes,
                                                       device_images), (
        raw_limit, raw_util, finding)
    assert finding.budget_bytes == int(
        device_util * mb.DEVICE_MEMORY_PROFILE_BYTES[arch]), (
        "utilization reader drift on {!r}: portal budget={} device "
        "util={}".format(raw_util, finding.budget_bytes, device_util))
