# Copyright 2026 Amazon Web Services, Inc.
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
"""Persistent, size-bounded vLLM log (spec vllm-jp7-engine-lifecycle,
bugfix.md 1.5 and 2.5).

vLLM logs through its own ``vllm`` logger, which it configures at import
time with a stdout handler and ``propagate=False``: its records never reach
``application.log``. The engine core vLLM forks inherits that logger, so its
records end up only in the container's stdout, and a rollback that recreates
the container loses them. That is how the 2026-10-01 construction hang on
jetson-thor1 left nothing to diagnose ("Loading vLLM model" was the last
line anywhere).

:func:`attach_persistent_vllm_log` adds one ``RotatingFileHandler`` on
``$COMPONENT_WORK_PATH/logs/vllm-engine.log`` (10 MB x 3 backups), the
persistent directory ``application.log`` uses, to the ``vllm`` logger. It
runs right after the first ``import vllm`` (from the manager's default
engine factory), because vLLM's import-time ``dictConfig`` removes any
handler attached to that logger before. vLLM's own stdout handler and its
configuration are left alone, so ``docker logs`` is unchanged. The engine
core is forked after the handler exists, so its records land in the same
file; each record carries the writer's pid.

Records logged while ``vllm`` itself is being imported (platform detection)
predate the handler and stay stdout-only.
"""
import logging
import logging.handlers
import os
import time
from typing import Optional

#: The persistent vLLM log's filename, under ``$COMPONENT_WORK_PATH/logs``.
VLLM_ENGINE_LOG_NAME = "vllm-engine.log"

#: Size cap: 10 MB per file, 3 rotated backups.
VLLM_ENGINE_LOG_MAX_BYTES = 10 * 1024 * 1024
VLLM_ENGINE_LOG_BACKUP_COUNT = 3

#: Logger vLLM logs through.
VLLM_LOGGER_NAME = "vllm"

#: Attribute that marks the handler this module attached.
_MARKER_ATTRIBUTE = "_dda_persistent_vllm_log"

_FORMAT = ("%(asctime)s.%(msecs)03dZ pid=%(process)d %(levelname)s "
           "[%(filename)s:%(lineno)d] %(message)s")
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def default_log_dir() -> Optional[str]:
    """``$COMPONENT_WORK_PATH/logs``, or ``None`` outside a component."""
    work = os.environ.get("COMPONENT_WORK_PATH")
    if not work:
        return None
    return os.path.join(work, "logs")


def persistent_vllm_log_handler(logger: Optional[logging.Logger] = None):
    """The handler this module attached to ``logger``, or ``None``."""
    target = logger if logger is not None else logging.getLogger(VLLM_LOGGER_NAME)
    for handler in target.handlers:
        if getattr(handler, _MARKER_ATTRIBUTE, False):
            return handler
    return None


def attach_persistent_vllm_log(log_dir: Optional[str] = None,
                               logger: Optional[logging.Logger] = None,
                               max_bytes: int = VLLM_ENGINE_LOG_MAX_BYTES,
                               backup_count: int = VLLM_ENGINE_LOG_BACKUP_COUNT):
    """Attach the persistent vLLM log handler once. Returns the handler, or
    ``None`` when there is no log directory or it cannot be created.
    Idempotent, and never raises: logging must not break a model load."""
    target = logger if logger is not None else logging.getLogger(VLLM_LOGGER_NAME)
    existing = persistent_vllm_log_handler(target)
    if existing is not None:
        return existing
    directory = log_dir if log_dir is not None else default_log_dir()
    if not directory:
        return None
    try:
        os.makedirs(directory, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(directory, VLLM_ENGINE_LOG_NAME),
            maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8", delay=True,
        )
    except Exception:  # noqa: BLE001 - never break a load over its log
        logging.getLogger(__name__).warning(
            "Could not open the persistent vLLM log in %s; vLLM keeps logging "
            "to stdout only", directory, exc_info=True)
        return None
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)
    formatter.converter = time.gmtime  # UTC, like application.log's Z stamps
    handler.setFormatter(formatter)
    setattr(handler, _MARKER_ATTRIBUTE, True)
    target.addHandler(handler)
    if target.level == logging.NOTSET or target.level > logging.INFO:
        # vLLM sets its logger to DEBUG; without vLLM's config the INFO
        # records must still reach the handler.
        target.setLevel(logging.INFO)
    logging.getLogger(__name__).info(
        "vLLM log records are also written to %s",
        os.path.join(directory, VLLM_ENGINE_LOG_NAME))
    return handler
