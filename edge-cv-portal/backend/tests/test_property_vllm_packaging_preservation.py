"""Property test pinning the vLLM packaging path's engine-configuration
preservation (vllm-sizing-and-packaging-errors, task 2.8).

**Feature: vllm-sizing-and-packaging-errors, Property 1: Packaging preserves
the stored engine configuration**

_For any_ vLLM_Model_Record with a resolved Engine_Configuration, the
`model.json` emitted by `packaging.generate_vllm_repository` contains
exactly the record's engine settings (numerically equal after Decimal
conversion) plus the `model` reference key, and nothing else.

**Validates: Requirements 1.1**

This is a regression pin on the existing, investigation-verified-correct
path (import -> package -> publish -> device model.json): packaging copies
the stored record verbatim (Decimal -> native number) into the on-device
model.json and injects no defaults and no mutations.

`generate_vllm_repository` is pure over the record, so it is exercised
directly with no AWS calls. functions/packaging.py is loaded under a
distinct module name inside the moto-backed session fixture (its file name
collides with the PyPI `packaging` distribution, and its module-level boto3
clients must bind inside the mock) — the dispatch-test pattern.
"""
import importlib.util
import json
import os
import sys
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

_PACKAGING_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "functions", "packaging.py")

KNOWN_ENGINE_KEYS = ("dtype", "gpu_memory_utilization", "max_model_len",
                     "tensor_parallel_size", "enforce_eager")


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Load functions/packaging.py under a distinct module name inside the
    moto mock (PyPI `packaging` collision + module-level boto3 clients)."""
    spec = importlib.util.spec_from_file_location(
        "portal_packaging_preservation", _PACKAGING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_packaging_preservation"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Generators: stored records as DynamoDB returns them (all numbers Decimal)
# ---------------------------------------------------------------------------

_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_HF_TAIL = _ALNUM + "._-"
_NAME_ALPHABET = _ALNUM + " ._-"

_gpu_memory_utilization = st.floats(
    min_value=1e-6, max_value=1.0, allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 6)).filter(lambda x: 0.0 < x <= 1.0)

# The stored (DynamoDB) representation: every number comes back as Decimal.
STORED_ENGINE_VALUE_STRATEGIES = {
    "dtype": st.sampled_from(("auto", "float16", "bfloat16", "float32")),
    "gpu_memory_utilization": _gpu_memory_utilization.map(
        lambda x: Decimal(str(x))),
    "max_model_len": st.integers(min_value=1, max_value=131072).map(Decimal),
    "tensor_parallel_size": st.integers(min_value=1, max_value=8).map(Decimal),
    "enforce_eager": st.booleans(),
}

stored_engine_configurations = st.fixed_dictionaries(
    {key: STORED_ENGINE_VALUE_STRATEGIES[key] for key in KNOWN_ENGINE_KEYS})

model_names = st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=30)

huggingface_model_ids = st.builds(
    lambda head, tail, name: f"{head}{tail}/{name}",
    st.text(alphabet=_ALNUM, min_size=1, max_size=1),
    st.text(alphabet=_HF_TAIL, min_size=0, max_size=20),
    st.text(alphabet=_HF_TAIL, min_size=1, max_size=24),
)

s3_artifacts = st.text(
    alphabet=_ALNUM + "-_/", min_size=1, max_size=30,
).filter(lambda k: not k.startswith("/") and "//" not in k).map(
    lambda k: f"s3://test-preservation-bucket/{k}.tar.gz")


@st.composite
def vllm_records(draw):
    """A stored vLLM_Model_Record (DynamoDB shape) with a resolved
    Engine_Configuration and exactly one source reference."""
    if draw(st.booleans()):
        model_source = {"huggingface_model_id": draw(huggingface_model_ids)}
    else:
        model_source = {"s3_model_artifact": draw(s3_artifacts)}
    return {
        "training_id": "prop-preservation",
        "model_name": draw(model_names),
        "model_type": "vllm",
        "source": "vllm",
        "model_source": model_source,
        "engine_configuration": draw(stored_engine_configurations),
    }


# ---------------------------------------------------------------------------
# Property 1: Packaging preserves the stored engine configuration
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(record=vllm_records())
def test_model_json_carries_exactly_the_stored_configuration(
        packaging, record):
    """**Feature: vllm-sizing-and-packaging-errors, Property 1: Packaging
    preserves the stored engine configuration**

    For any stored vLLM engine configuration, the emitted model.json
    carries exactly the record's engine settings (numerically equal after
    Decimal conversion) plus the `model` reference key, and nothing else
    (Requirement 1.1)."""
    stored = record["engine_configuration"]

    files = packaging.generate_vllm_repository(record)

    # Exactly the two repository files, sharing one directory prefix.
    assert len(files) == 2, f"expected exactly two files, got {sorted(files)}"
    model_json_path = [p for p in files if p.endswith("/1/model.json")]
    config_paths = [p for p in files if p.endswith("/config.pbtxt")]
    assert len(model_json_path) == 1 and len(config_paths) == 1, (
        f"expected {{name}}/1/model.json and {{name}}/config.pbtxt, got "
        f"{sorted(files)}")
    assert model_json_path[0].split("/", 1)[0] == \
        config_paths[0].split("/", 1)[0]

    model_json = json.loads(files[model_json_path[0]])

    # Exactly the stored settings plus the model reference — nothing else,
    # no injected defaults, no dropped settings.
    assert set(model_json) == set(stored) | {"model"}, (
        f"model.json keys {sorted(model_json)} must be exactly the stored "
        f"settings {sorted(stored)} plus 'model'")

    # Every stored setting verbatim (numerically equal after the
    # Decimal -> JSON-number conversion).
    for key, value in stored.items():
        emitted = model_json[key]
        if isinstance(value, bool):
            assert emitted is value, (
                f"{key}: emitted {emitted!r} must equal stored {value!r}")
        elif isinstance(value, Decimal):
            assert isinstance(emitted, (int, float)) and \
                not isinstance(emitted, bool), (
                    f"{key}: emitted {emitted!r} must be a JSON number")
            assert Decimal(str(emitted)) == value, (
                f"{key}: emitted {emitted!r} must equal stored {value!r}")
        else:
            assert emitted == value, (
                f"{key}: emitted {emitted!r} must equal stored {value!r}")

    # The model reference: the HF ID for HF-sourced records, the
    # repository-relative weights sentinel for S3-sourced records.
    model_source = record["model_source"]
    if model_source.get("huggingface_model_id"):
        assert model_json["model"] == model_source["huggingface_model_id"]
    else:
        assert model_json["model"] == packaging.VLLM_S3_MODEL_SENTINEL
