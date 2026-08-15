"""Property test for manifest integration atomicity (synthetic-defect-
data-generation, task 4.10).

**Feature: synthetic-defect-data-generation, Property 11: Integration
atomicity**

_For any_ integration run and any injected failure at any step before or
at the manifest write (image upload failure, manifest read failure,
conditional write failure): the target Data_Manifest content remains
byte-identical to its pre-integration state and the failure is recorded
on the Generation_Session; only a fully successful run changes the
manifest.

**Validates: Requirements 7.7**

Runs the real POST /synthetic/sessions/{id}/integrate handler against
moto S3 / DynamoDB with a failure-injecting wrapper around the data S3
client: the wrapper raises on the Nth S3 call, covering every step up to
and including the conditional manifest write.
"""
import json
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_env import SyntheticEnv


class FailingS3(object):
    """Proxy around a real (moto) S3 client that raises on the Nth call
    to get_object / put_object / copy_object. fail_at=None never fails."""

    COUNTED = ("get_object", "put_object", "copy_object")

    def __init__(self, real, fail_at):
        self._real = real
        self._fail_at = fail_at
        self.calls = 0

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name not in self.COUNTED or not callable(attr):
            return attr

        def wrapper(*args, **kwargs):
            self.calls += 1
            if self._fail_at is not None and self.calls == self._fail_at:
                raise RuntimeError(
                    f"injected-s3-failure at call {self.calls} ({name})")
            return attr(*args, **kwargs)

        return wrapper


@pytest.fixture(scope="module")
def senv(aws_stack):
    return SyntheticEnv(aws_stack)


manifest_lines = st.lists(
    st.fixed_dictionaries({
        "source-ref": st.from_regex(r"s3://b/[a-z0-9]{1,8}\.png",
                                    fullmatch=True),
        "anomaly-label": st.integers(min_value=0, max_value=1),
        "anomaly-label-metadata": st.fixed_dictionaries(
            {"class-name": st.sampled_from(["scratch", "dent"])}),
    }),
    max_size=4,
)


@st.composite
def integration_cases(draw):
    approved_count = draw(st.integers(min_value=1, max_value=3))
    other_states = draw(st.lists(
        st.sampled_from(["pending", "rejected"]), max_size=2))
    manifest_exists = draw(st.booleans())
    existing_records = draw(manifest_lines) if manifest_exists else None
    # The integrate S3 call sequence per approved preview is
    # get(staging) + put(target) [+ get(source) when diffable], then
    # get(manifest) + put(manifest). Drawing beyond the maximum possible
    # call count exercises the fully-successful branch.
    fail_at = draw(st.one_of(
        st.none(),
        st.integers(min_value=1, max_value=3 * approved_count + 2),
    ))
    return approved_count, other_states, existing_records, fail_at


@settings(deadline=None)
@given(case=integration_cases())
def test_integration_atomicity(senv, case):
    """Any injected failure up to the manifest write leaves the manifest
    byte-identical and records the failure on the session; only a fully
    successful run changes the manifest (Requirement 7.7)."""
    approved_count, other_states, existing_records, fail_at = case
    sd = senv.synthetic_data

    usecase_id = senv.create_usecase()
    user = senv.actor_with_role(usecase_id, "DataScientist")
    run_id = uuid.uuid4().hex[:12]
    target_prefix = f"datasets/atomicity-{run_id}/"
    manifest_key = f"{target_prefix}manifests/train.manifest"
    session_id = senv.put_session_meta(
        usecase_id,
        status="awaiting_review",
        target_dataset_prefix=target_prefix,
        target_manifest_key=manifest_key,
    )

    # Approved previews with staged (non-PNG -> full-image bbox fallback)
    # objects, plus non-approved previews.
    for index in range(approved_count):
        preview_id = senv.put_preview(session_id,
                                      approval_state="approved",
                                      variation_index=index)
        senv.s3.put_object(
            Bucket=senv.bucket,
            Key=f"synthetic-staging/{session_id}/{preview_id}.png",
            Body=f"generated-{index}".encode())
    for state in other_states:
        senv.put_preview(session_id, approval_state=state)

    # Pre-integration manifest state (present with known content, or
    # absent entirely).
    if existing_records is not None:
        pre_content = "".join(json.dumps(r) + "\n"
                              for r in existing_records)
        senv.s3.put_object(Bucket=senv.bucket, Key=manifest_key,
                           Body=pre_content.encode())
    else:
        pre_content = None

    # Wrap the data S3 client with the failure injector.
    wrapper_holder = {}
    original = sd._data_s3_client

    def wrapped(usecase):
        client, bucket = original(usecase)
        wrapper = FailingS3(client, fail_at)
        wrapper_holder["wrapper"] = wrapper
        return wrapper, bucket

    sd._data_s3_client = wrapped
    try:
        status, body = senv.invoke(
            "POST", "/synthetic/sessions/{id}/integrate", user,
            session_id=session_id)
    finally:
        sd._data_s3_client = original

    failure_injected = (
        fail_at is not None
        and wrapper_holder["wrapper"].calls >= fail_at)

    def read_manifest():
        try:
            obj = senv.s3.get_object(Bucket=senv.bucket, Key=manifest_key)
            return obj["Body"].read().decode()
        except senv.s3.exceptions.NoSuchKey:
            return None

    meta = senv.sessions_table.get_item(
        Key={"session_id": session_id, "sk": "META"})["Item"]

    if failure_injected:
        # 502 with the reason; manifest byte-identical to its
        # pre-integration state; failure recorded on the session.
        assert status == 502, body
        assert "injected-s3-failure" in body["error"]
        assert read_manifest() == pre_content, (
            "manifest must stay byte-identical after an injected failure")
        assert "last_failure" in meta
        assert "injected-s3-failure" in meta["last_failure"]["reason"]
        assert meta.get("status") != "integrated"
    else:
        # Fully successful run: manifest = pre-state + one record per
        # approved image, session integrated.
        assert status == 200, body
        content = read_manifest()
        expected_prefix = pre_content or ""
        assert content.startswith(expected_prefix)
        appended = [json.loads(line) for line in
                    content[len(expected_prefix):].splitlines() if line]
        assert len(appended) == approved_count
        assert body["appended_count"] == approved_count
        assert meta["status"] == "integrated"
