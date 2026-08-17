"""
Preservation property tests for the vLLM packaged/served model name fix
(spec: vllm-model-name-mismatch, task 2).

**Property 2: Preservation - Non-LLM Nodes and Stable Names Unchanged**

*For any* packaging input where the bug condition does NOT hold -
workflows with no ``llm_inference`` node, or LLM workflows whose
referenced model names are sanitization-stable - the fixed packaging
path SHALL produce the same artifact content as the original path, and
model-component dependency resolution SHALL continue to receive the
original registry names.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

Observation-first: these tests encode the OBSERVED behavior of the
UNFIXED packaging pipeline and MUST PASS both before and after the fix.
They pin four baselines:

1. (3.1) Sanitization-stable llm ``modelName`` values (``[a-z0-9-]+``)
   package verbatim - the fix's rewrite must be a no-op for them.
2. (3.2) Workflows with no ``llm_inference`` node serialize their node
   parameters unchanged, even for names the sanitizer WOULD alter
   (``model_inference`` is never rewritten), and the compiled document
   is byte-identical to the compiler's own serialization.
3. (3.3) ``gather_model_references`` (and, through the full packaging
   handler, ``resolve_model_components``) sees the ORIGINAL registry
   names - the Model_Registry snapshot is keyed by them.
   CONSCIOUS REPOINT (vllm-model-reload-after-backend-restart task 3.6,
   user-approved extension of that spec's task-2 record): requirement
   2.6 of that bugfix makes legacy singular-only records fail closed
   and forbids emitting the unsuffixed base component name, so the
   full-handler harness now seeds the shapes greengrass_publish.py
   writes TODAY (vLLM records with platform-suffixed per-JetPack
   ``components`` evidence, vision records in the plural
   ``published_components`` shape) and the recipe-dependency
   assertions expect the platform-suffixed names. The property under
   test - resolution is keyed by ORIGINAL registry names, never
   rewritten ones - is unchanged.
4. (3.4) The two private ``_safe_model_name`` copies
   (``greengrass_publish.py``, ``packaging.py``) and
   ``derive_vllm_component_name`` match the reference transform
   ``re.sub(r'[^a-zA-Z0-9-]', '-', name.lower())`` - the baseline the
   shared-transform refactor must reproduce exactly.

Harness: the established moto-backed conftest stack (``aws_stack`` /
``env``), the pure compile/serialization helpers of
``workflow_packaging.py`` (test_property_aravis_free_packaging_identity
pattern), the full-handler packaging environment with a seeded
Model_Registry (test_workflow_packaging_dependencies_exploration
pattern), and importlib-loaded ``greengrass_publish.py`` /
``packaging.py`` modules (test_greengrass_publish_localserver pattern).
Hypothesis runs under the conftest ``portal-fast`` profile.
"""
import importlib.util
import json
import os
import re
import sys
import uuid
import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from workflow_core.serializer import parse
from workflow_core.compiler import compile as compile_workflow, CompileContext
from workflow_core.catalog import DEVICE_ARCHITECTURES
from workflow_core.catalog.custom import resolve_catalog

from conftest import REGION

_FUNCTIONS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "functions")

TRAINING_JOBS_TABLE_NAME = "test-llm-name-preservation-training-jobs"

#: The live counterexample's registry name (unsafe) and the historic
#: smoke model's (sanitization-stable).
UNSAFE_LLM_NAME = "Qwen2.5-7B-Instruct-AWQ"
UNSAFE_LLM_COMPONENT = "model-vllm-qwen2-5-7b-instruct-awq"
STABLE_LLM_NAME = "opt125m-smoke"
STABLE_LLM_COMPONENT = "model-vllm-opt125m-smoke"
VISION_MODEL_NAME = "defect-model"
VISION_MODEL_COMPONENT = "model-defect-model"
#: A vision registry name the sanitizer WOULD alter - packaged verbatim
#: because model_inference is never rewritten (Requirement 3.2).
UNSAFE_VISION_NAME = "Defect_Model.V2"
UNSAFE_VISION_COMPONENT = "model-defect-model-v2"


def reference_safe_model_name(name):
    """The publish-time transform, written out independently so the tests
    do not depend on any production module for their oracle."""
    return re.sub(r"[^a-zA-Z0-9-]", "-", str(name).lower())


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: Sanitization-stable names: reference_safe_model_name is the identity.
stable_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=1, max_size=30)

#: Arbitrary registry names: mixed case, dots, underscores - including
#: (but not limited to) names the sanitizer would alter.
registry_names = st.text(
    alphabet=("abcdefghijklmnopqrstuvwxyz"
              "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
              "0123456789-._"),
    min_size=1, max_size=30)


def llm_definition(llm_model_name, vision_model_name=VISION_MODEL_NAME):
    """folder_source -> model_inference -> llm_inference -> mqtt_publish:
    compiles for arm64_jp6; binds two model refs
    (model_inference.modelName and llm_inference.modelName)."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "inf", "type": "model_inference", "position": {"x": 200, "y": 0},
             "parameters": {"modelName": vision_model_name}},
            {"id": "llm", "type": "llm_inference", "position": {"x": 400, "y": 0},
             "parameters": {"modelName": llm_model_name,
                            "prompt_template": "Summarize: {confidence}"}},
            {"id": "pub", "type": "mqtt_publish", "position": {"x": 600, "y": 0},
             "parameters": {"topic": "results", "broker_host": "localhost"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "inf", "port": "in"}},
            {"id": "c2", "from": {"node": "inf", "port": "out"},
             "to": {"node": "llm", "port": "in"}},
            {"id": "c3", "from": {"node": "llm", "port": "out"},
             "to": {"node": "pub", "port": "in"}},
        ],
    }


def non_llm_definition(vision_model_name):
    """folder_source -> model_inference -> mqtt_publish: no llm_inference
    node anywhere; compiles for every device architecture."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "inf", "type": "model_inference", "position": {"x": 200, "y": 0},
             "parameters": {"modelName": vision_model_name}},
            {"id": "pub", "type": "mqtt_publish", "position": {"x": 400, "y": 0},
             "parameters": {"topic": "results", "broker_host": "localhost"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "inf", "port": "in"}},
            {"id": "c2", "from": {"node": "inf", "port": "out"},
             "to": {"node": "pub", "port": "in"}},
        ],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """The training-jobs Model_Registry table (production GSI shape) plus a
    freshly imported workflow_packaging bound to it inside moto
    (test_workflow_packaging_dependencies_exploration pattern)."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-training-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )

    for module_name in ("workflow_packaging", "node_catalog_resolution",
                        "model_registry_snapshot"):
        sys.modules.pop(module_name, None)
    import workflow_packaging

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        packaging=workflow_packaging,
        training_table=resource.Table(TRAINING_JOBS_TABLE_NAME),
    )
    os.environ.pop("TRAINING_JOBS_TABLE", None)
    sys.modules.pop("workflow_packaging", None)


@pytest.fixture(scope="module")
def packaging(packaging_env):
    """The workflow_packaging module (pure helpers under test)."""
    return packaging_env.packaging


def _load_functions_module(file_name, module_name):
    """Load a functions/*.py Lambda module under a distinct module name
    (inside the moto mock, so module-level boto3 clients bind to the test
    stack) - the test_greengrass_publish_localserver pattern."""
    path = os.path.join(_FUNCTIONS_DIR, file_name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def publish(aws_stack):
    """functions/greengrass_publish.py: _safe_model_name +
    derive_vllm_component_name (Requirement 3.4 baseline)."""
    return _load_functions_module(
        "greengrass_publish.py", "portal_greengrass_publish_naming")


@pytest.fixture(scope="module")
def model_packaging(aws_stack):
    """functions/packaging.py: the second private _safe_model_name copy
    (Requirement 3.4 baseline)."""
    return _load_functions_module(
        "packaging.py", "portal_model_packaging_naming")


# ---------------------------------------------------------------------------
# Pure compile/serialization helpers (the packager's own path)
# ---------------------------------------------------------------------------

def compiled_artifact_json(packaging, definition, arch):
    """Run a definition through the exact pure pipeline package_workflow
    uses to produce one arch's compiled_pipeline.json content."""
    parse_result = parse(json.dumps(definition))
    assert parse_result.ok, parse_result.error
    graph = parse_result.graph

    catalog = resolve_catalog([])
    descriptors_by_id = {d.type_id: d for d in catalog}
    context = CompileContext(workflow_id="wf-p2", workflow_version="1")

    compiled = compile_workflow(graph, arch, context, simulation=False,
                                catalog=catalog)
    assert not isinstance(compiled, list), (
        "compilation failed on {}: {}".format(arch, compiled))

    camera_nodes = packaging.gather_camera_input_nodes(graph, set())
    hints = packaging.binding_hints_from_definition(definition)
    compiled_dict = compiled.to_dict()
    binding_points = packaging.build_binding_points(
        camera_nodes, compiled_dict, arch, hints, descriptors_by_id)
    return compiled, packaging.compiled_document_json(compiled, binding_points)


def llm_binding_model_names(document):
    """The llm_inference executor bindings' modelName values in a compiled
    document (the values output_bindings.py::_run_one sends to the
    Text_Generation_API)."""
    return [binding["parameters"]["modelName"]
            for binding in document.get("executorBindings", [])
            if binding.get("binding") == "llm_inference"]


def emltriton_model_args(document):
    """The model_inference (emltriton) element 'model' args in a compiled
    document."""
    return [element["args"]["model"]
            for segment in document.get("segments", [])
            for element in segment.get("elements", [])
            if element.get("factory") == "emltriton"]


# ===========================================================================
# Property 2, case 1 (Requirement 3.1): stable llm names package verbatim
# ===========================================================================

@settings(deadline=None)
@given(name=stable_names)
def test_stable_llm_model_names_package_verbatim(packaging, name):
    """**Property 2: Preservation - Non-LLM Nodes and Stable Names Unchanged**

    For any sanitization-stable registry name (``[a-z0-9-]+``), the
    compiled artifact's llm_inference binding carries the registry name
    verbatim - the fix's rewrite must be a no-op here, so this holds on
    the unfixed AND the fixed pipeline.

    **Validates: Requirements 3.1**
    """
    # Stability precondition the generator guarantees, stated explicitly:
    assert reference_safe_model_name(name) == name

    definition = llm_definition(name)
    _, packaged_text = compiled_artifact_json(packaging, definition,
                                              "arm64_jp6")
    document = json.loads(packaged_text)

    assert llm_binding_model_names(document) == [name], (
        "sanitization-stable llm modelName {!r} must package verbatim; "
        "compiled document carries {!r}".format(
            name, llm_binding_model_names(document)))


# ===========================================================================
# Property 2, case 2 (Requirement 3.2): non-LLM serialization unchanged
# ===========================================================================

@settings(deadline=None)
@given(name=registry_names)
def test_non_llm_definition_serialization_unchanged(packaging, name):
    """**Property 2: Preservation - Non-LLM Nodes and Stable Names Unchanged**

    For any definition with no ``llm_inference`` node, the packaged
    compiled document is byte-identical to the compiler's own
    serialization, and the ``model_inference`` node's ``modelName``
    passes through verbatim on every device architecture - even for
    names the sanitizer WOULD alter (only llm nodes are ever rewritten).

    **Validates: Requirements 3.2**
    """
    definition = non_llm_definition(name)
    assert packaging.gather_llm_inference_node_ids(definition) == []

    for arch in DEVICE_ARCHITECTURES:
        compiled, packaged_text = compiled_artifact_json(packaging,
                                                         definition, arch)
        # Observed unfixed baseline: with no camera nodes the packager
        # serializes the compiler output byte-identically.
        assert packaged_text == compiled.to_json()

        document = json.loads(packaged_text)
        assert llm_binding_model_names(document) == []
        assert emltriton_model_args(document) == [name], (
            "non-LLM modelName {!r} must serialize verbatim on {}; "
            "compiled document carries {!r}".format(
                name, arch, emltriton_model_args(document)))


# ===========================================================================
# Property 2, case 3 (Requirement 3.3): model references keep original names
# ===========================================================================

@settings(deadline=None)
@given(llm_name=registry_names, vision_name=registry_names)
def test_gather_model_references_returns_original_registry_names(
        packaging, llm_name, vision_name):
    """**Property 2: Preservation - Non-LLM Nodes and Stable Names Unchanged**

    ``gather_model_references`` - the input to Model_Registry component
    resolution, which is keyed by the original registry name - returns
    the definition's model refs verbatim (never sanitized), for LLM and
    non-LLM workflows alike.

    **Validates: Requirements 3.3**
    """
    llm_def = llm_definition(llm_name, vision_model_name=vision_name)
    non_llm_def = non_llm_definition(vision_name)

    catalog = resolve_catalog([])
    descriptors_by_id = {d.type_id: d for d in catalog}

    expected = [vision_name]           # definition order, deduplicated
    if llm_name != vision_name:
        expected.append(llm_name)
    assert packaging.gather_model_references(
        llm_def, descriptors_by_id) == expected, (
        "model references must be the ORIGINAL registry names "
        "(registry resolution is keyed by them)")

    assert packaging.gather_model_references(
        non_llm_def, descriptors_by_id) == [vision_name]


# ===========================================================================
# Property 2, case 4 (Requirement 3.4): publisher naming baseline
# ===========================================================================

@settings(deadline=None)
@given(name=registry_names)
def test_publisher_naming_baseline_for_shared_transform_refactor(
        publish, model_packaging, name):
    """**Property 2: Preservation - Non-LLM Nodes and Stable Names Unchanged**

    The two private ``_safe_model_name`` copies and
    ``derive_vllm_component_name`` match the reference transform
    ``re.sub(r'[^a-zA-Z0-9-]', '-', name.lower())`` - the recorded
    baseline the shared-transform refactor must reproduce exactly
    (component ``model-vllm-{safe_name}`` / repository ``{safe_name}``).

    **Validates: Requirements 3.4**
    """
    expected = reference_safe_model_name(name)

    assert publish._safe_model_name(name) == expected
    assert model_packaging._safe_model_name(name) == expected
    assert publish._safe_model_name(name) == \
        model_packaging._safe_model_name(name)
    assert publish.derive_vllm_component_name(name) == \
        "model-vllm-{}".format(expected)


# ===========================================================================
# Full-handler observations on the UNFIXED packaging path
# (LlmPackagingEnv / DependencyPackagingEnv pattern): what the produced
# zip artifacts and the registered recipe actually contain today.
# ===========================================================================

def make_deployable_greengrass():
    gg = MagicMock(name="greengrassv2")
    gg.create_component_version.return_value = {
        "arn": ("arn:aws:greengrass:us-east-1:123456789012:"
                f"components:test:versions:{uuid.uuid4()}")
    }
    gg.describe_component.return_value = {
        "status": {"componentState": "DEPLOYABLE", "message": "simulated"}
    }
    return gg


class PreservationPackagingEnv:
    """Full packaging-handler harness: a validated workflow version, a
    Use_Case with an S3 bucket, Model_Registry records seeded under the
    ORIGINAL registry names, and patched Use_Case-account clients."""

    def __init__(self, env, packaging_env, monkeypatch, definition,
                 model_records):
        self.env = env
        self.packaging = packaging_env.packaging
        monkeypatch.setattr(self.packaging, "COMPONENT_STATUS_POLL_SECONDS", 0)

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        env.s3.create_bucket(Bucket=self.usecase_bucket)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "LLM Name Preservation",
            "account_id": "123456789012",
            "s3_bucket": self.usecase_bucket,
        })

        # Model_Registry records in the shapes greengrass_publish.py
        # writes TODAY, keyed by the ORIGINAL registry name (Requirement
        # 3.3). Repointed at vllm-model-reload-after-backend-restart task
        # 3.6: 2.6 makes legacy singular-only records fail closed, so
        # vLLM records carry the platform-suffixed per-JetPack
        # ``components`` entry and vision records use the plural
        # ``published_components`` shape.
        for model_name, component_name, model_type in model_records:
            if model_type == "vllm":
                item = {
                    "training_id": f"tr-{uuid.uuid4()}",
                    "usecase_id": self.usecase_id,
                    "model_name": model_name,
                    "model_type": model_type,
                    "created_at": 1,
                    "published_component": {
                        "component_name": component_name,
                        "component_version": "1.0.0",
                        "runtime": "vllm",
                        "supported_architectures": ["arm64_jp6"],
                        "components": [{
                            "component_name":
                                f"{component_name}-jetson-xavier-jp6",
                            "component_version": "1.0.0",
                            "target": "jetson-xavier-jp6",
                            "architecture": "arm64_jp6",
                            "supported_architectures": ["arm64_jp6"],
                        }],
                    },
                }
            else:
                item = {
                    "training_id": f"tr-{uuid.uuid4()}",
                    "usecase_id": self.usecase_id,
                    "model_name": model_name,
                    "model_type": model_type,
                    "created_at": 1,
                    "published_component": None,
                    "published_components": [{
                        "component_name":
                            f"{component_name}-jetson-xavier-jp6",
                        "component_version": "1.0.0",
                        "target": "jetson-xavier-jp6",
                        "status": "published",
                    }],
                }
            packaging_env.training_table.put_item(Item=item)

        status, payload = env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id,
            "name": "preservation workflow",
            "definition": definition,
        })
        assert status == 201, payload
        self.workflow_id = payload["workflow"]["workflow_id"]

        env.stack.tables.versions.update_item(
            Key={"workflow_id": self.workflow_id, "version": 1},
            UpdateExpression="SET validation_status = :v",
            ExpressionAttributeValues={
                ":v": {"status": "passed", "validated_at": 1,
                       "findings_key": "findings/none.json"},
            },
        )

        self.greengrass = make_deployable_greengrass()

        def fake_get_usecase_client(service_name, usecase, session_name=None,
                                    region=None):
            if service_name == "s3":
                return env.s3
            if service_name == "greengrassv2":
                return self.greengrass
            raise AssertionError(f"unexpected usecase client: {service_name}")

        monkeypatch.setattr(self.packaging, "get_usecase_client",
                            fake_get_usecase_client)

    def package(self, architectures):
        event = self.env.event(
            "POST", "/workflows/{id}/package", self.user,
            workflow_id=self.workflow_id,
            body={"architectures": architectures},
        )
        response = self.packaging.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def artifact_documents(self, payload, arch):
        """Read workflow.json + compiled_pipeline.json back out of the
        promoted artifact zip in the Use_Case bucket."""
        uri = payload["artifacts"][arch]
        assert uri.startswith(f"s3://{self.usecase_bucket}/")
        key = uri[len(f"s3://{self.usecase_bucket}/"):]
        body = self.env.s3.get_object(
            Bucket=self.usecase_bucket, Key=key)["Body"].read()
        with zipfile.ZipFile(BytesIO(body)) as zf:
            workflow_doc = json.loads(zf.read("workflow.json"))
            compiled_doc = json.loads(zf.read("compiled_pipeline.json"))
        return workflow_doc, compiled_doc

    def registered_recipe(self):
        assert self.greengrass.create_component_version.called, (
            "no component version was registered")
        call = self.greengrass.create_component_version.call_args
        return json.loads(call.kwargs["inlineRecipe"])


def definition_node_parameters(document, node_id):
    """A node's parameters dict inside a workflow.json document."""
    for node in document.get("nodes", []):
        if node.get("id") == node_id:
            return node.get("parameters") or {}
    raise AssertionError(f"node {node_id!r} not found in workflow.json")


class TestFullPackagingPathPreservation:
    """End-to-end observations of the packaging handler on the conftest
    stack - the artifact zips and the registered recipe. Each observation
    must hold on the unfixed AND the fixed pipeline."""

    def test_stable_llm_workflow_artifacts_carry_registry_name_verbatim(
            self, env, packaging_env, monkeypatch):
        """Requirement 3.1 (observed unfixed baseline): a
        sanitization-stable llm modelName (opt125m-smoke, the historic
        smoke model) reaches both packaged artifacts verbatim, and the
        non-LLM node parameters of the same workflow are untouched."""
        harness = PreservationPackagingEnv(
            env, packaging_env, monkeypatch,
            llm_definition(STABLE_LLM_NAME),
            model_records=[
                (STABLE_LLM_NAME, STABLE_LLM_COMPONENT, "vllm"),
                (VISION_MODEL_NAME, VISION_MODEL_COMPONENT,
                 "anomaly_detection"),
            ])
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload

        workflow_doc, compiled_doc = harness.artifact_documents(
            payload, "arm64_jp6")

        # workflow.json: llm modelName verbatim (rewrite must be a no-op),
        # sibling node parameters byte-equal to the stored definition.
        assert definition_node_parameters(
            workflow_doc, "llm")["modelName"] == STABLE_LLM_NAME
        expected = llm_definition(STABLE_LLM_NAME)
        for node_id in ("src", "inf", "pub"):
            assert definition_node_parameters(workflow_doc, node_id) == \
                definition_node_parameters(expected, node_id)

        # compiled_pipeline.json: same invariants on the executor binding.
        assert llm_binding_model_names(compiled_doc) == [STABLE_LLM_NAME]
        assert emltriton_model_args(compiled_doc) == [VISION_MODEL_NAME]

    def test_non_llm_workflow_artifacts_unchanged_even_for_unsafe_names(
            self, env, packaging_env, monkeypatch):
        """Requirement 3.2 (observed unfixed baseline): a workflow with no
        llm_inference node packages its node parameters verbatim even when
        the model name is NOT sanitization-stable - model_inference is
        never rewritten."""
        harness = PreservationPackagingEnv(
            env, packaging_env, monkeypatch,
            non_llm_definition(UNSAFE_VISION_NAME),
            model_records=[
                (UNSAFE_VISION_NAME, UNSAFE_VISION_COMPONENT,
                 "anomaly_detection"),
            ])
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload

        workflow_doc, compiled_doc = harness.artifact_documents(
            payload, "arm64_jp6")

        assert definition_node_parameters(
            workflow_doc, "inf")["modelName"] == UNSAFE_VISION_NAME
        assert emltriton_model_args(compiled_doc) == [UNSAFE_VISION_NAME]
        assert llm_binding_model_names(compiled_doc) == []

    def test_model_resolution_keyed_by_original_registry_names(
            self, env, packaging_env, monkeypatch):
        """Requirement 3.3 (observed unfixed baseline): packaging resolves
        model_ref values against Model_Registry records stored under the
        ORIGINAL registry names - including the live counterexample's
        unsafe llm name - and the recipe's ComponentDependencies carry the
        published component names those records recorded (since the
        vllm-model-reload-after-backend-restart 3.6 repoint: the
        platform-suffixed per-JetPack names - 2.6 forbids the unsuffixed
        base names). If the fix ever rewrote the names BEFORE resolution,
        this packaging run would fail closed (502) instead of
        registering."""
        harness = PreservationPackagingEnv(
            env, packaging_env, monkeypatch,
            llm_definition(UNSAFE_LLM_NAME),
            model_records=[
                (UNSAFE_LLM_NAME, UNSAFE_LLM_COMPONENT, "vllm"),
                (VISION_MODEL_NAME, VISION_MODEL_COMPONENT,
                 "anomaly_detection"),
            ])
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, (
            "packaging must resolve model references by their ORIGINAL "
            "registry names; failure here means resolution saw rewritten "
            "names: {}".format(payload))

        deps = harness.registered_recipe().get("ComponentDependencies") or {}
        assert f"{UNSAFE_LLM_COMPONENT}-jetson-xavier-jp6" in deps
        assert f"{VISION_MODEL_COMPONENT}-jetson-xavier-jp6" in deps
        # 2.6 (vllm-model-reload-after-backend-restart): the unsuffixed
        # base names never appear as dependencies.
        assert UNSAFE_LLM_COMPONENT not in deps
        assert VISION_MODEL_COMPONENT not in deps
