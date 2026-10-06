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
"""The persistent vLLM log (spec vllm-jp7-engine-lifecycle, design change 5;
bugfix.md 1.5, 2.5; Property 6)."""
import logging
import os
import sys
import types
import uuid

from hypothesis import HealthCheck, given, settings, strategies as st

from vllm_runtime import engine_log
from vllm_runtime import manager as manager_module


def _logger():
    logger = logging.getLogger("vllm-test-{}".format(uuid.uuid4().hex))
    logger.propagate = False
    return logger


def _detach(logger):
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_attach_is_idempotent_and_writes_records(tmp_path):
    logger = _logger()
    try:
        first = engine_log.attach_persistent_vllm_log(str(tmp_path), logger=logger)
        second = engine_log.attach_persistent_vllm_log(str(tmp_path), logger=logger)
        assert first is not None and first is second
        assert len(logger.handlers) == 1
        logger.info("weights loaded in %d s", 12)
        first.flush()
        text = (tmp_path / engine_log.VLLM_ENGINE_LOG_NAME).read_text()
        assert "weights loaded in 12 s" in text
        assert "pid={}".format(os.getpid()) in text
        assert "Z pid=" in text  # UTC stamp
    finally:
        _detach(logger)


def test_no_log_dir_attaches_nothing(monkeypatch):
    monkeypatch.delenv("COMPONENT_WORK_PATH", raising=False)
    logger = _logger()
    assert engine_log.attach_persistent_vllm_log(logger=logger) is None
    assert logger.handlers == []


def test_default_dir_is_the_component_logs_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPONENT_WORK_PATH", str(tmp_path))
    assert engine_log.default_log_dir() == str(tmp_path / "logs")


def test_unwritable_dir_is_not_an_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    logger = _logger()
    assert engine_log.attach_persistent_vllm_log(str(blocker / "logs"), logger=logger) is None
    assert logger.handlers == []


def test_a_forked_childs_records_land_in_the_same_file(tmp_path):
    """The engine core is forked after the handler exists, so its records go
    to the same file, tagged with its own pid."""
    logger = _logger()
    try:
        handler = engine_log.attach_persistent_vllm_log(str(tmp_path), logger=logger)
        logger.info("parent before fork")
        handler.flush()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            try:
                logger.info("record from the engine core")
                handler.flush()
            finally:
                os._exit(0)
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        logger.info("parent after the child")
        handler.flush()
        text = (tmp_path / engine_log.VLLM_ENGINE_LOG_NAME).read_text()
        assert "pid={} INFO".format(pid) in text
        assert "record from the engine core" in text
        assert text.index("parent before fork") < text.index("record from the engine core")
        assert text.index("record from the engine core") < text.index("parent after the child")
    finally:
        _detach(logger)


@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(sizes=st.lists(st.integers(min_value=1, max_value=900), min_size=1, max_size=120))
def test_property6_every_record_lands_and_the_file_set_stays_capped(tmp_path, sizes):
    directory = tmp_path / uuid.uuid4().hex
    logger = _logger()
    max_bytes, backups = 2048, 3
    try:
        handler = engine_log.attach_persistent_vllm_log(
            str(directory), logger=logger, max_bytes=max_bytes, backup_count=backups)
        markers = []
        for index, size in enumerate(sizes):
            marker = "rec{:05d}".format(index)
            markers.append(marker)
            logger.info("%s %s", marker, "x" * size)
        handler.flush()
        files = sorted(directory.glob(engine_log.VLLM_ENGINE_LOG_NAME + "*"))
        assert 1 <= len(files) <= backups + 1
        longest_line = max(sizes) + 200
        for path in files:
            assert path.stat().st_size <= max_bytes + longest_line
        # Every record is in the file set unless rotation already dropped
        # the oldest file: the newest records are always there, in order.
        text = "".join(p.read_text() for p in sorted(
            files, key=lambda p: (p.name != engine_log.VLLM_ENGINE_LOG_NAME, p.name),
            reverse=True))
        kept = [m for m in markers if m in text]
        assert kept == markers[len(markers) - len(kept):]
        if len(files) <= backups:  # nothing rotated away yet
            assert kept == markers
    finally:
        _detach(logger)


def test_default_engine_factory_attaches_the_log_before_constructing(monkeypatch):
    """The handler is attached after ``import vllm`` (whose logging config
    would remove it) and before the engine (and its engine core) exists."""
    order = []
    fake_vllm = types.ModuleType("vllm")

    class AsyncEngineArgs:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class AsyncLLMEngine:
        @classmethod
        def from_engine_args(cls, args):
            order.append("construct")
            return ("engine", args.kwargs)

    fake_vllm.AsyncEngineArgs = AsyncEngineArgs
    engine_pkg = types.ModuleType("vllm.engine")
    async_mod = types.ModuleType("vllm.engine.async_llm_engine")
    async_mod.AsyncLLMEngine = AsyncLLMEngine
    monkeypatch.setitem(sys.modules, "vllm", fake_vllm)
    monkeypatch.setitem(sys.modules, "vllm.engine", engine_pkg)
    monkeypatch.setitem(sys.modules, "vllm.engine.async_llm_engine", async_mod)
    monkeypatch.setattr(engine_log, "attach_persistent_vllm_log",
                        lambda *a, **k: order.append("attach"))
    engine = manager_module._default_engine_factory({"model": "m"})
    assert engine == ("engine", {"model": "m"})
    assert order == ["attach", "construct"]
