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
"""Property test for the Prefix_Caching engine-args passthrough and the
load-time application log line (task 2.4).

# Feature: vllm-workflow-latency-optimization, Property 8: Prefix_Caching passthrough and load-time logging

*For any* engine-arguments mapping, the manager SHALL hand the mapping to
the engine factory unchanged (key-for-key, value-for-value), and SHALL
write the prefix-caching load log entry naming the model if and only if
``enable_prefix_caching`` is truthy in the mapping — falsy or absent
constructs the engine with pre-feature behavior and no such log entry.

**Validates: Requirements 4.1, 4.6**

The test hypothesis-generates JSON-serializable engine-arguments mappings
(nested JSON values, plus an optional ``enable_prefix_caching`` entry with
truthy/falsy values of several types), stages each one as a model
component's ``1/model.json``, drives ``VllmRuntimeManager.load`` with an
injected engine factory that snapshots the exact mapping it receives, and
asserts:

* the factory was invoked exactly once with the parsed model.json mapping
  unchanged key-for-key, value-for-value (compared through a
  type-preserving JSON serialization, so ``True`` vs ``1`` and ``0`` vs
  ``0.0`` cannot silently pass);
* exactly one INFO "Prefix_Caching ... active" application-log line naming
  the model appears iff ``enable_prefix_caching`` is truthy; otherwise no
  such line appears at any level.

Generated keys avoid the memory-preflight-sensitive engine-argument names
(``model``, ``gpu_memory_utilization``, ``max_model_len``,
``limit_mm_per_prompt``) and a generous fake ``/proc/meminfo`` reader is
injected, so no generated mapping can be refused by the preflight — every
load reaches READY through the same path the harness in
``test/backend-test/vllm_runtime/test_prefix_caching_load_log_units.py``
exercises.

Hypothesis and the pytest ``caplog`` fixture do not mix (function-scoped
fixtures are reused across examples), so log records are captured with a
``logging.Handler`` attached inside the test body per example.
"""
import asyncio
import json
import logging
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from vllm_runtime.manager import ModelState, VllmRuntimeManager

#: Generous fake /proc/meminfo so the device memory preflight always
#: clears, whatever the generated engine arguments say (1 TiB free).
_MEMINFO = "MemTotal:       1073741824 kB\nMemAvailable:   1073741824 kB\n"

#: Engine-argument keys the memory preflight inspects. The generator
#: never produces them, so a generated mapping cannot change the
#: preflight verdict; ``enable_prefix_caching`` is excluded here because
#: the flag strategy injects it deliberately.
_EXCLUDED_KEYS = frozenset({
    "model",
    "gpu_memory_utilization",
    "max_model_len",
    "limit_mm_per_prompt",
    "enable_prefix_caching",
})

#: Sentinel for "no enable_prefix_caching entry at all".
_ABSENT = object()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_SAFE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FF),
    max_size=12,
)

_KEYS = _SAFE_TEXT.filter(lambda key: key not in _EXCLUDED_KEYS)

_JSON_VALUES = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-10**9, max_value=10**9),
        st.floats(allow_nan=False, allow_infinity=False),
        _SAFE_TEXT,
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(_SAFE_TEXT, children, max_size=3),
    ),
    max_leaves=8,
)

_ENGINE_ARGS = st.dictionaries(_KEYS, _JSON_VALUES, max_size=6)

#: Truthy and falsy enable_prefix_caching values of several JSON types.
#: Note "false" and "0" are truthy strings — the manager tests bool(value),
#: exactly what R4.1's WHERE clause means by "enable Prefix_Caching".
_TRUTHY_FLAGS = st.sampled_from(
    [True, 1, 2, -1, 3.5, "true", "false", "yes", "0", [0], {"on": False}])
_FALSY_FLAGS = st.sampled_from([False, 0, 0.0, "", None, [], {}])

_FLAG = st.one_of(st.just(_ABSENT), _TRUTHY_FLAGS, _FALSY_FLAGS)

_MODEL_NAMES = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_",
    min_size=1,
    max_size=16,
)


# ---------------------------------------------------------------------------
# Harness (adapted from vllm_runtime/test_prefix_caching_load_log_units.py)
# ---------------------------------------------------------------------------

def _stage_repository(model_dir: Path, model_name: str,
                      engine_args: dict) -> None:
    version_dir = model_dir / model_name / "1"
    version_dir.mkdir(parents=True)
    (model_dir / model_name / "config.pbtxt").write_text('backend: "vllm"\n')
    (version_dir / "model.json").write_text(json.dumps(engine_args))


class _RecordCollector(logging.Handler):
    """Collects every record emitted through the manager's logger —
    attached inside the test body because caplog and hypothesis do not
    mix across examples."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _load_recording(model_name: str, engine_args: dict):
    """Stage ``engine_args`` as the model.json, load through a factory
    that snapshots its argument, and return (status, factory_snapshots,
    log_records)."""
    factory_snapshots = []

    def engine_factory(args):
        # Type-preserving snapshot at call time (True != 1, 0 != 0.0 here).
        factory_snapshots.append(json.dumps(args, sort_keys=True))
        return object()

    collector = _RecordCollector()
    manager_logger = logging.getLogger("vllm_runtime.manager")
    previous_level = manager_logger.level
    manager_logger.addHandler(collector)
    manager_logger.setLevel(logging.DEBUG)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp)
            _stage_repository(model_dir, model_name, engine_args)
            manager = VllmRuntimeManager(
                model_dir=model_dir,
                engine_factory=engine_factory,
                sampling_params_factory=dict,
                memory_reader=lambda: _MEMINFO,
            )
            status = asyncio.run(manager.load(model_name))
    finally:
        manager_logger.removeHandler(collector)
        manager_logger.setLevel(previous_level)
    return status, factory_snapshots, collector.records


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

# Feature: vllm-workflow-latency-optimization, Property 8: Prefix_Caching passthrough and load-time logging
@settings(max_examples=100, deadline=None)
@given(model_name=_MODEL_NAMES, base_args=_ENGINE_ARGS, flag=_FLAG)
def test_property_8_prefix_caching_passthrough_and_load_logging(
        model_name, base_args, flag):
    """**Feature: vllm-workflow-latency-optimization, Property 8:
    Prefix_Caching passthrough and load-time logging**

    **Validates: Requirements 4.1, 4.6**
    """
    engine_args = dict(base_args)
    if flag is not _ABSENT:
        engine_args["enable_prefix_caching"] = flag

    # Independent oracle: what parse_repository will hand the manager is
    # exactly the staged JSON round-tripped.
    expected_args = json.loads(json.dumps(engine_args))
    expected_log = bool(expected_args.get("enable_prefix_caching"))

    status, factory_snapshots, records = _load_recording(
        model_name, engine_args)

    # The load reached READY through the injected factory.
    assert status.state is ModelState.READY

    # Passthrough: the factory was invoked exactly once, with the parsed
    # model.json mapping unchanged key-for-key, value-for-value (R4.6 —
    # pre-feature construction path; R4.1 — same passthrough when the
    # flag is set).
    assert len(factory_snapshots) == 1
    assert factory_snapshots[0] == json.dumps(expected_args, sort_keys=True)

    # Load log entry iff enable_prefix_caching is truthy (R4.1, R4.6).
    prefix_records = [record for record in records
                      if "Prefix_Caching" in record.getMessage()]
    if expected_log:
        assert len(prefix_records) == 1
        record = prefix_records[0]
        assert record.levelno == logging.INFO
        message = record.getMessage()
        assert model_name in message
        assert "Prefix_Caching" in message
        assert "active" in message
    else:
        assert prefix_records == []
