"""
Unit tests for plugin_simulator.py (custom-node-designer task 8.2).

Covers the simulator start guard (409 describing the missing x86_64
build, 7.5), run starts with Test_Dataset or uploaded sample frame
input (7.1), re-runs with changed parameter values (7.4), the
SimulationRuns record shape, GET /simulations/{runId} status/results
(7.3), the state machine steps (guard / prepare staging under the
run's prefix / collect), and the timeout/failure recorders retaining
flushed partial results (7.6, 7.7).

Runs against the moto-backed stack from conftest.py (real Step
Functions state machine, DynamoDB, S3).
"""
import base64
import json
import uuid

import pytest

from conftest import TEST_ENV


class SimulatorEnv:
    """Facade for invoking the Plugin_Simulator API in tests."""

    def __init__(self, stack):
        self.stack = stack
        self.module = stack.plugin_simulator
        self.records = stack.plugin_records
        self.s3 = stack.s3
        self.bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]

    # ------------------------------------------------------------- setup
    def create_usecase(self):
        usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": usecase_id,
            "name": "Simulator Test Use Case",
            "account_id": "123456789012",
        })
        return usecase_id

    def make_user(self, role="Viewer"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def assign_role(self, user, usecase_id, role):
        self.stack.tables.user_roles.put_item(Item={
            "user_id": user["user_id"],
            "usecase_id": usecase_id,
            "role": role,
        })

    def make_admin(self, usecase_id):
        admin = self.make_user()
        self.assign_role(admin, usecase_id, "UseCaseAdmin")
        return admin

    def create_plugin(self, user, usecase_id, name="blur-regions"):
        event = self._event("POST", "/plugins", user, body={
            "usecase_id": usecase_id, "name": name, "kind": "scaffold"})
        response = self.records.handler(event, None)
        body = json.loads(response["body"])
        assert response["statusCode"] == 201, body
        return body["plugin"]

    def record_x86_64_artifact(self, plugin, data=b"\x7fELF-shared-object",
                               plugin_name="blur-regions"):
        """Store a successful x86_64 Plugin_Artifact on the record + in S3."""
        so_key = (f"workflow-plugins/custom/{plugin['usecase_id']}"
                  f"/x86_64/{plugin_name}.so")
        self.s3.put_object(Bucket=self.bucket, Key=so_key, Body=data)
        self.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"],
                 "version": plugin["version"]},
            UpdateExpression="SET artifacts.x86_64 = :entry",
            ExpressionAttributeValues={":entry": {
                "buildStatus": "succeeded",
                "s3Key": so_key,
                "checksum": "aa" * 32,
                "signature": "sig",
            }},
        )
        return so_key

    def create_dataset(self, usecase_id, frames=("frame1.jpg", "frame2.jpg")):
        dataset_id = f"ds-{uuid.uuid4()}"
        prefix = f"workflows/{usecase_id}/test-datasets/{dataset_id}/"
        for name in frames:
            self.s3.put_object(Bucket=self.bucket, Key=prefix + name,
                               Body=b"\xff\xd8\xff fake-jpeg")
        self.stack.tables.test_datasets.put_item(Item={
            "dataset_id": dataset_id,
            "usecase_id": usecase_id,
            "s3_prefix": prefix,
            "created_at": 1,
        })
        return dataset_id, prefix

    def get_run_item(self, run_id):
        return self.module.get_run_item(run_id)

    # ----------------------------------------------------------- invoke
    def _event(self, method, resource, user, plugin_id=None, version=None,
               run_id=None, body=None):
        path_params = {}
        if plugin_id is not None:
            path_params = {"id": plugin_id, "v": str(version)}
        if run_id is not None:
            path_params = {"runId": run_id}
        return {
            "httpMethod": method,
            "resource": resource,
            "path": resource,
            "pathParameters": path_params or None,
            "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": user["user_id"],
                        "email": user["email"],
                        "cognito:username": user["username"],
                        "custom:role": user["role"],
                    }
                }
            },
        }

    def post_simulate(self, user, plugin_id, version, body=None):
        event = self._event("POST", "/plugins/{id}/versions/{v}/simulate",
                            user, plugin_id=plugin_id, version=version,
                            body=body or {})
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def get_simulation(self, user, run_id):
        event = self._event("GET", "/simulations/{runId}", user, run_id=run_id)
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def run_step(self, step, step_input):
        return self.module.handler({"step": step, "input": step_input}, None)

    # ------------------------------------------------------ conveniences
    def started_run(self, body_extra=None, frames=("a.jpg", "b.jpg")):
        """Create usecase/admin/plugin with x86_64 artifact and start a run."""
        usecase_id = self.create_usecase()
        admin = self.make_admin(usecase_id)
        plugin = self.create_plugin(admin, usecase_id)
        self.record_x86_64_artifact(plugin)
        dataset_id, dataset_prefix = self.create_dataset(usecase_id, frames)
        body = {"dataset_id": dataset_id, "parameters": {"radius": 5}}
        body.update(body_extra or {})
        status, response = self.post_simulate(
            admin, plugin["plugin_id"], plugin["version"], body)
        assert status == 202, response
        run = response["simulation_run"]
        return {"usecase_id": usecase_id, "admin": admin, "plugin": plugin,
                "dataset_id": dataset_id, "dataset_prefix": dataset_prefix,
                "run": run}


@pytest.fixture
def senv(aws_stack):
    return SimulatorEnv(aws_stack)


class TestSimulationStartGuard:
    """The x86_64-artifact guard (7.5)."""

    def test_refuses_without_x86_64_artifact(self, senv):
        usecase_id = senv.create_usecase()
        admin = senv.make_admin(usecase_id)
        plugin = senv.create_plugin(admin, usecase_id)
        dataset_id, _ = senv.create_dataset(usecase_id)

        status, body = senv.post_simulate(
            admin, plugin["plugin_id"], plugin["version"],
            {"dataset_id": dataset_id})

        assert status == 409
        assert body["error"]["code"] == "SIMULATION_REQUIRES_X86_64_BUILD"
        # The refusal describes that simulation requires a successful
        # x86_64 build (7.5).
        assert "x86_64" in body["error"]["message"]
        assert body["error"]["details"]["missing"] == (
            "successful x86_64 Plugin_Artifact")

    def test_refuses_when_x86_64_build_failed(self, senv):
        usecase_id = senv.create_usecase()
        admin = senv.make_admin(usecase_id)
        plugin = senv.create_plugin(admin, usecase_id)
        senv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"],
                 "version": plugin["version"]},
            UpdateExpression="SET artifacts.x86_64 = :entry",
            ExpressionAttributeValues={":entry": {
                "buildStatus": "failed", "logTail": "compile error"}},
        )
        dataset_id, _ = senv.create_dataset(usecase_id)

        status, body = senv.post_simulate(
            admin, plugin["plugin_id"], plugin["version"],
            {"dataset_id": dataset_id})

        assert status == 409
        assert body["error"]["details"]["x86_64_build_status"] == "failed"

    def test_succeeded_build_on_other_arch_does_not_satisfy_guard(self, senv):
        usecase_id = senv.create_usecase()
        admin = senv.make_admin(usecase_id)
        plugin = senv.create_plugin(admin, usecase_id)
        senv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"],
                 "version": plugin["version"]},
            UpdateExpression="SET artifacts.arm64_jp5 = :entry",
            ExpressionAttributeValues={":entry": {
                "buildStatus": "succeeded", "s3Key": "some/key.so"}},
        )
        dataset_id, _ = senv.create_dataset(usecase_id)

        status, body = senv.post_simulate(
            admin, plugin["plugin_id"], plugin["version"],
            {"dataset_id": dataset_id})

        assert status == 409
        assert body["error"]["code"] == "SIMULATION_REQUIRES_X86_64_BUILD"


class TestSimulationStart:
    """POST /plugins/{id}/versions/{v}/simulate (7.1, 7.4)."""

    def test_starts_run_with_dataset_and_parameters(self, senv):
        ctx = senv.started_run()
        run = ctx["run"]

        assert run["status"] == "running"
        assert run["parameters"] == {"radius": 5}
        assert run["dataset"] == {"kind": "dataset",
                                  "dataset_id": ctx["dataset_id"]}
        assert run["results_s3_key"].startswith(
            f"plugin-simulations/{ctx['usecase_id']}/{run['run_id']}/")

        item = senv.get_run_item(run["run_id"])
        assert item["execution_arn"]
        assert item["created_by"] == ctx["admin"]["user_id"]

    def test_rerun_with_changed_parameters_is_a_new_run(self, senv):
        """A re-run passes changed parameter values (7.4)."""
        ctx = senv.started_run()
        status, body = senv.post_simulate(
            ctx["admin"], ctx["plugin"]["plugin_id"],
            ctx["plugin"]["version"],
            {"dataset_id": ctx["dataset_id"], "parameters": {"radius": 42}})

        assert status == 202
        rerun = body["simulation_run"]
        assert rerun["run_id"] != ctx["run"]["run_id"]
        assert rerun["parameters"] == {"radius": 42}
        # The first run's recorded parameters are untouched.
        assert senv.get_run_item(ctx["run"]["run_id"])["parameters"] == {
            "radius": 5}

    def test_starts_run_with_uploaded_sample_frames(self, senv):
        usecase_id = senv.create_usecase()
        admin = senv.make_admin(usecase_id)
        plugin = senv.create_plugin(admin, usecase_id)
        senv.record_x86_64_artifact(plugin)

        content = base64.b64encode(b"\xff\xd8\xff fake-jpeg").decode()
        status, body = senv.post_simulate(
            admin, plugin["plugin_id"], plugin["version"],
            {"sample_frames": [{"name": "up1.jpg", "content_base64": content},
                               {"name": "up2.jpg", "content_base64": content}]})

        assert status == 202, body
        run = body["simulation_run"]
        assert run["dataset"] == {"kind": "uploaded", "frame_count": 2}
        uploads_prefix = (f"plugin-simulations/{usecase_id}"
                          f"/{run['run_id']}/uploads/")
        listed = senv.s3.list_objects_v2(Bucket=senv.bucket,
                                         Prefix=uploads_prefix)
        names = sorted(o["Key"][len(uploads_prefix):]
                       for o in listed.get("Contents", []))
        assert names == ["up1.jpg", "up2.jpg"]

    def test_requires_exactly_one_input_source(self, senv):
        usecase_id = senv.create_usecase()
        admin = senv.make_admin(usecase_id)
        plugin = senv.create_plugin(admin, usecase_id)
        senv.record_x86_64_artifact(plugin)
        dataset_id, _ = senv.create_dataset(usecase_id)
        content = base64.b64encode(b"x").decode()

        for body in (
            {},
            {"dataset_id": dataset_id,
             "sample_frames": [{"name": "a.jpg", "content_base64": content}]},
        ):
            status, response = senv.post_simulate(
                admin, plugin["plugin_id"], plugin["version"], body)
            assert status == 400
            assert response["error"]["code"] == "MISSING_INPUT"

    def test_dataset_of_another_usecase_is_not_found(self, senv):
        usecase_id = senv.create_usecase()
        admin = senv.make_admin(usecase_id)
        plugin = senv.create_plugin(admin, usecase_id)
        senv.record_x86_64_artifact(plugin)
        other_usecase = senv.create_usecase()
        foreign_dataset, _ = senv.create_dataset(other_usecase)

        status, body = senv.post_simulate(
            admin, plugin["plugin_id"], plugin["version"],
            {"dataset_id": foreign_dataset})

        assert status == 404
        assert body["error"]["code"] == "TEST_DATASET_NOT_FOUND"

    def test_read_only_role_cannot_simulate(self, senv):
        usecase_id = senv.create_usecase()
        admin = senv.make_admin(usecase_id)
        plugin = senv.create_plugin(admin, usecase_id)
        senv.record_x86_64_artifact(plugin)
        dataset_id, _ = senv.create_dataset(usecase_id)
        viewer = senv.make_user()
        senv.assign_role(viewer, usecase_id, "DataScientist")

        status, body = senv.post_simulate(
            viewer, plugin["plugin_id"], plugin["version"],
            {"dataset_id": dataset_id})

        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"

    def test_cross_tenant_user_cannot_simulate(self, senv):
        """A UseCaseAdmin of a different Use_Case cannot start runs here
        (13.1: own Use_Case only). The shared RBAC layer resolves users
        without a role record to read-only, so the denial is a 403 on the
        simulate permission (same shape the node-designer RBAC suite
        asserts)."""
        usecase_id = senv.create_usecase()
        admin = senv.make_admin(usecase_id)
        plugin = senv.create_plugin(admin, usecase_id)
        senv.record_x86_64_artifact(plugin)
        other_usecase = senv.create_usecase()
        outsider = senv.make_admin(other_usecase)

        status, body = senv.post_simulate(
            outsider, plugin["plugin_id"], plugin["version"],
            {"dataset_id": "irrelevant"})

        assert status in (403, 404)


class TestSimulationStatusAndResults:
    """GET /simulations/{runId} (7.3, 7.7)."""

    def test_returns_status_and_results_document(self, senv):
        ctx = senv.started_run()
        run = ctx["run"]
        results_doc = {
            "status": "completed",
            "frames": [
                {"frameIndex": 0, "inputRef": "frames/input_00000.jpg",
                 "outputRef": "frames/output_00000.jpg",
                 "metadata": {"score": 0.4}},
            ],
        }
        senv.s3.put_object(Bucket=senv.bucket, Key=run["results_s3_key"],
                           Body=json.dumps(results_doc).encode())

        status, body = senv.get_simulation(ctx["admin"], run["run_id"])

        assert status == 200
        assert body["simulation_run"]["run_id"] == run["run_id"]
        assert body["results"] == results_doc

    def test_missing_results_document_returns_none(self, senv):
        ctx = senv.started_run()
        status, body = senv.get_simulation(ctx["admin"], ctx["run"]["run_id"])
        assert status == 200
        assert body["results"] is None

    def test_unknown_run_is_not_found(self, senv):
        ctx = senv.started_run()
        status, body = senv.get_simulation(ctx["admin"], "no-such-run")
        assert status == 404
        assert body["error"]["code"] == "SIMULATION_RUN_NOT_FOUND"


class TestStateMachineSteps:
    """Guard / Prepare / Collect / recorders (7.2, 7.5, 7.6, 7.7)."""

    def _execution_input(self, ctx):
        run = ctx["run"]
        item = ctx["plugin"]
        return {
            "run_id": run["run_id"],
            "plugin_id": item["plugin_id"],
            "version": item["version"],
            "usecase_id": ctx["usecase_id"],
            "results_s3_key": run["results_s3_key"],
            "input_kind": "dataset",
            "source_dataset_s3_prefix": ctx["dataset_prefix"],
            "plugin_source_s3_key": (
                f"workflow-plugins/custom/{ctx['usecase_id']}"
                "/x86_64/blur-regions.so"),
        }

    def test_guard_step_passes_with_artifact(self, senv):
        ctx = senv.started_run()
        result = senv.run_step("guard", self._execution_input(ctx))
        assert result == {"ok": True}

    def test_guard_step_marks_run_failed_without_artifact(self, senv):
        ctx = senv.started_run()
        senv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": ctx["plugin"]["plugin_id"],
                 "version": ctx["plugin"]["version"]},
            UpdateExpression="REMOVE artifacts.x86_64",
        )

        result = senv.run_step("guard", self._execution_input(ctx))

        assert result["ok"] is False
        assert result["error"]["code"] == "SIMULATION_REQUIRES_X86_64_BUILD"
        item = senv.get_run_item(ctx["run"]["run_id"])
        assert item["status"] == "failed"
        assert "x86_64" in item["failure"]["message"]

    def test_prepare_stages_dataset_and_plugin_under_run_prefix(self, senv):
        """Prepare copies everything into plugin-simulations/... so the
        sandbox task role never needs Plugin_Library or dataset access
        (7.2)."""
        ctx = senv.started_run(frames=("f1.jpg", "f2.jpg"))
        run_prefix = (f"plugin-simulations/{ctx['usecase_id']}"
                      f"/{ctx['run']['run_id']}/")

        result = senv.run_step("prepare", self._execution_input(ctx))

        assert result["dataset_s3_prefix"] == run_prefix + "inputs/"
        assert result["plugin_s3_key"] == run_prefix + "plugin/blur-regions.so"
        # Every staged object sits under the run's prefix.
        listed = senv.s3.list_objects_v2(Bucket=senv.bucket, Prefix=run_prefix)
        staged = sorted(o["Key"][len(run_prefix):]
                        for o in listed.get("Contents", []))
        assert staged == ["inputs/f1.jpg", "inputs/f2.jpg",
                          "plugin/blur-regions.so"]

    def test_prepare_uses_uploads_prefix_for_uploaded_frames(self, senv):
        ctx = senv.started_run()
        step_input = self._execution_input(ctx)
        uploads_prefix = (f"plugin-simulations/{ctx['usecase_id']}"
                          f"/{ctx['run']['run_id']}/uploads/")
        step_input.update({"input_kind": "uploaded",
                           "source_dataset_s3_prefix": uploads_prefix})

        result = senv.run_step("prepare", step_input)

        assert result["dataset_s3_prefix"] == uploads_prefix

    def test_collect_marks_run_completed(self, senv):
        ctx = senv.started_run()
        senv.s3.put_object(
            Bucket=senv.bucket, Key=ctx["run"]["results_s3_key"],
            Body=json.dumps({"status": "completed", "frames": []}).encode())

        senv.run_step("collect", self._execution_input(ctx))

        item = senv.get_run_item(ctx["run"]["run_id"])
        assert item["status"] == "completed"
        assert item["finished_at"]

    def test_collect_marks_run_failed_from_harness_error(self, senv):
        ctx = senv.started_run()
        senv.s3.put_object(
            Bucket=senv.bucket, Key=ctx["run"]["results_s3_key"],
            Body=json.dumps({
                "status": "failed",
                "error": {"code": "PIPELINE_EXECUTION_ERROR",
                          "message": "plugin crashed: segfault in blur"},
                "frames": [{"frameIndex": 0}],
            }).encode())

        senv.run_step("collect", self._execution_input(ctx))

        item = senv.get_run_item(ctx["run"]["run_id"])
        assert item["status"] == "failed"
        assert "segfault" in item["failure"]["message"]

    def test_record_timeout_marks_failed_and_retains_partial_results(self, senv):
        """7.7: failed-with-timeout, flushed partial results untouched."""
        ctx = senv.started_run()
        partial = {"status": "running",
                   "frames": [{"frameIndex": 0, "metadata": {}}]}
        senv.s3.put_object(Bucket=senv.bucket,
                           Key=ctx["run"]["results_s3_key"],
                           Body=json.dumps(partial).encode())

        senv.run_step("record_timeout", self._execution_input(ctx))

        item = senv.get_run_item(ctx["run"]["run_id"])
        assert item["status"] == "failed"
        assert item["failure"]["timeout"] is True
        assert "5 minute" in item["failure"]["message"]
        # The partial results document survives for display (7.7).
        status, body = senv.get_simulation(ctx["admin"], ctx["run"]["run_id"])
        assert status == 200
        assert body["results"] == partial

    def test_record_failure_carries_harness_error_output(self, senv):
        """7.6: the failure report includes the plugin's error output."""
        ctx = senv.started_run()
        senv.s3.put_object(
            Bucket=senv.bucket, Key=ctx["run"]["results_s3_key"],
            Body=json.dumps({
                "status": "failed",
                "error": {"message": "gst-launch error: assertion failed "
                                     "in blurregions"},
            }).encode())

        senv.run_step("record_failure", self._execution_input(ctx))

        item = senv.get_run_item(ctx["run"]["run_id"])
        assert item["status"] == "failed"
        assert item["failure"]["timeout"] is False
        assert "blurregions" in item["failure"]["message"]

    def test_failure_error_output_propagates_through_get_simulation(self, senv):
        """7.6 end to end across the backend: the plugin's error output in
        the flushed failure document (the shape the simulate harness
        writes on abnormal plugin termination — see the containerized
        test-sandbox suite) reaches the caller of
        GET /simulations/{runId}, together with the retained partial
        frame results (custom-node-designer task 8.5)."""
        ctx = senv.started_run()
        flushed = {
            "element": "blurregions",
            "parameters": {"radius": 5},
            "status": "failed",
            "frameCount": 4,
            "frames": [
                {"frameIndex": 0,
                 "inputRef": "frames/input_00000.jpg",
                 "outputRef": "frames/output_00000.jpg",
                 "metadata": {"bytes": 2048}},
                {"frameIndex": 1,
                 "inputRef": "frames/input_00001.jpg",
                 "outputRef": "frames/output_00001.jpg",
                 "metadata": {"bytes": 2011}},
            ],
            "error": {
                "code": "PIPELINE_EXECUTION_ERROR",
                "message": "Pipeline failed with: Internal data stream "
                           "error. (element blurregions0)",
                "errorOutput": "blurregions0: assertion 'frame != NULL' "
                               "failed\nsegmentation fault (core dumped)",
            },
        }
        senv.s3.put_object(Bucket=senv.bucket,
                           Key=ctx["run"]["results_s3_key"],
                           Body=json.dumps(flushed).encode())

        # The state machine's failure recorder finalizes the run item.
        senv.run_step("record_failure", self._execution_input(ctx))

        status, body = senv.get_simulation(ctx["admin"], ctx["run"]["run_id"])

        assert status == 200
        run = body["simulation_run"]
        assert run["status"] == "failed"
        assert run["failure"]["timeout"] is False
        # The run failure carries the harness error message...
        assert "blurregions0" in run["failure"]["message"]
        # ...and the full flushed document — the plugin's error output
        # included — is returned for display (7.6).
        assert body["results"]["error"]["errorOutput"] == (
            flushed["error"]["errorOutput"])
        assert body["results"]["error"]["code"] == "PIPELINE_EXECUTION_ERROR"
        # The partial frame results produced before the failure survive.
        assert [f["frameIndex"] for f in body["results"]["frames"]] == [0, 1]
