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
"""Item 2 (input-image jsonl fallback) spec-named case for
localserver-input-handling-hardening that the frozen task-1/2 module does not
pin. It lives in its own module so the frozen file stays byte-identical, and it
imports only Item 2's frozen helpers (``test_input_image_containment``), so
Item 2's tests revert together with Item 2's fix. The lifted route is still the
shipped route compiled with ``ast``.

Covered here:
- Requirement 2.2: a contained but malformed input-image jsonl logs exactly one
  WARNING record carrying ``exc_info`` while answering 500 with the fixed
  detail — that record is now the only diagnostic for the failure.
"""
import logging
import os

import pytest

from fastapi import HTTPException

from test_input_image_containment import (
    _lift as _ii_lift,
    _seed_tree as _ii_seed_tree,
)

_DOWNLOAD_LOGGER = "endpoints.download_file"


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


class TestInputImageContainedFailureLog:
    """Requirement 2.2: a contained read/parse failure logs one WARNING with
    exc_info and answers 500 with the fixed detail."""

    def test_a_contained_malformed_jsonl_logs_one_warning_with_exc_info(
        self, tmp_path
    ):
        inference_root, _image = _ii_seed_tree(tmp_path)
        with open(os.path.join(inference_root, "wf", "bad.jsonl"), "w") as handle:
            handle.write("{not json")
        open_calls = []
        route = _ii_lift(inference_root, open_calls)
        caught = {}

        def _run():
            with pytest.raises(HTTPException) as info:
                route("wf", "bad", token=None, db=None)
            caught["value"] = info.value

        warnings = _records_from(_DOWNLOAD_LOGGER, logging.WARNING, _run)
        assert caught["value"].status_code == 500
        assert caught["value"].detail == "Server cannot find the input image path."
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None
        assert warnings[0].exc_info[0] is not None
