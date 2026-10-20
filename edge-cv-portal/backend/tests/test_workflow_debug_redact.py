"""The Portal copy of the shared redaction module, ``workflow_debug.redact``
(workflow-debug-mode design §5.5; tasks.md 3.4). The same pattern cases the
device copy's test feeds, each alone, embedded in JSON, in a traceback and in
a URL; ``redact_structure(value, frozenset())`` as the ingest calls it; a
property that redaction is idempotent and never raises. Fixture secrets of
the §5.5 shapes only; none is a credential.
"""
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_debug import redact
from workflow_debug.redact import MASK, WITHHELD, redact_structure, redact_text

#: (text, substring that must vanish, substring that must remain)
CASES = [
    ("rtsp://operator:hunter2@camera.example.com/main", "hunter2", "rtsp://***@camera.example.com/main"),
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
    assert json.loads(out)
    traceback = ("Traceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n"
                 "    connect(%r)\nRuntimeError: refused: %s\n" % (text, text))
    out = redact_text(traceback)
    assert secret not in out, out
    assert "Traceback (most recent call last)" in out
    url = "https://example.com/path?a=1&detail=" + text.replace(" ", "")
    out = redact_text(url)
    assert secret.replace(" ", "") not in out, out
    assert out.startswith("https://example.com/path?a=1&")


@pytest.mark.parametrize("text, secret, expected", CASES, ids=[c[2][:24] for c in CASES])
def test_pattern_through_redact_structure_with_the_ingests_empty_secret_set(text, secret, expected):
    out = redact_structure({"trigger": {"payload": text}, "logs": [{"message": text}]}, frozenset())
    assert secret not in out["trigger"]["payload"]
    assert expected in out["trigger"]["payload"]
    assert secret not in out["logs"][0]["message"]


def test_json_header_form_keeps_the_quotes():
    text = json.dumps({"Authorization": "Bearer abcdef", "X-Api-Key": "k1k2k3k4", "ok": "v"})
    parsed = json.loads(redact_text(text))
    assert parsed == {"Authorization": MASK, "X-Api-Key": MASK, "ok": "v"}


def test_json_aws_values_keep_the_quotes():
    text = json.dumps({"AccessKeyId": "AKIAIOSFODNN7EXAMPLE",
                       "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG",
                       "SessionToken": "FwoGZXIvYXdzEBYaDHyvL", "Expiration": "2026"})
    parsed = json.loads(redact_text(text))
    assert parsed == {"AccessKeyId": MASK, "SecretAccessKey": MASK, "SessionToken": MASK,
                      "Expiration": "2026"}


def test_injected_secrets_of_four_or_more_characters():
    out = redact_text("see s3cretValue and abc and longer-one here", ["s3cretValue", "abc", "longer-one"])
    assert out == "see *** and abc and *** here"


def test_secret_free_text_is_unchanged():
    for text in ("hello world", "password=", "rtsp://camera.example.com/main", "key: value",
                 "the token was expired", "AKIA short", ""):
        assert redact_text(text) == text


def test_redact_structure_with_the_ingests_empty_secret_set():
    record = {
        "type": "run", "ts": 1792075062610,
        "trigger": {"payload": "password=hunter2&id=7"},
        "logs": [{"message": "Authorization: Bearer abc", "n": 3, "ok": True, "none": None, "f": 2.5}],
        "nested": ({"deep": ("password=abcd",)},),
        "raw": b"bytes",
        1: "key=value",
    }
    redacted = redact_structure(record, frozenset())
    assert redacted["trigger"]["payload"].startswith("password=***")
    assert "hunter2" not in redacted["trigger"]["payload"]
    assert redacted["logs"][0] == {"message": "Authorization: ***", "n": 3, "ok": True, "none": None, "f": 2.5}
    assert redacted["nested"] == [{"deep": ["password=***"]}]
    assert redacted["raw"] == WITHHELD
    assert redacted[1] == "key=***"
    assert redacted["ts"] == 1792075062610


def test_redact_structure_never_raises():
    class Odd:
        def __str__(self):
            raise RuntimeError("cannot stringify")

    assert redact_structure(Odd(), frozenset()) == WITHHELD
    assert redact_structure({"a": Odd()}, frozenset()) == {"a": WITHHELD}
    assert redact_structure("x", None) == "x"
    assert redact_structure("x", 42) == "x"


def test_non_strings_pass_through_redact_text():
    assert redact_text(None) is None
    assert redact_text(7) == 7
    assert redact_text("") == ""


def test_module_surface_and_the_layer_import():
    assert redact.MASK == "***" == MASK
    assert redact.WITHHELD == "[withheld]"
    assert set(redact.__all__) == {"redact_text", "redact_structure", "WITHHELD", "MASK"}
    # On the Portal the base rule comes from the workflow_core layer.
    import workflow_core.stream_url as stream_url
    assert redact._base_redact is stream_url.redact


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=60),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=12,
)


@settings(max_examples=100, deadline=None)
@given(json_values, st.lists(st.text(min_size=4, max_size=12), max_size=2))
def test_redaction_is_idempotent_and_never_raises(value, secrets):
    once = redact_structure(value, frozenset(secrets))
    twice = redact_structure(once, frozenset(secrets))
    assert once == twice
    json.dumps(once)
