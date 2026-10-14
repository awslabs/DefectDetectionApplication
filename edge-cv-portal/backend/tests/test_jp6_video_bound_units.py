"""Unit tests for the WIDENED multimodal schema and its bounded-video default
(jp6-vllm-kv-cache-oom-regression, design Decision 1 as amended 2026-08-19 —
portal leg, File 3 `model_import.py` + File 1 `vllm_fit_check.py`).

WHY the schema widened, measured on `ryanorinagxdevkithomelabjp622`
(LocalServer.arm64JP6 1.0.62), same model `qwen2-5-vl-7b-instruct-awq`, same
`gpu_memory_utilization = 0.55`, verbatim from vLLM's own profiling output:

  * staged `{"image": 1, "video": 0}` →
    `weights 6.59GiB; non_torch_memory 0.98GiB; PyTorch activation peak
    2.47GiB; KV Cache 6.43GiB`, `Maximum concurrency 29.41x` → **READY**
  * staged `{"image": 1}` (video UNBOUNDED) →
    `weights 6.59GiB; non_torch_memory 4.76GiB; activation peak 4.93GiB;
    KV Cache 0.20GiB`, `concurrency 0.89x` → **FAILED**: "The model's max seq
    len (4096) is larger than the maximum number of tokens that can be stored
    in KV cache (3664)"

vLLM's own warning explains it: "worst-case total number of multimodal tokens
(32768) ... out of which {'image': 16384, 'video': 16384} are reserved for
multi-modal embeddings". Half of that worst case is video, which this product
never sends (inputs are camera frames and folder images). Bounding video to 0
HALVES the measured activation peak, and before the widening the only
configuration that serves could not be AUTHORED: the validator accepted the
single key `image` only.

Covered here (nothing that
`test_jp6_engine_config_multimodal.py` already owns is repeated — that file
owns the per-value accept/reject matrix and the generated-args propagation
PBT):
- the widened validator's ACCEPTED shapes surfacing as NO findings through the
  public `validate_vllm_registration` API: `{"image": 1, "video": 0}`,
  `{"image": 2}`, `{"video": 0}`
- the REJECTED shapes surfacing as one per-field finding with the existing
  `{field, value, reason}` shape: a third key, non-integers, negatives, >8
- the resolved default being `{"image": 1, "video": 0}`
- that default reaching the packaged `model.json` VERBATIM as JSON integers
  (through `packaging.generate_vllm_repository`, over the stored DynamoDB
  `Decimal` shape)
- the Fit_Check reading the default as ONE multimodal unit while an absent
  `video` key is TWO — the whole point of the widening

_Requirements: 2.1, 2.4, 2.8, 3.3, 3.9_
"""
import importlib.util
import json
import os
import sys
from decimal import Decimal

import pytest

from vllm_fit_check import (
    activation_allowance,
    evaluate_fit,
    multimodal_units,
    videos_per_prompt,
)

_PACKAGING_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "functions", "packaging.py")

GIB = 1024 ** 3

#: The measured JP6 configuration: one image, video bounded to nothing.
MEASURED_SERVING_LIMIT = {"image": 1, "video": 0}


@pytest.fixture(scope="module")
def mi():
    """Freshly imported model_import (pure validation — no AWS needed)."""
    sys.modules.pop("model_import", None)
    import model_import
    yield model_import
    sys.modules.pop("model_import", None)


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """functions/packaging.py under a distinct module name inside the moto
    mock (its file name collides with the PyPI `packaging` distribution and
    its module-level boto3 clients must bind inside the mock)."""
    spec = importlib.util.spec_from_file_location(
        "portal_packaging_video_bound", _PACKAGING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_packaging_video_bound"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("portal_packaging_video_bound", None)


def _registration_findings(mi, limit):
    """Findings from the PUBLIC registration validator for one authored
    `limit_mm_per_prompt` value."""
    return mi.validate_vllm_registration({
        "huggingface_model_id": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ",
        "engine_configuration": {"limit_mm_per_prompt": limit},
    })


# ---------------------------------------------------------------------------
# Accepted shapes — the widened schema can express the measured configuration
# Validates: Requirements 2.4, 3.3
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("limit", [
    {"image": 1, "video": 0},   # the MEASURED serving configuration
    {"image": 2},               # two-image reference generation, video unbound
    {"video": 0},               # video bounded, image left at vLLM's default
    {"image": 8, "video": 8},   # both at the top of their ranges
])
def test_widened_validator_accepts_the_authorable_shapes(mi, limit):
    """`{"image": 1, "video": 0}` — the ONLY configuration measured to serve
    `qwen2-5-vl-7b-instruct-awq` on JP6 with real headroom — must be
    authorable, and so must a bare `{"image": N}` and a bare `{"video": N}`
    (both sub-keys are optional).

    # Validates: Requirements 2.4, 3.3
    """
    assert mi._validate_engine_setting("limit_mm_per_prompt", limit) == ""
    assert _registration_findings(mi, limit) == [], (
        "{!r} must register without findings".format(limit))


# ---------------------------------------------------------------------------
# Rejected shapes — still fail-closed, still one per-field finding each
# Validates: Requirements 2.8, 3.3
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("limit", [
    {"image": 1, "video": 0, "audio": 1},   # a third key
    {"audio": 1},                           # only an unknown key
    {"video": "0"},                         # non-integer (string)
    {"video": 1.5},                         # non-integer (float)
    {"video": 0.0},                         # non-integer (float, even at 0)
    {"video": True},                        # bool is an int subclass
    {"video": None},                        # non-integer (null)
    {"video": -1},                          # negative
    {"image": -1},                          # negative
    {"video": 9},                           # above the range
    {"image": 9},                           # above the range
    {"image": 1, "video": 9},               # one good sub-key, one bad
    {},                                     # neither sub-key
    "video",                                # not an object at all
])
def test_widened_validator_stays_fail_closed(mi, limit):
    """Widening the accepted KEYS did not widen the accepted VALUES: an
    unknown sub-key, a non-integer, a negative and an out-of-range count are
    each still rejected, each as exactly ONE finding carrying the existing
    `{field, value, reason}` shape.

    # Validates: Requirements 2.8, 3.3
    """
    reason = mi._validate_engine_setting("limit_mm_per_prompt", limit)
    assert reason, "{!r} must be rejected".format(limit)

    findings = _registration_findings(mi, limit)
    assert len(findings) == 1, findings
    finding = findings[0]
    assert set(finding) == {"field", "value", "reason"}, finding
    assert finding["field"] == "engine_configuration.limit_mm_per_prompt"
    assert finding["value"] == limit
    assert finding["reason"] == reason
    assert "limit_mm_per_prompt" in finding["reason"]


def test_unknown_engine_keys_are_still_rejected(mi):
    """The fail-closed unknown-SETTING rule is untouched by the widening
    (preservation 3.3).

    # Validates: Requirements 3.3
    """
    findings = mi.validate_vllm_registration({
        "huggingface_model_id": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ",
        "engine_configuration": {"limit_mm_per_video": {"video": 0}},
    })
    assert len(findings) == 1, findings
    assert findings[0]["field"] == "engine_configuration.limit_mm_per_video"
    assert "unknown engine setting" in findings[0]["reason"]


# ---------------------------------------------------------------------------
# The resolved default
# Validates: Requirements 2.4, 3.3
# ---------------------------------------------------------------------------

def test_resolved_default_bounds_video(mi):
    """A record that authors nothing resolves to the MEASURED serving shape:
    `{"image": 1, "video": 0}`.

    # Validates: Requirements 2.4, 3.3
    """
    assert mi.ENGINE_DEFAULTS["limit_mm_per_prompt"] == MEASURED_SERVING_LIMIT
    resolved = mi.resolve_engine_configuration({})
    assert resolved["limit_mm_per_prompt"] == MEASURED_SERVING_LIMIT
    # The other four settings keep their documented defaults.
    assert resolved["gpu_memory_utilization"] == 0.5
    assert resolved["dtype"] == "auto"
    assert resolved["max_model_len"] == 2048
    assert resolved["enforce_eager"] is True


def test_supplied_limit_replaces_the_default_verbatim(mi):
    """A supplied `limit_mm_per_prompt` wins VERBATIM — it is not merged with
    the default. Authoring `{"image": 2}` therefore leaves video UNBOUNDED,
    which the Fit_Check prices as a second unit and says so in its message
    (there is no silent, invisible bound anywhere).

    # Validates: Requirements 2.4, 3.3
    """
    resolved = mi.resolve_engine_configuration(
        {"limit_mm_per_prompt": {"image": 2}})
    assert resolved["limit_mm_per_prompt"] == {"image": 2}
    assert videos_per_prompt(resolved) == 1        # vLLM's own default
    assert multimodal_units(resolved) == 3         # 2 images + 1 video

    bounded = mi.resolve_engine_configuration(
        {"limit_mm_per_prompt": {"image": 2, "video": 0}})
    assert multimodal_units(bounded) == 2


# ---------------------------------------------------------------------------
# Verbatim propagation of the default into the packaged model.json
# Validates: Requirements 3.3
# ---------------------------------------------------------------------------

def test_default_limit_reaches_model_json_verbatim(mi, packaging):
    """The resolved default survives the DynamoDB round trip and lands in
    `{name}/1/model.json` verbatim, as JSON integers — `video: 0` must not
    become `0.0`, `"0"`, `false` or a `Decimal`, because the device passes the
    staged value straight to `AsyncEngineArgs`.

    # Validates: Requirements 3.3
    """
    resolved = mi.resolve_engine_configuration({})
    stored = mi._to_dynamo_compatible(resolved)
    # DynamoDB storage really is Decimal, one level down too.
    assert isinstance(stored["limit_mm_per_prompt"]["video"], int)
    assert stored["limit_mm_per_prompt"] == {"image": 1, "video": 0}
    # A record whose numbers came back from DynamoDB as Decimal.
    stored = dict(stored)
    stored["limit_mm_per_prompt"] = {"image": Decimal(1), "video": Decimal(0)}

    files = packaging.generate_vllm_repository({
        "model_name": "Video Bound LLM",
        "model_source": {
            "huggingface_model_id": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ"},
        "engine_configuration": stored,
    })
    paths = [path for path in files if path.endswith("/1/model.json")]
    assert len(paths) == 1, sorted(files)
    text = files[paths[0]]
    model_json = json.loads(text)

    assert model_json["limit_mm_per_prompt"] == {"image": 1, "video": 0}
    staged = model_json["limit_mm_per_prompt"]
    for key in ("image", "video"):
        assert isinstance(staged[key], int) and not isinstance(staged[key],
                                                              bool), (
            "{} staged as {!r}".format(key, staged[key]))
    # The serialized bytes carry the bound as a JSON number.
    assert '"video": 0' in text, text
    # Nothing was injected and nothing was dropped.
    assert set(model_json) == set(stored) | {"model"}


# ---------------------------------------------------------------------------
# The Fit_Check reads the default as ONE unit, an absent video key as TWO
# Validates: Requirements 2.1, 2.4
# ---------------------------------------------------------------------------

def test_fit_check_sizes_the_default_as_one_unit(mi):
    """The authored default is ONE multimodal unit; the same record with the
    `video` key absent is TWO, and its activation allowance is exactly double
    — the 2.47 vs 4.93 GiB the device measured.

    # Validates: Requirements 2.1, 2.4
    """
    weights = int(6.59 * GIB)
    resolved = mi.resolve_engine_configuration(
        {"gpu_memory_utilization": 0.55, "max_model_len": 4096})
    assert multimodal_units(resolved) == 1

    bounded = evaluate_fit(resolved, weights, ["arm64_jp6"])[0]
    assert bounded.videos_per_prompt == 0
    assert bounded.multimodal_units == 1
    assert bounded.activation_bytes == activation_allowance(weights, 1)

    legacy = dict(resolved)
    legacy.pop("limit_mm_per_prompt")
    unbounded = evaluate_fit(legacy, weights, ["arm64_jp6"])[0]
    assert unbounded.videos_per_prompt == 1
    assert unbounded.multimodal_units == 2
    # REPOINTED 2026-08-19 (task 14 / H8). SUPERSEDED assertion, recorded
    # verbatim:
    #     assert unbounded.activation_bytes == 2 * bounded.activation_bytes
    # The doubling still holds, but only to within ONE BYTE: the allowance is
    # `int(base * multiplier)` over a FLOAT base, and at the recalibrated
    # 0.375 coefficient `0.375 x int(6.59 GiB)` has a fractional part, so
    # truncating once at two units differs from truncating at one unit and
    # doubling. Not weakened — the tolerance is a single byte, and the
    # production formula is byte-identical on both the portal and the device
    # legs (Property 8 pins them equal).
    assert abs(unbounded.activation_bytes
               - 2 * bounded.activation_bytes) <= 1
    assert unbounded.activation_bytes == activation_allowance(weights, 2)
    assert unbounded.required_bytes > bounded.required_bytes
    # Both messages label the allowance an ESTIMATE and name their units.
    for finding, units in ((bounded, 1), (unbounded, 2)):
        assert "ESTIMATE" in finding.message
        assert "{} multimodal unit(s) per prompt".format(units) \
            in finding.message
