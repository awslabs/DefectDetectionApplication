"""
Test-run error path unit tests (workflow-manager Requirements 12.10,
12.11, 12.12).

Task 11.5 (spec: workflow-manager).

Exercised against the shared moto stack from conftest.py with the
TestDatasets / TestRuns tables created module-scoped here (they are not
part of the shared conftest stack):

1. Dataset upload boundaries (12.11, functions/workflow_testing.py):
   the declared-size pre-check accepts exactly 500 MB and rejects one
   byte over with DATASET_TOO_LARGE persisting nothing; unsupported
   declared extensions / content types are rejected at initiate; the
   finalize step re-verifies against what actually landed in S3 -
   actual total size over the limit and non-JPEG/PNG magic bytes each
   reject with the reason, delete every uploaded object, and persist
   no TestDatasets record.
2. Validator/compiler failure short-circuit (12.12,
   functions/workflow_test_steps.py): step_validate on a definition
   with validation errors writes per-node error records to the results
   document, marks the TestRuns item failed, and returns {ok: False}
   without ever compiling or executing the pipeline; step_compile with
   an unmapped target architecture short-circuits identically.
3. Mid-run failure retention (12.10): step_collect on a results
   document where one node failed marks the run failed with the
   failing node identified while the completed records produced before
   the failure remain readable; step_record_timeout marks the run
   failed-with-timeout leaving the partial results document untouched.

_Requirements: 12.10, 12.11, 12.12_
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION, TEST_ENV

TEST_DATASETS_TABLE_NAME = "test-test-datasets"
TEST_RUNS_TABLE_NAME = "test-test-runs"

MB = 1024 * 1024
MAX_DATASET_BYTES = 500 * MB

# Tiny but genuine JPEG/PNG heads (magic bytes are all the verifier reads).
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

# Catalog-conformant definition: camera_source -> capture with every
# required parameter set (validates clean, compiles for x86_64).
VALID_DEFINITION = {
    "schemaVersion": 1,
    "nodes": [
        {"id": "n1", "type": "camera_source",
         "position": {"x": 100, "y": 100}, "parameters": {}},
        {"id": "n2", "type": "capture",
         "position": {"x": 350, "y": 100},
         "parameters": {"output_path": "/data/captures"}},
    ],
    "connections": [
        {"id": "c1",
         "from": {"node": "n1", "port": "out"},
         "to": {"node": "n2", "port": "in"}},
    ],
}

# Same shape but the capture node is missing its required output_path
# parameter -> V4_MISSING_REQUIRED_PARAMETER on n2.
INVALID_DEFINITION = {
    "schemaVersion": 1,
    "nodes": [
        {"id": "n1", "type": "camera_source",
         "position": {"x": 100, "y": 100}, "parameters": {}},
        {"id": "n2", "type": "capture",
         "position": {"x": 350, "y": 100}, "parameters": {}},
    ],
    "connections": [
        {"id": "c1",
         "from": {"node": "n1", "port": "out"},
         "to": {"node": "n2", "port": "in"}},
    ],
}


@pytest.fixture(scope="module")
def testing_env(aws_stack):
    """TestDatasets/TestRuns tables plus freshly imported workflow_testing
    and workflow_test_steps modules bound to them inside moto."""
    import boto3

    os.environ["TEST_DATASETS_TABLE"] = TEST_DATASETS_TABLE_NAME
    os.environ["TEST_RUNS_TABLE"] = TEST_RUNS_TABLE_NAME

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TEST_DATASETS_TABLE_NAME,
        KeySchema=[{"AttributeName": "dataset_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "dataset_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-datasets-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=TEST_RUNS_TABLE_NAME,
        KeySchema=[{"AttributeName": "test_run_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "test_run_id", "AttributeType": "S"},
            {"AttributeName": "workflow_id", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "workflow-runs-index",
            "KeySchema": [
                {"AttributeName": "workflow_id", "KeyType": "HASH"},
                {"AttributeName": "started_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so the modules bind the table names above and
    # moto-intercepted boto3 clients (conftest pattern).
    # node_catalog_resolution is popped too so the merged-catalog
    # resolution the steps handler now performs (custom-node-designer
    # 12.1) binds moto-intercepted clients as well.
    for module_name in ("workflow_testing", "workflow_test_steps",
                        "node_catalog_resolution"):
        sys.modules.pop(module_name, None)
    import workflow_testing
    import workflow_test_steps

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        stack=aws_stack,
        testing=workflow_testing,
        steps=workflow_test_steps,
        datasets_table=resource.Table(TEST_DATASETS_TABLE_NAME),
        runs_table=resource.Table(TEST_RUNS_TABLE_NAME),
        s3=boto3.client("s3", region_name=REGION),
        bucket=TEST_ENV["PORTAL_ARTIFACTS_BUCKET"],
    )


@pytest.fixture
def ctx(env):
    """A fresh Use_Case and a DataScientist (workflow:test) on it."""
    usecase_id = env.create_usecase()
    user = env.make_user()
    env.assign_role(user, usecase_id, "DataScientist")
    return SimpleNamespace(usecase_id=usecase_id, user=user)


def invoke(testing_env, ctx, method, resource, body=None, resource_id=None):
    """Invoke the workflow_testing handler; returns (status, parsed body)."""
    event = {
        "httpMethod": method,
        "resource": resource,
        "path": resource.replace("{id}", resource_id or ""),
        "pathParameters": {"id": resource_id} if resource_id else None,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": ctx.user["user_id"],
                    "email": ctx.user["email"],
                    "cognito:username": ctx.user["username"],
                    "custom:role": ctx.user["role"],
                }
            }
        },
    }
    response = testing_env.testing.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def initiate(testing_env, ctx, files, name="ds"):
    return invoke(testing_env, ctx, "POST", "/test-datasets", {
        "usecase_id": ctx.usecase_id, "name": name, "files": files,
    })


def upload_and_finalize(testing_env, ctx, files):
    """Initiate, upload each file's content as a single part, finalize.

    ``files`` is [{name, content, declared_size?}]; a declared_size
    smaller than the content lets a test slip past the declared-size
    pre-check so the finalize-time server-side verification is what
    trips. Returns (status, body, s3_prefix)."""
    declared = [{"name": f["name"],
                 "size": f.get("declared_size", len(f["content"]))}
                for f in files]
    status, body = initiate(testing_env, ctx, declared)
    assert status == 201, body
    prefix = body["s3_prefix"]

    finalize_files = []
    for spec, upload in zip(files, body["upload"]["files"]):
        part = testing_env.s3.upload_part(
            Bucket=testing_env.bucket, Key=upload["key"],
            UploadId=upload["upload_id"], PartNumber=1,
            Body=spec["content"],
        )
        finalize_files.append({
            "key": upload["key"],
            "upload_id": upload["upload_id"],
            "parts": [{"part_number": 1, "etag": part["ETag"]}],
        })

    status, body = invoke(testing_env, ctx, "POST", "/test-datasets", {
        "action": "finalize",
        "usecase_id": ctx.usecase_id,
        "dataset_id": body["dataset_id"],
        "name": "ds",
        "files": finalize_files,
    })
    return status, body, prefix


def dataset_records(testing_env, usecase_id):
    """TestDatasets items of one Use_Case (fresh per test)."""
    items = testing_env.datasets_table.scan()["Items"]
    return [i for i in items if i.get("usecase_id") == usecase_id]


def objects_under(testing_env, prefix):
    response = testing_env.s3.list_objects_v2(
        Bucket=testing_env.bucket, Prefix=prefix)
    return [o["Key"] for o in response.get("Contents", [])]


# ===========================================================================
# 1. Dataset upload boundaries (Requirement 12.11)
# ===========================================================================

class TestDatasetUploadBoundaries:

    def test_declared_total_exactly_500mb_is_accepted(self, testing_env, ctx):
        """The declared-size pre-check accepts a total of exactly 500 MB
        and hands back presigned multipart uploads (12.3, 12.11)."""
        status, body = initiate(testing_env, ctx, [
            {"name": "a.jpg", "size": 400 * MB},
            {"name": "b.png", "size": 100 * MB},
        ])
        assert status == 201
        assert len(body["upload"]["files"]) == 2
        assert all(f["parts"] for f in body["upload"]["files"])
        # Nothing is committed until finalize verifies the content.
        assert dataset_records(testing_env, ctx.usecase_id) == []

    def test_declared_total_one_byte_over_500mb_is_rejected(self, testing_env,
                                                            ctx):
        """One byte over the 500 MB declared total rejects with
        DATASET_TOO_LARGE and persists nothing (12.11)."""
        status, body = initiate(testing_env, ctx, [
            {"name": "a.jpg", "size": 400 * MB},
            {"name": "b.png", "size": 100 * MB + 1},
        ])
        assert status == 400
        assert body["error"]["code"] == "DATASET_TOO_LARGE"
        assert body["error"]["details"]["total_bytes"] == MAX_DATASET_BYTES + 1
        assert body["error"]["details"]["max_bytes"] == MAX_DATASET_BYTES

        assert dataset_records(testing_env, ctx.usecase_id) == []
        assert objects_under(
            testing_env, f"workflows/{ctx.usecase_id}/test-datasets/") == []

    def test_unsupported_declared_extension_is_rejected(self, testing_env, ctx):
        """A declared file with a non-JPEG/PNG extension rejects with
        UNSUPPORTED_FORMAT identifying the file (12.11)."""
        status, body = initiate(testing_env, ctx, [
            {"name": "ok.png", "size": 10},
            {"name": "movie.gif", "size": 10},
        ])
        assert status == 400
        assert body["error"]["code"] == "UNSUPPORTED_FORMAT"
        assert body["error"]["details"]["file"] == "movie.gif"
        assert dataset_records(testing_env, ctx.usecase_id) == []

    def test_unsupported_declared_content_type_is_rejected(self, testing_env,
                                                           ctx):
        """A declared content type outside image/jpeg+image/png rejects
        with UNSUPPORTED_FORMAT (12.11)."""
        status, body = initiate(testing_env, ctx, [
            {"name": "sneaky.png", "size": 10, "content_type": "image/gif"},
        ])
        assert status == 400
        assert body["error"]["code"] == "UNSUPPORTED_FORMAT"
        assert dataset_records(testing_env, ctx.usecase_id) == []

    def test_finalize_actual_size_over_limit_rejects_and_cleans_up(
            self, testing_env, ctx, monkeypatch):
        """Server-side verification uses the sizes that actually landed in
        S3: a total over the limit rejects with DATASET_TOO_LARGE, deletes
        every uploaded object, and persists no record (12.11)."""
        # Shrink the module limit so real (small) uploads can exceed it.
        # The client declares sizes under the limit (passing the initiate
        # pre-check) but uploads more - only the finalize-time check
        # against actual S3 object sizes can catch that.
        monkeypatch.setattr(testing_env.testing, "MAX_DATASET_BYTES",
                            len(JPEG_BYTES) + len(PNG_BYTES) - 1)
        status, body, prefix = upload_and_finalize(testing_env, ctx, [
            {"name": "a.jpg", "content": JPEG_BYTES, "declared_size": 1},
            {"name": "b.png", "content": PNG_BYTES, "declared_size": 1},
        ])
        assert status == 400
        assert body["error"]["code"] == "DATASET_TOO_LARGE"
        assert body["error"]["details"]["total_bytes"] == \
            len(JPEG_BYTES) + len(PNG_BYTES)

        # Uploaded objects are removed and nothing is persisted.
        assert objects_under(testing_env, prefix) == []
        assert dataset_records(testing_env, ctx.usecase_id) == []

    def test_finalize_magic_byte_failure_rejects_and_cleans_up(self,
                                                               testing_env,
                                                               ctx):
        """An uploaded object whose content is not a JPEG/PNG (magic-byte
        check) rejects with UNSUPPORTED_FORMAT naming the file, deletes the
        uploaded objects, and persists no record (12.11)."""
        status, body, prefix = upload_and_finalize(testing_env, ctx, [
            {"name": "real.png", "content": PNG_BYTES},
            {"name": "fake.png", "content": b"GIF89a not an image at all"},
        ])
        assert status == 400
        assert body["error"]["code"] == "UNSUPPORTED_FORMAT"
        assert body["error"]["details"]["file"] == "fake.png"

        assert objects_under(testing_env, prefix) == []
        assert dataset_records(testing_env, ctx.usecase_id) == []

    def test_finalize_within_limits_commits_the_dataset(self, testing_env,
                                                        ctx):
        """Sanity check of the accept path: verified uploads within limits
        commit exactly one TestDatasets record (12.3)."""
        status, body, prefix = upload_and_finalize(testing_env, ctx, [
            {"name": "a.jpg", "content": JPEG_BYTES},
            {"name": "b.png", "content": PNG_BYTES},
        ])
        assert status == 201
        assert body["dataset"]["file_count"] == 2
        assert body["dataset"]["total_bytes"] == len(JPEG_BYTES) + len(PNG_BYTES)
        assert body["dataset"]["format"] == "jpeg+png"
        assert len(objects_under(testing_env, prefix)) == 2
        assert len(dataset_records(testing_env, ctx.usecase_id)) == 1


# ===========================================================================
# 2. Validator / compiler failure short-circuit (Requirement 12.12)
# ===========================================================================

def stage_run(testing_env, definition, target_arch="x86_64",
              usecase_id=None, custom_node_type_pins=None):
    """Stage a stored definition + TestRuns item; returns the step input."""
    test_run_id = f"run-{uuid.uuid4()}"
    usecase_id = usecase_id or f"uc-{uuid.uuid4()}"
    definition_key = f"workflows/{usecase_id}/defs/{test_run_id}.json"
    results_key = f"workflows/{usecase_id}/test-runs/{test_run_id}/results.json"
    testing_env.s3.put_object(
        Bucket=testing_env.bucket, Key=definition_key,
        Body=json.dumps(definition).encode("utf-8"),
    )
    testing_env.runs_table.put_item(Item={
        "test_run_id": test_run_id,
        "workflow_id": "wf-1",
        "usecase_id": usecase_id,
        "status": "running",
        "started_at": 1,
        "results_s3_key": results_key,
        "failure": None,
    })
    return {
        "test_run_id": test_run_id,
        "workflow_id": "wf-1",
        "workflow_version": 1,
        "usecase_id": usecase_id,
        "definition_s3_key": definition_key,
        "results_s3_key": results_key,
        "artifacts_bucket": testing_env.bucket,
        "target_arch": target_arch,
        "simulation": True,
        "custom_node_type_pins": custom_node_type_pins or {},
    }


def read_results_document(testing_env, results_key):
    obj = testing_env.s3.get_object(Bucket=testing_env.bucket, Key=results_key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def get_run(testing_env, test_run_id):
    return testing_env.runs_table.get_item(
        Key={"test_run_id": test_run_id})["Item"]


def compiled_key_absent(testing_env, results_key):
    compiled_key = results_key.rsplit("/", 1)[0] + "/compiled_pipeline.json"
    listed = testing_env.s3.list_objects_v2(
        Bucket=testing_env.bucket, Prefix=compiled_key)
    return listed.get("KeyCount", 0) == 0


class TestValidationAndCompileShortCircuit:

    def test_step_validate_errors_short_circuit_the_run(self, testing_env):
        """step_validate on a definition with validation errors records
        each error with its node identifier in the results document, marks
        the TestRuns item failed, and returns {ok: False} - the pipeline
        is never compiled or executed (12.12)."""
        inp = stage_run(testing_env, INVALID_DEFINITION)
        outcome = testing_env.steps.handler(
            {"step": "validate", "input": inp}, None)

        assert outcome["ok"] is False
        assert outcome["stage"] == "validate"
        assert outcome["errors"]

        # Per-node error records in the results document (12.7, 12.12).
        document = read_results_document(testing_env, inp["results_s3_key"])
        assert document["nodes"]
        for record in document["nodes"]:
            assert record["status"] == "error"
            assert record["error"]["code"]
            assert record["error"]["message"]
        assert any(r["nodeId"] == "n2" for r in document["nodes"])

        # TestRuns item failed with the failing node and no timeout.
        run = get_run(testing_env, inp["test_run_id"])
        assert run["status"] == "failed"
        assert run["failure"]["nodeId"] == "n2"
        assert run["failure"]["timeout"] is False
        assert "not" in run["failure"]["message"] and \
            "executed" in run["failure"]["message"]

        # The pipeline was never executed: no compiled document staged.
        assert compiled_key_absent(testing_env, inp["results_s3_key"])

    def test_step_compile_errors_short_circuit_the_run(self, testing_env):
        """step_compile on a valid definition with an unmapped target
        architecture records each compile error with its node identifier,
        marks the run failed, and returns {ok: False} without staging a
        compiled document (12.12)."""
        inp = stage_run(testing_env, VALID_DEFINITION,
                        target_arch="not-a-real-arch")
        # The definition itself validates clean.
        assert testing_env.steps.handler(
            {"step": "validate", "input": inp}, None)["ok"] is True

        outcome = testing_env.steps.handler(
            {"step": "compile", "input": inp}, None)

        assert outcome["ok"] is False
        assert outcome["stage"] == "compile"
        assert all(e["code"] == "UNMAPPED_ARCHITECTURE"
                   for e in outcome["errors"])

        document = read_results_document(testing_env, inp["results_s3_key"])
        node_ids = {r["nodeId"] for r in document["nodes"]}
        assert node_ids == {"n1", "n2"}
        assert all(r["status"] == "error" for r in document["nodes"])

        run = get_run(testing_env, inp["test_run_id"])
        assert run["status"] == "failed"
        assert run["failure"]["nodeId"] in ("n1", "n2")
        assert run["failure"]["timeout"] is False

        assert compiled_key_absent(testing_env, inp["results_s3_key"])

    def test_step_compile_succeeds_for_mapped_architecture(self, testing_env):
        """Control: the same valid definition compiles for x86_64 and
        stages the compiled document (the short-circuit above is caused by
        the unmapped architecture, not the definition)."""
        inp = stage_run(testing_env, VALID_DEFINITION)
        outcome = testing_env.steps.handler(
            {"step": "compile", "input": inp}, None)
        assert outcome["ok"] is True
        assert not compiled_key_absent(testing_env, inp["results_s3_key"])
        assert get_run(testing_env, inp["test_run_id"])["status"] == "running"


# ===========================================================================
# 3. Mid-run failure retention (Requirement 12.10)
# ===========================================================================

COMPLETED_RECORD = {
    "nodeId": "n1", "status": "completed",
    "outputs": [{"frame": 1}], "stubActivity": [], "error": None,
}
FAILED_RECORD = {
    "nodeId": "n2", "status": "failed", "outputs": [], "stubActivity": [],
    "error": {"code": "RUNTIME_ERROR", "message": "capture write failed"},
}


class TestMidRunFailureRetention:

    def put_results(self, testing_env, inp, records):
        testing_env.s3.put_object(
            Bucket=testing_env.bucket, Key=inp["results_s3_key"],
            Body=json.dumps({"nodes": records}).encode("utf-8"),
        )

    def test_step_collect_marks_failed_node_and_retains_prior_results(
            self, testing_env):
        """step_collect on a results document with a mid-run node failure
        marks the run failed with the failing node identified and the
        error description, while the per-node records produced before the
        failure remain in the results document (12.10)."""
        inp = stage_run(testing_env, VALID_DEFINITION)
        self.put_results(testing_env, inp, [COMPLETED_RECORD, FAILED_RECORD])

        outcome = testing_env.steps.handler(
            {"step": "collect", "input": inp}, None)

        assert outcome["status"] == "failed"
        assert outcome["failing_node_id"] == "n2"
        assert outcome["node_count"] == 2

        run = get_run(testing_env, inp["test_run_id"])
        assert run["status"] == "failed"
        assert run["failure"]["nodeId"] == "n2"
        assert run["failure"]["message"] == "capture write failed"
        assert run["failure"]["timeout"] is False

        # Partial results are retained: the completed record is untouched.
        document = read_results_document(testing_env, inp["results_s3_key"])
        assert document["nodes"] == [COMPLETED_RECORD, FAILED_RECORD]

    def test_step_collect_completes_when_no_record_failed(self, testing_env):
        """Control: all-completed records finalize the run as completed."""
        inp = stage_run(testing_env, VALID_DEFINITION)
        self.put_results(testing_env, inp, [COMPLETED_RECORD])

        outcome = testing_env.steps.handler(
            {"step": "collect", "input": inp}, None)
        assert outcome == {"status": "completed", "node_count": 1}
        assert get_run(testing_env, inp["test_run_id"])["status"] == "completed"

    def test_step_record_timeout_marks_timeout_and_keeps_partial_results(
            self, testing_env):
        """step_record_timeout marks the run failed with a timeout
        indication and leaves the partial results document untouched
        (12.13, retention per 12.10)."""
        inp = stage_run(testing_env, VALID_DEFINITION)
        self.put_results(testing_env, inp, [COMPLETED_RECORD])

        outcome = testing_env.steps.handler(
            {"step": "record_timeout", "input": inp}, None)
        assert outcome == {"ok": True, "status": "failed", "timeout": True}

        run = get_run(testing_env, inp["test_run_id"])
        assert run["status"] == "failed"
        assert run["failure"]["timeout"] is True
        assert "10 minute" in run["failure"]["message"]

        document = read_results_document(testing_env, inp["results_s3_key"])
        assert document["nodes"] == [COMPLETED_RECORD]

    def test_step_record_failure_marks_failed_and_keeps_partial_results(
            self, testing_env):
        """step_record_failure (sandbox task failure) marks the run failed
        with the delivered cause while partial results are retained
        (12.10)."""
        inp = stage_run(testing_env, VALID_DEFINITION)
        self.put_results(testing_env, inp, [COMPLETED_RECORD])

        inp = dict(inp, errorInfo={"Error": "States.TaskFailed",
                                   "Cause": "sandbox container exited 1"})
        outcome = testing_env.steps.handler(
            {"step": "record_failure", "input": inp}, None)
        assert outcome == {"ok": True, "status": "failed", "timeout": False}

        run = get_run(testing_env, inp["test_run_id"])
        assert run["status"] == "failed"
        assert run["failure"]["message"] == "sandbox container exited 1"
        assert run["failure"]["timeout"] is False

        document = read_results_document(testing_env, inp["results_s3_key"])
        assert document["nodes"] == [COMPLETED_RECORD]


# ===========================================================================
# 3b. Readable failure messages for ECS task causes
# ===========================================================================

def ecs_task_cause(**overrides):
    """A representative ECS task description blob as Step Functions
    delivers it in errorInfo.Cause for a failed Fargate RunTask.sync."""
    task = {
        "Attachments": [{
            "Id": "att-1", "Type": "eni", "Status": "DELETED",
            "Details": [{"Name": "networkInterfaceId",
                         "Value": "eni-0123456789abcdef0"}],
        }],
        "ClusterArn": "arn:aws:ecs:us-east-1:123456789012:cluster/test",
        "TaskArn": "arn:aws:ecs:us-east-1:123456789012:task/test/abc123",
        "LastStatus": "STOPPED",
        "StopCode": "EssentialContainerExited",
        "StoppedReason": "Essential container in task exited",
        "Containers": [{
            "Name": "sandbox",
            "ExitCode": 1,
            "LastStatus": "STOPPED",
        }],
    }
    task.update(overrides)
    return json.dumps(task)


class TestReadableEcsFailureMessages:
    """step_record_failure with an ECS task JSON Cause records a concise
    human-readable message instead of the raw blob (Attachments/eni/...)."""

    def record_failure(self, testing_env, cause):
        inp = stage_run(testing_env, VALID_DEFINITION)
        inp = dict(inp, errorInfo={"Error": "States.TaskFailed",
                                   "Cause": cause})
        outcome = testing_env.steps.handler(
            {"step": "record_failure", "input": inp}, None)
        assert outcome == {"ok": True, "status": "failed", "timeout": False}
        return get_run(testing_env, inp["test_run_id"])

    def test_exit_code_yields_concise_container_message(self, testing_env):
        """A stopped-task blob with a container exit code records 'The
        sandbox container exited with code 1' - not the raw JSON."""
        run = self.record_failure(testing_env, ecs_task_cause())
        message = run["failure"]["message"]
        assert message == "The sandbox container exited with code 1"
        assert "Attachments" not in message
        assert "eni" not in message

    def test_container_reason_is_preferred_with_exit_code_appended(
            self, testing_env):
        """A container-level Reason (e.g. image pull / OOM) wins, with the
        exit code appended when known."""
        cause = ecs_task_cause(Containers=[{
            "Name": "sandbox",
            "ExitCode": 137,
            "Reason": "OutOfMemoryError: Container killed due to memory usage",
        }])
        run = self.record_failure(testing_env, cause)
        assert run["failure"]["message"] == \
            ("OutOfMemoryError: Container killed due to memory usage "
             "(exit code 137)")

    def test_stopped_reason_used_when_no_container_detail(self, testing_env):
        """Without container Reason/ExitCode the task StoppedReason is
        used; StopCode is the last resort."""
        cause = ecs_task_cause(
            Containers=[{"Name": "sandbox", "LastStatus": "STOPPED"}],
            StoppedReason="Timeout waiting for network interface provisioning")
        run = self.record_failure(testing_env, cause)
        assert run["failure"]["message"] == \
            "Timeout waiting for network interface provisioning"

    def test_non_json_cause_keeps_truncation_fallback(self, testing_env):
        """A plain-text Cause keeps the existing truncate-at-512 fallback."""
        run = self.record_failure(testing_env, "x" * 600)
        assert run["failure"]["message"] == "x" * 512

    def test_json_cause_without_ecs_fields_keeps_fallback(self, testing_env):
        """JSON that is not an ECS task shape (no Containers/StoppedReason/
        StopCode/TaskArn) is stored as-is via the fallback path."""
        cause = json.dumps({"errorMessage": "boom", "errorType": "Runtime"})
        run = self.record_failure(testing_env, cause)
        assert run["failure"]["message"] == cause


# ===========================================================================
# 4. Step-level progress reporting (live status while a run executes)
# ===========================================================================

def progress_messages(run):
    """The messages of the run's `progress` entries, in append order."""
    entries = run.get("progress") or []
    for entry in entries:
        assert set(entry) == {"at", "message"}
        # DynamoDB stores numbers as Decimal; the API layer converts.
        assert int(entry["at"]) > 0
    return [entry["message"] for entry in entries]


class TestRunProgressReporting:
    """Each state-machine step appends human-readable {at, message}
    entries to the TestRuns item's `progress` list, and GET
    /test-runs/{id} surfaces it (together with the per-node results
    flushed so far) so the portal can show what the run is doing while
    the user waits."""

    def test_step_validate_appends_validating_then_passed(self, testing_env):
        inp = stage_run(testing_env, VALID_DEFINITION)
        testing_env.steps.handler({"step": "validate", "input": inp}, None)

        run = get_run(testing_env, inp["test_run_id"])
        assert progress_messages(run) == [
            "Validating the workflow definition",
            "Validation passed",
        ]
        # Status semantics are unchanged by progress recording.
        assert run["status"] == "running"

    def test_progress_entries_accumulate_across_steps(self, testing_env):
        """Entries append in run order: the compile step announces itself
        on entry; on success it appends the compilation result and the
        sandbox-start entry, because the Fargate RunSandbox state that
        follows runs no Lambda of its own."""
        inp = stage_run(testing_env, VALID_DEFINITION)
        testing_env.steps.handler({"step": "validate", "input": inp}, None)
        outcome = testing_env.steps.handler(
            {"step": "compile", "input": inp}, None)
        assert outcome["ok"] is True

        run = get_run(testing_env, inp["test_run_id"])
        assert progress_messages(run) == [
            "Validating the workflow definition",
            "Validation passed",
            "Compiling for x86_64 (simulation mode)",
            "Compilation succeeded",
            "Starting the sandbox container...",
        ]

    def test_failed_compile_appends_a_failure_entry(self, testing_env):
        """A short-circuited compile never reaches the sandbox: no
        sandbox-start entry is appended, and marking the run failed
        appends a failure entry (12.12)."""
        inp = stage_run(testing_env, VALID_DEFINITION,
                        target_arch="not-a-real-arch")
        outcome = testing_env.steps.handler(
            {"step": "compile", "input": inp}, None)
        assert outcome["ok"] is False

        run = get_run(testing_env, inp["test_run_id"])
        messages = progress_messages(run)
        assert messages[0] == "Compiling for not-a-real-arch (simulation mode)"
        assert messages[-1].startswith("Test run failed: ")
        assert "Starting the sandbox container..." not in messages
        assert run["status"] == "failed"

    def test_step_collect_appends_collecting_then_completed(self, testing_env):
        inp = stage_run(testing_env, VALID_DEFINITION)
        testing_env.s3.put_object(
            Bucket=testing_env.bucket, Key=inp["results_s3_key"],
            Body=json.dumps({"nodes": [COMPLETED_RECORD]}).encode("utf-8"),
        )
        testing_env.steps.handler({"step": "collect", "input": inp}, None)

        run = get_run(testing_env, inp["test_run_id"])
        assert progress_messages(run) == [
            "Collecting per-node results",
            "Test run completed",
        ]
        assert run["status"] == "completed"

    def test_record_timeout_appends_a_failure_entry(self, testing_env):
        inp = stage_run(testing_env, VALID_DEFINITION)
        testing_env.steps.handler({"step": "record_timeout", "input": inp}, None)

        run = get_run(testing_env, inp["test_run_id"])
        messages = progress_messages(run)
        assert messages[-1] == ("Test run failed: Test run exceeded the "
                                "10 minute execution limit")

    def test_get_test_run_returns_progress_and_partial_results(
            self, testing_env, ctx):
        """GET /test-runs/{id} on an in-flight run returns the progress
        entries plus the per-node results the sandbox has flushed
        incrementally so far, so the portal can render live progress."""
        test_run_id = f"run-{uuid.uuid4()}"
        results_key = (f"workflows/{ctx.usecase_id}/test-runs/"
                       f"{test_run_id}/results.json")
        testing_env.runs_table.put_item(Item={
            "test_run_id": test_run_id,
            "workflow_id": "wf-1",
            "usecase_id": ctx.usecase_id,
            "status": "running",
            "progress": [
                {"at": 1000, "message": "Validating the workflow definition"},
                {"at": 2000, "message": "Validation passed"},
                {"at": 3000, "message": "Starting the sandbox container..."},
            ],
            "started_at": 1,
            "results_s3_key": results_key,
            "failure": None,
        })
        # Two of the workflow's nodes have reported so far (12.10).
        testing_env.s3.put_object(
            Bucket=testing_env.bucket, Key=results_key,
            Body=json.dumps({"nodes": [COMPLETED_RECORD,
                                       COMPLETED_RECORD]}).encode("utf-8"),
        )

        status, body = invoke(testing_env, ctx, "GET", "/test-runs/{id}",
                              resource_id=test_run_id)
        assert status == 200
        assert body["test_run"]["status"] == "running"
        assert body["test_run"]["progress"] == [
            {"at": 1000, "message": "Validating the workflow definition"},
            {"at": 2000, "message": "Validation passed"},
            {"at": 3000, "message": "Starting the sandbox container..."},
        ]
        assert len(body["node_results"]) == 2


# ===========================================================================
# 5. Simulated inference outcome on test-run start (Requirement 12.6)
# ===========================================================================

class FakeStepFunctions:
    """Records start_execution calls the way workflow_testing issues them."""

    def __init__(self):
        self.calls = []

    def start_execution(self, **kwargs):
        self.calls.append(kwargs)
        return {"executionArn":
                "arn:aws:states:us-east-1:123456789012:execution:test:"
                + kwargs.get("name", "x")}


class TestSimulatedInferenceValidation:
    """validate_simulated_inference: shape validation of the optional
    simulated_inference request field (model inference is stubbed in the
    cloud sandbox; the user configures the injected outcome)."""

    def validate(self, testing_env, value):
        return testing_env.testing.validate_simulated_inference(value)

    def test_absent_yields_the_default(self, testing_env):
        normalized, err = self.validate(testing_env, None)
        assert err is None
        assert normalized == {"is_anomalous": False, "confidence": 0.9}

    def test_valid_payload_is_normalized(self, testing_env):
        normalized, err = self.validate(
            testing_env, {"is_anomalous": True, "confidence": 0.25})
        assert err is None
        assert normalized == {"is_anomalous": True, "confidence": 0.25}

    def test_partial_payload_fills_defaults(self, testing_env):
        normalized, err = self.validate(testing_env, {"is_anomalous": True})
        assert err is None
        assert normalized == {"is_anomalous": True, "confidence": 0.9}

    def test_boundary_confidences_accepted(self, testing_env):
        for confidence in (0, 1, 0.0, 1.0):
            normalized, err = self.validate(
                testing_env, {"confidence": confidence})
            assert err is None
            assert normalized["confidence"] == float(confidence)

    def _assert_rejected(self, testing_env, value):
        normalized, err = self.validate(testing_env, value)
        assert normalized is None
        assert err["statusCode"] == 400
        body = json.loads(err["body"])
        assert body["error"]["code"] == "INVALID_SIMULATED_INFERENCE"

    def test_bad_shapes_rejected(self, testing_env):
        self._assert_rejected(testing_env, "anomalous")
        self._assert_rejected(testing_env, [1, 2])
        self._assert_rejected(testing_env, {"is_anomalous": "yes"})
        self._assert_rejected(testing_env, {"confidence": "0.9"})
        self._assert_rejected(testing_env, {"confidence": True})
        self._assert_rejected(testing_env, {"confidence": -0.1})
        self._assert_rejected(testing_env, {"confidence": 1.1})
        self._assert_rejected(testing_env, {"is_anomalous": False,
                                            "extra": 1})


class TestSimulatedInferenceStartRun:
    """POST /workflows/{id}/test-runs forwards the validated
    simulated_inference outcome into the state machine input (including
    the pre-serialized simulated_inference_json the RunSandbox container
    override maps to the SIMULATED_INFERENCE env value), and rejects bad
    shapes with 400 before any execution is started (12.6)."""

    def stage(self, testing_env, ctx, monkeypatch):
        """A stored workflow version + dataset in ctx's Use_Case, with the
        Step Functions client stubbed to record executions."""
        import boto3
        resource = boto3.resource("dynamodb", region_name=REGION)
        workflow_id = f"wf-{uuid.uuid4()}"
        resource.Table(TEST_ENV["WORKFLOWS_TABLE"]).put_item(Item={
            "workflow_id": workflow_id,
            "usecase_id": ctx.usecase_id,
            "name": "wf",
            "latest_version": 1,
        })
        definition_key = f"workflows/{ctx.usecase_id}/defs/{workflow_id}.json"
        testing_env.s3.put_object(
            Bucket=testing_env.bucket, Key=definition_key,
            Body=json.dumps(VALID_DEFINITION).encode("utf-8"),
        )
        resource.Table(TEST_ENV["WORKFLOW_VERSIONS_TABLE"]).put_item(Item={
            "workflow_id": workflow_id,
            "version": 1,
            "s3_definition_key": definition_key,
        })
        dataset_id = f"ds-{uuid.uuid4()}"
        testing_env.datasets_table.put_item(Item={
            "dataset_id": dataset_id,
            "usecase_id": ctx.usecase_id,
            "s3_prefix": f"workflows/{ctx.usecase_id}/test-datasets/{dataset_id}/",
        })
        fake_sfn = FakeStepFunctions()
        monkeypatch.setattr(testing_env.testing, "stepfunctions", fake_sfn)
        monkeypatch.setattr(testing_env.testing, "TEST_RUN_STATE_MACHINE_ARN",
                            "arn:aws:states:us-east-1:123456789012:"
                            "stateMachine:test-runner")
        return workflow_id, dataset_id, fake_sfn

    def start(self, testing_env, ctx, workflow_id, body):
        return invoke(testing_env, ctx, "POST", "/workflows/{id}/test-runs",
                      body, resource_id=workflow_id)

    def execution_input(self, fake_sfn):
        assert len(fake_sfn.calls) == 1
        return json.loads(fake_sfn.calls[0]["input"])

    def test_default_outcome_forwarded_when_absent(self, testing_env, ctx,
                                                   monkeypatch):
        workflow_id, dataset_id, fake_sfn = self.stage(
            testing_env, ctx, monkeypatch)
        status, body = self.start(testing_env, ctx, workflow_id,
                                  {"dataset_id": dataset_id})
        assert status == 202, body
        inp = self.execution_input(fake_sfn)
        assert inp["simulated_inference"] == {"is_anomalous": False,
                                              "confidence": 0.9}
        assert json.loads(inp["simulated_inference_json"]) == \
            inp["simulated_inference"]

    def test_configured_outcome_forwarded(self, testing_env, ctx, monkeypatch):
        workflow_id, dataset_id, fake_sfn = self.stage(
            testing_env, ctx, monkeypatch)
        status, body = self.start(testing_env, ctx, workflow_id, {
            "dataset_id": dataset_id,
            "simulated_inference": {"is_anomalous": True, "confidence": 0.25},
        })
        assert status == 202, body
        inp = self.execution_input(fake_sfn)
        assert inp["simulated_inference"] == {"is_anomalous": True,
                                              "confidence": 0.25}
        assert json.loads(inp["simulated_inference_json"]) == \
            {"is_anomalous": True, "confidence": 0.25}

    def test_bad_shape_rejected_before_any_execution(self, testing_env, ctx,
                                                     monkeypatch):
        workflow_id, dataset_id, fake_sfn = self.stage(
            testing_env, ctx, monkeypatch)
        status, body = self.start(testing_env, ctx, workflow_id, {
            "dataset_id": dataset_id,
            "simulated_inference": {"confidence": 2},
        })
        assert status == 400
        assert body["error"]["code"] == "INVALID_SIMULATED_INFERENCE"
        assert fake_sfn.calls == []


# ===========================================================================
# 5. Custom_Node_Types in cloud test runs
#    (custom-node-designer task 13.1, Requirements 12.1, 12.2)
# ===========================================================================

from test_custom_node_types import make_declaration  # noqa: E402


def custom_definition(type_id):
    """camera_source -> <custom type> (VideoFrames in/out) -> capture."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "n1", "type": "camera_source",
             "position": {"x": 100, "y": 100}, "parameters": {}},
            {"id": "n2", "type": type_id,
             "position": {"x": 350, "y": 100}, "parameters": {"radius": 5}},
            {"id": "n3", "type": "capture",
             "position": {"x": 600, "y": 100},
             "parameters": {"output_path": "/data/captures"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "n1", "port": "out"},
             "to": {"node": "n2", "port": "in"}},
            {"id": "c2", "from": {"node": "n2", "port": "out"},
             "to": {"node": "n3", "port": "in"}},
        ],
    }


def seed_custom_node_type(testing_env, usecase_id, type_id,
                          plugin_name, with_artifact=True):
    """A registered Custom_Node_Type version item plus its backing
    Plugin_Record — with or without a successful x86_64 Plugin_Artifact
    promoted to the Plugin_Library prefix of the portal bucket."""
    plugin_id = f"plg-{uuid.uuid4().hex[:8]}"
    artifacts = {}
    if with_artifact:
        so_key = (f"workflow-plugins/custom/{usecase_id}/x86_64/"
                  f"{plugin_name}.so")
        testing_env.s3.put_object(Bucket=testing_env.bucket, Key=so_key,
                                  Body=b"\x7fELF custom plugin bytes")
        artifacts["x86_64"] = {
            "buildStatus": "succeeded", "s3Key": so_key,
            "checksum": "abc123", "signature": "c2ln", "logTail": "",
        }
    testing_env.stack.tables.plugin_records.put_item(Item={
        "plugin_id": plugin_id, "version": 1, "usecase_id": usecase_id,
        "name": plugin_name, "lifecycle_state": "test",
        "artifacts": artifacts,
    })
    declaration = make_declaration(type_id)
    for mapping in declaration["mappings"]:
        mapping["pluginDependencies"] = [f"custom:{usecase_id}/{plugin_name}"]
    testing_env.stack.tables.custom_node_types.put_item(Item={
        "node_type_id": type_id, "version": 1, "usecase_id": usecase_id,
        "plugin_id": plugin_id, "plugin_version": 1,
        "declaration": declaration, "deprecated": False,
    })
    return plugin_id


def compiled_document(testing_env, inp):
    key = inp["results_s3_key"].rsplit("/", 1)[0] + "/compiled_pipeline.json"
    obj = testing_env.s3.get_object(Bucket=testing_env.bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def elements_for_node(document, node_id):
    return [e for s in document["segments"] for e in s["elements"]
            if e.get("nodeId") == node_id]


def read_custom_plugins_manifest(testing_env, inp):
    key = inp["results_s3_key"].rsplit("/", 1)[0] + "/custom_plugins.json"
    obj = testing_env.s3.get_object(Bucket=testing_env.bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def manifest_absent(testing_env, inp):
    key = inp["results_s3_key"].rsplit("/", 1)[0] + "/custom_plugins.json"
    listed = testing_env.s3.list_objects_v2(
        Bucket=testing_env.bucket, Prefix=key)
    return listed.get("KeyCount", 0) == 0


class TestCustomPluginCompile:
    """The compile step compiles against the merged catalog of the run's
    Use_Case, stages custom x86_64 Plugin_Artifacts under the run's
    prefix for the sandbox task, and substitutes the pass-through
    recording stub for Custom_Node_Types without an x86_64 build
    (custom-node-designer 12.1, 12.2)."""

    def test_compile_uses_merged_catalog_and_stages_custom_plugin(
            self, testing_env):
        """A workflow using a Custom_Node_Type with a successful x86_64
        Plugin_Artifact validates and compiles against the merged catalog;
        the real declared element chain is emitted for the custom node and
        the artifact is staged under the run's plugins/ prefix with a
        manifest entry (12.1)."""
        usecase_id = f"uc-{uuid.uuid4()}"
        type_id = "custom.blur_real"
        seed_custom_node_type(testing_env, usecase_id, type_id, "blurplug",
                              with_artifact=True)
        inp = stage_run(testing_env, custom_definition(type_id),
                        usecase_id=usecase_id)

        # The merged catalog makes the custom type a known type (12.1).
        assert testing_env.steps.handler(
            {"step": "validate", "input": inp}, None)["ok"] is True

        outcome = testing_env.steps.handler(
            {"step": "compile", "input": inp}, None)
        assert outcome["ok"] is True, outcome
        assert outcome["stubbed_custom_node_types"] == []

        # The custom node compiled to its real declared element chain.
        document = compiled_document(testing_env, inp)
        factories = [e["factory"] for e in elements_for_node(document, "n2")]
        assert factories == ["blurregions"]

        # The x86_64 artifact is staged under the run's plugins/ prefix.
        manifest = read_custom_plugins_manifest(testing_env, inp)
        assert manifest["stubbedNodeTypeIds"] == []
        assert [p["nodeTypeId"] for p in manifest["plugins"]] == [type_id]
        staged_key = manifest["plugins"][0]["s3Key"]
        assert staged_key == (inp["results_s3_key"].rsplit("/", 1)[0]
                              + "/plugins/blurplug.so")
        staged = testing_env.s3.get_object(
            Bucket=testing_env.bucket, Key=staged_key)
        assert staged["Body"].read() == b"\x7fELF custom plugin bytes"

    def test_compile_stubs_custom_node_without_x86_64_artifact(
            self, testing_env):
        """A Custom_Node_Type without a successful x86_64 Plugin_Artifact
        is substituted with the pass-through recording stub — an identity
        element named custom_stub_<nodeId> — and identified as stubbed in
        the manifest; nothing is staged for it (12.2)."""
        usecase_id = f"uc-{uuid.uuid4()}"
        type_id = "custom.blur_stubbed"
        seed_custom_node_type(testing_env, usecase_id, type_id, "stubplug",
                              with_artifact=False)
        inp = stage_run(testing_env, custom_definition(type_id),
                        usecase_id=usecase_id)

        outcome = testing_env.steps.handler(
            {"step": "compile", "input": inp}, None)
        assert outcome["ok"] is True, outcome
        assert outcome["stubbed_custom_node_types"] == [type_id]

        # The stub passes frames through unchanged and carries the
        # per-node name the harness identifies stubbed nodes by.
        document = compiled_document(testing_env, inp)
        elements = elements_for_node(document, "n2")
        assert [e["factory"] for e in elements] == ["identity"]
        assert elements[0]["args"]["name"] == "custom_stub_n2"
        assert "custom:" not in json.dumps(document.get("pluginDependencies"))

        manifest = read_custom_plugins_manifest(testing_env, inp)
        assert manifest["plugins"] == []
        assert manifest["stubbedNodeTypeIds"] == [type_id]

        # No artifact staged under the run's plugins/ prefix.
        plugins_prefix = (inp["results_s3_key"].rsplit("/", 1)[0]
                          + "/plugins/")
        assert objects_under(testing_env, plugins_prefix) == []

    def test_builtin_only_run_writes_no_custom_manifest(self, testing_env):
        """Runs without custom nodes stay byte-identical to the
        pre-existing flow: no custom_plugins.json is written."""
        inp = stage_run(testing_env, VALID_DEFINITION)
        outcome = testing_env.steps.handler(
            {"step": "compile", "input": inp}, None)
        assert outcome["ok"] is True
        assert outcome["stubbed_custom_node_types"] == []
        assert manifest_absent(testing_env, inp)

    def test_stub_decision_is_exactly_artifact_absence(self, testing_env):
        """Pure decision examples: a type is stubbed iff its backing
        record lacks a successful x86_64 artifact entry (12.2)."""
        steps = testing_env.steps
        entries = {
            "t.real": {"buildStatus": "succeeded", "s3Key": "k.so"},
            "t.failed": {"buildStatus": "failed", "logTail": "boom"},
            "t.keyless": {"buildStatus": "succeeded"},
            "t.missing": None,
        }
        stubbed = steps.stubbed_custom_type_ids(sorted(entries), entries)
        assert stubbed == frozenset({"t.failed", "t.keyless", "t.missing"})
