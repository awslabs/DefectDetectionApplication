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
"""Forwarding of one ``POST /mcp`` request to the Edge MCP child (backend
process; design §5 ``proxy.py``, Error Handling (Edge) proxy rows).

``forward`` sends the request body to ``http://127.0.0.1:5790/mcp`` with an
allowlist of request headers (``Origin`` is never forwarded) and the
request's correlation id as ``X-Request-ID``, exactly once (no transport
retries: a retried ``tools/call`` would run a tool twice), and relays the
status and ``Content-Type`` (the body decoded; ``Content-Encoding`` and
hop-by-hop headers are not relayed):

- a non-``text/event-stream`` body (the only kind the child produces) is
  buffered up to 4 MiB, so an upstream read timeout anywhere in it is a
  ``504 mcp_timeout`` and a larger body a ``502 mcp_bad_upstream``;
- a ``text/event-stream`` body (defensive; unreachable with the child's
  JSON responses) is streamed; a timeout inside it can only end the stream;
- a refused connection is ``503 mcp_unavailable``.

Uses the backend's pinned ``httpx`` (0.24.1) with blocking I/O; the route
calling it is a plain ``def`` so FastAPI runs it in the threadpool.
"""
import logging
import uuid
from typing import Iterator, Mapping, Optional

import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse

from mcp_edge import CHILD_PORT

logger = logging.getLogger(__name__)

UPSTREAM_URL = f"http://127.0.0.1:{CHILD_PORT}/mcp"
# Above every edge spec's timeout_s (edge_textgen_generate: 130 s), so the
# child, not the proxy, gives up first.
MCP_PROXY_UPSTREAM_READ_S = 150.0
MCP_PROXY_MAX_UPSTREAM_BODY = 4 * 1024 * 1024

FORWARDED_HEADERS = ("authorization", "content-type", "accept",
                     "mcp-protocol-version", "mcp-method", "mcp-name",
                     "mcp-session-id")
FORWARDED_HEADER_PREFIXES = ("mcp-param-",)
RELAYED_RESPONSE_HEADERS = ("mcp-session-id", "mcp-protocol-version",
                            "www-authenticate")
EVENT_STREAM = "text/event-stream"

TIMEOUT = httpx.Timeout(connect=2.0, write=10.0, read=MCP_PROXY_UPSTREAM_READ_S,
                        pool=2.0)
# retries=0 is httpx's default, written out: one request reaches the child
# exactly once.
CLIENT = httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=TIMEOUT)


def error_response(status_code: int, error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status_code)


def forwarded_headers(headers: Mapping[str, str],
                      correlation_id: Optional[str]) -> dict:
    """The outbound header set: the allowlist plus ``X-Request-ID``. Never
    contains ``Origin``, cookies or hop-by-hop headers. Pure."""
    outbound = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in FORWARDED_HEADERS or lower.startswith(FORWARDED_HEADER_PREFIXES):
            outbound[lower] = value
    outbound["x-request-id"] = correlation_id or uuid.uuid4().hex
    return outbound


def _relayed_headers(upstream: httpx.Response) -> dict:
    return {name: upstream.headers[name] for name in RELAYED_RESPONSE_HEADERS
            if name in upstream.headers}


def _relay(upstream: httpx.Response) -> Iterator[bytes]:
    try:
        yield from upstream.iter_bytes()
    except httpx.TimeoutException:
        logger.warning("MCP child event stream timed out; the stream ends here")
    finally:
        upstream.close()


def _buffer(upstream: httpx.Response) -> Optional[bytes]:
    """The upstream body, or None when it exceeds the cap."""
    chunks, size = [], 0
    # Decoded bytes: Content-Encoding is not relayed.
    for chunk in upstream.iter_bytes():
        size += len(chunk)
        if size > MCP_PROXY_MAX_UPSTREAM_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def forward(body: bytes, headers: Mapping[str, str],
            correlation_id: Optional[str], client: Optional[httpx.Client] = None
            ) -> Response:
    """Forward one request to the child and relay its answer; never raises."""
    client = client or CLIENT
    request = client.build_request("POST", UPSTREAM_URL, content=body,
                                   headers=forwarded_headers(headers, correlation_id))
    try:
        upstream = client.send(request, stream=True)
    except httpx.ConnectError:
        logger.warning("MCP child refused the connection")
        return error_response(503, "mcp_unavailable")
    except httpx.TimeoutException:
        logger.warning("MCP child did not answer within %.0f s",
                       MCP_PROXY_UPSTREAM_READ_S)
        return error_response(504, "mcp_timeout")
    except httpx.HTTPError as error:
        logger.warning("MCP child request failed: %s", type(error).__name__)
        return error_response(502, "mcp_bad_upstream")

    content_type = upstream.headers.get("content-type", "")
    if content_type.split(";")[0].strip().lower() == EVENT_STREAM:
        return StreamingResponse(_relay(upstream), status_code=upstream.status_code,
                                 media_type=content_type,
                                 headers=_relayed_headers(upstream))
    try:
        content = _buffer(upstream)
    except httpx.TimeoutException:
        logger.warning("MCP child response body timed out")
        return error_response(504, "mcp_timeout")
    except httpx.HTTPError as error:
        logger.warning("MCP child response failed: %s", type(error).__name__)
        return error_response(502, "mcp_bad_upstream")
    finally:
        upstream.close()
    if content is None:
        logger.warning("MCP child response exceeds %d bytes",
                       MCP_PROXY_MAX_UPSTREAM_BODY)
        return error_response(502, "mcp_bad_upstream")
    return Response(content, status_code=upstream.status_code,
                    media_type=content_type or None,
                    headers=_relayed_headers(upstream))
