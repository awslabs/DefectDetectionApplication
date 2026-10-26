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
"""Entry point of the Edge MCP child: ``python -m mcp_edge.child``.

argparse, then ``configure_logging()``, then the server on
``127.0.0.1:<port>`` under the venv's uvicorn (design §5
``child/__main__.py``). Invalid arguments exit with status 2, which the
supervisor treats as non-retryable.

``configure_logging()`` keeps credentials out of the child's ``logging``
output (Requirement 9.7): the venv's ``httpx2`` logs every request URL,
query string included, at INFO, and for the image tools whose route takes
the credential as ``?token=`` that URL carries it. So the root logger and
``httpx2``, ``httpcore2``, ``uvicorn*`` and ``mcp`` are held at WARNING, a
``CredentialScrubFilter`` on the root handler drops any record mentioning
``token=``, ``Authorization`` or ``Bearer ``, and the root handler writes to
stderr; stdout is reserved for the JSON records of ``child/logging.emit``.
"""
import argparse
import logging
import re
import sys
from typing import List, Optional, Sequence
from urllib.parse import urlsplit

from mcp_edge import CHILD_PORT, __version__

QUIET_LOGGERS = ("httpx2", "httpcore2", "uvicorn", "uvicorn.access",
                 "uvicorn.error", "mcp")
CREDENTIAL_MARKERS = ("token=", "authorization", "bearer ")
SERVER_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(\+[A-Za-z0-9.\-]+)?$")
KNOWN_FEATURES = ("vllm",)
LOOPBACK_HOST = "127.0.0.1"


class CredentialScrubFilter(logging.Filter):
    """Drops every record whose message (or exception text) mentions a
    credential marker, case-insensitively."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 — an unformattable record is dropped
            return False
        if record.exc_info:
            text += logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            text += record.exc_text
        lowered = text.lower()
        return not any(marker in lowered for marker in CREDENTIAL_MARKERS)


def configure_logging() -> None:
    """Hold library loggers at WARNING and route the root logger to stderr
    through the scrub filter. Idempotent."""
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(CredentialScrubFilter())
    root.addHandler(handler)


# ----------------------------------------------------------- arguments

def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {value!r}")
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("the port must be within 1024-65535")
    return port


def _backend_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or parts.hostname != LOOPBACK_HOST \
            or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise argparse.ArgumentTypeError(
            "the backend URL must be http://127.0.0.1:<port> or "
            "https://127.0.0.1:<port>")
    try:
        parts.port
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port in {value!r}")
    return value.rstrip("/")


def _server_version(value: str) -> str:
    if not SERVER_VERSION_RE.match(value):
        raise argparse.ArgumentTypeError(
            "the server version must be <semver>[+<build metadata>]")
    return value


def _origin(value: str) -> str:
    parts = urlsplit(value)
    if not parts.scheme or not parts.hostname or parts.path or parts.query \
            or parts.fragment or parts.username or parts.password:
        raise argparse.ArgumentTypeError(
            f"an allowed origin must be scheme://host[:port]: {value!r}")
    try:
        parts.port
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port in {value!r}")
    return value


def _csv(item_type):
    def parse(value: str) -> List[str]:
        items = [part.strip() for part in value.split(",") if part.strip()]
        return [item_type(item) for item in items]
    return parse


def _feature(value: str) -> str:
    if value not in KNOWN_FEATURES:
        raise argparse.ArgumentTypeError(f"unknown feature: {value!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mcp_edge.child",
        description="Edge_MCP_Server child process (loopback, supervised).")
    parser.add_argument("--version", action="version",
                        version=f"dda-edge-mcp {__version__}")
    parser.add_argument("--port", type=_port, default=CHILD_PORT)
    parser.add_argument("--backend-url", type=_backend_url, required=True)
    parser.add_argument("--server-version", type=_server_version,
                        default=__version__)
    parser.add_argument("--allowed-origins", type=_csv(_origin), default=[])
    parser.add_argument("--features", type=_csv(_feature), default=[])
    parser.add_argument("--mutating-enabled", action="store_true")
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging()
    # Imported only after the arguments are valid: `--help`/`--version` and
    # argument errors need neither the SDK nor uvicorn.
    import uvicorn

    from mcp_edge.child.server import build_server

    app = build_server(args)
    uvicorn.run(app, host=LOOPBACK_HOST, port=args.port, log_config=None,
                access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
