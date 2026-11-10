# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""``mqtt_publish`` ``payload_format: json`` (bedrock-inspection-chain,
design 5.1): the structural renderer ``render_json_template`` (native types,
nesting, omission of unresolved keys and list elements, embedded text), the
three template functions (``{ulid}``, ``{now_iso}``, ``{latency_ms}``),
``ensure_ascii=False`` serialization, verbatim publish of a placeholder-free
template, and the ``text`` default left byte-identical.
"""
import json
import re
from datetime import datetime, timezone

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine import template_functions
from workflow_engine.output_bindings import (
    OutputBindingProcessor,
    render_json_template,
    render_template,
)

TRIGGER_TS = 1700000000.0
VALUES = {
    "is_anomalous": False,
    "confidence": 0.93,
    "trigger": {
        "topic": "plant/line1/evaluate",
        "timestamp": TRIGGER_TS,
        "payload_json": {"order_id": "o-1", "body": {"qc_stage": "qc2"}},
    },
    "bedrock": {"check": {"verdict": "pass", "confidence": 0.9,
                          "reason": "all parts placed", "objects": [1, 2],
                          "latency_ms": 1234}},
    "s3_upload": {"upload_raw": {"key": "images/o-1/u.jpg",
                                 "bytes": 10},
                  "upload_annotated": {
                      "key": "images/o-1/u.annotated.jpg",
                      "presigned_url": "https://example-bucket.s3.example.invalid/"
                                       "images/o-1/u.annotated.jpg?X-Amz-Signature=abc"}},
}


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def publish(template, values=None, **parameters):
    """Run one greengrass mqtt_publish binding; returns the payload text."""
    greengrass = Recorder()
    processor = OutputBindingProcessor(greengrass_publisher=greengrass)
    params = {"topic": "plant/line1/result", "qos": 1, "greengrass": True,
              "payload_template": template}
    params.update(parameters)
    document = {"schemaVersion": 1, "executorBindings": [{
        "nodeId": "publish", "binding": "mqtt_publish", "parameters": params,
        "upstreamNodeIds": [], "downstreamNodeIds": []}]}
    processor.process(None, document, dict(VALUES if values is None else values))
    ((topic, payload_text, qos), kwargs), = greengrass.calls
    assert (topic, qos, kwargs) == ("plant/line1/result", 1, {})
    return payload_text


# ---------------------------------------------------------------------------
# render_json_template
# ---------------------------------------------------------------------------

class TestRenderJsonTemplate:
    def test_whole_string_placeholders_keep_native_types(self):
        rendered = render_json_template({
            "number": "{bedrock.check.confidence}",
            "integer": "{bedrock.check.latency_ms}",
            "boolean": "{is_anomalous}",
            "object": "{bedrock.check}",
            "list": "{bedrock.check.objects}",
            "string": "{bedrock.check.verdict}",
        }, VALUES)
        assert rendered == {
            "number": 0.9, "integer": 1234, "boolean": False,
            "object": VALUES["bedrock"]["check"], "list": [1, 2],
            "string": "pass",
        }
        assert isinstance(rendered["number"], float)
        assert isinstance(rendered["boolean"], bool)

    def test_nested_objects_and_lists_recurse(self):
        rendered = render_json_template({
            "body": {"inner": {"verdict": "{bedrock.check.verdict}"},
                     "items": ["{bedrock.check.confidence}", 3, {"k": "{trigger.topic}"}]},
        }, VALUES)
        assert rendered == {"body": {"inner": {"verdict": "pass"},
                                     "items": [0.9, 3, {"k": "plant/line1/evaluate"}]}}

    def test_unresolved_whole_string_key_is_omitted(self):
        rendered = render_json_template(
            {"verdict": "{bedrock.check.verdict}", "error": "{bedrock.check.error}",
             "url": "{s3_upload.missing.presigned_url}"}, VALUES)
        assert rendered == {"verdict": "pass"}

    def test_unresolved_embedded_placeholder_omits_the_key(self):
        rendered = render_json_template(
            {"link": "https://screen.example.invalid/images/{s3_upload.missing.key}",
             "kept": "stage {trigger.payload_json.body.qc_stage}"}, VALUES)
        assert rendered == {"kept": "stage qc2"}

    def test_unresolved_list_elements_are_omitted(self):
        rendered = render_json_template(
            ["{bedrock.check.verdict}", "{nope}", "x-{nope.deeper}", 7, "{trigger.topic}"],
            VALUES)
        assert rendered == ["pass", 7, "plant/line1/evaluate"]

    def test_embedded_placeholders_render_as_text(self):
        rendered = render_json_template(
            {"text": "order {trigger.payload_json.order_id}: {bedrock.check.verdict} "
                     "({bedrock.check.confidence}, anomalous={is_anomalous})"}, VALUES)
        assert rendered == {"text": "order o-1: pass (0.9, anomalous=False)"}

    def test_other_scalars_pass_through(self):
        template = {"n": 1, "f": 2.5, "b": True, "z": None, "s": "plain text"}
        assert render_json_template(template, VALUES) == template

    def test_no_literal_braces_survive(self):
        rendered = render_json_template(
            {"a": "{nope}", "b": {"c": "{nope} and {trigger.topic}"}, "d": ["{nope}"]},
            VALUES)
        assert not re.search(r"\{[A-Za-z_.]+\}", json.dumps(rendered))
        assert rendered == {"b": {}, "d": []}

    def test_substituted_values_are_not_rescanned(self):
        values = dict(VALUES)
        values["tricky"] = "{trigger.topic}"
        rendered = render_json_template({"v": "{tricky}", "e": "x {tricky} y"}, values)
        assert rendered == {"v": "{trigger.topic}", "e": "x {trigger.topic} y"}

    def test_exact_key_wins_over_traversal_and_functions(self):
        values = dict(VALUES)
        values["a.b"] = "exact"
        values["a"] = {"b": "traversed"}
        values["ulid"] = "from-metadata"
        rendered = render_json_template({"x": "{a.b}", "u": "{ulid}"}, values)
        assert rendered == {"x": "exact", "u": "from-metadata"}

    def test_top_level_unresolved_string_renders_null(self):
        assert render_json_template("{nope}", VALUES) is None
        assert render_json_template("{trigger.topic}", VALUES) == "plant/line1/evaluate"


# ---------------------------------------------------------------------------
# The three functions
# ---------------------------------------------------------------------------

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class TestTemplateFunctions:
    def test_ulid_shape_uniqueness_and_monotonic_time_part(self):
        ulids = [template_functions.ulid() for _ in range(200)]
        assert all(ULID_RE.match(value) for value in ulids)
        assert len(set(ulids)) == 200
        earlier = template_functions.ulid(now_ms=1_700_000_000_000)
        later = template_functions.ulid(now_ms=1_700_000_000_001)
        assert earlier[:10] < later[:10]
        same_ms = [template_functions.ulid(now_ms=1_700_000_000_000)[:10] for _ in range(5)]
        assert len(set(same_ms)) == 1

    def test_ulid_time_part_decodes_to_the_millisecond(self):
        value = template_functions.ulid(now_ms=1_700_000_000_000)
        decoded = 0
        for char in value[:10]:
            decoded = decoded * 32 + template_functions.ULID_ALPHABET.index(char)
        assert decoded == 1_700_000_000_000

    def test_now_iso_is_an_aware_utc_date_time_with_milliseconds(self):
        before = datetime.now(timezone.utc)
        text = template_functions.now_iso()
        parsed = datetime.fromisoformat(text)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0
        assert text.endswith("+00:00")
        assert re.search(r"\.\d{3}\+00:00$", text)
        assert abs((parsed - before).total_seconds()) < 5

    def test_latency_ms_from_a_trigger_timestamp(self):
        assert template_functions.latency_ms(TRIGGER_TS, now=TRIGGER_TS + 1.2345) == 1234
        assert template_functions.latency_ms(None) is None
        assert template_functions.latency_ms("1700000000") is None
        assert template_functions.latency_ms(True) is None

    def test_functions_render_in_json_mode(self):
        rendered = render_json_template(
            {"msg_id": "{ulid}", "ts": "{now_iso}", "latency_ms": "{latency_ms}",
             "tagged": "run-{ulid}"}, VALUES)
        assert ULID_RE.match(rendered["msg_id"])
        datetime.fromisoformat(rendered["ts"])
        assert isinstance(rendered["latency_ms"], int) and rendered["latency_ms"] > 0
        assert rendered["tagged"].startswith("run-") and len(rendered["tagged"]) == 30

    def test_latency_ms_is_omitted_without_a_trigger_timestamp(self):
        values = {"trigger": {"topic": "t"}}
        assert render_json_template({"latency_ms": "{latency_ms}", "k": 1}, values) == {"k": 1}
        assert render_json_template({"latency_ms": "{latency_ms}"}, {}) == {}


# ---------------------------------------------------------------------------
# The binding
# ---------------------------------------------------------------------------

TEMPLATE = json.dumps({
    "msg_id": "{ulid}", "ts": "{now_iso}", "source": "edge.inspection",
    "order_id": "{trigger.payload_json.order_id}",
    "body": {"qc_stage": "{trigger.payload_json.body.qc_stage}",
             "verdict": "{bedrock.check.verdict}",
             "confidence": "{bedrock.check.confidence}",
             "reason": "{bedrock.check.reason}",
             "error": "{bedrock.check.error}",
             "image_s3_key": "{s3_upload.upload_raw.key}",
             "annotated_image_url": "{s3_upload.upload_annotated.presigned_url}",
             "latency_ms": "{latency_ms}"},
})


class TestJsonPayloadFormat:
    def test_json_mode_publishes_the_rendered_envelope(self):
        payload = json.loads(publish(TEMPLATE, payload_format="json"))
        assert ULID_RE.match(payload.pop("msg_id"))
        datetime.fromisoformat(payload.pop("ts"))
        assert isinstance(payload["body"].pop("latency_ms"), int)
        assert payload == {
            "source": "edge.inspection", "order_id": "o-1",
            "body": {"qc_stage": "qc2", "verdict": "pass", "confidence": 0.9,
                     "reason": "all parts placed", "image_s3_key": "images/o-1/u.jpg",
                     "annotated_image_url": VALUES["s3_upload"]["upload_annotated"]["presigned_url"]},
        }

    def test_error_outcome_carries_error_and_never_verdict(self):
        values = json.loads(json.dumps(VALUES))
        values["bedrock"]["check"] = {"outcome": "error", "error": "RuntimeError: boom"}
        del values["s3_upload"]["upload_annotated"]
        body = json.loads(publish(TEMPLATE, values, payload_format="json"))["body"]
        assert set(body) == {"qc_stage", "error", "image_s3_key", "latency_ms"}
        assert body["error"] == "RuntimeError: boom"

    def test_ensure_ascii_false_keeps_unicode(self):
        values = json.loads(json.dumps(VALUES))
        values["bedrock"]["check"]["reason"] = "Dach fehlt – Tür öffnen"
        payload_text = publish('{"reason": "{bedrock.check.reason}"}', values,
                               payload_format="json")
        assert payload_text == '{"reason": "Dach fehlt – Tür öffnen"}'
        assert "\\u" not in payload_text

    def test_placeholder_free_template_publishes_verbatim(self):
        template = json.dumps({"static": True, "n": [1, 2, {"k": "v"}],
                               "s": "plain text (no placeholder) [1] {2}"})
        payload_text = publish(template, payload_format="json")
        assert payload_text == template
        assert json.loads(payload_text)["s"] == "plain text (no placeholder) [1] {2}"

    def test_doubled_braces_are_not_an_escape_in_json_mode(self):
        """Unlike text mode, json mode has no brace escaping: ``{{b}}``
        contains the unresolved placeholder ``{b}`` and the key is omitted
        (nothing with a literal placeholder is ever published)."""
        payload = json.loads(publish('{"s": "a {{b}} c", "k": 1}', payload_format="json"))
        assert payload == {"k": 1}

    def test_non_json_template_fails_the_binding_loudly(self):
        from workflow_engine.output_bindings import OutputBindingError
        with pytest.raises(OutputBindingError) as info:
            publish('{"a": {bedrock.check.verdict}}', payload_format="json")
        assert "not valid JSON" in str(info.value)

    def test_native_values_with_quotes_and_newlines_are_escaped(self):
        values = json.loads(json.dumps(VALUES))
        values["bedrock"]["check"]["reason"] = 'said "no"\nsecond line'
        payload = json.loads(publish('{"reason": "{bedrock.check.reason}"}', values,
                                     payload_format="json"))
        assert payload == {"reason": 'said "no"\nsecond line'}

    def test_text_mode_default_is_byte_identical_to_render_template(self):
        template = '{{"verdict": "{bedrock.check.verdict}", "flag": {is_anomalous}}}'
        expected = render_template(template, dict(VALUES))
        assert publish(template) == expected
        assert publish(template, payload_format="text") == expected
        assert publish("{is_anomalous}") == json.dumps(False)

    def test_json_mode_embeds_attached_metadata_like_text_mode(self):
        greengrass = Recorder()
        processor = OutputBindingProcessor(greengrass_publisher=greengrass)
        document = {"schemaVersion": 1, "executorBindings": [
            {"nodeId": "meta", "binding": "metadata",
             "parameters": {}, "metadataMappings": [],
             "staticJson": {"site": "plant"}, "attachTo": ["publish"],
             "upstreamNodeIds": [], "downstreamNodeIds": ["publish"]},
            {"nodeId": "publish", "binding": "mqtt_publish",
             "parameters": {"topic": "t", "qos": 0, "greengrass": True,
                            "payload_format": "json",
                            "payload_template": '{"verdict": "{bedrock.check.verdict}"}'},
             "upstreamNodeIds": ["meta"], "downstreamNodeIds": []},
        ]}
        processor.process(None, document, dict(VALUES))
        ((_, payload_text, _), _), = greengrass.calls
        assert json.loads(payload_text) == {"site": "plant", "verdict": "pass"}
