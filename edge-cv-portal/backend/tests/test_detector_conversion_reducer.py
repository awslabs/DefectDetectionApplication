"""
The Conversion_Status reducer: ``plan_conversion_transition``,
``plan_finalize_transition``, ``apply_conversion_transition`` and
``reconcile_conversion`` (detector-checkpoint-import task 2.4).

* The design.md section 6 table, row by row.
* A fake table with real conditional-write semantics (a tiny evaluator for
  exactly the `SET a.b = :v, ...` / `a.b = :v` forms the reducer emits; any
  other syntax fails the test). Hypothesis drives interleaved writers --
  EventBridge, two concurrent sync-on-read GETs and the packaging finalize --
  over stale reads, and checks that terminal states are never overwritten, that
  only the allowed edges are taken and that exactly one finalize claim is made.
* The same expressions against moto's DynamoDB, so the syntax is real.
# Validates: Requirements 7.1, 7.2, 7.4, 7.5, 7.6, 7.7
"""
import copy
import re

import boto3
import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings, strategies as st

import detector_conversion as dc
from conftest import REGION

ARTIFACT = "s3://uc-bucket/models/conversion/job/job/output/model.tar.gz"
FATAL = ("AlgorithmError: FATAL: checkpoint task is 'segment'; only object detection "
         "('detect') converts, exit code: 1")
ALLOWED_EDGES = {("InProgress", "Finalizing"), ("InProgress", "Failed"),
                 ("Finalizing", "Completed"), ("Finalizing", "Failed")}


def record(status="InProgress", **extra):
    rec = {"training_id": "t-1", "source": "imported", "model_type": "object_detection",
           "runtime": "onnx", "status": "InProgress", "progress": 10,
           "conversion": {"status": status, "job_name": "j"}}
    rec.update(extra)
    return rec


# ---------------------------------------------------------------------------
# The design.md section 6 table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sm", ["InProgress", "Starting", "Downloading", "Training", "Uploading",
                                None, "Stopping"])
def test_running_job_moves_nothing(sm):
    assert dc.plan_conversion_transition(record(), sm, None, None, 1) is None


def test_completed_with_artifact_claims_finalizing():
    t = dc.plan_conversion_transition(record(), "Completed", None, ARTIFACT, 5)
    assert (t.from_status, t.to_status, t.invoke_finalize) == ("InProgress", "Finalizing", True)
    assert t.set_fields == {"status": "InProgress", "progress": 80, "artifact_s3": ARTIFACT,
                            "updated_at": 5}
    assert t.conversion_fields == {"finalizing_at": 5}


def test_completed_without_artifact_fails():
    t = dc.plan_conversion_transition(record(), "Completed", None, None, 5)
    assert (t.to_status, t.invoke_finalize) == ("Failed", False)
    assert t.set_fields["failure_reason"] == "Conversion job completed without an artifact"


@pytest.mark.parametrize("sm,reason,expected", [
    ("Failed", FATAL, FATAL),
    ("Stopped", None, "Conversion job was stopped"),
    ("Failed", None, "Conversion job failed"),
    ("Stopped", "Stopped by user", "Stopped by user"),
])
def test_failed_or_stopped_fails_with_the_reason_verbatim(sm, reason, expected):
    t = dc.plan_conversion_transition(record(), sm, reason, None, 7)
    assert (t.from_status, t.to_status) == ("InProgress", "Failed")
    assert t.set_fields == {"status": "Failed", "progress": 0, "failure_reason": expected,
                            "updated_at": 7}
    assert t.invoke_finalize is False


@pytest.mark.parametrize("current", ["Finalizing", "Completed", "Failed", None, "Weird"])
@pytest.mark.parametrize("sm", ["InProgress", "Completed", "Failed", "Stopped"])
def test_only_in_progress_reacts_to_sagemaker(current, sm):
    rec = record(current)
    if current is None:
        rec.pop("conversion")
    assert dc.plan_conversion_transition(rec, sm, FATAL, ARTIFACT, 1) is None


def test_finalize_transitions():
    ok = dc.plan_finalize_transition(success=True, now_ms=9, packaged_components=[{"a": 1}],
                                     onnx_sha256="ab" * 32, onnx_summary={"opset": 17})
    assert (ok.from_status, ok.to_status) == ("Finalizing", "Completed")
    assert ok.set_fields["status"] == "Completed" and ok.set_fields["progress"] == 100
    assert ok.set_fields["packaged_components"] == [{"a": 1}]
    assert ok.conversion_fields == {"completed_at": 9, "onnx_sha256": "ab" * 32,
                                    "onnx_summary": {"opset": 17}}
    bad = dc.plan_finalize_transition(success=False, now_ms=9,
                                      failure_reason="Conversion output rejected (tar-member): x")
    assert (bad.from_status, bad.to_status) == ("Finalizing", "Failed")
    assert bad.set_fields["failure_reason"] == "Conversion output rejected (tar-member): x"
    assert bad.set_fields["progress"] == 0


def test_progress_and_status_per_conversion_status():
    assert dc.PROGRESS_FOR_STATUS == {"InProgress": 10, "Finalizing": 80, "Completed": 100,
                                      "Failed": 0}


def test_apply_transition_to_record_is_the_post_write_view():
    t = dc.plan_conversion_transition(record(), "Failed", FATAL, None, 3)
    after = dc.apply_transition_to_record(record(), t)
    assert after["status"] == "Failed" and after["failure_reason"] == FATAL
    assert after["conversion"]["status"] == "Failed" and after["conversion"]["failed_at"] == 3
    assert after["conversion"]["job_name"] == "j"
    assert record()["conversion"]["status"] == "InProgress"  # input untouched


# ---------------------------------------------------------------------------
# Fake table with real conditional semantics
# ---------------------------------------------------------------------------

class FakeTable:
    """Evaluates exactly `SET p = :v, ...` and `p = :v` where p is `#a` or
    `#a.#b`; anything else raises, so the reducer cannot drift into syntax
    this fake does not model."""

    _PATH = r"#\w+(?:\.#\w+)?"

    def __init__(self, item):
        self.items = {item["training_id"]: copy.deepcopy(item)}
        self.history = [item["conversion"]["status"]]

    def get_item(self, Key):
        return {"Item": copy.deepcopy(self.items[Key["training_id"]])}

    @staticmethod
    def _path(expr, names):
        return [names[p] for p in expr.split(".")]

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues):
        item = self.items[Key["training_id"]]
        m = re.fullmatch(rf"({self._PATH}) = (:\w+)", ConditionExpression)
        assert m, ConditionExpression
        node = item
        for part in self._path(m.group(1), ExpressionAttributeNames):
            node = node.get(part) if isinstance(node, dict) else None
        if node != ExpressionAttributeValues[m.group(2)]:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                         "Message": "The conditional request failed"}}, "UpdateItem")
        assert UpdateExpression.startswith("SET ")
        for clause in UpdateExpression[4:].split(", "):
            m = re.fullmatch(rf"({self._PATH}) = (:\w+)", clause)
            assert m, clause
            path = self._path(m.group(1), ExpressionAttributeNames)
            target = item
            for part in path[:-1]:
                target = target[part]  # a missing parent map is a DynamoDB error too
            target[path[-1]] = copy.deepcopy(ExpressionAttributeValues[m.group(2)])
        self.history.append(item["conversion"]["status"])
        return {}


class FakeLambda:
    def __init__(self):
        self.invokes = []

    def invoke(self, **kwargs):
        self.invokes.append(kwargs)
        return {"StatusCode": 202}


def test_conditional_write_loses_the_race_quietly():
    table = FakeTable(record())
    t = dc.plan_conversion_transition(record(), "Completed", None, ARTIFACT, 1)
    assert dc.apply_conversion_transition(table, "t-1", t) is True
    assert dc.apply_conversion_transition(table, "t-1", t) is False  # second writer: no-op
    assert table.history == ["InProgress", "Finalizing"]


def test_other_client_errors_propagate():
    class Broken:
        def update_item(self, **_):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")

    t = dc.plan_conversion_transition(record(), "Failed", FATAL, None, 1)
    with pytest.raises(ClientError):
        dc.apply_conversion_transition(Broken(), "t-1", t)


def test_reconcile_invokes_finalize_only_for_the_winner():
    table, lam = FakeTable(record()), FakeLambda()
    stale = table.get_item(Key={"training_id": "t-1"})["Item"]
    first = dc.reconcile_conversion(table, stale, "Completed", None, ARTIFACT, 1, lam, "pkg-fn")
    second = dc.reconcile_conversion(table, stale, "Completed", None, ARTIFACT, 2, lam, "pkg-fn")
    assert first is not None and second is None
    assert len(lam.invokes) == 1
    invoke = lam.invokes[0]
    assert invoke["FunctionName"] == "pkg-fn" and invoke["InvocationType"] == "Event"
    event = dc.build_finalize_event("t-1")
    assert invoke["Payload"] == __import__("json").dumps(event)
    assert event["pathParameters"] == {"id": "t-1"}
    assert __import__("json").loads(event["body"]) == {"finalize_conversion": True,
                                                       "auto_triggered": True}


def test_reconcile_without_a_packaging_function_still_claims():
    table, lam = FakeTable(record()), FakeLambda()
    t = dc.reconcile_conversion(table, record(), "Completed", None, ARTIFACT, 1, lam, None)
    assert t.to_status == "Finalizing" and lam.invokes == []


# ---------------------------------------------------------------------------
# Hypothesis: interleaved writers over stale reads
# ---------------------------------------------------------------------------

TERMINAL_SM = st.sampled_from([("Completed", None, ARTIFACT), ("Completed", None, None),
                               ("Failed", FATAL, None), ("Stopped", None, None),
                               ("Stopped", "Stopped by user", None)])
WRITERS = ("events", "sync_a", "sync_b", "finalize")
STEPS = st.lists(st.tuples(st.sampled_from(WRITERS), st.sampled_from(["read", "write"]),
                           st.booleans()), min_size=1, max_size=40)


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps=STEPS, end_at=st.integers(0, 40), terminal=TERMINAL_SM)
def test_interleaved_writers_keep_the_state_machine(steps, end_at, terminal):
    table, lam = FakeTable(record()), FakeLambda()
    snapshots = {}
    finalize_claims = 0

    def sm_at(i):
        return terminal if i >= end_at else ("InProgress", None, None)

    def step(i, writer, op, success):
        nonlocal finalize_claims
        if op == "read":
            snapshots[writer] = (table.get_item(Key={"training_id": "t-1"})["Item"], sm_at(i))
            return
        if writer not in snapshots:
            return
        snap, (sm, reason, artifact) = snapshots[writer]
        if writer == "finalize":
            # packaging only acts on a record it read as Finalizing
            if dc.conversion_status(snap) == "Finalizing":
                t = dc.plan_finalize_transition(success=success, now_ms=i,
                                                failure_reason="Conversion output rejected (x): y")
                dc.apply_conversion_transition(table, "t-1", t)
            return
        t = dc.reconcile_conversion(table, snap, sm, reason, artifact, i, lam, "pkg-fn")
        if t is not None and t.invoke_finalize:
            finalize_claims += 1

    for i, (writer, op, success) in enumerate(steps):
        step(i, writer, op, success)
    # A final sync-on-read GET after the job has certainly ended.
    last = max(len(steps), end_at)
    step(last, "sync_a", "read", True)
    step(last + 1, "sync_a", "write", True)

    history = table.history
    for before, after in zip(history, history[1:]):
        assert (before, after) in ALLOWED_EDGES, history
    for i, status in enumerate(history):
        if status in dc.TERMINAL_CONVERSION_STATUSES:
            assert all(s == status for s in history[i:]), history
    assert finalize_claims == len(lam.invokes) <= 1

    final = table.items["t-1"]
    conv = final["conversion"]["status"]
    sm, reason, artifact = terminal
    if sm == "Completed" and artifact:
        assert len(lam.invokes) == 1  # exactly one claim once the job has completed
        assert conv in ("Finalizing", "Completed", "Failed")
        assert final["artifact_s3"] == ARTIFACT
    else:
        assert lam.invokes == []
        assert conv == "Failed"
        expected = reason or ("Conversion job was stopped" if sm == "Stopped" else
                              "Conversion job completed without an artifact"
                              if sm == "Completed" else "Conversion job failed")
        assert final["failure_reason"] == expected
    top = {"InProgress": "InProgress", "Finalizing": "InProgress", "Completed": "Completed",
           "Failed": "Failed"}[conv]
    assert final["status"] == top
    assert final["progress"] == dc.PROGRESS_FOR_STATUS[conv]


# ---------------------------------------------------------------------------
# Real DynamoDB expression syntax (moto)
# ---------------------------------------------------------------------------

@pytest.fixture
def ddb_table(aws_stack):
    client = boto3.client("dynamodb", region_name=REGION)
    name = "test-training-jobs-dci-reducer"
    try:
        client.create_table(TableName=name,
                            KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "training_id",
                                                   "AttributeType": "S"}],
                            BillingMode="PAY_PER_REQUEST")
    except client.exceptions.ResourceInUseException:
        pass
    table = boto3.resource("dynamodb", region_name=REGION).Table(name)
    table.put_item(Item=record())
    return table


def test_moto_accepts_every_transition_expression(ddb_table):
    t = dc.plan_conversion_transition(record(), "Completed", None, ARTIFACT, 11)
    assert dc.apply_conversion_transition(ddb_table, "t-1", t) is True
    assert dc.apply_conversion_transition(ddb_table, "t-1", t) is False
    item = ddb_table.get_item(Key={"training_id": "t-1"})["Item"]
    assert item["conversion"]["status"] == "Finalizing"
    assert item["conversion"]["finalizing_at"] == 11 and item["conversion"]["job_name"] == "j"
    assert item["progress"] == 80 and item["artifact_s3"] == ARTIFACT

    done = dc.plan_finalize_transition(
        success=True, now_ms=12, packaged_components=[{"platform": "jetson-xavier-jp7"}],
        onnx_sha256="cd" * 32, onnx_summary={"parity_max_abs": {"box_max_abs": 0.0018}})
    assert dc.apply_conversion_transition(ddb_table, "t-1", done) is True
    item = ddb_table.get_item(Key={"training_id": "t-1"})["Item"]
    assert (item["status"], item["progress"], item["conversion"]["status"]) == ("Completed", 100,
                                                                                "Completed")
    assert item["conversion"]["onnx_summary"]["parity_max_abs"]["box_max_abs"] == \
        __import__("decimal").Decimal("0.0018")
    # Terminal: neither a late SageMaker failure nor a second finalize moves it.
    late = dc.plan_finalize_transition(success=False, now_ms=13, failure_reason="late")
    assert dc.apply_conversion_transition(ddb_table, "t-1", late) is False
    assert dc.plan_conversion_transition(item, "Failed", FATAL, None, 14) is None
    assert ddb_table.get_item(Key={"training_id": "t-1"})["Item"]["status"] == "Completed"
