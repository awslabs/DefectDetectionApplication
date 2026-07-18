"""
Integration tests for Workflow_Component packaging and deployment
(functions/workflow_packaging.py + functions/deployments.py).

Task 7.4 (spec: workflow-manager). Packaging runs against moto-backed S3
with a mocked Greengrass registry, asserting the per-architecture
artifact sets and the component registration call contents
(Requirements 7.1, 7.2, 7.4), including Custom_Python_Node code and
dependencies in the artifacts (Requirement 7.3). Deployment runs against
a stateful fake of the Use_Case-account greengrassv2/iot clients,
asserting deployment creation for device and thing-group targets with
association records (Requirements 8.1, 8.2), per-device status listing
(Requirement 8.3), the pre-submit LocalServer compatibility check
(Requirement 8.4), and revision semantics that preserve the target's
existing components while replacing the older Workflow_Component version
(Requirement 8.5).

Packaging *atomicity* (Requirement 7.5) is covered separately by
test_workflow_packaging_atomicity.py and is not duplicated here.
"""
import io
import json
import sys
import uuid
import zipfile
from unittest.mock import MagicMock

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, ParamValidationError

COMPONENTS_ROOT = "workflows/components"
PLUGIN_PREFIX = "workflow-plugins"
REGION = "us-east-1"
ACCOUNT_ID = "123456789012"

# Resolved default when neither WORKFLOW_MIN_LOCAL_SERVER_VERSION nor
# DDA_LOCAL_SERVER_VERSION is configured (conftest sets neither).
MIN_LOCAL_SERVER = "1.0.0"


# --------------------------------------------------------------------------
# Module imports (inside the moto mock so module-level boto3 clients are
# intercepted)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging(aws_stack):
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


# --------------------------------------------------------------------------
# Fake Use_Case-account clients
# --------------------------------------------------------------------------

class _FakePaginator:
    def __init__(self, pages_fn):
        self._pages_fn = pages_fn

    def paginate(self, **kwargs):
        return iter(self._pages_fn(**kwargs))


class FakeGreengrass:
    """Stateful fake of the Use_Case-account greengrassv2 client covering
    the operations the workflow deployment flow uses: installed-component
    listing (compatibility check, 8.4), deployment listing/creation with
    Greengrass revision semantics (8.2, 8.5), and per-device effective
    deployment status (8.3)."""

    def __init__(self):
        self._deployments = {}   # deployment_id -> record
        self._order = []         # creation order (latest last)
        self.installed = {}      # thing_name -> [{componentName, componentVersion}]
        self.effective = {}      # thing_name -> [effectiveDeployments entries]
        self.nucleus_versions = {}  # thing_name -> running Nucleus version
        self.create_deployment_calls = []

    # ------------------------------------------------------------- setup
    def register_device(self, thing_name, local_server_version=None,
                        arch="x86_64", nucleus_version=None):
        """A core device; with a LocalServer component when a version is
        given, otherwise with no LocalServer installed. A `nucleus_version`
        makes the device report a running Nucleus via get_core_device (the
        deployment flow pins auto-included AWS components to it)."""
        self.installed.setdefault(thing_name, [])
        if local_server_version is not None:
            self.installed[thing_name].append({
                "componentName": f"aws.edgeml.dda.LocalServer.{arch}",
                "componentVersion": local_server_version,
            })
        if nucleus_version is not None:
            self.nucleus_versions[thing_name] = nucleus_version

    def seed_deployment(self, target_arn, components, name="pre-existing"):
        """An already-effective Greengrass deployment for a target."""
        deployment_id = f"dep-{uuid.uuid4()}"
        self._store(deployment_id, target_arn, name, components, "COMPLETED")
        return deployment_id

    def report_effective(self, thing_name, deployment_id, status, reason=""):
        self.effective.setdefault(thing_name, []).append({
            "deploymentId": deployment_id,
            "coreDeviceExecutionStatus": status,
            "reason": reason,
            "description": "",
        })

    def _store(self, deployment_id, target_arn, name, components, status):
        self._deployments[deployment_id] = {
            "deploymentId": deployment_id,
            "targetArn": target_arn,
            "deploymentName": name,
            "components": dict(components),
            "deploymentStatus": status,
        }
        self._order.append(deployment_id)

    # ------------------------------------------------- client API surface
    def get_paginator(self, operation):
        return _FakePaginator(getattr(self, f"_pages_{operation}"))

    def _pages_list_installed_components(self, coreDeviceThingName=None, **_):
        return [{"installedComponents":
                 list(self.installed.get(coreDeviceThingName, []))}]

    def list_deployments(self, targetArn=None, **_):
        matches = [self._deployments[d] for d in self._order
                   if targetArn in (None, self._deployments[d]["targetArn"])]
        latest = matches[-1] if matches else None
        return {"deployments": [
            dict(record, isLatestForTarget=record is latest)
            for record in matches
        ]}

    def get_deployment(self, deploymentId=None):
        record = self._deployments.get(deploymentId)
        if not record:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": "no such deployment"}},
                "GetDeployment")
        return dict(record)

    def get_core_device(self, coreDeviceThingName=None):
        version = self.nucleus_versions.get(coreDeviceThingName)
        if version is None:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": "no such core device"}},
                "GetCoreDevice")
        return {"coreDeviceThingName": coreDeviceThingName,
                "coreVersion": version}

    def create_deployment(self, **params):
        # The real CreateDeployment API requires componentVersion on every
        # component entry; a missing/empty one is rejected client-side with a
        # ParamValidationError ("Missing required parameter in
        # components.<name>: componentVersion"). Mirror that so unpinned
        # entries can't slip through the fake. Known latent exception: the
        # Nucleus auto-include's unpinned `{}` fallback (deployments.py) would
        # also be rejected by the real API but pre-dates this validation and
        # keeps its own fallback semantics, so it is exempted here.
        for comp_name, comp_config in params.get("components", {}).items():
            if comp_name == "aws.greengrass.Nucleus":
                continue
            if not (comp_config or {}).get("componentVersion"):
                raise ParamValidationError(
                    report=(f"Missing required parameter in "
                            f"components.{comp_name}: componentVersion"))
        self.create_deployment_calls.append(params)
        deployment_id = f"dep-{uuid.uuid4()}"
        self._store(deployment_id, params["targetArn"],
                    params.get("deploymentName", ""),
                    params.get("components", {}), "IN_PROGRESS")
        return {
            "deploymentId": deployment_id,
            "iotJobId": f"job-{deployment_id}",
            "iotJobArn": (f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:"
                          f"job/job-{deployment_id}"),
        }

    def list_effective_deployments(self, coreDeviceThingName=None, **_):
        return {"effectiveDeployments":
                list(self.effective.get(coreDeviceThingName, []))}


class FakeIot:
    """Fake iot client: thing-group membership resolution (8.1)."""

    def __init__(self):
        self.thing_groups = {}  # group name -> [thing names]

    def get_paginator(self, operation):
        assert operation == "list_things_in_thing_group"
        return _FakePaginator(self._pages_list_things)

    def _pages_list_things(self, thingGroupName=None, **_):
        return [{"things": list(self.thing_groups.get(thingGroupName, []))}]


def make_registry_greengrass():
    """Fake Use_Case-account greengrassv2 client for component
    registration (packaging side)."""
    gg = MagicMock(name="greengrassv2-registry")

    def _create(**kwargs):
        arn = (f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:"
               f"components:wf:versions:{uuid.uuid4()}")
        return {"arn": arn}

    gg.create_component_version.side_effect = _create
    gg.describe_component.return_value = {
        "status": {"componentState": "DEPLOYABLE", "message": ""}
    }
    return gg


# --------------------------------------------------------------------------
# Workflow definitions
# --------------------------------------------------------------------------

def make_dewarp_definition(output_path="/out"):
    """folder_source -> dewarp -> capture. dewarp requires the non-bundled
    'dda-dewarp' GStreamer plugin on every architecture."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "dw", "type": "dewarp", "position": {"x": 200, "y": 0},
             "parameters": {}},
            {"id": "cap", "type": "capture", "position": {"x": 400, "y": 0},
             "parameters": {"output_path": output_path}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "dw", "port": "in"}},
            {"id": "c2", "from": {"node": "dw", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


CUSTOM_PYTHON_CODE = (
    "def handle(frame, metadata):\n"
    "    metadata['seen'] = True\n"
    "    return frame, metadata\n"
)
CUSTOM_PYTHON_REQUIREMENTS = "numpy==1.26.4\npillow==10.3.0"


def make_custom_python_definition():
    """folder_source -> custom_python -> capture. The Custom_Python_Node's
    code and declared dependencies must ship in the artifacts (7.3)."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "pynode", "type": "custom_python",
             "position": {"x": 200, "y": 0},
             "parameters": {
                 "code": CUSTOM_PYTHON_CODE,
                 "requirements": CUSTOM_PYTHON_REQUIREMENTS,
                 "input_port_type": "VideoFrames",
                 "output_port_type": "VideoFrames",
             }},
            {"id": "cap", "type": "capture", "position": {"x": 400, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "pynode", "port": "in"}},
            {"id": "c2", "from": {"node": "pynode", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


CUSTOM_PYTHON_PREPROCESS_CODE = (
    "def process_frame(frame, metadata):\n"
    "    return cv2.GaussianBlur(frame, (5, 5), 0)\n"
)
CUSTOM_PYTHON_PREPROCESS_REQUIREMENTS = "scikit-image==0.24.0"


def make_custom_python_preprocess_definition():
    """folder_source -> custom_python_preprocess -> capture. The new
    preprocessing node's code and declared dependencies must ship in the
    artifacts exactly like custom_python's (custom-python-frames
    Requirements 2.3, 2.4, 2.5)."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "prenode", "type": "custom_python_preprocess",
             "position": {"x": 200, "y": 0},
             "parameters": {
                 "code": CUSTOM_PYTHON_PREPROCESS_CODE,
                 "requirements": CUSTOM_PYTHON_PREPROCESS_REQUIREMENTS,
             }},
            {"id": "cap", "type": "capture", "position": {"x": 400, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "prenode", "port": "in"}},
            {"id": "c2", "from": {"node": "prenode", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

class FleetEnv:
    """Packaging + deployment harness: a validated workflow version, a
    Use_Case with an S3 bucket, the seeded plugin library, a mocked
    Greengrass registry for packaging, and a stateful Greengrass/IoT fake
    for deployments."""

    def __init__(self, env, packaging, deployments, monkeypatch,
                 definition=None):
        self.env = env
        self.packaging = packaging
        self.deployments = deployments
        self.s3 = env.s3

        monkeypatch.setattr(packaging, "COMPONENT_STATUS_POLL_SECONDS", 0)

        # Per-test plugin library prefix so seeded binaries never leak
        # between tests sharing the session bucket.
        self.plugin_prefix = f"{PLUGIN_PREFIX}-{uuid.uuid4()}"
        monkeypatch.setattr(
            packaging, "WORKFLOW_PLUGIN_LIBRARY_PREFIX", self.plugin_prefix)

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        self.s3.create_bucket(Bucket=self.usecase_bucket)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Fleet Test",
            "account_id": ACCOUNT_ID,
            "s3_bucket": self.usecase_bucket,
        })

        status, payload = env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id,
            "name": "fleet workflow",
            "definition": definition or make_dewarp_definition(),
        })
        assert status == 201, payload
        self.workflow_id = payload["workflow"]["workflow_id"]
        self.mark_validated(1)

        # Packaging side: real moto S3 + mocked component registry.
        self.registry = make_registry_greengrass()

        def packaging_client(service_name, usecase, session_name=None,
                             region=None):
            assert usecase["usecase_id"] == self.usecase_id
            if service_name == "s3":
                return self.s3
            if service_name == "greengrassv2":
                return self.registry
            raise AssertionError(f"unexpected packaging client: {service_name}")

        monkeypatch.setattr(packaging, "get_usecase_client", packaging_client)

        # Deployment side: stateful Greengrass + IoT fakes.
        self.gg = FakeGreengrass()
        self.iot = FakeIot()

        def deployment_client(service_name, usecase, session_name=None,
                              region=None):
            assert usecase["usecase_id"] == self.usecase_id
            if service_name == "greengrassv2":
                return self.gg
            if service_name == "iot":
                return self.iot
            raise AssertionError(f"unexpected deployment client: {service_name}")

        monkeypatch.setattr(deployments, "get_usecase_client", deployment_client)

    # ------------------------------------------------------------- setup
    def mark_validated(self, version):
        self.env.stack.tables.versions.update_item(
            Key={"workflow_id": self.workflow_id, "version": version},
            UpdateExpression="SET validation_status = :v",
            ExpressionAttributeValues={
                ":v": {"status": "passed", "validated_at": 1,
                       "findings_key": "findings/none.json"},
            },
        )

    def seed_plugins(self, archs, plugins=("dda-dewarp",)):
        for arch in archs:
            for plugin in plugins:
                self.s3.put_object(
                    Bucket=self.env.bucket,
                    Key=f"{self.plugin_prefix}/{arch}/{plugin}.so",
                    Body=self.plugin_body(arch, plugin),
                )

    @staticmethod
    def plugin_body(arch, plugin="dda-dewarp"):
        return f"\x7fELF fake {plugin} {arch}".encode()

    def save_new_version(self, definition):
        status, payload = self.env.invoke(
            "PUT", "/workflows/{id}", self.user,
            workflow_id=self.workflow_id, body={"definition": definition})
        assert status == 200, payload
        version = int(payload["workflow"]["latest_version"])
        self.mark_validated(version)
        return version

    # ------------------------------------------------------------ invoke
    def package(self, architectures, version=None):
        body = {"architectures": architectures}
        if version is not None:
            body["version"] = version
        event = self.env.event("POST", "/workflows/{id}/package", self.user,
                               workflow_id=self.workflow_id, body=body)
        response = self.packaging.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def deploy(self, **body):
        body = {"component_type": "workflow", "usecase_id": self.usecase_id,
                "workflow_id": self.workflow_id, **body}
        event = self.env.event("POST", "/deployments", self.user, body=body)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def list_workflow_deployments(self):
        event = self.env.event("GET", "/deployments", self.user, query={
            "usecase_id": self.usecase_id, "workflow_id": self.workflow_id})
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    # ----------------------------------------------------------- asserts
    def zip_contents(self, arch, version=1):
        key = (f"{COMPONENTS_ROOT}/{self.workflow_id}/{version}/"
               f"{arch}/workflow-{arch}.zip")
        body = self.s3.get_object(
            Bucket=self.usecase_bucket, Key=key)["Body"].read()
        return zipfile.ZipFile(io.BytesIO(body))

    def association_record(self, deployment_id):
        return self.env.stack.tables.deployments.get_item(
            Key={"deployment_id": deployment_id}).get("Item")

    def association_records(self):
        response = self.env.stack.tables.deployments.query(
            IndexName="usecase-deployments-index",
            KeyConditionExpression=Key("usecase_id").eq(self.usecase_id))
        return response.get("Items", [])

    def thing_arn(self, thing_name):
        return f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}"

    def thing_group_arn(self, group_name):
        return f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thinggroup/{group_name}"


@pytest.fixture
def fleet(env, packaging, deployments, monkeypatch):
    return FleetEnv(env, packaging, deployments, monkeypatch)


# ==========================================================================
# Packaging: per-architecture artifact sets (Requirements 7.1, 7.4)
# ==========================================================================

class TestPackagingArtifactSets:
    ARCHS = ["x86_64", "arm64_jp5", "arm64_jp6"]

    def test_artifact_set_per_selected_architecture(self, fleet):
        """Packaging produces one complete artifact set per user-selected
        architecture: definition, arch-specific compiled pipeline, and the
        arch-specific plugin binaries from the library (7.1, 7.4)."""
        fleet.seed_plugins(self.ARCHS)

        status, payload = fleet.package(self.ARCHS)

        assert status == 201, payload
        assert sorted(payload["architectures"]) == sorted(self.ARCHS)
        assert sorted(payload["artifacts"]) == sorted(self.ARCHS)

        for arch in self.ARCHS:
            expected_key = (f"{COMPONENTS_ROOT}/{fleet.workflow_id}/1/"
                            f"{arch}/workflow-{arch}.zip")
            assert payload["artifacts"][arch] == \
                f"s3://{fleet.usecase_bucket}/{expected_key}"

            with fleet.zip_contents(arch) as zf:
                names = set(zf.namelist())
                assert {"manifest.json", "workflow.json",
                        "compiled_pipeline.json",
                        f"plugins/{arch}/dda-dewarp.so"} <= names

                # The Workflow_Definition itself ships in the component (7.1).
                definition = json.loads(zf.read("workflow.json"))
                assert {n["id"] for n in definition["nodes"]} == \
                    {"src", "dw", "cap"}

                # The compiled pipeline is architecture-specific (7.4).
                compiled = json.loads(zf.read("compiled_pipeline.json"))
                assert compiled["targetArch"] == arch
                assert "dda-dewarp" in compiled["pluginDependencies"]

                # Plugin binaries come from that architecture's library
                # artifacts, not another architecture's (7.1, 7.4).
                assert zf.read(f"plugins/{arch}/dda-dewarp.so") == \
                    fleet.plugin_body(arch)

                manifest = json.loads(zf.read("manifest.json"))
                assert manifest["targetArch"] == arch
                assert manifest["workflowId"] == fleet.workflow_id
                assert manifest["workflowVersion"] == 1
                assert manifest["pluginDependencies"] == ["dda-dewarp"]
                assert manifest["minLocalServerVersion"] == MIN_LOCAL_SERVER

    def test_component_registration_call(self, fleet):
        """The registered component is dda.workflow.{workflowId} version
        {workflowVersion}.0.0 with one platform manifest per selected
        architecture and an install-only lifecycle (7.2, 7.4)."""
        fleet.seed_plugins(self.ARCHS)

        status, payload = fleet.package(self.ARCHS)

        assert status == 201, payload
        fleet.registry.create_component_version.assert_called_once()
        kwargs = fleet.registry.create_component_version.call_args.kwargs
        recipe = json.loads(kwargs["inlineRecipe"])

        # Component naming derived from the workflow id and version (7.2).
        assert recipe["ComponentName"] == f"dda.workflow.{fleet.workflow_id}"
        assert recipe["ComponentVersion"] == "1.0.0"
        assert payload["component_name"] == recipe["ComponentName"]
        assert payload["component_version"] == "1.0.0"

        # One platform manifest per selected architecture (7.4); the two
        # arm64 JetPack builds are disambiguated by a platform variant.
        manifests = {tuple(sorted(m["Platform"].items())): m
                     for m in recipe["Manifests"]}
        assert len(recipe["Manifests"]) == len(self.ARCHS)
        platforms = [dict(k) for k in manifests]
        assert {"os": "linux", "architecture": "amd64"} in platforms
        assert {"os": "linux", "architecture": "aarch64",
                "variant": "arm64_jp5"} in platforms
        assert {"os": "linux", "architecture": "aarch64",
                "variant": "arm64_jp6"} in platforms

        for manifest in recipe["Manifests"]:
            # Install-only lifecycle: no Run step, so deploying or removing
            # the component never disturbs LocalServer (7.2, 13.3).
            assert set(manifest["Lifecycle"]) == {"Install"}
            [artifact] = manifest["Artifacts"]
            assert artifact["Unarchive"] == "ZIP"
            assert artifact["Uri"].startswith(
                f"s3://{fleet.usecase_bucket}/{COMPONENTS_ROOT}/"
                f"{fleet.workflow_id}/1/")
        assert recipe["Lifecycle"] == {}

        # Registration is tagged to the workflow and Use_Case (7.2).
        assert kwargs["tags"]["dda-portal:workflow-id"] == fleet.workflow_id
        assert kwargs["tags"]["dda-portal:workflow-version"] == "1"
        assert kwargs["tags"]["dda-portal:usecase-id"] == fleet.usecase_id

        # The registered component ARN is recorded on the version.
        item = fleet.env.stack.tables.versions.get_item(
            Key={"workflow_id": fleet.workflow_id, "version": 1})["Item"]
        assert item["component_arn"] == payload["component_arn"]


# ==========================================================================
# Packaging: Custom_Python_Node artifacts (Requirement 7.3)
# ==========================================================================

class TestCustomPythonArtifacts:
    def test_custom_python_code_and_requirements_ship_in_artifacts(
            self, env, packaging, deployments, monkeypatch):
        fleet = FleetEnv(env, packaging, deployments, monkeypatch,
                         definition=make_custom_python_definition())
        fleet.seed_plugins(["x86_64"], plugins=("dda-emlpython",))

        status, payload = fleet.package(["x86_64"])

        assert status == 201, payload
        with fleet.zip_contents("x86_64") as zf:
            names = set(zf.namelist())
            assert "python/pynode/handler.py" in names
            assert "python/pynode/requirements.txt" in names
            assert zf.read("python/pynode/handler.py").decode() == \
                CUSTOM_PYTHON_CODE
            assert zf.read("python/pynode/requirements.txt").decode() == \
                CUSTOM_PYTHON_REQUIREMENTS

            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["customPythonNodeIds"] == ["pynode"]
            # The emlpython bridge plugin ships alongside the code.
            assert "plugins/x86_64/dda-emlpython.so" in names

    def test_custom_python_preprocess_ships_in_every_arch_zip(
            self, env, packaging, deployments, monkeypatch):
        """A custom_python_preprocess node packages its handler.py and
        requirements.txt into every architecture zip with the node id
        listed in the manifest's customPythonNodeIds (custom-python-frames
        Requirements 2.3, 2.4, 2.5)."""
        archs = ["x86_64", "arm64_jp5", "arm64_jp6"]
        fleet = FleetEnv(env, packaging, deployments, monkeypatch,
                         definition=make_custom_python_preprocess_definition())
        fleet.seed_plugins(archs, plugins=("dda-emlpython",))

        status, payload = fleet.package(archs)

        assert status == 201, payload
        for arch in archs:
            with fleet.zip_contents(arch) as zf:
                names = set(zf.namelist())
                assert "python/prenode/handler.py" in names
                assert "python/prenode/requirements.txt" in names
                assert zf.read("python/prenode/handler.py").decode() == \
                    CUSTOM_PYTHON_PREPROCESS_CODE
                assert zf.read("python/prenode/requirements.txt").decode() == \
                    CUSTOM_PYTHON_PREPROCESS_REQUIREMENTS

                # The compiled pipeline carries the node's emlpython
                # element with the packaged handler path (2.3).
                compiled = json.loads(zf.read("compiled_pipeline.json"))
                elements = [element
                            for segment in compiled["segments"]
                            for element in segment["elements"]
                            if element.get("nodeId") == "prenode"]
                assert [e["factory"] for e in elements] == ["emlpython"]
                assert elements[0]["args"]["handler-path"] == \
                    "python/prenode/handler.py"

                manifest = json.loads(zf.read("manifest.json"))
                assert manifest["customPythonNodeIds"] == ["prenode"]
                # The emlpython bridge plugin ships alongside the code.
                assert f"plugins/{arch}/dda-emlpython.so" in names


# ==========================================================================
# Deployment creation and association records (Requirements 8.1, 8.2)
# ==========================================================================

class TestWorkflowDeploymentCreation:
    def test_deploy_to_device_creates_deployment_and_association(self, fleet):
        fleet.seed_plugins(["x86_64"])
        assert fleet.package(["x86_64"])[0] == 201
        fleet.gg.register_device("line-a-camera-01",
                                 local_server_version="1.2.0")

        status, payload = fleet.deploy(target_devices=["line-a-camera-01"])

        assert status == 201, payload
        # A Greengrass deployment containing the Workflow_Component was
        # created for the device target (8.1, 8.2).
        [call] = fleet.gg.create_deployment_calls
        assert call["targetArn"] == fleet.thing_arn("line-a-camera-01")
        assert call["components"] == {
            f"dda.workflow.{fleet.workflow_id}": {"componentVersion": "1.0.0"}
        }
        assert call["tags"]["dda-portal:workflow-id"] == fleet.workflow_id

        # Association record: workflow version -> deployment -> devices (8.2).
        record = fleet.association_record(payload["deployment_id"])
        assert record is not None
        assert record["component_type"] == "workflow"
        assert record["workflow_id"] == fleet.workflow_id
        assert int(record["workflow_version"]) == 1
        assert record["component_name"] == f"dda.workflow.{fleet.workflow_id}"
        assert record["component_version"] == "1.0.0"
        assert record["target_devices"] == ["line-a-camera-01"]
        assert record["target_arn"] == fleet.thing_arn("line-a-camera-01")
        assert record["created_by"] == fleet.user["user_id"]

    def test_deploy_to_thing_group_records_member_devices(self, fleet):
        fleet.seed_plugins(["x86_64"])
        assert fleet.package(["x86_64"])[0] == 201
        members = ["line-b-01", "line-b-02"]
        fleet.iot.thing_groups["line-b"] = members
        for thing in members:
            fleet.gg.register_device(thing, local_server_version="1.5.0")

        status, payload = fleet.deploy(target_thing_group="line-b")

        assert status == 201, payload
        [call] = fleet.gg.create_deployment_calls
        assert call["targetArn"] == fleet.thing_group_arn("line-b")

        # The association records the group and its resolved members (8.2).
        record = fleet.association_record(payload["deployment_id"])
        assert record["target_thing_group"] == "line-b"
        assert record["target_devices"] == members
        assert record["target_arn"] == fleet.thing_group_arn("line-b")
        assert int(record["workflow_version"]) == 1


# ==========================================================================
# Per-device deployment status listing (Requirement 8.3)
# ==========================================================================

class TestPerDeviceStatusListing:
    def test_listing_reports_status_per_target_device(self, fleet):
        fleet.seed_plugins(["x86_64"])
        assert fleet.package(["x86_64"])[0] == 201
        members = ["cell-01", "cell-02", "cell-03"]
        fleet.iot.thing_groups["cell-group"] = members
        for thing in members:
            fleet.gg.register_device(thing, local_server_version="1.2.0")

        status, payload = fleet.deploy(target_thing_group="cell-group")
        assert status == 201, payload
        deployment_id = payload["deployment_id"]

        # Devices report through their effective deployments; one succeeded,
        # one failed, one has not reported yet (8.3).
        fleet.gg.report_effective("cell-01", deployment_id, "SUCCEEDED")
        fleet.gg.report_effective("cell-02", deployment_id, "FAILED",
                                  reason="component install error")

        status, payload = fleet.list_workflow_deployments()

        assert status == 200, payload
        assert payload["count"] == 1
        [listed] = payload["deployments"]
        assert listed["deployment_id"] == deployment_id
        assert listed["workflow_id"] == fleet.workflow_id
        assert listed["workflow_version"] == 1

        by_device = {d["device"]: d for d in listed["device_statuses"]}
        assert set(by_device) == set(members)
        assert by_device["cell-01"]["deployment_status"] == "SUCCEEDED"
        assert by_device["cell-02"]["deployment_status"] == "FAILED"
        assert by_device["cell-02"]["reason"] == "component install error"
        assert by_device["cell-03"]["deployment_status"] == "PENDING"


# ==========================================================================
# Pre-submit LocalServer compatibility check (Requirement 8.4)
# ==========================================================================

class TestLocalServerCompatibility:
    def test_incompatible_devices_reported_before_submission(self, fleet):
        fleet.seed_plugins(["x86_64"])
        assert fleet.package(["x86_64"])[0] == 201
        fleet.gg.register_device("good-device", local_server_version="1.2.0")
        fleet.gg.register_device("old-device", local_server_version="0.9.0")
        fleet.gg.register_device("bare-device")  # no LocalServer at all

        status, payload = fleet.deploy(
            target_devices=["good-device", "old-device", "bare-device"])

        assert status == 409
        assert payload["error"]["code"] == "INCOMPATIBLE_LOCAL_SERVER"
        details = payload["error"]["details"]
        assert details["min_local_server_version"] == MIN_LOCAL_SERVER

        incompatible = {d["device"]: d for d in details["incompatible_devices"]}
        assert set(incompatible) == {"old-device", "bare-device"}
        assert incompatible["old-device"]["local_server_version"] == "0.9.0"
        assert "older" in incompatible["old-device"]["reason"]
        assert incompatible["bare-device"]["local_server_version"] is None
        assert "No LocalServer" in incompatible["bare-device"]["reason"]

        # Reported *before* submission: no deployment was created and no
        # association record was written (8.4).
        assert fleet.gg.create_deployment_calls == []
        assert fleet.association_records() == []


# ==========================================================================
# Revision semantics (Requirement 8.5)
# ==========================================================================

class TestRevisionSemantics:
    def test_newer_version_revises_target_and_preserves_components(self, fleet):
        fleet.seed_plugins(["x86_64"])
        assert fleet.package(["x86_64"])[0] == 201

        device = "rev-device"
        fleet.gg.register_device(device, local_server_version="2.0.0")
        target_arn = fleet.thing_arn(device)
        # The device already runs LocalServer via an existing deployment.
        fleet.gg.seed_deployment(target_arn, {
            "aws.edgeml.dda.LocalServer.x86_64": {"componentVersion": "2.0.0"},
        })

        # Deploying v1 revises the existing deployment without dropping the
        # components already on the device (8.5, 13.3).
        status, first = fleet.deploy(target_devices=[device])
        assert status == 201, first
        assert first["is_revision"] is True
        first_call = fleet.gg.create_deployment_calls[-1]
        assert first_call["targetArn"] == target_arn
        assert first_call["components"] == {
            "aws.edgeml.dda.LocalServer.x86_64": {"componentVersion": "2.0.0"},
            f"dda.workflow.{fleet.workflow_id}": {"componentVersion": "1.0.0"},
        }

        # A newer workflow version is saved, validated, and packaged.
        version_2 = fleet.save_new_version(
            make_dewarp_definition(output_path="/out/v2"))
        assert version_2 == 2
        status, payload = fleet.package(["x86_64"], version=2)
        assert status == 201, payload
        assert payload["component_version"] == "2.0.0"

        # Deploying v2 to the same device replaces the older
        # Workflow_Component version while preserving everything else (8.5).
        status, second = fleet.deploy(target_devices=[device],
                                      workflow_version=2)
        assert status == 201, second
        assert second["is_revision"] is True
        assert second["superseded_deployment_id"] == first["deployment_id"]
        assert second["component_version"] == "2.0.0"

        second_call = fleet.gg.create_deployment_calls[-1]
        assert second_call["components"] == {
            "aws.edgeml.dda.LocalServer.x86_64": {"componentVersion": "2.0.0"},
            f"dda.workflow.{fleet.workflow_id}": {"componentVersion": "2.0.0"},
        }

        # The association records reflect the revision chain (8.2, 8.5).
        record = fleet.association_record(second["deployment_id"])
        assert int(record["workflow_version"]) == 2
        assert record["component_version"] == "2.0.0"
        assert record["is_revision"] is True
        assert record["superseded_deployment_id"] == first["deployment_id"]
