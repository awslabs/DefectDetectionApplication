#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""The MQTT topic convention of the device telemetry and the debug mode
(workflow-telemetry-dashboards design §3; workflow-debug-mode design §3).

    dda/telemetry/{usecase_id}/{workflow_id}/{kind}    kind in summary, event, live
    dda/debug/{usecase_id}/{workflow_id}/{kind}        kind in record

Two literal root levels, the Portal's use case and workflow ids, one kind:
five levels, four slashes, no device level (the device comes from the
publishing certificate; the payload's ``deviceId`` is a cross-check). The
ingest rules match ``dda/telemetry/+/+/+`` and ``dda/debug/+/+/+``; a sixth
level or another root is not a convention topic.

This file exists twice and must stay byte-identical: the device copy at
``src/backend/workflow_engine/telemetry/topics.py`` and the Portal copy at
``edge-cv-portal/backend/functions/edge_telemetry_common/topics.py``
(``test_telemetry_topics_two_copies.py`` compares them). It is therefore
environment-neutral: standard library only, no logging, no engine or layer
import.
"""

from collections import namedtuple
from typing import Dict, Tuple

__all__ = [
    "TELEMETRY_ROOT",
    "DEBUG_ROOT",
    "KINDS_BY_ROOT",
    "TopicError",
    "ParsedTopic",
    "convention_topic",
    "parse_convention_topic",
]

TELEMETRY_ROOT = "dda/telemetry"
DEBUG_ROOT = "dda/debug"

#: The ``kind`` level each root admits.
KINDS_BY_ROOT: Dict[str, Tuple[str, ...]] = {
    TELEMETRY_ROOT: ("summary", "event", "live"),
    DEBUG_ROOT: ("record",),
}

#: Characters an id level may not carry: the MQTT separator and wildcards,
#: and whitespace. Ids are the Portal's UUIDs in practice; the helper only
#: refuses what would change the topic's level structure or match a filter
#: it should not.
_FORBIDDEN_ID_CHARACTERS = frozenset("/+#\x00 \t\r\n")

#: The longest topic the helper builds: AWS IoT Core limits a topic to 256
#: bytes; the convention stays far below it with UUID ids (about 95 bytes).
MAX_TOPIC_BYTES = 256

ParsedTopic = namedtuple("ParsedTopic", ["root", "usecase_id", "workflow_id", "kind"])


class TopicError(ValueError):
    """A topic that does not follow the convention; ``reason`` is one of
    ``not_a_string``, ``empty``, ``unknown_root``, ``wrong_level_count``,
    ``empty_level``, ``invalid_usecase_id``, ``invalid_workflow_id``,
    ``unknown_kind``, ``too_long``."""

    def __init__(self, reason: str, topic: str = "") -> None:
        super().__init__(
            "{0}: {1!r}".format(reason, topic) if topic else reason
        )
        self.reason = reason
        self.topic = topic


def _check_id(value: str, reason: str, topic: str) -> str:
    if not isinstance(value, str) or not value:
        raise TopicError(reason, topic)
    if any(character in _FORBIDDEN_ID_CHARACTERS for character in value):
        raise TopicError(reason, topic)
    return value


def convention_topic(root: str, usecase_id: str, workflow_id: str, kind: str) -> str:
    """``{root}/{usecase_id}/{workflow_id}/{kind}`` for a known root and a
    kind that root admits; raises :class:`TopicError` otherwise."""
    if root not in KINDS_BY_ROOT:
        raise TopicError("unknown_root", str(root))
    if kind not in KINDS_BY_ROOT[root]:
        raise TopicError("unknown_kind", "{0}/{1}".format(root, kind))
    _check_id(usecase_id, "invalid_usecase_id", str(usecase_id))
    _check_id(workflow_id, "invalid_workflow_id", str(workflow_id))
    topic = "/".join((root, usecase_id, workflow_id, kind))
    if len(topic.encode("utf-8")) > MAX_TOPIC_BYTES:
        raise TopicError("too_long", topic)
    return topic


def parse_convention_topic(topic: str) -> ParsedTopic:
    """The ``(root, usecase_id, workflow_id, kind)`` of a convention topic;
    raises :class:`TopicError(reason)` for anything else (a sixth level, an
    unknown root, an unknown kind, an empty or wildcard-bearing level)."""
    if not isinstance(topic, str):
        raise TopicError("not_a_string")
    if not topic:
        raise TopicError("empty", topic)
    if len(topic.encode("utf-8")) > MAX_TOPIC_BYTES:
        raise TopicError("too_long", topic)
    levels = topic.split("/")
    if len(levels) != 5:
        if len(levels) >= 2 and "/".join(levels[:2]) not in KINDS_BY_ROOT:
            raise TopicError("unknown_root", topic)
        raise TopicError("wrong_level_count", topic)
    root = "/".join(levels[:2])
    if root not in KINDS_BY_ROOT:
        raise TopicError("unknown_root", topic)
    usecase_id, workflow_id, kind = levels[2], levels[3], levels[4]
    if not usecase_id or not workflow_id or not kind:
        raise TopicError("empty_level", topic)
    _check_id(usecase_id, "invalid_usecase_id", topic)
    _check_id(workflow_id, "invalid_workflow_id", topic)
    if kind not in KINDS_BY_ROOT[root]:
        raise TopicError("unknown_kind", topic)
    return ParsedTopic(root, usecase_id, workflow_id, kind)
