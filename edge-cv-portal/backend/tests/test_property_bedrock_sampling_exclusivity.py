"""Property test for sampling parameter exclusivity
(custom-node-code-assist, task 1.4).

**Feature: custom-node-code-assist, Property 11: Sampling parameter
exclusivity**

_For any_ combination of `temperature` and `top_p` values (each set or
unset, where unset means the key is absent from the resolved configuration
or holds an explicit None), `bedrock_common.build_inference_config` SHALL
emit at most one sampling parameter - `temperature` when it is set, else
`topP` when it is set - and SHALL omit any parameter that is unset. The
result always carries `maxTokens` as an int and nothing else.

**Validates: Requirements 4.2, 4.3**

`build_inference_config` is a pure function over an already-resolved
Bedrock_Configuration dict (Decimal values have been converted to native
int/float by `get_bedrock_configuration`), so this test needs no moto
stack - it drives the function directly with native values.
"""
import pytest
from hypothesis import given
from hypothesis import strategies as st

import bedrock_common

# Sentinel for "key absent from the resolved configuration" - distinct
# from an explicit None, though both mean the parameter is unset
# (Requirement 4.2).
UNSET = object()

# A set sampling parameter: any int or float in [0, 1] (the values the
# settings API accepts; 0 is set, not falsy-unset).
set_sampling_values = st.one_of(
    st.integers(min_value=0, max_value=1),
    st.floats(min_value=0, max_value=1,
              allow_nan=False, allow_infinity=False),
)

# Each sampling parameter is independently: absent, explicit None, or set.
sampling_states = st.one_of(
    st.just(UNSET), st.none(), set_sampling_values)

max_tokens_values = st.one_of(
    st.integers(min_value=1, max_value=100000),
    st.floats(min_value=1, max_value=100000,
              allow_nan=False, allow_infinity=False),
)


@st.composite
def resolved_configs(draw):
    """A resolved Bedrock_Configuration dict with every set/unset
    combination of the two sampling parameters."""
    config = {
        'model_id': 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
        'region': 'us-east-1',
        'max_tokens': draw(max_tokens_values),
        'timeout_seconds': draw(st.integers(min_value=1, max_value=60)),
    }
    for key in ('temperature', 'top_p'):
        state = draw(sampling_states)
        if state is not UNSET:
            config[key] = state
    return config


@given(config=resolved_configs())
def test_sampling_parameter_exclusivity(config):
    """For any set/unset combination of temperature and top_p, the
    inferenceConfig contains at most one sampling parameter - temperature
    when set, else topP when set - and omits unset parameters
    (Requirements 4.2, 4.3)."""
    result = bedrock_common.build_inference_config(config)

    temperature = config.get('temperature')
    top_p = config.get('top_p')

    # At most one sampling parameter is ever emitted (Requirement 4.3).
    emitted = {'temperature', 'topP'} & set(result)
    assert len(emitted) <= 1, (
        f"at most one sampling parameter may be emitted, got {result!r}")

    if temperature is not None:
        # Temperature wins whenever set - even when top_p is also set.
        assert set(result) == {'maxTokens', 'temperature'}, (
            f"set temperature must be emitted alone, got {result!r}")
        assert result['temperature'] == pytest.approx(float(temperature))
    elif top_p is not None:
        # topP is sent only when temperature is unset (absent or None).
        assert set(result) == {'maxTokens', 'topP'}, (
            f"set top_p with unset temperature must emit topP alone, "
            f"got {result!r}")
        assert result['topP'] == pytest.approx(float(top_p))
    else:
        # Both unset: no sampling parameter at all (Requirement 4.2).
        assert set(result) == {'maxTokens'}, (
            f"unset sampling parameters must be omitted, got {result!r}")

    assert isinstance(result['maxTokens'], int)
    assert result['maxTokens'] == int(config['max_tokens'])
