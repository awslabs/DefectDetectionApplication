"""Fix-checking PROPERTIES for the multimodal-UNITS activation term
(jp6-vllm-kv-cache-oom-regression, design Decision 1/2 as amended 2026-08-19 —
portal leg, File 1 `vllm_fit_check.py`).

# Validates: Requirements 2.1, 2.4

The amendment, MEASURED on `ryanorinagxdevkithomelabjp622` (LocalServer.
arm64JP6 1.0.62), same model, same `gpu_memory_utilization = 0.55`, verbatim
from vLLM: `{"image": 1, "video": 0}` → `activation peak 2.47GiB; KV Cache
6.43GiB`, `Maximum concurrency 29.41x`, READY; `{"image": 1}` with video
UNBOUNDED → `activation peak 4.93GiB; KV Cache 0.20GiB`, `concurrency 0.89x`,
FAILED. vLLM's warning names the cause: "worst-case total number of multimodal
tokens (32768) ... out of which {'image': 16384, 'video': 16384} are reserved
for multi-modal embeddings" — half the worst case is video this product never
sends. So the activation allowance scales with the TOTAL authored multimodal
units, and an UNAUTHORED `video` key costs a full extra unit because vLLM's own
per-modality default is 1.

Properties in this file:
  P-V1 **An authored `video: 0` is STRICTLY cheaper than an absent `video`**
       [2.1, 2.4] — for the same weights, architecture and utilization, the
       bounded configuration's activation allowance AND `required_bytes` are
       strictly smaller than the unbounded one's, and the difference is
       exactly the one-unit allowance (the measured halving).
  P-V2 **Monotone in the authored units** [2.1] — `required_bytes` is
       non-decreasing in the authored video count and strictly increasing per
       extra unit, so no authoring choice can ever make a bigger demand look
       cheaper.
  P-V3 **Bounding video never makes a verdict worse** [2.1, 2.4] — whenever
       the unbounded configuration is reported as fitting, the bounded one is
       too: the cheapest remediation the product can always take can never
       cost an operator a passing verdict.
  P-V4 **Every message names the units it assumed, and still calls the
       allowance an ESTIMATE** [2.1] — passing or failing, both authoring
       shapes, with the unbounded shape additionally stating the omission and
       its fix.

HONESTY GUARD (design "Honesty Guard"). This file proves PURE MATH and MESSAGE
composition in a stdlib-only module. It does not load a vLLM engine, allocate
GPU memory or reproduce Jetson unified-memory accounting; the device numbers
above are quoted as the CALIBRATION SOURCE, not re-derived here. The
per-unit coefficient itself (`ACTIVATION_WEIGHT_FRACTION`, which those
measurements put nearer 0.375 than 0.75) is deliberately NOT recalibrated by
this suite: the constant is mirrored in the device module
`src/backend/vllm_runtime/memory_budget.py`, which ships only inside an
`aws.edgeml.dda.LocalServer.arm64JP6` component build, so both legs move
together in task 14 / H8.

Hypothesis budget comes from the conftest-registered profiles
(`portal-fast` / `ci`); NO `max_examples` is hardcoded here.

_Requirements: 2.1, 2.4_
"""
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from vllm_fit_check import (
    DEVICE_MEMORY_PROFILE_BYTES,
    GIB,
    MINIMUM_KV_CACHE_BYTES,
    activation_allowance,
    evaluate_fit,
    multimodal_units,
)

_architectures = st.sampled_from(sorted(DEVICE_MEMORY_PROFILE_BYTES))

#: Whole-MiB weights from the weightless edge to 128 GiB (dwarfs every
#: profile), so both verdicts are exercised on every architecture.
_weights = st.integers(min_value=0, max_value=128 * 1024).map(
    lambda mib: mib * (1024 ** 2))

_utilizations = st.floats(min_value=0.01, max_value=1.0, allow_nan=False,
                          allow_infinity=False).map(lambda x: round(x, 3))

_image_counts = st.integers(min_value=1, max_value=8)
_video_counts = st.integers(min_value=0, max_value=8)

_NEVER_LOWER = re.compile(
    r"(lower|decrease|reduce)\w*\s+gpu_memory_utilization", re.IGNORECASE)


def _finding(config, weights_bytes, arch):
    findings = evaluate_fit(config, weights_bytes, [arch])
    assert len(findings) == 1, findings
    return findings[0]


# ---------------------------------------------------------------------------
# P-V1 — an authored `video: 0` is STRICTLY cheaper than an absent `video`
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(arch=_architectures, weights_bytes=_weights,
       utilization=_utilizations, images=_image_counts)
def test_property_bounded_video_is_strictly_cheaper_than_unbounded(
        arch, weights_bytes, utilization, images):
    """P-V1. For the same weights, architecture and utilization, authoring
    `video: 0` yields a STRICTLY SMALLER activation allowance — and therefore
    a strictly smaller `required_bytes` — than leaving the key out, where
    vLLM's own per-modality default of 1 applies. The gap is exactly the
    one-unit allowance, which is the halving the device measured (4.93 ->
    2.47 GiB).

    # Validates: Requirements 2.1, 2.4
    """
    base = {"gpu_memory_utilization": utilization, "max_model_len": 4096}
    unbounded = _finding(dict(base, limit_mm_per_prompt={"image": images}),
                         weights_bytes, arch)
    bounded = _finding(
        dict(base, limit_mm_per_prompt={"image": images, "video": 0}),
        weights_bytes, arch)

    # Same images, same budget — only the video dimension differs.
    assert unbounded.images_per_prompt == bounded.images_per_prompt == images
    assert unbounded.budget_bytes == bounded.budget_bytes
    assert unbounded.weights_bytes == bounded.weights_bytes == weights_bytes

    assert bounded.videos_per_prompt == 0
    assert unbounded.videos_per_prompt == 1
    assert bounded.multimodal_units == images
    assert unbounded.multimodal_units == images + 1

    assert bounded.activation_bytes < unbounded.activation_bytes, (
        "bounding video must be strictly cheaper: bounded={} unbounded={} "
        "(arch={}, weights={}, images={})".format(
            bounded.activation_bytes, unbounded.activation_bytes, arch,
            weights_bytes, images))
    assert bounded.required_bytes < unbounded.required_bytes, (
        bounded.required_bytes, unbounded.required_bytes)

    one_unit = activation_allowance(weights_bytes, 1)
    assert unbounded.activation_bytes - bounded.activation_bytes == one_unit
    assert unbounded.required_bytes - bounded.required_bytes == one_unit
    # And the KV floor is still inside both requirements, unchanged.
    assert bounded.kv_floor_bytes == unbounded.kv_floor_bytes \
        == MINIMUM_KV_CACHE_BYTES


# ---------------------------------------------------------------------------
# P-V2 — monotone in the authored units
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(arch=_architectures, weights_bytes=_weights,
       utilization=_utilizations, images=_image_counts,
       videos=_video_counts)
def test_property_required_bytes_is_monotone_in_the_authored_units(
        arch, weights_bytes, utilization, images, videos):
    """P-V2. `required_bytes` never decreases when an authored count grows: a
    bigger multimodal demand can never be sized cheaper. Each extra unit adds
    exactly the one-unit allowance.

    # Validates: Requirements 2.1
    """
    base = {"gpu_memory_utilization": utilization}
    smaller = _finding(
        dict(base, limit_mm_per_prompt={"image": images, "video": videos}),
        weights_bytes, arch)
    bigger = _finding(
        dict(base, limit_mm_per_prompt={"image": images, "video": videos + 1}),
        weights_bytes, arch)

    assert smaller.multimodal_units == images + videos
    assert bigger.multimodal_units == images + videos + 1
    assert multimodal_units(
        {"limit_mm_per_prompt": {"image": images, "video": videos}}) \
        == smaller.multimodal_units

    assert bigger.required_bytes > smaller.required_bytes
    one_unit = activation_allowance(weights_bytes, 1)
    assert bigger.required_bytes - smaller.required_bytes == one_unit


# ---------------------------------------------------------------------------
# P-V3 — bounding video never makes a verdict worse
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(arch=_architectures, weights_bytes=_weights,
       utilization=_utilizations, images=_image_counts)
def test_property_bounding_video_never_loses_a_passing_verdict(
        arch, weights_bytes, utilization, images):
    """P-V3. Whenever the UNBOUNDED configuration is reported as fitting, the
    bounded one fits too — the demand-reducing remediation the messages lead
    with can never cost an operator a passing verdict. (The converse is
    deliberately NOT claimed: bounding video is exactly what turns some
    failing configurations into fitting ones, which is the incident's fix.)

    # Validates: Requirements 2.1, 2.4
    """
    base = {"gpu_memory_utilization": utilization}
    unbounded = _finding(dict(base, limit_mm_per_prompt={"image": images}),
                         weights_bytes, arch)
    bounded = _finding(
        dict(base, limit_mm_per_prompt={"image": images, "video": 0}),
        weights_bytes, arch)

    if unbounded.fits:
        assert bounded.fits, (
            "bounding video turned a fitting verdict into a failing one: "
            "bounded={!r}".format(bounded))
    # The co-tenancy condition depends only on the fraction, so it can never
    # differ between the two shapes.
    assert (('co_tenancy' in bounded.failed_conditions)
            == ('co_tenancy' in unbounded.failed_conditions))


# ---------------------------------------------------------------------------
# P-V4 — every message names the units and keeps labelling the ESTIMATE
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(arch=_architectures, weights_bytes=_weights,
       utilization=_utilizations, images=_image_counts,
       videos=st.one_of(st.none(), _video_counts))
def test_property_every_message_names_the_multimodal_units_it_assumed(
        arch, weights_bytes, utilization, images, videos):
    """P-V4. Passing or failing, every message that quotes the activation
    allowance labels it an ESTIMATE and names the multimodal units it assumed,
    broken down per modality. When `video` is NOT authored the message also
    states that vLLM's own default applies and how to bound it — otherwise the
    cheapest remediation stays invisible, which is the shape of defect 1.4.
    The never-lower invariant holds throughout.

    # Validates: Requirements 2.1
    """
    limit = {"image": images}
    if videos is not None:
        limit["video"] = videos
    finding = _finding(
        {"gpu_memory_utilization": utilization, "limit_mm_per_prompt": limit},
        weights_bytes, arch)

    effective_videos = 1 if videos is None else videos
    units = images + effective_videos
    message = finding.message

    assert re.search(r"\bESTIMATE\b", message), message
    assert "{} multimodal unit(s) per prompt".format(units) in message, message
    assert "{} image(s) + {} video(s)".format(images, effective_videos) \
        in message, message
    assert finding.multimodal_units == units
    assert finding.videos_per_prompt == effective_videos
    assert not _NEVER_LOWER.search(message), message

    if videos is None:
        assert "limit_mm_per_prompt.video is NOT authored" in message, message
        assert '"video": 0' in message, message
        if not finding.fits:
            # The cheapest demand reduction is offered explicitly, with the
            # measured numbers behind it.
            assert "Cheapest first: set limit_mm_per_prompt.video = 0" \
                in message, message
            assert "4.93 GiB" in message and "2.47 GiB" in message, message
    else:
        assert "NOT authored" not in message, message


# ---------------------------------------------------------------------------
# The incident, both ways round — the measured pair as a worked example
# Validates: Requirements 2.1, 2.4
# ---------------------------------------------------------------------------

def test_measured_pair_at_util_055_is_reproduced_directionally():
    """The 2026-08-19 pair, sized by the corrected model: at `util = 0.55` on
    `arm64_jp6` with 6.59 GiB of weights, the BOUNDED configuration
    (`{"image": 1, "video": 0}`) is sized one unit and the UNBOUNDED one
    (`{"image": 1}`) two, so the bounded requirement is smaller by exactly one
    unit's allowance — the direction the device measured (2.47 vs 4.93 GiB of
    activation peak, 6.43 vs 0.20 GiB of KV cache left).

    The MAGNITUDES are now claimed too, within 0.05 GiB, because
    `ACTIVATION_WEIGHT_FRACTION` HAS been recalibrated to the measured
    0.375 per unit in the same change as the device mirror (task 14 / H8):
    at 6.59 GiB of weights the model predicts 2.47 GiB for one unit and
    4.94 GiB for two against the measured 2.47 and 4.93.

    # Validates: Requirements 2.1, 2.4
    """
    weights = int(6.59 * GIB)
    bounded = _finding({"gpu_memory_utilization": 0.55, "max_model_len": 4096,
                        "limit_mm_per_prompt": {"image": 1, "video": 0}},
                       weights, "arm64_jp6")
    unbounded = _finding({"gpu_memory_utilization": 0.55,
                          "max_model_len": 4096,
                          "limit_mm_per_prompt": {"image": 1}},
                         weights, "arm64_jp6")

    assert bounded.multimodal_units == 1
    assert unbounded.multimodal_units == 2
    # REPOINTED 2026-08-19 (task 14 / H8). SUPERSEDED assertion, recorded
    # verbatim:
    #     assert unbounded.activation_bytes == 2 * bounded.activation_bytes
    # The doubling holds to within ONE BYTE: the allowance truncates a FLOAT
    # (`int(base * multiplier)`) and at 0.375 the base for these weights has a
    # fractional part. Not weakened — one byte, and the same arithmetic runs on
    # the device leg (Property 8 pins the two equal).
    assert abs(unbounded.activation_bytes
               - 2 * bounded.activation_bytes) <= 1
    assert bounded.required_bytes < unbounded.required_bytes
    # The recalibrated magnitudes against the MEASURED peaks (2.47 / 4.93 GiB).
    assert round(bounded.activation_bytes / GIB, 2) == 2.47
    assert round(unbounded.activation_bytes / GIB, 2) == 4.94
    # The budget is the same 16.50 GiB in both cases (0.55 x 30 GiB).
    assert bounded.budget_bytes == unbounded.budget_bytes
    assert round(bounded.budget_bytes / GIB, 2) == 16.50
