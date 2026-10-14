"""Amazon request-body byte-preservation oracle (stability-generation-models,
task 1.1).

``_reference_build_image_request`` below is a FROZEN, VERBATIM copy of the
pre-change ``_build_image_request`` implementation from
``synthetic_data.py``, captured BEFORE task 2.3 relocated the function into
``synthetic_core.build_amazon_request_body``. It is the byte-preservation
reference oracle for Property 2 (Requirements 2.2, 2.4, 8.1), mirroring the
frozen-oracle pattern of ``test_property_bedrock_sampling_preservation.py``.

DO NOT EDIT the oracle: any change would silently invalidate the
preservation guarantee the property test provides.
"""
from typing import Any, Dict, Optional


def _reference_build_image_request(model_entry: Dict, method: str,
                                   prompt: str, source_b64: str, seed,
                                   params: Dict,
                                   mask_prompt: Optional[str]) -> Dict:
    """Bedrock invoke_model JSON body for one task (numberOfImages: 1)."""
    capabilities = model_entry.get('capabilities', {})
    config: Dict[str, Any] = {'numberOfImages': 1}
    if seed is not None and capabilities.get('seed'):
        config['seed'] = int(seed)
    cfg_scale = params.get('cfg_scale')
    if cfg_scale is None:
        cfg_scale = model_entry.get('randomization_defaults', {}).get(
            'cfg_scale')
    if cfg_scale is not None and capabilities.get('cfg_scale'):
        config['cfgScale'] = float(cfg_scale)

    if method == 'inpainting':
        return {
            'taskType': 'INPAINTING',
            'inPaintingParams': {
                'image': source_b64,
                'maskPrompt': mask_prompt or 'the defect region',
                'text': prompt,
            },
            'imageGenerationConfig': config,
        }
    return {
        'taskType': 'IMAGE_VARIATION',
        'imageVariationParams': {
            'images': [source_b64],
            'text': prompt,
        },
        'imageGenerationConfig': config,
    }


# ---------------------------------------------------------------------------
# Task 3.2
#
# **Feature: stability-generation-models, Property 2: Amazon request body
# byte-preservation**
#
# _For any_ model entry (capability flags and randomization defaults),
# generation method, prompt, source image base64 string, seed (including
# None), params (including cfg_scale present/absent), and mask prompt, the
# JSON serialization of build_amazon_request_body is byte-identical to the
# serialization produced by the pre-change _build_image_request
# implementation (frozen as the reference oracle above).
#
# **Validates: Requirements 2.2, 2.4, 8.1**
# ---------------------------------------------------------------------------
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_core import SEED_MODULUS, build_amazon_request_body


capability_flags = st.fixed_dictionaries(
    {},
    optional={
        "text_to_image": st.booleans(),
        "inpainting": st.booleans(),
        "image_variation": st.booleans(),
        "seed": st.booleans(),
        "cfg_scale": st.booleans(),
    },
)

randomization_defaults = st.fixed_dictionaries(
    {},
    optional={
        "seed": st.none(),
        "cfg_scale": st.one_of(
            st.none(),
            st.floats(min_value=1.0, max_value=10.0, allow_nan=False)),
    },
)

model_entries = st.fixed_dictionaries(
    {},
    optional={
        "model_id": st.sampled_from([
            "amazon.nova-canvas-v1:0",
            "amazon.titan-image-generator-v2:0",
        ]),
        "capabilities": capability_flags,
        "randomization_defaults": randomization_defaults,
    },
)

methods = st.sampled_from(["inpainting", "image_variation"])

prompts = st.text(max_size=200)

source_b64s = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
             "0123456789+/=",
    min_size=1, max_size=120)

seeds = st.one_of(st.none(),
                  st.integers(min_value=0, max_value=SEED_MODULUS - 1))

params_dicts = st.fixed_dictionaries(
    {},
    optional={
        "cfg_scale": st.one_of(
            st.none(),
            st.floats(min_value=1.0, max_value=10.0, allow_nan=False)),
    },
)

mask_prompts = st.one_of(st.none(), st.text(max_size=80))


@settings(deadline=None)
@given(model_entry=model_entries, method=methods, prompt=prompts,
       source_b64=source_b64s, seed=seeds, params=params_dicts,
       mask_prompt=mask_prompts)
def test_amazon_request_body_bytes_preserved(model_entry, method, prompt,
                                             source_b64, seed, params,
                                             mask_prompt):
    """json.dumps of build_amazon_request_body is byte-identical to the
    frozen pre-change implementation for every input combination
    (Requirements 2.2, 2.4, 8.1)."""
    reference = _reference_build_image_request(
        model_entry, method, prompt, source_b64, seed, params, mask_prompt)
    relocated = build_amazon_request_body(
        model_entry, method, prompt, source_b64, seed, params, mask_prompt)

    assert (json.dumps(relocated).encode()
            == json.dumps(reference).encode())
