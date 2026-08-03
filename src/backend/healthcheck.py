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
"""Docker healthcheck helper for the flask-app backend container
(edge-deploy-reliability, Defect B).

Wired in ``src/docker-compose.yaml`` as::

    healthcheck:
      test: ["CMD", "python3", "/healthcheck.py"]

Probes the backend's unauthenticated ``GET /health`` endpoint
(``endpoints/health.py``): first ``http://127.0.0.1:5000/health`` (the
plaintext listener when station authorization is disabled), then falls back to
``https://127.0.0.1:5443/health`` with certificate verification disabled (the
TLS listener when station authorization is enabled — the cert is a
device-local self-signed cert, and the probe never leaves loopback). Exits 0
iff either returns HTTP 200.

Python (stdlib only) because the backend image is not guaranteed to carry
curl/wget; both backend services run with ``network_mode: host``, so loopback
reaches the uvicorn listener directly.
"""
import ssl
import sys
import urllib.request

#: Probe order per app.py's main(): port 5000 plaintext (authorization
#: disabled), else 5443 TLS (authorization enabled).
HEALTH_URLS = (
    "http://127.0.0.1:5000/health",
    "https://127.0.0.1:5443/health",
)

#: Per-probe budget; the compose healthcheck timeout is 10s, so two probes
#: at 4s each stay inside it.
PROBE_TIMEOUT_SECONDS = 4


def probe(url, timeout=PROBE_TIMEOUT_SECONDS):
    """True iff ``url`` answers HTTP 200 within ``timeout`` seconds."""
    context = None
    if url.startswith("https"):
        # Device-local self-signed cert on a loopback-only probe: verification
        # is intentionally disabled (the healthcheck asserts liveness, not
        # identity — nothing off-device can intercept 127.0.0.1).
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(  # nosec B310 — fixed loopback http(s) URLs
                url, timeout=timeout, context=context) as response:
            return response.status == 200
    except Exception:
        # Connection refused, TLS handshake failure, HTTP error status,
        # timeout — all mean "this listener is not serving healthy".
        return False


def main(urls=HEALTH_URLS):
    """Exit code for the docker healthcheck: 0 iff any URL returns 200."""
    for url in urls:
        if probe(url):
            return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
