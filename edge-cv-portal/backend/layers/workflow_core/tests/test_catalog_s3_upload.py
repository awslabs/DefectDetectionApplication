"""Unit tests for the ``s3_upload`` catalog descriptor.

Asserts the descriptor's identity, ports (one VideoFrames input, no
outputs), every parameter's shape (``destination`` required; the
``content_type`` default; the optional suffix rewrite pair; the optional
``condition`` in the shared grammar; ``timeout_s`` / ``retries`` /
``region`` bounds), the device architecture mappings (executor binding
``s3_upload`` with the ``python:boto3`` runtime dependency), the ``sim``
recording stub (``recording_s3_upload``), ``hardware_dependent``, the
appended catalog position, and the baseline delta scope (the recorded
``catalog_baseline.json`` covers exactly the live catalog and its
``s3_upload`` entry equals the live descriptor).
"""

import dataclasses
import json
import os

from workflow_core.catalog import (
    ARCH_SIM,
    CATEGORY_OUTPUT,
    CONDITION_EXAMPLES,
    CONDITION_LANGUAGE_DESCRIPTION,
    DEVICE_ARCHITECTURES,
    NODE_CATALOG,
    PORT_TYPE_VIDEO_FRAMES,
    SIM_RECORDING_BINDING_PREFIX,
    get_node_type,
)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _descriptor():
    descriptor = get_node_type("s3_upload")
    assert descriptor is not None
    return descriptor


def _params_by_name(descriptor):
    return {param.name: param for param in descriptor.parameters}


class TestS3UploadIdentityAndPorts:
    def test_identity_and_category(self):
        descriptor = _descriptor()
        assert descriptor.type_id == "s3_upload"
        assert descriptor.category == CATEGORY_OUTPUT
        assert descriptor.display_name == "S3 Upload"

    def test_exactly_one_video_frames_input_and_zero_outputs(self):
        # VideoFrames like ``capture``: InferenceMeta outputs connect via
        # the declared coercion, and the compiler terminates the branch in
        # its synthetic frame-capture sink chain.
        descriptor = _descriptor()
        assert len(descriptor.inputs) == 1
        assert descriptor.inputs[0].name == "in"
        assert descriptor.inputs[0].port_type == PORT_TYPE_VIDEO_FRAMES
        assert descriptor.outputs == []


class TestS3UploadParameters:
    def test_parameter_names_in_order(self):
        assert [p.name for p in _descriptor().parameters] == [
            "destination", "content_type", "strip_suffix", "append_suffix",
            "condition", "timeout_s", "retries", "region",
        ]

    def test_destination(self):
        param = _params_by_name(_descriptor())["destination"]
        assert param.param_type == "string"
        assert param.required is True
        assert param.default is None
        assert param.constraints == {"min_length": 1}
        # Both destination forms and the template rendering are documented.
        for token in ("s3://", "https://", "presigned", "PutObject",
                      "{trigger.payload_json.body.image_key}"):
            assert token in param.description, token
        assert any(example.startswith("s3://") for example in param.examples)

    def test_content_type(self):
        param = _params_by_name(_descriptor())["content_type"]
        assert param.param_type == "string"
        assert param.required is False
        assert param.default == "image/jpeg"
        assert param.constraints == {"min_length": 1}

    def test_suffix_rewrite_pair_is_optional_and_unconstrained(self):
        params = _params_by_name(_descriptor())
        for name in ("strip_suffix", "append_suffix"):
            param = params[name]
            assert param.param_type == "string", name
            assert param.required is False, name
            assert param.default is None, name
            assert param.constraints == {}, name
            assert "s3://" in param.description, name
        assert params["strip_suffix"].examples == [".jpg"]
        assert params["append_suffix"].examples == [".annotated.jpg"]

    def test_condition_is_optional_and_uses_the_shared_grammar(self):
        param = _params_by_name(_descriptor())["condition"]
        assert param.param_type == "string"
        assert param.required is False
        assert param.default is None
        assert param.constraints == {}
        assert param.description.endswith(CONDITION_LANGUAGE_DESCRIPTION)
        assert param.examples[:len(CONDITION_EXAMPLES)] == list(CONDITION_EXAMPLES)
        # A dotted trigger-payload example is offered as well.
        assert any("trigger.payload_json" in example
                   for example in param.examples)

    def test_timeout_s(self):
        param = _params_by_name(_descriptor())["timeout_s"]
        assert param.param_type == "int"
        assert param.required is False
        assert param.default == 10
        assert param.constraints == {"min": 1, "max": 60}

    def test_retries(self):
        param = _params_by_name(_descriptor())["retries"]
        assert param.param_type == "int"
        assert param.required is False
        assert param.default == 1
        assert param.constraints == {"min": 0, "max": 3}

    def test_region(self):
        param = _params_by_name(_descriptor())["region"]
        assert param.param_type == "string"
        assert param.required is False
        assert param.default is None
        assert param.constraints == {"min_length": 1}

    def test_no_parameter_is_gated(self):
        for param in _descriptor().parameters:
            assert param.depends_on is None, param.name


class TestS3UploadMappings:
    def test_device_mappings_bind_s3_upload_with_boto3_runtime(self):
        descriptor = _descriptor()
        device_mappings = [m for m in descriptor.mappings
                           if m.arch != ARCH_SIM]
        assert [m.arch for m in device_mappings] == list(DEVICE_ARCHITECTURES)
        for mapping in device_mappings:
            assert mapping.executor_binding == "s3_upload"
            assert mapping.element_chain == []
            assert mapping.plugin_dependencies == ["python:boto3"]

    def test_sim_mapping_is_the_recording_stub(self):
        descriptor = _descriptor()
        sim_mappings = [m for m in descriptor.mappings if m.arch == ARCH_SIM]
        assert len(sim_mappings) == 1
        stub = sim_mappings[0]
        assert stub.executor_binding == \
            SIM_RECORDING_BINDING_PREFIX + "s3_upload"
        assert stub.executor_binding == "recording_s3_upload"
        assert stub.element_chain == []
        assert stub.plugin_dependencies == []

    def test_hardware_dependent(self):
        assert _descriptor().hardware_dependent is True


class TestS3UploadCatalogPositionAndBaseline:
    def test_appended_last(self):
        order = [d.type_id for d in NODE_CATALOG]
        assert order[-1] == "s3_upload"
        assert order.count("s3_upload") == 1
        # The descriptor right before it is the last pre-existing one.
        assert order[-2] == "event_gate"

    def test_baseline_delta_scoped_to_the_new_descriptor(self):
        with open(os.path.join(_HERE, "catalog_baseline.json"),
                  encoding="utf-8") as fh:
            baseline = json.load(fh)
        assert set(baseline) == {d.type_id for d in NODE_CATALOG}
        assert baseline["s3_upload"] == \
            dataclasses.asdict(get_node_type("s3_upload"))
