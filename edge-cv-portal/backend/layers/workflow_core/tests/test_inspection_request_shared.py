"""Shared inspection request preparation and reference lists
(inspection-format tuning fix, design §5, §6 and §6.1).

Layer halves of FR-1 (the tunability rule), FR-4.1 (the renderers moved
from the device and the request preparation the Portal's replay shares
with the device), AC-28 and AC-29 (order, labels, blocks and kinds of a
list of reference files), the upload-line rules of AC-42 and
``document_signature_problem``.

Names that are new in this fix are imported inside the tests, so on the
device's commit each such test fails on its own with the ImportError (or
the TypeError of the three-argument ``is_tunable_node``) naming the
missing interface. Tests marked "Preservation" pin behaviour that holds
before and after the fix.
"""

import pytest

from workflow_core.anomaly_invocation import (
    ANOMALY_INSTRUCTION_SEPARATOR,
    BEDROCK_DEFAULT_MODEL,
    INPUT_IMAGE_LABEL,
    INSPECTION_JSON_INSTRUCTION,
    NODE_TYPE_BEDROCK_INFERENCE,
    NODE_TYPE_LLM_INFERENCE,
    REFERENCE_IMAGE_LABEL,
    build_bedrock_invocation,
    is_anomaly_mode,
    is_tunable_node,
)

INPUT = b"\xff\xd8input-frame"
LEGEND = b"\xff\xd8legend"
NOTES = "Part list: front fender, rear fender.\n".encode("utf-8")
SHEET = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
HEX = "0123456789abcdef" * 4

#: The anomaly_mode values of the existing eligibility case table.
ANOMALY_MODE_VALUES = (None, True, False, "true", "false", "TRUE", " false ",
                       "", "yes", 0, 1, 0.0, "0", "1", [], {})
#: answer_format values crossed with them; ABSENT = two-argument call.
ABSENT = object()
ANSWER_FORMAT_VALUES = (ABSENT, "inspection", "legacy", "Inspection",
                        " inspection", "", None)
NODE_TYPES = (NODE_TYPE_BEDROCK_INFERENCE, NODE_TYPE_LLM_INFERENCE,
              "model_inference", None)

INSPECTION = {
    "model": "us.amazon.nova-pro-v1:0",
    "region": "us-west-2",
    "prompt": "Inspect the assembly.",
    "answer_format": "inspection",
    "max_tokens": 600,
}


def _content(kwargs):
    return kwargs["messages"][0]["content"]


# ---------------------------------------------------------------------------
# FR-1: tunability
# ---------------------------------------------------------------------------

class TestTunability:
    def test_inspection_format_bedrock_node_is_tunable_for_every_anomaly_mode(self):
        for anomaly_mode in ANOMALY_MODE_VALUES:
            assert is_tunable_node(
                NODE_TYPE_BEDROCK_INFERENCE, anomaly_mode, "inspection") is True

    def test_rule_crossed_with_answer_format(self):
        for node_type in NODE_TYPES:
            for anomaly_mode in ANOMALY_MODE_VALUES:
                for answer_format in ANSWER_FORMAT_VALUES:
                    if answer_format is ABSENT:
                        actual = is_tunable_node(node_type, anomaly_mode)
                        inspection = False
                    else:
                        actual = is_tunable_node(
                            node_type, anomaly_mode, answer_format)
                        inspection = answer_format == "inspection"
                    expected = (
                        (node_type == NODE_TYPE_BEDROCK_INFERENCE and inspection)
                        or is_anomaly_mode(node_type, anomaly_mode))
                    assert actual is expected, (
                        node_type, anomaly_mode, answer_format)

    def test_inspection_on_an_llm_node_changes_nothing(self):
        assert is_tunable_node(NODE_TYPE_LLM_INFERENCE, None, "inspection") is False
        assert is_tunable_node(NODE_TYPE_LLM_INFERENCE, True, "inspection") is True

    def test_two_argument_calls_are_unchanged(self):
        """Preservation: two-argument callers get is_anomaly_mode."""
        for node_type in NODE_TYPES:
            for anomaly_mode in ANOMALY_MODE_VALUES:
                assert is_tunable_node(node_type, anomaly_mode) is \
                    is_anomaly_mode(node_type, anomaly_mode)

    def test_is_inspection_format_is_the_executors_comparison(self):
        from workflow_core.anomaly_invocation import (
            ANSWER_FORMAT_PARAMETER,
            is_inspection_format,
        )
        assert ANSWER_FORMAT_PARAMETER == "answer_format"
        assert is_inspection_format({"answer_format": "inspection"}) is True
        for value in ("Inspection", " inspection", "legacy", "", None):
            assert is_inspection_format({"answer_format": value}) is False
        assert is_inspection_format({}) is False


# ---------------------------------------------------------------------------
# FR-4.1: the renderers moved from the device
# ---------------------------------------------------------------------------

class TestMovedRenderers:
    def test_render_prompt_substitutes_dotted_names_and_escapes(self):
        from workflow_core.anomaly_invocation import render_prompt
        values = {"a": {"b": 2}, "name": "x"}
        assert render_prompt("{name}-{a.b} {{lit}} }} {", values) == \
            "x-2 {lit} } {"

    def test_render_prompt_raises_naming_the_placeholder(self):
        from workflow_core.anomaly_invocation import (
            UnresolvedPlaceholderError,
            render_prompt,
        )
        with pytest.raises(UnresolvedPlaceholderError) as raised:
            render_prompt("{a.missing}", {"a": {}})
        assert raised.value.name == "a.missing"
        assert str(raised.value) == "unresolved placeholder a.missing"
        assert not isinstance(raised.value, ValueError)

    def test_placeholder_regex(self):
        from workflow_core.anomaly_invocation import PLACEHOLDER_RE
        assert PLACEHOLDER_RE.pattern == r'\{([A-Za-z_][A-Za-z0-9_.]*)\}'

    def test_render_prompt_text_inserts_compact_json(self):
        from workflow_core.anomaly_invocation import render_prompt_text
        values = {"trigger": {"payload_json": {
            "items": [{"shape": "roof", "color": "gold"}],
            "label": "Größe", "scale": 2.0}}}
        text = render_prompt_text(
            "Items {json:trigger.payload_json.items} at "
            "{trigger.payload_json.scale} {json:trigger.payload_json.label}",
            values)
        assert text == ('Items [{"shape":"roof","color":"gold"}] at 2.0 '
                        '"Größe"')

    def test_render_prompt_text_never_rescans_substituted_text(self):
        from workflow_core.anomaly_invocation import render_prompt_text
        values = {"v": "{w}", "j": {"k": "{w}"}}
        assert render_prompt_text("{v} {json:j}", values) == '{w} {"k":"{w}"}'

    def test_render_prompt_text_unresolved_json_token(self):
        from workflow_core.anomaly_invocation import (
            UnresolvedPlaceholderError,
            render_prompt_text,
        )
        with pytest.raises(UnresolvedPlaceholderError) as raised:
            render_prompt_text("{json:trigger.nope}", {"trigger": {}})
        assert raised.value.name == "json:trigger.nope"

    def test_resolve_field_path(self):
        from workflow_core.anomaly_invocation import resolve_field_path
        document = {"items": [{"id": 7}], "none": None}
        assert resolve_field_path(document, "items.0.id") == (True, 7)
        assert resolve_field_path(document, " none ") == (True, None)
        assert resolve_field_path(document, "items.1.id") == (False, None)
        assert resolve_field_path(document, "items.x") == (False, None)
        assert resolve_field_path(document, "") == (False, None)
        assert resolve_field_path(document, None) == (False, None)

    def test_resolve_box_scale_and_expected_count(self):
        from workflow_core.anomaly_invocation import (
            resolve_box_scale,
            resolve_expected_count,
        )
        assert resolve_box_scale(None, "us.amazon.nova-pro-v1:0") == "normalized_1000"
        assert resolve_box_scale("auto", "anthropic.claude-x") == "pixels"
        assert resolve_box_scale(" pixels ", "us.amazon.nova-pro-v1:0") == "pixels"
        assert resolve_expected_count(True, [1, 2, 3]) == 3
        assert resolve_expected_count(True, 2) == 2
        for found, value in ((True, []), (True, 0), (True, -1), (True, True),
                             (True, "3"), (False, [1])):
            assert resolve_expected_count(found, value) is None


# ---------------------------------------------------------------------------
# FR-4.1: inspection request preparation
# ---------------------------------------------------------------------------

METADATA = {
    "trigger": {"payload_json": {
        "order_id": "o-1",
        "body": {"qc_stage": "qc2", "order": {"items": [
            {"shape": "front_fender", "color": "blue"},
            {"shape": "roof", "color": "gold"}]}}}},
}


class TestPrepareInspectionRequest:
    def test_expected_count(self):
        from workflow_core.anomaly_invocation import (
            InspectionRequestError,
            inspection_expected_count,
        )
        assert issubclass(InspectionRequestError, ValueError)
        assert inspection_expected_count({}, METADATA) is None
        assert inspection_expected_count(
            {"expected_count_path": "  "}, METADATA) is None
        assert inspection_expected_count(
            {"expected_count_path": " trigger.payload_json.body.order.items "},
            METADATA) == 2
        with pytest.raises(InspectionRequestError) as raised:
            inspection_expected_count(
                {"expected_count_path": "trigger.payload_json.body.nope"},
                METADATA)
        assert str(raised.value) == (
            "expected count path trigger.payload_json.body.nope did not "
            "resolve to a positive count")

    def test_render_off_returns_the_same_mapping(self):
        from workflow_core.anomaly_invocation import render_inspection_parameters
        for raw in (None, "", "  ", False, "false", 0):
            parameters = dict(INSPECTION, prompt="{trigger.nope}",
                              render_prompt=raw)
            if raw is None:
                parameters.pop("render_prompt")
            assert render_inspection_parameters(
                parameters, METADATA, (640, 480)) is parameters

    def test_render_on_renders_prompt_and_system_prompt(self):
        from workflow_core.anomaly_invocation import render_inspection_parameters
        parameters = dict(
            INSPECTION, render_prompt="true",
            prompt="Order {trigger.payload_json.order_id}: "
                   "{json:trigger.payload_json.body.order.items} "
                   "{image.width}x{image.height}",
            system_prompt="Stage {trigger.payload_json.body.qc_stage}")
        rendered = render_inspection_parameters(parameters, METADATA, (640, 480))
        assert rendered is not parameters
        assert rendered["prompt"] == (
            'Order o-1: [{"shape":"front_fender","color":"blue"},'
            '{"shape":"roof","color":"gold"}] 640x480')
        assert rendered["system_prompt"] == "Stage qc2"
        assert parameters["prompt"].startswith("Order {trigger")
        assert "image" not in METADATA

    def test_blank_system_prompt_is_not_rendered(self):
        from workflow_core.anomaly_invocation import render_inspection_parameters
        parameters = dict(INSPECTION, render_prompt=True, system_prompt="  ")
        assert render_inspection_parameters(
            parameters, METADATA, None)["system_prompt"] == "  "

    def test_unresolved_placeholder_message(self):
        from workflow_core.anomaly_invocation import (
            InspectionRequestError,
            render_inspection_parameters,
        )
        parameters = dict(INSPECTION, render_prompt=True,
                          prompt="{image.width} {trigger.payload_json.nope}")
        with pytest.raises(InspectionRequestError) as raised:
            render_inspection_parameters(parameters, METADATA, None)
        assert str(raised.value) == "unresolved prompt placeholder image.width"
        parameters["prompt"] = "{json:trigger.nope}"
        with pytest.raises(InspectionRequestError) as raised:
            render_inspection_parameters(parameters, METADATA, (1, 1))
        assert str(raised.value) == "unresolved prompt placeholder json:trigger.nope"

    def test_prepare_runs_the_executors_order(self):
        from workflow_core.anomaly_invocation import (
            InspectionRequest,
            InspectionRequestError,
            prepare_inspection_request,
        )
        parameters = dict(INSPECTION, render_prompt=True,
                          prompt="{trigger.nope}",
                          expected_count_path="trigger.nope")
        with pytest.raises(InspectionRequestError) as raised:
            prepare_inspection_request(parameters, METADATA, (10, 10))
        assert str(raised.value).startswith("expected count path")
        parameters = dict(INSPECTION, render_prompt=True,
                          prompt="{image.height}",
                          expected_count_path="trigger.payload_json.body.order.items")
        request = prepare_inspection_request(parameters, METADATA, (10, 20))
        assert isinstance(request, InspectionRequest)
        assert request.parameters["prompt"] == "20"
        assert request.expected_count == 2
        assert request.box_scale == "normalized_1000"

    def test_box_scale_uses_the_effective_model(self):
        from workflow_core.anomaly_invocation import prepare_inspection_request
        assert "amazon.nova" in BEDROCK_DEFAULT_MODEL
        assert prepare_inspection_request(
            {"answer_format": "inspection"}, {}, None).box_scale == "normalized_1000"
        assert prepare_inspection_request(
            {"model": "anthropic.claude-x"}, {}, None).box_scale == "pixels"
        assert prepare_inspection_request(
            {"box_scale": "pixels"}, {}, None).box_scale == "pixels"

    def test_prompt_field_paths(self):
        from workflow_core.anomaly_invocation import prompt_field_paths
        text = ("{trigger.a} {{escaped}} {json:trigger.b.0} {trigger.a} "
                "}}{c} {{{d}}} { e } {json:bad-token} {image.width}")
        assert prompt_field_paths(text) == (
            "trigger.a", "trigger.b.0", "c", "d", "image.width")
        assert prompt_field_paths(None) == ()
        assert prompt_field_paths("{{x}}") == ()

    def test_inspection_metadata_roots(self):
        from workflow_core.anomaly_invocation import inspection_metadata_roots
        assert inspection_metadata_roots({}) == ("trigger",)
        assert inspection_metadata_roots({
            "prompt": "{station.id} {json:order.items} {image.width} {{zz}}",
            "system_prompt": "{batch}",
            "expected_count_path": " lot.items ",
        }) == ("batch", "lot", "order", "station", "trigger")

    def test_omitted_path_conflict(self):
        from workflow_core.anomaly_invocation import omitted_path_conflict
        paths = ("trigger.payload_json.refs.0.image", "trigger.payload_json.id")
        assert omitted_path_conflict(paths, ()) is None
        assert omitted_path_conflict(paths, ("trigger.payload_json.idx",)) is None
        assert omitted_path_conflict(
            paths, ("trigger.payload_json.refs",)) == "trigger.payload_json.refs"
        assert omitted_path_conflict(
            paths, ("trigger.payload_json.id",)) == "trigger.payload_json.id"
        assert omitted_path_conflict(
            ("trigger.payload_json",),
            ("x.y", "trigger.payload_json.refs.0.image")) == \
            "trigger.payload_json.refs.0.image"

    def test_inline_and_static_reference_values(self):
        from workflow_core.anomaly_invocation import (
            REFERENCE_URI_SCHEMES,
            is_inline_reference_value,
            is_static_reference,
        )
        assert REFERENCE_URI_SCHEMES == ("s3://", "http://", "https://", "file://")
        assert is_inline_reference_value("data:image/png;base64,iVBORw0KGgo=")
        assert is_inline_reference_value("iVBORw0KGgo=")
        assert not is_inline_reference_value("s3://bucket/key.jpg")
        assert not is_inline_reference_value(b"bytes")
        assert is_static_reference(" file:///aws_dda/images/source.jpg ")
        assert is_static_reference("https://host/a.png")
        assert not is_static_reference("S3://bucket/key.jpg")
        assert not is_static_reference("refs.0.image")
        assert not is_static_reference("upload://" + HEX + ".png")
        assert not is_static_reference(None)

    def test_inspection_verdict(self):
        from workflow_core.anomaly_invocation import inspection_verdict
        assert inspection_verdict({"verdict": "fail", "confidence": 1}) == {
            "is_anomalous": True, "confidence": 1.0, "verdict": "fail"}
        assert inspection_verdict({"verdict": "pass", "confidence": 0.5}) == {
            "is_anomalous": False, "confidence": 0.5, "verdict": "pass"}


# ---------------------------------------------------------------------------
# AC-28: order, labels and blocks of a list of reference files
# ---------------------------------------------------------------------------

def _files(*items):
    from workflow_core.anomaly_invocation import ReferenceFile
    return [ReferenceFile(kind, fmt, data) for kind, fmt, data in items]


class TestReferenceListRequest:
    def test_image_txt_pdf_list_gives_nine_blocks(self):
        from workflow_core.anomaly_invocation import Attachment
        references = _files(("image", "jpeg", LEGEND), ("document", "txt", NOTES),
                            ("document", "pdf", SHEET))
        invocation = build_bedrock_invocation(INSPECTION, INPUT, references=references)
        content = _content(invocation.converse_kwargs())
        assert len(content) == 9
        assert content == [
            {"text": "Inspect the assembly." + ANOMALY_INSTRUCTION_SEPARATOR
                     + INSPECTION_JSON_INSTRUCTION},
            {"text": "Input image:"},
            {"image": {"format": "jpeg", "source": {"bytes": INPUT}}},
            {"text": "Reference 1:"},
            {"image": {"format": "jpeg", "source": {"bytes": LEGEND}}},
            {"text": "Reference 2:"},
            {"document": {"format": "txt", "name": "Reference 2",
                          "source": {"bytes": NOTES}}},
            {"text": "Reference 3:"},
            {"document": {"format": "pdf", "name": "Reference 3",
                          "source": {"bytes": SHEET}}},
        ]
        assert invocation.attachments[0] == Attachment(
            INPUT_IMAGE_LABEL, "image", "jpeg", INPUT)
        assert invocation.images == ((INPUT_IMAGE_LABEL, INPUT),
                                     ("Reference 1", LEGEND))
        assert invocation.image_count == 2
        assert invocation.document_count == 2

    def test_one_image_list_is_todays_two_image_request(self):
        single = build_bedrock_invocation(INSPECTION, INPUT, LEGEND)
        listed = build_bedrock_invocation(
            INSPECTION, INPUT, references=_files(("image", "jpeg", LEGEND)))
        assert listed == single
        assert listed.attachments == ()
        assert listed.converse_kwargs() == single.converse_kwargs()
        assert listed.images[1][0] == REFERENCE_IMAGE_LABEL
        assert listed.image_count == 2 and listed.document_count == 0

    def test_one_pdf_list_is_reference_1_document(self):
        invocation = build_bedrock_invocation(
            INSPECTION, INPUT, references=_files(("document", "pdf", SHEET)))
        content = _content(invocation.converse_kwargs())
        assert content[1:] == [
            {"text": "Input image:"},
            {"image": {"format": "jpeg", "source": {"bytes": INPUT}}},
            {"text": "Reference 1:"},
            {"document": {"format": "pdf", "name": "Reference 1",
                          "source": {"bytes": SHEET}}},
        ]
        assert invocation.image_count == 1

    def test_reference_image_and_references_are_exclusive(self):
        with pytest.raises(ValueError):
            build_bedrock_invocation(INSPECTION, INPUT, LEGEND,
                                     references=_files(("image", "jpeg", LEGEND)))

    def test_empty_references_is_the_single_image_request(self):
        assert build_bedrock_invocation(INSPECTION, INPUT, references=[]) == \
            build_bedrock_invocation(INSPECTION, INPUT)

    def test_single_image_request_is_byte_identical(self):
        """Preservation: the one-reference and no-reference requests keep
        today's exact converse kwargs."""
        prompt = ("Inspect the assembly." + ANOMALY_INSTRUCTION_SEPARATOR
                  + INSPECTION_JSON_INSTRUCTION)
        two = build_bedrock_invocation(
            dict(INSPECTION, system_prompt="Be strict."), INPUT, LEGEND)
        assert two.converse_kwargs() == {
            "modelId": "us.amazon.nova-pro-v1:0",
            "messages": [{"role": "user", "content": [
                {"text": prompt},
                {"text": "Input image:"},
                {"image": {"format": "jpeg", "source": {"bytes": INPUT}}},
                {"text": "Reference image:"},
                {"image": {"format": "jpeg", "source": {"bytes": LEGEND}}},
            ]}],
            "inferenceConfig": {"maxTokens": 600},
            "system": [{"text": "Be strict."}],
        }
        legacy = build_bedrock_invocation({"prompt": "Is it OK?"}, INPUT)
        assert list(legacy.converse_kwargs()) == [
            "modelId", "messages", "inferenceConfig"]
        assert _content(legacy.converse_kwargs())[1:] == [
            {"text": "Input image:"},
            {"image": {"format": "jpeg", "source": {"bytes": INPUT}}},
        ]
        assert legacy.images == ((INPUT_IMAGE_LABEL, INPUT),)


class TestParseReferenceList:
    def test_three_origins_kinds_and_labels(self):
        from workflow_core.anomaly_invocation import parse_reference_list
        specs = parse_reference_list(
            "file:///tmp/legend.png\nrefs.0.image\r\nupload://" + HEX
            + ".pdf\rs3://plant-images/notes.TXT")
        assert [(s.index, s.text, s.static, s.uploaded, s.upload_name,
                 s.kind, s.format, s.label) for s in specs] == [
            (1, "file:///tmp/legend.png", True, False, None, "image", "jpeg",
             "Reference 1"),
            (2, "refs.0.image", False, False, None, "image", "jpeg",
             "Reference 2"),
            (3, "upload://" + HEX + ".pdf", True, True, HEX + ".pdf",
             "document", "pdf", "Reference 3"),
            (4, "s3://plant-images/notes.TXT", True, False, None, "document",
             "txt", "Reference 4"),
        ]

    def test_blank_lines_and_spaces_give_the_trimmed_list(self):
        from workflow_core.anomaly_invocation import parse_reference_list
        trimmed = parse_reference_list("file:///r/legend.png\nfile:///r/sheet.pdf")
        assert parse_reference_list(
            "  file:///r/legend.png  \n\n   \r\n\tfile:///r/sheet.pdf \n") == trimmed
        assert [s.label for s in trimmed] == ["Reference 1", "Reference 2"]

    def test_single_image_line_is_the_reference_image(self):
        from workflow_core.anomaly_invocation import parse_reference_list
        (spec,) = parse_reference_list("refs.0.image")
        assert spec.label == REFERENCE_IMAGE_LABEL and spec.kind == "image"
        (spec,) = parse_reference_list(" file:///aws_dda/images/source.jpg ")
        assert spec.label == REFERENCE_IMAGE_LABEL and spec.static

    def test_blank_or_non_string_values(self):
        from workflow_core.anomaly_invocation import parse_reference_list
        for value in (None, "", "  \n \r\n", 5, ["refs.0.image"]):
            assert parse_reference_list(value) == ()

    def test_reference_labels(self):
        from workflow_core.anomaly_invocation import reference_labels
        assert reference_labels(("image",)) == ("Reference image",)
        assert reference_labels(("document",)) == ("Reference 1",)
        assert reference_labels(("image", "image", "document")) == (
            "Reference 1", "Reference 2", "Reference 3")
        assert reference_labels(()) == ()


# ---------------------------------------------------------------------------
# AC-29: kinds and document checks
# ---------------------------------------------------------------------------

class TestKindsAndDocumentChecks:
    def test_every_document_extension_maps_case_insensitively(self):
        from workflow_core.anomaly_invocation import (
            DOCUMENT_FORMATS_BY_EXTENSION,
            document_format_of,
        )
        assert DOCUMENT_FORMATS_BY_EXTENSION == {
            ".pdf": "pdf", ".txt": "txt", ".md": "md", ".markdown": "md",
            ".csv": "csv", ".html": "html", ".htm": "html", ".doc": "doc",
            ".docx": "docx", ".xls": "xls", ".xlsx": "xlsx"}
        for extension, fmt in DOCUMENT_FORMATS_BY_EXTENSION.items():
            for scheme in ("file:///r/", "s3://bucket/k/", "https://h/x/"):
                assert document_format_of(scheme + "a" + extension) == fmt
                assert document_format_of(
                    scheme + "a" + extension.upper() + "?v=1#p") == fmt

    def test_query_and_fragment_are_ignored(self):
        from workflow_core.anomaly_invocation import document_format_of
        assert document_format_of("https://h/sheet.pdf?X-Amz-Expires=60") == "pdf"
        assert document_format_of("https://h/view?file=sheet.pdf") is None
        assert document_format_of("https://h/a#sheet.pdf") is None

    def test_images_and_payload_paths_are_not_documents(self):
        from workflow_core.anomaly_invocation import document_format_of
        for text in ("refs.0.pdf", "file:///r/legend.jpg", "file:///r/legend.png",
                     "s3://bucket/noext", "https://host.pdf", None,
                     "upload://" + HEX + ".png", "upload://bad.pdf"):
            assert document_format_of(text) is None, text
        assert document_format_of("upload://" + HEX + ".docx") == "docx"

    def test_payload_reference_named_like_a_pdf_is_an_image(self):
        from workflow_core.anomaly_invocation import parse_reference_list
        (spec,) = parse_reference_list("refs.0.pdf")
        assert (spec.kind, spec.format, spec.static) == ("image", "jpeg", False)

    def test_document_signature_problems(self):
        from workflow_core.anomaly_invocation import document_signature_problem
        assert document_signature_problem("pdf", SHEET) is None
        assert document_signature_problem("pdf", b"PDF-1.4") == \
            "it does not start with %PDF-"
        assert document_signature_problem("docx", b"PK\x03\x04rest") is None
        assert document_signature_problem("xlsx", b"%PDF-") == \
            "it is not a ZIP archive"
        ole2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest"
        assert document_signature_problem("doc", ole2) is None
        assert document_signature_problem("xls", b"PK\x03\x04") == \
            "it is not an OLE2 compound file"
        assert document_signature_problem("txt", b"\xff\xfe\x00bad") == \
            "it is not UTF-8 text"
        assert document_signature_problem("md", b"") == "it is empty"
        assert document_signature_problem("html", b" \n\t") == "it is empty"
        assert document_signature_problem(
            "csv", b"\xef\xbb\xbfpart,qty\nroof,1\n") is None
        assert document_signature_problem("txt", NOTES) is None
        with pytest.raises(ValueError):
            document_signature_problem("gif", b"GIF89a")

    def test_limits(self):
        from workflow_core import anomaly_invocation as ai
        assert (ai.MAX_DOCUMENT_REFERENCES, ai.MAX_IMAGE_REFERENCES,
                ai.MAX_DOCUMENT_BYTES, ai.MAX_SENT_IMAGE_BYTES,
                ai.MAX_IMAGE_SIDE_PX) == (5, 19, 4_500_000, 3_750_000, 8000)
        assert ai.LIST_REFERENCE_LABEL.format(n=2) == "Reference 2"
        assert (ai.KIND_IMAGE, ai.KIND_DOCUMENT) == ("image", "document")


# ---------------------------------------------------------------------------
# AC-42 (layer half): upload lines; list problems
# ---------------------------------------------------------------------------

MALFORMED_UPLOAD_LINES = (
    "upload://" + HEX.upper() + ".png",          # uppercase hexadecimal
    "upload://" + HEX[:63] + ".png",             # 63 characters
    "upload://" + HEX + ".gif",                  # not one of the extensions
    "upload://" + HEX[:32] + "/" + HEX[32:] + ".png",   # a / in the name
    "upload://" + HEX + ".jpeg",
)


class TestUploadLines:
    def test_constants(self):
        from workflow_core import anomaly_invocation as ai
        assert ai.UPLOAD_REFERENCE_SCHEME == "upload://"
        assert ai.UPLOAD_IMAGE_EXTENSIONS == ("jpg", "png")
        assert ai.UPLOAD_DOCUMENT_EXTENSIONS == (
            "pdf", "txt", "md", "csv", "html", "doc", "docx", "xls", "xlsx")
        assert ai.PACKAGED_REFERENCES_DIR == "references"
        assert ai.MANIFEST_REFERENCE_FILES_KEY == "referenceFiles"
        assert ai.UPLOAD_LINE_RE.pattern == (
            r"^upload://([0-9a-f]{64})\.(jpg|png|pdf|txt|md|csv|html|doc|docx"
            r"|xls|xlsx)$")

    def test_parse_upload_line(self):
        from workflow_core.anomaly_invocation import parse_upload_line
        assert parse_upload_line("upload://" + HEX + ".jpg") == (HEX, "jpg")
        assert parse_upload_line("upload://" + HEX + ".xlsx") == (HEX, "xlsx")
        for text in MALFORMED_UPLOAD_LINES + (
                "upload://" + HEX + ".png\n", " upload://" + HEX + ".png",
                "file:///" + HEX + ".png", None):
            assert parse_upload_line(text) is None, text

    def test_upload_kinds(self):
        from workflow_core.anomaly_invocation import parse_reference_list
        image, document = parse_reference_list(
            "upload://" + HEX + ".png\nupload://" + HEX + ".csv")
        assert (image.kind, image.format, image.upload_name) == (
            "image", "jpeg", HEX + ".png")
        assert (document.kind, document.format, document.upload_name) == (
            "document", "csv", HEX + ".csv")

    def test_malformed_upload_line_is_reported_with_its_line(self):
        from workflow_core.anomaly_invocation import (
            parse_reference_list,
            reference_list_problems,
        )
        for line in MALFORMED_UPLOAD_LINES:
            specs = parse_reference_list("refs.0.image\n" + line)
            assert (specs[1].uploaded, specs[1].upload_name, specs[1].kind,
                    specs[1].static) == (True, None, "image", True)
            assert reference_list_problems(specs, "inspection") == (
                "line 2 of reference_payload_path is not a valid uploaded-file "
                "reference: expected upload://<64 lowercase hex characters>."
                "<jpg|png|pdf|txt|md|csv|html|doc|docx|xls|xlsx>",)

    def test_uploaded_reference_names(self):
        from workflow_core.anomaly_invocation import uploaded_reference_names
        other = "f" * 64
        value = "\n".join(["upload://" + HEX + ".pdf", "refs.0.image",
                           "upload://" + other + ".png", MALFORMED_UPLOAD_LINES[0],
                           "upload://" + HEX + ".pdf"])
        assert uploaded_reference_names({"reference_payload_path": value}) == (
            HEX + ".pdf", other + ".png")
        assert uploaded_reference_names({}) == ()


class TestReferenceListProblems:
    def test_legacy_node_accepts_only_one_image(self):
        from workflow_core.anomaly_invocation import (
            parse_reference_list,
            reference_list_problems,
        )
        legacy_text = (
            "reference_payload_path lists {0} reference(s) or a document; only "
            "answer_format inspection accepts several references or documents")
        for answer_format in (None, "", "legacy", "Inspection"):
            assert reference_list_problems(parse_reference_list(
                "refs.0.image\nfile:///r/b.jpg"), answer_format) == (
                legacy_text.format(2),)
            assert reference_list_problems(parse_reference_list(
                "file:///r/sheet.pdf"), answer_format) == (legacy_text.format(1),)
            assert reference_list_problems(parse_reference_list(
                "refs.0.image"), answer_format) == ()
        assert reference_list_problems(parse_reference_list(
            "refs.0.image\nfile:///r/sheet.pdf"), "inspection") == ()

    def test_converse_limits(self):
        from workflow_core.anomaly_invocation import (
            parse_reference_list,
            reference_list_problems,
        )
        six_documents = "\n".join(
            "file:///r/d{0}.pdf".format(i) for i in range(6))
        assert reference_list_problems(
            parse_reference_list(six_documents), "inspection") == (
            "reference_payload_path lists 6 documents; Bedrock Converse "
            "accepts at most 5 per request",)
        twenty_images = "\n".join("refs.{0}.image".format(i) for i in range(20))
        assert reference_list_problems(
            parse_reference_list(twenty_images), "inspection") == (
            "reference_payload_path lists 20 images; with the input image that "
            "is more than the 20 images Bedrock Converse accepts per request",)
        nineteen = "\n".join("refs.{0}.image".format(i) for i in range(19))
        five = "\n".join("file:///r/d{0}.txt".format(i) for i in range(5))
        assert reference_list_problems(
            parse_reference_list(nineteen + "\n" + five), "inspection") == ()

    def test_problems_come_in_order(self):
        from workflow_core.anomaly_invocation import (
            parse_reference_list,
            reference_list_problems,
        )
        value = "\n".join(
            ["upload://bad.png"]
            + ["file:///r/d{0}.pdf".format(i) for i in range(6)]
            + ["refs.{0}.image".format(i) for i in range(19)])
        problems = reference_list_problems(parse_reference_list(value), "legacy")
        assert [p.split(";")[0].split(" is not")[0] for p in problems] == [
            "line 1 of reference_payload_path",
            "reference_payload_path lists 26 reference(s) or a document",
            "reference_payload_path lists 6 documents",
            "reference_payload_path lists 20 images",
        ]
