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
"""Item 4 (``_failure_reason``) spec-named case for
localserver-input-handling-hardening that the frozen task-1/2 module does not
pin. It lives in its own module so the frozen file stays byte-identical, and it
imports only Item 4's frozen helpers (``test_failure_reason``), so Item 4's
tests revert together with Item 4's fix.

Covered here:
- Requirement 2.4: ``_failure_reason`` for a ``GenerationError`` with a falsy
  reason returns the fixed string and logs one ERROR record.
"""
import logging

from endpoints import text_generation
from vllm_runtime.manager import GenerationError

from test_failure_reason import _FIXED, _MODEL


class _CollectingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _records_from(logger_name, level, run):
    logger = logging.getLogger(logger_name)
    handler = _CollectingHandler()
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        run()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return [record for record in handler.records if record.levelno == level]


class TestFailureReasonEmptyReason:
    """Requirement 2.4: a GenerationError with a falsy reason returns the fixed
    string and logs one ERROR record carrying the error."""

    def test_empty_reason_generation_error_returns_the_fixed_string(self):
        error = GenerationError(_MODEL, "")
        result = {}

        def _run():
            result["value"] = text_generation._failure_reason(error)

        errors = _records_from(text_generation.logger.name, logging.ERROR, _run)
        assert result["value"] == _FIXED
        assert len(errors) == 1
        assert errors[0].exc_info[1] is error
