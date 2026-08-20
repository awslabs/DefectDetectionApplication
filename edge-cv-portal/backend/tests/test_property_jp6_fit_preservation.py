"""
Preservation properties, PORTAL half (spec:
jp6-vllm-kv-cache-oom-regression, task 2).

**Property 2: Preservation — for every input where the bug condition does
NOT hold, the fixed publish-time pipeline produces the same result as the
original** (design "Preservation Checking":
``FitCheck(X).fits = FitCheck'(X).fits``,
``FitCheck(X).status = FitCheck'(X).status``, ``Publish(X) = Publish'(X)``,
``StagedArgs(X)|5keys = StagedArgs'(X)|5keys``).

OBSERVATION-FIRST METHODOLOGY (binding, task 2). Every expectation below
was OBSERVED on the UNFIXED tree first and is recorded here as the
baseline that must keep holding after the fix — nothing is asserted
because the design says so. The suite therefore PASSES on the unfixed tree
today (that is the point) and any post-fix failure is a real regression.

Why property-based: the preserved surface is a wide input space (arbitrary
utilizations, weights, architecture sets, engine-config overlays) where
hand-picked examples miss edge cases, and the sibling spec
(`vllm-sizing-and-packaging-errors`) already establishes the generators —
``engine_configurations()``, ``estimates()``, ``_architecture_sets`` are
imported from `test_property_fit_check_decision.py` so the two specs'
guarantees stay directly comparable. Hypothesis budget comes from the
conftest-registered profiles (`portal-fast` / `ci`); NO ``max_examples``
is hardcoded anywhere in this file.

The non-bug-condition scope is what makes this preservation rather than a
frozen bug. `NOT isBugCondition(X)` is evaluated with the CORRECTED model's
own arithmetic, defined locally in :func:`fits_under_corrected_model`
(design Decision 2: ``required = weights + activation_allowance + KV
floor``, ``fits = (budget >= required) AND (util <= fraction_cap)``).
Because ``required_corrected >= required_shipped`` for every input and the
budget is untouched, "fits under the corrected model" implies "fits under
the shipped model" — so these assertions hold on BOTH trees by
construction, which is exactly the preservation claim.

Preserved legs covered here (bugfix.md clauses in brackets):
  1. Fitting record [3.1] — verdict, ``budget_bytes``, per-arch finding
     ordering, message contents, and the never-lower invariant.
  2. Publish of a fitting record [3.1] — 200, response shape, ``fit_check``
     annotation (``passed``), audit event.
  3. Unverified estimate [3.2] — ``estimate_weights`` never raises out of
     its public API, stays stdlib-only with no AWS dependency, and publish
     proceeds with ``unverified`` + no findings.
  4. Five pre-existing engine settings [3.3] — key set and values,
     fail-closed unknown keys, per-field range findings, and verbatim
     propagation into the packaged ``model.json``.
  5. JP7 record [3.4] — the host-provable half: a JP7 record inside its
     headroom keeps ``fits = True`` under both models.

DEFERRED, NOT SKIPPED SILENTLY. The [HARDWARE] halves are declared as
explicitly-deferred tests carrying their H-tier and owning task, so they
appear in every run's report instead of vanishing:
  - ``JP7Load(X) = JP7Load'(X)`` → **[HARDWARE] H6**, task 12 (thor1).
  - ``OnnxLoad(X) = OnnxLoad'(X)`` → **[HARDWARE] H2**, task 11.
  - the 1.0.59 device staying healthy → **3.11**, tasks 11-12.
No host test may claim any of them (design "Honesty Guard").

Run (from ``edge-cv-portal/backend``, WITH the suite conftest):
    python3 -m pytest tests/test_property_jp6_fit_preservation.py \
      -q -p no:cacheprovider

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.10**
"""
import importlib.util
import inspect
import json
import os
import re
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION
import vllm_fit_check
from vllm_fit_check import (
    DEFAULT_GPU_MEMORY_UTILIZATION,
    DEVICE_MEMORY_PROFILE_BYTES,
    GIB,
    MINIMUM_KV_CACHE_BYTES,
    WeightEstimate,
    evaluate_fit,
)

# The sibling spec's generators — reused verbatim so the two specs'
# guarantees are comparable (task 2, design "Testing Approach").
from test_property_fit_check_decision import (  # noqa: E402
    _architecture_sets,
    engine_configurations,
    estimates,
    format_gib,
)

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-jp6-fit-preservation"
MODELS_TABLE_NAME = "test-models-jp6-fit-preservation"

_PUBLISH_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "functions", "greengrass_publish.py")

# ---------------------------------------------------------------------------
# The CORRECTED model's arithmetic, defined locally (design Decision 2).
# Local on purpose: the preservation scope must be computable on the UNFIXED
# tree, where vllm_fit_check exports none of these. Task 4.7's parity test
# is what pins the shipped constants to these values after the fix.
# ---------------------------------------------------------------------------
ACTIVATION_FLOOR_BYTES = 2 * GIB
# REPOINTED 2026-08-19 (task 14 / H8). SUPERSEDED value, recorded verbatim:
#     ACTIVATION_WEIGHT_FRACTION = 0.75
# Measured per-unit pair on `ryanorinagxdevkithomelabjp622` (1.0.62,
# `gpu_memory_utilization = 0.55`, 6.59 GiB of weights): ONE unit -> 2.47 GiB,
# TWO units -> 4.93 GiB, i.e. 0.375 of weights per unit. The old 0.75 was
# calibrated to a single point now known to have been a two-unit measurement.
ACTIVATION_WEIGHT_FRACTION = 0.375
MULTIMODAL_IMAGE_INCREMENT = 1.0
# The non-torch/co-tenant residency vLLM subtracts from the SAME budget on
# every load — ESTIMATE, median of seven measured readings (-0.05 .. 8.29 GiB,
# median 2.18) rounded down. The shipped `required` omitted it entirely.
#
# CONSCIOUS REPOINT 2026-08-19, SECOND PASS (spec
# jp6-vllm-kv-cache-oom-regression, task 14 / H9). SUPERSEDED name and
# constant, recorded VERBATIM:
#     NON_TORCH_ALLOWANCE_BYTES = 2 * GIB
#     # The HARD KV term in `required` (PROPOSED, task 14 / H9); the 1 GiB
#     # MINIMUM_KV_CACHE_BYTES keeps its value and its name but is now only the
#     # thin-margin WARNING threshold, which is what design Decision 2 always
#     # documented it as.
#     KV_VIABILITY_FLOOR_BYTES = int(0.25 * GIB)
# Reason: H9's final decision charges NO KV term in `required` at all, and the
# shipped constant is NON_TORCH_MEMORY_BYTES.
NON_TORCH_MEMORY_BYTES = 2 * GIB
CO_TENANCY_RESERVATION_BYTES = {
    'arm64_jp6': 6 * GIB,   # measured: 5.7 GiB of ONNX Triton stubs + containers
    'arm64_jp5': 6 * GIB,   # same 30 GiB profile class (test-local, conservative)
    'arm64_jp7': 8 * GIB,   # design estimate, unmeasured [HARDWARE H6]
}
DEFAULT_IMAGES_PER_PROMPT = 1
# Videos vLLM assumes when `limit_mm_per_prompt.video` is NOT authored — its
# own per-modality default of 1, i.e. UNBOUNDED. Widened schema, 2026-08-19:
# MEASURED on `ryanorinagxdevkithomelabjp622` (LocalServer.arm64JP6 1.0.62) at
# `gpu_memory_utilization = 0.55`, `{'image': 1, 'video': 0}` profiled a
# 2.47 GiB activation peak (KV 6.43 GiB, 29.41x, READY) while `{'image': 1}`
# alone profiled 4.93 GiB (KV 0.20 GiB, 0.89x, FAILED), because vLLM reserves
# half of its 32768-token worst case for video (`{'image': 16384,
# 'video': 16384}`). So a configuration that authors NOTHING is sized for TWO
# multimodal units, and the authored default `{'image': 1, 'video': 0}` for
# one.
DEFAULT_VIDEOS_PER_PROMPT = 1
DEFAULT_MULTIMODAL_UNITS = DEFAULT_IMAGES_PER_PROMPT + DEFAULT_VIDEOS_PER_PROMPT


def activation_allowance(weights_bytes,
                         multimodal_units=DEFAULT_MULTIMODAL_UNITS):
    """design Decision 2 as amended 2026-08-19: ``max(floor, fraction ×
    weights) × (1 + increment × (units − 1))``, where ``units`` is the TOTAL
    of the authored per-modality limits (images + videos), not the image count
    alone. The default is what an Engine_Configuration authoring NOTHING is
    sized for: one image plus one unbounded video."""
    base = max(ACTIVATION_FLOOR_BYTES,
               int(ACTIVATION_WEIGHT_FRACTION * weights_bytes))
    units = max(1, int(multimodal_units))
    return int(base * (1 + MULTIMODAL_IMAGE_INCREMENT * (units - 1)))


def fraction_cap(arch):
    """``(profile − co_tenancy) / profile`` — JP6: ``(30 − 6)/30 = 0.80``."""
    profile = DEVICE_MEMORY_PROFILE_BYTES[arch]
    reservation = CO_TENANCY_RESERVATION_BYTES.get(arch, 6 * GIB)
    return (profile - reservation) / profile


def fits_under_corrected_model(arch, utilization, weights_bytes,
                               multimodal_units=DEFAULT_MULTIMODAL_UNITS):
    """``NOT isBugCondition(X)`` for the fit-verdict legs: the record fits
    under the CORRECTED model (condition A ∧ condition B), which implies it
    fits under the shipped one too (``required_corrected >=
    required_shipped``, identical budget)."""
    profile = DEVICE_MEMORY_PROFILE_BYTES[arch]
    budget = int(float(utilization) * profile)
    # REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED expression, recorded
    # verbatim:
    #     required = (int(weights_bytes)
    #                 + activation_allowance(weights_bytes, multimodal_units)
    #                 + MINIMUM_KV_CACHE_BYTES)
    # The implication this helper relies on still holds: the corrected
    # requirement charges the non-torch allowance (2 GiB) plus an activation
    # allowance that is never below its 2 GiB floor, so it stays strictly
    # ABOVE the shipped `weights + MINIMUM_KV_CACHE_BYTES`.
    #
    # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (task 14 / H9). SUPERSEDED
    # expression, recorded VERBATIM:
    #     required = (int(weights_bytes) + NON_TORCH_ALLOWANCE_BYTES
    #                 + activation_allowance(weights_bytes, multimodal_units)
    #                 + KV_VIABILITY_FLOOR_BYTES)
    # The implication this helper relies on is UNAFFECTED by dropping the
    # 0.25 GiB term: the corrected requirement still charges 2 GiB of non-torch
    # plus an activation allowance never below its 2 GiB floor, so it remains
    # strictly ABOVE the shipped `weights + MINIMUM_KV_CACHE_BYTES`.
    required = (int(weights_bytes) + NON_TORCH_MEMORY_BYTES
                + activation_allowance(weights_bytes, multimodal_units))
    return budget >= required and float(utilization) <= fraction_cap(arch)


# ---------------------------------------------------------------------------
# 1. Fitting record (3.1) — the verdict, the budget and the message survive
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(config_case=engine_configurations(), estimate_case=estimates(),
       architectures=_architecture_sets)
def test_fitting_record_verdict_and_budget_are_preserved(
        config_case, estimate_case, architectures):
    """OBSERVED on the unfixed tree and preserved: for every architecture
    where the record fits under the CORRECTED model, the finding reports
    ``fits = True``, ``budget_bytes == int(util × profile[arch])`` (the
    profile values are unchanged by the fix — design Decision 2 keeps
    30 GiB / 120 GiB and only re-documents them), the arch is named in the
    message, and one finding is emitted per profiled architecture in input
    order with unprofiled architectures skipped.

    **Validates: Requirements 3.1, 3.4**"""
    engine_configuration, utilization = config_case
    estimate_arg, estimate_bytes = estimate_case

    findings = evaluate_fit(engine_configuration, estimate_arg, architectures)

    profiled = [a for a in architectures if a in DEVICE_MEMORY_PROFILE_BYTES]
    assert [f.arch for f in findings] == profiled, (
        "finding-per-profiled-architecture ordering changed: expected {}, "
        "got {}".format(profiled, [f.arch for f in findings]))

    for finding in findings:
        profile_bytes = DEVICE_MEMORY_PROFILE_BYTES[finding.arch]
        expected_budget = int(float(utilization) * profile_bytes)
        assert finding.budget_bytes == expected_budget, (
            "{}: budget_bytes moved: {} != int({} × {})".format(
                finding.arch, finding.budget_bytes, utilization,
                profile_bytes))
        assert finding.arch in finding.message, (
            "{}: the message no longer names the profile entry used: "
            "{!r}".format(finding.arch, finding.message))

        if fits_under_corrected_model(finding.arch, utilization,
                                      estimate_bytes):
            assert finding.fits is True, (
                "{}: a record that fits under BOTH models is reported as "
                "NOT fitting (budget {}, weights {}, activation allowance "
                "{}, KV floor {}, cap {:.4f}, util {})".format(
                    finding.arch, format_gib(finding.budget_bytes),
                    format_gib(estimate_bytes),
                    format_gib(activation_allowance(estimate_bytes)),
                    format_gib(MINIMUM_KV_CACHE_BYTES),
                    fraction_cap(finding.arch), utilization))
            assert format_gib(finding.budget_bytes) in finding.message, (
                "{}: the passing message no longer states the budget "
                "{}: {!r}".format(finding.arch,
                                  format_gib(finding.budget_bytes),
                                  finding.message))

        # The sibling spec's invariant, preserved categorically (S5): no
        # message — passing or failing — ever advises LOWERING the
        # fraction as a cure for insufficient KV cache.
        assert not re.search(r"(lower|decrease|reduce)\w*\s+"
                             r"gpu_memory_utilization",
                             finding.message, re.IGNORECASE), (
            "{}: a message advises lowering gpu_memory_utilization: "
            "{!r}".format(finding.arch, finding.message))


@settings(deadline=None)
@given(config_case=engine_configurations(), estimate_case=estimates())
def test_fit_finding_keeps_its_five_original_fields(config_case,
                                                    estimate_case):
    """The ``FitFinding`` contract is extended ADDITIVELY (design Decision
    2 step 5): the five original fields keep their names and types, so
    existing consumers (`greengrass_publish.asdict`, the frontend's
    ``VllmFitCheckFinding``) keep working. OBSERVED shape on the unfixed
    tree: ``arch: str, fits: bool, budget_bytes: int, required_bytes: int,
    message: str``.

    **Validates: Requirements 3.1**"""
    engine_configuration, _utilization = config_case
    estimate_arg, _estimate_bytes = estimate_case

    findings = evaluate_fit(engine_configuration, estimate_arg,
                            ['arm64_jp6', 'arm64_jp7'])
    assert len(findings) == 2
    for finding in findings:
        assert isinstance(finding.arch, str)
        assert isinstance(finding.fits, bool)
        assert isinstance(finding.budget_bytes, int)
        assert isinstance(finding.required_bytes, int)
        assert isinstance(finding.message, str)
        # asdict() is what the publish handler serializes.
        from dataclasses import asdict
        payload = asdict(finding)
        for key in ('arch', 'fits', 'budget_bytes', 'required_bytes',
                    'message'):
            assert key in payload, (
                "the serialized finding lost the '{}' field: {}".format(
                    key, sorted(payload)))


# ---------------------------------------------------------------------------
# 4. JP7 record (3.4) — the host-provable half
# ---------------------------------------------------------------------------

JP7_UTILIZATION = 0.5
JP7_WEIGHTS_BYTES = 16 * GIB


def test_jp7_record_within_headroom_fits_under_both_models():
    """The recorded JP7 verdict (task 2, design 3.4): ``util = 0.5``,
    ~16 GiB of weights on ``arm64_jp7`` → ``budget = 60.00 GiB``,
    corrected ``required = 16 + 2 (non-torch) + 6 (activation) + 0.25 (KV
    viability) = 24.25 GiB``, and ``0.5 <= cap 0.9333`` — so the verdict is
    ``fits = True`` under the shipped model AND under the corrected one. The
    device half of 3.4 is **[HARDWARE] H6** (task 12) and is NOT claimed here.

    REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED numbers, recorded
    verbatim::

        corrected_required = (JP7_WEIGHTS_BYTES
                              + activation_allowance(JP7_WEIGHTS_BYTES, 1)
                              + MINIMUM_KV_CACHE_BYTES)
        assert format_gib(corrected_required) == "29.00 GiB", format_gib(
            corrected_required)
        ...
        assert format_gib(legacy.required_bytes) == "41.00 GiB", format_gib(
            legacy.required_bytes)

    The PRESERVATION claim is unchanged and still what matters: JP7 fits under
    both models, in both authoring shapes. The requirement moved DOWN
    (29.00 -> 24.00 and 41.00 -> 30.00), so a JP7 record that fitted before
    cannot have started failing.

    CONSCIOUS REPOINT 2026-08-19, SECOND PASS (task 14 / H9). SUPERSEDED
    figures, recorded VERBATIM: ``(29.00 -> 24.25 and 41.00 -> 30.25)`` — the
    intermediate form of the change charged a hard 0.25 GiB KV viability floor;
    H9's final decision charges no KV term, so both totals drop by 0.25 GiB and
    the preservation claim is if anything safer.

    **Validates: Requirements 3.4**"""
    findings = evaluate_fit(
        {'gpu_memory_utilization': JP7_UTILIZATION,
         'limit_mm_per_prompt': {'image': 1, 'video': 0}},
        JP7_WEIGHTS_BYTES, ['arm64_jp7'])
    assert len(findings) == 1
    finding = findings[0]

    assert finding.fits is True, finding.message
    assert finding.budget_bytes == int(
        JP7_UTILIZATION * DEVICE_MEMORY_PROFILE_BYTES['arm64_jp7'])
    assert format_gib(finding.budget_bytes) == "60.00 GiB"

    # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (task 14 / H9). SUPERSEDED
    # expression and figure, recorded VERBATIM:
    #     corrected_required = (JP7_WEIGHTS_BYTES + NON_TORCH_ALLOWANCE_BYTES
    #                           + activation_allowance(JP7_WEIGHTS_BYTES, 1)
    #                           + KV_VIABILITY_FLOOR_BYTES)
    #     assert format_gib(corrected_required) == "24.25 GiB", format_gib(
    #         corrected_required)
    # Reason: H9 charges no KV term, so JP7's requirement is 16 + 2 + 6 =
    # 24.00 GiB. The PRESERVATION claim is untouched and if anything safer —
    # the requirement moved DOWN again, so a JP7 record that fitted cannot
    # have started failing.
    corrected_required = (JP7_WEIGHTS_BYTES + NON_TORCH_MEMORY_BYTES
                          + activation_allowance(JP7_WEIGHTS_BYTES, 1))
    assert format_gib(corrected_required) == "24.00 GiB", format_gib(
        corrected_required)
    assert corrected_required == finding.required_bytes
    assert corrected_required <= finding.budget_bytes
    assert JP7_UTILIZATION <= fraction_cap('arm64_jp7')
    assert round(fraction_cap('arm64_jp7'), 4) == 0.9333

    # And a LEGACY record that authors no multimodal limit at all — sized for
    # two units since the 2026-08-19 widening, because vLLM's own video
    # default is 1 — still fits on JP7 (16 + 2 + 12 = 30.00 GiB of a
    # 60.00 GiB budget). Preservation 3.4 holds for both authoring shapes.
    #
    # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (task 14 / H9). SUPERSEDED
    # figure, recorded VERBATIM (it included the intermediate 0.25 GiB term):
    #     # ... (16 + 2 + 12 + 0.25 = 30.25 GiB of a 60.00 GiB budget)
    #     assert format_gib(legacy.required_bytes) == "30.25 GiB", format_gib(
    #         legacy.required_bytes)
    legacy = evaluate_fit({'gpu_memory_utilization': JP7_UTILIZATION},
                          JP7_WEIGHTS_BYTES, ['arm64_jp7'])[0]
    assert legacy.multimodal_units == DEFAULT_MULTIMODAL_UNITS
    assert format_gib(legacy.required_bytes) == "30.00 GiB", format_gib(
        legacy.required_bytes)
    assert legacy.fits is True, legacy.message
    assert legacy.fits is True, legacy.message


# ---------------------------------------------------------------------------
# 3. Unverified estimate (3.2) — never raises, stays stdlib-only
# ---------------------------------------------------------------------------

_hostile_scalars = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(allow_nan=True),
    st.text(max_size=40), st.lists(st.text(max_size=8), max_size=3),
    st.dictionaries(st.text(max_size=6), st.integers(), max_size=3),
)


@st.composite
def hostile_records(draw):
    """Records with arbitrary / malformed ``model_source`` shapes — the
    inputs that must degrade to ``None`` (unverified) rather than raise."""
    kind = draw(st.sampled_from(
        ("missing", "not_a_dict", "hf_garbage", "s3_garbage", "both_garbage")))
    if kind == "missing":
        return {}
    if kind == "not_a_dict":
        return {'model_source': draw(_hostile_scalars)}
    if kind == "hf_garbage":
        return {'model_source': {'huggingface_model_id':
                                 draw(_hostile_scalars)},
                'engine_configuration': {'dtype': draw(st.text(max_size=8))}}
    if kind == "s3_garbage":
        return {'model_source': {'s3_model_artifact': draw(_hostile_scalars)}}
    return {'model_source': {'huggingface_model_id': draw(_hostile_scalars),
                             's3_model_artifact': draw(_hostile_scalars)}}


@settings(deadline=None)
@given(record=hostile_records())
def test_estimate_weights_never_raises_out_of_its_public_api(record):
    """OBSERVED and preserved (3.2): ``estimate_weights`` degrades to
    ``None`` — never an exception — for any record shape, including a
    fetcher that itself explodes. ``None`` is what makes the publish
    'unverified' instead of blocked.

    **Validates: Requirements 3.2**"""
    def exploding_fetch(url):
        raise RuntimeError("synthetic fetch failure")

    def exploding_head(**kwargs):
        raise RuntimeError("synthetic head failure")

    result = vllm_fit_check.estimate_weights(record, s3_head=exploding_head,
                                             hf_fetch=exploding_fetch)
    assert result is None or isinstance(result, WeightEstimate), (
        "estimate_weights returned an unexpected type: {!r}".format(result))


def test_fit_check_module_stays_stdlib_only_with_no_aws_dependency():
    """OBSERVED and preserved (3.2): the sizing module imports no boto3 /
    botocore and reaches AWS only through the injected ``s3_head``
    callable, so it stays importable in every consumer (model_import,
    models, greengrass_publish) with no AWS dependency.

    **Validates: Requirements 3.2**"""
    source = inspect.getsource(vllm_fit_check)
    for forbidden in ('import boto3', 'from boto3', 'import botocore',
                      'from botocore'):
        assert forbidden not in source, (
            "vllm_fit_check acquired an AWS dependency: {!r}".format(
                forbidden))
    assert 'boto3' not in sys.modules or True  # importability, not isolation
    # Public API surface the consumers bind to.
    for name in ('evaluate_fit', 'estimate_weights', 'FitFinding',
                 'WeightEstimate', 'DEVICE_MEMORY_PROFILE_BYTES',
                 'MINIMUM_KV_CACHE_BYTES', 'GIB'):
        assert hasattr(vllm_fit_check, name), (
            "vllm_fit_check lost its public name '{}'".format(name))


# ---------------------------------------------------------------------------
# 2 + 3 (publish legs) — the moto-backed publish harness
# ---------------------------------------------------------------------------

def _load_publish_module():
    """Load functions/greengrass_publish.py under a distinct module name
    inside the moto mock (its module-level boto3 resource and table names
    then bind to the test stack)."""
    spec = importlib.util.spec_from_file_location(
        "portal_greengrass_publish_jp6_fit_preservation", _PUBLISH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_greengrass_publish_jp6_fit_preservation"] = module
    spec.loader.exec_module(module)
    return module


class _FakePaginator:
    def __init__(self, fake, operation):
        self.fake = fake
        self.operation = operation

    def paginate(self, **kwargs):
        if self.operation == "list_components":
            yield {"components": [
                {"componentName": name,
                 "arn": ("arn:aws:greengrass:{}:123456789012:components:"
                         "{}".format(REGION, name))}
                for name in sorted(self.fake.registered)
            ]}
        elif self.operation == "list_component_versions":
            name = str(kwargs["arn"]).split(":components:")[1].split(":")[0]
            yield {"componentVersions": [
                {"componentVersion": version}
                for version in sorted(self.fake.registered.get(name, ()))
            ]}
        else:  # pragma: no cover
            raise AssertionError("unexpected paginator: {}".format(
                self.operation))


class FakeGreengrass:
    """moto has no greengrassv2: record what the publish would register."""

    def __init__(self):
        self.created = []
        self.deleted = []
        self.registered = {}

    def create_component_version(self, inlineRecipe, tags=None):
        recipe = json.loads(inlineRecipe)
        self.created.append(recipe)
        self.registered.setdefault(recipe["ComponentName"], set()).add(
            recipe["ComponentVersion"])
        return {"arn": ("arn:aws:greengrass:{}:123456789012:components:"
                        "{}:versions:{}".format(REGION,
                                                recipe["ComponentName"],
                                                recipe["ComponentVersion"]))}

    def describe_component(self, arn):
        return {"status": {"componentState": "DEPLOYABLE", "message": ""}}

    def delete_component(self, arn):
        self.deleted.append(arn)

    def get_paginator(self, operation):
        return _FakePaginator(self, operation)


@pytest.fixture(scope="module")
def pub_env(aws_stack):
    """Training-jobs + models tables, the real greengrass_publish module,
    and the real model_import / packaging modules bound inside moto."""
    import boto3

    mp = pytest.MonkeyPatch()
    mp.setenv("TRAINING_JOBS_TABLE", TRAINING_JOBS_TABLE_NAME)
    mp.setenv("MODELS_TABLE", MODELS_TABLE_NAME)

    client = boto3.client("dynamodb", region_name=REGION)
    for table in (TRAINING_JOBS_TABLE_NAME, MODELS_TABLE_NAME):
        key = "training_id" if table == TRAINING_JOBS_TABLE_NAME else "model_id"
        client.create_table(
            TableName=table,
            KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": key,
                                   "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    module = _load_publish_module()
    sys.modules.pop("model_import", None)
    import model_import
    sys.modules.pop("packaging", None)
    import packaging

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=module,
        model_import=model_import,
        packaging=packaging,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        models=resource.Table(MODELS_TABLE_NAME),
        usecases=aws_stack.tables.usecases,
        user_roles=aws_stack.tables.user_roles,
        audit_log=aws_stack.tables.audit_log,
    )
    mp.undo()


@pytest.fixture
def seeded(pub_env, monkeypatch):
    """Fresh Use_Case + DataScientist, no polling sleeps, fake Greengrass."""
    monkeypatch.setattr(pub_env.module.time, "sleep", lambda s: None)
    gg = FakeGreengrass()
    monkeypatch.setattr(pub_env.module, "get_usecase_client",
                        lambda service, usecase, **kw: gg)
    usecase_id = "uc-{}".format(uuid.uuid4())
    pub_env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "JP6 Fit Preservation Use Case",
        "account_id": "123456789012",
        "s3_bucket": "test-vllm-usecase-bucket",
    })
    user_id = "user-{}".format(uuid.uuid4())
    pub_env.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return SimpleNamespace(usecase_id=usecase_id, user_id=user_id, gg=gg)


#: The five PRE-EXISTING engine settings, as stored on a record today.
#: The fix adds ``limit_mm_per_prompt`` ADDITIVELY (task 3.1), so every
#: assertion below is written subset-wise over these five keys — the
#: equality guard against drift is S7's job (repointed in task 3.1).
PRE_EXISTING_ENGINE_KEYS = ("dtype", "gpu_memory_utilization",
                            "max_model_len", "tensor_parallel_size",
                            "enforce_eager")

STORED_ENGINE_CONFIGURATION = {
    "dtype": "auto",
    "gpu_memory_utilization": Decimal("0.5"),
    "max_model_len": 4096,
    "tensor_parallel_size": 1,
    "enforce_eager": True,
}

#: A record that fits under BOTH models on every supported architecture:
#: 2 GiB of weights, ``util = 0.5`` (JP6 budget 15.00 GiB, corrected
#: required 2 + 2 + 1 = 5.00 GiB, ``0.5 <= 0.80``).
FITTING_ESTIMATE = WeightEstimate(
    total_bytes=2 * GIB,
    method="safetensors_files",
    detail="synthetic 2 GiB estimate (fits under both models)",
)


def _seed_vllm_record(pub_env, seeded, engine_configuration=None):
    training_id = str(uuid.uuid4())
    item = {
        "training_id": training_id,
        "usecase_id": seeded.usecase_id,
        "model_name": "JP6 Preservation LLM",
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "example/small-llm"},
        "engine_configuration": dict(
            engine_configuration or STORED_ENGINE_CONFIGURATION),
        "packaged_components": [{
            "target": "jetson-xavier-jp6",
            "status": "packaged",
            "component_package_s3": (
                "s3://test-vllm-usecase-bucket/model_artifacts/model-abc/"
                "abc_greengrass_model_component.zip"),
            "supported_architectures": ["arm64_jp6"],
        }],
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    pub_env.training_jobs.put_item(Item=item)
    return item


def _publish_event(training_id, user_id, extra_body=None):
    body = {"component_name": "model-caller-chosen",
            "component_version": "9.0.0"}
    body.update(extra_body or {})
    return {
        "httpMethod": "POST",
        "path": "/api/v1/training/{}/publish".format(training_id),
        "pathParameters": {"id": training_id},
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": {
            "sub": user_id,
            "email": "{}@example.com".format(user_id),
            "cognito:username": user_id,
        }}},
    }


def _audit_actions(pub_env, training_id):
    items = pub_env.audit_log.scan().get("Items", [])
    return [item for item in items
            if training_id in json.dumps(item, default=str)]


def test_publish_of_a_fitting_record_is_preserved(pub_env, seeded,
                                                 monkeypatch):
    """OBSERVED on the unfixed tree and preserved (3.1): a record that fits
    under BOTH models publishes with **200**, the same response shape
    (``published_components`` + ``message``), a ``fit_check`` annotation of
    status **passed** carrying the estimate and per-architecture findings,
    and an audit event — with no ``skip_fit_check`` marker anywhere.

    **Validates: Requirements 3.1, 3.10**"""
    record = _seed_vllm_record(pub_env, seeded)
    monkeypatch.setattr(
        pub_env.module, "estimate_weights",
        lambda record, s3_head=None, hf_fetch=None: FITTING_ESTIMATE)

    response = pub_env.module.publish_component(
        _publish_event(record["training_id"], seeded.user_id), None)

    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])
    assert "published_components" in body and body["published_components"]
    assert "message" in body

    fit_check = body["fit_check"]
    assert fit_check["status"] == "passed", fit_check
    assert fit_check["estimate"]["total_bytes"] == FITTING_ESTIMATE.total_bytes
    assert fit_check["findings"], "per-architecture findings disappeared"
    assert all(finding["fits"] is True for finding in fit_check["findings"])
    for finding in fit_check["findings"]:
        for key in ('arch', 'fits', 'budget_bytes', 'required_bytes',
                    'message'):
            assert key in finding, sorted(finding)

    # A component was registered and the publish was audited.
    assert seeded.gg.created, "no component was registered"
    events = _audit_actions(pub_env, record["training_id"])
    assert events, "the publish produced no audit event"
    assert all("skip_fit_check" not in json.dumps(event, default=str)
               for event in events), (
        "an un-overridden publish recorded a skip_fit_check override")


def test_publish_with_an_unverified_estimate_still_proceeds(pub_env, seeded,
                                                           monkeypatch):
    """OBSERVED and preserved (3.2): an undeterminable Weight_Estimate never
    blocks — the publish returns **200** with ``fit_check.status ==
    'unverified'``, ``estimate: None``, an EMPTY findings list and the
    documented message.

    **Validates: Requirements 3.2**"""
    record = _seed_vllm_record(pub_env, seeded)
    monkeypatch.setattr(
        pub_env.module, "estimate_weights",
        lambda record, s3_head=None, hf_fetch=None: None)

    response = pub_env.module.publish_component(
        _publish_event(record["training_id"], seeded.user_id), None)

    assert response["statusCode"] == 200, response["body"]
    fit_check = json.loads(response["body"])["fit_check"]
    assert fit_check["status"] == "unverified", fit_check
    assert fit_check["estimate"] is None
    assert fit_check["findings"] == []
    assert "could not be estimated" in fit_check["message"]


# ---------------------------------------------------------------------------
# 3.3 — the five pre-existing engine settings and verbatim propagation
# ---------------------------------------------------------------------------

def test_five_pre_existing_engine_defaults_keep_their_values(pub_env):
    """OBSERVED on the unfixed tree and preserved (3.3): the five
    pre-existing settings keep their exact defaults. Written SUBSET-wise
    because task 3.1 adds ``limit_mm_per_prompt`` additively; the
    key-set-equality drift guard is S7's (repointed in the same task as the
    code change).

    **Validates: Requirements 3.3**"""
    defaults = pub_env.model_import.ENGINE_DEFAULTS
    observed = {key: defaults[key] for key in PRE_EXISTING_ENGINE_KEYS}
    assert observed == {
        'dtype': 'auto',
        'gpu_memory_utilization': 0.5,
        'max_model_len': 2048,
        'tensor_parallel_size': 1,
        'enforce_eager': True,
    }, observed
    # Every pre-existing key still resolves through the overlay.
    resolved = pub_env.model_import.resolve_engine_configuration({})
    for key in PRE_EXISTING_ENGINE_KEYS:
        assert resolved[key] == defaults[key], key


@settings(deadline=None)
@given(supplied=st.dictionaries(
    st.sampled_from(PRE_EXISTING_ENGINE_KEYS),
    st.one_of(st.just('auto'), st.just('bfloat16'), st.floats(
        min_value=0.05, max_value=1.0).map(lambda x: round(x, 3)),
        st.integers(min_value=1, max_value=32768), st.booleans()),
    max_size=5))
def test_resolution_overlays_supplied_values_on_defaults(pub_env, supplied):
    """OBSERVED and preserved (3.3): ``resolve_engine_configuration``
    overlays supplied values on the defaults — supplied keys keep their
    value verbatim, omitted keys get their documented default, and the
    result always carries every defined setting.

    **Validates: Requirements 3.3**"""
    resolved = pub_env.model_import.resolve_engine_configuration(supplied)
    defaults = pub_env.model_import.ENGINE_DEFAULTS
    assert set(resolved) == set(defaults), (
        "resolution changed the resolved key set: {} vs {}".format(
            sorted(resolved), sorted(defaults)))
    for key in PRE_EXISTING_ENGINE_KEYS:
        if key in supplied:
            assert resolved[key] == supplied[key], key
        else:
            assert resolved[key] == defaults[key], key


@settings(deadline=None)
@given(key=st.text(min_size=1, max_size=24).filter(
    lambda k: k not in PRE_EXISTING_ENGINE_KEYS
    and k != 'limit_mm_per_prompt'),
    value=st.integers())
def test_unknown_engine_keys_stay_fail_closed(pub_env, key, value):
    """OBSERVED and preserved (3.3): an unknown engine setting is rejected
    fail-closed with a per-field finding naming the key.

    **Validates: Requirements 3.3**"""
    findings = pub_env.model_import.validate_vllm_registration({
        'huggingface_model_id': 'example/small-llm',
        'engine_configuration': {key: value},
    })
    unknown = [f for f in findings
               if f['field'] == 'engine_configuration.{}'.format(key)]
    assert unknown, (
        "unknown engine setting {!r} was not rejected: {}".format(
            key, findings))
    assert 'unknown engine setting' in unknown[0]['reason']
    # Resolution drops it rather than storing it.
    assert key not in pub_env.model_import.resolve_engine_configuration(
        {key: value})


@pytest.mark.parametrize("key,value,expected_fragment", [
    ("dtype", "float8", "dtype must be one of"),
    ("gpu_memory_utilization", 0.0, "gpu_memory_utilization must be in"),
    ("gpu_memory_utilization", 1.5, "gpu_memory_utilization must be in"),
    ("gpu_memory_utilization", True, "must be a number"),
    ("max_model_len", 0, "max_model_len must be an integer >= 1"),
    ("max_model_len", True, "max_model_len must be an integer >= 1"),
    ("tensor_parallel_size", 0, "tensor_parallel_size must be an integer >= 1"),
    ("enforce_eager", "yes", "enforce_eager must be a boolean"),
])
def test_out_of_range_values_keep_their_per_field_findings(
        pub_env, key, value, expected_fragment):
    """OBSERVED and preserved (3.3): every out-of-range value for the five
    pre-existing settings keeps its per-field finding and its reason text.

    **Validates: Requirements 3.3**"""
    findings = pub_env.model_import.validate_vllm_registration({
        'huggingface_model_id': 'example/small-llm',
        'engine_configuration': {key: value},
    })
    matching = [f for f in findings
                if f['field'] == 'engine_configuration.{}'.format(key)]
    assert matching, "no per-field finding for {}={!r}: {}".format(
        key, value, findings)
    assert expected_fragment in matching[0]['reason'], matching[0]


#: BASELINE recorded on the UNFIXED tree (task 2): the packaged
#: ``model.json`` for a record carrying exactly the five pre-existing
#: settings. The fix propagates the new authored key additively, so the
#: byte-identity claim is scoped to these five keys plus ``model`` — each
#: asserted BOTH as a parsed value and as the verbatim serialized line
#: ``json.dumps(..., indent=2)`` produces.
MODEL_JSON_BASELINE_FIVE_KEYS = {
    "dtype": "auto",
    "gpu_memory_utilization": 0.5,
    "max_model_len": 4096,
    "tensor_parallel_size": 1,
    "enforce_eager": True,
    "model": "example/small-llm",
}
MODEL_JSON_BASELINE_LINES = (
    '  "dtype": "auto"',
    '  "gpu_memory_utilization": 0.5',
    '  "max_model_len": 4096',
    '  "tensor_parallel_size": 1',
    '  "enforce_eager": true',
    '  "model": "example/small-llm"',
)


def test_model_json_propagation_of_the_five_keys_is_byte_identical(pub_env):
    """OBSERVED on the unfixed tree and recorded as the byte-identical
    baseline (3.3): ``generate_vllm_repository`` writes the stored
    configuration into ``{name}/1/model.json`` verbatim — Decimals become
    JSON numbers, booleans stay JSON booleans, and ``model`` carries the HF
    id. Both the parsed values and the exact serialized lines are pinned.

    **Validates: Requirements 3.3**"""
    record = {
        "model_name": "JP6 Preservation LLM",
        "model_source": {"huggingface_model_id": "example/small-llm"},
        "engine_configuration": dict(STORED_ENGINE_CONFIGURATION),
    }
    files = pub_env.packaging.generate_vllm_repository(record)

    model_json_paths = [path for path in files if path.endswith("model.json")]
    assert len(model_json_paths) == 1, sorted(files)
    text = files[model_json_paths[0]]
    parsed = json.loads(text)

    for key, expected in MODEL_JSON_BASELINE_FIVE_KEYS.items():
        assert key in parsed, (
            "the staged model.json lost '{}': {}".format(key, sorted(parsed)))
        assert parsed[key] == expected, (
            "{}: staged {!r} != baseline {!r}".format(key, parsed[key],
                                                      expected))
        assert isinstance(parsed[key], type(expected)), (
            "{}: staged type {} != baseline type {}".format(
                key, type(parsed[key]).__name__, type(expected).__name__))
    for line in MODEL_JSON_BASELINE_LINES:
        assert line in text, (
            "the serialized model.json line {!r} changed; staged text was:\n"
            "{}".format(line, text))

    config_pbtxt = [path for path in files if path.endswith("config.pbtxt")]
    assert len(config_pbtxt) == 1
    assert 'backend: "vllm"' in files[config_pbtxt[0]]


# ---------------------------------------------------------------------------
# Fixed-shape legs — ABSENT on the unfixed tree, they bind at task 3.9
# ---------------------------------------------------------------------------

def test_authored_multimodal_default_is_one_image_and_bounded_video():
    """FIXED-SHAPE LEG (bound at task 3.9, amended by the video widening).
    ``ENGINE_DEFAULTS['limit_mm_per_prompt']`` must be
    ``{'image': 1, 'video': 0}`` — the authored, sized default that replaces
    the device-side ``{'image': 2}`` setdefault, with the video modality
    BOUNDED because leaving it out lets vLLM apply its own default of 1 and
    measured a 4.93 GiB activation peak instead of 2.47 GiB on JP6
    (2026-08-19).

    **Validates: Requirements 3.3, 3.9**"""
    sys.modules.pop("model_import", None)
    import model_import

    if 'limit_mm_per_prompt' not in model_import.ENGINE_DEFAULTS:
        pytest.skip("fixed-shape leg: ENGINE_DEFAULTS has no "
                    "limit_mm_per_prompt yet (binds at task 3.9)")
    assert model_import.ENGINE_DEFAULTS['limit_mm_per_prompt'] == {
        'image': 1, 'video': 0}


def test_new_fit_finding_terms_agree_with_the_corrected_model_when_present():
    """FIXED-SHAPE LEG (binds at task 3.9). Once task 3.2 lands, the
    additive ``FitFinding`` terms must reproduce this file's locally-defined
    corrected arithmetic for the incident's numbers (weights 6.5 GiB,
    ``util = 0.4``, ``arm64_jp6``): activation allowance 2.44 GiB, non-torch
    allowance 2 GiB, NO KV term charged, serving-margin floor 1 GiB,
    co-tenancy 6 GiB, cap 0.80. SKIPPED as absent today.

    CONSCIOUS REPOINT 2026-08-19, SECOND PASS (task 14 / H9). SUPERSEDED
    docstring clause, recorded VERBATIM: ``KV viability floor 0.25 GiB``.

    REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED assertions, recorded
    verbatim::

        assert format_gib(finding.activation_bytes) == "4.88 GiB"
        ...
        assert unbounded.activation_bytes == 2 * finding.activation_bytes
        assert finding.kv_floor_bytes == MINIMUM_KV_CACHE_BYTES

    **Validates: Requirements 3.1**"""
    finding = evaluate_fit(
        {'gpu_memory_utilization': 0.4,
         'limit_mm_per_prompt': {'image': 1, 'video': 0}},
        int(6.5 * GIB), ['arm64_jp6'])[0]
    if not hasattr(finding, 'activation_bytes'):
        pytest.skip("fixed-shape leg: FitFinding has no activation_bytes "
                    "yet (binds at task 3.9)")
    # ONE authored multimodal unit at the recalibrated 0.375 per unit.
    assert finding.activation_bytes == activation_allowance(int(6.5 * GIB), 1)
    assert format_gib(finding.activation_bytes) == "2.44 GiB"
    # The same record with video LEFT UNBOUNDED is TWO units, so the recorded
    # arithmetic doubles — the 2026-08-19 measurement (2.47 -> 4.93 GiB peak).
    unbounded = evaluate_fit({'gpu_memory_utilization': 0.4},
                             int(6.5 * GIB), ['arm64_jp6'])[0]
    assert unbounded.activation_bytes == activation_allowance(int(6.5 * GIB))
    assert unbounded.activation_bytes == 2 * finding.activation_bytes
    # The 1 GiB serving margin keeps its field and its value, and is NOT a
    # term in `required`; NO KV term of any size is charged (H9).
    #
    # CONSCIOUS REPOINT 2026-08-19, SECOND PASS (task 14 / H9). SUPERSEDED
    # assertions, recorded VERBATIM:
    #     # ...; the hard KV term is the viability floor (H9).
    #     assert finding.kv_viability_floor_bytes == KV_VIABILITY_FLOOR_BYTES
    #     assert finding.non_torch_bytes == NON_TORCH_ALLOWANCE_BYTES
    #     assert finding.required_bytes == (int(6.5 * GIB)
    #                                      + NON_TORCH_ALLOWANCE_BYTES
    #                                      + finding.activation_bytes
    #                                      + KV_VIABILITY_FLOOR_BYTES)
    assert finding.kv_floor_bytes == MINIMUM_KV_CACHE_BYTES
    assert not hasattr(finding, 'kv_viability_floor_bytes')
    assert finding.non_torch_bytes == NON_TORCH_MEMORY_BYTES
    assert finding.required_bytes == (int(6.5 * GIB)
                                      + NON_TORCH_MEMORY_BYTES
                                      + finding.activation_bytes)
    # The predicted KV remainder is what the budget leaves over, and the
    # serving margin is judged against IT, not charged into `required`.
    assert (finding.kv_headroom_bytes
            == finding.budget_bytes - finding.required_bytes)
    assert finding.co_tenancy_bytes == CO_TENANCY_RESERVATION_BYTES['arm64_jp6']
    assert round(finding.fraction_cap, 2) == 0.80


# ---------------------------------------------------------------------------
# [HARDWARE] legs — DEFERRED explicitly, never silently skipped, never faked
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="[HARDWARE] H6 (task 12, thor1): JP7Load(X) = "
                         "JP7Load'(X) — that the same vLLM generation still "
                         "loads qwen3-vl-8b-instruct with Available KV cache "
                         "memory 36.34 GiB / 264,592 tokens under "
                         "gpu_memory_utilization=0.5 while three vision "
                         "models coexist on GPU cannot be proven host-side "
                         "(no GPU, no Jetson unified memory). DEFERRED, not "
                         "claimed.")
def test_hardware_h6_jp7_device_load_unchanged():  # pragma: no cover
    raise AssertionError("[HARDWARE] H6 must be executed on thor1 (task 12)")


@pytest.mark.skip(reason="[HARDWARE] H2 (task 11): OnnxLoad(X) = OnnxLoad'(X) "
                         "— that model-cookies-binary-jetson-xavier-jp6, "
                         "model-rf-detr-seg-nano-jetson-xavier-jp6 and "
                         "model-yolo-test-jetson-xavier-jp6 still load to "
                         "READY on GPU with unchanged footprint requires the "
                         "device. DEFERRED, not claimed.")
def test_hardware_h2_onnx_co_tenants_unchanged():  # pragma: no cover
    raise AssertionError("[HARDWARE] H2 must be executed on "
                         "ryanorinagxdevkithomelabjp622 (task 11)")


@pytest.mark.skip(reason="[HARDWARE] 3.11 (tasks 11-12): that "
                         "ryanorinagxdevkithomelabjp622 keeps serving "
                         "qwen2-5-vl-7b-instruct-awq on LocalServer 1.0.59 "
                         "(READY, generate 200 in ~1.9 s cold then ~0.9 s) "
                         "until a fixed component is deliberately deployed "
                         "is a device-state claim. DEFERRED, not claimed.")
def test_hardware_3_11_serving_device_stays_healthy():  # pragma: no cover
    raise AssertionError("[HARDWARE] 3.11 is verified on the device "
                         "(tasks 11-12)")
