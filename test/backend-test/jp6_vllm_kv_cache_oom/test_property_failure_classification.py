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
"""Fix-checking properties, DEVICE half (spec:
jp6-vllm-kv-cache-oom-regression).

FILE SCOPE. This file is shared by two tasks, each owning its own clearly
scoped section:

- **Task 4.3 — Property 4 (device half), the class
  ``TestProperty4MultimodalLimit``**: the multimodal limit is authored,
  staged verbatim, and enforced at request time.
- **Task 4.6 — Property 7, the classes
  ``TestProperty7FailureClassification``, ``TestProperty7ThinMargin`` and
  ``TestProperty7NoNewStatusSurface``**: distinguishable symptoms and
  visible thin margins, inside the existing surfaces only.

**Property 4: Bug Condition — the multimodal limit is authored and
budgeted** (design "Correctness Properties"). _For any_ staged engine args,
the fixed runtime SHALL NOT inject a ``limit_mm_per_prompt`` value that the
authored configuration did not specify; and a two-image request against a
model authored for one image SHALL fail with a diagnostic naming the limit
and the remediation rather than silently using one image — while a model
authored with ``{"image": 2}`` keeps building the two-image reference
prompt exactly as today (preservation 3.9).

**Property 7: Bug Condition — distinguishable symptoms and visible thin
margins** (design "Correctness Properties", Decision 6). _For any_ failure
reason, the fixed manager SHALL prepend exactly one stable category token
(``kv-cache-exhaustion``, ``allocator-nvml-fault``, ``preflight-refused``,
``repository-invalid``, ``engine-construction-error``) while preserving the
original reason text verbatim so existing marker matching still works; and
_for any_ engine whose post-load KV sizing is readable and below the floor
or the thin-margin concurrency, it SHALL log a WARNING naming the margin
rather than reporting an unqualified success. One RECORDED deviation binds
the "for any failure reason" half — see COLLISION A in
``TestProperty7FailureClassification``'s docstring.

OPEN QUESTION (binding honesty note, design Decision 6). The NVML
allocator INTERNAL ASSERT's **root cause stays an open question** — whether
it is the same exhaustion seen from the allocator or a distinct CUDA/NVML
fault is UNRESOLVED. This file proves REPORTING DISTINGUISHABILITY only
(the two symptoms carry different tokens); the determination is
**[HARDWARE] H7** (task 14) and no test here may claim it.

HONESTY GUARD (binding). No real vLLM engine, no GPU allocation, no
CUDA/NVML, no Jetson unified-memory simulation: the engine is the manager's
public ``engine_factory`` seam, memory readings are an injected fake
``/proc/meminfo`` reader, KV sizing is a fake ``cache_config`` object, and
the images are real tiny PNGs decoded by the real PIL. These tests prove
decision logic, message content and classification — nothing about a GPU.
The GPU-only claims are the [HARDWARE] H1-H8 tasks'.

Hypothesis conventions for the device suites (``--noconftest``, so no
profile is registered): ``@settings(deadline=None)`` with **no hardcoded
``max_examples``**, matching
``test/backend-test/vllm_model_reload/test_property_*.py``.

Run (host-side, from the repo root):
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
      test/backend-test/jp6_vllm_kv_cache_oom/test_property_failure_classification.py \
      -q -p no:cacheprovider --noconftest

# Validates: Requirements 2.4, 3.9 (Property 4) and 2.6, 2.7 (Property 7)
"""
import asyncio
import contextlib
import dataclasses
import itertools
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

import dda_triton.vllm_model_prep as mp
from vllm_runtime.manager import (
    ALLOCATOR_NVML_FAULT_TOKEN,
    ENGINE_CONSTRUCTION_ERROR_TOKEN,
    FAILURE_CATEGORY_TOKENS,
    KV_CACHE_EXHAUSTION_TOKEN,
    REPOSITORY_INVALID_TOKEN,
    UNCLASSIFIED_FAILURE_TOKEN,
    GenerationError,
    ModelState,
    ModelStatus,
    classify_failure,
    classify_failure_reason,
)
from vllm_runtime.server import _status_payload, create_app
from vllm_runtime.memory_budget import (
    MINIMUM_KV_CACHE_BYTES,
    PREFLIGHT_REFUSED_MARKER,
    THIN_MARGIN_CONCURRENCY,
    format_gib,
)
from jp6_vllm_kv_cache_oom.fakes import (
    DEFAULT_MODEL_NAME,
    DEVICE_TOTAL_BYTES,
    GENERATED_TEXT,
    GIB,
    INCIDENT_ENGINE_ARGS,
    KV_OOM_REASON,
    NVML_ASSERT_REASON,
    FailingEngineFactory,
    FakeMeminfoReader,
    RecordingEngineFactory,
    build_staged_repo,
    healthy_cache_config,
    make_manager,
    png_bytes,
    thin_cache_config,
    weight_tree,
)

# ---------------------------------------------------------------------------
# Generator: a staged model.json whose limit_mm_per_prompt presence/absence
# is an explicit part of every example (so the iff-clause is exercised on
# both sides)
# ---------------------------------------------------------------------------

_dtypes = st.sampled_from(("auto", "float16", "bfloat16", "float32"))
_utilizations = st.floats(min_value=0.05, max_value=1.0,
                          allow_nan=False, allow_infinity=False
                          ).map(lambda x: round(x, 3))


@st.composite
def staged_args_cases(draw):
    """(engine_args, authored_images) — a staged ``model.json`` object:
    the ``model`` reference plus an arbitrary subset of the five
    pre-existing settings, and — drawn explicitly — an OPTIONAL authored
    ``limit_mm_per_prompt`` (``authored_images`` is None when the staged
    args omit the key)."""
    args = {"model": draw(st.sampled_from((
        "Qwen/Qwen2.5-VL-7B-Instruct-AWQ",
        "example/small-llm",
        "/aws_dda/weights/model-x",
    )))}
    if draw(st.booleans()):
        args["dtype"] = draw(_dtypes)
    if draw(st.booleans()):
        args["gpu_memory_utilization"] = draw(_utilizations)
    if draw(st.booleans()):
        args["max_model_len"] = draw(st.integers(min_value=256,
                                                 max_value=32768))
    if draw(st.booleans()):
        args["tensor_parallel_size"] = draw(st.integers(min_value=1,
                                                        max_value=4))
    if draw(st.booleans()):
        args["enforce_eager"] = draw(st.booleans())
    authored_images = draw(st.one_of(
        st.none(), st.integers(min_value=1, max_value=8)))
    if authored_images is not None:
        args["limit_mm_per_prompt"] = {"image": authored_images}
    return args, authored_images


# ---------------------------------------------------------------------------
# Task 4.3 — Property 4 (device half)
# ---------------------------------------------------------------------------

class TestProperty4MultimodalLimit:
    """The multimodal limit is authored, staged verbatim, and enforced at
    request time (design Property 4; task 4.3 device half)."""

    # Validates: Requirements 2.4, 3.9
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(case=staged_args_cases())
    def test_property_engine_args_carry_the_limit_iff_the_staged_args_did(
            self, tmp_path, case):
        """**Property 4 (iff clause)**: for any staged ``model.json``, the
        recorded engine args after ``manager.load`` contain
        ``limit_mm_per_prompt`` ONLY when the staged args did — and when
        they did, verbatim. On the unfixed tree the ``setdefault`` injected
        ``{"image": 2}`` whenever the staged args omitted the key (defect
        1.4); the fixed load path defaults NOTHING into the engine args.

        # Validates: Requirements 2.4, 3.9
        """
        engine_args, authored_images = case
        model_dir = tmp_path / "repo-{}".format(abs(hash(json.dumps(
            engine_args, sort_keys=True))))
        build_staged_repo(model_dir, engine_args=engine_args)
        factory = RecordingEngineFactory()
        manager = make_manager(model_dir, factory)

        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

        assert status.state is ModelState.READY, status.reason
        assert factory.call_count == 1
        recorded = factory.calls[0]
        if authored_images is None:
            assert "limit_mm_per_prompt" not in recorded, (
                "the runtime injected a multimodal limit the staged "
                "model.json never authored: {!r}".format(
                    recorded.get("limit_mm_per_prompt")))
        else:
            assert recorded["limit_mm_per_prompt"] == {
                "image": authored_images}, (
                "the authored limit was rewritten: staged {{'image': {}}} "
                "-> engine {!r}".format(
                    authored_images, recorded.get("limit_mm_per_prompt")))
        # The manager's tracked args (what request validation reads) agree.
        tracked = manager.engine_args(DEFAULT_MODEL_NAME)
        if authored_images is None:
            assert "limit_mm_per_prompt" not in tracked, tracked
        else:
            assert tracked["limit_mm_per_prompt"] == {
                "image": authored_images}, tracked

    # Validates: Requirements 2.4
    def test_reference_request_against_a_one_image_model_is_refused_before_the_engine(
            self, tmp_path):
        """**Property 4 (enforcement clause)**: a reference-image request
        against a model authored ``limit_mm_per_prompt = {"image": 1}``
        raises ``GenerationError`` naming the model, the effective limit
        and the remediation — BEFORE the engine is invoked. The reference
        image is never silently dropped: a one-image answer would be a
        confident verdict about a different question.

        # Validates: Requirements 2.4
        """
        engine_args = dict(INCIDENT_ENGINE_ARGS,
                           limit_mm_per_prompt={"image": 1})
        build_staged_repo(tmp_path, engine_args=engine_args)
        factory = RecordingEngineFactory()
        manager = make_manager(tmp_path, factory)
        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
        assert status.state is ModelState.READY, status.reason

        with pytest.raises(GenerationError) as excinfo:
            asyncio.run(manager.generate(
                DEFAULT_MODEL_NAME,
                "Is the part defective compared with the reference?",
                image=png_bytes(),
                reference_image=png_bytes(color=(30, 200, 30)),
            ))

        message = str(excinfo.value)
        assert DEFAULT_MODEL_NAME in message, (
            "the failure does not name the model: {!r}".format(message))
        assert "limit_mm_per_prompt" in message and "1" in message, (
            "the failure does not name the effective limit: "
            "{!r}".format(message))
        assert "re-publish" in message or "engine configuration" in message, (
            "the failure carries no remediation: {!r}".format(message))
        # BEFORE the engine is invoked: the engine saw no prompt at all.
        assert factory.engines[0].prompts == [], (
            "the engine was invoked with a two-image prompt the model is "
            "not sized for: {!r}".format(factory.engines[0].prompts))

    # Validates: Requirements 3.9
    def test_model_authored_for_two_images_builds_the_two_image_prompt_as_today(
            self, tmp_path):
        """**Property 4 (preservation clause, 3.9)**: a model authored with
        ``{"image": 2}`` (and sized for it by the publish-time Fit_Check)
        keeps serving two-image anomaly-reference requests exactly as
        today: the labelled two-pad prompt text with
        ``multi_modal_data['image']`` as the two-element list in
        input-then-reference order, and a normal completion.

        # Validates: Requirements 3.9
        """
        engine_args = dict(INCIDENT_ENGINE_ARGS,
                           limit_mm_per_prompt={"image": 2})
        build_staged_repo(tmp_path, engine_args=engine_args)
        factory = RecordingEngineFactory()
        manager = make_manager(tmp_path, factory)
        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
        assert status.state is ModelState.READY, status.reason

        result = asyncio.run(manager.generate(
            DEFAULT_MODEL_NAME,
            "Is the part defective compared with the reference?",
            image=png_bytes(size=(8, 8)),
            reference_image=png_bytes(size=(16, 16), color=(30, 200, 30)),
        ))

        assert result == GENERATED_TEXT
        # The authored limit reached the engine verbatim.
        assert factory.calls[0]["limit_mm_per_prompt"] == {"image": 2}
        # The two-image prompt, exactly as today (3.9): labelled two-pad
        # text plus the two-element image list, input first.
        prompt = factory.engines[0].prompts[0]
        assert isinstance(prompt, dict), prompt
        assert "Input image:" in prompt["prompt"], prompt["prompt"]
        assert "Reference image:" in prompt["prompt"], prompt["prompt"]
        assert "Is the part defective" in prompt["prompt"], prompt["prompt"]
        images = prompt["multi_modal_data"]["image"]
        assert isinstance(images, list) and len(images) == 2, images
        assert images[0].size == (8, 8), (
            "the input image must come first: {!r}".format(
                [image.size for image in images]))
        assert images[1].size == (16, 16), (
            "the reference image must come second: {!r}".format(
                [image.size for image in images]))


# ===========================================================================
# Task 4.6 — Property 7: distinguishable symptoms and visible thin margins
# ===========================================================================

#: Per-example unique directory names (tmp_path is function-scoped while
#: Hypothesis re-enters the test body many times) — the sibling device
#: suites' pattern.
_dir_counter = itertools.count()

#: A generous injected reading (the fixed manager reads /proc/meminfo on
#: every load): plenty available, so the preflight never interferes with
#: the classification / thin-margin paths under test.
_GENEROUS_READINGS = [(DEVICE_TOTAL_BYTES, 23 * GIB)]

#: Engine args whose ``model`` is NOT on disk: the weights are
#: undeterminable, so the preflight's verified-arithmetic arms never
#: enforce and every load reaches engine construction — isolating the
#: classifier and the thin-margin check from the budget math (task 4.4's
#: territory).
_UNSIZABLE_ARGS = dict(INCIDENT_ENGINE_ARGS,
                       model="example/never-pulled-model")

#: Bytes of KV cache per token under ``fakes.fake_model_config``'s KV
#: geometry: 28 layers x 4 KV heads x 128 head size x 2 tensors (K and V)
#: x 2 bytes (fp16/bf16) — the same arithmetic the manager's
#: ``_kv_bytes_from_geometry`` computes.
_KV_BYTES_PER_TOKEN = 28 * 4 * 128 * 2 * 2

#: The byte-identical READY line (preservation: Decision 6 adds a WARNING
#: beside it, never a changed line).
_READY_LINE = "vLLM model '{}' is READY".format(DEFAULT_MODEL_NAME)

#: The one debug line an unreadable engine shape produces.
_NOT_READABLE_SIGNATURE = "no thin-margin check was performed"

#: The thin-margin WARNING's signature.
_THIN_MARGIN_SIGNATURE = "THIN KV-CACHE MARGIN"

#: Case-insensitive signatures, mirrored from the manager for the
#: generator (the manager's own `_KV_CACHE_SIGNATURES` /
#: `_ALLOCATOR_NVML_SIGNATURES` are private; these literals are pinned
#: here so a silent signature change fails this suite loudly).
_KV_SIGNATURES = (
    "no available memory for the cache",
    "memory for the cache blocks",
    "gpu_memory_utilization",
)
_NVML_SIGNATURES = (
    "nvml_success",
    "cudacachingallocator",
)


@contextlib.contextmanager
def _collected_logs(level=logging.DEBUG):
    """Collect every log record emitted in the block (root handler + root
    level lowered so DEBUG/INFO/WARNING pass the effective-level check),
    restoring the previous configuration afterwards. Per-example safe."""
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    collector = _Collector()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(collector)
    root.setLevel(level)
    try:
        yield records
    finally:
        root.setLevel(previous_level)
        root.removeHandler(collector)


def _messages(records, level=None, source="vllm_runtime.manager"):
    return [record.getMessage() for record in records
            if (level is None or record.levelno == level)
            and record.name == source]


def _token_count_at_start(text):
    """How many category tokens ``text`` starts with (peeling them one at
    a time) — the no-double-prefix oracle. "Exactly one" means 1."""
    count = 0
    remainder = text
    while True:
        stripped = remainder.strip()
        match = next((token for token in FAILURE_CATEGORY_TOKENS
                      if stripped.startswith(token)), None)
        if match is None:
            return count
        count += 1
        remainder = stripped[len(match):]


def _build_manager(tmp_path, factory, engine_args=None, readings=None):
    """A manager over a freshly staged repo (per-example unique dir) with
    a generous injected memory reading, so only the code path under test
    decides the outcome."""
    repo = tmp_path / "repo-{}".format(next(_dir_counter))
    build_staged_repo(repo, engine_args=engine_args or _UNSIZABLE_ARGS)
    reader = FakeMeminfoReader(readings or _GENEROUS_READINGS)
    return make_manager(repo, factory, memory_reader=reader), repo


# Reason-text alphabet for the generated failure reasons: no ':' and no
# '-' pair can spell a category token, so a generated reason never starts
# with one by accident; signature matching is case-insensitive substring,
# which the oracle below replicates faithfully.
_reason_text = st.text(
    alphabet=" abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "0123456789.,()`'\"\n\t",
    max_size=80,
)


@st.composite
def recognized_reasons(draw):
    """(reason, expected_token) — arbitrary text around ONE embedded
    category signature, in a drawn letter case. The expected token is
    computed with the design's precedence (allocator/NVML is checked
    FIRST, so the assert never reads as a budget fault)."""
    prefix = draw(_reason_text)
    suffix = draw(_reason_text)
    signature = draw(st.sampled_from(_KV_SIGNATURES + _NVML_SIGNATURES))
    transform = draw(st.sampled_from(
        (str.lower, str.upper, str.title, lambda s: s)))
    reason = prefix + transform(signature) + suffix
    lowered = reason.lower()
    if any(s in lowered for s in _NVML_SIGNATURES):
        expected = ALLOCATOR_NVML_FAULT_TOKEN
    else:
        expected = KV_CACHE_EXHAUSTION_TOKEN
    return reason, expected


@st.composite
def unrecognized_reasons(draw):
    """Reason text carrying NO category signature and NO leading token."""
    reason = draw(_reason_text).lower()
    signatures = _KV_SIGNATURES + _NVML_SIGNATURES
    # Scrub until provably signature-free (a replace can, in principle,
    # splice a new match together).
    while any(signature in reason for signature in signatures):
        for signature in signatures:
            reason = reason.replace(signature, "")
    return reason


class TestProperty7FailureClassification:
    """**Property 7, classification half** — _for any_ failure reason the
    fixed manager prepends **exactly one** stable category token with the
    original reason text preserved **verbatim**, and classification is
    idempotent-safe (no double prefixing).

    COLLISION A — RECORDED DEVIATION (task 3.6; binding here). Design
    Property 7 names ``engine-construction-error:`` as the token for an
    unrecognised failure reason. The fixed manager DELIBERATELY leaves an
    unrecognised reason RAW (``UNCLASSIFIED_FAILURE_TOKEN == ""``): the
    sibling spec ``vllm-model-reload-after-backend-restart`` — preserved
    verbatim by Requirement 3.7 — pins the retained FAILED reason to the
    BACKEND text exactly in five validated legs, and prefixing every
    unrecognised reason breaks them. Nothing distinguishable is lost: the
    categories that make defect 1.6's symptoms tellable-apart
    (``kv-cache-exhaustion:`` vs ``allocator-nvml-fault:``, plus
    ``preflight-refused:`` and ``repository-invalid:``) are all recognised
    and tokenized, and ``engine-construction-error:`` remains a defined
    member of ``FAILURE_CATEGORY_TOKENS`` that callers may pass
    explicitly. This class tests THAT contract — the "for any failure
    reason" quantifier binds through the ``default_token`` parameter, not
    through an unconditional prefix.
    """

    # Validates: Requirements 2.6
    @settings(deadline=None)
    @example(case=(KV_OOM_REASON, KV_CACHE_EXHAUSTION_TOKEN))
    @example(case=(NVML_ASSERT_REASON, ALLOCATOR_NVML_FAULT_TOKEN))
    # Leading/trailing whitespace must survive verbatim after the token
    # (the prep and reconciler match substrings of the retained reason).
    @example(case=("  " + KV_OOM_REASON + " \n",
                   KV_CACHE_EXHAUSTION_TOKEN))
    @given(case=recognized_reasons())
    def test_property_recognized_reason_gains_exactly_one_token_verbatim(
            self, case):
        """**P7-A**: for any reason carrying a KV-cache or allocator/NVML
        signature, ``classify_failure`` prepends exactly ONE stable
        category token and the original text follows it byte for byte
        (whitespace included), so every existing substring consumer keeps
        matching.

        # Validates: Requirements 2.6
        """
        reason, expected_token = case
        assert classify_failure_reason(reason) == expected_token

        classified = classify_failure(reason)
        assert classified == "{} {}".format(expected_token, reason), (
            "the original reason text did not survive verbatim after the "
            "token: {!r} -> {!r}".format(reason, classified))
        assert _token_count_at_start(classified) == 1, (
            "expected exactly one category token, found {} in {!r}".format(
                _token_count_at_start(classified), classified))

    # Validates: Requirements 2.6
    @settings(deadline=None)
    # A whitespace-only reason must not be stripped (the sibling suite
    # generates one against the retained-reason contract).
    @example(reason=" ", leading_token=None, default_token="")
    @example(reason=KV_OOM_REASON, leading_token=None, default_token="")
    # The preflight's own composed reason arrives already tokenized and
    # must never gain a second token.
    @example(reason="the device memory preflight refused this load",
             leading_token=PREFLIGHT_REFUSED_MARKER, default_token="")
    @given(reason=_reason_text,
           leading_token=st.sampled_from((None,) + FAILURE_CATEGORY_TOKENS),
           default_token=st.sampled_from(
               ("", REPOSITORY_INVALID_TOKEN,
                ENGINE_CONSTRUCTION_ERROR_TOKEN,
                KV_CACHE_EXHAUSTION_TOKEN)))
    def test_property_classification_is_idempotent_never_double_prefixed(
            self, reason, leading_token, default_token):
        """**P7-B**: classification is idempotent-safe. For any reason —
        raw, or already carrying ANY of the five category tokens
        (including ``preflight-refused:``, composed upstream by
        ``memory_budget``) — a second classification under ANY default
        changes nothing, and the result never starts with two tokens.

        # Validates: Requirements 2.6
        """
        text = ("{} {}".format(leading_token, reason)
                if leading_token else reason)

        once = classify_failure(text, default_token)
        assert _token_count_at_start(once) <= 1, (
            "double prefix after one classification: {!r}".format(once))
        if leading_token:
            assert once == text, (
                "an already-tokenized reason was rewritten: "
                "{!r} -> {!r}".format(text, once))

        # Idempotent under the SAME default...
        assert classify_failure(once, default_token) == once, (
            "classification is not idempotent: {!r} -> {!r}".format(
                once, classify_failure(once, default_token)))
        # ...and once a token is present the reason is a FIXED POINT
        # under ANY default; a reason still raw after classification
        # (the COLLISION A default) may gain its one token from a later
        # explicit category, but NEVER a second one.
        for second_default in ("",) + FAILURE_CATEGORY_TOKENS:
            twice = classify_failure(once, second_default)
            if _token_count_at_start(once) == 1:
                assert twice == once, (
                    "an already-tokenized reason was rewritten (second "
                    "default {!r}): {!r} -> {!r}".format(
                        second_default, once, twice))
            assert _token_count_at_start(twice) <= 1, (
                "double prefix (second default {!r}): {!r}".format(
                    second_default, twice))
            # A third pass under the same default changes nothing.
            assert classify_failure(twice, second_default) == twice

    # Validates: Requirements 2.6
    @settings(deadline=None)
    @example(reason="")
    @example(reason=" ")
    @example(reason="model weights are corrupt")
    @given(reason=unrecognized_reasons())
    def test_property_unrecognized_reasons_bind_through_the_default_token(
            self, reason):
        """**P7-C**: the COLLISION A contract (class docstring). An
        unrecognised reason with the manager's default stays RAW —
        byte-identical, the recorded deviation preserving the sibling
        spec's retained-reason pins — while an explicit caller-supplied
        category (``repository-invalid:``, ``engine-construction-error:``)
        is prepended exactly once with the original text verbatim.

        # Validates: Requirements 2.6
        """
        assert classify_failure_reason(reason) == UNCLASSIFIED_FAILURE_TOKEN
        assert classify_failure(reason) == reason, (
            "an unrecognised reason was rewritten under the default "
            "(COLLISION A pins it RAW): {!r} -> {!r}".format(
                reason, classify_failure(reason)))

        for explicit in (REPOSITORY_INVALID_TOKEN,
                         ENGINE_CONSTRUCTION_ERROR_TOKEN):
            classified = classify_failure(reason, explicit)
            assert classified == "{} {}".format(explicit, reason), (
                "the explicit category was not prepended verbatim: "
                "{!r}".format(classified))
            assert _token_count_at_start(classified) == 1

    # Validates: Requirements 2.6
    def test_nvml_signature_outranks_kv_signature(self):
        """**P7-D**: a reason carrying BOTH symptom signatures classifies
        as ``allocator-nvml-fault:`` — the allocator/NVML check runs
        FIRST, so the assert (which often mentions memory) never reads as
        a budget fault. Distinguishability is exactly what defect 1.6
        lacked. The assert's ROOT CAUSE stays an open question —
        [HARDWARE] H7, task 14 — this only proves the reporting.

        # Validates: Requirements 2.6
        """
        combined = "{} while handling: {}".format(
            NVML_ASSERT_REASON, KV_OOM_REASON)
        assert classify_failure_reason(combined) == \
            ALLOCATOR_NVML_FAULT_TOKEN
        classified = classify_failure(combined)
        assert classified == "{} {}".format(
            ALLOCATOR_NVML_FAULT_TOKEN, combined)
        assert _token_count_at_start(classified) == 1

    # Validates: Requirements 2.6
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @example(case=(KV_OOM_REASON, KV_CACHE_EXHAUSTION_TOKEN))
    @example(case=(NVML_ASSERT_REASON, ALLOCATOR_NVML_FAULT_TOKEN))
    @given(case=recognized_reasons())
    def test_property_end_to_end_status_reason_keeps_marker_matching(
            self, tmp_path, case):
        """**P7-E, end to end**: for any recognised engine-construction
        failure reason, the FAILED ``ModelStatus.reason`` carries exactly
        one token with the original backend text verbatim after it — so
        the prep's ``KV_CACHE_HINT_MARKERS`` (and the reconciler's mirror)
        match the classified reason exactly when they matched the raw one.

        # Validates: Requirements 2.6
        """
        from vllm_runtime.reconciler import (
            KV_CACHE_HINT_MARKERS as reconciler_markers,
        )
        reason, expected_token = case
        factory = FailingEngineFactory(reason)
        manager, _repo = _build_manager(tmp_path, factory)

        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

        assert status.state is ModelState.FAILED
        assert factory.call_count == 1, (
            "harness precondition: the load must reach engine "
            "construction (unsizable weights; generous reading)")
        assert status.reason == "{} {}".format(expected_token, reason), (
            "the retained reason is not token + verbatim original: "
            "{!r}".format(status.reason))
        assert _token_count_at_start(status.reason) == 1

        # The existing marker matching still works: classification changes
        # WHETHER a marker matches for no reason whatsoever.
        for markers in (mp.KV_CACHE_HINT_MARKERS, reconciler_markers):
            raw_match = any(marker.lower() in reason.lower()
                            for marker in markers)
            classified_match = any(
                marker.lower() in status.reason.lower()
                for marker in markers)
            assert classified_match == raw_match, (
                "classification changed marker matching: raw={} "
                "classified={} reason={!r}".format(
                    raw_match, classified_match, status.reason))

    # Validates: Requirements 2.6
    def test_repository_invalid_and_preflight_refusals_carry_one_token(
            self, tmp_path):
        """**P7-F**: the two non-engine failure layers carry their own
        single token. A repository validation failure (the caller's
        explicit category — no engine was attempted) starts with
        ``repository-invalid:`` with the validation text verbatim after
        it; a preflight refusal arrives already composed with
        ``preflight-refused:`` and the classifier never adds a second
        token on top.

        # Validates: Requirements 2.6
        """
        # Repository half: nothing staged at all -> RepositoryValidationError.
        factory = RecordingEngineFactory()
        manager = make_manager(
            tmp_path / "empty", factory,
            memory_reader=FakeMeminfoReader(_GENEROUS_READINGS))
        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
        assert status.state is ModelState.FAILED
        assert factory.call_count == 0
        assert status.reason.startswith(REPOSITORY_INVALID_TOKEN), \
            status.reason
        assert _token_count_at_start(status.reason) == 1
        original = status.reason[len(REPOSITORY_INVALID_TOKEN):].lstrip()
        assert "does not exist" in original, (
            "the validation text did not survive after the token: "
            "{!r}".format(status.reason))

        # Preflight half: sized weights + a starved reading -> an enforced
        # refusal whose reason is composed upstream with the marker and
        # must NOT be double-prefixed by `_fail`'s classifier.
        weights_dir = weight_tree(tmp_path / "weights", int(6.5 * GIB))
        engine_args = dict(INCIDENT_ENGINE_ARGS, model=str(weights_dir))
        repo = tmp_path / "repo-preflight"
        build_staged_repo(repo, engine_args=engine_args)
        refusing = make_manager(
            repo, RecordingEngineFactory(),
            memory_reader=FakeMeminfoReader([(DEVICE_TOTAL_BYTES, 3 * GIB)]))
        refused = asyncio.run(refusing.load(DEFAULT_MODEL_NAME))
        assert refused.state is ModelState.FAILED
        assert refused.reason.startswith(PREFLIGHT_REFUSED_MARKER), \
            refused.reason
        assert _token_count_at_start(refused.reason) == 1, (
            "the preflight's own marker was double-prefixed: "
            "{!r}".format(refused.reason))


# ---------------------------------------------------------------------------
# Task 4.6 — Property 7, thin-margin half
# ---------------------------------------------------------------------------

def _cache_config(num_gpu_blocks, block_size, max_model_len):
    """A fake ``cache_config`` in the shape the manager's KV-margin reader
    introspects (``fakes.thin_cache_config``'s shape plus an explicit
    ``max_model_len`` so the concurrency arm is driven per example)."""
    return SimpleNamespace(
        num_gpu_blocks=num_gpu_blocks,
        num_cpu_blocks=0,
        block_size=block_size,
        max_model_len=max_model_len,
        cache_dtype="auto",
        gpu_memory_utilization=0.4,
    )


@st.composite
def kv_geometries(draw):
    """(num_gpu_blocks, block_size, max_model_len) spanning both sides of
    BOTH thin-margin arms (bytes floor and concurrency threshold)."""
    num_gpu_blocks = draw(st.integers(min_value=1, max_value=60000))
    block_size = draw(st.sampled_from((1, 8, 16, 32)))
    max_model_len = draw(st.integers(min_value=256, max_value=32768))
    return num_gpu_blocks, block_size, max_model_len


class _RaisingCacheConfig:
    """A cache_config whose every attribute read raises — introspection
    must swallow it (one debug line, no warning, no behavior change)."""

    def __getattr__(self, name):
        raise RuntimeError("exotic engine shape: attribute {} exploded"
                           .format(name))


#: Exotic / unreadable engine shapes: missing attributes, booleans (a
#: bool is an int), strings, floats, zero and negative block counts, and
#: an attribute access that raises.
_EXOTIC_CACHE_CONFIGS = (
    ("no attributes", lambda: SimpleNamespace()),
    ("boolean blocks", lambda: SimpleNamespace(
        num_gpu_blocks=True, block_size=16, max_model_len=4096)),
    ("string blocks", lambda: SimpleNamespace(
        num_gpu_blocks="340", block_size=16, max_model_len=4096)),
    ("float blocks", lambda: SimpleNamespace(
        num_gpu_blocks=340.0, block_size=16, max_model_len=4096)),
    ("zero blocks", lambda: SimpleNamespace(
        num_gpu_blocks=0, block_size=16, max_model_len=4096)),
    ("negative blocks", lambda: SimpleNamespace(
        num_gpu_blocks=-5, block_size=16, max_model_len=4096)),
    ("None block size", lambda: SimpleNamespace(
        num_gpu_blocks=340, block_size=None, max_model_len=4096)),
    ("raising attributes", _RaisingCacheConfig),
)


class TestProperty7ThinMargin:
    """**Property 7, thin-margin half** — _for any_ engine whose post-load
    KV sizing is readable and below the floor (`MINIMUM_KV_CACHE_BYTES`,
    the observed 0.65 GiB against the 1 GiB floor) or below the
    thin-margin concurrency (`THIN_MARGIN_CONCURRENCY`, an
    estimate/placeholder per design Open question 5), the fixed manager
    logs a WARNING naming the margin and the concurrency — with READY
    still READY. Ample KV produces NO warning and the byte-identical
    READY line; an exotic/unreadable engine shape produces one debug line,
    no warning, and no behavior change.

    HONESTY GUARD: the "engine" is a fake ``cache_config`` the manager's
    best-effort reader introspects — no GPU, no real KV allocation. This
    proves the reporting decision logic only.
    """

    # Validates: Requirements 2.7
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    # The incident's shape: thin on both arms (≈0.29 GiB, 1.33x).
    @example(geometry=(340, 16, 4096))
    # Healthy control: 320,000 tokens, 78x.
    @example(geometry=(20000, 16, 4096))
    # Concurrency boundary: exactly 2.0x is NOT thin (strictly below)...
    @example(geometry=(2048, 16, 16384))
    # ...one block less (1.999x) is, isolating the concurrency arm
    # (kv bytes ample at ≈1.75 GiB).
    @example(geometry=(2047, 16, 16384))
    # Bytes-floor boundary pair isolating the bytes arm (concurrency
    # ≈4.57x is ample): 18,720 tokens ≈ 1023.6 MiB < 1 GiB floor is thin,
    # 18,736 tokens ≈ 1024.5 MiB is not.
    @example(geometry=(1170, 16, 4096))
    @example(geometry=(1171, 16, 4096))
    @given(geometry=kv_geometries())
    def test_property_warning_iff_the_margin_is_thin(self, tmp_path,
                                                     geometry):
        """**P7-G**: for any readable KV geometry, the thin-margin WARNING
        is logged **iff** derived KV bytes fall below
        ``MINIMUM_KV_CACHE_BYTES`` or derived concurrency falls below
        ``THIN_MARGIN_CONCURRENCY``; the WARNING names the margin (the KV
        byte figure), the concurrency, the model, the floor and the
        threshold; READY is still READY either way, and the READY line is
        byte-identical in BOTH arms — the warning is beside it, never
        instead of it.

        # Validates: Requirements 2.7
        """
        num_gpu_blocks, block_size, max_model_len = geometry
        tokens = num_gpu_blocks * block_size
        kv_bytes = tokens * _KV_BYTES_PER_TOKEN
        concurrency = tokens / float(max_model_len)
        should_warn = (kv_bytes < MINIMUM_KV_CACHE_BYTES
                       or concurrency < THIN_MARGIN_CONCURRENCY)

        factory = RecordingEngineFactory(
            cache_config=_cache_config(num_gpu_blocks, block_size,
                                       max_model_len))
        manager, _repo = _build_manager(tmp_path, factory)

        with _collected_logs() as records:
            status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

        # READY still READY: the status object is exactly the healthy
        # one — same state, no reason, no new field smuggled in.
        assert status == ModelStatus(ModelState.READY), (status, geometry)
        assert manager.state(DEFAULT_MODEL_NAME) == \
            ModelStatus(ModelState.READY)
        # The byte-identical READY line, in both arms.
        infos = _messages(records, logging.INFO)
        assert _READY_LINE in infos, (
            "the READY line changed shape: {!r}".format(infos))

        warnings = [message for message in
                    _messages(records, logging.WARNING)
                    if _THIN_MARGIN_SIGNATURE in message]
        if should_warn:
            assert warnings, (
                "a READY load with thin KV margin (kv_bytes={} floor={}, "
                "concurrency={:.2f}x threshold={:.1f}x) produced no "
                "thin-margin WARNING".format(
                    format_gib(kv_bytes),
                    format_gib(MINIMUM_KV_CACHE_BYTES),
                    concurrency, THIN_MARGIN_CONCURRENCY))
            message = warnings[0]
            assert DEFAULT_MODEL_NAME in message, message
            # Names the margin...
            assert format_gib(kv_bytes) in message, (
                "the WARNING does not name the KV margin {}: {!r}".format(
                    format_gib(kv_bytes), message))
            # ...and the concurrency...
            assert "{:.2f}x".format(concurrency) in message, (
                "the WARNING does not name the concurrency {:.2f}x: "
                "{!r}".format(concurrency, message))
            # ...against the labelled floor and threshold.
            assert format_gib(MINIMUM_KV_CACHE_BYTES) in message, message
            assert "{:.1f}x".format(THIN_MARGIN_CONCURRENCY) in message, \
                message
        else:
            assert not warnings, (
                "an ample KV margin (kv_bytes={}, concurrency={:.2f}x) "
                "was reported thin: {!r}".format(
                    format_gib(kv_bytes), concurrency, warnings))

    # Validates: Requirements 2.7
    def test_ample_kv_no_warning_and_the_byte_identical_ready_line(
            self, tmp_path):
        """**P7-H, deterministic control**: the suite-standard healthy
        sizing (320,000 tokens, 78x at ``max_model_len=4096``) reaches
        READY with NO warning of any kind from the manager and the
        byte-identical ``vLLM model '<name>' is READY`` INFO line.

        # Validates: Requirements 2.7
        """
        factory = RecordingEngineFactory(cache_config=healthy_cache_config())
        manager, _repo = _build_manager(tmp_path, factory)

        with _collected_logs() as records:
            status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

        assert status == ModelStatus(ModelState.READY)
        assert not _messages(records, logging.WARNING), (
            "an ample-KV load warned: {!r}".format(
                _messages(records, logging.WARNING)))
        infos = _messages(records, logging.INFO)
        assert _READY_LINE in infos, infos

    # Validates: Requirements 2.7
    @settings(deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(shape=st.sampled_from(_EXOTIC_CACHE_CONFIGS))
    def test_property_unreadable_engine_shape_one_debug_line_no_warning(
            self, tmp_path, shape):
        """**P7-I**: for any exotic/unreadable engine shape (missing
        attributes, booleans, strings, floats, zero/negative counts, an
        attribute access that raises), the load behaves exactly as before
        Decision 6: READY, the byte-identical READY line, NO warning —
        and exactly ONE debug line saying the sizing was not readable.

        # Validates: Requirements 2.7
        """
        label, build = shape
        factory = RecordingEngineFactory(cache_config=build())
        manager, _repo = _build_manager(tmp_path, factory)

        with _collected_logs() as records:
            status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

        assert status == ModelStatus(ModelState.READY), (label, status)
        assert not _messages(records, logging.WARNING), (
            "an unreadable engine shape ({}) warned: {!r}".format(
                label, _messages(records, logging.WARNING)))
        infos = _messages(records, logging.INFO)
        assert _READY_LINE in infos, (label, infos)
        debugs = [message for message in
                  _messages(records, logging.DEBUG)
                  if _NOT_READABLE_SIGNATURE in message]
        assert len(debugs) == 1, (
            "expected exactly one 'not readable' debug line for shape "
            "({}), got {}: {!r}".format(label, len(debugs), debugs))
        assert DEFAULT_MODEL_NAME in debugs[0], debugs[0]

    # Validates: Requirements 2.7
    def test_engine_without_any_cache_config_is_also_unreadable(
            self, tmp_path):
        """**P7-I, bare-engine leg**: an engine object exposing NEITHER
        ``engine.cache_config`` nor ``cache_config`` at all (no inner
        engine either) is simply unreadable: READY, one debug line, no
        warning, no behavior change.

        # Validates: Requirements 2.7
        """
        def bare_engine_factory(engine_args):
            return SimpleNamespace(errored=False)

        manager, _repo = _build_manager(tmp_path, bare_engine_factory)
        with _collected_logs() as records:
            status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

        assert status == ModelStatus(ModelState.READY)
        assert not _messages(records, logging.WARNING)
        assert _READY_LINE in _messages(records, logging.INFO)
        debugs = [message for message in
                  _messages(records, logging.DEBUG)
                  if _NOT_READABLE_SIGNATURE in message]
        assert len(debugs) == 1, debugs


# ---------------------------------------------------------------------------
# Task 4.6 — Property 7, no-new-status-surface half
# ---------------------------------------------------------------------------

class TestProperty7NoNewStatusSurface:
    """**Property 7, surface preservation** — the classifier and the
    thin-margin WARNING live INSIDE the existing surfaces (design Decision
    6: "no new status surface"): ``ModelStatus`` keeps exactly its two
    fields, the 409 body keeps exactly its shape, and every status map is
    structurally identical. The category token travels inside the
    existing ``reason`` string; the thin margin travels in a log line —
    neither adds a field anywhere.
    """

    # Validates: Requirements 2.6, 2.7
    def test_model_status_and_model_state_are_structurally_identical(self):
        """``ModelStatus`` carries exactly ``state`` and ``reason`` (in
        that order, ``reason`` defaulting to None) and ``ModelState``
        carries exactly the six recorded members — the classifier and the
        thin-margin WARNING added NO field and NO state.

        # Validates: Requirements 2.6, 2.7
        """
        fields = dataclasses.fields(ModelStatus)
        assert [f.name for f in fields] == ["state", "reason"], (
            "ModelStatus grew/lost a field: {!r}".format(
                [f.name for f in fields]))
        assert fields[1].default is None

        assert {member.name: member.value for member in ModelState} == {
            "STAGED": "STAGED",
            "LOADING": "LOADING",
            "READY": "READY",
            "FAILED": "FAILED",
            "UNKNOWN": "UNKNOWN",
            "UNLOADED": "UNLOADED",
        }, "ModelState grew/lost a member"

        # A classified FAILED status is still just the two fields.
        status = ModelStatus(ModelState.FAILED,
                             reason=classify_failure(KV_OOM_REASON))
        assert set(dataclasses.asdict(status)) == {"state", "reason"}

    # Validates: Requirements 2.6
    def test_409_body_shape_is_structurally_identical_for_failures(
            self, tmp_path):
        """The load endpoint's 409 body for a classified failure is
        exactly ``{name, state, reason}`` — the token rides INSIDE the
        existing ``reason`` string — and the ready endpoint's 409 body is
        exactly ``{error, name, state, reason}``, byte-compatible with
        every existing consumer.

        # Validates: Requirements 2.6
        """
        factory = FailingEngineFactory(KV_OOM_REASON)
        manager, _repo = _build_manager(tmp_path, factory)
        client = TestClient(create_app(manager))

        response = client.post(
            "/v2/repository/models/{}/load".format(DEFAULT_MODEL_NAME))
        assert response.status_code == 409
        body = response.json()
        assert set(body) == {"name", "state", "reason"}, (
            "the 409 load body changed shape: {!r}".format(body))
        assert body["name"] == DEFAULT_MODEL_NAME
        assert body["state"] == "FAILED"
        assert body["reason"].startswith(KV_CACHE_EXHAUSTION_TOKEN)
        assert KV_OOM_REASON in body["reason"]

        ready = client.get("/v2/models/{}/ready".format(DEFAULT_MODEL_NAME))
        assert ready.status_code == 409
        ready_body = ready.json()
        assert set(ready_body) == {"error", "name", "state", "reason"}, (
            "the ready 409 body changed shape: {!r}".format(ready_body))
        assert ready_body["state"] == "FAILED"

        index = client.get("/v2/repository/index")
        assert index.status_code == 200
        for entry in index.json():
            assert set(entry) <= {"name", "state", "reason"}, (
                "a repository-index entry changed shape: {!r}".format(
                    entry))

    # Validates: Requirements 2.7
    def test_thin_margin_ready_body_is_structurally_identical(
            self, tmp_path):
        """A load that reaches READY with a THIN margin answers 200 with
        exactly ``{name, state}`` — no margin field, no warning field, no
        new anything: the thin margin is a LOG WARNING, not a status
        surface (Decision 6).

        # Validates: Requirements 2.7
        """
        factory = RecordingEngineFactory(cache_config=thin_cache_config())
        manager, _repo = _build_manager(tmp_path, factory)
        client = TestClient(create_app(manager))

        response = client.post(
            "/v2/repository/models/{}/load".format(DEFAULT_MODEL_NAME))
        assert response.status_code == 200
        body = response.json()
        assert body == {"name": DEFAULT_MODEL_NAME, "state": "READY"}, (
            "the thin-margin READY body changed shape: {!r}".format(body))

    # Validates: Requirements 2.6, 2.7
    def test_every_status_map_is_structurally_identical(self):
        """The two status maps every consumer reads are byte-identical to
        their recorded shapes: the server's ``_status_payload`` (the 409 /
        index body builder) emits exactly ``{name, state[, reason]}``, and
        the Text_Generation_API's ``_STATE_CATEGORY`` (the feature-config
        / 409-category map) carries exactly its six recorded entries.

        # Validates: Requirements 2.6, 2.7
        """
        # Server payload builder: reason present iff retained.
        with_reason = _status_payload(
            DEFAULT_MODEL_NAME,
            ModelStatus(ModelState.FAILED,
                        reason=classify_failure(NVML_ASSERT_REASON)))
        assert set(with_reason) == {"name", "state", "reason"}
        assert with_reason["state"] == "FAILED"
        assert with_reason["reason"].startswith(ALLOCATOR_NVML_FAULT_TOKEN)
        without_reason = _status_payload(
            DEFAULT_MODEL_NAME, ModelStatus(ModelState.READY))
        assert set(without_reason) == {"name", "state"}

        # The 409-category map (endpoints.text_generation), exactly its
        # recorded six entries — imported here so a collection failure in
        # that module surfaces as THIS test's failure, not the file's.
        from endpoints.text_generation import _STATE_CATEGORY, state_category

        assert _STATE_CATEGORY == {
            "READY": "ready",
            "STAGED": "loading",
            "LOADING": "loading",
            "FAILED": "failed",
            "UNKNOWN": "unknown",
            "UNLOADED": "unloaded",
        }, "the 409-category status map changed shape"
        assert state_category(ModelState.FAILED) == "failed"
        assert state_category("never-heard-of-it") == "unknown"
