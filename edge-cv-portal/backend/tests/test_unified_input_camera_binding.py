"""
Bug-condition and preservation tests for unified-input-camera-binding.

**Feature: unified-input-camera-binding**

A workflow whose Input Source node (``unified_input``) is set to a camera
kind (``aravis_camera``, ``csi_camera`` or ``icam``) must package exactly
like the same workflow with the dedicated camera node in its place: the
same ``bindingPoints`` in every architecture's compiled document, the same
``camera_input_nodes`` record and ``has_binding_points`` flag on the
version item, and an unchanged compiled pipeline. Every other workflow
must package exactly as before.

**Property 1: Bug Condition** (``test_camera_input_source_packages_like_
its_dedicated_node``), plus one example per camera kind and the
deployment camera check. On the unfixed packager these FAIL: the packager
gathers Camera_Input_Nodes from the saved, unexpanded graph, so the Input
Source gets no binding point and no camera_input_nodes record.

**Property 2: Preservation** (``test_packages_outside_the_bug_condition_
are_unchanged``) and its examples. These PASS on the unfixed packager and
must keep passing: the oracle is today's handler path rebuilt from the
unchanged pure helpers over the saved graph.

**Validates: Requirements 1.1-1.4, 2.1-2.3, 3.1-3.4**

Everything is driven through the real packaging handler
(``POST /workflows/{id}/package``), because the bug is in the handler's
call sequence rather than in any one helper. The handler modules are
imported inside module-scoped fixtures that depend on ``aws_stack`` (never
at module level), so their module-level boto3 clients bind to the moto
mock. The device's own Aravis feed planner is loaded by path from
``src/backend/workflow_engine/aravis_feed.py`` and checks what a device
would grab from each packaged document.
"""
import importlib.util
import io
import json
import os
import sys
import uuid
import zipfile
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import DEVICE_ARCHITECTURES, get_node_type
from workflow_core.catalog.custom import resolve_catalog
from workflow_core.compiler import CompileContext
from workflow_core.compiler import compile as compile_workflow
from workflow_core.serializer import parse

COMPONENTS_ROOT = "workflows/components"
ARCHITECTURES = list(DEVICE_ARCHITECTURES)

UNIFIED_INPUT = "unified_input"

#: Input Source kind -> the dedicated node type it stands for, restated
#: from the catalog's SOURCE_KIND_TO_SOURCE_TYPE contract.
KIND_TYPE = {
    "aravis_camera": "aravis_camera_source",
    "csi_camera": "csi_camera_source",
    "icam": "icam_source",
    "folder": "folder_source",
}
CAMERA_KINDS = ("aravis_camera", "csi_camera", "icam")
DEDICATED_CAMERA_TYPES = ("aravis_camera_source", "csi_camera_source",
                          "icam_source")

#: The parameters each dedicated node declares. An Input Source keeps
#: exactly these when it stands for that node.
KIND_PARAMS = {kind: frozenset(p.name for p in get_node_type(node_type).parameters)
               for kind, node_type in KIND_TYPE.items()}

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DEVICE_ARAVIS_FEED = os.path.join(_REPO, "src", "backend", "workflow_engine",
                                  "aravis_feed.py")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging(aws_stack):
    """workflow_packaging imported inside the moto mock."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """deployments imported inside the moto mock."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


@pytest.fixture(scope="module")
def device_aravis_feed():
    """The device's pure Aravis feed planner, loaded by path."""
    if not os.path.isfile(DEVICE_ARAVIS_FEED):
        pytest.skip("device source tree not present next to the portal")
    spec = importlib.util.spec_from_file_location(
        "device_aravis_feed_uicb", DEVICE_ARAVIS_FEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deployable_greengrass():
    greengrass = MagicMock(name="greengrassv2")
    greengrass.create_component_version.return_value = {
        "arn": "arn:aws:greengrass:us-east-1:123456789012:components:"
               f"test:versions:{uuid.uuid4()}"
    }
    greengrass.describe_component.return_value = {
        "status": {"componentState": "DEPLOYABLE", "message": "simulated"}
    }
    return greengrass


class Packaged:
    """One packaged workflow: its id, version item, the stored (canonical)
    definition the handler packaged, and the compiled_pipeline.json text
    of each architecture's artifact zip."""

    def __init__(self, workflow_id, version_item, definition_json,
                 compiled_texts):
        self.workflow_id = workflow_id
        self.version_item = version_item
        self.definition_json = definition_json
        self.compiled_texts = compiled_texts

    def document(self, arch):
        return json.loads(self.compiled_texts[arch])

    def binding_points(self, arch):
        return self.document(arch).get("bindingPoints")


class PackagingHarness:
    """One use case with its S3 bucket and a UseCaseAdmin user; each call
    to ``package`` saves a new workflow, records a passed validation and
    packages it through the real handler."""

    def __init__(self, stack, packaging, monkeypatch):
        from conftest import WorkflowStoreEnv

        self.env = WorkflowStoreEnv(stack)
        self.stack = stack
        self.packaging = packaging
        monkeypatch.setattr(packaging, "COMPONENT_STATUS_POLL_SECONDS", 0)

        self.user = self.env.make_user(role="UseCaseAdmin")
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        self.env.s3.create_bucket(Bucket=self.usecase_bucket)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Unified Input camera binding",
            "account_id": "123456789012",
            "s3_bucket": self.usecase_bucket,
        })
        self.greengrass = _deployable_greengrass()

        def fake_get_usecase_client(service_name, usecase, session_name=None,
                                    region=None):
            if service_name == "s3":
                return self.env.s3
            if service_name == "greengrassv2":
                return self.greengrass
            raise AssertionError(f"unexpected usecase client: {service_name}")

        monkeypatch.setattr(packaging, "get_usecase_client",
                            fake_get_usecase_client)

    def package(self, definition, architectures=ARCHITECTURES):
        status, payload = self.env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id,
            "name": f"uicb {uuid.uuid4().hex[:8]}",
            "definition": definition,
        })
        assert status == 201, payload
        workflow_id = payload["workflow"]["workflow_id"]
        self.stack.tables.versions.update_item(
            Key={"workflow_id": workflow_id, "version": 1},
            UpdateExpression="SET validation_status = :v",
            ExpressionAttributeValues={
                ":v": {"status": "passed", "validated_at": 1,
                       "findings_key": "findings/none.json"},
            },
        )

        event = self.env.event("POST", "/workflows/{id}/package", self.user,
                               workflow_id=workflow_id,
                               body={"architectures": list(architectures)})
        response = self.packaging.handler(event, None)
        assert response["statusCode"] == 201, response["body"]

        version_item = self.stack.tables.versions.get_item(
            Key={"workflow_id": workflow_id, "version": 1})["Item"]
        # The workflows handler stores the serializer's canonical form,
        # which is what the packager parses and compiles.
        definition_json = self.env.s3.get_object(
            Bucket=self.env.bucket,
            Key=version_item["s3_definition_key"])["Body"].read().decode("utf-8")
        component_version = version_item.get("component_version", "1.0.0")
        texts = {}
        for arch in architectures:
            key = (f"{COMPONENTS_ROOT}/{workflow_id}/1/{component_version}/"
                   f"{arch}/workflow-{arch}.zip")
            body = self.env.s3.get_object(
                Bucket=self.usecase_bucket, Key=key)["Body"].read()
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                texts[arch] = archive.read("compiled_pipeline.json").decode("utf-8")
        return Packaged(workflow_id, version_item, definition_json, texts)


@pytest.fixture(scope="module")
def harness(aws_stack, packaging):
    monkeypatch = pytest.MonkeyPatch()
    try:
        yield PackagingHarness(aws_stack, packaging, monkeypatch)
    finally:
        monkeypatch.undo()


# --------------------------------------------------------------------------
# Definitions
# --------------------------------------------------------------------------

def _capture(index):
    return {"id": f"cap{index}", "type": "capture",
            "position": {"x": 300.0, "y": 150.0 * index},
            "parameters": {"output_path": f"/out/{index}"}}


def _definition(sources):
    """source -> capture, one chain per source node."""
    nodes, connections = [], []
    for index, source in enumerate(sources):
        nodes.append(source)
        nodes.append(_capture(index))
        connections.append({
            "id": f"c{index}",
            "from": {"node": source["id"], "port": "out"},
            "to": {"node": f"cap{index}", "port": "in"},
        })
    return {"schemaVersion": 1, "nodes": nodes, "connections": connections}


def _source(node_id, node_type, index, parameters, data=None):
    node = {"id": node_id, "type": node_type,
            "position": {"x": 0.0, "y": 150.0 * index},
            "parameters": dict(parameters)}
    if data:
        node["data"] = data
    return node


def dedicated_equivalent(definition):
    """E: the definition with every camera-kind Input Source written as
    the dedicated node it stands for - same id, position and data, and
    only the parameters that node declares."""
    rewritten = json.loads(json.dumps(definition))
    for node in rewritten["nodes"]:
        if node["type"] != UNIFIED_INPUT:
            continue
        kind = node["parameters"].get("source_kind")
        if kind not in CAMERA_KINDS:
            continue
        node["type"] = KIND_TYPE[kind]
        node["parameters"] = {name: value
                              for name, value in node["parameters"].items()
                              if name in KIND_PARAMS[kind]}
    return rewritten


def plain_compiled(definition_json, workflow_id, arch):
    """The compiler's own output for the stored definition."""
    result = parse(definition_json)
    assert result.ok, result.error
    compiled = compile_workflow(
        result.graph, arch,
        CompileContext(workflow_id=workflow_id, workflow_version="1"),
        simulation=False, catalog=resolve_catalog([]))
    assert not isinstance(compiled, list), compiled
    return compiled


# --------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------

_camera_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.",
    min_size=1, max_size=32)
_hint_text = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_ ", min_size=1, max_size=24)
_hints = st.fixed_dictionaries(
    {"cameraSourceId": _hint_text},
    optional={"cameraName": _hint_text, "sourceDeviceId": _hint_text})

#: A valid value for every parameter an Input Source can carry.
PARAMETER_VALUES = {
    "camera_id": _camera_ids,
    "gain": st.integers(min_value=0, max_value=100),
    "exposure": st.integers(min_value=0, max_value=10_000_000),
    "device": st.integers(min_value=0, max_value=63).map(
        lambda n: f"/dev/video{n}"),
    "location": st.sampled_from(["/aws_dda/images", "/data/line1",
                                 "/aws_dda/images/latest.jpg"]),
    "file_pattern": st.sampled_from(["*.jpg", "line1_*.jpg", "*.png"]),
}
#: Parameters a node needs set to compile (required, no usable default).
REQUIRED_BY_KIND = {"aravis_camera": ("camera_id",), "icam": ("device",),
                    "folder": ("location",), "csi_camera": ()}


@st.composite
def _parameters(draw, kind, stray):
    """The kind's own parameters (required ones always set), plus, when
    ``stray``, parameters that belong only to other kinds."""
    own = KIND_PARAMS[kind]
    parameters = {}
    for name in sorted(own):
        if name in REQUIRED_BY_KIND[kind] or draw(st.booleans()):
            parameters[name] = draw(PARAMETER_VALUES[name])
    if stray:
        others = sorted(set(PARAMETER_VALUES) - own)
        for name in draw(st.lists(st.sampled_from(others), unique=True,
                                  max_size=len(others))):
            parameters[name] = draw(PARAMETER_VALUES[name])
    return parameters


@st.composite
def _source_nodes(draw, choices, index, aravis_taken):
    """One chain head drawn from ``choices``: ('input', kind) for an Input
    Source or ('dedicated', type) for a dedicated node. At most one
    Aravis source per workflow (V7)."""
    allowed = [c for c in choices
               if not (aravis_taken and c in (("input", "aravis_camera"),
                                              ("dedicated", "aravis_camera_source")))]
    form, what = draw(st.sampled_from(allowed))
    node_id = f"src{index}"
    hint = draw(st.one_of(st.none(), _hints))
    data = {"cameraBindingHint": hint} if hint is not None else None
    if form == "input":
        parameters = {"source_kind": what}
        parameters.update(draw(_parameters(what, stray=draw(st.booleans()))))
        node = _source(node_id, UNIFIED_INPUT, index, parameters, data)
        is_aravis = what == "aravis_camera"
    else:
        kind = next(k for k, t in KIND_TYPE.items() if t == what)
        node = _source(node_id, what, index,
                       draw(_parameters(kind, stray=False)), data)
        is_aravis = what == "aravis_camera_source"
    return node, is_aravis


CAMERA_INPUT_CHOICES = [("input", kind) for kind in CAMERA_KINDS]
OTHER_CHOICES = ([("dedicated", node_type) for node_type in DEDICATED_CAMERA_TYPES]
                 + [("dedicated", "folder_source"), ("input", "folder")])


@st.composite
def bug_condition_definitions(draw):
    """1-3 chains; the first is headed by a camera-kind Input Source, the
    rest by anything, with at most one Aravis source."""
    chain_count = draw(st.integers(min_value=1, max_value=3))
    sources, aravis_taken = [], False
    for index in range(chain_count):
        choices = CAMERA_INPUT_CHOICES if index == 0 else (
            CAMERA_INPUT_CHOICES + OTHER_CHOICES)
        node, is_aravis = draw(_source_nodes(choices, index, aravis_taken))
        aravis_taken = aravis_taken or is_aravis
        sources.append(node)
    order = draw(st.permutations(range(chain_count)))
    return _definition([sources[i] for i in order])


@st.composite
def preserved_definitions(draw):
    """1-3 chains headed by dedicated camera nodes, folder sources, or
    folder-kind Input Sources (which may carry camera parameters and a
    hint), with at most one Aravis source: outside the bug condition."""
    chain_count = draw(st.integers(min_value=1, max_value=3))
    sources, aravis_taken = [], False
    for index in range(chain_count):
        node, is_aravis = draw(_source_nodes(OTHER_CHOICES, index, aravis_taken))
        aravis_taken = aravis_taken or is_aravis
        sources.append(node)
    order = draw(st.permutations(range(chain_count)))
    return _definition([sources[i] for i in order])


def is_bug_condition(definition):
    return any(node["type"] == UNIFIED_INPUT
               and KIND_TYPE.get(node["parameters"].get("source_kind") or "folder")
               in DEDICATED_CAMERA_TYPES
               for node in definition["nodes"])


# ==========================================================================
# Property 1: Bug Condition
# ==========================================================================

@settings(deadline=None)
@given(definition=bug_condition_definitions())
def test_camera_input_source_packages_like_its_dedicated_node(
        harness, device_aravis_feed, definition):
    """**Feature: unified-input-camera-binding, Property 1: Fix Checking**

    For a workflow W with a camera-kind Input Source and its dedicated
    equivalent E: per architecture, bindingPoints(W) = bindingPoints(E);
    camera_input_nodes and has_binding_points are equal; W's pipeline is
    the plain compiler output; an Aravis Input Source is planned a feed.

    **Validates: Requirements 1.1-1.4, 2.1-2.3, 3.4**
    """
    assert is_bug_condition(definition)
    equivalent = dedicated_equivalent(definition)
    assert not is_bug_condition(equivalent)

    packaged = harness.package(definition)
    expected = harness.package(equivalent)

    for arch in ARCHITECTURES:
        # 2.1: the Input Source gets the dedicated node's binding point.
        assert packaged.binding_points(arch) == expected.binding_points(arch), arch

        # 3.4: the pipeline itself is the compiler's own output; only the
        # bindingPoints section is added, in the packager's serialization.
        document = packaged.document(arch)
        binding_points = document.pop("bindingPoints")
        compiled = plain_compiled(packaged.definition_json, packaged.workflow_id,
                                  arch).to_dict()
        assert document == compiled, arch
        compiled["bindingPoints"] = binding_points
        assert packaged.compiled_texts[arch] == json.dumps(
            compiled, sort_keys=True, indent=2, ensure_ascii=True), arch

        # 2.3: the device plans a frame grab for an Aravis Input Source,
        # from the camera id typed into it.
        aravis_inputs = [n for n in definition["nodes"]
                         if n["type"] == UNIFIED_INPUT
                         and n["parameters"].get("source_kind") == "aravis_camera"]
        feeds = device_aravis_feed.plan_aravis_feeds(packaged.document(arch), None)
        for node in aravis_inputs:
            assert [(f.node_id, f.camera_id) for f in feeds] == [
                (node["id"], node["parameters"]["camera_id"])], arch

    # 2.2: the version item records the Input Source as the dedicated node.
    assert packaged.version_item["has_binding_points"] is True
    assert (packaged.version_item["camera_input_nodes"]
            == expected.version_item["camera_input_nodes"])


# ==========================================================================
# Examples, one per camera kind (Bug Condition)
# ==========================================================================

ARAVIS_HINT = {"cameraSourceId": "cfg-a1b2c3d4", "cameraName": "Line 2",
               "sourceDeviceId": "jetson-thor1"}


def _single_input_source(parameters, data=None):
    return _definition([_source("src", UNIFIED_INPUT, 0, parameters, data)])


def aravis_input_definition():
    return _single_input_source(
        {"source_kind": "aravis_camera", "camera_id": "Fake_1", "gain": 7,
         "location": "/aws_dda/images", "device": "/dev/video3"},
        {"cameraBindingHint": ARAVIS_HINT})


def test_aravis_input_source_gets_a_frame_feed_on_the_device(
        harness, device_aravis_feed):
    """1.1, 1.3, 2.1, 2.3: aravisBinding point with the rendered Aravis
    parameters and the hint; the device plans one grab for the node."""
    packaged = harness.package(aravis_input_definition())
    for arch in ARCHITECTURES:
        assert packaged.binding_points(arch) == [{
            "nodeId": "src",
            "nodeType": "aravis_camera_source",
            "parameters": {"camera_id": "Fake_1", "gain": 7, "exposure": 5000000},
            "slots": [],
            "bindingHint": ARAVIS_HINT,
            "aravisBinding": True,
        }], arch
        feeds = device_aravis_feed.plan_aravis_feeds(packaged.document(arch), None)
        assert [(f.node_id, f.camera_id, f.config) for f in feeds] == [
            ("src", "Fake_1", {"gain": 7, "exposure": 5000000})], arch

    item = packaged.version_item
    assert item["has_binding_points"] is True
    assert item["camera_input_nodes"] == [{
        "node_id": "src", "node_type": "aravis_camera_source",
        "binding_hint": ARAVIS_HINT, "compiled_device_paths": {}}]


def test_csi_input_source_gets_the_csi_sensor_binding(harness):
    """1.1, 1.4, 2.1: csiSensorBinding point with the rendered gain and
    exposure, and no stray parameter."""
    packaged = harness.package(_single_input_source(
        {"source_kind": "csi_camera", "gain": 10, "exposure": 16000000,
         "camera_id": "Basler-1", "location": "/aws_dda/images"}))
    for arch in ARCHITECTURES:
        assert packaged.binding_points(arch) == [{
            "nodeId": "src",
            "nodeType": "csi_camera_source",
            "parameters": {"gain": 10, "exposure": 16000000},
            "slots": [],
            "csiSensorBinding": True,
        }], arch

    assert packaged.version_item["camera_input_nodes"] == [{
        "node_id": "src", "node_type": "csi_camera_source",
        "compiled_device_paths": {}}]


def test_icam_input_source_gets_the_device_slot(harness):
    """1.1, 1.4, 2.1, 2.2: one slot on the v4l2src device argument, and
    the compiled device path recorded for every architecture."""
    packaged = harness.package(_single_input_source(
        {"source_kind": "icam", "device": "/dev/video2",
         "camera_id": "Fake_1", "gain": 3}))
    for arch in ARCHITECTURES:
        document = packaged.document(arch)
        assert document["bindingPoints"] == [{
            "nodeId": "src",
            "nodeType": "icam_source",
            "parameters": {"device": "/dev/video2"},
            "slots": [{"param": "device", "segment": 0, "element": 0,
                       "arg": "device"}],
        }], arch
        element = document["segments"][0]["elements"][0]
        assert (element["nodeId"], element["factory"], element["args"]["device"]) == (
            "src", "v4l2src", "/dev/video2"), arch

    assert packaged.version_item["camera_input_nodes"] == [{
        "node_id": "src", "node_type": "icam_source",
        "compiled_device_paths": {arch: "/dev/video2" for arch in ARCHITECTURES}}]


def test_deployment_check_asks_for_a_camera_for_the_input_source(
        harness, deployments):
    """1.2, 2.2: the deploy-time camera check covers the Input Source: no
    binding is an unbound-camera error naming it; a compatible registered
    camera satisfies it."""
    packaged = harness.package(aravis_input_definition(), ["arm64_jp7"])
    registry = {"jetson-thor1": {"never_synced": False, "cameras": {
        "arv-fake-1": {"type": "AravisDiscovered",
                       "params": {"cameraId": "Fake_1"},
                       "sync_status": "synced", "absent": False,
                       "stale": False}}}}

    errors, warnings = deployments.validate_camera_bindings(
        packaged.version_item, ["jetson-thor1"], registry, {}, [])
    assert [(e["code"], e["device"], e["nodeId"]) for e in errors] == [
        (deployments.CAMERA_ERROR_UNBOUND, "jetson-thor1", "src")]
    assert warnings == []

    errors, warnings = deployments.validate_camera_bindings(
        packaged.version_item, ["jetson-thor1"], registry,
        {"jetson-thor1": {"src": {"cameraSourceId": "arv-fake-1"}}}, [])
    assert (errors, warnings) == ([], [])


# ==========================================================================
# Property 2: Preservation
# ==========================================================================

def todays_outputs(packaging, definition_json, workflow_id):
    """Today's handler path over the stored, unexpanded graph, rebuilt
    from the unchanged pure helpers: each architecture's
    compiled_pipeline.json text, camera_input_nodes and
    has_binding_points."""
    result = parse(definition_json)
    assert result.ok, result.error
    graph = result.graph
    catalog = resolve_catalog([])
    descriptors_by_id = {d.type_id: d for d in catalog}
    camera_nodes = packaging.gather_camera_input_nodes(graph, set())
    source_nodes = packaging.gather_python_source_nodes(graph)
    hints = packaging.binding_hints_from_definition(json.loads(definition_json))

    texts, arch_points, arch_dicts = {}, {}, {}
    for arch in ARCHITECTURES:
        compiled = plain_compiled(definition_json, workflow_id, arch)
        compiled_dict = compiled.to_dict()
        points = packaging.build_binding_points(
            camera_nodes + source_nodes, compiled_dict, arch, hints,
            descriptors_by_id)
        texts[arch] = packaging.compiled_document_json(compiled, points)
        arch_points[arch] = points
        arch_dicts[arch] = compiled_dict
    records = packaging.camera_input_nodes_record(
        camera_nodes, hints, arch_points, arch_dicts)
    return texts, records, bool(camera_nodes)


@settings(deadline=None)
@given(definition=preserved_definitions())
def test_packages_outside_the_bug_condition_are_unchanged(
        harness, packaging, definition):
    """**Feature: unified-input-camera-binding, Property 2: Preservation**

    For every workflow without a camera-kind Input Source, each
    architecture's compiled_pipeline.json text and the version item's
    camera_input_nodes and has_binding_points equal today's path over the
    saved graph, byte for byte.

    **Validates: Requirements 3.1, 3.2, 3.3**
    """
    assert not is_bug_condition(definition)
    packaged = harness.package(definition)
    texts, records, has_points = todays_outputs(
        packaging, packaged.definition_json, packaged.workflow_id)

    for arch in ARCHITECTURES:
        assert packaged.compiled_texts[arch] == texts[arch], arch
    assert packaged.version_item["camera_input_nodes"] == records
    assert packaged.version_item["has_binding_points"] is has_points


def test_folder_input_source_with_camera_parameters_stays_unbound(harness):
    """3.2: a folder-kind Input Source packages as today even when it
    carries camera parameters and a hint: no binding point, no camera
    record, and the plain compiler output."""
    definition = _single_input_source(
        {"source_kind": "folder", "location": "/aws_dda/images",
         "camera_id": "Fake_1", "device": "/dev/video1", "gain": 9},
        {"cameraBindingHint": ARAVIS_HINT})
    packaged = harness.package(definition)
    for arch in ARCHITECTURES:
        assert packaged.compiled_texts[arch] == plain_compiled(
            packaged.definition_json, packaged.workflow_id, arch).to_json(), arch
    assert packaged.version_item["has_binding_points"] is False
    assert packaged.version_item["camera_input_nodes"] == []


@pytest.mark.parametrize("parameters", [
    {"source_kind": "aravis_camera", "camera_id": "Fake_1"},
    {"source_kind": "csi_camera", "gain": 12},
    {"source_kind": "icam", "device": "/dev/video4"},
], ids=["aravis", "csi", "icam"])
def test_camera_input_source_pipeline_is_the_plain_compiler_output(
        harness, parameters):
    """3.4: whatever binding metadata a camera-kind Input Source gets, its
    compiled pipeline is the compiler's own output."""
    definition = _single_input_source(parameters)
    packaged = harness.package(definition)
    for arch in ARCHITECTURES:
        document = packaged.document(arch)
        document.pop("bindingPoints", None)
        assert document == plain_compiled(
            packaged.definition_json, packaged.workflow_id, arch).to_dict(), arch
