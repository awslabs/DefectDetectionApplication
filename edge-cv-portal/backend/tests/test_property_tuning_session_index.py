"""Property tests for the Portal's Tuning_Session sample index, Labels and
Synthetic_Negatives (spec: .kiro/specs/quality-prompt-tuning, task 6.4).

- **Feature: quality-prompt-tuning, Property 5: Indexing is faithful,
  additive, bounded and label-preserving** — **Validates: Requirements 3.1,
  3.2, 3.3, 3.4, 3.5, 3.6**
- **Feature: quality-prompt-tuning, Property 12: Synthetic negatives are
  exactly the OK × sibling-reference product and toggle cleanly** —
  **Validates: Requirements 4.5, 4.6, 4.7**
- **Feature: quality-prompt-tuning, Property 13: Labels are total,
  persisted and govern scoring membership** — **Validates: Requirements
  4.2, 4.3**

How these are driven
--------------------

Each example drives the REAL ``functions/workflow_tuning.py`` handler
against moto — the portal tables and artifacts bucket from the shared
``aws_stack`` fixture, the tuning single table and the Use_Case's inference
results bucket (the Sample_Store) created here — through the actual routes:
a fresh workflow and Tuning_Session per example (so every example owns its
own Sample_Store prefix and session partition), Tuning_Samples written into
the Sample_Store exactly as the device writes them, then
``POST .../refresh``, ``PUT .../samples/labels``,
``PUT .../synthetic-negatives`` and ``POST .../score-runs``.

Every expectation is an **independent restatement** of the requirements,
transcribed in this file and never imported from ``workflow_tuning`` — the
newest-N window, the skip reasons, the duplicate rule, the different-prompt
flag, the synthetic product and the scoring membership are all computed
here from the drawn data, so a mutation of the module fails these tests.

Two deliberate deviations, both recorded in the OUTCOME of task 6.4:

* Requirement 3.1's bound is **2000** samples. Driving 2000+ real samples
  through the store per example is not feasible, so the property patches
  ``SAMPLE_INDEX_BOUND`` to a small drawn value and asserts the windowing
  behaviour over it; :func:`test_documented_bounds` pins the module's real
  constant at 2000 literally, so a change of the number fails too.
* Property 13's "govern scoring membership" clause is asserted through the
  real admission path (``POST .../score-runs`` with a recording
  ``dispatch_action``), not by inspecting a private helper.

The enumerated cases over the same space are task 6.1's
``test_tuning_session_routes.py``; Properties 10/11 are
``test_property_tuning_score_runs.py``, 14/15/16
``test_property_tuning_apply_and_guards.py`` and 18
``test_property_tuning_preview.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from decimal import Decimal
from unittest import mock

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from dynamo_helpers import all_table_names

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
TUNING_TABLE_NAME = "test-workflow-tuning"
SAMPLE_BUCKET = f"dda-inference-results-{ACCOUNT_ID}"

# --------------------------------------------------------------------------
# Independent restatement of the contract under test.
#
# Transcribed from requirements.md / design.md, never imported from
# workflow_tuning — a mutation of the module must fail these tests.
# --------------------------------------------------------------------------

#: Requirement 2.3 / design "LocalServer component configuration": the
#: Sample_Store layout the device writes and the Portal indexes.
REF_SAMPLES_PREFIX = "workflow-tuning/samples/"
REF_SIDECAR_SUFFIX = ".json"
REF_INPUT_SUFFIX = ".input.jpg"
REF_REFERENCE_SUFFIX = ".reference.jpg"

#: Requirement 3.1: the newest-N bound of one index refresh.
REF_SAMPLE_INDEX_BOUND = 2000

#: Requirement 4.2: the three Labels, and "no Label" as a fourth state
#: (Requirement 4.3 treats an unlabelled sample as excluded).
REF_LABELS = ("OK", "NOK", "EXCLUDE")

#: Requirement 4.5 / design data model: a Synthetic_Negative's id.
REF_SYNTHETIC_MARKER = "|syn|"

#: Requirement 3.5's skip reasons, as the refresh summary counts them.
REF_SKIP_UNREADABLE = "unreadable"
REF_SKIP_MALFORMED = "malformed"
REF_SKIP_MISSING_INPUT = "missing_input"

#: The image byte marker: it appears in NO indexed attribute, so its
#: absence from a persisted item proves "no image bytes" (Requirement 3.2,
#: 9.5) without depending on how the item is shaped.
IMAGE_MARKER = b"JPEGDATA"

TARGET_NODE_ID = "bedrock_1"

BEDROCK_NODE = {
    "id": TARGET_NODE_ID,
    "type": "bedrock_inference",
    "position": {"x": 0, "y": 0},
    "parameters": {
        "model": "us.amazon.nova-lite-v1:0",
        "prompt": "Compare the plate to the reference.",
        "system_prompt": "You are a quality inspector.",
        "max_tokens": 256,
        "region": "us-west-2",
        "anomaly_mode": True,
    },
}


def native(value):
    """DynamoDB Decimals to native numbers, restated locally."""
    if isinstance(value, Decimal):
        return float(value) if value % 1 else int(value)
    if isinstance(value, dict):
        return {key: native(item) for key, item in value.items()}
    if isinstance(value, list):
        return [native(item) for item in value]
    return value


def image_bytes(content: str) -> bytes:
    """Input image bytes whose content — and therefore whose sha256 — is
    decided by ``content``, so equal contents are byte-identical images
    (Requirement 3.3)."""
    return b"\xff\xd8" + IMAGE_MARKER + b"-" + content.encode() + b"\xff\xd9"


# ==========================================================================
# Harness
# ==========================================================================

@pytest.fixture(scope="module")
def tuning(aws_stack):
    """The real handler module against moto, with the tuning table and the
    Use_Case's Sample_Store bucket in place."""
    dynamodb = boto3.client("dynamodb", region_name=REGION)
    if TUNING_TABLE_NAME not in all_table_names(dynamodb):
        dynamodb.create_table(
            TableName=TUNING_TABLE_NAME,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=SAMPLE_BUCKET)
    except Exception:
        pass
    os.environ["WORKFLOW_TUNING_TABLE"] = TUNING_TABLE_NAME
    os.environ["WORKFLOW_TUNING_FUNCTION_NAME"] = "test-workflow-tuning-fn"
    sys.modules.pop("workflow_tuning", None)
    import workflow_tuning

    return workflow_tuning


class World:
    """One Use_Case and its users; a fresh workflow + session per example."""

    def __init__(self, stack, module):
        self.stack = stack
        self.module = module
        self.s3 = boto3.client("s3", region_name=REGION)
        self.table = boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Tuning use case",
            "account_id": ACCOUNT_ID,
            "tuning_sample_export": True,
        })
        self.editor = self._user("DataScientist")

    @staticmethod
    def _user(role):
        user_id = f"user-{uuid.uuid4()}"
        return {"user_id": user_id, "email": f"{user_id}@example.com",
                "username": user_id, "role": role}

    # ------------------------------------------------------------- setup
    def fresh_workflow(self, nodes=None, version=1):
        """A workflow whose latest version holds the Tunable_Node."""
        workflow_id = str(uuid.uuid4())
        document = {"schemaVersion": "1.0",
                    "nodes": nodes or [BEDROCK_NODE], "connections": []}
        key = (f"workflows/{self.usecase_id}/{workflow_id}/versions/"
               f"{version}/workflow.json")
        self.s3.put_object(Bucket="test-portal-artifacts", Key=key,
                           Body=json.dumps(document).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id, "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID, "name": "Tuning workflow",
            "created_at": 1, "updated_at": version,
            "latest_version": version,
            "created_by": self.editor["user_id"]})
        self.stack.tables.versions.put_item(Item={
            "workflow_id": workflow_id, "version": version,
            "s3_definition_key": key, "created_at": 1,
            "created_by": self.editor["user_id"]})
        return workflow_id

    def deploy_to(self, workflow_id, devices):
        self.stack.tables.deployments.put_item(Item={
            "deployment_id": f"dep-{uuid.uuid4()}",
            "usecase_id": self.usecase_id, "created_at": 1,
            "deployment_status": "IN_PROGRESS", "component_type": "workflow",
            "workflow_id": workflow_id, "target_devices": list(devices)})

    # ---------------------------------------------------------- requests
    def call(self, method, resource, path_params=None, body=None, query=None,
             user=None):
        user = user or self.editor
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", str(value))
        event = {
            "httpMethod": method, "resource": resource, "path": path,
            "pathParameters": path_params or None,
            "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {
                "sub": user["user_id"], "email": user["email"],
                "cognito:username": user["username"],
                "custom:role": user["role"]}}},
        }
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"] or "{}")

    def open_session(self, workflow_id, node_id=TARGET_NODE_ID):
        status, body = self.call(
            "POST", "/workflow-tuning/anomaly/sessions",
            body={"workflow_id": workflow_id, "node_id": node_id})
        assert status in (200, 201), body
        return body["session"]

    def refresh(self, session_id):
        return self.call("POST",
                         "/workflow-tuning/anomaly/sessions/{id}/refresh",
                         {"id": session_id})

    def set_labels(self, session_id, sample_ids, label):
        return self.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/samples/labels",
            {"id": session_id},
            body={"sampleIds": list(sample_ids), "label": label})

    def toggle_synthetic(self, session_id, enabled):
        return self.call(
            "PUT",
            "/workflow-tuning/anomaly/sessions/{id}/synthetic-negatives",
            {"id": session_id}, body={"enabled": enabled})

    def create_candidate(self, session_id, **fields):
        payload = {"name": "Candidate A", "prompt": "Is the plate bad?",
                   "systemPrompt": "Answer as an inspector.",
                   "maxTokens": 256}
        payload.update(fields)
        status, body = self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/candidates",
            {"id": session_id}, body=payload)
        assert status == 201, body
        return body["candidate"]["candidateId"]

    def start_run(self, session_id, candidate_id, repeats=None, device=None):
        payload = {"candidateId": candidate_id}
        if repeats is not None:
            payload["repeats"] = repeats
        if device is not None:
            payload["deviceThingName"] = device
        return self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/score-runs",
            {"id": session_id}, body=payload)

    # ----------------------------------------------------- sample store
    def node_prefix(self, workflow_id, node_id=TARGET_NODE_ID):
        return f"{REF_SAMPLES_PREFIX}{workflow_id}/{node_id}/"

    def put_object(self, key, body):
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=key, Body=body)

    def seed_sample(self, workflow_id, thing, execution, content="A",
                    exported_at=1000, fingerprint=None, reference=True,
                    state="ok", node_id=TARGET_NODE_ID, version=1,
                    source="live"):
        """Write one Tuning_Sample exactly as the device writes it.

        ``state`` picks the Requirement 3.5 situation: ``ok`` (readable
        sidecar + input object), ``unreadable`` (sidecar that is not JSON),
        ``malformed`` (sidecar without an ``input`` block) or
        ``missing_input`` (sidecar naming an input object that is not in
        the store).
        """
        base = f"{self.node_prefix(workflow_id, node_id)}{thing}/{execution}"
        data = image_bytes(content)
        reference_data = image_bytes(content + "-ref")
        document = {
            "schemaVersion": 1, "source": source, "workflowId": workflow_id,
            "version": version, "executionId": execution, "nodeId": node_id,
            "nodeType": "bedrock_inference", "thingName": thing,
            "exportedAt": exported_at,
            "input": {"key": base + REF_INPUT_SUFFIX,
                      "sha256": hashlib.sha256(data).hexdigest(),
                      "bytes": len(data)},
            "recorded": {"isAnomalous": True, "confidence": 0.9,
                         "answer": '{"is_anomalous": true}',
                         "parseError": None},
            "promptFingerprint": fingerprint,
            "detectionId": None, "detectionSlot": None,
        }
        if reference:
            document["reference"] = {
                "key": base + REF_REFERENCE_SUFFIX,
                "sha256": hashlib.sha256(reference_data).hexdigest(),
                "bytes": len(reference_data)}
        if state == "malformed":
            document.pop("input")
        if state != "missing_input":
            self.put_object(base + REF_INPUT_SUFFIX, data)
        if reference:
            self.put_object(base + REF_REFERENCE_SUFFIX, reference_data)
        if state == "unreadable":
            self.put_object(base + REF_SIDECAR_SUFFIX, b"{not json at all")
        else:
            self.put_object(base + REF_SIDECAR_SUFFIX,
                            json.dumps(document).encode("utf-8"))
        return {"sampleId": f"{thing}/{execution}", "base": base,
                "document": document, "sha256": (document.get("input") or {}
                                                 ).get("sha256")}

    def seed_sibling_reference(self, workflow_id, node_id, thing, execution,
                               reference=True):
        """A sibling Inspection_Node's exported objects for the same
        (device, execution): a Reference_Image, or only an input."""
        base = f"{self.node_prefix(workflow_id, node_id)}{thing}/{execution}"
        self.put_object(base + REF_INPUT_SUFFIX, image_bytes("sib-in"))
        if reference:
            self.put_object(base + REF_REFERENCE_SUFFIX,
                            image_bytes("sib-ref"))
            return base + REF_REFERENCE_SUFFIX
        return None

    # ------------------------------------------------------------- reads
    def items(self, session_id, prefix="SAMPLE#"):
        condition = Key("pk").eq(f"SESSION#{session_id}")
        if prefix:
            condition = condition & Key("sk").begins_with(prefix)
        return [native(i) for i in self.table.query(
            KeyConditionExpression=condition).get("Items", [])]

    def samples_by_id(self, session_id):
        return {i["sampleId"]: i for i in self.items(session_id)}


@pytest.fixture(scope="module")
def world(aws_stack, tuning):
    return World(aws_stack, tuning)


# ==========================================================================
# The restated refresh contract (Requirement 3.1-3.6)
# ==========================================================================

def expected_refresh(existing, discovered, bound, baseline_fingerprint):
    """The refresh contract, restated from Requirements 3.1-3.6.

    ``existing`` maps the sample ids already indexed to
    ``{'exportedAt', 'sha256'}``; ``discovered`` is the list of store
    samples not yet indexed, each ``{'sampleId', 'exportedAt', 'sha256',
    'fingerprint', 'state'}``. Returns the indexed samples (with their
    expected ``duplicateOf``/``differentPrompt``), the skip counts by
    reason and the beyond-bound count.
    """
    skipped = {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    readable = []
    for sample in discovered:
        state = sample["state"]
        if state == "unreadable":
            skip(REF_SKIP_UNREADABLE)
        elif state == "malformed":
            skip(REF_SKIP_MALFORMED)
        elif state == "missing_input":
            skip(REF_SKIP_MISSING_INPUT)
        else:
            readable.append(sample)

    # Requirement 3.1: the newest ``bound`` by exportedAt over the union of
    # what is indexed and what was just discovered; the rest is reported.
    window = [(int(entry["exportedAt"]), sample_id)
              for sample_id, entry in existing.items()]
    window.extend((int(s["exportedAt"]), s["sampleId"]) for s in readable)
    window.sort(reverse=True)
    in_bound = {sample_id for _at, sample_id in window[:bound]}

    # Requirement 3.3: the earliest sample per input hash, over the index
    # and the newly indexed samples, processed oldest first.
    earliest = {}
    for sample_id, entry in existing.items():
        digest = entry.get("sha256")
        if not digest:
            continue
        candidate = (int(entry["exportedAt"]), sample_id)
        if digest not in earliest or candidate < earliest[digest]:
            earliest[digest] = candidate

    indexed = {}
    beyond = 0
    for sample in sorted(readable,
                         key=lambda s: (int(s["exportedAt"]), s["sampleId"])):
        if sample["sampleId"] not in in_bound:
            beyond += 1
            continue
        entry = (int(sample["exportedAt"]), sample["sampleId"])
        digest = sample.get("sha256")
        duplicate_of = None
        if digest:
            previous = earliest.get(digest)
            if previous is not None and previous < entry:
                duplicate_of = previous[1]
            elif previous is None or entry < previous:
                earliest[digest] = entry
        fingerprint = sample.get("fingerprint")
        indexed[sample["sampleId"]] = {
            "duplicateOf": duplicate_of,
            # Requirement 3.6: flagged iff it differs from the baseline's.
            "differentPrompt": bool(baseline_fingerprint and fingerprint
                                    and fingerprint != baseline_fingerprint),
        }
    return indexed, skipped, beyond


# ==========================================================================
# Property 5: indexing
# ==========================================================================

THINGS = ("dev-1", "dev-2")
STATES = ("ok", "ok", "unreadable", "malformed", "missing_input")
CONTENTS = ("A", "B")
FINGERPRINTS = ("baseline", "other", None)

sample_specs = st.lists(
    st.fixed_dictionaries({
        "thing": st.sampled_from(THINGS),
        "exportedAt": st.integers(min_value=1000, max_value=1003),
        "state": st.sampled_from(STATES),
        "content": st.sampled_from(CONTENTS),
        "fingerprint": st.sampled_from(FINGERPRINTS),
        "reference": st.booleans(),
    }), min_size=0, max_size=4)


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(batch_a=sample_specs, batch_b=sample_specs,
       bound=st.integers(min_value=1, max_value=6),
       labels=st.lists(st.sampled_from(REF_LABELS + (None,)),
                       min_size=0, max_size=4))
def test_property_indexing_is_faithful_additive_bounded_and_label_preserving(
        world, batch_a, batch_b, bound, labels):
    """**Feature: quality-prompt-tuning, Property 5: Indexing is faithful,
    additive, bounded and label-preserving** — **Validates: Requirements
    3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

    *For any* Sample_Store contents and any prior session index with
    Labels, refreshing indexes every readable sample not yet present
    (newest N by ``exportedAt``, reporting the remainder), copies sidecar
    fields verbatim without image bytes, marks equal-hash inputs as
    duplicates of the earliest, flags fingerprints differing from the
    baseline's, counts unreadable/missing samples by reason, and leaves
    every pre-existing sample and Label unchanged.

    Each example seeds a first batch, refreshes, Labels what was indexed,
    seeds a second batch and refreshes again — so the additive and
    label-preserving clauses are asserted against a real prior index, not
    a fabricated one.
    """
    workflow_id = world.fresh_workflow()
    session = world.open_session(workflow_id)
    session_id = session["sessionId"]
    baseline_fingerprint = session["baselineFingerprint"]
    assert baseline_fingerprint, "the session must snapshot a baseline"

    def seed(batch, tag):
        seeded = []
        for index, spec in enumerate(batch):
            execution = f"exec-{tag}{index}"
            written = world.seed_sample(
                workflow_id, spec["thing"], execution,
                content=spec["content"], exported_at=spec["exportedAt"],
                fingerprint=(baseline_fingerprint
                             if spec["fingerprint"] == "baseline"
                             else spec["fingerprint"]),
                reference=spec["reference"], state=spec["state"])
            seeded.append({
                "sampleId": written["sampleId"],
                "exportedAt": spec["exportedAt"],
                "sha256": written["sha256"],
                "fingerprint": (baseline_fingerprint
                                if spec["fingerprint"] == "baseline"
                                else spec["fingerprint"]),
                "state": spec["state"],
                "reference": spec["reference"],
                "document": written["document"],
                "base": written["base"],
            })
        return seeded

    with mock.patch.object(world.module, "SAMPLE_INDEX_BOUND", bound):
        # -- first refresh: an empty index --------------------------------
        seeded_a = seed(batch_a, "a")
        expected_a, skipped_a, beyond_a = expected_refresh(
            {}, seeded_a, bound, baseline_fingerprint)
        status, body = world.refresh(session_id)
        assert status == 200, body
        summary = body["refresh"]
        assert summary["indexed"] == len(expected_a)
        assert summary["skipped"] == skipped_a
        assert summary["beyondBound"] == beyond_a
        assert summary["discovered"] == len(seeded_a)
        indexed_now = world.samples_by_id(session_id)
        assert set(indexed_now) == set(expected_a)

        # -- Labels on what was indexed (Requirement 4.2) -----------------
        for sample_id, label in zip(sorted(indexed_now), labels):
            status, _body = world.set_labels(session_id, [sample_id], label)
            assert status == 200
        before = world.samples_by_id(session_id)

        # -- second refresh: additive over a labelled index ---------------
        seeded_b = seed(batch_b, "b")
        existing = {sample_id: {"exportedAt": item.get("exportedAt") or 0,
                                "sha256": item.get("inputSha256")}
                    for sample_id, item in before.items()}
        # A sample the first refresh skipped is discovered again.
        rediscovered = [s for s in seeded_a if s["sampleId"] not in before]
        expected_b, skipped_b, beyond_b = expected_refresh(
            existing, rediscovered + seeded_b, bound, baseline_fingerprint)
        status, body = world.refresh(session_id)
        assert status == 200, body
        summary = body["refresh"]
        assert summary["indexed"] == len(expected_b)
        assert summary["skipped"] == skipped_b
        assert summary["beyondBound"] == beyond_b
        assert summary["discovered"] == len(rediscovered) + len(seeded_b)
        # A pre-existing sample is never re-indexed (Requirement 3.4).
        assert "already_indexed" not in summary["skipped"]

    after = world.samples_by_id(session_id)

    # -- Requirement 3.4: every pre-existing sample and Label unchanged --
    assert set(after) == set(before) | set(expected_b)
    for sample_id, item in before.items():
        assert after[sample_id] == item, (
            f"pre-existing sample {sample_id} was rewritten")

    # -- the newly indexed samples, field by field -----------------------
    seeded_by_id = {s["sampleId"]: s
                    for s in rediscovered + seeded_b}
    for sample_id, expectation in expected_b.items():
        item = after[sample_id]
        source = seeded_by_id[sample_id]
        document = source["document"]
        # Requirement 3.2: the sidecar's fields, verbatim.
        assert item["sidecar"] == json.loads(json.dumps(document))
        assert item["inputKey"] == document["input"]["key"]
        assert item["inputSha256"] == document["input"]["sha256"]
        assert item["referenceKey"] == (
            (document.get("reference") or {}).get("key"))
        assert item["thingName"] == document["thingName"]
        assert item["executionId"] == document["executionId"]
        assert item["exportedAt"] == document["exportedAt"]
        assert item["version"] == document["version"]
        assert item["source"] == document["source"]
        assert item["promptFingerprint"] == document["promptFingerprint"]
        # Requirement 3.3 / 3.6.
        assert item["duplicateOf"] == expectation["duplicateOf"]
        assert item["differentPrompt"] == expectation["differentPrompt"]
        # A freshly indexed sample carries no Label of its own.
        assert item.get("label") is None
        assert item.get("synthetic") is False

    # -- Requirements 3.2 / 9.5: no image bytes anywhere in the index ----
    serialized = json.dumps(after, default=str).encode("utf-8")
    assert IMAGE_MARKER not in serialized
    for item in after.values():
        for value in item.values():
            assert not isinstance(value, (bytes, bytearray))


# ==========================================================================
# Property 12: synthetic negatives
# ==========================================================================

SIBLING_NODES = ("sib_a", "sib_b", TARGET_NODE_ID)

synthetic_specs = st.lists(
    st.fixed_dictionaries({
        "thing": st.sampled_from(THINGS),
        "label": st.sampled_from(REF_LABELS + (None,)),
        "siblings": st.lists(
            st.tuples(st.sampled_from(SIBLING_NODES), st.booleans()),
            min_size=0, max_size=3, unique_by=lambda pair: pair[0]),
    }), min_size=1, max_size=4)


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(specs=synthetic_specs, decoys=st.booleans())
def test_property_synthetic_negatives_are_the_ok_sibling_product(
        world, specs, decoys):
    """**Feature: quality-prompt-tuning, Property 12: Synthetic negatives
    are exactly the OK × sibling-reference product and toggle cleanly** —
    **Validates: Requirements 4.5, 4.6, 4.7**

    *For any* session whose samples carry Labels and whose executions carry
    sibling Inspection_Nodes with or without exported references on the
    same device, enabling creates exactly one NOK synthetic sample per
    (OK sample, sibling node with an exported reference for the same
    execution and device), each linked to its source; disabling removes
    every synthetic sample and leaves every indexed sample and Label
    unchanged.
    """
    workflow_id = world.fresh_workflow()
    session_id = world.open_session(workflow_id)["sessionId"]

    seeded = []
    for index, spec in enumerate(specs):
        execution = f"exec-{index}"
        written = world.seed_sample(workflow_id, spec["thing"], execution,
                                    content="A", exported_at=1000 + index)
        references = {}
        for sibling_node, has_reference in spec["siblings"]:
            key = world.seed_sibling_reference(
                workflow_id, sibling_node, spec["thing"], execution,
                reference=has_reference)
            if has_reference:
                references[sibling_node] = key
        if decoys:
            # A sibling reference of ANOTHER execution and ANOTHER device:
            # never a pair for this sample.
            world.seed_sibling_reference(workflow_id, "sib_a", spec["thing"],
                                        f"other-{index}")
            world.seed_sibling_reference(workflow_id, "sib_a", "dev-decoy",
                                         execution)
        seeded.append({"sampleId": written["sampleId"],
                       "label": spec["label"], "references": references,
                       "base": written["base"]})

    status, body = world.refresh(session_id)
    assert status == 200, body
    assert set(world.samples_by_id(session_id)) == {
        s["sampleId"] for s in seeded}

    for sample in seeded:
        if sample["label"] is not None:
            status, _body = world.set_labels(
                session_id, [sample["sampleId"]], sample["label"])
            assert status == 200
    before = world.samples_by_id(session_id)

    # Requirement 4.5, restated: one NOK synthetic per (OK sample, sibling
    # node OTHER than the tuned one that exported a reference for the same
    # execution on the same device).
    expected = {}
    for sample in seeded:
        if sample["label"] != "OK":
            continue
        for sibling_node, key in sample["references"].items():
            if sibling_node == TARGET_NODE_ID:
                continue
            expected[f"{sample['sampleId']}{REF_SYNTHETIC_MARKER}"
                     f"{sibling_node}"] = {
                "sourceSampleId": sample["sampleId"],
                "siblingNodeId": sibling_node,
                "referenceKey": key,
                "inputKey": before[sample["sampleId"]]["inputKey"]}

    status, body = world.toggle_synthetic(session_id, True)
    assert status == 200, body
    assert body["enabled"] is True
    assert body["created"] == len(expected)

    items = world.samples_by_id(session_id)
    synthetic = {sid: item for sid, item in items.items()
                 if item.get("synthetic")}
    assert set(synthetic) == set(expected)
    for sample_id, expectation in expected.items():
        item = synthetic[sample_id]
        assert item["label"] == "NOK"
        assert item["sourceSampleId"] == expectation["sourceSampleId"]
        assert item["siblingNodeId"] == expectation["siblingNodeId"]
        assert item["referenceKey"] == expectation["referenceKey"]
        assert item["inputKey"] == expectation["inputKey"]
    assert body["labelCounts"]["synthetic"] == len(expected)
    assert body["labelCounts"]["NOK"] == len(expected) + sum(
        1 for s in seeded if s["label"] == "NOK")

    # Enabling twice creates nothing more, and touches nothing.
    status, body = world.toggle_synthetic(session_id, True)
    assert status == 200 and body["created"] == 0
    assert world.samples_by_id(session_id) == items
    # Every indexed sample is untouched while the toggle is on.
    for sample_id, item in before.items():
        assert items[sample_id] == item

    # Requirement 4.6: disabling removes every synthetic and leaves every
    # indexed sample and Label exactly as it was.
    status, body = world.toggle_synthetic(session_id, False)
    assert status == 200, body
    assert body["enabled"] is False
    assert body["removed"] == len(expected)
    assert world.samples_by_id(session_id) == before
    status, view = world.call("GET", "/workflow-tuning/anomaly/sessions/{id}",
                             {"id": session_id})
    assert status == 200
    assert view["session"]["syntheticNegativesEnabled"] is False


# ==========================================================================
# Property 13: Labels
# ==========================================================================

label_operations = st.lists(
    st.fixed_dictionaries({
        "indices": st.lists(st.integers(min_value=0, max_value=4),
                            min_size=0, max_size=3),
        "label": st.sampled_from(REF_LABELS + (None,)),
        "bogus": st.booleans(),
    }), min_size=0, max_size=5)


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(count=st.integers(min_value=1, max_value=5),
       operations=label_operations,
       repeats=st.integers(min_value=1, max_value=3))
def test_property_labels_are_total_persisted_and_govern_scoring(
        world, count, operations, repeats):
    """**Feature: quality-prompt-tuning, Property 13: Labels are total,
    persisted and govern scoring membership** — **Validates: Requirements
    4.2, 4.3**

    *For any* sequence of label operations over a session, each sample's
    persisted Label equals the last operation applied to it; unlabelled and
    EXCLUDE samples are absent from any planned Score_Run and OK/NOK
    samples are all present.

    The membership clause is asserted through the real admission path
    (``POST .../score-runs``), whose planned units are what every execution
    step replays.
    """
    workflow_id = world.fresh_workflow()
    session_id = world.open_session(workflow_id)["sessionId"]
    seeded = [world.seed_sample(workflow_id, THINGS[index % len(THINGS)],
                                f"exec-{index}", content="A",
                                exported_at=1000 + index)
              for index in range(count)]
    status, body = world.refresh(session_id)
    assert status == 200, body
    sample_ids = [s["sampleId"] for s in seeded]
    assert set(world.samples_by_id(session_id)) == set(sample_ids)

    # The fold: each sample's Label is the last operation applied to it.
    expected = {sample_id: None for sample_id in sample_ids}
    for operation in operations:
        targets = [sample_ids[i] for i in operation["indices"]
                   if i < len(sample_ids)]
        requested = list(dict.fromkeys(targets))
        if operation["bogus"]:
            requested = requested + ["dev-1/does-not-exist"]
        if not requested:
            # Requirement 4.2 needs at least one sample to label.
            status, body = world.set_labels(session_id, requested,
                                            operation["label"])
            assert status == 400
            assert body["error"]["code"] == "MISSING_FIELDS"
            continue
        status, body = world.set_labels(session_id, requested,
                                        operation["label"])
        assert status == 200, body
        for sample_id in dict.fromkeys(targets):
            expected[sample_id] = operation["label"]
        assert set(body["updated"]) == set(dict.fromkeys(targets))
        if operation["bogus"]:
            assert body["missing"] == ["dev-1/does-not-exist"]

    persisted = world.samples_by_id(session_id)
    assert set(persisted) == set(sample_ids)
    for sample_id, label in expected.items():
        assert persisted[sample_id].get("label") == label
    # An unknown sample id never created an item.
    assert "dev-1/does-not-exist" not in persisted

    # Requirement 4.3: exactly the OK/NOK samples are scored.
    scored = sorted(sample_id for sample_id, label in expected.items()
                    if label in ("OK", "NOK"))
    candidate_id = world.create_candidate(session_id)
    dispatched = []
    with mock.patch.object(world.module, "dispatch_action",
                           lambda payload: dispatched.append(dict(payload))):
        status, body = world.start_run(session_id, candidate_id,
                                       repeats=repeats)
    if not scored:
        assert status == 400, body
        assert body["error"]["code"] == "NO_LABELLED_SAMPLES"
        assert dispatched == []
        assert not [i for i in world.items(session_id, "RUN#")]
        return
    assert status == 202, body
    assert body["samples"] == len(scored)
    assert body["repeats"] == repeats
    assert body["plannedInvocations"] == len(scored) * repeats
    runs = world.items(session_id, "RUN#")
    assert len(runs) == 1
    planned = runs[0]["plannedSamples"]
    assert sorted(entry["sampleId"] for entry in planned) == scored
    for entry in planned:
        assert entry["label"] == expected[entry["sampleId"]]
        assert entry["label"] in ("OK", "NOK")
    # And the units the scorer walks are exactly those samples × repeats.
    units = world.module.plan_units(runs[0])
    assert sorted(units) == sorted(
        (entry["sampleId"], entry["label"], repeat)
        for entry in planned for repeat in range(1, repeats + 1))


# ==========================================================================
# Supporting checks: the bounds and vocabulary the properties assert are
# the design's
# ==========================================================================

def test_documented_bounds(tuning):
    """The numbers and names Properties 5, 12 and 13 are stated over are
    the design's, literally.

    Property 5 patches the index bound so an example stays small; this
    pins the real constant, so raising or lowering it fails here.
    """
    assert tuning.SAMPLE_INDEX_BOUND == REF_SAMPLE_INDEX_BOUND == 2000
    assert tuning.LABELS == REF_LABELS == ("OK", "NOK", "EXCLUDE")
    assert tuning.SYNTHETIC_MARKER == REF_SYNTHETIC_MARKER == "|syn|"
    assert tuning.SIDECAR_SUFFIX == REF_SIDECAR_SUFFIX
    assert tuning.INPUT_SUFFIX == REF_INPUT_SUFFIX
    assert tuning.REFERENCE_SUFFIX == REF_REFERENCE_SUFFIX
    assert tuning.tuning_settings.SAMPLE_STORE_PREFIX == REF_SAMPLES_PREFIX


def test_unlabelled_and_excluded_samples_are_never_scored(world):
    """The membership clause of Property 13 at its two extremes: a session
    where nothing is labelled OK/NOK admits no run at all, and one where
    everything is admits every sample."""
    workflow_id = world.fresh_workflow()
    session_id = world.open_session(workflow_id)["sessionId"]
    seeded = [world.seed_sample(workflow_id, "dev-1", f"exec-{i}",
                               exported_at=1000 + i) for i in range(3)]
    assert world.refresh(session_id)[0] == 200
    ids = [s["sampleId"] for s in seeded]
    assert world.set_labels(session_id, ids, "EXCLUDE")[0] == 200
    candidate_id = world.create_candidate(session_id)
    with mock.patch.object(world.module, "dispatch_action", lambda p: None):
        status, body = world.start_run(session_id, candidate_id)
        assert status == 400 and body["error"]["code"] == "NO_LABELLED_SAMPLES"
        assert world.set_labels(session_id, ids, "OK")[0] == 200
        status, body = world.start_run(session_id, candidate_id, repeats=2)
    assert status == 202, body
    assert body["samples"] == 3 and body["plannedInvocations"] == 6
