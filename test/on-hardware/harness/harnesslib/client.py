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
error reprs, logs, or the results bundle.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

import requests
from harnesslib.config import Timeouts
from harnesslib.sse import SseStreamError, iter_data_events

#: Upper bound on the response-body excerpt captured into diagnostics (Req 8.2).
BODY_EXCERPT_LIMIT = 8 * 1024

#: Per-call timeout for simple request/response calls that no stage timeout
#: covers (health, enumeration, start/stop kicks).
DEFAULT_REQUEST_TIMEOUT_S = 30.0

#: Replacement value the diagnostics formatter substitutes for secrets.
REDACTED = "<redacted>"

#: Header names (lowercase) whose values must never reach diagnostics.
_SENSITIVE_HEADERS = frozenset({"authorization"})


def redact_headers(headers: Mapping[str, Any]) -> Dict[str, str]:
    """A diagnostics-safe copy of ``headers`` with sensitive values replaced
    by :data:`REDACTED` (Req 3.3)."""
    return {
        name: (REDACTED if name.lower() in _SENSITIVE_HEADERS else str(value))
        for name, value in headers.items()
    }


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
    ) -> Any:
        """One Backend_API call; raises :class:`DeviceApiError` on non-2xx."""
        started = self._monotonic()
        response = self._session.request(
            method,
            self.base_url + path,
            json=json_body,
            params=params,
            timeout=timeout if timeout is not None else DEFAULT_REQUEST_TIMEOUT_S,
            stream=stream,
        )
        elapsed_s = self._monotonic() - started
        if not 200 <= response.status_code < 300:
            raise DeviceApiError(
                method=method,
                path=path,
                status=response.status_code,
                body_excerpt=response.text or "",
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
