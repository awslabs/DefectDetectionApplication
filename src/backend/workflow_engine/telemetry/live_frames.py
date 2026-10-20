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

"""The live-frame sender of the Telemetry_Agent (workflow-telemetry-dashboards
design §4.3, §5.2 ``live_frames.py``, §9.3).

:class:`LiveFrameSender` holds the applied ``desired.live.<workflowId>``
leases (replaced wholesale by :meth:`set_leases` from each complete shadow
document). After each completed run of a leased Pair, when ``now <
expiresAt`` and ``maxFps`` allows, its thread reads the run's overlay JPEG
(``run_artifacts.overlay_image_path``) or the base capture, downscales it
with Pillow to within ``maxWidth x maxHeight``, re-encodes at quality 80 and
then 60, skips and counts ``framesTooLarge`` when still above ``maxBytes``,
``PutObject``s to ``s3://<bucket>/<prefix><seq mod 16>.jpg`` with
``Content-Type: image/jpeg`` and ``Metadata={"seq": str(seq)}`` through
``boto3.client("s3", region_name=lease.region)`` (one client per (bucket,
region), 5 s timeouts, no ``GetBucketLocation``), and only then publishes
``dda.live.v1`` at QoS 0 with the agent's ``instance`` and the per-Pair
``seq`` that starts at 0 with every sender instance. A failed PUT publishes
nothing and is counted. The sender stops within 10 s of a lease lapsing
(``expiresAt`` passed, or the entry absent from the complete document).
"""

import collections
import io
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from workflow_engine import run_artifacts

logger = logging.getLogger(__name__)

__all__ = ["Lease", "LiveFrameSender", "encode_frame", "SCHEMA", "LIVE_QOS", "SLOTS",
           "QUALITIES", "LEASE_LAPSE_S"]

SCHEMA = "dda.live.v1"
LIVE_QOS = 0
#: The ring of keys under a Pair's prefix: ``<prefix><seq mod 16>.jpg``.
SLOTS = 16
QUALITIES = (80, 60)
PUT_TIMEOUT_S = 5
#: A lapsed lease is dropped within this long (seconds).
LEASE_LAPSE_S = 10.0
#: Frames waiting for the sender thread; the newest supersedes the oldest.
QUEUE_MAXLEN = 4
#: The lease caps (§5.4, §15.2).
MAX_FPS = 2.0
MAX_WIDTH = 1280
MAX_HEIGHT = 720
MAX_BYTES = 307200
REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")


@dataclass(frozen=True)
class Lease:
    """One applied ``desired.live.<workflowId>`` entry (validated by
    ``desired_state``)."""

    workflow_id: str
    expires_at_ms: int
    max_fps: float
    max_width: int
    max_height: int
    max_bytes: int
    bucket: str
    region: str
    prefix: str

    def active(self, now_ms: int) -> bool:
        return now_ms < self.expires_at_ms


def _default_client_factory(region: str):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3", region_name=region,
        config=Config(connect_timeout=PUT_TIMEOUT_S, read_timeout=PUT_TIMEOUT_S,
                      retries={"max_attempts": 1}),
    )


def encode_frame(data: bytes, max_width: int, max_height: int, max_bytes: int) -> Optional[Tuple[bytes, int, int]]:
    """Downscale ``data`` (a JPEG/PNG) to within ``max_width x max_height``
    (never upscaled) and re-encode at quality 80 then 60; ``(jpeg_bytes,
    width, height)`` of the first encoding within ``max_bytes``, else
    ``None``."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        width, height = image.size
        scale = min(1.0, float(max_width) / float(width), float(max_height) / float(height))
        if scale < 1.0:
            image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        width, height = image.size
        for quality in QUALITIES:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = buffer.getvalue()
            if len(encoded) <= max_bytes:
                return encoded, width, height
    return None


class _PairState:
    __slots__ = ("seq", "last_accepted_ms", "frames_sent", "frames_too_large", "put_failures", "last_seq")

    def __init__(self) -> None:
        self.seq = 0
        self.last_accepted_ms: Optional[int] = None
        self.frames_sent = 0
        self.frames_too_large = 0
        self.put_failures = 0
        self.last_seq: Optional[int] = None


class LiveFrameSender:
    """See the module docstring.

    ``publish(topic, payload: bytes, qos)`` is the agent's publisher;
    ``topic_for(workflow_id) -> topic | None`` builds the ``.../live`` topic
    (``None`` while the agent has no use case id, in which case nothing is
    published); ``person_classes()`` returns the labels counted as
    ``people``. ``client_factory(region)`` and ``clock`` (epoch seconds) are
    injectable; ``run_thread=False`` lets a test drain the queue itself with
    :meth:`drain`.
    """

    def __init__(
        self,
        *,
        instance: str,
        device_id: str,
        publish: Callable[[str, bytes, int], None],
        topic_for: Callable[[str], Optional[str]],
        person_classes: Optional[Callable[[], Any]] = None,
        client_factory: Optional[Callable[[str], Any]] = None,
        clock: Callable[[], float] = time.time,
        run_thread: bool = True,
    ) -> None:
        self.instance = str(instance)
        self.device_id = str(device_id)
        self._publish = publish
        self._topic_for = topic_for
        self._person_classes = person_classes or (lambda: ("person", "human"))
        self._client_factory = client_factory or _default_client_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._leases: Dict[str, Lease] = {}
        self._pairs: Dict[str, _PairState] = {}
        self._clients: Dict[Tuple[str, str], Any] = {}
        self._queue: Deque[Tuple[Any, int]] = collections.deque(maxlen=QUEUE_MAXLEN)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.frames_sent = 0
        self.frames_too_large = 0
        self.put_failures = 0
        self.frames_skipped = 0
        self.queue_drops = 0
        if run_thread:
            self._thread = threading.Thread(target=self._loop, name="telemetry-live-frames", daemon=True)
            self._thread.start()

    # -- time -----------------------------------------------------------------------

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    # -- leases ---------------------------------------------------------------------

    def set_leases(self, leases: Dict[str, Lease]) -> None:
        """Replace the held leases with the complete document's (§5.4): a
        workflow absent from ``leases`` stops at once."""
        with self._lock:
            self._leases = {wf: lease for wf, lease in (leases or {}).items() if isinstance(lease, Lease)}
            for workflow_id in [wf for wf in self._pairs if wf not in self._leases]:
                self._pairs[workflow_id].last_accepted_ms = None
            self._cond.notify_all()

    def leases(self) -> Dict[str, Lease]:
        with self._lock:
            return dict(self._leases)

    def tick(self, now_ms: Optional[int] = None) -> None:
        """Drop the leases whose ``expiresAt`` passed."""
        now = self._now_ms() if now_ms is None else int(now_ms)
        with self._lock:
            lapsed = [wf for wf, lease in self._leases.items() if not lease.active(now)]
            for workflow_id in lapsed:
                del self._leases[workflow_id]
                logger.info("Live frames: lease for workflow %s lapsed", workflow_id)

    def _lease_for(self, workflow_id: str, now_ms: int) -> Optional[Lease]:
        lease = self._leases.get(workflow_id)
        if lease is None or not lease.active(now_ms):
            return None
        return lease

    # -- the run observer (enqueue and return) --------------------------------------------

    def on_run_finished(self, record: Any) -> bool:
        """Queue the run's frame when its Pair is leased, the lease is
        current and ``maxFps`` allows. Returns whether it was queued."""
        if getattr(record, "status", None) != "completed":
            return False
        workflow_id = getattr(record, "workflow_id", None)
        if not isinstance(workflow_id, str) or not workflow_id:
            return False
        now = self._now_ms()
        with self._lock:
            lease = self._lease_for(workflow_id, now)
            if lease is None:
                return False
            pair = self._pairs.get(workflow_id)
            if pair is None:
                pair = self._pairs[workflow_id] = _PairState()
            min_gap_ms = int(1000.0 / max(0.01, float(lease.max_fps)))
            if pair.last_accepted_ms is not None and now - pair.last_accepted_ms < min_gap_ms:
                self.frames_skipped += 1
                return False
            pair.last_accepted_ms = now
            if len(self._queue) >= self._queue.maxlen:
                self.queue_drops += 1
            self._queue.append((record, now))
            self._cond.notify()
        return True

    # -- the thread ------------------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._cond.notify_all()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                while not self._queue and not self._stop.is_set():
                    self._cond.wait(1.0)
                if self._stop.is_set():
                    return
                record, _stamp = self._queue.popleft()
            try:
                self._send(record)
            except Exception:  # noqa: BLE001 - the thread never dies
                logger.warning("Live frames: sending a frame failed", exc_info=True)

    def drain(self) -> int:
        """Send every queued frame on the calling thread (tests). Returns
        the number of frames published."""
        sent = 0
        while True:
            with self._lock:
                if not self._queue:
                    return sent
                record, _stamp = self._queue.popleft()
            try:
                if self._send(record):
                    sent += 1
            except Exception:  # noqa: BLE001
                logger.warning("Live frames: sending a frame failed", exc_info=True)

    # -- one frame ---------------------------------------------------------------------------------

    def _send(self, record: Any) -> bool:
        workflow_id = str(getattr(record, "workflow_id", ""))
        now = self._now_ms()
        with self._lock:
            lease = self._lease_for(workflow_id, now)
            pair = self._pairs.get(workflow_id)
        if lease is None or pair is None:
            self.frames_skipped += 1
            return False
        topic = self._topic_for(workflow_id)
        if not topic:
            self.frames_skipped += 1
            return False
        path = run_artifacts.overlay_image_path(record.output_dir, record.capture_id) \
            or run_artifacts.base_output_image_path(record.output_dir, record.capture_id)
        if path is None:
            logger.debug("Live frames: run %s of workflow %s left no frame on disk",
                         getattr(record, "execution_id", "?"), workflow_id)
            return False
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            encoded = encode_frame(data, lease.max_width, lease.max_height, lease.max_bytes)
        except Exception:  # noqa: BLE001 - an unreadable or undecodable frame
            logger.debug("Live frames: could not read or encode %s", path, exc_info=True)
            self.put_failures += 1
            return False
        if encoded is None:
            with self._lock:
                self.frames_too_large += 1
                pair.frames_too_large += 1
            logger.debug("Live frames: frame of workflow %s is above %d bytes after both re-encodes",
                         workflow_id, lease.max_bytes)
            return False
        jpeg, width, height = encoded
        seq = pair.seq
        key = "{0}{1}.jpg".format(lease.prefix, seq % SLOTS)
        try:
            self._client(lease.bucket, lease.region).put_object(
                Bucket=lease.bucket, Key=key, Body=jpeg, ContentType="image/jpeg",
                Metadata={"seq": str(seq)},
            )
        except Exception:  # noqa: BLE001 - counted; nothing is published
            with self._lock:
                self.put_failures += 1
                pair.put_failures += 1
            logger.warning("Live frames: PUT of %s to %s failed; no live message", key, lease.bucket,
                           exc_info=True)
            return False
        payload = {
            "schema": SCHEMA,
            "ts": self._now_ms(),
            "instance": self.instance,
            "seq": seq,
            "deviceId": self.device_id,
            "workflowId": workflow_id,
            "workflowVersion": str(getattr(record, "workflow_version", "") or ""),
            "frame": {"bucket": lease.bucket, "key": key, "width": width, "height": height,
                      "bytes": len(jpeg)},
        }
        per_class, people = self._detections(record)
        payload["perClass"] = per_class
        payload["people"] = people
        self._publish(topic, json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"),
                      LIVE_QOS)
        with self._lock:
            pair.seq = seq + 1
            pair.last_seq = seq
            pair.frames_sent += 1
            self.frames_sent += 1
        return True

    def _detections(self, record: Any) -> Tuple[Dict[str, int], int]:
        detections = getattr(record, "detections", None)
        per_class: Dict[str, int] = {}
        people = 0
        if not isinstance(detections, list):
            return per_class, people
        try:
            wanted = {str(c).strip().lower() for c in (self._person_classes() or ())}
        except Exception:  # noqa: BLE001
            wanted = {"person", "human"}
        for item in detections:
            if not isinstance(item, dict) or item.get("label") is None:
                continue
            label = str(item["label"])[:64]
            per_class[label] = per_class.get(label, 0) + 1
            if label.strip().lower() in wanted:
                people += 1
        return per_class, people

    def _client(self, bucket: str, region: str):
        key = (bucket, region)
        with self._lock:
            client = self._clients.get(key)
        if client is None:
            client = self._client_factory(region)
            with self._lock:
                self._clients[key] = client
        return client

    # -- diagnostics ---------------------------------------------------------------------------------

    def live_status(self) -> Dict[str, Dict[str, Any]]:
        """``{<wf>: {activeUntil, lastSeq, framesTooLarge}}`` for the held
        leases (the ``reported.live`` map)."""
        with self._lock:
            out: Dict[str, Dict[str, Any]] = {}
            for workflow_id, lease in self._leases.items():
                pair = self._pairs.get(workflow_id)
                out[workflow_id] = {
                    "activeUntil": lease.expires_at_ms,
                    "lastSeq": pair.last_seq if pair is not None else None,
                    "framesTooLarge": pair.frames_too_large if pair is not None else 0,
                }
            return out

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "leases": sorted(self._leases),
                "framesSent": self.frames_sent,
                "framesTooLarge": self.frames_too_large,
                "putFailures": self.put_failures,
                "framesSkipped": self.frames_skipped,
                "queueDrops": self.queue_drops,
                "queued": len(self._queue),
                "threadAlive": bool(self._thread is not None and self._thread.is_alive()),
            }
