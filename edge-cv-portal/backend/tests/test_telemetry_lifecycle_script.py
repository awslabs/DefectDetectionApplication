"""``edge-cv-portal/scripts/telemetry_lifecycle.py`` (workflow-debug-mode
tasks.md 3.3): the dry run makes no ``PutBucketLifecycleConfiguration`` call,
``--apply`` makes exactly one with the merged rules, a missing configuration
is an empty rule list, and ``--debug-days`` is bounded to 1-30.
"""
import importlib.util
import io
import json
import os

import pytest
from botocore.exceptions import ClientError

from telemetry import retention

SCRIPT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts", "telemetry_lifecycle.py"))
BUCKET = "dda-inference-results-222222222222"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("telemetry_lifecycle_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubS3:
    def __init__(self, rules=None, missing=False):
        self.rules = rules
        self.missing = missing
        self.get_calls = []
        self.put_calls = []

    def get_bucket_lifecycle_configuration(self, **kwargs):
        self.get_calls.append(kwargs)
        if self.missing:
            raise ClientError({"Error": {"Code": "NoSuchLifecycleConfiguration", "Message": "none"}},
                              "GetBucketLifecycleConfiguration")
        return {"Rules": list(self.rules or [])}

    def put_bucket_lifecycle_configuration(self, **kwargs):
        self.put_calls.append(kwargs)
        return {}


FOREIGN = {"ID": "operator-archive", "Filter": {"Prefix": "archive/"}, "Status": "Enabled",
           "Expiration": {"Days": 365}}


class TestDryRun:
    def test_dry_run_reads_and_prints_but_never_writes(self, script):
        s3 = _StubS3(rules=[FOREIGN])
        out = io.StringIO()
        assert script.main(["--bucket", BUCKET], s3_client=s3, out=out) == 0
        assert s3.get_calls == [{"Bucket": BUCKET}]
        assert s3.put_calls == []
        text = out.getvalue()
        assert "Dry run" in text
        assert "DDATelemetryDebugImages" in text and "operator-archive" in text
        assert "change needed: yes" in text

    def test_missing_configuration_is_an_empty_list(self, script):
        s3 = _StubS3(missing=True)
        assert script.read_rules(s3, BUCKET) == []
        out = io.StringIO()
        assert script.main(["--bucket", BUCKET], s3_client=s3, out=out) == 0
        assert "Existing rules: 0; merged rules: 3" in out.getvalue()
        assert s3.put_calls == []

    def test_other_read_errors_propagate(self, script):
        class Failing(_StubS3):
            def get_bucket_lifecycle_configuration(self, **kwargs):
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}},
                                  "GetBucketLifecycleConfiguration")

        with pytest.raises(ClientError):
            script.read_rules(Failing(), BUCKET)


class TestApply:
    def test_apply_writes_exactly_once_with_the_merged_rules(self, script):
        s3 = _StubS3(rules=[FOREIGN])
        out = io.StringIO()
        assert script.main(["--bucket", BUCKET, "--clip-days", "45", "--debug-days", "3", "--apply"],
                           s3_client=s3, out=out) == 0
        assert len(s3.put_calls) == 1
        call = s3.put_calls[0]
        assert call["Bucket"] == BUCKET
        expected, changed = retention.merge_telemetry_rules([FOREIGN], clip_days=45, debug_days=3)
        assert changed is True
        assert call["LifecycleConfiguration"] == {"Rules": expected}
        assert call["LifecycleConfiguration"]["Rules"][0] == FOREIGN
        assert [r["ID"] for r in call["LifecycleConfiguration"]["Rules"]] == [
            "operator-archive", "DDATelemetryClips", "DDATelemetryLive", "DDATelemetryDebugImages"]
        assert call["LifecycleConfiguration"]["Rules"][1]["Expiration"] == {"Days": 45}
        assert call["LifecycleConfiguration"]["Rules"][3]["Expiration"] == {"Days": 3}
        assert "Applied 4 rules" in out.getvalue()

    def test_apply_on_a_bucket_already_in_force_writes_nothing(self, script):
        s3 = _StubS3(rules=[FOREIGN] + retention.telemetry_rules(clip_days=90, debug_days=7))
        out = io.StringIO()
        assert script.main(["--bucket", BUCKET, "--apply"], s3_client=s3, out=out) == 0
        assert s3.put_calls == []
        assert "already carries" in out.getvalue()

    def test_the_printed_rules_are_the_merged_rules(self, script):
        s3 = _StubS3(rules=[])
        out = io.StringIO()
        script.main(["--bucket", BUCKET], s3_client=s3, out=out)
        text = out.getvalue()
        start, end = text.index("["), text.rindex("]") + 1
        assert json.loads(text[start:end]) == retention.telemetry_rules()


class TestArguments:
    @pytest.mark.parametrize("value", ["0", "31", "-2", "x"])
    def test_debug_days_out_of_range_is_a_usage_error(self, script, value):
        with pytest.raises(SystemExit) as excinfo:
            script.parse_args(["--bucket", BUCKET, "--debug-days", value])
        assert excinfo.value.code == 2

    @pytest.mark.parametrize("value", ["1", "30"])
    def test_debug_days_bounds_accepted(self, script, value):
        assert script.parse_args(["--bucket", BUCKET, "--debug-days", value]).debug_days == int(value)

    def test_clip_days_must_be_positive(self, script):
        with pytest.raises(SystemExit):
            script.parse_args(["--bucket", BUCKET, "--clip-days", "0"])

    def test_bucket_is_required_and_apply_defaults_off(self, script):
        with pytest.raises(SystemExit):
            script.parse_args([])
        args = script.parse_args(["--bucket", BUCKET])
        assert args.apply is False
        assert args.clip_days == 90 and args.debug_days == 7
