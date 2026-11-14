"""``additional_model_request_fields`` with reference lists, attachments,
the validator, the catalog and the compiler: the layer half of the
integration of the two features (design section 25).

- A list-form request carries the image and document blocks and
  ``additionalModelRequestFields``; the fields add that one key and change
  nothing else. One image reference with fields is the reference-image form.
- ``BedrockInvocation`` keeps main's field layout as a prefix:
  ``additional_model_request_fields`` is the ninth field and ``attachments``
  the tenth.
- The validator judges the reference list and the fields independently, one
  finding per rule; the catalog appends the fields parameter after the
  reference parameters, all three multiline; the compiled binding carries
  both values verbatim.

That the vendored device copy stays byte-identical to this layer is
``test/backend-test/workflow_engine/test_vendored_catalog_mirror.py``'s job.
The new names are imported inside the tests and the finding codes compared
as strings, so on c299a6e1 the tests run to their assertions. Tests whose
docstring starts "Preservation" pin behaviour that holds before and after.
"""
import dataclasses

from workflow_core.anomaly_invocation import (
    BedrockInvocation,
    build_bedrock_invocation,
    prompt_fingerprint,
)
from workflow_core.catalog import get_node_type
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import SEVERITY_ERROR, validate

FIELDS = "additional_model_request_fields"
#: A field Amazon Nova accepts (the catalog's second example).
TOP_K = '{"inferenceConfig": {"topK": 20}}'
TOP_K_OBJECT = {"inferenceConfig": {"topK": 20}}
INPUT = b"\xff\xd8\xffinput"
LEGEND = b"\xff\xd8\xfflegend"
NOTES = b"Reference 1 is the legend.\n"
SHEET = b"%PDF-1.4\n%%EOF\n"
INSPECTION = {"model": "us.amazon.nova-pro-v1:0", "region": "us-west-2",
              "prompt": "Inspect.", "answer_format": "inspection",
              "system_prompt": "Be terse.", "max_tokens": 600}
LIST = "\n".join(["file:///aws_dda/reference/legend.jpg",
                  "file:///aws_dda/reference/notes.txt",
                  "file:///aws_dda/reference/sheet.pdf"])
PREFIX = "file:///aws_dda/reference/"
LIST_BLOCKS = ["text", "text", "image", "text", "image", "text", "document",
               "text", "document"]
_POS = Position(0.0, 0.0)


def _references():
    from workflow_core.anomaly_invocation import (
        KIND_DOCUMENT,
        KIND_IMAGE,
        ReferenceFile,
    )
    return [ReferenceFile(KIND_IMAGE, "jpeg", LEGEND),
            ReferenceFile(KIND_DOCUMENT, "txt", NOTES),
            ReferenceFile(KIND_DOCUMENT, "pdf", SHEET)]


def _without_fields(kwargs):
    return {key: value for key, value in kwargs.items()
            if key != "additionalModelRequestFields"}


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source, target, target_port="in"):
    return Connection(id=conn_id, source=PortEndpoint(source, "out"),
                      target=PortEndpoint(target, target_port))


def _graph(**bedrock_parameters):
    """cam -> bedrock -> mqtt; ``bedrock`` carries the parameters."""
    return WorkflowGraph(
        nodes=[_node("cam", "icam_source"),
               _node("bedrock", "bedrock_inference", **bedrock_parameters),
               _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                     topic="plant/line1/result")],
        connections=[_conn("c1", "cam", "bedrock"),
                     _conn("c2", "bedrock", "mqtt")])


def _list_node(**extra):
    return dict(INSPECTION, reference_payload_path=LIST,
                allowed_uri_prefixes=PREFIX, **extra)


def _error_codes(graph):
    return sorted(f.code for f in validate(graph)
                  if f.severity == SEVERITY_ERROR)


class TestInvocation:
    def test_a_list_form_request_carries_documents_and_request_fields(self):
        with_fields = build_bedrock_invocation(
            dict(INSPECTION, **{FIELDS: TOP_K}), INPUT,
            references=_references())
        without = build_bedrock_invocation(INSPECTION, INPUT,
                                           references=_references())
        kwargs = with_fields.converse_kwargs()
        assert [next(iter(block)) for block
                in kwargs["messages"][0]["content"]] == LIST_BLOCKS
        assert kwargs["additionalModelRequestFields"] == TOP_K_OBJECT
        assert _without_fields(kwargs) == without.converse_kwargs()
        assert with_fields.attachments == without.attachments
        assert (with_fields.image_count, with_fields.document_count) == (2, 2)

    def test_one_image_reference_with_fields_is_the_reference_image_form(self):
        from workflow_core.anomaly_invocation import KIND_IMAGE, ReferenceFile
        parameters = dict(INSPECTION, **{FIELDS: TOP_K})
        listed = build_bedrock_invocation(
            parameters, INPUT, references=[ReferenceFile(KIND_IMAGE, "jpeg",
                                                         LEGEND)])
        assert listed == build_bedrock_invocation(parameters, INPUT, LEGEND)
        assert listed.attachments == ()
        assert listed.converse_kwargs()["additionalModelRequestFields"] == \
            TOP_K_OBJECT

    def test_mains_field_layout_stays_a_prefix(self):
        """additional_model_request_fields (main) is the ninth field and
        attachments (this branch) the tenth, so positional construction as
        on main keeps working."""
        names = [field.name for field in dataclasses.fields(BedrockInvocation)]
        assert names[7:] == ["answer_format", FIELDS, "attachments"]
        invocation = BedrockInvocation(
            "us.amazon.nova-pro-v1:0", "Inspect.", (("Input image", INPUT),),
            "us-west-2", 600, None, False, "inspection", '{"a":1}')
        assert invocation.converse_kwargs()["additionalModelRequestFields"] \
            == {"a": 1}

    def test_the_prompt_fingerprint_ignores_request_fields(self):
        """Preservation: the Prompt_Set fingerprint (prompt, system prompt,
        max tokens) does not change with the node's request fields, so a
        sample's recorded fingerprint still matches the Baseline_Candidate."""
        assert prompt_fingerprint(dict(INSPECTION, **{FIELDS: TOP_K})) == \
            prompt_fingerprint(INSPECTION)


class TestValidatorCatalogCompiler:
    def test_each_rule_judges_its_own_parameter(self):
        """A valid list with valid fields: no error. Invalid fields: only
        BEDROCK_REQUEST_FIELDS_JSON. An invalid list too: one finding of
        each code."""
        assert _error_codes(_graph(**_list_node(**{FIELDS: TOP_K}))) == []
        assert _error_codes(_graph(**_list_node(**{FIELDS: "[1]"}))) == [
            "BEDROCK_REQUEST_FIELDS_JSON"]
        six_documents = "\n".join(
            "{0}d{1}.pdf".format(PREFIX, index) for index in range(6))
        assert _error_codes(_graph(**dict(
            _list_node(**{FIELDS: "[1]"}),
            reference_payload_path=six_documents))) == [
            "BEDROCK_REFERENCE_LIST_INVALID", "BEDROCK_REQUEST_FIELDS_JSON"]

    def test_the_fields_parameter_follows_the_reference_parameters(self):
        parameters = get_node_type("bedrock_inference").parameters
        names = [parameter.name for parameter in parameters]
        assert names[-1] == FIELDS
        assert names.index("reference_payload_path") < names.index(FIELDS)
        multiline = {parameter.name for parameter in parameters
                     if parameter.multiline}
        assert {"reference_payload_path", "allowed_uri_prefixes",
                FIELDS} <= multiline

    def test_the_binding_carries_the_list_and_the_fields_verbatim(self):
        def binding(graph):
            document = compile(graph, "arm64_jp6")
            assert isinstance(document, CompiledPipelineDocument), document
            (found,) = [b for b in document.executor_bindings
                        if b["binding"] == "bedrock_inference"]
            return found["parameters"]

        unset = binding(_graph(**_list_node()))
        assert (unset["reference_payload_path"], unset[FIELDS]) == (LIST, "")
        value = '{\n  "inferenceConfig": {"topK": 20}\n}'
        assert binding(_graph(**_list_node(**{FIELDS: value})))[FIELDS] == value
