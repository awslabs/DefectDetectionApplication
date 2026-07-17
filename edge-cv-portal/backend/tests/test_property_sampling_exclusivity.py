"""Property test for the shared Bedrock inference-config builder
(custom-node-code-assist, task 1.4).

**Feature: custom-node-code-assist, Property 11: Sampling parameter
exclusivity**

*For any* combination of `temperature` and `top_p` values (each set or
unset, where unset means absent from the resolved configuration or
present as None), `build_inference_config` emits at most one sampling
parameter: `temperature` when it is set, else `topP` when it is set,
and omits any parameter that is unset.

**Validates: Requirements 4.2, 4.3**

`build_inference_config` is a pure function over a resolved
Bedrock_Configuration dict (bedrock_common.py, shared by workflow
generation and code assist), so no AWS stack is required; conftest.py
puts backend/functions on sys.path and pins fake AWS credentials for
bedrock_common's module-level boto3 resource.
"""
import math

from hypothesis import given
from hypothesis import strategies as st

from bedrock_common import build_inference_config

# A sampling parameter as it appears in a resolved configuration:
# unset as "absent from the dict", unset as an explicit None (both arise
# from get_bedrock_configuration), or set to a number (int or float --
# build_inference_config coerces through float()).
_ABSENT = object()

sampling_values = st.one_of(
    st.just(_ABSENT),
    st.none(),
    st.floats(min_value=0, max_value=2,
              allow_nan=False, allow_infinity=False),
    st.integers(min_value=0, max_value=2),
)


@st.composite
def resolved_configs(draw):
    """A resolved Bedrock_Configuration covering every set/unset
    combination of the two sampling parameters."""
    config = {"max_tokens": draw(st.integers(min_value=1, max_value=200_000))}
    temperature = draw(sampling_values)
    top_p = draw(sampling_values)
    if temperature is not _ABSENT:
        config["temperature"] = temperature
    if top_p is not _ABSENT:
        config["top_p"] = top_p
    return config


def _is_set(config, key):
    return config.get(key) is not None


@given(config=resolved_configs())
def test_at_most_one_sampling_parameter_temperature_wins(config):
    """temperature when set, else topP when set; unset parameters are
    omitted; nothing but maxTokens and the winning sampling parameter is
    emitted (Requirements 4.2, 4.3)."""
    inference_config = build_inference_config(config)

    # maxTokens is always present; no other keys beyond the sampling pair.
    assert inference_config["maxTokens"] == int(config["max_tokens"])
    assert set(inference_config) <= {"maxTokens", "temperature", "topP"}

    # At most ONE sampling parameter, never both (Requirement 4.3).
    assert not ("temperature" in inference_config
                and "topP" in inference_config)

    if _is_set(config, "temperature"):
        # temperature wins whenever it is set, regardless of top_p.
        assert math.isclose(inference_config["temperature"],
                            float(config["temperature"]))
        assert "topP" not in inference_config
    elif _is_set(config, "top_p"):
        # topP is sent only when temperature is unset (Requirement 4.2/4.3).
        assert math.isclose(inference_config["topP"],
                            float(config["top_p"]))
        assert "temperature" not in inference_config
    else:
        # Both unset: both omitted (Requirement 4.2).
        assert "temperature" not in inference_config
        assert "topP" not in inference_config
