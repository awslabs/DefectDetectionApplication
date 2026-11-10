"""The inspection answer contract of ``workflow_core.anomaly_invocation``
(bedrock-inspection-chain, ``answer_format: inspection``).

``parse_inspection_answer`` accepts exactly one well-formed JSON object
and applies the consistency rules that make a false OK impossible: every
malformed shape below raises ``ValueError`` (one failed attempt, never a
pass); the ``>=`` rule (a ``pass`` with fewer objects than expected is
malformed, a ``fail`` with fewer stays a ``fail``, extra objects are
accepted); NOK-with-pass; the 0..1000 range under ``normalized_1000``;
degenerate boxes flagged, not fatal. ``build_bedrock_invocation`` keeps
today's request bytes for an absent/``legacy`` ``answer_format`` and
appends the inspection instruction for ``inspection``.
"""

import json

import pytest

from workflow_core import anomaly_invocation as ai

INPUT_JPEG = b"\xff\xd8input\xff\xd9"


def _box(x_min=40, y_min=120, x_max=220, y_max=260):
    return {"x_min": x_min, "y_min": y_min, "x_max": x_max, "y_max": y_max}


def good_answer(verdict="pass", objects=None, confidence=0.93, reason=None,
                **extra):
    """A well-formed three-object answer; ``extra`` adds top-level keys."""
    if objects is None:
        qc = "OK" if verdict == "pass" else "NOK"
        objects = [
            {"name": "gold front fender", "qc": "OK",
             "reason": "seated at the front", "bounding_box": _box()},
            {"name": "black rear fender", "qc": qc,
             "reason": "" if qc == "OK" else "swapped with the front",
             "bounding_box": _box(400, 120, 600, 260)},
            {"name": "blue roof", "qc": "OK", "reason": "on top",
             "bounding_box": _box(200, 40, 440, 140)},
        ]
    if reason is None:
        reason = ("assembly is correct" if verdict == "pass"
                  else "the fenders are swapped")
    answer = {"verdict": verdict, "confidence": confidence,
              "reason": reason, "objects": objects}
    answer.update(extra)
    return answer


def _one_object(**overrides):
    obj = {"name": "blue roof", "qc": "OK", "reason": "on top",
           "bounding_box": _box(200, 40, 440, 140)}
    obj.update(overrides)
    return obj


def _dumps(answer):
    return json.dumps(answer)


#: Every malformed/incomplete shape; each is parsed with expected_count=3
#: (the three-part order) and must raise ValueError. The first eleven are
#: the shapes the hand-written predecessor of this parser was tested
#: against; the rest exercise every schema rule of the contract.
MALFORMED = {
    "prose": "The car looks fine to me, verdict pass.",
    "fenced_with_extra_key": "```json\n" + _dumps(good_answer(extra_key=1)) + "\n```",
    "not_json": "{verdict: pass",
    "empty_objects": _dumps(good_answer(objects=[])),
    "bad_verdict": _dumps(good_answer("OK")),
    "confidence_out_of_range": _dumps(good_answer(confidence=1.5)),
    "missing_boxes": _dumps({"verdict": "pass", "confidence": 0.9, "reason": "ok",
                             "objects": [{"name": "roof", "qc": "OK", "reason": ""}]}),
    "pass_with_nok_object": _dumps(good_answer(objects=[
        _one_object(qc="NOK", reason="missing", bounding_box=_box(1, 1, 9, 9))])),
    "empty": "",
    "two_objects_json": _dumps(good_answer()) + _dumps(good_answer()),
    "pass_with_too_few_objects": _dumps(good_answer(objects=[_one_object()])),
    "whitespace_only": "   \n\t ",
    "json_array": _dumps([good_answer()]),
    "json_string": _dumps("pass"),
    "missing_verdict": _dumps({k: v for k, v in good_answer().items() if k != "verdict"}),
    "missing_reason": _dumps({k: v for k, v in good_answer().items() if k != "reason"}),
    "verdict_null": _dumps(good_answer(verdict=None)),
    "objects_not_a_list": _dumps(good_answer(objects={"name": "roof"})),
    "confidence_string": _dumps(good_answer(confidence="0.9")),
    "confidence_negative": _dumps(good_answer(confidence=-0.1)),
    "object_extra_key": _dumps(good_answer(objects=[_one_object(color="blue")])),
    "object_missing_name": _dumps(good_answer(objects=[
        {k: v for k, v in _one_object().items() if k != "name"}])),
    "empty_name": _dumps(good_answer(objects=[_one_object(name="")])),
    "bad_qc_case": _dumps(good_answer(objects=[_one_object(qc="ok")])),
    "box_float_coordinate": _dumps(good_answer(objects=[
        _one_object(bounding_box=_box(x_min=1.5))])),
    "box_string_coordinate": _dumps(good_answer(objects=[
        _one_object(bounding_box=_box(x_min="1"))])),
    "box_bool_coordinate": _dumps(good_answer(objects=[
        _one_object(bounding_box=_box(x_min=True))])),
    "box_missing_key": _dumps(good_answer(objects=[
        _one_object(bounding_box={"x_min": 1, "y_min": 1, "x_max": 9})])),
    "box_extra_key": _dumps(good_answer(objects=[
        _one_object(bounding_box=dict(_box(), w=5))])),
    "two_fenced_blocks": ("```json\n" + _dumps(good_answer()) + "\n```\n"
                          "```json\n" + _dumps(good_answer()) + "\n```"),
    "prose_around_bare_object": "Answer: " + _dumps(good_answer()) + " done.",
}


@pytest.mark.parametrize("variant", sorted(MALFORMED))
def test_malformed_answers_raise_value_error(variant):
    assert len(MALFORMED) >= 21
    with pytest.raises(ValueError):
        ai.parse_inspection_answer(MALFORMED[variant], expected_count=3)


class TestAcceptedAnswers:
    def test_bare_object_is_parsed_with_native_fields(self):
        parsed = ai.parse_inspection_answer(_dumps(good_answer()), expected_count=3)
        assert parsed["verdict"] == "pass"
        assert parsed["confidence"] == 0.93 and isinstance(parsed["confidence"], float)
        assert parsed["reason"] == "assembly is correct"
        assert parsed["objects"] == good_answer()["objects"]
        assert parsed["dropped_box_indices"] == []
        assert parsed["box_scale"] == "pixels"
        assert parsed["expected_count"] == 3

    @pytest.mark.parametrize("wrap", [
        "```json\n{0}\n```", "```\n{0}\n```",
        "Here is my answer:\n```json\n{0}\n```\nLet me know.",
        "\n\n  {0}  \n",
    ])
    def test_single_fenced_block_or_padded_object_is_accepted(self, wrap):
        parsed = ai.parse_inspection_answer(wrap.format(_dumps(good_answer())))
        assert parsed["verdict"] == "pass" and len(parsed["objects"]) == 3

    def test_no_expected_count_sets_no_minimum(self):
        parsed = ai.parse_inspection_answer(_dumps(good_answer(objects=[_one_object()])))
        assert parsed["verdict"] == "pass" and parsed["expected_count"] is None

    def test_fail_with_fewer_objects_than_expected_stays_fail(self):
        one_nok = [_one_object(name="black rear fender", qc="NOK",
                               reason="swapped with the front")]
        parsed = ai.parse_inspection_answer(
            _dumps(good_answer("fail", objects=one_nok)), expected_count=3)
        assert parsed["verdict"] == "fail" and len(parsed["objects"]) == 1

    def test_pass_with_more_objects_than_expected_is_accepted(self):
        extra = good_answer()["objects"] + [_one_object(name="wheel", bounding_box=_box(1, 1, 5, 5))]
        parsed = ai.parse_inspection_answer(_dumps(good_answer(objects=extra)), expected_count=3)
        assert parsed["verdict"] == "pass" and len(parsed["objects"]) == 4

    def test_fail_may_carry_nok_objects(self):
        parsed = ai.parse_inspection_answer(_dumps(good_answer("fail")), expected_count=3)
        assert parsed["verdict"] == "fail"
        assert [o["qc"] for o in parsed["objects"]] == ["OK", "NOK", "OK"]

    def test_degenerate_boxes_are_flagged_not_fatal(self):
        objects = [_one_object(name="a", bounding_box=_box(10, 10, 10, 50)),
                   _one_object(name="b", bounding_box=_box(10, 60, 50, 20)),
                   _one_object(name="c")]
        parsed = ai.parse_inspection_answer(_dumps(good_answer(objects=objects)))
        assert parsed["dropped_box_indices"] == [0, 1]
        assert len(parsed["objects"]) == 3


class TestBoxScale:
    def test_normalized_1000_rejects_a_coordinate_above_1000(self):
        text = _dumps(good_answer(objects=[_one_object(bounding_box=_box(0, 0, 1001, 500))]))
        with pytest.raises(ValueError):
            ai.parse_inspection_answer(text, box_scale="normalized_1000")
        # The same numbers are plain pixels under the default convention.
        assert ai.parse_inspection_answer(text)["verdict"] == "pass"

    def test_normalized_1000_rejects_a_negative_coordinate(self):
        text = _dumps(good_answer(objects=[_one_object(bounding_box=_box(-1, 0, 10, 10))]))
        with pytest.raises(ValueError):
            ai.parse_inspection_answer(text, box_scale="normalized_1000")

    def test_normalized_1000_accepts_the_full_grid(self):
        text = _dumps(good_answer(objects=[_one_object(bounding_box=_box(0, 0, 1000, 1000))]))
        parsed = ai.parse_inspection_answer(text, box_scale="normalized_1000")
        assert parsed["box_scale"] == "normalized_1000"
        assert parsed["objects"][0]["bounding_box"] == _box(0, 0, 1000, 1000)

    def test_unresolved_auto_is_rejected(self):
        with pytest.raises(ValueError):
            ai.parse_inspection_answer(_dumps(good_answer()), box_scale="auto")


def _json_values_for_cross_check():
    """Every MALFORMED text that is one JSON value, plus accepted answers
    and draft-07 borderline values (1.0 is an integer, a bool is never a
    number, an int is a number)."""
    cases = {}
    for name, text in MALFORMED.items():
        try:
            cases["malformed:" + name] = json.loads(text)
        except ValueError:
            continue
    cases["good:pass"] = good_answer()
    cases["good:fail"] = good_answer("fail")
    cases["border:float_integer_box"] = good_answer(objects=[
        _one_object(bounding_box=_box(x_min=1.0))])
    cases["border:int_confidence"] = good_answer(confidence=1)
    cases["border:bool_confidence"] = good_answer(confidence=True)
    cases["border:bool_as_verdict"] = good_answer(verdict=True)
    cases["border:nested_null"] = good_answer(objects=[_one_object(reason=None)])
    return cases


@pytest.mark.parametrize("name", sorted(_json_values_for_cross_check()))
def test_schema_interpreter_agrees_with_jsonschema(name):
    """The module's standard-library schema interpreter accepts exactly
    the instances ``jsonschema`` accepts for the contract (the module
    stays import-pure; the reference library is the oracle)."""
    jsonschema = pytest.importorskip("jsonschema")
    instance = _json_values_for_cross_check()[name]
    try:
        jsonschema.validate(instance, ai.INSPECTION_ANSWER_SCHEMA)
        reference_accepts = True
    except jsonschema.ValidationError:
        reference_accepts = False
    try:
        ai._validate_schema(instance, ai.INSPECTION_ANSWER_SCHEMA, "answer")
        ours_accepts = True
    except ValueError:
        ours_accepts = False
    assert ours_accepts is reference_accepts, name


class TestSchemaAndConstants:
    def test_schema_is_closed_at_every_level(self):
        schema = ai.INSPECTION_ANSWER_SCHEMA
        assert schema["required"] == ["verdict", "confidence", "reason", "objects"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["verdict"]["enum"] == ["pass", "fail"]
        items = schema["properties"]["objects"]["items"]
        assert schema["properties"]["objects"]["minItems"] == 1
        assert items["additionalProperties"] is False
        assert items["properties"]["qc"]["enum"] == ["OK", "NOK"]
        box = items["properties"]["bounding_box"]
        assert box["additionalProperties"] is False
        assert sorted(box["required"]) == ["x_max", "x_min", "y_max", "y_min"]

    def test_timeouts(self):
        assert ai.BEDROCK_CONNECT_TIMEOUT_SEC == 2
        assert ai.BEDROCK_READ_TIMEOUT_SEC == 30

    def test_instruction_names_the_shape(self):
        for token in ('"verdict"', '"confidence"', '"reason"', '"objects"',
                      '"qc"', '"bounding_box"', "exactly one JSON object"):
            assert token in ai.INSPECTION_JSON_INSTRUCTION


class TestBuildBedrockInvocationAnswerFormat:
    BASE = {"model": "us.amazon.nova-pro-v1:0", "prompt": "Inspect the car.",
            "region": "us-west-2", "max_tokens": 600, "system_prompt": "Be terse."}

    @pytest.mark.parametrize("anomaly_mode", [True, False])
    @pytest.mark.parametrize("answer_format", [None, "", "legacy"])
    def test_absent_or_legacy_keeps_todays_request_bytes(self, anomaly_mode, answer_format):
        legacy = dict(self.BASE, anomaly_mode=anomaly_mode)
        parameters = dict(legacy)
        if answer_format is not None:
            parameters["answer_format"] = answer_format
        expected = ai.build_bedrock_invocation(legacy, INPUT_JPEG)
        actual = ai.build_bedrock_invocation(parameters, INPUT_JPEG)
        assert actual == expected
        assert actual.converse_kwargs() == expected.converse_kwargs()
        assert actual.answer_format == "legacy"
        assert actual.anomaly_mode is anomaly_mode
        if anomaly_mode:
            assert actual.prompt == ("Inspect the car." + ai.ANOMALY_INSTRUCTION_SEPARATOR
                                     + ai.BEDROCK_JSON_INSTRUCTION)
        else:
            assert actual.prompt == "Inspect the car."

    @pytest.mark.parametrize("anomaly_mode", [True, False, None])
    def test_inspection_appends_the_inspection_instruction_and_ignores_anomaly_mode(
            self, anomaly_mode):
        parameters = dict(self.BASE, answer_format="inspection", anomaly_mode=anomaly_mode)
        invocation = ai.build_bedrock_invocation(parameters, INPUT_JPEG)
        assert invocation.prompt == ("Inspect the car." + ai.ANOMALY_INSTRUCTION_SEPARATOR
                                     + ai.INSPECTION_JSON_INSTRUCTION)
        assert ai.BEDROCK_JSON_INSTRUCTION not in invocation.prompt
        assert invocation.answer_format == "inspection"
        assert invocation.anomaly_mode is False
        kwargs = invocation.converse_kwargs()
        assert kwargs["modelId"] == "us.amazon.nova-pro-v1:0"
        assert kwargs["messages"][0]["content"][0] == {"text": invocation.prompt}
        assert kwargs["system"] == [{"text": "Be terse."}]
        assert kwargs["inferenceConfig"] == {"maxTokens": 600}
