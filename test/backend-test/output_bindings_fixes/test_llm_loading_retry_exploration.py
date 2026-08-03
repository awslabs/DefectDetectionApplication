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
"""Bug-condition exploration test — case 3: 409-loading treated terminal
(workflow-output-bindings-fixes, Defect B, ``isBugCondition_B``).

Property 2: Bug Condition — llm_inference transient retry.

**This test asserts the FIXED (post-fix) invoker behavior, so it is
EXPECTED TO FAIL on the UNFIXED tree.** The failure is the counterexample
confirming Defect B's first gap: the Text_Generation_API answers
``409 {'model_name': ..., 'state': 'loading'}`` while a model warms up
(``vllm_runtime/server.py`` maps non-READY state to 409 exactly so callers
can distinguish loading from failed), but ``_default_llm_invoker`` treats
every non-200 as terminal and raises immediately — no retry, no waiting for
READY. Observed on live JP6 run 85bf7a61: ``Text_Generation_API returned
409 for model 'opt125m-smoke': {'model_name': 'opt125m-smoke', 'state':
'loading'}``.

Expected counterexample on the UNFIXED tree:
    RuntimeError raised on the FIRST 409-loading response (the exact live
    message above); the queued 200 with the generated text is never reached.

The SAME test is re-run in task 3.4 against the fixed invoker (409-loading
re-POSTed every LLM_LOADING_POLL_INTERVAL_SEC within LLM_LOADING_BUDGET_SEC;
first 200 wins), where it must PASS.

The HTTP boundary is faked in ``sys.modules`` (the invoker imports
``requests`` lazily) and ``time.sleep`` is stubbed so no poll interval
really elapses.

Validates: Requirements 1.4 (expected behavior 2.3)
"""
import sys
import types
from unittest.mock import patch

from workflow_engine.output_bindings import _default_llm_invoker

MODEL = "opt125m-smoke"

#: The live-device warming payload (execution 85bf7a61).
LOADING_PAYLOAD = {"model_name": MODEL, "state": "loading"}
GENERATED = "ok"


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _fake_requests(responses):
    """A fake ``requests`` module returning ``responses`` in order and
    recording each POST."""
    queue = list(responses)
    posts = []
    module = types.ModuleType("requests")

    def post(url, json=None, timeout=None):
        posts.append({"url": url, "json": json, "timeout": timeout})
        if not queue:
            raise AssertionError(
                "unexpected extra POST #{0} — the response queue is "
                "exhausted".format(len(posts)))
        return queue.pop(0)

    module.post = post
    module.calls = posts
    return module


def test_409_loading_is_retried_until_the_model_is_ready():
    """isBugCondition_B: the API answers 409 state='loading' twice (model
    warming), then 200 with the generated text. The fixed invoker retries
    within the bounded budget and returns the text.

    EXPECTED FAILURE on the unfixed tree: RuntimeError on the first 409 —
    "Text_Generation_API returned 409 for model 'opt125m-smoke':
    {'model_name': 'opt125m-smoke', 'state': 'loading'}" (the exact
    live-device message), the model's warm-up never awaited.

    Validates: Requirements 1.4 (expected behavior 2.3)
    """
    fake = _fake_requests([
        _Response(409, dict(LOADING_PAYLOAD)),
        _Response(409, dict(LOADING_PAYLOAD)),
        _Response(200, {"generated_text": GENERATED}),
    ])

    # Stub the poll sleep so the loading window costs no wall clock; the
    # unfixed invoker never sleeps (it raises first), the fixed one polls.
    with patch.dict(sys.modules, {"requests": fake}), \
            patch("time.sleep", lambda seconds: None):
        result = _default_llm_invoker(
            MODEL, "Describe the inspection result", {"max_tokens": 64})

    assert result == GENERATED, (
        "COUNTEREXAMPLE (Defect B): the invoker returned {0!r} instead of "
        "the post-warm-up generated text {1!r}".format(result, GENERATED))
    assert len(fake.calls) == 3, (
        "expected 3 POSTs (409-loading, 409-loading, 200); saw {0}"
        .format(len(fake.calls)))
