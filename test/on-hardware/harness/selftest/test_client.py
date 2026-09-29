"""Unit tests for harnesslib.client (Reqs 3.3, 4.2, 5.1, 5.3, 8.2).

All transport is mocked (a FakeSession standing in for requests.Session):
bearer token attach after login, token never in error reprs, body excerpt
bounding, poll-loop terminal states, and streaming event decoding; for the
stream camera stage, every new endpoint's method/path/timeout, stream
credentials never in error diagnostics, and the capability and execution
pollers.
"""

import json

import pytest
import requests
from harnesslib.client import (
    BODY_EXCERPT_LIMIT,
    CONNECTION_TEST_TIMEOUT_S,
    REDACTED,
    STREAM_CAPABILITIES_TIMEOUT_S,
    DeviceApiError,
    EdgeApiClient,
    ExecutionWaitError,
    ModelWaitError,
    credential_secrets,
    redact_headers,
    redact_secrets,
)
from harnesslib.config import SecretStr, Timeouts
from harnesslib.sse import SseStreamError

SECRET_TOKEN = "sekrit-token-value"
STREAM_PASSWORD = "stream-pass-SECRET"
STREAM_URL_SECRET = "live-key-SECRET"


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
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def make_client(session, **kwargs):
    kwargs.setdefault("sleep", lambda seconds: None)
    return EdgeApiClient("http://device:5000", session=session, **kwargs)


def make_clocked_client(responses, **kwargs):
    """Client over queued responses with a fake clock that advances by each
    sleep; returns ``(client, session, sleeps)``."""
    session = FakeSession(responses)
    clock = {"now": 0.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    client = EdgeApiClient(
        "http://device:5000",
        session=session,
        sleep=sleep,
        monotonic=lambda: clock["now"],
        **kwargs,
    )
    return client, session, sleeps


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

    def test_a_read_on_a_stale_connection_is_sent_once_more(self):
        # Found over the device tunnel (task 25.3): the device closed a
        # keep-alive connection while a large preview was still in transit.
        stale = requests.exceptions.ConnectionError("Remote end closed connection without response")
        session = FakeSession([stale, FakeResponse(body={"state": "streaming"})])
        assert make_client(session).stream_health("src1") == {"state": "streaming"}
        assert [call["method"] for call in session.calls] == ["GET", "GET"]

    def test_a_second_connection_error_is_raised(self):
        stale = requests.exceptions.ConnectionError("closed")
        session = FakeSession([stale, stale])
        with pytest.raises(requests.exceptions.ConnectionError):
            make_client(session).system_health()
        assert len(session.calls) == 2

    @pytest.mark.parametrize("call", [
        lambda client: client.connection_test("src1"),
        lambda client: client.delete_image_source("src1"),
        lambda client: client.create_image_source({"type": "RTSP"}),
    ])
    def test_nothing_but_a_read_is_sent_twice(self, call):
        session = FakeSession([requests.exceptions.ConnectionError("closed")])
        with pytest.raises(requests.exceptions.ConnectionError):
            call(make_client(session))
        assert len(session.calls) == 1


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


class TestStreamCameraEndpoints:
    def test_stream_capabilities_path_and_timeout(self):
        session = FakeSession([FakeResponse(body={"rtsp": True})])
        assert make_client(session).stream_capabilities() == {"rtsp": True}
        call = session.calls[0]
        assert call["method"] == "GET"
        assert call["url"].endswith("/streams/capabilities")
        # The device blocks up to 30 s while probing: the call outlasts it.
        assert call["timeout"] == STREAM_CAPABILITIES_TIMEOUT_S > 30.0

    def test_create_image_source_returns_the_new_id(self):
        body = {
            "type": "RTSP",
            "name": "cam",
            "location": "rtsp://cam:8554/h264",
            "streamSettings": {"decoder": "auto"},
        }
        session = FakeSession([FakeResponse(body={"imageSourceId": "src-1"})])
        assert make_client(session).create_image_source(body) == "src-1"
        call = session.calls[0]
        assert call["method"] == "POST"
        assert call["url"].endswith("/image-sources")
        assert call["json"] == body

    def test_create_without_an_id_in_the_response_fails(self):
        session = FakeSession([FakeResponse(body={"unexpected": True})])
        with pytest.raises(DeviceApiError, match="no imageSourceId"):
            make_client(session).create_image_source({"type": "RTSP"})

    def test_image_source_crud_paths(self):
        session = FakeSession(
            [
                FakeResponse(body={"imageSourceId": "src-1"}),
                FakeResponse(body={"imageSourceId": "src-1"}),
                FakeResponse(body={"imageSourceId": "src-1", "credentialsConfigured": True}),
                FakeResponse(body=[]),
                FakeResponse(body=[]),
            ]
        )
        client = make_client(session)
        client.update_image_source("src-1", {"streamSettings": {"latencyMs": 100}})
        client.delete_image_source("src-1")
        client.image_source("src-1")
        client.image_sources("RTMP")
        client.image_sources()
        methods_urls = [(call["method"], call["url"]) for call in session.calls]
        assert methods_urls == [
            ("PATCH", "http://device:5000/image-sources/src-1"),
            ("DELETE", "http://device:5000/image-sources/src-1"),
            ("GET", "http://device:5000/image-sources/src-1"),
            ("GET", "http://device:5000/image-sources"),
            ("GET", "http://device:5000/image-sources"),
        ]
        assert session.calls[0]["json"] == {"streamSettings": {"latencyMs": 100}}
        assert session.calls[3]["params"] == {"type": "RTMP"}
        assert session.calls[4]["params"] is None

    def test_stream_session_paths(self):
        session = FakeSession(
            [
                FakeResponse(body={"ok": True, "category": None}),
                FakeResponse(body={"state": "streaming"}),
                FakeResponse(body={"image": "aGk=", "imageFileName": None}),
            ]
        )
        client = make_client(session)
        assert client.connection_test("src-1")["ok"] is True
        assert client.stream_health("src-1")["state"] == "streaming"
        assert client.preview_image_source("src-1")["image"] == "aGk="
        test, health, preview = session.calls
        assert (test["method"], test["url"]) == (
            "POST",
            "http://device:5000/image-sources/src-1/test-connection",
        )
        # The device answers within 20 s; the call waits a little longer.
        assert test["timeout"] == CONNECTION_TEST_TIMEOUT_S > 20.0
        assert (health["method"], health["url"]) == (
            "GET",
            "http://device:5000/image-sources/src-1/stream-health",
        )
        assert (preview["method"], preview["url"]) == (
            "POST",
            "http://device:5000/image-sources/src-1/preview",
        )
        assert preview["json"] == {}

    def test_workflow_engine_paths(self):
        session = FakeSession([FakeResponse(body=[])] + [FakeResponse(body={}) for _ in range(7)])
        client = make_client(session)
        client.workflow_registrations()
        client.trigger_registration("reg-1")
        client.workflow_execution("exec-1")
        client.workflow_execution_metadata("exec-1")
        client.continuous_status("reg-1")
        client.pause_continuous("reg-1")
        client.resume_continuous("reg-1")
        client.workflow_registrations(include_inactive=True)
        base = "http://device:5000/workflows"
        assert [(call["method"], call["url"]) for call in session.calls] == [
            ("GET", f"{base}/registrations"),
            ("POST", f"{base}/registrations/reg-1/trigger"),
            ("GET", f"{base}/executions/exec-1"),
            ("GET", f"{base}/executions/exec-1/metadata"),
            ("GET", f"{base}/registrations/reg-1/continuous"),
            ("POST", f"{base}/registrations/reg-1/continuous/pause"),
            ("POST", f"{base}/registrations/reg-1/continuous/resume"),
            ("GET", f"{base}/registrations"),
        ]
        assert session.calls[0]["params"] is None
        assert session.calls[7]["params"] == {"includeInactive": "true"}


class TestStreamCredentialRedaction:
    def credentialed_body(self):
        return {
            "type": "RTMP",
            "name": "cam",
            "location": "rtmp://cam:1935/live",
            "credentials": {
                "username": "camuser",
                "password": SecretStr(STREAM_PASSWORD),
                "urlSecret": STREAM_URL_SECRET,
            },
        }

    def echoing_error(self):
        """A 400 whose body echoes the credentials (a device bug)."""
        return FakeResponse(
            status_code=400,
            text=json.dumps({"detail": f"rejected {STREAM_PASSWORD} / {STREAM_URL_SECRET}"}),
        )

    def assert_redacted(self, err: DeviceApiError):
        for secret in (STREAM_PASSWORD, STREAM_URL_SECRET):
            assert secret not in str(err)
            assert secret not in repr(err)
            assert secret not in json.dumps(err.diagnostic())
        assert REDACTED in err.body_excerpt

    def test_create_failure_never_carries_the_credentials(self):
        session = FakeSession([self.echoing_error()])
        with pytest.raises(DeviceApiError) as excinfo:
            make_client(session).create_image_source(self.credentialed_body())
        self.assert_redacted(excinfo.value)
        # The request itself carried the real values.
        assert session.calls[0]["json"]["credentials"]["password"] == STREAM_PASSWORD

    def test_patch_failure_never_carries_the_credentials(self):
        session = FakeSession([self.echoing_error()])
        with pytest.raises(DeviceApiError) as excinfo:
            make_client(session).update_image_source(
                "src-1", {"credentials": self.credentialed_body()["credentials"]}
            )
        self.assert_redacted(excinfo.value)

    def test_request_body_never_reaches_a_diagnostic(self):
        session = FakeSession([FakeResponse(status_code=500, text="boom")])
        with pytest.raises(DeviceApiError) as excinfo:
            make_client(session).create_image_source(self.credentialed_body())
        assert STREAM_PASSWORD not in json.dumps(excinfo.value.diagnostic())
        assert excinfo.value.body_excerpt == "boom"

    def test_json_escaped_secret_redacted(self):
        secret = 'pa"ss\\wörd-SECRET'
        for ensure_ascii in (True, False):
            text = json.dumps({"detail": secret}, ensure_ascii=ensure_ascii)
            assert redact_secrets(text, [secret]) == json.dumps({"detail": REDACTED})

    def test_containing_secret_masked_whole(self):
        assert redact_secrets("key=abcdef", ["abc", "abcdef"]) == f"key={REDACTED}"

    def test_empty_secrets_ignored(self):
        assert redact_secrets("unchanged", ["", None]) == "unchanged"

    def test_credential_secrets_are_password_and_url_secret(self):
        body = {"credentials": {"username": "u", "password": "p", "urlSecret": "k"}}
        assert credential_secrets(body) == ["p", "k"]
        assert credential_secrets({"credentials": {"username": "u", "password": ""}}) == []
        assert credential_secrets({"name": "no credentials"}) == []
        assert credential_secrets(None) == []


class TestWaitForStreamCapabilities:
    def probing(self):
        return FakeResponse(status_code=503, text='{"detail": "probe still running"}')

    def test_retries_503_until_the_probe_answers(self):
        client, session, sleeps = make_clocked_client(
            [self.probing(), self.probing(), FakeResponse(body={"rtsp": True})]
        )
        assert client.wait_for_stream_capabilities(timeout_s=60.0, interval_s=2.0) == {
            "rtsp": True
        }
        assert len(session.calls) == 3
        assert sleeps == [2.0, 2.0]

    def test_other_errors_raise_at_once(self):
        client, session, _ = make_clocked_client([FakeResponse(status_code=404, text="nope")])
        with pytest.raises(DeviceApiError) as excinfo:
            client.wait_for_stream_capabilities(timeout_s=60.0)
        assert excinfo.value.status == 404
        assert len(session.calls) == 1

    def test_503_past_the_deadline_raises(self):
        client, _, sleeps = make_clocked_client([self.probing() for _ in range(20)])
        with pytest.raises(DeviceApiError) as excinfo:
            client.wait_for_stream_capabilities(timeout_s=5.0, interval_s=2.0)
        assert excinfo.value.status == 503
        assert sum(sleeps) == pytest.approx(5.0)


def execution(status):
    return FakeResponse(body={"executionId": "exec-1", "status": status})


class TestWaitForExecution:
    def test_returns_the_terminal_execution(self):
        client, session, sleeps = make_clocked_client(
            [execution("pending"), execution("running"), execution("completed")]
        )
        assert client.wait_for_execution("exec-1", timeout_s=60.0)["status"] == "completed"
        assert all(call["url"].endswith("/workflows/executions/exec-1") for call in session.calls)
        assert sleeps == [1.0, 1.5]

    def test_failed_is_terminal(self):
        client, _, _ = make_clocked_client([execution("running"), execution("failed")])
        assert client.wait_for_execution("exec-1", timeout_s=60.0)["status"] == "failed"

    def test_timeout_raises_with_the_last_status(self):
        client, _, _ = make_clocked_client([execution("running") for _ in range(50)])
        with pytest.raises(ExecutionWaitError) as excinfo:
            client.wait_for_execution("exec-1", timeout_s=10.0)
        assert excinfo.value.status == "running"
        assert "running" in str(excinfo.value)
        assert "exec-1" in str(excinfo.value)

    def test_defaults_to_the_workflow_output_timeout(self):
        client, _, sleeps = make_clocked_client(
            [execution("pending") for _ in range(50)], timeouts=Timeouts(workflow_output_s=4.0)
        )
        with pytest.raises(ExecutionWaitError):
            client.wait_for_execution("exec-1")
        assert sum(sleeps) == pytest.approx(4.0)
