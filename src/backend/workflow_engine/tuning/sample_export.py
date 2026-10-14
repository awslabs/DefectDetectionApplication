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

"""Sample_Export: the device's tuning sample side channel (Requirement 2).

For every Anomaly_Mode invocation that returned an answer, the executor
hands :meth:`SampleExporter.enqueue` the EXACT image bytes it sent plus
the recorded answer and context; a background worker uploads three
objects per sample to the Use_Case's Sample_Store::

    {prefix}{workflowId}/{nodeId}/{thingName}/{executionId}.json
    {prefix}{workflowId}/{nodeId}/{thingName}/{executionId}.input.jpg
    {prefix}{workflowId}/{nodeId}/{thingName}/{executionId}.reference.jpg

so the Portal's tuning workspace works from what the model really saw
without anyone logging into a device (Requirements 2.2, 2.3).

Design constraints this module implements literally:

- **Inert unless configured** (Requirements 2.6, 11.3). The
  ``workflowTuning`` LocalServer component configuration is parsed by
  :meth:`ExportConfig.from_component_configuration`; an absent,
  non-object, disabled, bucket-less or non-``/``-terminated
  configuration yields ``None``, :func:`configure_sample_exporter` then
  registers NO exporter, and the processors' export call sites return
  immediately — no queue is allocated, no S3 client is constructed and
  no S3 request is issued.
- **Never in the run's way** (Requirements 2.4, 11.1, 11.2).
  :meth:`SampleExporter.enqueue` only appends to a bounded in-memory
  deque under a short-lived lock and returns; uploading, hashing and
  JSON serialization all happen on the worker thread. Every export call
  site is wrapped in a bare ``except`` logged at debug, so an exporter
  defect can never change an Execution's outcome, artifacts or
  Run_Metadata.
- **Bounded** (Requirements 2.4, 2.5, 2.10). At most
  :data:`DEFAULT_QUEUE_SIZE` entries are queued (the oldest is dropped
  with a WARNING naming its execution), a sample whose input or
  reference image exceeds :data:`MAX_IMAGE_BYTES` is skipped with an
  INFO naming its execution, and each sample is attempted at most
  :data:`MAX_UPLOAD_ATTEMPTS` times with backoff before an ERROR naming
  the object key.
- **No image bytes in the sidecar** (Requirement 2.3): the JSON object
  carries only the images' keys, sizes and SHA-256 digests. The images
  are uploaded BEFORE the sidecar, so an indexer never sees a sidecar
  whose images are missing.

Stdlib only at import time; ``boto3`` is imported lazily by the default
client factory, which is injectable so tests never need it.
"""

import base64
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

from workflow_engine.llm_inference import PLACEHOLDER_RE
from workflow_engine.vendor.workflow_core.anomaly_invocation import (
    prompt_fingerprint,
)

logger = logging.getLogger(__name__)

#: LocalServer component configuration key carrying the Sample_Export
#: location and enablement flag (Requirement 2.1).
CONFIG_KEY = "workflowTuning"

#: Bounded export queue depth (Requirement 2.4).
DEFAULT_QUEUE_SIZE = 200

#: Per-image size cap; a larger input or reference skips the sample
#: (Requirement 2.10).
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: Upload attempts per sample before the failure is logged with the key
#: (Requirement 2.5).
MAX_UPLOAD_ATTEMPTS = 3

#: First retry delay; each further attempt doubles it (Requirement 2.5).
BACKOFF_BASE_SECONDS = 1.0

#: Sidecar schema version (design "Sample_Export objects").
SIDECAR_SCHEMA_VERSION = 1

#: Sample sources: an invocation observed live, or one reconstructed
#: from existing Run_Artifacts by the one-shot backfill (Requirement
#: 2.7).
SOURCE_LIVE = "live"
SOURCE_BACKFILL = "backfill"

#: Object suffixes of one exported sample.
SIDECAR_SUFFIX = ".json"
INPUT_SUFFIX = ".input.jpg"
REFERENCE_SUFFIX = ".reference.jpg"

#: Content types of the uploaded objects.
SIDECAR_CONTENT_TYPE = "application/json"
IMAGE_CONTENT_TYPE = "image/jpeg"

#: Serialized size cap of an ``llm_inference`` sample's
#: ``metadataSnippet`` (the Run_Metadata fragment a Device_Score_Job
#: needs to re-render the node's Prompt_Template). Large entries are
#: dropped largest-first until the snippet fits; the shadow/manifest
#: path never carries images, only this text.
METADATA_SNIPPET_MAX_BYTES = 64 * 1024

#: Environment variable naming the device (the Greengrass thing name).
THING_NAME_ENV = "AWS_IOT_THING_NAME"


# ---------------------------------------------------------------------------
# Configuration (Requirements 2.1, 2.6, 11.3 — Property 17's device half)
# ---------------------------------------------------------------------------

def _parse_enabled(value: Any) -> bool:
    """Parse the ``enabled`` flag the fail-safe way.

    Enabled ONLY for the boolean ``True`` or the string ``"true"``
    (case-insensitive, surrounding whitespace ignored) — Greengrass
    ``DefaultConfiguration`` values arrive as strings, so both forms
    occur. Everything else (absent, ``None``, ``"false"``, numbers,
    lists) is disabled, exactly like ``LocalLoginEnabled``.
    """
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


@dataclass(frozen=True)
class ExportConfig:
    """Where exported samples go: the Use_Case bucket and the Sample_Store
    prefix, both delivered as LocalServer component configuration."""

    bucket: str
    prefix: str

    @classmethod
    def from_component_configuration(
        cls, configuration: Any
    ) -> Optional["ExportConfig"]:
        """Parse the ``workflowTuning`` section of the LocalServer
        component configuration, or ``None`` when export is disabled.

        ``None`` (export disabled, no queue, no client, no request) for
        every malformed shape: a non-mapping configuration, an absent or
        non-mapping ``workflowTuning`` section, ``enabled`` not
        explicitly true, a missing/blank/non-string ``bucket``, and a
        ``prefix`` that is missing, blank or does not end in ``/``
        (Requirement 2.6, Property 17).
        """
        if not isinstance(configuration, Mapping):
            return None
        return cls.from_section(configuration.get(CONFIG_KEY))

    @classmethod
    def from_section(cls, section: Any) -> Optional["ExportConfig"]:
        """Parse the ``workflowTuning`` value itself (see
        :meth:`from_component_configuration`)."""
        if not isinstance(section, Mapping):
            return None
        if not _parse_enabled(section.get("enabled")):
            return None
        bucket = section.get("bucket")
        if not isinstance(bucket, str) or not bucket.strip():
            return None
        prefix = section.get("prefix")
        if not isinstance(prefix, str) or not prefix or not prefix.endswith(
            "/"
        ):
            return None
        return cls(bucket=bucket.strip(), prefix=prefix)


@dataclass(frozen=True)
class ExportContext:
    """The run identity an exported sample is attributed to.

    Built once per Execution by the pipeline executor and threaded into
    the Bedrock / LLM processors, which know their node and its images
    but not which run they belong to. ``None`` anywhere on the path
    means no export (the pre-feature call shape).
    """

    workflow_id: str = ""
    version: Any = None
    execution_id: str = ""


@dataclass(frozen=True)
class ExportedSample:
    """One Anomaly_Mode invocation, as exported (Requirement 2.2).

    ``input_bytes`` / ``reference_bytes`` are the EXACT bytes the
    invocation sent — the Detection_Crop or captured ``in`` frame, and
    the captured/payload-resolved reference, downscaled when the node
    downscales — so a replay sees what the model saw.
    """

    workflow_id: str
    node_id: Optional[str]
    node_type: str
    execution_id: str
    input_bytes: bytes
    version: Any = None
    reference_bytes: Optional[bytes] = None
    answer: Optional[str] = None
    verdict: Optional[Mapping[str, Any]] = None
    parse_error: Optional[str] = None
    prompt_fingerprint: Optional[str] = None
    detection_id: Optional[str] = None
    detection_slot: Optional[int] = None
    metadata_snippet: Optional[Mapping[str, Any]] = None
    input_sha256: Optional[str] = None
    source: str = SOURCE_LIVE
    exported_at: Optional[int] = None

    @property
    def oversize(self) -> bool:
        """True when either image exceeds the 8 MiB cap (Requirement
        2.10)."""
        if len(self.input_bytes or b"") > MAX_IMAGE_BYTES:
            return True
        return len(self.reference_bytes or b"") > MAX_IMAGE_BYTES


# ---------------------------------------------------------------------------
# Object layout and the sidecar document (Requirement 2.3)
# ---------------------------------------------------------------------------

def object_key_base(
    prefix: str, sample: ExportedSample, thing_name: str
) -> str:
    """``{prefix}{workflowId}/{nodeId}/{thingName}/{executionId}`` — the
    common stem of a sample's three objects.

    Identifiers are used verbatim: the Portal indexes a node's samples by
    listing ``{prefix}{workflowId}/{nodeId}/``, so any rewriting here
    would break that mapping.
    """
    return "{0}{1}/{2}/{3}/{4}".format(
        prefix,
        sample.workflow_id,
        sample.node_id,
        thing_name,
        sample.execution_id,
    )


def object_keys(
    prefix: str, sample: ExportedSample, thing_name: str
) -> Tuple[str, str, Optional[str]]:
    """``(sidecar_key, input_key, reference_key)``; the reference key is
    ``None`` for a single-image sample."""
    base = object_key_base(prefix, sample, thing_name)
    reference_key = (
        base + REFERENCE_SUFFIX if sample.reference_bytes is not None else None
    )
    return base + SIDECAR_SUFFIX, base + INPUT_SUFFIX, reference_key


def _normalize_version(version: Any) -> Any:
    """The registration version as the sidecar records it.

    The device stores versions as strings; the Portal's sidecar shape
    shows a number. An all-digit version becomes an ``int`` so both
    agree, anything else is kept verbatim as a string (``None`` stays
    ``None``).
    """
    if version is None:
        return None
    if isinstance(version, bool):
        return str(version)
    if isinstance(version, (int, float)):
        return version
    text = str(version).strip()
    if text.isdigit():
        return int(text)
    return text or None


def _digest(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    return hashlib.sha256(data).hexdigest()


def build_sidecar(
    sample: ExportedSample,
    thing_name: str,
    prefix: str,
    exported_at: Optional[int] = None,
) -> Dict[str, Any]:
    """The sample's JSON sidecar — identifiers, image references and the
    recorded answer, and NEVER image bytes (Requirement 2.3).

    Pure: the digests and sizes are computed from the sample's own bytes,
    so the document is a function of the sample, the device name and the
    Sample_Store prefix.
    """
    sidecar_key, input_key, reference_key = object_keys(
        prefix, sample, thing_name)
    # The sidecar never references itself.
    del sidecar_key
    verdict = sample.verdict if isinstance(sample.verdict, Mapping) else None
    document: Dict[str, Any] = {
        "schemaVersion": SIDECAR_SCHEMA_VERSION,
        "source": sample.source,
        "workflowId": sample.workflow_id,
        "version": _normalize_version(sample.version),
        "executionId": sample.execution_id,
        "nodeId": sample.node_id,
        "nodeType": sample.node_type,
        "thingName": thing_name,
        "exportedAt": int(
            exported_at
            if exported_at is not None
            else (sample.exported_at
                  if sample.exported_at is not None else time.time())
        ),
        "input": {
            "key": input_key,
            "sha256": sample.input_sha256 or _digest(sample.input_bytes),
            "bytes": len(sample.input_bytes or b""),
        },
        "recorded": {
            "isAnomalous": (
                bool(verdict["is_anomalous"])
                if verdict is not None and "is_anomalous" in verdict
                else None
            ),
            "confidence": (
                verdict.get("confidence") if verdict is not None else None
            ),
            "answer": sample.answer,
            "parseError": sample.parse_error,
        },
        "promptFingerprint": sample.prompt_fingerprint,
        "detectionId": sample.detection_id,
        "detectionSlot": sample.detection_slot,
    }
    if reference_key is not None:
        document["reference"] = {
            "key": reference_key,
            "sha256": _digest(sample.reference_bytes),
            "bytes": len(sample.reference_bytes or b""),
        }
    if sample.metadata_snippet is not None:
        document["metadataSnippet"] = dict(sample.metadata_snippet)
    return document


# ---------------------------------------------------------------------------
# The exporter
# ---------------------------------------------------------------------------

def _default_s3_factory():
    """The device's S3 client, constructed on the first upload attempt.

    ``boto3`` is imported here (never at module import) so this module
    stays importable in every environment and so a disabled exporter
    constructs no client at all (Requirement 2.6).
    """
    import boto3

    return boto3.client("s3")


class SampleExporter:
    """Uploads exported samples on a bounded background worker.

    ``config`` of ``None`` builds an INERT exporter: no queue, no thread,
    no client, and :meth:`enqueue` is a no-op — the shape a device
    without tuning sample export runs with (Requirement 2.6).
    """

    def __init__(
        self,
        config: Optional[ExportConfig],
        s3_factory: Optional[Callable[[], Any]] = None,
        thing_name: Optional[str] = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        max_attempts: int = MAX_UPLOAD_ATTEMPTS,
        backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._s3_factory = s3_factory
        self._thing_name = (
            thing_name
            if thing_name is not None
            else os.environ.get(THING_NAME_ENV, "")
        )
        self._queue_size = max(1, int(queue_size))
        self._max_attempts = max(1, int(max_attempts))
        self._backoff_base_seconds = float(backoff_base_seconds)
        self._sleep = sleep
        self._clock = clock
        self._client: Any = None
        self._worker: Optional[threading.Thread] = None
        self._stopping = False
        self._in_flight = 0
        self._condition = threading.Condition()
        #: Allocated ONLY for a configured exporter, so a disabled one
        #: holds no queue at all (Property 3).
        self._queue: Optional[Deque[ExportedSample]] = (
            deque() if config is not None else None
        )

    # -- state -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._queue is not None

    @property
    def config(self) -> Optional[ExportConfig]:
        return self._config

    @property
    def thing_name(self) -> str:
        return self._thing_name

    @property
    def queue_size(self) -> int:
        """The queue's bound (entries; the oldest is dropped beyond it)."""
        return self._queue_size

    @property
    def queue_depth(self) -> int:
        with self._condition:
            return len(self._queue) if self._queue is not None else 0

    # -- producer side ---------------------------------------------------
    def enqueue(self, sample: ExportedSample) -> None:
        """Queue a sample for upload; never blocks, never raises.

        A disabled exporter returns immediately. An oversized sample is
        skipped with an INFO naming the execution (Requirement 2.10).
        When the queue is at its bound the OLDEST entry is dropped with a
        WARNING naming its execution, so the newest samples survive a
        stalled uploader (Requirement 2.4).
        """
        if self._queue is None:
            return
        try:
            if sample.oversize:
                logger.info(
                    "Tuning sample export skipped for execution %s (node "
                    "%s): an image exceeds the %d-byte cap (input %d bytes, "
                    "reference %d bytes)",
                    sample.execution_id, sample.node_id, MAX_IMAGE_BYTES,
                    len(sample.input_bytes or b""),
                    len(sample.reference_bytes or b""),
                )
                return
            with self._condition:
                while len(self._queue) >= self._queue_size:
                    dropped = self._queue.popleft()
                    logger.warning(
                        "Tuning sample export queue is full (%d entries); "
                        "dropped the oldest sample (execution %s, node %s)",
                        self._queue_size, dropped.execution_id,
                        dropped.node_id,
                    )
                self._queue.append(sample)
                self._condition.notify()
            self.start()
        except Exception:  # noqa: BLE001 - export never touches the run
            logger.debug(
                "Tuning sample export could not enqueue the sample for "
                "execution %s; the run is unaffected",
                getattr(sample, "execution_id", None), exc_info=True,
            )

    # -- worker lifecycle ------------------------------------------------
    def start(self) -> None:
        """Start the upload worker (idempotent, no-op when disabled)."""
        if self._queue is None:
            return
        with self._condition:
            if self._stopping:
                return
            if self._worker is not None and self._worker.is_alive():
                return
            worker = threading.Thread(
                target=self._run, name="tuning-sample-export", daemon=True)
            self._worker = worker
        worker.start()

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        """Ask the worker to finish the queue and exit; never raises."""
        worker = self._worker
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if worker is not None and worker.is_alive():
            try:
                worker.join(timeout)
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                logger.debug(
                    "Tuning sample export worker join failed", exc_info=True)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until the queue is empty and no upload is in flight.

        Test/shutdown helper; returns False on timeout. Never used on the
        run path.
        """
        if self._queue is None:
            return True
        deadline = self._clock() + max(0.0, float(timeout))
        with self._condition:
            while self._queue or self._in_flight:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    self._condition.wait(1.0)
                if not self._queue:
                    if self._stopping:
                        return
                    continue
                sample = self._queue.popleft()
                self._in_flight += 1
            try:
                self._upload_with_retries(sample)
            except Exception:  # noqa: BLE001 - a defect must not kill the
                # worker: the next sample still uploads.
                logger.debug(
                    "Tuning sample export worker contained an error for "
                    "execution %s", getattr(sample, "execution_id", None),
                    exc_info=True,
                )
            finally:
                with self._condition:
                    self._in_flight -= 1
                    self._condition.notify_all()

    # -- upload ----------------------------------------------------------
    def _s3(self) -> Any:
        if self._client is None:
            factory = self._s3_factory or _default_s3_factory
            self._client = factory()
        return self._client

    def _upload_with_retries(self, sample: ExportedSample) -> None:
        """Upload one sample's objects, at most ``max_attempts`` times
        with doubling backoff; a final failure logs an ERROR naming the
        object key and changes nothing else (Requirement 2.5)."""
        config = self._config
        if config is None:
            return
        sidecar_key, _, _ = object_keys(
            config.prefix, sample, self._thing_name)
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._upload(sample)
                return
            except Exception as error:  # noqa: BLE001 - retried, then logged
                if attempt >= self._max_attempts:
                    logger.error(
                        "Tuning sample export failed after %d attempt(s) "
                        "for %s (execution %s): %s; the execution's status, "
                        "artifacts and metadata are unaffected",
                        attempt, sidecar_key, sample.execution_id, error,
                    )
                    return
                delay = self._backoff_base_seconds * (2 ** (attempt - 1))
                logger.debug(
                    "Tuning sample export attempt %d/%d for %s failed (%s); "
                    "retrying in %.1fs", attempt, self._max_attempts,
                    sidecar_key, error, delay,
                )
                try:
                    self._sleep(delay)
                except Exception:  # noqa: BLE001 - contained
                    logger.debug(
                        "Tuning sample export backoff sleep failed",
                        exc_info=True)

    def _upload(self, sample: ExportedSample) -> None:
        """Put the sample's images and then its sidecar.

        Images FIRST: the sidecar is the index entry, so it must never
        exist without the objects it references (Requirement 3.5 reads a
        missing input image as an unreadable sample).
        """
        config = self._config
        client = self._s3()
        sidecar_key, input_key, reference_key = object_keys(
            config.prefix, sample, self._thing_name)
        client.put_object(
            Bucket=config.bucket,
            Key=input_key,
            Body=sample.input_bytes,
            ContentType=IMAGE_CONTENT_TYPE,
        )
        if reference_key is not None:
            client.put_object(
                Bucket=config.bucket,
                Key=reference_key,
                Body=sample.reference_bytes,
                ContentType=IMAGE_CONTENT_TYPE,
            )
        document = build_sidecar(sample, self._thing_name, config.prefix)
        client.put_object(
            Bucket=config.bucket,
            Key=sidecar_key,
            Body=json.dumps(document, sort_keys=True).encode("utf-8"),
            ContentType=SIDECAR_CONTENT_TYPE,
        )
        logger.debug(
            "Exported tuning sample %s (execution %s, node %s)",
            sidecar_key, sample.execution_id, sample.node_id,
        )


# ---------------------------------------------------------------------------
# Process-wide exporter (configured once at startup)
# ---------------------------------------------------------------------------

_exporter_lock = threading.Lock()
_exporter: Optional[SampleExporter] = None


def sample_exporter() -> Optional[SampleExporter]:
    """The process-wide exporter, or ``None`` when export is disabled or
    unconfigured — the state every export call site checks first."""
    return _exporter


def set_sample_exporter(exporter: Optional[SampleExporter]) -> None:
    """Install (or clear) the process-wide exporter."""
    global _exporter  # noqa: WPS420 - process-wide singleton, see module doc
    with _exporter_lock:
        _exporter = exporter


def configure_sample_exporter(
    configuration: Any,
    s3_factory: Optional[Callable[[], Any]] = None,
    thing_name: Optional[str] = None,
    queue_size: int = DEFAULT_QUEUE_SIZE,
) -> Optional[SampleExporter]:
    """Parse the LocalServer component configuration once at startup and
    install the exporter it describes (Requirements 2.1, 2.6, 11.3).

    Returns the exporter, or ``None`` when export is disabled — in which
    case no exporter is installed, no queue is allocated and no S3
    client is created. A present-but-malformed ``workflowTuning`` section
    logs one WARNING; an absent one is silent (the default state of every
    device that has not enabled tuning).
    """
    config = ExportConfig.from_component_configuration(configuration)
    if config is None:
        if (
            isinstance(configuration, Mapping)
            and configuration.get(CONFIG_KEY) is not None
        ):
            logger.warning(
                "Tuning sample export is disabled: the '%s' component "
                "configuration is absent, disabled or malformed (it needs "
                "enabled=true, a bucket, and a prefix ending in '/')",
                CONFIG_KEY,
            )
        else:
            logger.debug(
                "Tuning sample export is not configured ('%s' absent); no "
                "queue and no S3 client are created", CONFIG_KEY)
        set_sample_exporter(None)
        return None
    exporter = SampleExporter(
        config,
        s3_factory=s3_factory,
        thing_name=thing_name,
        queue_size=queue_size,
    )
    set_sample_exporter(exporter)
    logger.info(
        "Tuning sample export enabled for device '%s': s3://%s/%s",
        exporter.thing_name, config.bucket, config.prefix,
    )
    return exporter


def shutdown_sample_exporter(timeout: Optional[float] = 5.0) -> None:
    """Stop and clear the process-wide exporter (test/shutdown helper)."""
    exporter = _exporter
    set_sample_exporter(None)
    if exporter is not None:
        exporter.stop(timeout)


# ---------------------------------------------------------------------------
# Run_Metadata snippet for llm_inference replay
# ---------------------------------------------------------------------------

def _json_safe(value: Any) -> Any:
    """A JSON-serializable projection of a Run_Metadata value.

    Mappings and lists are copied recursively; scalars pass through;
    ``bytes`` are DROPPED (an image or payload blob must never travel in
    the sidecar — Requirement 2.3, Property 16) and anything else
    becomes its ``str``, which is exactly what ``render_prompt``
    substitutes.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if isinstance(value, Mapping):
        safe: Dict[str, Any] = {}
        for key, item in value.items():
            projected = _json_safe(item)
            if projected is None and item is not None:
                continue
            safe[str(key)] = projected
        return safe
    if isinstance(value, (list, tuple)):
        return [
            _json_safe(item) for item in value
            if not isinstance(item, (bytes, bytearray, memoryview))
        ]
    return str(value)


def metadata_snippet(
    prompt_template: Any,
    metadata: Any,
    max_bytes: int = METADATA_SNIPPET_MAX_BYTES,
) -> Optional[Dict[str, Any]]:
    """The Run_Metadata fragment a Device_Score_Job needs to re-render
    this node's Prompt_Template, or ``None``.

    Only the TOP-LEVEL keys the template's ``{placeholder}`` names
    reference are carried (``{trigger.payload_json.part}`` carries
    ``trigger``), JSON-projected and bounded: entries are dropped
    largest-first until the serialized snippet fits ``max_bytes``. A
    template without placeholders, an absent metadata mapping or a
    snippet that cannot be bounded yields ``None`` — the replay then
    reports the unresolved placeholder rather than inventing metadata.
    """
    if not isinstance(metadata, Mapping):
        return None
    template = str(prompt_template or "")
    names = sorted({
        match.group(1).split(".")[0]
        for match in PLACEHOLDER_RE.finditer(template)
    })
    snippet: Dict[str, Any] = {}
    for name in names:
        if name not in metadata:
            continue
        projected = _json_safe(metadata[name])
        if projected is None and metadata[name] is not None:
            continue
        snippet[name] = projected
    if not snippet:
        return None
    while snippet:
        try:
            encoded = json.dumps(snippet, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return None
        if len(encoded.encode("utf-8")) <= max_bytes:
            return snippet
        largest = max(
            snippet,
            key=lambda key: len(json.dumps(snippet[key], default=str)),
        )
        logger.debug(
            "Tuning sample export: dropping metadata snippet key '%s' to "
            "stay within %d bytes", largest, max_bytes)
        snippet.pop(largest)
    return None


# ---------------------------------------------------------------------------
# Executor call sites (Requirements 2.2, 11.1, 11.2)
#
# Both helpers are TOTALLY contained: they return immediately when no
# exporter is installed, and any failure is logged at debug and
# swallowed, so an Execution's outcome, artifacts and Run_Metadata are
# identical to a run with export disabled.
# ---------------------------------------------------------------------------

def _coerce_slot(raw: Any) -> Optional[int]:
    """The Detection_Crop's slot (its ``crop_detection_index``), or
    ``None``."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def export_bedrock_sample(
    context: Optional[ExportContext],
    node_id: Optional[str],
    invocation: Any,
    parameters: Mapping[str, Any],
    answer: Optional[str],
    verdict: Optional[Mapping[str, Any]] = None,
    parse_error: Optional[str] = None,
    detection_id: Optional[str] = None,
    exporter: Optional[SampleExporter] = None,
) -> None:
    """Export one ``bedrock_inference`` Anomaly_Mode invocation.

    The images come from the built :class:`BedrockInvocation`, so the
    exported bytes are by construction the bytes the invoker was handed
    (Property 2). Freeform invocations export nothing.
    """
    try:
        exporter = exporter if exporter is not None else sample_exporter()
        if exporter is None or context is None:
            return
        if not getattr(invocation, "anomaly_mode", False):
            return
        images = list(getattr(invocation, "images", ()) or ())
        if not images:
            return
        input_bytes = images[0][1]
        reference_bytes = images[1][1] if len(images) > 1 else None
        exporter.enqueue(ExportedSample(
            workflow_id=context.workflow_id,
            node_id=node_id,
            node_type="bedrock_inference",
            execution_id=context.execution_id,
            input_bytes=input_bytes,
            version=context.version,
            reference_bytes=reference_bytes,
            answer=answer,
            verdict=verdict,
            parse_error=parse_error,
            prompt_fingerprint=prompt_fingerprint(parameters),
            detection_id=detection_id,
            detection_slot=(
                _coerce_slot(parameters.get("crop_detection_index"))
                if detection_id is not None else None
            ),
        ))
    except Exception:  # noqa: BLE001 - export never touches the run
        logger.debug(
            "Tuning sample export skipped for bedrock node %s; the run is "
            "unaffected", node_id, exc_info=True)


def export_llm_sample(
    context: Optional[ExportContext],
    node_id: Optional[str],
    invocation: Any,
    parameters: Mapping[str, Any],
    answer: Optional[str],
    metadata: Any = None,
    verdict: Optional[Mapping[str, Any]] = None,
    parse_error: Optional[str] = None,
    exporter: Optional[SampleExporter] = None,
) -> None:
    """Export one ``llm_inference`` Anomaly_Mode invocation.

    The images are decoded back from the invocation's base64 fields, so
    the exported bytes are exactly the (downscaled) bytes the request
    carried. A sample without an image exports nothing — there would be
    nothing to replay. ``metadataSnippet`` carries the Run_Metadata keys
    the node's Prompt_Template references so a Device_Score_Job can
    re-render it.
    """
    try:
        exporter = exporter if exporter is not None else sample_exporter()
        if exporter is None or context is None:
            return
        if not getattr(invocation, "anomaly_mode", False):
            return
        image_b64 = getattr(invocation, "image_b64", None)
        if not image_b64:
            return
        input_bytes = base64.b64decode(image_b64)
        reference_b64 = getattr(invocation, "reference_b64", None)
        reference_bytes = (
            base64.b64decode(reference_b64) if reference_b64 else None
        )
        exporter.enqueue(ExportedSample(
            workflow_id=context.workflow_id,
            node_id=node_id,
            node_type="llm_inference",
            execution_id=context.execution_id,
            input_bytes=input_bytes,
            version=context.version,
            reference_bytes=reference_bytes,
            answer=answer,
            verdict=verdict,
            parse_error=parse_error,
            prompt_fingerprint=prompt_fingerprint(parameters),
            metadata_snippet=metadata_snippet(
                parameters.get("prompt_template"), metadata),
        ))
    except Exception:  # noqa: BLE001 - export never touches the run
        logger.debug(
            "Tuning sample export skipped for llm node %s; the run is "
            "unaffected", node_id, exc_info=True)


__all__: List[str] = [
    "BACKOFF_BASE_SECONDS",
    "CONFIG_KEY",
    "DEFAULT_QUEUE_SIZE",
    "ExportConfig",
    "ExportContext",
    "ExportedSample",
    "INPUT_SUFFIX",
    "MAX_IMAGE_BYTES",
    "MAX_UPLOAD_ATTEMPTS",
    "METADATA_SNIPPET_MAX_BYTES",
    "REFERENCE_SUFFIX",
    "SIDECAR_SCHEMA_VERSION",
    "SIDECAR_SUFFIX",
    "SOURCE_BACKFILL",
    "SOURCE_LIVE",
    "SampleExporter",
    "build_sidecar",
    "configure_sample_exporter",
    "export_bedrock_sample",
    "export_llm_sample",
    "metadata_snippet",
    "object_key_base",
    "object_keys",
    "sample_exporter",
    "set_sample_exporter",
    "shutdown_sample_exporter",
]
