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
"""CUDA-hygiene property suite for ``_reclaim_gpu_memory``
(spec: vllm-jp7-engine-cuda-init).

Property 2 (Preservation) — encoded in this file BEFORE the fix lands,
following observation-first methodology: the behaviors below were
observed on the UNFIXED tree and must remain identical after the fix.

Preservation subtlety (why the fake torch states are shaped the way
they are): the unfixed reclaim gates on ``torch.cuda.is_available()``;
the fixed reclaim gates on ``torch.cuda.is_initialized()``. The
genuinely preserved behavior is the JP6/V0 in-process reality, where
torch CUDA is initialized whenever there is engine memory to reclaim —
i.e. states where BOTH probes return True. Over exactly those states
the property holds on both trees: ``empty_cache()`` runs (the KV-cache
OOM recovery substrate) and every torch error is swallowed.

Property 3 (Fix Checking, added in task 5.1) — the failure handler
never initializes CUDA: over generated fake-torch states whose
``cuda.is_initialized()`` is False (crossed with ``empty_cache``
raising or not, ``is_available`` present or absent, torch importable
or not, and adversarial extra ``cuda`` attributes), the fixed
``_reclaim_gpu_memory`` performs NO CUDA-initializing call
(``is_available`` never invoked; ``empty_cache`` never invoked when
uninitialized) and never raises — so the manager itself can never
re-create a poisoned-parent state, and a load re-attempt after a
failure starts from an uncontaminated parent (hygiene defect 1.3,
optional hardening; spec re-scope 2026-08-15).

Follows the sibling convention of
``test/backend-test/vllm_runtime/test_manager_memory_reclaim.py``:
``sys.path`` shim to ``src/backend``, runnable in the flask-app
container, no real vllm/torch/GPU dependency. Hypothesis: no hardcoded
``max_examples`` (profiles/defaults decide the budget).

Validates: Requirements 2.3, 3.1, 3.2, 3.6
"""
import sys
from contextlib import contextmanager
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src" / "backend"))

from vllm_runtime.manager import VllmRuntimeManager  # noqa: E402

_MISSING = object()


@contextmanager
def _installed_torch(module):
    """Install ``module`` as ``sys.modules['torch']`` for the duration of
    one example (``None`` forces ``import torch`` to raise
    ``ModuleNotFoundError`` — the vLLM-free image reality). Manual
    save/restore instead of ``monkeypatch``: Hypothesis runs many
    examples per test call, so function-scoped fixtures must not carry
    state across examples."""
    saved = sys.modules.get("torch", _MISSING)
    sys.modules["torch"] = module
    try:
        yield
    finally:
        if saved is _MISSING:
            del sys.modules["torch"]
        else:
            sys.modules["torch"] = saved


class _InitializedCuda:
    """Fake ``torch.cuda`` for the JP6/V0 in-process reality: torch CUDA
    IS initialized in this process (an engine lived here), so BOTH the
    unfixed gate (``is_available``) and the fixed gate
    (``is_initialized``) answer True. Records every ``cuda.*`` call."""

    def __init__(self, empty_cache_error=None):
        self.calls = []
        self._empty_cache_error = empty_cache_error

    def is_initialized(self):
        self.calls.append("is_initialized")
        return True

    def is_available(self):
        self.calls.append("is_available")
        return True

    def empty_cache(self):
        self.calls.append("empty_cache")
        if self._empty_cache_error is not None:
            raise self._empty_cache_error

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append(name)
            return None

        return _record


class _FakeTorch:
    """Module-shaped stand-in for the manager's lazy ``import torch``."""

    def __init__(self, cuda):
        self.cuda = cuda


#: Model names as they reach reclaim (used only for log interpolation);
#: printable text keeps log formatting realistic without over-constraining.
_model_names = st.text(
    alphabet=st.characters(codec="ascii", exclude_categories=("Cc",)),
    min_size=1,
    max_size=64,
)

#: torch.cuda.empty_cache() outcomes: succeed, or raise one of the
#: Exception subclasses real torch surfaces (CUDA/runtime/OS-level
#: errors). The reclaim swallow is ``except Exception``, matching what
#: torch can actually raise here.
_empty_cache_errors = st.one_of(
    st.none(),
    st.builds(
        lambda exc_type, message: exc_type(message),
        st.sampled_from([RuntimeError, OSError, ValueError, MemoryError]),
        st.text(max_size=40),
    ),
)


class TestProperty2ReclaimWhenInitializedIdentity:
    """Reclaim-when-initialized identity (the JP6/V0 recovery substrate).

    Observed on the UNFIXED tree with this fake (gate
    ``is_available() = True``): ``empty_cache()`` is called exactly
    once, and when it raises, the error is swallowed — reclaim never
    breaks unload/fail handling. The fixed tree (gate
    ``is_initialized() = True``) must behave identically over these
    states.
    """

    # Validates: Requirements 3.1, 3.6
    @settings(deadline=None)  # gc.collect() inside reclaim; timing varies
    @given(model_name=_model_names, empty_cache_error=_empty_cache_errors)
    def test_reclaim_empties_cache_and_swallows_errors(
            self, model_name, empty_cache_error):
        cuda = _InitializedCuda(empty_cache_error=empty_cache_error)
        with _installed_torch(_FakeTorch(cuda)):
            result = VllmRuntimeManager._reclaim_gpu_memory(model_name)

        assert result is None, "reclaim returns nothing (best-effort)"
        assert cuda.calls.count("empty_cache") == 1, (
            "with torch CUDA initialized in-process, reclaim must call "
            "empty_cache() exactly once (KV-cache OOM recovery substrate, "
            "3.1/3.6); cuda.* calls recorded: {}".format(cuda.calls)
        )
        # No exception propagated (the `with` block completed): every
        # empty_cache() error — raised or not — was swallowed, exactly
        # as observed on the unfixed tree.


class TestProperty2TorchMissingIdentity:
    """Torch-missing identity: on images without torch (JP5, host-side
    portal runs), reclaim returns silently.

    Observed on the UNFIXED tree: the lazy ``import torch`` raises,
    the ``except ImportError: return`` path runs, nothing propagates.
    """

    # Validates: Requirements 3.2
    @settings(deadline=None)  # gc.collect() inside reclaim; timing varies
    @given(model_name=_model_names)
    def test_reclaim_returns_silently_without_torch(self, model_name):
        # sys.modules['torch'] = None makes `import torch` raise
        # ModuleNotFoundError deterministically, independent of the host
        # environment.
        with _installed_torch(None):
            result = VllmRuntimeManager._reclaim_gpu_memory(model_name)

        assert result is None, (
            "with no torch importable, reclaim must return silently "
            "(3.2 — JP5 ships without vLLM/torch)"
        )


#: Attribute names reserved by the fake cuda object itself — adversarial
#: extra attributes must not shadow the probes under test.
_RESERVED_CUDA_ATTRS = frozenset(
    {"calls", "is_initialized", "is_available", "empty_cache"}
)

#: Adversarial extra ``cuda.*`` attribute names: plausible torch.cuda
#: surface the reclaim must never touch when uninitialized.
_extra_cuda_attrs = st.lists(
    st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True).filter(
        lambda name: name not in _RESERVED_CUDA_ATTRS
        and not name.startswith("_")
    ),
    max_size=4,
    unique=True,
)


class _UninitializedCuda:
    """Fake ``torch.cuda`` for a process whose torch CUDA was NEVER
    initialized: ``is_initialized()`` is False. ``is_available`` — THE
    driver-initializing probe — is either present (returning a generated
    bool; on the unfixed tree a True here routed into ``empty_cache``)
    or absent (accessing it raises ``AttributeError``). Records every
    ``cuda.*`` call/access so the property can prove the fixed reclaim
    made no CUDA-initializing call."""

    def __init__(self, empty_cache_error, has_is_available,
                 is_available_result, extra_attrs):
        self.calls = []
        self._empty_cache_error = empty_cache_error
        self._has_is_available = has_is_available
        self._is_available_result = is_available_result
        for name in extra_attrs:
            setattr(self, name, self._make_recorder(name))

    def _make_recorder(self, name):
        def _record(*args, **kwargs):
            self.calls.append(name)
            return None

        return _record

    def is_initialized(self):
        # Pure state read: this process never touched CUDA.
        self.calls.append("is_initialized")
        return False

    def empty_cache(self):
        self.calls.append("empty_cache")
        if self._empty_cache_error is not None:
            raise self._empty_cache_error

    def __getattr__(self, name):
        # Only reached for names not found by normal lookup.
        if name == "is_available":
            calls = object.__getattribute__(self, "calls")
            calls.append("is_available")
            if not object.__getattribute__(self, "_has_is_available"):
                raise AttributeError(name)
            result = object.__getattribute__(self, "_is_available_result")

            def _is_available():
                return result

            return _is_available
        return object.__getattribute__(self, "_make_recorder")(name)


class TestProperty3FixCheckReclaimNeverInitializesCuda:
    """Property 3 (Fix Checking): the failure handler never initializes
    CUDA. For any process whose torch CUDA is NOT initialized — whatever
    else the fake torch looks like — the fixed ``_reclaim_gpu_memory``
    makes no CUDA-initializing call and never raises, so a load
    re-attempt after a failure starts from an uncontaminated parent.
    """

    # Validates: Requirements 2.3
    @settings(deadline=None)  # gc.collect() inside reclaim; timing varies
    @given(
        model_name=_model_names,
        torch_importable=st.booleans(),
        empty_cache_error=_empty_cache_errors,
        has_is_available=st.booleans(),
        is_available_result=st.booleans(),
        extra_attrs=_extra_cuda_attrs,
    )
    def test_reclaim_never_makes_cuda_initializing_call_when_uninitialized(
            self, model_name, torch_importable, empty_cache_error,
            has_is_available, is_available_result, extra_attrs):
        cuda = _UninitializedCuda(
            empty_cache_error=empty_cache_error,
            has_is_available=has_is_available,
            is_available_result=is_available_result,
            extra_attrs=extra_attrs,
        )
        module = _FakeTorch(cuda) if torch_importable else None

        with _installed_torch(module):
            result = VllmRuntimeManager._reclaim_gpu_memory(model_name)

        # Never raises (the `with` block completed) and returns nothing.
        assert result is None, "reclaim returns nothing (best-effort)"

        if not torch_importable:
            # torch missing: the lazy import raised, reclaim returned
            # silently before ever touching cuda.*.
            assert cuda.calls == [], (
                "with torch unimportable, reclaim must not reach any "
                "cuda.* attribute; recorded: {}".format(cuda.calls)
            )
            return

        assert "is_available" not in cuda.calls, (
            "the fixed reclaim must never invoke (or even resolve) "
            "torch.cuda.is_available() — a driver-initializing probe "
            "that poisons the parent backend process (defect 1.3); "
            "cuda.* calls recorded: {}".format(cuda.calls)
        )
        assert "empty_cache" not in cuda.calls, (
            "the fixed reclaim must never call empty_cache() when torch "
            "CUDA is uninitialized in this process (nothing to reclaim "
            "here; on JP7/V1 the engine memory lives in the child); "
            "cuda.* calls recorded: {}".format(cuda.calls)
        )
