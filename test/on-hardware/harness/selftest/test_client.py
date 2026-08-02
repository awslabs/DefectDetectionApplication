"""Unit tests for harnesslib.client (Reqs 3.3, 4.2, 5.1, 5.3, 8.2).

All transport is mocked (a FakeSession standing in for requests.Session):
bearer token attach after login, token never in error reprs, body excerpt
bounding, poll-loop terminal states, and streaming event decoding.
"""

import json

import pytest
from harnesslib.client import (
    BODY_EXCERPT_LIMIT,
    REDACTED,
    DeviceApiError,
    EdgeApiClient,
    ModelWaitError,
    redact_headers,
)
from harnesslib.config import Timeouts
from harnesslib.sse import SseStreamError

SECRET_TOKEN = "sekrit-token-value"


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None, lines=None):
        self.status_code = status_code
        self._body = body
        self._text = text
        self._lines = lines or []

    @property
    def text(self):
        if self._text is not None:
            return self._text
        return json.dumps(self._body) if self._body is not None else ""

    def json(self):
        return self._body

    def iter_lines(self):
        return iter(self._lines)


class FakeSession:
    """Mocked transport: answers queued responses and records every call."""

    def __init__(self, responses=None):
        self.headers = {}
        self.responses = list(responses or [])
        self.calls = []

    def queue(self, response):
        self.responses.append(response)
        return self

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"Unexpected request: {method} {url}")
        return self.responses.pop(0)


def make_client(session, **kwargs):
    kwargs.setdefault("sleep", lambda seconds: None)
    return EdgeApiClient("http://device:5000", session=session, **kwargs)


def feature_entry(name, status, reason=None):
    entry = {"type": "VllmModel", "modelName": name, "status": status}
    if reason is not None:
        entry["defaultConfiguration"] = {"failureReason": reason}
    return entry


class TestTransport:
    def test_base_url_trailing_slash_normalized(self):
        session = FakeSession([FakeResponse(body={})])
        client = EdgeApiClient("http://device:5000/", session=session)
        client.system_health()
        assert session.calls[0]["url"] == "http://device:5000/system-health"

    def test_every_call_carries_a_timeout(self):
        session = FakeSession([FakeResponse(body={})])
        make_client(session).system_health()
        assert session.calls[0]["timeout"] is not None

    def test_generate_uses_generate_stage_timeout(self):
        session = FakeSession([FakeResponse(body={"generated_text": "hi"})])
        client = make_client(session, timeouts=Timeouts(generate_s=42.0))
        client.generate("m", "prompt")
        assert session.calls[0]["timeout"] == 42.0

    def test_non_2xx_raises_device_api_error(self):
        session = FakeSession([FakeResponse(status_code=502, text="bad gateway")])
        with pytest.raises(DeviceApiError) as excinfo:
            make_client(session).system_health()
        err = excinfo.value
        assert err.method == "GET"
        assert err.path == "/system-health"
        assert err.status == 502
        assert err.body_excerpt == "bad gateway"


class TestAuth:
    def login_response(self):
        return FakeResponse(
            body={
                "token": SECRET_TOKEN,
                "expiresAt": 1234,
                "role": "admin",
                "username": "op",
            }
        )

    def test_bearer_token_attached_after_login(self):
        session = FakeSession([self.login_response(), FakeResponse(body={})])
        client = make_client(session)
        client.login("op", "pw")
        assert session.headers["Authorization"] == f"Bearer {SECRET_TOKEN}"
        client.system_health()  # subsequent calls ride the same session

    def test_login_returns_metadata_without_token(self):
        session = FakeSession([self.login_response()])
        result = make_client(session).login("op", "pw")
        assert result == {"expiresAt": 1234, "role": "admin", "username": "op"}
        assert SECRET_TOKEN not in repr(result)

    def test_login_without_token_in_response_fails(self):
        session = FakeSession([FakeResponse(body={"role": "admin"})])
        with pytest.raises(DeviceApiError, match="no token"):
            make_client(session).login("op", "pw")

    def test_set_bearer_token_directly(self):
        session = FakeSession()
        make_client(session).set_bearer_token(SECRET_TOKEN)
        assert session.headers["Authorization"] == f"Bearer {SECRET_TOKEN}"

    def test_token_never_in_error_reprs(self):
        session = FakeSession([self.login_response(), FakeResponse(status_code=500, text="boom")])
        client = make_client(session)
        client.login("op", "pw")
        with pytest.raises(DeviceApiError) as excinfo:
            client.system_health()
        err = excinfo.value
        assert SECRET_TOKEN not in str(err)
        assert SECRET_TOKEN not in repr(err)
        assert SECRET_TOKEN not in json.dumps(err.diagnostic())
        assert err.request_headers["Authorization"] == REDACTED

    def test_redact_headers_preserves_other_headers(self):
        redacted = redact_headers(
            {"Authorization": f"Bearer {SECRET_TOKEN}", "Accept": "application/json"}
        )
        assert redacted == {"Authorization": REDACTED, "Accept": "application/json"}


class TestDeviceApiErrorDiagnostics:
    def test_body_excerpt_bounded_to_8kb(self):
        session = FakeSession(
            [FakeResponse(status_code=500, text="x" * (BODY_EXCERPT_LIMIT + 1000))]
        )
        with pytest.raises(DeviceApiError) as excinfo:
            make_client(session).system_health()
        assert len(excinfo.value.body_excerpt) == BODY_EXCERPT_LIMIT

    def test_diagnostic_shape(self):
        err = DeviceApiError(
            method="POST",
            path="/x",
            status=409,
            body_excerpt="conflict",
            elapsed_s=0.5,
            request_headers={"Authorization": "Bearer t"},
        )
        assert err.diagnostic() == {
            "method": "POST",
            "path": "/x",
            "status": 409,
            "body_excerpt": "conflict",
            "elapsed_s": 0.5,
            "request_headers": {"Authorization": REDACTED},
        }


class TestEndpointPaths:
    def test_start_and_stop_model_paths(self):
        session = FakeSession([FakeResponse(body={}), FakeResponse(body={})])
        client = make_client(session)
        client.start_model("model-a")
        client.stop_model("model-a")
        assert session.calls[0]["url"].endswith("/feature-configurations/models/model-a/start")
        assert session.calls[1]["url"].endswith("/feature-configurations/models/model-a/stop")

    def test_workflow_endpoints(self):
        session = FakeSession(
            [
                FakeResponse(body=[]),
                FakeResponse(body={"captureId": "c1"}),
                FakeResponse(body={"images": []}),
                FakeResponse(body={}),
            ]
        )
        client = make_client(session)
        client.workflows()
        client.run_workflow("wf1", {"returnImageString": False})
        client.workflow_images("wf1", params={"maxResults": 1})
        client.capture_task("wf1")
        urls = [call["url"] for call in session.calls]
        assert urls[0].endswith("/workflows")
        assert urls[1].endswith("/workflows/wf1/run")
        assert urls[2].endswith("/workflows/wf1/images")
        assert urls[3].endswith("/workflows/wf1/capture-task")
        assert session.calls[1]["json"] == {"returnImageString": False}
        assert session.calls[2]["params"] == {"maxResults": 1}

    def test_textgen_models_path(self):
        session = FakeSession([FakeResponse(body=[])])
        make_client(session).textgen_models()
        assert session.calls[0]["url"].endswith("/text-generation/models")


class TestWaitForModelState:
    def make_polling_client(self, responses, timeout_s=100.0):
        """Client with a fake clock advancing 1s per sleep call."""
        session = FakeSession(responses)
        clock = {"now": 0.0}

        def sleep(seconds):
            clock["now"] += seconds

        client = EdgeApiClient(
            "http://device:5000",
            session=session,
            sleep=sleep,
            monotonic=lambda: clock["now"],
        )
        return client, session

    def test_returns_when_target_reached(self):
        responses = [
            FakeResponse(body=[feature_entry("m", "LOADING")]),
            FakeResponse(body=[feature_entry("m", "LOADING")]),
            FakeResponse(body=[feature_entry("m", "READY")]),
        ]
        client, _ = self.make_polling_client(responses)
        assert client.wait_for_model_state("m", timeout_s=100.0) == "READY"

    def test_failed_state_raises_with_verbatim_reason(self):
        reason = "Engine core initialization failed: CUDA out of memory"
        responses = [
            FakeResponse(body=[feature_entry("m", "LOADING")]),
            FakeResponse(body=[feature_entry("m", "FAILED", reason=reason)]),
        ]
        client, _ = self.make_polling_client(responses)
        with pytest.raises(ModelWaitError) as excinfo:
            client.wait_for_model_state("m", timeout_s=100.0)
        assert excinfo.value.reason == reason
        assert reason in str(excinfo.value)  # surfaced verbatim
        assert not excinfo.value.timed_out

    def test_timeout_raises_with_last_observed_state(self):
        responses = [FakeResponse(body=[feature_entry("m", "LOADING")]) for _ in range(50)]
        client, _ = self.make_polling_client(responses)
        with pytest.raises(ModelWaitError) as excinfo:
            client.wait_for_model_state("m", timeout_s=5.0)
        assert excinfo.value.timed_out
        assert excinfo.value.state == "LOADING"
        assert "LOADING" in str(excinfo.value)

    def test_timeout_on_model_never_reported(self):
        responses = [FakeResponse(body=[]) for _ in range(50)]
        client, _ = self.make_polling_client(responses)
        with pytest.raises(ModelWaitError, match="never reported"):
            client.wait_for_model_state("ghost", timeout_s=5.0)

    def test_poll_interval_backs_off(self):
        responses = [FakeResponse(body=[feature_entry("m", "LOADING")]) for _ in range(4)] + [
            FakeResponse(body=[feature_entry("m", "READY")])
        ]
        session = FakeSession(responses)
        sleeps = []
        clock = {"now": 0.0}

        def sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds

        client = EdgeApiClient(
            "http://device:5000",
            session=session,
            sleep=sleep,
            monotonic=lambda: clock["now"],
        )
        client.wait_for_model_state("m", timeout_s=1000.0)
        assert sleeps == [1.0, 1.5, 2.25, 3.375]

    def test_custom_target_state(self):
        responses = [FakeResponse(body=[feature_entry("m", "UNAVAILABLE")])]
        client, _ = self.make_polling_client(responses)
        assert (
            client.wait_for_model_state("m", target="UNAVAILABLE", timeout_s=10.0) == "UNAVAILABLE"
        )


class TestGenerate:
    def test_generate_posts_prompt_and_params(self):
        session = FakeSession([FakeResponse(body={"model_name": "m", "generated_text": "hello"})])
        client = make_client(session)
        result = client.generate("m", "say hi", params={"max_tokens": 8})
        assert result["generated_text"] == "hello"
        assert session.calls[0]["json"] == {"prompt": "say hi", "max_tokens": 8}
        assert session.calls[0]["url"].endswith("/text-generation/m/generate")


class TestGenerateStream:
    def sse_lines(self, payloads):
        lines = []
        for payload in payloads:
            lines.append(f"data: {json.dumps(payload)}")
            lines.append("")
        return lines

    def test_events_decoded_in_order(self):
        payloads = [{"token": "a"}, {"token": "b"}, {"done": True}]
        session = FakeSession([FakeResponse(lines=self.sse_lines(payloads))])
        events = list(make_client(session).generate_stream("m", "hi"))
        assert events == payloads
        assert session.calls[0]["stream"] is True
        assert session.calls[0]["url"].endswith("/text-generation/m/generate-stream")

    def test_non_2xx_raises_before_iteration(self):
        session = FakeSession([FakeResponse(status_code=409, text='{"state": "loading"}')])
        # DeviceApiError must surface at call time, not on first next().
        with pytest.raises(DeviceApiError):
            make_client(session).generate_stream("m", "hi")

    def test_malformed_event_payload_raises(self):
        session = FakeSession([FakeResponse(lines=["data: not-json{", ""])])
        events = make_client(session).generate_stream("m", "hi")
        with pytest.raises(SseStreamError, match="not valid JSON"):
            list(events)

    def test_truncated_stream_raises(self):
        session = FakeSession([FakeResponse(lines=['data: {"token": "a"}', "", 'data: {"tok'])])
        events = make_client(session).generate_stream("m", "hi")
        with pytest.raises(SseStreamError, match="mid-event"):
            list(events)
