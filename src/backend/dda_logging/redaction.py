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
"""Log redaction (rtsp-rtmp-stream-cameras Requirements 6.1, 6.3).

:class:`RedactingFilter` masks, in every log record it sees:

- URL user information (``scheme://user:pass@host`` becomes
  ``scheme://***@host``),
- Secret_Query_Parameter values (``password=***``),
- every value held in the Credential_Store,

using the vendored ``stream_url.redact``. It formats the message with its
arguments first and redacts the result, so a secret passed as an argument
is masked too, and it masks a traceback that carries one. It is attached to
handlers, not loggers, because a handler sees the records every logger
propagates to it: the root logger's handlers (``custom_logging``) and the
per-run capture handler (``workflow_engine/run_log.py``).

The filter never raises: a record it cannot redact is replaced by a notice
rather than written unredacted.
"""
import logging
import sys
import threading
from typing import Callable, Iterable, List, Optional

from workflow_engine.vendor.workflow_core.stream_url import redact

#: What a record the filter could not process is replaced with.
UNREDACTABLE_NOTICE = "[log record withheld: it could not be redacted]"

#: The attributes every LogRecord has, plus the ones structlog's
#: ``wrap_for_formatter`` adds. Any other attribute is redacted like the
#: message, because structlog's ``ExtraAdder`` renders it: an ``extra=``
#: field, and the ``message`` a handler that ran earlier (without this
#: filter) left formatted on the record.
_RECORD_ATTRIBUTES = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "taskName", "_logger", "_name", "_from_structlog", "_record"}


def credential_store_secrets() -> List[str]:
    """Every value in the device's Credential_Store; empty when there is no
    store (no ``COMPONENT_WORK_PATH``, as in some tools and tests)."""
    try:
        from stream_ingest.credentials import get_credential_store
        return get_credential_store().secret_values()
    except Exception:  # noqa: BLE001 - redaction of URLs still applies
        return []


class RedactingFilter(logging.Filter):
    """Masks stream credentials in log records (see the module docstring).

    ``secret_values`` returns the literal secrets to mask; it defaults to
    the Credential_Store's values. It is re-read for every record, so a
    newly stored credential is masked from the next record on.
    """

    def __init__(self, secret_values: Optional[Callable[[], Iterable[str]]] = None):
        super().__init__()
        self._secret_values = secret_values or credential_store_secrets
        self._local = threading.local()

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(self._local, "active", False):
            # A record logged while redacting (the store reporting a read
            # error) carries no secret of its own and must not recurse.
            return True
        self._local.active = True
        try:
            self._redact(record)
        except Exception:  # noqa: BLE001 - never write an unredacted record
            record.msg, record.args = UNREDACTABLE_NOTICE, None
            record.exc_info, record.exc_text = None, None
        finally:
            self._local.active = False
        return True

    def _redact(self, record: logging.LogRecord) -> None:
        secrets = [secret for secret in (self._secret_values() or []) if secret]
        if isinstance(record.msg, dict):
            # A structlog event (``ProcessorFormatter.wrap_for_formatter``)
            # stays a dict for its formatter; its values are redacted.
            event = _redact_structure(record.msg, secrets)
            _redact_event_exception(event, secrets)
            record.msg = event
            if record.args:
                record.args = _redact_structure(record.args, secrets)
        else:
            message = record.getMessage()
            redacted = redact(message, secrets)
            if redacted != message or record.args:
                # The formatted message replaces msg + args, so no argument
                # reaches a formatter unredacted.
                record.msg, record.args = redacted, None
        if record.exc_info:
            traceback_text = record.exc_text or logging.Formatter().formatException(record.exc_info)
            redacted_traceback = redact(traceback_text, secrets)
            if redacted_traceback != traceback_text:
                # Formatters render exc_info themselves (structlog ignores
                # exc_text), so the redacted traceback moves into the
                # message (a structlog event's ``exception``) and the
                # exception object is dropped.
                if isinstance(record.msg, dict):
                    record.msg["exception"] = redacted_traceback
                else:
                    record.msg = f"{record.msg}\n{redacted_traceback}"
                record.exc_info, record.exc_text = None, None
        if record.stack_info:
            record.stack_info = redact(record.stack_info, secrets)
        for name, value in list(record.__dict__.items()):
            if name not in _RECORD_ATTRIBUTES and isinstance(value, (str, dict, list, tuple)):
                setattr(record, name, _redact_structure(value, secrets))


def _redact_event_exception(event: dict, secrets) -> None:
    """Redact the exception a structlog event carries as ``exc_info``.

    Its renderer formats ``exc_info`` itself (``True`` meaning the exception
    being handled), after every filter ran, so a traceback that carries a
    secret is rendered here, redacted, and handed over as the ``exception``
    string both structlog renderers print. An event whose traceback needs
    no redaction is left exactly as it was.
    """
    exc_info = event.get("exc_info")
    if not exc_info:
        return
    if isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    elif not isinstance(exc_info, tuple):
        exc_info = sys.exc_info()
    if len(exc_info) != 3 or exc_info[0] is None:
        return
    rendered = logging.Formatter().formatException(exc_info)
    redacted = redact(rendered, secrets)
    if redacted != rendered:
        event.pop("exc_info")
        event["exception"] = redacted


def _redact_structure(value, secrets):
    """``value`` with every string inside dicts, lists and tuples redacted."""
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {key: _redact_structure(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_structure(item, secrets) for item in value)
    return value


_shared_filter: Optional[RedactingFilter] = None
_shared_lock = threading.Lock()


def get_redacting_filter() -> RedactingFilter:
    """The process-wide filter, shared by every handler."""
    global _shared_filter
    with _shared_lock:
        if _shared_filter is None:
            _shared_filter = RedactingFilter()
        return _shared_filter


def install_redaction(handlers: Iterable[logging.Handler]) -> None:
    """Attach the shared filter to each handler once."""
    shared = get_redacting_filter()
    for handler in handlers:
        if not any(isinstance(existing, RedactingFilter) for existing in handler.filters):
            handler.addFilter(shared)
