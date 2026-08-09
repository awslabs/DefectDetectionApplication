"""Stage 25 — VLM image inference smoke (edge-vlm-image-inference, Reqs 4.5, 3.6).

Capability-gated on ``vllm`` like stage 20: the module skips with a recorded
reason when the Device_Profile does not grant it, and the session
``vllm_surface`` probe fails the stage with ``CapabilityMismatchError`` when
the capability is declared but absent on the device.

On top of the capability gate, the stage self-gates on a deployed
vision-language model: it scans the device's ``VllmModel`` entries for a
Qwen VL / multimodal candidate (by model naming — ``*-vl-*``, ``vlm``,
``vision``) and **skips gracefully** when none is deployed, so the stage is
safe on devices serving only text-only models (e.g. the ``opt125m-smoke``
reference stack).

Against a READY multimodal model it then runs two plain integration examples
(deliberately NOT property-based — this validates the deployed runtime, not
the input space):

* an image-carrying generate — base64 JPEG in the ``image`` field of
  ``POST /text-generation/{model}/generate`` — answers 200 with
  ``image_used: true`` and non-empty generated text (Reqs 4.5, 3.6);
* a text-only generate against the same model is unchanged: 200 with
  non-empty ``generated_text`` and NO ``image_used`` key (Req 3.6 —
  text-only responses stay byte-identical to pre-feature behavior).
"""

from __future__ import annotations

import base64
import re
import time
from typing import Any, Dict, Optional

import pytest
from harnesslib.restoration import RUNNING_PRE_STATES

pytestmark = [pytest.mark.stage("vlm_image_generate"), pytest.mark.capability("vllm")]

#: Small deterministic-leaning request bodies (same bounds as stage 20).
IMAGE_PROMPT = "In one short sentence, describe what you see in this image."
TEXT_ONLY_PROMPT = "Complete this sentence in a few words: edge devices run"
GENERATE_PARAMS = {"max_tokens": 64, "temperature": 0.0}

#: Model-name patterns that identify a deployed vision-language (multimodal)
#: model: a separated ``vl`` token (``qwen2-vl-2b``, ``qwen2.5-vl-3b``),
#: ``vlm``, or ``vision``. Text-only models (``opt125m-smoke``, plain Qwen)
#: match none of these, so the stage skips instead of failing on them.
_MULTIMODAL_NAME_PATTERN = re.compile(r"(?<![a-z0-9])(vl|vlm|vision)(?![a-z])", re.IGNORECASE)

#: Bundled test asset: a 64x64 white JPEG with a centered red square
#: (707 bytes), generated with Pillow and embedded so the harness needs no
#: image dependency and no binary file. The payload only has to be a valid
#: JPEG the vLLM runtime can decode; content is irrelevant to the assertions.
TEST_IMAGE_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8l"
    "JCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIo"
    "Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAAR"
    "CABAAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
    "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK"
    "FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG"
    "h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl"
    "5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYk"
    "NOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE"
    "hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
    "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD2aiiigAooooAKKKKACiiigAooooA+c6KK"
    "K8g/RAooooA+jKKKK9c/OwooooA+c6KKK8g/RAooooA+jKKKK9c/OwooooAKKKKACiiigAoo"
    "ooA//9k="
)

# The embedded asset must stay a decodable payload; fail collection early if
# it is ever corrupted by an edit.
assert base64.b64decode(TEST_IMAGE_B64)[:3] == b"\xff\xd8\xff", "TEST_IMAGE_B64 is not a JPEG"


def _looks_multimodal(entry: Dict[str, Any]) -> bool:
    """Whether a ``VllmModel`` feature entry names a vision-language model."""
    name = entry.get("modelName") or ""
    if _MULTIMODAL_NAME_PATTERN.search(name):
        return True
    # Fall back to the registered source model id when present (e.g. an HF id
    # like ``Qwen/Qwen2-VL-2B-Instruct`` behind a neutral local name).
    configuration = entry.get("defaultConfiguration") or {}
    if isinstance(configuration, dict):
        for key in ("modelId", "model_id", "sourceModel", "hfModelId"):
            value = configuration.get(key)
            if isinstance(value, str) and _MULTIMODAL_NAME_PATTERN.search(value):
                return True
    return False


@pytest.fixture(scope="module")
def multimodal_model_name(harness_target, vllm_surface) -> str:
    """The first deployed Qwen VL / multimodal ``VllmModel`` entry, or a
    graceful skip when the device serves only text-only models."""
    entries = vllm_surface["feature_entries"]
    for entry in entries:
        if _looks_multimodal(entry):
            return entry.get("modelName")
    reported = [entry.get("modelName") for entry in entries]
    pytest.skip(
        f"no Qwen VL / multimodal vLLM model deployed on device "
        f"{harness_target.name}; VllmModel entries: {reported} — the VLM "
        "image smoke needs a vision-language model (e.g. qwen2-vl / "
        "qwen2.5-vl) to exercise image inference"
    )


@pytest.fixture(scope="module")
def ready_multimodal_model(
    harness_target, edge_client, state_registry, multimodal_model_name
) -> str:
    """The multimodal model brought to READY within ``timeouts.vllm_ready_s``
    (stage-20 pattern), recorded for State_Restoration with its
    device-reported pre-run state before any start is issued."""
    name = multimodal_model_name
    entry = edge_client.model_entry(name)
    pre_state = entry.get("status") if entry is not None else None
    state_registry.record("model", name, pre_state, lambda n=name: edge_client.stop_model(n))
    if pre_state not in RUNNING_PRE_STATES:
        edge_client.start_model(name)
    edge_client.wait_for_model_state(
        name, target="READY", timeout_s=harness_target.timeouts.vllm_ready_s
    )
    return name


def test_image_generate_returns_answer_with_image_used(
    harness_target, edge_client, ready_multimodal_model, record_metric
):
    """An image-carrying generate (base64 JPEG in the ``image`` field)
    against a READY Qwen VL model answers 200 with ``image_used: true`` and
    non-empty generated text (Reqs 4.5, 3.6)."""
    model = ready_multimodal_model
    params: Dict[str, Any] = dict(GENERATE_PARAMS)
    params["image"] = TEST_IMAGE_B64
    started = time.monotonic()
    response = edge_client.generate(
        model,
        IMAGE_PROMPT,
        params=params,
        timeout_s=harness_target.timeouts.generate_s,
    )
    latency_s = time.monotonic() - started
    assert isinstance(response, dict), (
        f"image generate answered with a non-object payload: {response!r}"
    )
    assert "image_used" in response, (
        f"image generate against model {model!r} answered without an "
        f"'image_used' key — the deployed LocalServer likely predates the "
        f"edge-vlm-image-inference feature; response: {response!r}"
    )
    assert response.get("image_used") is True, (
        f"model {model!r} did not consume the image (image_used="
        f"{response.get('image_used')!r}) — expected a multimodal Qwen VL "
        f"model to report image_used: true; response: {response!r}"
    )
    text = response.get("generated_text")
    assert isinstance(text, str) and text.strip(), (
        f"image generate against model {model!r} returned no generated "
        f"text; response: {response!r}"
    )
    record_metric("vlm_image_generate_latency_s", round(latency_s, 3))
    record_metric("vlm_image_generate_text_chars", len(text))


def test_text_only_generate_unchanged(
    harness_target, edge_client, ready_multimodal_model
):
    """A text-only generate against the same model is unchanged by the image
    feature: 200 with non-empty ``generated_text`` and NO ``image_used`` key
    (Req 3.6 — text-only responses stay identical to pre-feature behavior)."""
    model = ready_multimodal_model
    response = edge_client.generate(
        model,
        TEXT_ONLY_PROMPT,
        params=GENERATE_PARAMS,
        timeout_s=harness_target.timeouts.generate_s,
    )
    assert isinstance(response, dict), (
        f"text-only generate answered with a non-object payload: {response!r}"
    )
    text = response.get("generated_text")
    assert isinstance(text, str) and text.strip(), (
        f"text-only generate against model {model!r} returned no generated "
        f"text; response: {response!r}"
    )
    assert "image_used" not in response, (
        f"text-only generate response unexpectedly carries an 'image_used' "
        f"key — text-only behavior must be unchanged; response: {response!r}"
    )
