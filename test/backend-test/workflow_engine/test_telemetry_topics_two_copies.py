# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The topic helper exists twice and must stay byte-identical
(workflow-telemetry-dashboards design §3; workflow-debug-mode tasks.md 1.5,
decision P2): the device copy ``src/backend/workflow_engine/telemetry/topics.py``
and the Portal copy ``edge-cv-portal/backend/functions/edge_telemetry_common/topics.py``.

Beside ``test_vendored_catalog_mirror.py`` (the existing Portal-versus-vendored
comparison). Skipped with the reason "Portal copy not present" until the Portal
package lands (debug tasks.md 3.2); the behaviour of the device copy is tested
here regardless.
"""
import hashlib
from pathlib import Path

import pytest

from workflow_engine.telemetry import topics
from workflow_engine.telemetry.topics import (
    DEBUG_ROOT,
    TELEMETRY_ROOT,
    TopicError,
    convention_topic,
    parse_convention_topic,
)

DEVICE_RELATIVE = Path("src/backend/workflow_engine/telemetry/topics.py")
PORTAL_RELATIVE = Path("edge-cv-portal/backend/functions/edge_telemetry_common/topics.py")


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / DEVICE_RELATIVE).is_file() and (candidate / "edge-cv-portal").is_dir():
            return candidate
    raise AssertionError("Could not locate the repository root containing " + str(DEVICE_RELATIVE))


def test_device_and_portal_topics_copies_are_byte_identical():
    root = _repo_root()
    portal = root / PORTAL_RELATIVE
    if not portal.is_file():
        pytest.skip("Portal copy not present")
    device_bytes = (root / DEVICE_RELATIVE).read_bytes()
    portal_bytes = portal.read_bytes()
    assert device_bytes == portal_bytes, (
        "topics.py copies differ:\n  device sha256=%s (%s)\n  portal sha256=%s (%s)\n"
        "Copy one over the other: cp %s %s" % (
            hashlib.sha256(device_bytes).hexdigest(), DEVICE_RELATIVE,
            hashlib.sha256(portal_bytes).hexdigest(), PORTAL_RELATIVE,
            DEVICE_RELATIVE, PORTAL_RELATIVE,
        )
    )


UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"


class TestConventionTopic:
    def test_telemetry_and_debug_roots(self):
        assert convention_topic(TELEMETRY_ROOT, UC, WF, "summary") == "dda/telemetry/%s/%s/summary" % (UC, WF)
        assert convention_topic(TELEMETRY_ROOT, UC, WF, "event").endswith("/event")
        assert convention_topic(TELEMETRY_ROOT, UC, WF, "live").endswith("/live")
        assert convention_topic(DEBUG_ROOT, UC, WF, "record") == "dda/debug/%s/%s/record" % (UC, WF)

    def test_round_trip(self):
        topic = convention_topic(DEBUG_ROOT, UC, WF, "record")
        assert parse_convention_topic(topic) == (DEBUG_ROOT, UC, WF, "record")
        parsed = parse_convention_topic(convention_topic(TELEMETRY_ROOT, UC, WF, "live"))
        assert parsed.root == TELEMETRY_ROOT
        assert parsed.usecase_id == UC
        assert parsed.workflow_id == WF
        assert parsed.kind == "live"

    @pytest.mark.parametrize("root, kind, reason", [
        ("dda", "summary", "unknown_root"),
        ("dda/other", "summary", "unknown_root"),
        (TELEMETRY_ROOT, "record", "unknown_kind"),
        (DEBUG_ROOT, "summary", "unknown_kind"),
    ])
    def test_unknown_root_or_kind(self, root, kind, reason):
        with pytest.raises(TopicError) as excinfo:
            convention_topic(root, UC, WF, kind)
        assert excinfo.value.reason == reason

    @pytest.mark.parametrize("bad", ["", "a/b", "a+b", "a#b", "a b", None, 7])
    def test_invalid_ids(self, bad):
        with pytest.raises(TopicError) as uc:
            convention_topic(DEBUG_ROOT, bad, WF, "record")
        assert uc.value.reason == "invalid_usecase_id"
        with pytest.raises(TopicError) as wf:
            convention_topic(DEBUG_ROOT, UC, bad, "record")
        assert wf.value.reason == "invalid_workflow_id"

    def test_too_long(self):
        with pytest.raises(TopicError) as excinfo:
            convention_topic(DEBUG_ROOT, "u" * 200, "w" * 200, "record")
        assert excinfo.value.reason == "too_long"


class TestParseConventionTopic:
    @pytest.mark.parametrize("topic, reason", [
        ("", "empty"),
        (None, "not_a_string"),
        ("dda/telemetry/%s/%s" % (UC, WF), "wrong_level_count"),
        ("dda/telemetry/%s/%s/summary/extra" % (UC, WF), "wrong_level_count"),
        ("dda/results", "unknown_root"),
        ("dda/trigger/start", "unknown_root"),
        ("ppe-demo/mic730/invoke/a/b", "unknown_root"),
        ("other/telemetry/%s/%s/summary" % (UC, WF), "unknown_root"),
        ("dda/telemetry//%s/summary" % WF, "empty_level"),
        ("dda/telemetry/%s//summary" % UC, "empty_level"),
        ("dda/telemetry/%s/%s/" % (UC, WF), "empty_level"),
        ("dda/telemetry/%s/%s/record" % (UC, WF), "unknown_kind"),
        ("dda/debug/%s/%s/summary" % (UC, WF), "unknown_kind"),
        ("dda/debug/%s/%s/+" % (UC, WF), "unknown_kind"),
        ("dda/debug/+/%s/record" % WF, "invalid_usecase_id"),
        ("dda/debug/%s/#/record" % UC, "invalid_workflow_id"),
        ("dda/debug/%s/a b/record" % UC, "invalid_workflow_id"),
    ])
    def test_rejections(self, topic, reason):
        with pytest.raises(TopicError) as excinfo:
            parse_convention_topic(topic)
        assert excinfo.value.reason == reason
        assert isinstance(excinfo.value, ValueError)

    def test_too_long(self):
        with pytest.raises(TopicError) as excinfo:
            parse_convention_topic("dda/debug/%s/%s/record" % ("u" * 200, "w" * 200))
        assert excinfo.value.reason == "too_long"

    def test_error_carries_reason_and_topic(self):
        with pytest.raises(TopicError) as excinfo:
            parse_convention_topic("dda/results")
        assert excinfo.value.reason == "unknown_root"
        assert excinfo.value.topic == "dda/results"
        assert "unknown_root" in str(excinfo.value)


def test_module_is_environment_neutral():
    source = (_repo_root() / DEVICE_RELATIVE).read_text(encoding="utf-8")
    assert "import logging" not in source
    assert "workflow_engine" not in source.split('"""', 2)[2]  # only in the docstring
    assert set(topics.__all__) == {
        "TELEMETRY_ROOT", "DEBUG_ROOT", "KINDS_BY_ROOT", "TopicError", "ParsedTopic",
        "convention_topic", "parse_convention_topic",
    }
