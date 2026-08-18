"""Unit tests for the authored multimodal engine setting
(jp6-vllm-kv-cache-oom-regression, task 3.1 — design Decision 1, File 3).

`limit_mm_per_prompt` becomes a first-class vLLM_Engine_Configuration
setting: default `{'image': 1}`, validated as a dict whose SOLE key is
`image` mapped to an integer in 1..8, stored on the record and propagated
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

def test_default_is_a_single_image(mi):
    """The authored default matches 1.0.59's effective demand: one image."""
    assert mi.ENGINE_DEFAULTS["limit_mm_per_prompt"] == {"image": 1}


def test_resolve_backfills_the_default_without_aliasing_it(mi):
    resolved = mi.resolve_engine_configuration({})
    assert resolved["limit_mm_per_prompt"] == {"image": 1}

    # Mutating the resolved copy must never reach the module default.
    resolved["limit_mm_per_prompt"]["image"] = 8
    assert mi.ENGINE_DEFAULTS["limit_mm_per_prompt"] == {"image": 1}


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

EXTRA_KEY_VALUES = [
    ({"image": 1, "video": 1}, 'the only accepted key is "image"'),
    ({"image": 2, "audio": 1}, 'the only accepted key is "image"'),
    ({}, 'the only accepted key is "image"'),
    ({"video": 1}, 'the only accepted key is "image"'),
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

    assert field["default"] == {"image": 1}
    assert field["type"] == "object"
    assert "1..8" in field["range"], (
        f"the accepted range must be stated, got {field['range']!r}")

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
    """(config, images) — a generated staged-args dictionary in the stored
    DynamoDB shape: a non-empty subset of the five pre-existing settings,
    plus — explicitly drawn so presence/absence is part of every example —
    an OPTIONAL authored ``limit_mm_per_prompt`` (``images`` is None when
    the configuration does not author the limit)."""
    keys = draw(st.sets(
        st.sampled_from(sorted(_STORED_ENGINE_VALUE_STRATEGIES)),
        min_size=1))
    config = {key: draw(_STORED_ENGINE_VALUE_STRATEGIES[key])
              for key in sorted(keys)}
    images = draw(st.one_of(st.none(),
                            st.integers(min_value=1, max_value=8)))
    if images is not None:
        config["limit_mm_per_prompt"] = {"image": Decimal(images)}
    return config, images


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
    config, images = case
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

    # The authored multimodal limit is staged verbatim — or absent.
    if images is None:
        assert "limit_mm_per_prompt" not in model_json, (
            "a limit_mm_per_prompt the configuration never authored was "
            "injected into model.json: {!r}".format(
                model_json.get("limit_mm_per_prompt")))
    else:
        staged = model_json["limit_mm_per_prompt"]
        assert staged == {"image": images}, (
            "the authored limit was rewritten: authored {{'image': {}}} -> "
            "staged {!r}".format(images, staged))
        assert isinstance(staged["image"], int) and \
            not isinstance(staged["image"], bool), (
            "the staged image count must be a JSON integer, got "
            "{!r}".format(staged["image"]))

    # The authored value is the multimodal term the Fit_Check sizes —
    # identically from the record's configuration and the staged JSON.
    expected = DEFAULT_IMAGES_PER_PROMPT if images is None else images
    assert images_per_prompt(config) == expected, (
        "images_per_prompt over the authored configuration disagrees with "
        "the authored value")
    assert images_per_prompt(model_json) == expected, (
        "images_per_prompt over the staged model.json disagrees with the "
        "authored value")
