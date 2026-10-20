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
"""The debug mode's redaction (workflow-debug-mode design §5.5; tasks.md 2.1).

Each pattern of §5.5 is masked alone, embedded in JSON, in a traceback and in
a URL; redaction is idempotent and never raises (a hypothesis property); the
vendored ``stream_url`` import path is taken when the Portal's
``workflow_core`` package is absent.

Validates: debug R7.4, R12.3.
"""
import json
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.telemetry.debug import redact as redact_module
from workflow_engine.telemetry.debug.redact import (
    MASK,
    WITHHELD,
    redact_structure,
    redact_text,
)

#: Fixture secrets of the §5.5 shapes. Fake values; none is a credential.
CASES = [
    # (text, substring that must vanish, substring that must remain)
    ("rtsp://operator:hunter2@camera.local/main", "hunter2", "rtsp://***@camera.local/main"),
    ("password=hunter2", "hunter2", "password=***"),
    ("pwd=hunter2&x=1", "hunter2", "pwd=***"),
    ("pass=hunter2", "hunter2", "pass=***"),
    ("key=abcdef12", "abcdef12", "key=***"),
    ("signature=abcdef12", "abcdef12", "signature=***"),
    ("X-Amz-Signature=0123456789abcdef", "0123456789abcdef", "X-Amz-Signature=***"),
    ("X-Amz-Credential=AKIAIOSFODNN7EXAMPLE/20260101/us-east-1/s3/aws4_request",
     "20260101/us-east-1", "X-Amz-Credential=***"),
    ("X-Amz-Security-Token=IQoJb3JpZ2luX2VjEJr", "IQoJb3JpZ2luX2VjEJr", "X-Amz-Security-Token=***"),
    ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi", "Authorization: ***"),
    ("Proxy-Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz", "Proxy-Authorization: ***"),
    ("X-Api-Key: 9f8e7d6c5b4a", "9f8e7d6c5b4a", "X-Api-Key: ***"),
    ("Cookie: session=abc123; theme=dark", "abc123", "Cookie: ***"),
    ("Set-Cookie: sid=abc123; Path=/", "abc123", "Set-Cookie: ***"),
    ("DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=Zm9vYmFy==;EndpointSuffix=core",
     "Zm9vYmFy==", "AccountKey=***;EndpointSuffix=core"),
    ("Endpoint=sb://ns/;SharedAccessKeyName=root;SharedAccessKey=Zm9vYmFyMTIz=",
     "Zm9vYmFyMTIz=", "SharedAccessKey=***"),
    ("Server=db;User Id=u;password=Sup3rS3cret;Database=d", "Sup3rS3cret", "password=***;Database=d"),
    ("Driver=x;pwd=Sup3rS3cret;db=d", "Sup3rS3cret", "pwd=***;db=d"),
    ("access key AKIAIOSFODNN7EXAMPLE in use", "AKIAIOSFODNN7EXAMPLE", "access key *** in use"),
    ("temporary ASIAJEXAMPLEXEG2JICE", "ASIAJEXAMPLEXEG2JICE", "temporary ***"),
    ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI",
     "aws_secret_access_key = ***"),
    ("SecretAccessKey=wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI", "SecretAccessKey=***"),
    ("SessionToken: FwoGZXIvYXdzEBYaDHyvL", "FwoGZXIvYXdzEBYaDHyvL", "SessionToken: ***"),
]


@pytest.mark.parametrize("text, secret, expected", CASES, ids=[c[2][:24] for c in CASES])
def test_pattern_alone(text, secret, expected):
    out = redact_text(text)
    assert secret not in out, out
    assert expected in out, out


@pytest.mark.parametrize("text, secret, expected", CASES, ids=[c[2][:24] for c in CASES])
def test_pattern_embedded_in_json_traceback_and_url(text, secret, expected):
    as_json = json.dumps({"detail": text, "nested": [text]})
    out = redact_text(as_json)
    assert secret not in out, out
    assert json.loads(out)  # still valid JSON

    traceback = ("Traceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n"
                 "    connect(%r)\nRuntimeError: refused: %s\n" % (text, text))
    out = redact_text(traceback)
    assert secret not in out, out
    assert "Traceback (most recent call last)" in out

    # In a URL the shape has no spaces (``aws_secret_access_key=...``).
    url = "https://example.com/path?a=1&detail=" + text.replace(" ", "")
    out = redact_text(url)
    assert secret.replace(" ", "") not in out, out
    assert out.startswith("https://example.com/path?a=1&")


def test_json_header_form_keeps_the_quotes():
    text = json.dumps({"Authorization": "Bearer abcdef", "X-Api-Key": "k1k2k3k4", "ok": "v"})
    out = redact_text(text)
    parsed = json.loads(out)
    assert parsed == {"Authorization": MASK, "X-Api-Key": MASK, "ok": "v"}


def test_json_aws_values_keep_the_quotes():
    text = json.dumps({"AccessKeyId": "AKIAIOSFODNN7EXAMPLE",
                       "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG",
                       "SessionToken": "FwoGZXIvYXdzEBYaDHyvL", "Expiration": "2026"})
    parsed = json.loads(redact_text(text))
    assert parsed == {"AccessKeyId": MASK, "SecretAccessKey": MASK, "SessionToken": MASK,
                      "Expiration": "2026"}


def test_injected_secrets_of_four_or_more_characters():
    secrets = ["s3cretValue", "abc", "longer-one"]
    out = redact_text("see s3cretValue and abc and longer-one here", secrets)
    assert out == "see *** and abc and *** here"


def test_secret_free_text_is_unchanged():
    for text in ("hello world", "password=", "rtsp://camera.local/main", "key: value",
                 "the token was expired", "AKIA short", ""):
        assert redact_text(text) == text


def test_redact_structure_walks_dicts_lists_and_strings():
    value = {
        "url": "rtsp://u:p4ssw0rd@h/x",
        "list": ["Authorization: Bearer tok", 3, None, True, 2.5],
        "nested": {"deep": ("password=abcd",)},
        "AKIAIOSFODNN7EXAMPLE": "key",
        "bytes": b"\x00\x01",
        "obj": object(),
    }
    out = redact_structure(value, frozenset({"p4ssw0rd"}))
    assert out["url"] == "rtsp://***@h/x"
    assert out["list"] == ["Authorization: ***", 3, None, True, 2.5]
    assert out["nested"] == {"deep": ["password=***"]}
    assert MASK in out  # the key was redacted too
    assert out["bytes"] == WITHHELD
    assert out["obj"].startswith("<object object at")


def test_redact_structure_never_raises():
    class Angry:
        def __str__(self):
            raise RuntimeError("no")

    assert redact_structure(Angry()) == WITHHELD
    assert redact_structure({"a": Angry()}) == {"a": WITHHELD}
    assert redact_structure("x", secrets=None) == "x"
    assert redact_structure("x", secrets=object()) == "x"  # type: ignore[arg-type]
    deep = current = []
    for _ in range(100):
        nxt = []
        current.append(nxt)
        current = nxt
    assert redact_structure(deep) is not None


_PIECES = st.sampled_from([
    "password=hunter2", "Authorization: Bearer x.y.z", "AKIAIOSFODNN7EXAMPLE",
    '"SecretAccessKey": "abc"', "rtsp://u:p@h/", "X-Amz-Signature=deadbeef",
    "AccountKey=Zm9v==;", "token=abcd", "plain words", "{", "}", '"', "'", ";", "&", "=", ":",
    "\n", "  ", "***", "key=", "pwd",
])


@settings(max_examples=60, deadline=None)
@given(st.lists(st.one_of(_PIECES, st.text(max_size=12)), max_size=12),
       st.lists(st.text(min_size=4, max_size=10), max_size=3))
def test_property_redaction_is_idempotent_and_never_raises(pieces, secrets):
    text = "".join(pieces)
    once = redact_text(text, secrets)
    twice = redact_text(once, secrets)
    assert once == twice
    assert isinstance(once, str)
    structure = {"m": text, "l": [text, 1, None], "n": {"t": text}}
    first = redact_structure(structure, secrets)
    assert redact_structure(first, secrets) == first


def test_vendored_import_path_is_taken_without_workflow_core():
    assert "workflow_core" not in sys.modules or sys.modules["workflow_core"] is None
    assert redact_module._base_redact.__module__ == "workflow_engine.vendor.workflow_core.stream_url"


def test_module_is_environment_neutral():
    import inspect

    source = inspect.getsource(redact_module)
    body = source.split('"""', 2)[2]
    assert "dda_logging" not in body
    assert "import logging" not in body
    assert "from workflow_core.stream_url import redact" in body
    assert "from workflow_engine.vendor.workflow_core.stream_url import redact" in body
