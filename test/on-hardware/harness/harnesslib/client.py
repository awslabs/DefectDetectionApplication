"""EdgeApiClient: typed HTTP wrapper over the Target_Device Backend_API.

One method per endpoint the stages drive — the harness is a pure HTTP client
of the device (design: Device-only surface). Every request applies a per-call
timeout derived from the stage timeouts so a hung device cannot stall a call
indefinitely (Req 8.4 support), and every non-2xx response raises
:class:`DeviceApiError` carrying the method, path, status, a size-bounded
body excerpt, and the elapsed time for failure diagnostics (Req 8.2).

Credential hygiene (Req 3.3): ``login()`` attaches the bearer token to the
session and returns only the non-secret login metadata; the diagnostics
formatter redacts the ``Authorization`` header, so tokens can never appear in
error reprs, logs, or the results bundle. Request bodies never reach a
diagnostic, and the stream camera calls that carry Stream_Credentials also
scrub the password and URL secret from the response excerpt, so a device
that echoed them still could not leak them through the harness.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional

import requests
from harnesslib.config import Timeouts
from harnesslib.sse import SseStreamError, iter_data_events

#: Upper bound on the response-body excerpt captured into diagnostics (Req 8.2).
BODY_EXCERPT_LIMIT = 8 * 1024

#: Per-call timeout for simple request/response calls that no stage timeout
#: covers (health, enumeration, start/stop kicks).
DEFAULT_REQUEST_TIMEOUT_S = 30.0

#: Methods sent a second time after a connection error: reads only, which
#: change nothing on the device (see ``EdgeApiClient._request``).
RETRIED_METHODS = frozenset({"GET", "HEAD"})

#: Per-call timeout of ``POST /image-sources/{id}/test-connection``: the
#: device answers within 20 s (rtsp-rtmp-stream-cameras Requirement 4.3).
CONNECTION_TEST_TIMEOUT_S = 30.0

#: Per-call timeout of ``GET /streams/capabilities``: the device blocks up to
#: 30 s while its startup probe runs, then answers 503.
STREAM_CAPABILITIES_TIMEOUT_S = 45.0

#: How long :meth:`EdgeApiClient.wait_for_stream_capabilities` keeps retrying
#: a 503 (the device's own probe gives up after 90 s).
STREAM_CAPABILITIES_WAIT_S = 120.0

#: Terminal statuses of a Workflow_Engine execution (device:
#: ``workflow_engine.pipeline_executor.EXECUTION_STATUS_*``).
EXECUTION_TERMINAL_STATUSES = frozenset({"completed", "failed"})

#: Replacement value the diagnostics formatter substitutes for secrets.
REDACTED = "<redacted>"

#: Header names (lowercase) whose values must never reach diagnostics.
_SENSITIVE_HEADERS = frozenset({"authorization"})

#: Stream_Credentials fields whose values are secrets.
_CREDENTIAL_SECRET_FIELDS = ("password", "urlSecret")


def redact_headers(headers: Mapping[str, Any]) -> Dict[str, str]:
    """A diagnostics-safe copy of ``headers`` with sensitive values replaced
    by :data:`REDACTED` (Req 3.3)."""
    return {
        name: (REDACTED if name.lower() in _SENSITIVE_HEADERS else str(value))
        for name, value in headers.items()
    }


def redact_secrets(text: str, secrets: Iterable[Any]) -> str:
    """``text`` with every occurrence of each secret replaced by
    :data:`REDACTED`, both verbatim and in its JSON-escaped forms (a
    response body is JSON, so a quote, backslash or non-ASCII character in
    a secret shows up escaped)."""
    forms = set()
    for secret in secrets:
        if not secret:
            continue
        value = str(secret)
        forms.add(value)
        forms.add(json.dumps(value)[1:-1])
        forms.add(json.dumps(value, ensure_ascii=False)[1:-1])
    # Longest first, so a secret that contains another is masked whole.
    for form in sorted(forms, key=len, reverse=True):
        if form:
            text = text.replace(form, REDACTED)
    return text


def credential_secrets(body: Optional[Mapping[str, Any]]) -> List[str]:
    """The secret values (password, URL secret) of an Image_Source request
    body's write-only ``credentials`` object."""
    credentials = (body or {}).get("credentials")
    if not isinstance(credentials, Mapping):
        return []
    return [str(credentials[name]) for name in _CREDENTIAL_SECRET_FIELDS if credentials.get(name)]


class DeviceApiError(Exception):
    """A non-2xx Backend_API response, carrying bounded diagnostics (Req 8.2).

    Request headers are redacted at construction time (never stored raw), so
    no repr, log line, or serialized diagnostic can leak the bearer token
    (Req 3.3).
    """

    def __init__(
        self,
        method: str,
        path: str,
        status: int,
        body_excerpt: str,
        elapsed_s: float,
        request_headers: Optional[Mapping[str, Any]] = None,
    ):
        self.method = method
        self.path = path
        self.status = status
        self.body_excerpt = body_excerpt[:BODY_EXCERPT_LIMIT]
        self.elapsed_s = elapsed_s
        self.request_headers = redact_headers(request_headers or {})
        super().__init__(
            f"{method} {path} -> HTTP {status} after {elapsed_s:.2f}s: " f"{self.body_excerpt}"
        )

    def diagnostic(self) -> Dict[str, Any]:
        """The structured failure diagnostic for the results bundle."""
        return {
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "body_excerpt": self.body_excerpt,
            "elapsed_s": self.elapsed_s,
            "request_headers": dict(self.request_headers),
        }


class ModelWaitError(Exception):
    """A model failed to reach the requested state (Reqs 4.2, 5.1).

    ``reason`` carries the device-reported failure reason verbatim when the
    device supplied one; ``timed_out`` distinguishes a poll-loop timeout from
    a device-reported FAILED state.
    """

    def __init__(
        self,
        model_name: str,
        target: str,
        state: Optional[str],
        reason: Optional[str],
        elapsed_s: float,
        timed_out: bool = False,
    ):
        self.model_name = model_name
        self.target = target
        self.state = state
        self.reason = reason
        self.elapsed_s = elapsed_s
        self.timed_out = timed_out
        if timed_out:
            observed = state if state is not None else "never reported by device"
            message = (
                f"Model {model_name!r} did not reach {target} within "
                f"{elapsed_s:.1f}s (last state: {observed})"
            )
        else:
            message = (
                f"Model {model_name!r} reached {state} while waiting for "
                f"{target} after {elapsed_s:.1f}s"
            )
        if reason:
            message += f"; device-reported reason: {reason}"
        super().__init__(message)


class ExecutionWaitError(Exception):
    """A Workflow_Engine execution did not reach a terminal status in time;
    ``status`` is the last one the device reported."""

    def __init__(self, execution_id: str, status: Optional[str], elapsed_s: float):
        self.execution_id = execution_id
        self.status = status
        self.elapsed_s = elapsed_s
        observed = status if status is not None else "never reported by device"
        super().__init__(
            f"Workflow execution {execution_id!r} did not finish within "
            f"{elapsed_s:.1f}s (last status: {observed})"
        )


def _vllm_failure_reason(entry: Mapping[str, Any]) -> Optional[str]:
    """The verbatim device-reported failure reason of a feature-config entry
    (vLLM entries carry it as ``defaultConfiguration.failureReason``)."""
    default_configuration = entry.get("defaultConfiguration") or {}
    if isinstance(default_configuration, Mapping):
        return default_configuration.get("failureReason")
    return None


class EdgeApiClient:
    """Thin typed wrapper over ``requests.Session`` for the Backend_API.

    :param base_url: device base URL (``http://host:5000``), no trailing slash
        required.
    :param timeouts: stage timeout bounds; defaults to the design defaults.
    :param session: injectable transport for tests; defaults to a fresh
        ``requests.Session``.
    :param sleep: injectable sleep for the poll loop (tests pass a no-op).
    :param monotonic: injectable clock for elapsed/deadline computation.
    """

    def __init__(
        self,
        base_url: str,
        timeouts: Optional[Timeouts] = None,
        session: Optional[Any] = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeouts = timeouts if timeouts is not None else Timeouts()
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._monotonic = monotonic

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        stream: bool = False,
        secrets: Iterable[Any] = (),
    ) -> Any:
        """One Backend_API call; raises :class:`DeviceApiError` on non-2xx.

        ``secrets`` are values the request carries (never the body itself,
        which no diagnostic includes); they are scrubbed from the response
        excerpt before it is bounded, in case the device echoes them.
        """
        started = self._monotonic()
        send = lambda: self._session.request(  # noqa: E731 - sent once, twice for a stale read
            method,
            self.base_url + path,
            json=json_body,
            params=params,
            timeout=timeout if timeout is not None else DEFAULT_REQUEST_TIMEOUT_S,
            stream=stream,
        )
        try:
            response = send()
        except requests.exceptions.ConnectionError:
            # Over a slow tunnel the device can close a keep-alive
            # connection (its idle timeout starts when it finished writing
            # a large response, not when the harness finished reading it),
            # so a read sent on it fails before reaching the device. A read
            # is sent once more on a fresh connection; nothing else is.
            if method.upper() not in RETRIED_METHODS:
                raise
            response = send()
        elapsed_s = self._monotonic() - started
        if not 200 <= response.status_code < 300:
            raise DeviceApiError(
                method=method,
                path=path,
                status=response.status_code,
                body_excerpt=redact_secrets(response.text or "", secrets),
                elapsed_s=elapsed_s,
                request_headers=getattr(self._session, "headers", {}),
            )
        return response

    # ------------------------------------------------------------------
    # Health and identity (Reqs 3.1, 3.2)
    # ------------------------------------------------------------------

    def system_health(self) -> Dict[str, Any]:
        """GET ``/system-health`` — the readiness probe."""
        return self._request("GET", "/system-health").json()

    def component_status(self) -> Dict[str, Any]:
        """GET ``/dda-component-status`` — device identity (LocalServer
        version) for the Results_Bundle."""
        return self._request("GET", "/dda-component-status").json()

    # ------------------------------------------------------------------
    # Authentication (Req 3.3)
    # ------------------------------------------------------------------

    def auth_status(self) -> Dict[str, Any]:
        """GET ``/local-auth/status`` → ``{localLoginEnabled}``."""
        return self._request("GET", "/local-auth/status").json()

    def login(self, username: str, password: str) -> Dict[str, Any]:
        """POST ``/local-auth/login``; attach the issued bearer token to the
        session and return only the non-secret metadata (never the token)."""
        response = self._request(
            "POST",
            "/local-auth/login",
            json_body={"username": username, "password": password},
        )
        body = response.json()
        token = body.get("token")
        if not token:
            raise DeviceApiError(
                method="POST",
                path="/local-auth/login",
                status=response.status_code,
                body_excerpt="login succeeded but the response carried no token",
                elapsed_s=0.0,
                request_headers=getattr(self._session, "headers", {}),
            )
        self.set_bearer_token(token)
        return {key: value for key, value in body.items() if key != "token"}

    def set_bearer_token(self, token: str) -> None:
        """Attach a bearer token to every subsequent request (for targets
        whose credential reference resolves to a ready-made token)."""
        self._session.headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------------
    # Model lifecycle (Reqs 4.1, 4.2, 5.1)
    # ------------------------------------------------------------------

    def feature_configurations(self) -> List[Dict[str, Any]]:
        """GET ``/feature-configurations`` — vision models and ``VllmModel``
        entries with their status."""
        return self._request("GET", "/feature-configurations").json()

    def start_model(self, model_name: str) -> Dict[str, Any]:
        """GET ``/feature-configurations/models/{name}/start``."""
        return self._request("GET", f"/feature-configurations/models/{model_name}/start").json()

    def stop_model(self, model_name: str) -> Dict[str, Any]:
        """GET ``/feature-configurations/models/{name}/stop``."""
        return self._request("GET", f"/feature-configurations/models/{model_name}/stop").json()

    def model_entry(self, model_name: str) -> Optional[Dict[str, Any]]:
        """The feature-config entry for ``model_name``, or None when the
        device does not report it."""
        for entry in self.feature_configurations():
            if entry.get("modelName") == model_name:
                return entry
        return None

    def wait_for_model_state(
        self,
        model_name: str,
        target: str = "READY",
        timeout_s: Optional[float] = None,
        initial_interval_s: float = 1.0,
        backoff: float = 1.5,
        max_interval_s: float = 10.0,
    ) -> str:
        """Poll ``/feature-configurations`` until ``model_name`` reaches
        ``target``; returns the terminal state (Reqs 4.2, 5.1).

        Backoff grows the poll interval from ``initial_interval_s`` by
        ``backoff`` per attempt, capped at ``max_interval_s``.

        :raises ModelWaitError: when the device reports FAILED (carrying the
            device-reported reason verbatim) or the timeout elapses.
        """
        if timeout_s is None:
            timeout_s = self.timeouts.model_ready_s
        started = self._monotonic()
        deadline = started + timeout_s
        interval = initial_interval_s
        state: Optional[str] = None
        reason: Optional[str] = None
        while True:
            entry = self.model_entry(model_name)
            if entry is not None:
                state = entry.get("status")
                reason = _vllm_failure_reason(entry)
                if state == target:
                    return state
                if state == "FAILED":
                    raise ModelWaitError(
                        model_name,
                        target=target,
                        state=state,
                        reason=reason,
                        elapsed_s=self._monotonic() - started,
                    )
            now = self._monotonic()
            if now >= deadline:
                raise ModelWaitError(
                    model_name,
                    target=target,
                    state=state,
                    reason=reason,
                    elapsed_s=now - started,
                    timed_out=True,
                )
            self._sleep(min(interval, deadline - now))
            interval = min(interval * backoff, max_interval_s)

    # ------------------------------------------------------------------
    # Text generation (Reqs 5.2, 5.3)
    # ------------------------------------------------------------------

    def textgen_models(self) -> List[Dict[str, Any]]:
        """GET ``/text-generation/models`` — every vLLM model with its
        serving state (``{model_name, state, reason?}``)."""
        return self._request("GET", "/text-generation/models").json()

    def generate(
        self,
        model_name: str,
        prompt: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """POST ``/text-generation/{model}/generate`` (non-streaming)."""
        body: Dict[str, Any] = {"prompt": prompt}
        body.update(params or {})
        response = self._request(
            "POST",
            f"/text-generation/{model_name}/generate",
            json_body=body,
            timeout=timeout_s if timeout_s is not None else self.timeouts.generate_s,
        )
        return response.json()

    def generate_stream(
        self,
        model_name: str,
        prompt: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
    ) -> Iterator[Dict[str, Any]]:
        """POST ``/text-generation/{model}/generate-stream`` and yield each
        SSE event's JSON payload in order (Req 5.3).

        The request (and its status check) happens eagerly; only event
        consumption is lazy, so a non-2xx raises :class:`DeviceApiError` at
        call time, before iteration begins.
        """
        body: Dict[str, Any] = {"prompt": prompt}
        body.update(params or {})
        response = self._request(
            "POST",
            f"/text-generation/{model_name}/generate-stream",
            json_body=body,
            timeout=timeout_s if timeout_s is not None else self.timeouts.generate_s,
            stream=True,
        )
        return self._decode_sse_events(response)

    @staticmethod
    def _decode_sse_events(response: Any) -> Iterator[Dict[str, Any]]:
        for payload in iter_data_events(response.iter_lines()):
            try:
                yield json.loads(payload)
            except ValueError as err:
                raise SseStreamError(
                    f"SSE event payload is not valid JSON: {payload[:200]!r}"
                ) from err

    # ------------------------------------------------------------------
    # Workflows (Req 6)
    # ------------------------------------------------------------------

    def workflows(self) -> List[Dict[str, Any]]:
        """GET ``/workflows`` — the Deployed_Workflows the device reports."""
        return self._request("GET", "/workflows").json()

    def run_workflow(
        self,
        workflow_id: str,
        request: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """POST ``/workflows/{id}/run``."""
        response = self._request(
            "POST",
            f"/workflows/{workflow_id}/run",
            json_body=request,
            timeout=(timeout_s if timeout_s is not None else self.timeouts.workflow_output_s),
        )
        return response.json()

    def workflow_images(
        self, workflow_id: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """GET ``/workflows/{id}/images`` — captured output artifacts."""
        return self._request("GET", f"/workflows/{workflow_id}/images", params=params).json()

    def capture_task(self, workflow_id: str) -> Dict[str, Any]:
        """GET ``/workflows/{id}/capture-task`` — capture task status
        (``{}`` when none is running)."""
        return self._request("GET", f"/workflows/{workflow_id}/capture-task").json()

    # ------------------------------------------------------------------
    # Stream cameras (rtsp-rtmp-stream-cameras)
    # ------------------------------------------------------------------

    def stream_capabilities(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """GET ``/streams/capabilities`` — the Device_Stream_Capabilities
        (protocols, TLS, per-codec hardware/software decoders, versions).
        The device blocks up to 30 s while its probe runs, then answers 503."""
        timeout = timeout_s if timeout_s is not None else STREAM_CAPABILITIES_TIMEOUT_S
        return self._request("GET", "/streams/capabilities", timeout=timeout).json()

    def wait_for_stream_capabilities(
        self, timeout_s: float = STREAM_CAPABILITIES_WAIT_S, interval_s: float = 1.0
    ) -> Dict[str, Any]:
        """:meth:`stream_capabilities`, retried while the device answers 503
        (its startup probe is still running) until ``timeout_s``.

        :raises DeviceApiError: for any other error status, or a 503 that
            outlasts ``timeout_s``.
        """
        deadline = self._monotonic() + timeout_s
        while True:
            try:
                return self.stream_capabilities()
            except DeviceApiError as err:
                now = self._monotonic()
                if err.status != 503 or now >= deadline:
                    raise
                self._sleep(min(interval_s, deadline - now))

    def create_image_source(self, body: Dict[str, Any]) -> str:
        """POST ``/image-sources``; returns the new ``imageSourceId``.

        Write-only ``credentials`` in ``body`` are scrubbed from any failure
        diagnostic (:func:`credential_secrets`).
        """
        response = self._request(
            "POST", "/image-sources", json_body=body, secrets=credential_secrets(body)
        )
        payload = response.json()
        image_source_id = payload.get("imageSourceId") if isinstance(payload, dict) else None
        if not image_source_id:
            raise DeviceApiError(
                method="POST",
                path="/image-sources",
                status=response.status_code,
                body_excerpt="create succeeded but the response carried no imageSourceId",
                elapsed_s=0.0,
                request_headers=getattr(self._session, "headers", {}),
            )
        return image_source_id

    def update_image_source(self, image_source_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """PATCH ``/image-sources/{id}`` (stream settings merge; blank
        credentials keep what is stored). Credentials in ``body`` are
        scrubbed from any failure diagnostic."""
        return self._request(
            "PATCH",
            f"/image-sources/{image_source_id}",
            json_body=body,
            secrets=credential_secrets(body),
        ).json()

    def delete_image_source(self, image_source_id: str) -> Dict[str, Any]:
        """DELETE ``/image-sources/{id}``; for a stream camera this also
        removes its stored credentials and stops its session."""
        return self._request("DELETE", f"/image-sources/{image_source_id}").json()

    def image_source(self, image_source_id: str) -> Dict[str, Any]:
        """GET ``/image-sources/{id}`` (stream cameras carry
        ``credentialsConfigured``, never the credentials)."""
        return self._request("GET", f"/image-sources/{image_source_id}").json()

    def image_sources(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET ``/image-sources``, optionally ``?type=RTSP|RTMP|...``."""
        params = {"type": source_type} if source_type else None
        return self._request("GET", "/image-sources", params=params).json()

    def connection_test(
        self, image_source_id: str, timeout_s: Optional[float] = None
    ) -> Dict[str, Any]:
        """POST ``/image-sources/{id}/test-connection`` — ``{ok, category,
        message, streamHealth, image, imageError}``; the device answers
        within 20 s."""
        timeout = timeout_s if timeout_s is not None else CONNECTION_TEST_TIMEOUT_S
        return self._request(
            "POST", f"/image-sources/{image_source_id}/test-connection", timeout=timeout
        ).json()

    def stream_health(self, image_source_id: str) -> Dict[str, Any]:
        """GET ``/image-sources/{id}/stream-health`` — the camera's
        Stream_Health (``stopped`` when no session runs)."""
        return self._request("GET", f"/image-sources/{image_source_id}/stream-health").json()

    def preview_image_source(self, image_source_id: str) -> Dict[str, Any]:
        """POST ``/image-sources/{id}/preview`` with ``{}`` — ``{image,
        imageFileName}``; 503 while a stream camera has no frame."""
        return self._request(
            "POST", f"/image-sources/{image_source_id}/preview", json_body={}
        ).json()

    # ------------------------------------------------------------------
    # Workflow_Engine registrations (deployed workflows)
    # ------------------------------------------------------------------

    def workflow_registrations(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        """GET ``/workflows/registrations`` — the deployed workflow
        registrations (``registered`` ones are runnable)."""
        params = {"includeInactive": "true"} if include_inactive else None
        return self._request("GET", "/workflows/registrations", params=params).json()

    def trigger_registration(self, registration_id: str) -> Dict[str, Any]:
        """POST ``/workflows/registrations/{id}/trigger`` — the pending
        execution. 409 when the registration is invalid, or
        ``CONTINUOUS_WORKFLOW_RUNNING`` when it is continuous and not paused."""
        return self._request("POST", f"/workflows/registrations/{registration_id}/trigger").json()

    def workflow_execution(self, execution_id: str) -> Dict[str, Any]:
        """GET ``/workflows/executions/{id}`` — one run's status."""
        return self._request("GET", f"/workflows/executions/{execution_id}").json()

    def workflow_execution_metadata(self, execution_id: str) -> Dict[str, Any]:
        """GET ``/workflows/executions/{id}/metadata`` — the run metadata
        (``stream`` and ``trigger`` for a stream run); ``{}`` when absent."""
        return self._request("GET", f"/workflows/executions/{execution_id}/metadata").json()

    def wait_for_execution(
        self,
        execution_id: str,
        timeout_s: Optional[float] = None,
        initial_interval_s: float = 1.0,
        backoff: float = 1.5,
        max_interval_s: float = 5.0,
    ) -> Dict[str, Any]:
        """Poll :meth:`workflow_execution` until the run reaches a terminal
        status (:data:`EXECUTION_TERMINAL_STATUSES`) and return it.

        :raises ExecutionWaitError: when ``timeout_s`` (default
            ``timeouts.workflow_output_s``) elapses first.
        """
        if timeout_s is None:
            timeout_s = self.timeouts.workflow_output_s
        started = self._monotonic()
        deadline = started + timeout_s
        interval = initial_interval_s
        while True:
            execution = self.workflow_execution(execution_id)
            status = execution.get("status")
            if status in EXECUTION_TERMINAL_STATUSES:
                return execution
            now = self._monotonic()
            if now >= deadline:
                raise ExecutionWaitError(execution_id, status, now - started)
            self._sleep(min(interval, deadline - now))
            interval = min(interval * backoff, max_interval_s)

    def continuous_status(self, registration_id: str) -> Dict[str, Any]:
        """GET ``/workflows/registrations/{id}/continuous`` — state,
        configured/effective rate, counters; 404 for a registration that is
        not continuous."""
        return self._request("GET", f"/workflows/registrations/{registration_id}/continuous").json()

    def pause_continuous(self, registration_id: str) -> Dict[str, Any]:
        """POST ``/workflows/registrations/{id}/continuous/pause`` — the
        continuous status after the pause (it persists across restarts)."""
        return self._request(
            "POST", f"/workflows/registrations/{registration_id}/continuous/pause"
        ).json()

    def resume_continuous(self, registration_id: str) -> Dict[str, Any]:
        """POST ``/workflows/registrations/{id}/continuous/resume`` — the
        continuous status after the resume (a no-op when not paused)."""
        return self._request(
            "POST", f"/workflows/registrations/{registration_id}/continuous/resume"
        ).json()
