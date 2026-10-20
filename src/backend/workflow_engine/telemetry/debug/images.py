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

"""The optional frame upload of a debug session (workflow-debug-mode design
§5.1 ``images.py``; R4.5).

When a session's ``images`` is on and the budgets allow (2 per second and
2,000 per session, ``imagesCapped`` otherwise), :class:`ImageUploader` loads
the run's overlay JPEG (``run_artifacts.overlay_image_path``) or its base
capture, downscales it with Pillow to within 960 x 540, re-encodes it at
quality 75 and then 55, and PUTs it to ``s3://<bucket>/<prefix><executionId>.jpg``
with ``boto3.client("s3", region_name=entry.region)`` (one client per
(bucket, region), the container's default credential chain, a 5 s timeout, no
``GetBucketLocation``). A frame still above ``imageMaxKb`` after the second
re-encode is not uploaded (``imagesTooLarge``); a failed PUT is counted; in
both cases the record carries ``image: null``.
"""

import io
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from workflow_engine import run_artifacts

logger = logging.getLogger(__name__)

__all__ = ["ImageUploader", "MAX_WIDTH", "MAX_HEIGHT", "QUALITIES", "PER_SECOND_BUDGET",
           "PER_SESSION_BUDGET"]

MAX_WIDTH, MAX_HEIGHT = 960, 540
QUALITIES = (75, 55)
PER_SECOND_BUDGET = 2
PER_SESSION_BUDGET = 2000
PUT_TIMEOUT_S = 5


def _default_client_factory(region: str):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3", region_name=region,
        config=Config(connect_timeout=PUT_TIMEOUT_S, read_timeout=PUT_TIMEOUT_S,
                      retries={"max_attempts": 1}),
    )


def encode_within_budget(data: bytes, max_bytes: int) -> Optional[Tuple[bytes, int, int]]:
    """Downscale ``data`` (a JPEG/PNG) to within 960 x 540 and re-encode at
    quality 75 then 55; ``(jpeg_bytes, width, height)`` of the first encoding
    within ``max_bytes``, else ``None``."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        width, height = image.size
        scale = min(1.0, MAX_WIDTH / float(width), MAX_HEIGHT / float(height))
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


class ImageUploader:
    """See the module docstring. ``client_factory(region) -> s3 client`` and
    ``clock`` are injectable (tests)."""

    def __init__(self, client_factory: Optional[Callable[[str], Any]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._client_factory = client_factory or _default_client_factory
        self._clock = clock
        self._clients: Dict[Tuple[str, str], Any] = {}
        self._second_window: Optional[int] = None
        self._sent_this_second = 0
        self.failures = 0

    def _client(self, bucket: str, region: str):
        key = (bucket, region)
        client = self._clients.get(key)
        if client is None:
            client = self._client_factory(region)
            self._clients[key] = client
        return client

    def _budget_allows(self, session: Any) -> bool:
        if getattr(session, "images_sent", 0) >= PER_SESSION_BUDGET:
            return False
        second = int(self._clock())
        if second != self._second_window:
            self._second_window = second
            self._sent_this_second = 0
        return self._sent_this_second < PER_SECOND_BUDGET

    def maybe_upload(self, session: Any, run_record: Any) -> Optional[dict]:
        """Upload the run's frame when the session asks for images and the
        budgets allow. Returns the record's ``image`` field (``None`` when
        nothing was uploaded); counts ``imagesSent``, ``imagesCapped`` and
        ``imagesTooLarge`` on the session."""
        entry = getattr(session, "entry", None) or {}
        if not entry.get("images"):
            return None
        counters = getattr(session, "counters", None)
        if counters is None:
            counters = {}
        if not self._budget_allows(session):
            counters["imagesCapped"] = counters.get("imagesCapped", 0) + 1
            return None
        path = run_artifacts.overlay_image_path(run_record.output_dir, run_record.capture_id) \
            or run_artifacts.base_output_image_path(run_record.output_dir, run_record.capture_id)
        if path is None:
            return None
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            encoded = encode_within_budget(data, int(entry.get("imageMaxKb", 150)) * 1024)
        except Exception:  # noqa: BLE001 - an unreadable or undecodable frame
            logger.debug("Debug images: could not read or encode %s", path, exc_info=True)
            self.failures += 1
            return None
        if encoded is None:
            counters["imagesTooLarge"] = counters.get("imagesTooLarge", 0) + 1
            return None
        jpeg, width, height = encoded
        bucket = entry.get("bucket")
        region = entry.get("region")
        key = "{0}{1}.jpg".format(entry.get("prefix", ""), run_record.execution_id)
        try:
            self._client(bucket, region).put_object(
                Bucket=bucket, Key=key, Body=jpeg, ContentType="image/jpeg",
            )
        except Exception:  # noqa: BLE001 - counted, image null
            self.failures += 1
            counters["imageFailures"] = counters.get("imageFailures", 0) + 1
            logger.warning("Debug images: upload of %s to %s failed", key, bucket, exc_info=True)
            return None
        self._sent_this_second += 1
        session.images_sent = getattr(session, "images_sent", 0) + 1
        counters["imagesSent"] = counters.get("imagesSent", 0) + 1
        return {"bucket": bucket, "key": key, "width": width, "height": height, "bytes": len(jpeg)}
