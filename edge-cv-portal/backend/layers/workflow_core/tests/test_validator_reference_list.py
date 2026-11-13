"""Validator, catalog and compiler rules of reference lists
(inspection-format tuning fix, design §12, §9.13.6 and §14).

Layer halves of AC-15 (the lexical ``file://`` rule), AC-30 and AC-32
(list limits, the legacy path), AC-37 (per-line findings, the model
warning, ``BEDROCK_PAYLOAD_NO_TRIGGER`` only for payload lines, the
catalog's ``multiline`` hints and texts, the compiler pin) and AC-42
(upload lines).

The new finding codes are compared as strings, so on the device's
commit the behavioural tests run to their assertions (a literal
``file://`` URI gives no finding there, and a literal URI without a
trigger gives ``BEDROCK_PAYLOAD_NO_TRIGGER``). ``reference_uploads`` is
passed inside the tests that need it. Tests marked "Preservation" pin
behaviour that holds before and after the fix.
"""

from workflow_core.catalog import get_node_type
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import SEVERITY_ERROR, SEVERITY_WARNING, validate
from workflow_core.validator.checks import (
    CODE_BEDROCK_INSPECTION_INPUTS,
    CODE_BEDROCK_PAYLOAD_NO_TRIGGER,
    CODE_BEDROCK_REFERENCE_CONFLICT,
)

URI_NOT_ALLOWED = "BEDROCK_REFERENCE_URI_NOT_ALLOWED"
LIST_INVALID = "BEDROCK_REFERENCE_LIST_INVALID"
UPLOAD_MISSING = "BEDROCK_REFERENCE_UPLOAD_MISSING"
MODEL_UNVERIFIED = "BEDROCK_REFERENCE_MODEL_UNVERIFIED"
REFERENCE_CODES = (URI_NOT_ALLOWED, LIST_INVALID, UPLOAD_MISSING,
                   MODEL_UNVERIFIED, CODE_BEDROCK_PAYLOAD_NO_TRIGGER,
                   CODE_BEDROCK_REFERENCE_CONFLICT)

HEX = "0123456789abcdef" * 4
OTHER_HEX = "f" * 64

#: A's parameter values on its check node (generic: an on-device legend
#: and its directory, the Nova Pro profile).
A_STYLE = {
    "model": "us.amazon.nova-pro-v1:0",
    "region": "us-west-2",
    "prompt": "Check the parts against the legend.",
    "answer_format": "inspection",
    "reference_payload_path": "file:///aws_dda/images/source.jpg",
    "allowed_uri_prefixes": "file:///aws_dda/images/",
    "on_error": "record",
    "error_verdict": "error",
    "attempts": 2,
    "attempt_timeout_s": 6,
    "budget_s": 11,
    "max_image_dimension": 1280,
    "render_prompt": True,
    "expected_count_path": "trigger.payload_json.body.order.items",
    "box_scale": "normalized_1000",
    "max_tokens": 600,
}

_POS = Position(0.0, 0.0)


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _graph(trigger=True, fed_reference=False, **parameters):
    """trig -> src.activation, src -> bed -> cap; ``bed`` carries
    ``parameters``."""
    nodes = [
        _node("src", "folder_source", location="/data/images"),
        _node("bed", "bedrock_inference", **parameters),
        _node("cap", "capture", output_path="/out"),
    ]
    connections = [_conn("c2", "src", "bed"), _conn("c3", "bed", "cap")]
    if trigger:
        nodes.insert(0, _node("trig", "mqtt_subscribe",
                              topic="plant/line1/evaluate", greengrass=True))
        connections.insert(0, _conn("c1", "trig", "src", target_port="activation"))
    if fed_reference:
        nodes.append(_node("ref", "folder_source", location="/data/ref"))
        connections.append(_conn("c4", "ref", "bed", target_port="reference"))
    return WorkflowGraph(nodes=nodes, connections=connections)


def _reference_findings(findings):
    return [f for f in findings if f.code in REFERENCE_CODES]


def _codes(findings):
    return [f.code for f in _reference_findings(findings)]


def _inspection(**parameters):
    return dict({"answer_format": "inspection"}, **parameters)


# ---------------------------------------------------------------------------
# AC-15: the lexical file:// rule
# ---------------------------------------------------------------------------

class TestFileUriRule:
    def test_literal_file_uri_without_a_permitting_entry_is_an_error(self):
        findings = validate(_graph(**_inspection(
            reference_payload_path="file:///aws_dda/images/source.jpg")))
        assert _codes(findings) == [URI_NOT_ALLOWED]
        (finding,) = _reference_findings(findings)
        assert finding.severity == SEVERITY_ERROR
        assert finding.node_id == "bed"
        assert finding.message == (
            "Node 'bed': 'reference_payload_path' is the fixed URI "
            "file:///aws_dda/images/source.jpg but no allowed_uri_prefixes "
            "entry permits it (a file:// reference needs an explicit file:// "
            "entry naming its directory)")

    def test_literal_uri_without_a_trigger_is_not_a_payload_path(self):
        findings = validate(_graph(trigger=False, **_inspection(
            reference_payload_path="file:///aws_dda/images/source.jpg",
            allowed_uri_prefixes="file:///aws_dda/images/")))
        assert _codes(findings) == []

    def test_a_host_authority_is_reported(self):
        findings = validate(_graph(**_inspection(
            reference_payload_path="file://plant-host/aws_dda/images/source.jpg",
            allowed_uri_prefixes="file:///aws_dda/images/")))
        assert _codes(findings) == [URI_NOT_ALLOWED]
        assert "'plant-host'" in _reference_findings(findings)[0].message

    def test_a_path_that_is_not_absolute_is_reported(self):
        for uri in ("file://", "file:///", "file://localhost/"):
            findings = validate(_graph(**_inspection(
                reference_payload_path=uri, allowed_uri_prefixes="file:///")))
            assert _codes(findings) == [URI_NOT_ALLOWED], uri

    def test_dot_segments_in_the_entry_are_normalized(self):
        findings = validate(_graph(**_inspection(
            reference_payload_path="file:///aws_dda/images/source.jpg",
            allowed_uri_prefixes="file:///aws_dda/./images/")))
        assert _codes(findings) == []
        findings = validate(_graph(**_inspection(
            reference_payload_path="file:///aws_dda/images/source.jpg",
            allowed_uri_prefixes="file:///aws_dda/./other/")))
        assert _codes(findings) == [URI_NOT_ALLOWED]

    def test_dot_dot_segments_in_the_uri_are_normalized(self):
        accepted = validate(_graph(**_inspection(
            reference_payload_path="file:///aws_dda/images/../images/source.jpg",
            allowed_uri_prefixes="file:///aws_dda/images/")))
        assert _codes(accepted) == []
        refused = validate(_graph(**_inspection(
            reference_payload_path="file:///aws_dda/images/../secret.jpg",
            allowed_uri_prefixes="file:///aws_dda/images/")))
        assert _codes(refused) == [URI_NOT_ALLOWED]

    def test_an_entry_is_a_directory_root_not_a_string_prefix(self):
        findings = validate(_graph(**_inspection(
            reference_payload_path="file:///aws_dda/images/source.jpg",
            allowed_uri_prefixes="file:///aws_dda/ima")))
        assert _codes(findings) == [URI_NOT_ALLOWED]

    def test_localhost_forms(self):
        for uri, entry in (
                ("file://localhost/aws_dda/images/a.jpg", "file:///aws_dda/images"),
                ("file:///aws_dda/images/a.jpg", "file://localhost/aws_dda/images/"),
                ("file:///aws_dda/images/a.jpg", "s3://x/\n  file:///  \n")):
            findings = validate(_graph(**_inspection(
                reference_payload_path=uri, allowed_uri_prefixes=entry)))
            assert _codes(findings) == [], (uri, entry)
        findings = validate(_graph(**_inspection(
            reference_payload_path="file:///aws_dda/images/a.jpg",
            allowed_uri_prefixes="file://aws_dda/images/")))
        assert _codes(findings) == [URI_NOT_ALLOWED]

    def test_a_style_parameter_values_give_no_finding_on_the_node(self):
        """Preservation: A's check parameters give no finding on the
        node, before the fix (no URI rule) and after it (the rule
        permits them)."""
        findings = validate(_graph(**A_STYLE))
        assert [f for f in findings if f.node_id == "bed"] == []


class TestRemoteUriRule:
    def test_remote_uri_outside_the_prefixes_is_reported(self):
        findings = validate(_graph(**_inspection(
            reference_payload_path="s3://other-bucket/legend.jpg",
            allowed_uri_prefixes="s3://plant-images/\nhttps://mes.local/")))
        assert _codes(findings) == [URI_NOT_ALLOWED]
        assert _reference_findings(findings)[0].message == (
            "Node 'bed': 'reference_payload_path' is the fixed URI "
            "s3://other-bucket/legend.jpg but no allowed_uri_prefixes entry "
            "is a prefix of it")

    def test_remote_uri_inside_the_prefixes_or_with_no_prefixes(self):
        for uri, entries in (("https://mes.local/legend.png", "https://mes.local/"),
                             ("s3://plant-images/legend.jpg", ""),
                             ("https://any.example/legend.jpg", "  \n")):
            findings = validate(_graph(**_inspection(
                reference_payload_path=uri, allowed_uri_prefixes=entries)))
            assert _codes(findings) == [], uri
        # The rule is a string prefix: the same URI outside the entry.
        findings = validate(_graph(**_inspection(
            reference_payload_path="https://mes.local.example/legend.png",
            allowed_uri_prefixes="https://mes.local/")))
        assert _codes(findings) == [URI_NOT_ALLOWED]


# ---------------------------------------------------------------------------
# AC-30 / AC-32 (validator halves): list rules
# ---------------------------------------------------------------------------

class TestListRules:
    def test_legacy_node_with_two_lines_or_a_document(self):
        for value, count in (("refs.0.image\nrefs.1.image", 2),
                             ("file:///aws_dda/reference/sheet.pdf", 1)):
            findings = validate(_graph(
                reference_payload_path=value,
                allowed_uri_prefixes="file:///aws_dda/reference/"))
            assert _codes(findings) == [LIST_INVALID], value
            assert _reference_findings(findings)[0].message == (
                "Node 'bed': reference_payload_path lists {0} reference(s) or "
                "a document; only answer_format inspection accepts several "
                "references or documents".format(count))

    def test_legacy_node_with_one_image_line(self):
        """Preservation: a legacy node with one payload line has no list
        finding."""
        assert _codes(validate(_graph(reference_payload_path="refs.0.image"))) == []

    def test_converse_limits(self):
        documents = "\n".join(
            "file:///aws_dda/reference/d{0}.pdf".format(i) for i in range(6))
        findings = validate(_graph(**_inspection(
            reference_payload_path=documents,
            allowed_uri_prefixes="file:///aws_dda/reference/")))
        assert _codes(findings) == [LIST_INVALID]
        assert "lists 6 documents" in _reference_findings(findings)[0].message
        images = "\n".join("refs.{0}.image".format(i) for i in range(20))
        findings = validate(_graph(**_inspection(reference_payload_path=images)))
        assert _codes(findings) == [LIST_INVALID]
        assert "lists 20 images" in _reference_findings(findings)[0].message


# ---------------------------------------------------------------------------
# AC-37: per-line findings, the model warning, payload lines and triggers
# ---------------------------------------------------------------------------

class TestPerLineFindings:
    def test_the_finding_names_the_line(self):
        value = ("file:///aws_dda/reference/legend.jpg\n"
                 "file:///aws_dda/other/sheet.pdf\n"
                 "refs.0.image")
        findings = validate(_graph(**_inspection(
            reference_payload_path=value,
            allowed_uri_prefixes="file:///aws_dda/reference/")))
        assert _codes(findings) == [URI_NOT_ALLOWED]
        assert _reference_findings(findings)[0].message.startswith(
            "Node 'bed': line 2 of 'reference_payload_path' is the fixed URI "
            "file:///aws_dda/other/sheet.pdf but")

    def test_each_line_is_reported_at_most_once(self):
        value = "file://plant-host/a.jpg\ns3://x/b.jpg"
        findings = validate(_graph(**_inspection(
            reference_payload_path=value, allowed_uri_prefixes="file:///r/")))
        messages = [f.message for f in _reference_findings(findings)]
        assert len(messages) == 2
        assert "line 1 of" in messages[0] and "line 2 of" in messages[1]

    def test_document_with_an_unverified_model_is_a_warning(self):
        value = "file:///aws_dda/reference/legend.jpg\nfile:///aws_dda/reference/sheet.pdf"
        findings = validate(_graph(**_inspection(
            model="qwen.qwen3-vl-235b-a22b", reference_payload_path=value,
            allowed_uri_prefixes="file:///aws_dda/reference/")))
        assert _codes(findings) == [MODEL_UNVERIFIED]
        (finding,) = _reference_findings(findings)
        assert finding.severity == SEVERITY_WARNING
        assert finding.message == (
            "Node 'bed' sends documents, but this Portal does not know whether "
            "model 'qwen.qwen3-vl-235b-a22b' accepts documents in Bedrock "
            "Converse; if it does not, every run records an error.")

    def test_document_capable_models_give_no_warning(self):
        value = "file:///aws_dda/reference/sheet.pdf"
        for model in ("us.amazon.nova-pro-v1:0", "amazon.nova-lite-v1:0",
                      "us.anthropic.claude-sonnet-4-20250514-v1:0", None):
            parameters = _inspection(reference_payload_path=value,
                                     allowed_uri_prefixes="file:///aws_dda/reference/")
            if model is not None:
                parameters["model"] = model
            assert _codes(validate(_graph(**parameters))) == [], model
        parameters["model"] = "amazon.titan-image-generator-v2:0"
        assert _codes(validate(_graph(**parameters))) == [MODEL_UNVERIFIED]

    def test_images_only_with_any_model_give_no_warning(self):
        findings = validate(_graph(**_inspection(
            model="qwen.qwen3-vl-235b-a22b",
            reference_payload_path="refs.0.image\nrefs.1.image")))
        assert _codes(findings) == []
        findings = validate(_graph(**_inspection(
            model="qwen.qwen3-vl-235b-a22b",
            reference_payload_path="refs.0.image\ns3://plant-images/sheet.pdf")))
        assert _codes(findings) == [MODEL_UNVERIFIED]

    def test_no_trigger_fires_only_for_a_payload_line(self):
        fixed = ("file:///aws_dda/reference/legend.jpg\n"
                 "file:///aws_dda/reference/sheet.pdf")
        findings = validate(_graph(trigger=False, **_inspection(
            reference_payload_path=fixed,
            allowed_uri_prefixes="file:///aws_dda/reference/")))
        assert _codes(findings) == []
        findings = validate(_graph(trigger=False, **_inspection(
            reference_payload_path=fixed + "\nrefs.0.image\nrefs.1.image",
            allowed_uri_prefixes="file:///aws_dda/reference/")))
        assert _codes(findings) == [CODE_BEDROCK_PAYLOAD_NO_TRIGGER]

    def test_inspection_inputs_message(self):
        findings = validate(_graph(fed_reference=True, **_inspection()))
        (finding,) = [f for f in findings
                      if f.code == CODE_BEDROCK_INSPECTION_INPUTS]
        assert finding.message.endswith(
            "— the inspection answer judges the whole captured frame, and its "
            "reference image can only come from reference_payload_path")


# ---------------------------------------------------------------------------
# AC-42 (validator half): upload lines
# ---------------------------------------------------------------------------

MALFORMED_UPLOAD_LINES = (
    "upload://" + HEX.upper() + ".png",
    "upload://" + HEX[:63] + ".png",
    "upload://" + HEX + ".gif",
    "upload://" + HEX[:32] + "/" + HEX[32:] + ".png",
)


class TestUploadLines:
    def test_a_malformed_upload_line_is_a_list_error_naming_its_line(self):
        for line in MALFORMED_UPLOAD_LINES:
            findings = validate(_graph(**_inspection(
                reference_payload_path="refs.0.image\n" + line)))
            assert _codes(findings) == [LIST_INVALID], line
            assert _reference_findings(findings)[0].message == (
                "Node 'bed': line 2 of reference_payload_path is not a valid "
                "uploaded-file reference: expected upload://<64 lowercase hex "
                "characters>.<jpg|png|pdf|txt|md|csv|html|doc|docx|xls|xlsx>")

    def test_a_missing_upload_is_reported_only_with_reference_uploads(self):
        value = "upload://{0}.png\nupload://{1}.pdf".format(HEX, OTHER_HEX)
        graph = _graph(**_inspection(reference_payload_path=value))
        assert _codes(validate(graph)) == []
        findings = validate(graph, reference_uploads={HEX + ".png"})
        assert _codes(findings) == [UPLOAD_MISSING]
        (finding,) = _reference_findings(findings)
        assert finding.severity == SEVERITY_ERROR
        assert finding.message == (
            "Node 'bed': line 2 of 'reference_payload_path' names an uploaded "
            "file that this workflow does not store (upload://ffffffffffff"
            "\u2026.pdf); upload the file again in this workflow")
        assert _codes(validate(
            graph, reference_uploads={HEX + ".png", OTHER_HEX + ".pdf"})) == []

    def test_upload_lines_need_no_prefix_entry_and_no_trigger(self):
        value = "upload://{0}.png\nupload://{0}.pdf".format(HEX)
        findings = validate(_graph(trigger=False, **_inspection(
            reference_payload_path=value,
            allowed_uri_prefixes="s3://plant-images/")))
        assert _codes(findings) == []
        findings = validate(_graph(trigger=False, **_inspection(
            reference_payload_path="upload://{0}.jpg".format(HEX))))
        assert _codes(findings) == []

    def test_reference_conflict_still_applies_to_upload_lines(self):
        """Preservation: any reference line plus a connected reference
        port is BEDROCK_REFERENCE_CONFLICT."""
        findings = validate(_graph(
            fed_reference=True,
            reference_payload_path="upload://{0}.jpg".format(HEX)))
        assert CODE_BEDROCK_REFERENCE_CONFLICT in _codes(findings)


# ---------------------------------------------------------------------------
# AC-37: catalog texts and the compiler
# ---------------------------------------------------------------------------

def _bedrock_parameter(name):
    return {p.name: p for p in get_node_type("bedrock_inference").parameters}[name]


class TestCatalog:
    def test_reference_parameters_are_multiline(self):
        assert _bedrock_parameter("reference_payload_path").multiline is True
        assert _bedrock_parameter("allowed_uri_prefixes").multiline is True

    def test_reference_payload_path_describes_the_list_form(self):
        parameter = _bedrock_parameter("reference_payload_path")
        description = parameter.description
        for phrase in ("one per line", '"Reference 1"', '"Reference image"',
                       ".pdf", ".txt", ".md", ".csv", ".html", ".doc", ".docx",
                       ".xls", ".xlsx", "answer_format inspection",
                       "upload://<sha256>.<ext>",
                       '"Workflow tuning for inspection-format Bedrock nodes, '
                       'reference files and live tuning configuration"',
                       "dotted path", "refs.0.image", "s3://", "http(s)://",
                       "base64"):
            assert phrase in description, phrase
        assert parameter.examples == [
            "refs.0.image", "reference.image_url",
            "file:///aws_dda/reference-images/legend.jpg",
            "file:///aws_dda/reference/legend.jpg\n"
            "file:///aws_dda/reference/assembly.pdf"]

    def test_allowed_uri_prefixes_names_both_forms_and_uploads(self):
        description = _bedrock_parameter("allowed_uri_prefixes").description
        for phrase in ("newline-separated", "empty permits any source",
                       "A file:// entry names a directory",
                       "Files uploaded in the workflow editor need no entry",
                       "setting prefixes for the sources you use is recommended"):
            assert phrase in description, phrase

    def test_answer_format_and_max_image_dimension_texts(self):
        assert "Reference files, if configured, are attached after the input " \
               "image" in _bedrock_parameter("answer_format").description
        assert "never upscaled" in _bedrock_parameter(
            "max_image_dimension").description
        assert "the reference image" in _bedrock_parameter(
            "max_image_dimension").description

    def test_llm_max_image_dimension_is_untouched(self):
        """Preservation: only the bedrock_inference text moved."""
        llm = {p.name: p for p in get_node_type("llm_inference").parameters}
        assert llm["max_image_dimension"].description == (
            "Optional maximum pixel dimension for the longer edge of captured "
            "frames sent to the model. Larger frames are downscaled (aspect "
            "preserved) before inference to cut image-token prefill cost; "
            "smaller frames are never upscaled. Empty sends frames unmodified.")


class TestCompilerCarriesTheList:
    def test_a_three_line_value_reaches_the_binding_unchanged(self):
        """Preservation: the compiler copies a multi-line value, upload
        line included, onto the executor binding verbatim."""
        value = ("upload://{0}.png\r\n  file:///aws_dda/reference/sheet.pdf\n"
                 "refs.0.image").format(HEX)
        graph = WorkflowGraph(
            nodes=[
                _node("cam", "icam_source"),
                _node("bedrock", "bedrock_inference", answer_format="inspection",
                      reference_payload_path=value,
                      allowed_uri_prefixes="file:///aws_dda/reference/"),
                _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                      topic="plant/line1/result"),
            ],
            connections=[
                Connection(id="c1", source=PortEndpoint("cam", "out"),
                           target=PortEndpoint("bedrock", "in")),
                Connection(id="c2", source=PortEndpoint("bedrock", "out"),
                           target=PortEndpoint("mqtt", "in")),
            ],
        )
        document = compile(graph, "arm64_jp5")
        assert isinstance(document, CompiledPipelineDocument), document
        (binding,) = [b for b in document.executor_bindings
                      if b["binding"] == "bedrock_inference"]
        assert binding["parameters"]["reference_payload_path"] == value
        assert binding["parameters"]["allowed_uri_prefixes"] == \
            "file:///aws_dda/reference/"
