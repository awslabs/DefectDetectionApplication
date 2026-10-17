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
"""Exploration, Property 4 (triton-model-cleanup-wedge, task 2.2).

The per-run gate sees an ``UNKNOWN`` model whose repository entry is still
changing (a step's ``model.py`` was written 2 s ago). The fixed gate must
not request a load until the entry has been unchanged for 10 s. On the
unfixed code it requests the load on its first poll, which is the load the
wedge needs (bugfix.md 1.3).

_Requirements: 1.3, 2.3_
"""
import os

from dda_triton import model_readiness

from triton_cleanup_wedge.fakes import (
    STATE_UNKNOWN,
    FakeTime,
    FakeTritonClient,
    set_age,
    write_entry,
)

MODEL = "ppe-detector"
TARGET = "model-explore-gate-jetson-xavier-jp5"


def test_the_gate_requests_no_load_for_an_entry_that_is_still_changing(tmp_path, monkeypatch):
    repo = str(tmp_path / "dda_triton" / "triton_model_repo")
    write_entry(repo, TARGET)
    set_age(os.path.join(repo, "base_" + TARGET, "1", "model.py"), 2.0)
    monkeypatch.setattr(model_readiness, "_repo_has_models", lambda: True)
    monkeypatch.setattr(model_readiness, "TRITON_MODEL_DIR", repo, raising=False)
    client = FakeTritonClient([STATE_UNKNOWN])
    fake = FakeTime()

    outcome = model_readiness.ensure_model_ready(
        MODEL, TARGET, client=client, sleep=fake.sleep, clock=fake.monotonic,
        timeout_s=6.0, poll_interval_s=3.0)

    assert client.start_calls == [], (
        "a load was requested for an entry whose model.py changed 2 s ago: {0}".format(
            client.start_calls))
    assert outcome.ready is False
