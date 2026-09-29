"""Stage 35 — stream cameras (rtsp-rtmp-stream-cameras task 25.5, Req 17.6).

Capability-gated on ``stream_cameras``: the module skips with a recorded
reason when the Device_Profile does not grant it, and the session
``stream_cameras_surface`` probe fails every test with
``CapabilityMismatchError`` when the capability is declared but
``GET /streams/capabilities`` does not answer (a LocalServer without the
feature).

The checks, each skipping with a reason naming its missing ``expected.*``
key when its input is not configured:

* capabilities — RTSP and RTMP ingest and H.264/H.265 software decoding are
  available; each codec's hardware decoder and the PyAV/FFmpeg/GStreamer
  versions are recorded as metrics (Requirement 16.5);
* ``stream_urls`` — each URL, as an Image_Source with the ``auto`` decoder
  policy, passes the connection test, previews an image and reports
  ``streaming``; codec, resolution, source rate and decoder are recorded
  per URL (Requirements 4.1, 4.3, 4.4, 7.4, 8.8);
* ``stream_secure_url`` + ``stream_credentials`` — a credentialed source
  connects, no response carries the password or URL user information, and
  after a wrong password is PATCHed the connection test fails with
  ``authentication_failed`` (Requirements 4.3, 6.1);
* ``stream_failures`` — each URL's connection test fails with exactly the
  configured category (Requirement 4.3);
* ``stream_workflow`` — a triggered run of that on_trigger workflow
  completes and its metadata records the frame it analyzed (Requirement
  10.4);
* ``continuous_workflow`` — the continuous workflow runs at a non-zero
  effective rate with its completed counter rising; a pause stops it and a
  resume restarts it (Requirements 11.1, 11.6, 12.5).

State_Restoration: every Image_Source the stage creates is recorded in the
session ``state_registry`` as soon as the device returns its id (pre-state
``None``: the harness created it) and deleted when its check finishes;
restoration deletes whatever a check left behind, at teardown, on failure
alike. The continuous pause is recorded before it is issued, so teardown
resumes it however the check ends. A triggered run is one-shot and leaves
its registration untouched, as in stage 30.

Credential hygiene: the stream password is resolved from its reference only
inside the credentialed check and wrapped so its repr is redacted; the
client scrubs it from failure diagnostics; the check scrubs device text and
reports leaks as JSON paths, never values, through ``pytest.fail`` (no
assertion introspection), and never records it as a metric.

Out of scope for this HTTP-only stage: backend/worker RSS and container
RestartCount soak sampling (task 25.3), which need shell access to the
device.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from secrets import token_hex
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pytest
import requests
from harnesslib.client import DeviceApiError, redact_secrets
from harnesslib.config import (
    HarnessConfigError,
    SecretStr,
    StreamCredentials,
    resolve_stream_credentials,
    stream_source_type,
)

pytestmark = [pytest.mark.stage("stream_cameras"), pytest.mark.capability("stream_cameras")]

#: Name prefix of every Image_Source the stage creates (plus a per-run token
#: and a sequence number), so a leftover is recognizable on the device.
SOURCE_NAME_PREFIX = "dda-harness-stream"
SOURCE_DESCRIPTION = (
    "Created by the Edge_Test_Harness stream camera stage; deleted when its check finishes."
)
#: Settings of every created source: the ``auto`` Decoder_Policy, so the
#: device decodes in hardware wherever it can (Requirement 7.4).
STREAM_SETTINGS = {"decoder": "auto"}

#: Device vocabulary (stream_ingest/health.py, workflow_engine/api.py,
#: workflow_engine/continuous_runner.py).
STREAMING = "streaming"
AUTHENTICATION_FAILED = "authentication_failed"
REGISTERED = "registered"
EXECUTION_COMPLETED = "completed"
CONTINUOUS_RUNNING = "running"
CONTINUOUS_PAUSED = "paused"
CONTINUOUS_WORKFLOW_RUNNING = "CONTINUOUS_WORKFLOW_RUNNING"

#: Stream_Health fields recorded per connected URL.
HEALTH_METRIC_KEYS = (
    "codec",
    "width",
    "height",
    "sourceFps",
    "decoder",
    "decoderElement",
    "decoderFallback",
)

#: A preview answering 503 (no frame yet) is retried within this window.
PREVIEW_RETRY_WINDOW_S = 10.0
PREVIEW_RETRY_INTERVAL_S = 2.0

#: Poll interval of continuous status transitions.
CONTINUOUS_POLL_INTERVAL_S = 1.0

#: The paused observation window covers two runs at the configured rate,
#: lasts at least this long, and at most ``timeouts.continuous_window_s``.
PAUSE_WINDOW_MIN_S = 5.0

#: A URL carrying user information (``scheme://user[:password]@``). The
#: device's own mask, ``scheme://***@host``, is not a leak.
_URL_USER_INFO = re.compile(r"[a-z][a-z0-9+.-]*://(?!\*\*\*@)[^/\s@]*@", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require(harness_target, *keys: str) -> None:
    """Skip unless every ``expected.<key>`` is configured; the reason names
    the missing keys."""
    missing = [key for key in keys if not getattr(harness_target.expected, key)]
    if missing:
        names = " and ".join(f"expected.{key}" for key in missing)
        verb = "is" if len(missing) == 1 else "are"
        pytest.skip(f"{names} {verb} not configured for device {harness_target.name}")


def _fail_with(header: str, problems: List[str]) -> None:
    """Fail with every problem in one message. ``pytest.fail`` rather than
    ``assert``: no introspection repr of the problem list is added."""
    if problems:
        pytest.fail(header + ":\n  " + "\n  ".join(problems), pytrace=False)


class _StreamSources:
    """Creates the stage's Image_Sources and guarantees they are deleted.

    Each source is recorded in the session ``state_registry`` right after
    the device returns its id, with pre-state ``None`` (it did not exist),
    so restoration deletes it at teardown when its check did not. A check
    deletes its source inline when it finishes; restoration then has
    nothing left to do for it.
    """

    def __init__(self, client, registry):
        self._client = client
        self._registry = registry
        self._token = token_hex(4)
        self._count = 0
        self._live = set()

    def create(
        self, url: str, purpose: str, credentials: Optional[StreamCredentials] = None
    ) -> str:
        self._count += 1
        name = f"{SOURCE_NAME_PREFIX}-{self._token}-{self._count:02d}-{purpose}"
        body: Dict[str, Any] = {
            "type": stream_source_type(url),
            "name": name,
            "description": SOURCE_DESCRIPTION,
            "location": url,
            "streamSettings": dict(STREAM_SETTINGS),
        }
        if credentials is not None:
            body["credentials"] = credentials.request_body()
        source_id = self._client.create_image_source(body)
        self._live.add(source_id)
        self._registry.record(
            "image_source", f"{source_id} ({name})", None, lambda: self._restore(source_id)
        )
        return source_id

    def delete(self, source_id: str) -> None:
        self._client.delete_image_source(source_id)
        self._live.discard(source_id)

    def _restore(self, source_id: str) -> None:
        if source_id in self._live:
            self.delete(source_id)


def _delete_now(stream_sources: _StreamSources, source_id: str, problems: List[str]) -> None:
    """Delete a source inline; a failure is a problem of the check, and
    restoration retries the delete at teardown."""
    try:
        stream_sources.delete(source_id)
    except (DeviceApiError, requests.exceptions.RequestException) as err:
        problems.append(
            f"DELETE /image-sources/{source_id} failed (restoration retries it "
            f"at teardown): {err}"
        )


def _credential_leaks(
    document: Any, secret_values: Iterable[str], skip_keys: Iterable[str] = ()
) -> List[str]:
    """Where ``document`` carries credential material: the JSON path of each
    key or string value containing one of ``secret_values`` or a URL with
    user information. Findings name paths only, never values; a key that
    itself carries a secret is shown as ``<redacted key>``. ``skip_keys``
    are top-level keys left out (a base64 image is not text)."""
    values = [str(value) for value in secret_values if value]
    skip = set(skip_keys)
    findings: List[str] = []

    def kinds(text: str) -> List[str]:
        found = []
        if any(value in text for value in values):
            found.append("a credential value")
        if _URL_USER_INFO.search(text):
            found.append("URL user information")
        return found

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                key_text = str(key)
                if not path and key_text in skip:
                    continue
                key_kinds = kinds(key_text)
                shown = "<redacted key>" if key_kinds else key_text
                child_path = f"{path}.{shown}" if path else shown
                findings.extend(f"{child_path} (key): {kind}" for kind in key_kinds)
                visit(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{path}[{index}]")
        elif isinstance(node, str):
            findings.extend(f"{path or '<document>'}: {kind}" for kind in kinds(node))

    visit(document, "")
    return findings


def _preview(edge_client, source_id: str) -> Tuple[Optional[str], Optional[int]]:
    """``(problem, decoded image bytes)`` of the source's preview, retrying
    a 503 (no frame yet) within :data:`PREVIEW_RETRY_WINDOW_S`."""
    deadline = time.monotonic() + PREVIEW_RETRY_WINDOW_S
    while True:
        try:
            preview = edge_client.preview_image_source(source_id)
            break
        except DeviceApiError as err:
            if err.status != 503 or time.monotonic() >= deadline:
                return f"preview failed: {err}", None
            time.sleep(PREVIEW_RETRY_INTERVAL_S)
    image = preview.get("image") if isinstance(preview, dict) else None
    if not isinstance(image, str) or not image:
        shape = sorted(preview) if isinstance(preview, dict) else type(preview).__name__
        return f"preview answered without an image ({shape})", None
    try:
        data = base64.b64decode(image)
    except (binascii.Error, ValueError):
        return "preview image is not valid base64", None
    if not data:
        return "preview image is empty", None
    return None, len(data)


def _health_metrics(*documents: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The recorded Stream_Health fields: from the first document that
    reports each one (the later stream-health read wins over the
    connection test's snapshot, whose source rate may not be measured yet)."""
    reports = [doc for doc in documents if isinstance(doc, dict)]
    return {
        key: next((doc[key] for doc in reports if doc.get(key) is not None), None)
        for key in HEALTH_METRIC_KEYS
    }


def _version_key(version: Any) -> Tuple:
    """Numeric-aware ordering of registration versions (``"10"`` after
    ``"9"``, ``"1.10"`` after ``"1.9"``)."""
    parts = re.split(r"[.\-_]", str(version if version is not None else ""))
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in parts)


def _registration_for(edge_client, workflow_id: str, key: str) -> Dict[str, Any]:
    """The runnable (``registered``) registration of ``workflow_id`` with
    the highest version; fails naming what the device reports otherwise."""
    registrations = edge_client.workflow_registrations()
    matching = [entry for entry in registrations if entry.get("workflowId") == workflow_id]
    runnable = [entry for entry in matching if entry.get("status") == REGISTERED]
    if runnable:
        return max(runnable, key=lambda entry: _version_key(entry.get("version")))
    if matching:
        found = [
            {k: entry.get(k) for k in ("registrationId", "version", "status", "invalidReason")}
            for entry in matching
        ]
        pytest.fail(
            f"expected.{key} {workflow_id!r} has no registered (runnable) "
            f"registration on the device; its registrations: {found}",
            pytrace=False,
        )
    reported = sorted({str(entry.get("workflowId")) for entry in registrations})
    pytest.fail(
        f"expected.{key} {workflow_id!r} is not deployed on the device; "
        f"deployed workflowIds: {reported}",
        pytrace=False,
    )


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _poll_continuous(
    edge_client, registration_id: str, done: Callable[[Dict[str, Any]], bool], timeout_s: float
) -> Dict[str, Any]:
    """Poll the continuous status until ``done(status)`` or ``timeout_s``;
    returns the last status."""
    deadline = time.monotonic() + timeout_s
    while True:
        status = edge_client.continuous_status(registration_id)
        remaining = deadline - time.monotonic()
        if done(status) or remaining <= 0:
            return status
        time.sleep(min(CONTINUOUS_POLL_INTERVAL_S, remaining))


def _settled_status(edge_client, registration_id: str, timeout_s: float) -> Tuple[Dict, bool]:
    """The continuous status once no run is in progress and the counters
    held still over one poll interval (a finishing run is counted just
    after it stops being in progress); ``(last status, False)`` on timeout."""
    deadline = time.monotonic() + timeout_s
    previous: Optional[Dict[str, Any]] = None
    while True:
        status = edge_client.continuous_status(registration_id)
        if (
            previous is not None
            and not previous.get("runInProgress")
            and not status.get("runInProgress")
            and status.get("counters") == previous.get("counters")
        ):
            return status, True
        if time.monotonic() >= deadline:
            return status, False
        previous = status
        time.sleep(CONTINUOUS_POLL_INTERVAL_S)


def _counter_deltas(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Per-counter increase between two continuous status documents."""
    old = before.get("counters") or {}
    new = after.get("counters") or {}
    return {
        key: new[key] - old[key]
        for key in sorted(set(old) | set(new))
        if isinstance(new.get(key), (int, float)) and isinstance(old.get(key), (int, float))
    }


def _pause_window_s(status: Dict[str, Any], cap_s: float) -> float:
    """Two runs' worth at the configured rate, at least
    :data:`PAUSE_WINDOW_MIN_S`, at most ``cap_s``."""
    window = PAUSE_WINDOW_MIN_S
    fps = status.get("configuredFps")
    if isinstance(fps, (int, float)) and fps > 0:
        window = max(window, 2.0 / fps)
    return min(window, cap_s)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stream_capabilities(stream_cameras_surface, device_identity) -> Dict[str, Any]:
    """The Device_Stream_Capabilities from the session probe, which fails
    the stage with ``CapabilityMismatchError`` when the granted capability
    is absent. Depending on ``device_identity`` keeps the health stage
    first."""
    return stream_cameras_surface


@pytest.fixture(scope="module")
def stream_sources(edge_client, state_registry, stream_capabilities) -> _StreamSources:
    """The stage's Image_Source factory, restoration-backed."""
    return _StreamSources(edge_client, state_registry)


@pytest.fixture(scope="module")
def continuous_registration(harness_target, edge_client, stream_capabilities) -> Dict[str, Any]:
    """The registration of ``expected.continuous_workflow``, found not
    paused. An operator's pause is left alone: the checks skip instead."""
    _require(harness_target, "continuous_workflow")
    workflow_id = harness_target.expected.continuous_workflow
    registration = _registration_for(edge_client, workflow_id, "continuous_workflow")
    registration_id = registration.get("registrationId")
    try:
        status = edge_client.continuous_status(registration_id)
    except DeviceApiError as err:
        if err.status == 404:
            pytest.fail(
                f"expected.continuous_workflow {workflow_id!r} (registration "
                f"{registration_id}) reports no continuous status, so it is not a "
                f"continuous stream workflow: {err.body_excerpt}",
                pytrace=False,
            )
        raise
    if status.get("state") == CONTINUOUS_PAUSED:
        pytest.skip(
            f"continuous workflow {workflow_id!r} (registration {registration_id}) "
            f"was found paused on device {harness_target.name} (pausedAtMs="
            f"{status.get('pausedAtMs')}); the harness never resumes a pause it "
            "did not make, so resume it on the device to run these checks"
        )
    return registration


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def test_stream_capabilities_report_ingest_and_decoders(
    harness_target, stream_capabilities, record_metric
):
    """RTSP and RTMP ingest and H.264/H.265 software decoding are available;
    each codec's hardware decoder (element name, or None) and the media
    stack versions are recorded (Requirement 16.5)."""
    capabilities = stream_capabilities
    problems = []
    for protocol in ("rtsp", "rtmp"):
        if capabilities.get(protocol) is not True:
            problems.append(
                f"{protocol} ingest is unavailable ({protocol}={capabilities.get(protocol)!r})"
            )
    codecs = capabilities.get("codecs") if isinstance(capabilities.get("codecs"), dict) else {}
    for codec in ("h264", "h265"):
        entry = codecs.get(codec) if isinstance(codecs.get(codec), dict) else {}
        record_metric(f"stream_{codec}_hardware_decoder", entry.get("hardware"))
        record_metric(f"stream_{codec}_software_decoder", entry.get("software"))
        if not entry.get("software"):
            problems.append(f"no {codec} software decoder (codecs.{codec}={entry!r})")
    for component in ("pyav", "ffmpeg", "gstreamer"):
        record_metric(f"stream_{component}_version", capabilities.get(component))
    record_metric(
        "stream_tls", {key: capabilities.get(key) for key in ("tls", "rtspTls", "rtmpTls")}
    )
    if capabilities.get("probeError"):
        problems.append(f"probeError: {capabilities.get('probeError')!r}")
    _fail_with(f"stream capabilities of device {harness_target.name}", problems)


def _check_stream_url(edge_client, stream_sources, url: str) -> Tuple[List[str], Dict[str, Any]]:
    """Create, connect, preview and read the health of one stream URL,
    then delete its source: ``(problems, metrics)``."""
    problems: List[str] = []
    metrics: Dict[str, Any] = {}
    source_id = None
    try:
        source_id = stream_sources.create(url, "connect")
        started = time.monotonic()
        result = edge_client.connection_test(source_id)
        metrics["connectionTestS"] = round(time.monotonic() - started, 3)
        metrics["ok"] = result.get("ok")
        metrics["category"] = result.get("category")
        if result.get("ok") is not True:
            problems.append(
                f"test-connection answered ok={result.get('ok')!r}, category="
                f"{result.get('category')!r}: {result.get('message')!r}"
            )
        else:
            problem, preview_bytes = _preview(edge_client, source_id)
            metrics["previewBytes"] = preview_bytes
            if problem:
                problems.append(problem)
            health = edge_client.stream_health(source_id)
            if health.get("state") != STREAMING:
                problems.append(
                    f"stream-health state is {health.get('state')!r}, expected "
                    f"{STREAMING!r} (lastError: {health.get('lastError')!r})"
                )
            metrics.update(_health_metrics(health, result.get("streamHealth")))
    except (DeviceApiError, requests.exceptions.RequestException) as err:
        problems.append(str(err))
    finally:
        if source_id is not None:
            _delete_now(stream_sources, source_id, problems)
    return problems, metrics


def test_stream_urls_connect_preview_and_stream(
    harness_target, edge_client, stream_capabilities, stream_sources, record_metric
):
    """Each ``expected.stream_urls`` entry, as an Image_Source with the
    ``auto`` decoder policy, passes the connection test, previews an image,
    and reports ``streaming``; its codec, resolution, source rate and
    decoder are recorded (Requirements 4.1, 4.3, 4.4, 7.4, 8.8). Every
    failing URL is reported in one message."""
    _require(harness_target, "stream_urls")
    urls = harness_target.expected.stream_urls
    failures = []
    for url in urls:
        problems, metrics = _check_stream_url(edge_client, stream_sources, url)
        record_metric(f"stream_source[{url}]", metrics)
        if problems:
            failures.append(f"{url}: " + "; ".join(problems))
    _fail_with(
        f"{len(failures)} of {len(urls)} stream URL(s) failed on device {harness_target.name}",
        failures,
    )


def test_credentialed_source_connects_without_leaking_credentials(
    harness_target, edge_client, stream_capabilities, stream_sources
):
    """A source created with ``expected.stream_credentials`` connects; its
    connection test, GET and stream-health responses carry neither the
    password nor URL user information; after a wrong password is PATCHed
    the connection test fails with ``authentication_failed``
    (Requirements 4.3, 6.1). Deleting the source restores the device."""
    _require(harness_target, "stream_secure_url", "stream_credentials")
    url = harness_target.expected.stream_secure_url
    try:
        credentials = resolve_stream_credentials(harness_target.expected.stream_credentials)
    except HarnessConfigError as err:
        pytest.fail(
            f"device {harness_target.name}: cannot resolve expected.stream_credentials: {err}",
            pytrace=False,
        )
    wrong = StreamCredentials(
        username=credentials.username, password=SecretStr(f"harness-wrong-{token_hex(8)}")
    )
    secret_values = (credentials.password, wrong.password)
    problems: List[str] = []

    def scrub(text: Any) -> str:
        return redact_secrets(str(text), secret_values)

    def check_leaks(label: str, document: Any, skip_keys: Iterable[str] = ()) -> None:
        for finding in _credential_leaks(document, secret_values, skip_keys):
            problems.append(f"{label} carries credential material at {finding}")

    source_id = None
    try:
        source_id = stream_sources.create(url, "secure", credentials=credentials)
        result = edge_client.connection_test(source_id)
        check_leaks("the connection test (configured credentials)", result, skip_keys=("image",))
        if result.get("ok") is not True:
            problems.append(
                f"with the configured credentials, test-connection answered ok="
                f"{result.get('ok')!r}, category={scrub(result.get('category'))!r}: "
                f"{scrub(result.get('message'))}"
            )
        document = edge_client.image_source(source_id)
        check_leaks(f"GET /image-sources/{source_id}", document)
        if document.get("credentialsConfigured") is not True:
            problems.append(
                f"GET /image-sources/{source_id} reports credentialsConfigured="
                f"{document.get('credentialsConfigured')!r}, expected True"
            )
        health = edge_client.stream_health(source_id)
        check_leaks(f"GET /image-sources/{source_id}/stream-health", health)
        if health.get("credentialsConfigured") is not True:
            problems.append(
                f"stream-health reports credentialsConfigured="
                f"{health.get('credentialsConfigured')!r}, expected True"
            )

        edge_client.update_image_source(source_id, {"credentials": wrong.request_body()})
        result = edge_client.connection_test(source_id)
        check_leaks("the connection test (wrong password)", result, skip_keys=("image",))
        if result.get("ok") is not False or result.get("category") != AUTHENTICATION_FAILED:
            state = (result.get("streamHealth") or {}).get("state")
            problems.append(
                f"after PATCHing a wrong password, test-connection answered ok="
                f"{result.get('ok')!r}, category={scrub(result.get('category'))!r} "
                f"(expected ok=False, category {AUTHENTICATION_FAILED!r}); stream "
                f"state {scrub(state)!r}: {scrub(result.get('message'))}"
            )
    except (DeviceApiError, requests.exceptions.RequestException) as err:
        problems.append(scrub(err))
    finally:
        if source_id is not None:
            _delete_now(stream_sources, source_id, problems)
    _fail_with(
        scrub(f"credentialed stream source {url} on device {harness_target.name}"),
        [scrub(problem) for problem in problems],
    )


def test_connection_failures_report_expected_categories(
    harness_target, edge_client, stream_capabilities, stream_sources, record_metric
):
    """Each ``expected.stream_failures`` URL's connection test answers
    ``ok: false`` with exactly the configured category (Requirement 4.3).
    Every mismatch is reported in one message."""
    _require(harness_target, "stream_failures")
    failures = harness_target.expected.stream_failures
    mismatches = []
    for category, url in failures:
        problems: List[str] = []
        source_id = None
        try:
            source_id = stream_sources.create(url, f"fail-{category}")
            started = time.monotonic()
            result = edge_client.connection_test(source_id)
            record_metric(
                f"stream_failure[{url}]",
                {
                    "expected": category,
                    "ok": result.get("ok"),
                    "category": result.get("category"),
                    "connectionTestS": round(time.monotonic() - started, 3),
                },
            )
            if result.get("ok") is not False or result.get("category") != category:
                problems.append(
                    f"expected ok=False with category {category!r}, got ok="
                    f"{result.get('ok')!r} with category {result.get('category')!r}: "
                    f"{result.get('message')!r}"
                )
        except (DeviceApiError, requests.exceptions.RequestException) as err:
            problems.append(str(err))
        finally:
            if source_id is not None:
                _delete_now(stream_sources, source_id, problems)
        if problems:
            mismatches.append(f"{url}: " + "; ".join(problems))
    _fail_with(
        f"{len(mismatches)} of {len(failures)} connection failure check(s) did not "
        f"match on device {harness_target.name}",
        mismatches,
    )


def test_stream_workflow_trigger_records_the_frame(
    harness_target, edge_client, stream_capabilities, state_registry, record_metric
):
    """A triggered run of ``expected.stream_workflow`` (its highest
    registered version) completes within ``timeouts.workflow_output_s`` and
    its metadata records the stream frame it analyzed, with ``seq`` and
    ``acquiredAtMs`` (Requirement 10.4)."""
    _require(harness_target, "stream_workflow")
    workflow_id = harness_target.expected.stream_workflow
    registration = _registration_for(edge_client, workflow_id, "stream_workflow")
    registration_id = registration.get("registrationId")
    # A trigger is a one-shot run of a registration the device already
    # serves: restoration must leave it untouched (found-running entry).
    state_registry.record("workflow", registration_id, "RUNNING", lambda: None)
    started = time.monotonic()
    try:
        execution = edge_client.trigger_registration(registration_id)
    except DeviceApiError as err:
        if err.status == 409 and CONTINUOUS_WORKFLOW_RUNNING in err.body_excerpt:
            pytest.fail(
                f"expected.stream_workflow {workflow_id!r} runs continuously; name an "
                "on_trigger stream workflow here and the continuous one as "
                "expected.continuous_workflow",
                pytrace=False,
            )
        raise
    execution_id = execution.get("executionId")
    assert execution_id, (
        f"trigger of registration {registration_id} answered without an "
        f"executionId: {execution!r}"
    )
    finished = edge_client.wait_for_execution(
        execution_id, timeout_s=harness_target.timeouts.workflow_output_s
    )
    record_metric("stream_workflow_run_s", round(time.monotonic() - started, 3))
    assert finished.get("status") == EXECUTION_COMPLETED, (
        f"run {execution_id} of stream workflow {workflow_id!r} (registration "
        f"{registration_id}) ended {finished.get('status')!r}: failing node "
        f"{finished.get('failingNodeId')!r}, error {finished.get('error')!r}"
    )
    metadata = edge_client.workflow_execution_metadata(execution_id)
    stream = metadata.get("stream") if isinstance(metadata, dict) else None
    frames = {
        node_id: entry
        for node_id, entry in (stream.items() if isinstance(stream, dict) else ())
        if isinstance(entry, dict)
        and _positive_int(entry.get("seq"))
        and _positive_int(entry.get("acquiredAtMs"))
    }
    record_metric("stream_workflow_frame", frames)
    assert frames, (
        f"run {execution_id} of stream workflow {workflow_id!r} completed, but its "
        f"metadata has no stream entry with seq and acquiredAtMs (stream: {stream!r}; "
        f"metadata keys: {sorted(metadata) if isinstance(metadata, dict) else metadata!r})"
    )


def test_continuous_workflow_runs_at_its_rate(
    harness_target, edge_client, continuous_registration, record_metric
):
    """The continuous workflow is ``running`` and, over
    ``timeouts.continuous_window_s``, keeps a non-zero effective rate while
    its completed counter rises (Requirements 11.1, 12.5)."""
    registration_id = continuous_registration.get("registrationId")
    window_s = harness_target.timeouts.continuous_window_s
    first = _poll_continuous(
        edge_client,
        registration_id,
        lambda status: status.get("state") == CONTINUOUS_RUNNING,
        window_s,
    )
    assert first.get("state") == CONTINUOUS_RUNNING, (
        f"continuous workflow {harness_target.expected.continuous_workflow!r} "
        f"(registration {registration_id}) is {first.get('state')!r}, not "
        f"{CONTINUOUS_RUNNING!r}, after {window_s:.0f}s; stream health: "
        f"{first.get('streamHealth')!r}"
    )
    time.sleep(window_s)
    second = edge_client.continuous_status(registration_id)
    deltas = _counter_deltas(first, second)
    effective_fps = second.get("effectiveFps")
    record_metric(
        "continuous_workflow_window",
        {
            "windowS": window_s,
            "configuredFps": second.get("configuredFps"),
            "effectiveFps": effective_fps,
            "counterDeltas": deltas,
        },
    )
    problems = []
    if second.get("state") != CONTINUOUS_RUNNING:
        problems.append(
            f"state is {second.get('state')!r} after the window (stream health: "
            f"{second.get('streamHealth')!r})"
        )
    if not isinstance(effective_fps, (int, float)) or effective_fps <= 0:
        problems.append(f"effectiveFps is {effective_fps!r}, expected > 0")
    if deltas.get("completed", 0) <= 0:
        problems.append(f"counters.completed did not increase (counter deltas: {deltas})")
    _fail_with(
        f"continuous workflow {harness_target.expected.continuous_workflow!r} "
        f"(registration {registration_id}) over {window_s:.0f}s",
        problems,
    )


def test_continuous_workflow_pauses_and_resumes(
    harness_target, edge_client, state_registry, continuous_registration, record_metric
):
    """A pause reports ``paused`` and no run starts or completes while it
    lasts; a resume reports ``running`` again (Requirement 11.6). The
    resume is recorded for State_Restoration before the pause is issued."""
    registration_id = continuous_registration.get("registrationId")
    workflow_id = harness_target.expected.continuous_workflow
    timeouts = harness_target.timeouts
    pause = {"active": False}

    def undo_pause() -> None:
        if pause["active"]:
            edge_client.resume_continuous(registration_id)
            pause["active"] = False

    state_registry.record("continuous_pause", registration_id, None, undo_pause)
    # Marked before the call: a pause that lands but whose answer is lost
    # (timeout) is still undone at teardown; resuming a running workflow is
    # a no-op on the device.
    pause["active"] = True
    paused = edge_client.pause_continuous(registration_id)
    problems = []
    if paused.get("state") != CONTINUOUS_PAUSED:
        problems.append(
            f"the pause answered state {paused.get('state')!r}, expected {CONTINUOUS_PAUSED!r}"
        )
    settled, is_settled = _settled_status(edge_client, registration_id, timeouts.workflow_output_s)
    if not is_settled:
        problems.append(
            f"the status did not settle within {timeouts.workflow_output_s:.0f}s of the "
            "pause (timeouts.workflow_output_s): a run stayed in progress or the "
            f"counters kept changing: {settled!r}"
        )
    window_s = _pause_window_s(settled, timeouts.continuous_window_s)
    time.sleep(window_s)
    after = edge_client.continuous_status(registration_id)
    deltas = _counter_deltas(settled, after)
    record_metric("continuous_pause_window", {"windowS": window_s, "counterDeltas": deltas})
    if after.get("state") != CONTINUOUS_PAUSED:
        problems.append(
            f"state is {after.get('state')!r} while paused, expected {CONTINUOUS_PAUSED!r}"
        )
    if deltas.get("started", 0) or deltas.get("completed", 0):
        problems.append(
            f"runs went on while paused over {window_s:.0f}s (counter deltas: {deltas})"
        )

    edge_client.resume_continuous(registration_id)
    pause["active"] = False
    running = _poll_continuous(
        edge_client,
        registration_id,
        lambda status: status.get("state") == CONTINUOUS_RUNNING,
        timeouts.continuous_window_s,
    )
    if running.get("state") != CONTINUOUS_RUNNING:
        problems.append(
            f"after the resume the state is {running.get('state')!r}, expected "
            f"{CONTINUOUS_RUNNING!r} (stream health: {running.get('streamHealth')!r})"
        )
    _fail_with(
        f"continuous workflow {workflow_id!r} (registration {registration_id}) pause and resume",
        problems,
    )
