"""Validator rules of the built-in inspection chain (bedrock-inspection-chain).

- ``_check_w1`` no longer warns about the unconnected result port of an
  output-category node (``s3_upload.out``), connected or not, while an
  unconnected inference ``out`` port still warns;
- one positive and one negative case for each new code:
  ``MQTT_PAYLOAD_TEMPLATE_JSON``, ``S3_UPLOAD_SOURCE_NO_BEDROCK``,
  ``BEDROCK_EXPECTED_COUNT_PATH``, ``BEDROCK_INSPECTION_INPUTS``,
  ``BEDROCK_BUDGET_TOO_SMALL``;
- the demo-shaped chain (trigger -> camera -> bedrock -> two uploads ->
  publish) validates with zero errors and a legacy Bedrock graph produces
  none of the new codes.
"""

from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import (
    CODE_BEDROCK_BUDGET_TOO_SMALL,
    CODE_BEDROCK_EXPECTED_COUNT_PATH,
    CODE_BEDROCK_INSPECTION_INPUTS,
    CODE_MQTT_PAYLOAD_TEMPLATE_JSON,
    CODE_S3_UPLOAD_SOURCE_NO_BEDROCK,
    CODE_W1_UNUSED_OUTPUT_PORT,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    validate,
)

NEW_CODES = (
    CODE_MQTT_PAYLOAD_TEMPLATE_JSON, CODE_S3_UPLOAD_SOURCE_NO_BEDROCK,
    CODE_BEDROCK_EXPECTED_COUNT_PATH, CODE_BEDROCK_INSPECTION_INPUTS,
    CODE_BEDROCK_BUDGET_TOO_SMALL,
)
_POS = Position(0.0, 0.0)
_JSON_TEMPLATE = ('{"msg_id": "{ulid}", "body": {"verdict": "{bedrock.check.verdict}", '
                  '"image_s3_key": "{s3_upload.upload_raw.key}"}}')


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(cid, source, target, source_port="out", target_port="in"):
    return Connection(id=cid, source=PortEndpoint(source, source_port),
                      target=PortEndpoint(target, target_port))


def _trigger():
    return _node("trigger", "mqtt_subscribe", topic="plant/line1/evaluate",
                 greengrass=True, qos=1)


def _camera():
    return _node("camera", "aravis_camera_source", camera_id="cam-01")


def _upload(node_id, **extra):
    parameters = {"destination": "s3://example-bucket/images/{trigger.payload_json.order_id}.jpg"}
    parameters.update(extra)
    return _node(node_id, "s3_upload", **parameters)


def _publish(**extra):
    parameters = {"topic": "plant/line1/result", "greengrass": True}
    parameters.update(extra)
    return _node("mqtt", "mqtt_publish", **parameters)


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


def _codes(findings):
    return {f.code for f in findings}


def inspection_chain(bedrock_extra=None, publish_extra=None):
    """trigger -> camera.activation; camera -> check; check -> upload_raw
    (frame) and upload_annotated (annotated_image); both uploads -> mqtt."""
    bedrock = {"answer_format": "inspection", "render_prompt": True,
               "expected_count_path": "trigger.payload_json.body.order.items",
               "attempts": 2, "attempt_timeout_s": 6, "budget_s": 11,
               "on_error": "record", "model": "us.amazon.nova-pro-v1:0"}
    bedrock.update(bedrock_extra or {})
    publish = {"payload_format": "json", "payload_template": _JSON_TEMPLATE}
    publish.update(publish_extra or {})
    return WorkflowGraph(
        nodes=[_trigger(), _camera(), _node("check", "bedrock_inference", **bedrock),
               _upload("upload_raw"),
               _upload("upload_annotated", source="annotated_image",
                       strip_suffix=".jpg", append_suffix=".annotated.jpg",
                       presign_get_expiry_s=900),
               _publish(**publish)],
        connections=[_conn("c1", "trigger", "camera", target_port="activation"),
                     _conn("c2", "camera", "check"),
                     _conn("c3", "check", "upload_raw"),
                     _conn("c4", "check", "upload_annotated"),
                     _conn("c5", "upload_raw", "mqtt"),
                     _conn("c6", "upload_annotated", "mqtt")],
    )


def legacy_bedrock_graph():
    return WorkflowGraph(
        nodes=[_node("cam", "icam_source"),
               _node("ref", "folder_source", location="/aws_dda/ref/golden.jpg"),
               _node("bedrock", "bedrock_inference"),
               _node("mqtt", "mqtt_publish", broker_host="10.0.0.12", topic="factory/line1")],
        connections=[_conn("c1", "cam", "bedrock"),
                     _conn("c2", "ref", "bedrock", target_port="reference"),
                     _conn("c3", "bedrock", "mqtt")],
    )


class TestUnusedOutputPortOnOutputNodes:
    def test_connected_upload_result_port_raises_no_warning(self):
        findings = validate(inspection_chain())
        assert [f.node_id for f in _by_code(findings, CODE_W1_UNUSED_OUTPUT_PORT)] == []

    def test_unconnected_upload_result_port_raises_no_warning(self):
        graph = WorkflowGraph(
            nodes=[_trigger(), _camera(), _upload("upload")],
            connections=[_conn("c1", "trigger", "camera", target_port="activation"),
                         _conn("c2", "camera", "upload")])
        findings = validate(graph)
        assert _by_code(findings, CODE_W1_UNUSED_OUTPUT_PORT) == []
        assert [f for f in findings if f.severity == SEVERITY_ERROR] == []

    def test_unconnected_inference_out_port_still_warns(self):
        graph = WorkflowGraph(
            nodes=[_node("src", "folder_source", location="/data/images"),
                   _node("inf", "model_inference", modelName="widget-anomaly-v3")],
            connections=[_conn("c1", "src", "inf")])
        found = _by_code(validate(graph), CODE_W1_UNUSED_OUTPUT_PORT)
        assert [f.node_id for f in found] == ["inf"]
        assert found[0].severity == SEVERITY_WARNING


class TestInspectionChainCodes:
    def test_demo_shaped_chain_has_no_errors_and_none_of_the_new_codes(self):
        findings = validate(inspection_chain())
        assert [f for f in findings if f.severity == SEVERITY_ERROR] == []
        assert not (_codes(findings) & set(NEW_CODES))

    def test_legacy_bedrock_graph_produces_none_of_the_new_codes(self):
        assert not (_codes(validate(legacy_bedrock_graph())) & set(NEW_CODES))

    def test_mqtt_payload_template_json(self):
        bad = validate(inspection_chain(publish_extra={"payload_template": "{inference_json}"}))
        (finding,) = _by_code(bad, CODE_MQTT_PAYLOAD_TEMPLATE_JSON)
        assert finding.severity == SEVERITY_ERROR and finding.node_id == "mqtt"
        # An array template is fine; so is any template in text mode.
        ok_array = validate(inspection_chain(publish_extra={"payload_template": '["{ulid}"]'}))
        assert _by_code(ok_array, CODE_MQTT_PAYLOAD_TEMPLATE_JSON) == []
        text_mode = validate(inspection_chain(publish_extra={
            "payload_format": "text", "payload_template": "{inference_json}"}))
        assert _by_code(text_mode, CODE_MQTT_PAYLOAD_TEMPLATE_JSON) == []

    def test_s3_upload_source_no_bedrock(self):
        camera_fed = WorkflowGraph(
            nodes=[_trigger(), _camera(), _upload("upload", source="annotated_image")],
            connections=[_conn("c1", "trigger", "camera", target_port="activation"),
                         _conn("c2", "camera", "upload")])
        (finding,) = _by_code(validate(camera_fed), CODE_S3_UPLOAD_SOURCE_NO_BEDROCK)
        assert finding.severity == SEVERITY_ERROR and finding.node_id == "upload"
        frame_source = WorkflowGraph(
            nodes=[_trigger(), _camera(), _upload("upload", source="frame")],
            connections=camera_fed.connections)
        assert _by_code(validate(frame_source), CODE_S3_UPLOAD_SOURCE_NO_BEDROCK) == []
        assert _by_code(validate(inspection_chain()), CODE_S3_UPLOAD_SOURCE_NO_BEDROCK) == []

    def test_bedrock_expected_count_path_without_trigger(self):
        graph = WorkflowGraph(
            nodes=[_node("cam", "icam_source"),
                   _node("check", "bedrock_inference", answer_format="inspection",
                         expected_count_path="trigger.payload_json.body.order.items"),
                   _node("mqtt", "mqtt_publish", broker_host="10.0.0.12", topic="factory/line1")],
            connections=[_conn("c1", "cam", "check"), _conn("c2", "check", "mqtt")])
        (finding,) = _by_code(validate(graph), CODE_BEDROCK_EXPECTED_COUNT_PATH)
        assert finding.severity == SEVERITY_WARNING and finding.node_id == "check"
        assert _by_code(validate(inspection_chain()), CODE_BEDROCK_EXPECTED_COUNT_PATH) == []

    def test_bedrock_inspection_inputs(self):
        crop = validate(inspection_chain(bedrock_extra={"crop_detection_index": 0}))
        (finding,) = _by_code(crop, CODE_BEDROCK_INSPECTION_INPUTS)
        assert finding.severity == SEVERITY_ERROR and finding.node_id == "check"
        assert "crop_detection_index" in finding.message
        reference = legacy_bedrock_graph()
        reference.nodes[2].parameters["answer_format"] = "inspection"
        (finding,) = _by_code(validate(reference), CODE_BEDROCK_INSPECTION_INPUTS)
        assert "'reference'" in finding.message and finding.node_id == "bedrock"
        legacy_crop = validate(inspection_chain(bedrock_extra={
            "answer_format": "legacy", "crop_detection_index": 0}))
        assert _by_code(legacy_crop, CODE_BEDROCK_INSPECTION_INPUTS) == []

    def test_bedrock_budget_too_small(self):
        small = validate(inspection_chain(bedrock_extra={"budget_s": 5, "attempt_timeout_s": 6}))
        (finding,) = _by_code(small, CODE_BEDROCK_BUDGET_TOO_SMALL)
        assert finding.severity == SEVERITY_WARNING and finding.node_id == "check"
        for budget in (0, 6, 11):
            ok = validate(inspection_chain(bedrock_extra={"budget_s": budget, "attempt_timeout_s": 6}))
            assert _by_code(ok, CODE_BEDROCK_BUDGET_TOO_SMALL) == [], budget
