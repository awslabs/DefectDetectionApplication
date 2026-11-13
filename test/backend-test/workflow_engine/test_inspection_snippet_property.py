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
"""Snippet transport (AC-6a, device lane): rendering an inspection prompt
from ``json.loads`` of the exported ``metadataSnippetJson`` string equals
rendering it from the full run metadata.

The property runs the real ``inspection_metadata_snippet_json`` (the
projection, the masking, the omission and the encoding) on generated
JSON-native trigger payloads. Every generated string comes from an
alphabet without ``x`` and ``X``: the device's signed-query mask is
case-insensitive, so only that alphabet guarantees that no value forms
an ``X-Amz-`` parameter, which masking changes by design.

At c299a6e1 the snippet builder does not exist: both tests fail with an
``ImportError`` naming it.
"""
import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import render_prompt_text

#: Text without x or X (and without lone surrogates, which JSON cannot
#: carry as UTF-8).
TEXT = st.text(
    alphabet=st.characters(blacklist_characters="xX",
                           blacklist_categories=("Cs",)),
    max_size=12)
#: A payload key a ``{trigger.payload_json.<key>}`` placeholder can name.
KEY = st.from_regex(r"[a-wyzA-WYZ_][a-wyzA-WYZ0-9_]{0,7}", fullmatch=True)
LEAF = st.one_of(
    st.none(), st.booleans(),
    st.integers(min_value=-10 ** 12, max_value=10 ** 12),
    st.floats(allow_nan=False, allow_infinity=False), TEXT)
JSON_VALUE = st.recursive(
    LEAF,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(TEXT, children, max_size=3)),
    max_leaves=12)
PAYLOAD = st.dictionaries(KEY, JSON_VALUE, min_size=1, max_size=5)


def metadata_of(payload):
    return {
        "trigger": {"topic": "plant/line1/evaluate",
                    "payload": json.dumps(payload), "payload_json": payload,
                    "qos": 1},
        "detections": [{"id": "0a1b2c3d", "label": "part"}],
    }


@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(payload=PAYLOAD, data=st.data())
def test_rendering_from_the_snippet_equals_rendering_from_the_metadata(
        payload, data):
    from workflow_engine.tuning.sample_export import (
        inspection_metadata_snippet_json,
    )
    key = data.draw(st.sampled_from(sorted(payload)))
    prompt = ("All: {json:trigger.payload_json}. One: "
              "{trigger.payload_json." + key + "}. As JSON: "
              "{json:trigger.payload_json." + key + "}.")
    parameters = {"prompt": prompt, "render_prompt": True,
                  "answer_format": "inspection"}
    metadata = metadata_of(payload)

    snippet = inspection_metadata_snippet_json(parameters, metadata)

    assert isinstance(snippet, str)
    values = json.loads(snippet)
    assert set(values) == {"trigger"}
    assert render_prompt_text(prompt, values) == render_prompt_text(
        prompt, metadata)


def test_unsorted_keys_and_an_integral_float_survive_the_round_trip():
    """The golden's case: order items whose keys are not sorted and an
    integral float the prompt renders."""
    from workflow_engine.tuning.sample_export import (
        inspection_metadata_snippet_json,
    )
    payload = {"order_id": "o-1", "body": {"scale": 2.0, "order": {
        "items": [{"shape": "roof", "color": "red"}]}}}
    prompt = ("Order {trigger.payload_json.order_id} at scale "
              "{trigger.payload_json.body.scale}: "
              "{json:trigger.payload_json.body.order.items}")
    snippet = inspection_metadata_snippet_json(
        {"prompt": prompt, "render_prompt": True}, metadata_of(payload))
    # Re-serialized with sorted keys, as the uploader does for the sidecar.
    carried = json.loads(json.loads(json.dumps(
        {"metadataSnippetJson": snippet}, sort_keys=True))[
            "metadataSnippetJson"])
    assert render_prompt_text(prompt, carried) == (
        'Order o-1 at scale 2.0: [{"shape":"roof","color":"red"}]')
