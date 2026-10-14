"""Unit tests for the authored multimodal engine setting
(jp6-vllm-kv-cache-oom-regression, task 3.1 — design Decision 1, File 3).

`limit_mm_per_prompt` becomes a first-class vLLM_Engine_Configuration
setting: default `{'image': 1, 'video': 0}`, validated as a dict whose keys
are a non-empty subset of `{image, video}` (`image` an integer 1..8, `video`
an integer 0..8), stored on the record and propagated
verbatim into `model.json`. An unbudgeted device-side default is invisible
to every sizing surface by construction (defect 1.4); authoring the limit is
what makes it the term the Fit_Check can size (expected behavior 2.4).

Covers:
- the default value and the fact that resolving never aliases it
- the accepted arm: every integer in 1..8
- the rejected arms, each with its own per-field reason: non-dict, extra
  key, missing key, non-int (including `bool`, an `int` subclass) and
  out-of-range images
- the reason surfacing as a per-field finding through
  `validate_vllm_registration`, with the fail-closed unknown-key rule
  untouched (preservation 3.3)
- the engine-spec endpoint advertising the field (both frontend forms are
  schema-driven off it, so no frontend wiring is needed) with the
  profiling-peak note and the two-image reference-generation note
  (vlm-anomaly-reference-parity Requirement 6.6)
- the Decimal/DynamoDB conversions recursing into the nested map, which is
  what keeps propagation into `model.json` verbatim

Grown by task 4.3 (design Property 4, portal half) with the fix-checking
PBT over generated staged-args dictionaries: no key is ever injected that
the authored configuration did not contain; the authored
`limit_mm_per_prompt` reaches `packaging.generate_vllm_repository`'s
`model.json` verbatim (inspected as JSON); and that authored value is
exactly the value `vllm_fit_check.images_per_prompt` feeds the Fit_Check.
Hypothesis budget comes from the conftest-registered profiles
(`portal-fast` / `ci`); NO ``max_examples`` is hardcoded anywhere in this
file.

_Requirements: 1.4, 2.4, 3.3, 3.9_
"""
import importlib.util
import json
import os
import sys
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vllm_fit_check import DEFAULT_IMAGES_PER_PROMPT, images_per_prompt


@pytest.fixture(scope="module")
def mi():
    """Freshly imported model_import (pure validation — no AWS needed)."""
    sys.modules.pop("model_import", None)
    import model_import
    yield model_import
    # Leave the module table clean for suites that re-import under moto.
    sys.modules.pop("model_import", None)


# ---------------------------------------------------------------------------
# The authored default
# ---------------------------------------------------------------------------

def test_default_is_a_single_image_and_no_video(mi):
    """The authored default is ONE image and an explicitly BOUNDED video
    modality: `{'image': 1, 'video': 0}`.

    `video: 0` is in the default because of what was measured on
    `ryanorinagxdevkithomelabjp622` (LocalServer.arm64JP6 1.0.62,
    2026-08-19), same model, same `gpu_memory_utilization = 0.55`:
    `{'image': 1, 'video': 0}` profiled a 2.47 GiB activation peak with
    6.43 GiB of KV cache (29.41x concurrency, READY), while `{'image': 1}`
    alone — video unbounded at vLLM's own per-modality default of 1 —
    profiled 4.93 GiB with 0.20 GiB of KV and FAILED. vLLM reserves half of
    its 32768-token worst-case multimodal budget for video
    (`{'image': 16384, 'video': 16384}`) on a product that never sends any,
    so bounding it is the demand reduction every newly authored record
    should start with.
    """
    assert mi.ENGINE_DEFAULTS["limit_mm_per_prompt"] == {"image": 1,
                                                        "video": 0}
    assert mi.ENGINE_DEFAULTS["limit_mm_per_prompt"]["video"] == 0, (
        "the default must BOUND video: omitting the key lets vLLM apply its "
        "own default of 1, which measured a 4.93 GiB activation peak "
        "instead of 2.47 GiB")


def test_resolve_backfills_the_default_without_aliasing_it(mi):
    resolved = mi.resolve_engine_configuration({})
    assert resolved["limit_mm_per_prompt"] == {"image": 1, "video": 0}

    # Mutating the resolved copy must never reach the module default.
    resolved["limit_mm_per_prompt"]["image"] = 8
    resolved["limit_mm_per_prompt"]["video"] = 8
    assert mi.ENGINE_DEFAULTS["limit_mm_per_prompt"] == {"image": 1,
                                                        "video": 0}


def test_resolve_keeps_a_supplied_multimodal_limit(mi):
    resolved = mi.resolve_engine_configuration({
        "limit_mm_per_prompt": {"image": 2}})
    assert resolved["limit_mm_per_prompt"] == {"image": 2}
    # ...and the other settings still get their documented defaults.
    assert resolved["gpu_memory_utilization"] == 0.5
    assert resolved["dtype"] == "auto"


# ---------------------------------------------------------------------------
# Accepted: {"image": <int 1..8>}
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("images", [1, 2, 3, 4, 5, 6, 7, 8])
def test_accepts_image_counts_one_through_eight(mi, images):
    assert mi._validate_engine_setting(
        "limit_mm_per_prompt", {"image": images}) == ""


# ---------------------------------------------------------------------------
# Accepted: the optional "video" sub-key, 0..8 — the JP6-measured bound
# ---------------------------------------------------------------------------

def test_accepts_the_measured_jp6_configuration(mi):
    """`{"image": 1, "video": 0}` is the ONLY configuration measured to serve
    this model with real headroom (activation peak 2.47 GiB, KV 6.43 GiB,
    concurrency 29.41x, READY) — it must be authorable."""
    assert mi._validate_engine_setting(
        "limit_mm_per_prompt", {"image": 1, "video": 0}) == ""


@pytest.mark.parametrize("videos", [0, 1, 2, 3, 4, 5, 6, 7, 8])
def test_accepts_video_counts_zero_through_eight(mi, videos):
    """`video` accepts 0 — bounding the modality to nothing is the point."""
    assert mi._validate_engine_setting(
        "limit_mm_per_prompt", {"video": videos}) == "", (
        f'video={videos} must be accepted on its own')
    assert mi._validate_engine_setting(
        "limit_mm_per_prompt", {"image": 2, "video": videos}) == "", (
        f'image=2 with video={videos} must be accepted')


def test_accepts_a_bounded_video_without_an_image_key(mi):
    """`video` alone is accepted: both sub-keys are optional."""
    assert mi._validate_engine_setting(
        "limit_mm_per_prompt", {"video": 0}) == ""
    assert mi._validate_engine_setting(
        "limit_mm_per_prompt", {"video": 2}) == ""


def test_image_range_still_excludes_zero_while_video_admits_it(mi):
    """The two sub-keys keep DIFFERENT ranges: an image-less vision-language
    model is not a configuration this portal authors, a video-less one is."""
    assert mi._validate_engine_setting(
        "limit_mm_per_prompt", {"image": 0}) != ""
    assert mi._validate_engine_setting(
        "limit_mm_per_prompt", {"video": 0}) == ""


# ---------------------------------------------------------------------------
# Rejected, each with its per-field reason
# ---------------------------------------------------------------------------

# (value, distinguishing substring the reason must carry)
NON_DICT_VALUES = [
    (2, 'must be an object'),
    ("2", 'must be an object'),
    (1.0, 'must be an object'),
    (True, 'must be an object'),
    (False, 'must be an object'),
    (None, 'must be an object'),
    ([2], 'must be an object'),
    ([], 'must be an object'),
]

# Unknown sub-keys stay fail-closed. `{"image": 1, "video": 1}` and
# `{"video": 1}` USED to live here and are now ACCEPTED (the video widening —
# see the module docstring); `{}` is still rejected because a limit that
# bounds nothing is not a configuration.
EXTRA_KEY_VALUES = [
    ({"image": 2, "audio": 1}, 'the accepted keys are "image" and "video"'),
    ({"audio": 1}, 'the accepted keys are "image" and "video"'),
    ({"image": 1, "video": 0, "audio": 1},
     'the accepted keys are "image" and "video"'),
    ({}, 'at least one of "image", "video" is required'),
]

NON_INT_IMAGE_VALUES = [
    ({"image": True}, 'limit_mm_per_prompt.image must be an integer'),
    ({"image": False}, 'limit_mm_per_prompt.image must be an integer'),
    ({"image": 1.5}, 'limit_mm_per_prompt.image must be an integer'),
    ({"image": 2.0}, 'limit_mm_per_prompt.image must be an integer'),
    ({"image": "2"}, 'limit_mm_per_prompt.image must be an integer'),
    ({"image": None}, 'limit_mm_per_prompt.image must be an integer'),
    ({"image": [2]}, 'limit_mm_per_prompt.image must be an integer'),
]

OUT_OF_RANGE_IMAGE_VALUES = [
    ({"image": 0}, 'limit_mm_per_prompt.image must be an integer'),
    ({"image": -1}, 'limit_mm_per_prompt.image must be an integer'),
    ({"image": 9}, 'limit_mm_per_prompt.image must be an integer'),
    ({"image": 1024}, 'limit_mm_per_prompt.image must be an integer'),
]

REJECTED_VALUES = (NON_DICT_VALUES + EXTRA_KEY_VALUES
                   + NON_INT_IMAGE_VALUES + OUT_OF_RANGE_IMAGE_VALUES)

# `video` has its OWN range (0..8), so its per-sub-key reason quotes 0..8 and
# these cases are asserted separately from REJECTED_VALUES above.
REJECTED_VIDEO_VALUES = [
    {"video": -1},
    {"video": 9},
    {"video": 1024},
    {"video": True},
    {"video": False},
    {"video": 1.5},
    {"video": 0.0},
    {"video": "0"},
    {"video": None},
    {"video": [0]},
    {"image": 1, "video": -1},
    {"image": 1, "video": 9},
]


@pytest.mark.parametrize("value,expected_substring", REJECTED_VALUES)
def test_rejects_with_a_reason(mi, value, expected_substring):
    reason = mi._validate_engine_setting("limit_mm_per_prompt", value)
    assert reason, f"{value!r} must be rejected"
    assert "limit_mm_per_prompt" in reason, (
        f"the reason must name the field, got {reason!r}")
    assert expected_substring in reason, (
        f"{value!r}: expected a reason carrying {expected_substring!r}, got "
        f"{reason!r}")
    # Every reason states the accepted range so the operator can fix it.
    assert "1..8" in reason, (
        f"{value!r}: the reason must state the accepted range, got "
        f"{reason!r}")


@pytest.mark.parametrize("value", REJECTED_VIDEO_VALUES)
def test_rejects_out_of_range_and_non_integer_video(mi, value):
    reason = mi._validate_engine_setting("limit_mm_per_prompt", value)
    assert reason, f"{value!r} must be rejected"
    assert "limit_mm_per_prompt.video must be an integer" in reason, (
        f"{value!r}: the reason must name the offending sub-key, got "
        f"{reason!r}")
    assert "0..8" in reason, (
        f"{value!r}: the reason must state video's accepted range, got "
        f"{reason!r}")


def test_bool_is_rejected_even_though_it_is_an_int_subclass(mi):
    """`True` would pass a naive isinstance(int) check and would then be
    passed to the engine as `limit_mm_per_prompt={'image': True}`."""
    assert mi._validate_engine_setting(
        "limit_mm_per_prompt", {"image": True}) != ""
    assert mi._validate_engine_setting(
        "limit_mm_per_prompt", {"image": 1}) == ""


# ---------------------------------------------------------------------------
# The reason surfaces as a per-field finding (3.3)
# ---------------------------------------------------------------------------

def _registration_body(engine_configuration):
    return {
        "model_name": "multimodal-llm",
        "model_version": "1.0",
        "usecase_id": "uc-1",
        "huggingface_model_id": "example/multimodal-llm",
        "engine_configuration": engine_configuration,
    }


def test_invalid_multimodal_limit_is_a_per_field_finding(mi):
    findings = mi.validate_vllm_registration(
        _registration_body({"limit_mm_per_prompt": {"image": 9}}))

    matching = [f for f in findings
                if f["field"] == "engine_configuration.limit_mm_per_prompt"]
    assert len(matching) == 1, (
        f"expected exactly one finding for the multimodal limit, got "
        f"{findings!r}")
    finding = matching[0]
    assert finding["value"] == {"image": 9}
    assert finding["reason"]


def test_valid_two_image_limit_produces_no_finding(mi):
    """The two-image reference capability is preserved, not removed (3.9)."""
    findings = mi.validate_vllm_registration(
        _registration_body({"limit_mm_per_prompt": {"image": 2}}))
    assert findings == [], f"expected no findings, got {findings!r}"


def test_measured_jp6_configuration_produces_no_finding(mi):
    """`{"image": 1, "video": 0}` must be authorable end-to-end: it is the
    only configuration measured to serve with real headroom."""
    findings = mi.validate_vllm_registration(
        _registration_body({"limit_mm_per_prompt": {"image": 1, "video": 0},
                            "gpu_memory_utilization": 0.55}))
    assert findings == [], f"expected no findings, got {findings!r}"


def test_unknown_multimodal_sub_key_is_a_per_field_finding(mi):
    """Fail-closed posture preserved for sub-keys: only image and video."""
    value = {"image": 1, "video": 0, "audio": 1}
    findings = mi.validate_vllm_registration(
        _registration_body({"limit_mm_per_prompt": value}))

    matching = [f for f in findings
                if f["field"] == "engine_configuration.limit_mm_per_prompt"]
    assert len(matching) == 1, (
        f"expected exactly one finding for the multimodal limit, got "
        f"{findings!r}")
    finding = matching[0]
    assert finding["value"] == value
    assert 'the accepted keys are "image" and "video"' in finding["reason"]
    assert "audio" in finding["reason"], (
        f"the reason must name the rejected sub-key, got "
        f"{finding['reason']!r}")


def test_out_of_range_video_is_a_per_field_finding(mi):
    findings = mi.validate_vllm_registration(
        _registration_body({"limit_mm_per_prompt": {"image": 1,
                                                    "video": 9}}))
    matching = [f for f in findings
                if f["field"] == "engine_configuration.limit_mm_per_prompt"]
    assert len(matching) == 1, findings
    assert "limit_mm_per_prompt.video" in matching[0]["reason"]


def test_unknown_engine_keys_stay_fail_closed(mi):
    """The fail-closed unknown-key rule is untouched by the new field."""
    findings = mi.validate_vllm_registration(
        _registration_body({"limit_mm_per_prompt_image": 2}))
    fields = [f["field"] for f in findings]
    assert "engine_configuration.limit_mm_per_prompt_image" in fields
    reason = next(f["reason"] for f in findings
                  if f["field"]
                  == "engine_configuration.limit_mm_per_prompt_image")
    assert "unknown engine setting" in reason


# ---------------------------------------------------------------------------
# The engine-spec endpoint (both frontend forms are schema-driven off it)
# ---------------------------------------------------------------------------

def test_engine_spec_advertises_the_multimodal_limit(mi):
    response = mi.get_vllm_engine_spec({}, None)
    assert response["statusCode"] == 200
    settings = json.loads(response["body"])["settings"]

    assert "limit_mm_per_prompt" in settings, (
        "the settings endpoint must advertise the new field — both frontend "
        "forms render from it")
    field = settings["limit_mm_per_prompt"]

    assert field["default"] == {"image": 1, "video": 0}
    assert field["type"] == "object"
    assert "1..8" in field["range"], (
        f"the accepted range must be stated, got {field['range']!r}")

    # The video bound is advertised too, with its own range starting at 0 —
    # the forms are schema-driven, so an unadvertised key is unauthorable.
    assert "0..8" in field["range"], (
        f"video's accepted range must be stated, got {field['range']!r}")
    assert field["accepted_keys"] == ["image", "video"], (
        f"both sub-keys must be advertised, got {field['accepted_keys']!r}")
    assert '"video": 0' in field["description"], (
        "the description must state that a model only ever asked for images "
        f"should bound video to 0, got {field['description']!r}")

    description = field["description"].lower()
    assert "profiling peak" in description, (
        "the description must state that raising the limit increases the "
        f"engine's profiling peak, got {field['description']!r}")
    assert "6.6" in field["description"] and "image" in description, (
        "the description must point at the two-image reference-generation "
        f"requirement, got {field['description']!r}")
    assert '"image": 2' in field["description"], (
        "the description must state that two-image reference generation "
        f"requires image: 2, got {field['description']!r}")

    # The five pre-existing settings are still advertised (3.3).
    for key in ("dtype", "gpu_memory_utilization", "max_model_len",
                "tensor_parallel_size", "enforce_eager"):
        assert key in settings


# ---------------------------------------------------------------------------
# Verbatim propagation: the conversions recurse into the nested map
# ---------------------------------------------------------------------------

def test_dynamo_conversions_round_trip_the_nested_map_verbatim(mi):
    resolved = mi.resolve_engine_configuration(
        {"limit_mm_per_prompt": {"image": 2}, "gpu_memory_utilization": 0.4})

    ddb = mi._to_dynamo_compatible(resolved)
    assert ddb["limit_mm_per_prompt"] == {"image": 2}
    assert isinstance(ddb["gpu_memory_utilization"], Decimal)

    # As DynamoDB hands the nested integer back.
    from_ddb = dict(ddb)
    from_ddb["limit_mm_per_prompt"] = {"image": Decimal("2")}
    native = mi._decimal_to_native(from_ddb)
    assert native["limit_mm_per_prompt"] == {"image": 2}
    assert isinstance(native["limit_mm_per_prompt"]["image"], int)
    assert native["gpu_memory_utilization"] == 0.4

    # ...and the packaged model.json therefore carries the field verbatim.
    assert json.loads(json.dumps(native))["limit_mm_per_prompt"] == {
        "image": 2}


# ---------------------------------------------------------------------------
# Task 4.3 — Property 4 (portal half): the authored multimodal limit is
# staged verbatim and is the term the Fit_Check sizes
# ---------------------------------------------------------------------------

_PACKAGING_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "functions", "packaging.py")


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Load functions/packaging.py under a distinct module name inside the
    moto mock (its file name collides with the PyPI `packaging`
    distribution, and its module-level boto3 clients must bind inside the
    mock) — the `test_property_vllm_packaging_preservation.py` pattern."""
    spec = importlib.util.spec_from_file_location(
        "portal_packaging_multimodal", _PACKAGING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_packaging_multimodal"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("portal_packaging_multimodal", None)


# The stored (DynamoDB) representation of every authored engine setting:
# numbers come back as Decimal — exactly the shape the packaging path is
# handed. Values stay inside each setting's accepted range (these are
# AUTHORED configurations: validation admitted them).
_STORED_ENGINE_VALUE_STRATEGIES = {
    "dtype": st.sampled_from(("auto", "float16", "bfloat16", "float32")),
    "gpu_memory_utilization": st.floats(
        min_value=0.05, max_value=1.0,
        allow_nan=False, allow_infinity=False,
    ).map(lambda x: Decimal(str(round(x, 6)))),
    "max_model_len": st.integers(min_value=256, max_value=32768).map(Decimal),
    "tensor_parallel_size": st.integers(min_value=1, max_value=8).map(Decimal),
    "enforce_eager": st.booleans(),
}


@st.composite
def authored_configurations(draw):
    """(config, images, videos) — a generated staged-args dictionary in the
    stored DynamoDB shape: a non-empty subset of the five pre-existing
    settings, plus — explicitly drawn so presence/absence is part of every
    example — an OPTIONAL authored ``limit_mm_per_prompt`` with an optional
    ``image`` (1..8) and an optional ``video`` (0..8). ``images`` / ``videos``
    are None when the configuration does not author that sub-key; the limit
    itself is absent when neither is drawn."""
    keys = draw(st.sets(
        st.sampled_from(sorted(_STORED_ENGINE_VALUE_STRATEGIES)),
        min_size=1))
    config = {key: draw(_STORED_ENGINE_VALUE_STRATEGIES[key])
              for key in sorted(keys)}
    images = draw(st.one_of(st.none(),
                            st.integers(min_value=1, max_value=8)))
    videos = draw(st.one_of(st.none(),
                            st.integers(min_value=0, max_value=8)))
    limit = {}
    if images is not None:
        limit["image"] = Decimal(images)
    if videos is not None:
        limit["video"] = Decimal(videos)
    if limit:
        config["limit_mm_per_prompt"] = limit
    return config, images, videos


# Validates: Requirements 2.4, 3.9
@settings(deadline=None)
@given(case=authored_configurations())
def test_property_staged_model_json_is_the_authored_configuration_verbatim(
        packaging, case):
    """**Property 4: Bug Condition — the multimodal limit is authored and
    budgeted** (portal half). For any generated staged-args dictionary:

    - `generate_vllm_repository`'s `model.json` (inspected as JSON) carries
      EXACTLY the authored keys plus the documented `model` reference —
      no key is ever injected that the authored configuration did not
      contain (the device-side `{"image": 2}` default was invisible to
      every sizing surface precisely because it never appeared here);
    - the authored `limit_mm_per_prompt` value reaches `model.json`
      verbatim (`{"image": N}` as a JSON integer), and the key is ABSENT
      whenever the configuration did not author it;
    - that authored value is exactly the value
      `vllm_fit_check.images_per_prompt` feeds the Fit_Check — computed
      identically from the authored configuration (what publish sizes)
      and from the staged `model.json` (what the device loads).

    # Validates: Requirements 2.4, 3.9
    """
    config, images, videos = case
    record = {
        "training_id": "prop-4-multimodal",
        "model_name": "multimodal-llm",
        "model_type": "vllm",
        "source": "vllm",
        "model_source": {"huggingface_model_id": "example/multimodal-llm"},
        "engine_configuration": config,
    }

    files = packaging.generate_vllm_repository(record)

    model_json_paths = [p for p in files if p.endswith("/1/model.json")]
    assert len(model_json_paths) == 1, sorted(files)
    model_json = json.loads(files[model_json_paths[0]])

    # No key is ever injected that the authored configuration did not
    # contain (`model` is the documented reference key, not an engine
    # setting).
    assert set(model_json) == set(config) | {"model"}, (
        "model.json keys {} must be exactly the authored settings {} plus "
        "'model'".format(sorted(model_json), sorted(config)))

    # The authored multimodal limit is staged verbatim — or absent. Both
    # sub-keys propagate: a `video` bound is worthless if packaging drops it.
    expected_limit = {}
    if images is not None:
        expected_limit["image"] = images
    if videos is not None:
        expected_limit["video"] = videos

    if not expected_limit:
        assert "limit_mm_per_prompt" not in model_json, (
            "a limit_mm_per_prompt the configuration never authored was "
            "injected into model.json: {!r}".format(
                model_json.get("limit_mm_per_prompt")))
    else:
        staged = model_json["limit_mm_per_prompt"]
        assert staged == expected_limit, (
            "the authored limit was rewritten: authored {!r} -> staged "
            "{!r}".format(expected_limit, staged))
        for sub_key, count in staged.items():
            assert isinstance(count, int) and not isinstance(count, bool), (
                "the staged {} count must be a JSON integer, got {!r}".format(
                    sub_key, count))

    # The authored value is the multimodal term the Fit_Check sizes —
    # identically from the record's configuration and the staged JSON.
    expected = DEFAULT_IMAGES_PER_PROMPT if images is None else images
    assert images_per_prompt(config) == expected, (
        "images_per_prompt over the authored configuration disagrees with "
        "the authored value")
    assert images_per_prompt(model_json) == expected, (
        "images_per_prompt over the staged model.json disagrees with the "
        "authored value")


# ---------------------------------------------------------------------------
# The measured JP6 configuration, end to end: authored -> stored -> packaged
# ---------------------------------------------------------------------------

def test_measured_configuration_reaches_model_json_verbatim(mi, packaging):
    """`{"image": 1, "video": 0}` authored at `gpu_memory_utilization = 0.55`
    must land in the packaged `model.json` byte-for-byte — the bound is
    worthless if resolution, the DynamoDB round trip or packaging drops or
    rewrites it.

    Measured on `ryanorinagxdevkithomelabjp622` (LocalServer.arm64JP6 1.0.62):
    with the video bound the engine reports activation peak 2.47 GiB and
    6.43 GiB of KV cache at 29.41x concurrency and reaches READY; without it,
    activation peak 4.93 GiB, 0.20 GiB of KV, and the load FAILS.
    """
    authored = {"gpu_memory_utilization": 0.55,
                "max_model_len": 4096,
                "limit_mm_per_prompt": {"image": 1, "video": 0}}

    assert mi.validate_vllm_registration(_registration_body(authored)) == []

    resolved = mi.resolve_engine_configuration(authored)
    assert resolved["limit_mm_per_prompt"] == {"image": 1, "video": 0}

    # Stored the way the handler stores it, read back the way DynamoDB
    # returns it.
    stored = mi._to_dynamo_compatible(resolved)
    assert stored["limit_mm_per_prompt"] == {"image": 1, "video": 0}

    from_ddb = dict(stored)
    from_ddb["limit_mm_per_prompt"] = {"image": Decimal("1"),
                                       "video": Decimal("0")}

    files = packaging.generate_vllm_repository({
        "training_id": "measured-jp6-configuration",
        "model_name": "qwen2-5-vl-7b-instruct-awq",
        "model_type": "vllm",
        "source": "vllm",
        "model_source": {
            "huggingface_model_id": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ"},
        "engine_configuration": from_ddb,
    })

    model_json = json.loads(
        files["qwen2-5-vl-7b-instruct-awq/1/model.json"])
    staged = model_json["limit_mm_per_prompt"]
    assert staged == {"image": 1, "video": 0}, (
        f"the measured bound was rewritten on the way to model.json: "
        f"{staged!r}")
    assert isinstance(staged["video"], int) and \
        not isinstance(staged["video"], bool), (
        f"the video bound must be a JSON integer 0, got {staged['video']!r}")
    assert '"video": 0' in files[
        "qwen2-5-vl-7b-instruct-awq/1/model.json"], (
        "the serialized model.json must carry the video bound literally")

    # The image term the Fit_Check sizes is untouched by the video bound.
    assert images_per_prompt(model_json) == 1
