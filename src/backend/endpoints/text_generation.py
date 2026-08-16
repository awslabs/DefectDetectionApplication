#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""Text_Generation_API request handling for vLLM models.

The module has two layers:

- a pure request-normalization core (validation and default
  application) shared by the generate and generate-stream endpoints and
  property-tested without FastAPI or the vLLM runtime;
- the FastAPI router (state check, bounded retry, wall-clock timeout,
  SSE streaming) registered by app.py beside the other endpoint routers
  only on vLLM-capable images (task 12.2).

Like every other device router, route paths carry no ``/api`` prefix —
the externally visible ``/api/text-generation/...`` form is produced by
the frontend proxy, exactly as for ``/workflows`` and friends.

The runtime dependency is injectable: app.py provides the started
``VllmRuntimeManager`` through :func:`set_runtime`, and tests override
the FastAPI dependency :func:`get_runtime` with a fake exposing the
same surface (``state``, ``list_models``, ``engine_args``, ``generate``,
``generate_stream``).
"""

# System Modules
import asyncio
import base64
import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Union

# Fast api
from fastapi import Body, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from endpoints.route.access_log_router import get_api_router
# Importing vllm_runtime never imports vllm itself (the manager defers
# that to engine construction), so this module is safe on every image.
from vllm_runtime.manager import (
    GenerationError,
    ModelState,
    ModelUnavailableError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure request-normalization core
# ---------------------------------------------------------------------------

# Documented default values applied to omitted generation parameters
# (Requirement 5.8).
GENERATION_DEFAULTS = {"max_tokens": 256, "temperature": 0.7, "top_p": 1.0}


def _is_int(value: Any) -> bool:
    """True for ints, excluding bools (bool is a subclass of int)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    """True for ints and floats, excluding bools."""
    return (isinstance(value, (int, float))
            and not isinstance(value, bool))


def _validate_image_field(
    body: Dict[str, Any],
    field_name: str,
    findings: List[Dict[str, str]],
) -> Optional[bytes]:
    """Validate one optional base64 image field of a generate request.

    One rule set for both ``image`` and ``reference_image``
    (vlm-anomaly-reference-parity Requirement 5.1): the field must be a
    string of valid base64 decoding to 1..MAX_IMAGE_BYTES bytes. Returns
    the decoded bytes when the field is present and valid; ``None`` when
    the field is absent or invalid — an invalid value appends a finding
    naming ``field_name``.
    """
    if field_name not in body:
        return None
    value = body[field_name]
    if not isinstance(value, str):
        findings.append({
            "field": field_name,
            "reason": "{} must be a base64-encoded string".format(
                field_name),
        })
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        findings.append({
            "field": field_name,
            "reason": "{} is not valid base64".format(field_name),
        })
        return None
    max_image_bytes = get_max_image_bytes()
    if len(decoded) == 0:
        findings.append({
            "field": field_name,
            "reason": "{} decodes to zero bytes".format(field_name),
        })
        return None
    if len(decoded) > max_image_bytes:
        findings.append({
            "field": field_name,
            "reason": "{} decodes to {} bytes, exceeding "
                      "the maximum of {} bytes".format(
                          field_name, len(decoded), max_image_bytes),
        })
        return None
    return decoded


def normalize_generation_request(
    model_name: Any,
    body: Dict[str, Any],
    model_max_len: int,
) -> Union[Dict[str, Any], List[Dict[str, str]]]:
    """Validate a text-generation request and apply parameter defaults.

    Returns the effective (normalized) request as a dict when the request
    is valid: supplied values overlaid on GENERATION_DEFAULTS for exactly
    the omitted parameters (Requirements 5.1, 5.8).

    Returns a non-empty list of findings, each naming the offending field
    and the reason, when the request is invalid (Requirement 5.9):
      - prompt empty or missing
      - model_name empty or missing
      - supplied max_tokens < 1 or > model_max_len (the loaded model's
        configured max_model_len)
      - supplied temperature outside [0.0, 2.0]
      - supplied top_p outside (0.0, 1.0] (exclusive lower bound)
      - supplied image not a string, not valid base64, decoding to zero
        bytes, or decoding to more than the configured maximum image
        size (edge-vlm-image-inference Requirements 3.4, 3.5)
      - supplied reference_image failing the same image rules
        (vlm-anomaly-reference-parity Requirement 5.1), or supplied
        without a valid image (Requirement 5.4)

    Omitted generation parameters are never findings: their defaults are
    applied and the request is processed (Requirement 5.8). An omitted
    ``image`` leaves the normalized result identical to pre-feature
    behavior (Requirements 3.3, 6.2); a valid ``image`` is decoded once
    here, at the validation boundary, and stored as
    ``effective["image_bytes"]`` (Requirement 3.1). Likewise an omitted
    ``reference_image`` leaves the result identical to pre-feature
    behavior and a valid one is stored as
    ``effective["reference_image_bytes"]`` (Requirements 5.2, 5.3).

    Callers distinguish the outcomes with isinstance(result, list).
    """
    findings: List[Dict[str, str]] = []

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or len(prompt) < 1:
        findings.append({
            "field": "prompt",
            "reason": "prompt is missing or empty; it must contain at "
                      "least 1 character",
        })

    if not isinstance(model_name, str) or len(model_name) < 1:
        findings.append({
            "field": "model_name",
            "reason": "model_name is missing or empty",
        })

    if "max_tokens" in body:
        max_tokens = body["max_tokens"]
        if not _is_int(max_tokens):
            findings.append({
                "field": "max_tokens",
                "reason": "max_tokens must be an integer",
            })
        elif max_tokens < 1 or max_tokens > model_max_len:
            findings.append({
                "field": "max_tokens",
                "reason": "max_tokens must be at least 1 and no greater "
                          "than the model's max_model_len "
                          "({})".format(model_max_len),
            })

    if "temperature" in body:
        temperature = body["temperature"]
        if not _is_number(temperature):
            findings.append({
                "field": "temperature",
                "reason": "temperature must be a number",
            })
        elif temperature < 0.0 or temperature > 2.0:
            findings.append({
                "field": "temperature",
                "reason": "temperature must be between 0.0 and 2.0 "
                          "inclusive",
            })

    if "top_p" in body:
        top_p = body["top_p"]
        if not _is_number(top_p):
            findings.append({
                "field": "top_p",
                "reason": "top_p must be a number",
            })
        elif top_p <= 0.0 or top_p > 1.0:
            findings.append({
                "field": "top_p",
                "reason": "top_p must be greater than 0.0 and no greater "
                          "than 1.0",
            })

    image_bytes = _validate_image_field(body, "image", findings)
    reference_image_bytes = _validate_image_field(
        body, "reference_image", findings)

    # A reference image only makes sense beside a primary image
    # (vlm-anomaly-reference-parity Requirement 5.4): reject a valid
    # reference_image whose request carries no valid image.
    if reference_image_bytes is not None and image_bytes is None:
        findings.append({
            "field": "reference_image",
            "reason": "reference_image requires a valid image field; a "
                      "reference image cannot be sent without the "
                      "primary image",
        })

    if findings:
        return findings

    effective = dict(GENERATION_DEFAULTS)
    for key in GENERATION_DEFAULTS:
        if key in body:
            effective[key] = body[key]
    effective["model_name"] = model_name
    effective["prompt"] = prompt
    if image_bytes is not None:
        effective["image_bytes"] = image_bytes
    if reference_image_bytes is not None:
        effective["reference_image_bytes"] = reference_image_bytes
    return effective


# ---------------------------------------------------------------------------
# Configuration (environment-overridable, read per request so tests and
# operators can change them without a restart)
# ---------------------------------------------------------------------------

#: Default number of retries after a transient generate failure
#: (Requirement 5.6).
DEFAULT_TEXT_GEN_RETRY_LIMIT = 2

#: Default wall-clock timeout over a whole non-streaming generate call,
#: retries included (Requirement 5.11).
DEFAULT_TEXT_GEN_TIMEOUT_SECONDS = 120.0

#: Default maximum decoded size of an ``image`` payload
#: (edge-vlm-image-inference Requirement 3.5): 16 MiB.
DEFAULT_MAX_IMAGE_BYTES = 16 * 1024 * 1024

#: max_tokens upper bound applied when the model's max_model_len is not
#: known to the manager (e.g. the model is not loaded — such requests are
#: rejected 409 by the state check anyway, Requirement 5.5).
FALLBACK_MAX_MODEL_LEN = 2 ** 31 - 1


def get_retry_limit() -> int:
    """The transient-error retry limit: ``TEXT_GEN_RETRY_LIMIT`` when set
    to a parseable non-negative integer, else the default of 2."""
    raw = os.environ.get("TEXT_GEN_RETRY_LIMIT")
    if raw is not None:
        try:
            value = int(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
        logger.warning("Ignoring invalid TEXT_GEN_RETRY_LIMIT=%r", raw)
    return DEFAULT_TEXT_GEN_RETRY_LIMIT


def get_timeout_seconds() -> float:
    """The non-streaming wall-clock timeout: ``TEXT_GEN_TIMEOUT_SECONDS``
    when set to a parseable positive number, else the default of 120."""
    raw = os.environ.get("TEXT_GEN_TIMEOUT_SECONDS")
    if raw is not None:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        logger.warning("Ignoring invalid TEXT_GEN_TIMEOUT_SECONDS=%r", raw)
    return DEFAULT_TEXT_GEN_TIMEOUT_SECONDS


def get_max_image_bytes() -> int:
    """The maximum decoded ``image`` payload size in bytes:
    ``TEXT_GEN_MAX_IMAGE_BYTES`` when set to a parseable positive
    integer, else the default of 16 MiB."""
    raw = os.environ.get("TEXT_GEN_MAX_IMAGE_BYTES")
    if raw is not None:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        logger.warning("Ignoring invalid TEXT_GEN_MAX_IMAGE_BYTES=%r", raw)
    return DEFAULT_MAX_IMAGE_BYTES


# ---------------------------------------------------------------------------
# Injectable runtime dependency
# ---------------------------------------------------------------------------

_runtime: Optional[Any] = None


def set_runtime(runtime: Optional[Any]) -> None:
    """Install the runtime the router serves (app.py calls this with the
    started ``VllmRuntimeManager``; ``None`` uninstalls it)."""
    global _runtime
    _runtime = runtime


def get_runtime() -> Any:
    """FastAPI dependency resolving the installed runtime. Tests override
    this dependency with a fake; 503 when no runtime is installed (the
    router should only be registered on vLLM-capable images)."""
    if _runtime is None:
        raise HTTPException(
            status_code=503,
            detail="The vLLM runtime is not available on this device.",
        )
    return _runtime


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

#: Manager state -> the category the API reports (Requirement 5.5
#: distinguishes loading / failed / unknown; STAGED is known to the
#: device and on its way to serving, so it reports as loading).
_STATE_CATEGORY = {
    ModelState.READY.value: "ready",
    ModelState.STAGED.value: "loading",
    ModelState.LOADING.value: "loading",
    ModelState.FAILED.value: "failed",
    ModelState.UNKNOWN.value: "unknown",
}


def state_category(state: Any) -> str:
    """The reported category of a manager model state (tolerant of fakes
    passing plain strings)."""
    name = getattr(state, "value", state)
    return _STATE_CATEGORY.get(str(name).upper(), "unknown")


def is_transient_error(error: BaseException) -> bool:
    """Whether a generate failure is transient and therefore retryable
    (Requirement 5.6): temporary runtime unavailability (connection
    refused/reset while the runtime restarts) or a runtime-flagged
    retryable failure (a truthy ``retryable`` attribute on the error or,
    for wrapped ``GenerationError``, on its cause)."""
    if getattr(error, "retryable", False):
        return True
    if isinstance(error, ConnectionError):
        return True
    cause = error.__cause__
    if isinstance(error, GenerationError) and cause is not None:
        return bool(getattr(cause, "retryable", False)) or isinstance(
            cause, ConnectionError
        )
    return False


def _failure_reason(error: BaseException) -> str:
    """The backend failure reason of a generate error (Requirement 5.7)."""
    reason = getattr(error, "reason", None)
    return reason if reason else str(error)


def _model_max_len(runtime: Any, model_name: str) -> int:
    """The loaded model's configured max_model_len for request validation
    (Requirement 5.1), falling back to a permissive bound when the
    manager does not know the model (the state check rejects those
    requests with the precise 409 instead of a misleading 422)."""
    getter = getattr(runtime, "engine_args", None)
    if callable(getter):
        try:
            engine_args = getter(model_name) or {}
        except Exception:  # noqa: BLE001 - validation must never 500
            engine_args = {}
        value = engine_args.get("max_model_len")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return FALLBACK_MAX_MODEL_LEN


def _status_content(model_name: str, status: Any) -> Dict[str, Any]:
    """The 409 body for a non-READY model: ``{model_name, state, reason?}``
    with state one of loading / failed / unknown (Requirement 5.5)."""
    content: Dict[str, Any] = {
        "model_name": model_name,
        "state": state_category(getattr(status, "state", status)),
    }
    reason = getattr(status, "reason", None)
    if reason:
        content["reason"] = reason
    return content


def _not_ready_response(
    runtime: Any, model_name: str
) -> Optional[JSONResponse]:
    """409 with the model's state category when the model is not READY,
    None when it is — evaluated before any generate invocation
    (Requirement 5.5)."""
    status = runtime.state(model_name)
    state = getattr(status, "state", status)
    if state_category(state) == "ready":
        return None
    return JSONResponse(
        status_code=409, content=_status_content(model_name, status)
    )


def _validate(
    runtime: Any, model_name: str, body: Dict[str, Any]
) -> Union[Dict[str, Any], JSONResponse]:
    """The normalized effective request, or the 422 response carrying the
    complete finding list — in which case the runtime's generate interface
    is never invoked (Requirement 5.9)."""
    result = normalize_generation_request(
        model_name, body, _model_max_len(runtime, model_name)
    )
    if isinstance(result, list):
        return JSONResponse(status_code=422, content={"findings": result})
    return result


def _sampling_params(effective: Dict[str, Any]) -> Dict[str, Any]:
    """The vLLM sampling parameters of a normalized request."""
    return {key: effective[key] for key in GENERATION_DEFAULTS}


def _sse_event(payload: Dict[str, Any]) -> str:
    """One server-sent event carrying a JSON payload."""
    return "data: {}\n\n".format(json.dumps(payload))


def _generate_kwargs(effective: Dict[str, Any]) -> Dict[str, Any]:
    """Extra keyword arguments for the runtime generate invocation:
    ``image=`` only when the normalized request carries decoded image
    bytes (edge-vlm-image-inference Requirement 3.2), and
    ``reference_image=`` only when it additionally carries decoded
    reference bytes (vlm-anomaly-reference-parity Requirement 5.2).
    Imageless requests produce an empty dict so the runtime invocation
    stays byte-identical to pre-feature behavior — and fakes without an
    ``image``/``reference_image`` parameter keep working for tests that
    do not exercise those fields (Requirements 3.3, 5.3)."""
    kwargs: Dict[str, Any] = {}
    image_bytes = effective.get("image_bytes")
    if image_bytes is not None:
        kwargs["image"] = image_bytes
    reference_image_bytes = effective.get("reference_image_bytes")
    if reference_image_bytes is not None:
        kwargs["reference_image"] = reference_image_bytes
    return kwargs


def _image_used(runtime: Any, model_name: str) -> bool:
    """Whether the serving model consumed the request's image, sourced
    from the manager's multimodal capability (edge-vlm-image-inference
    Requirement 3.6). Tolerant of fakes lacking ``image_supported`` —
    a runtime that cannot report capability reports no consumption."""
    supported = getattr(runtime, "image_supported", None)
    if not callable(supported):
        return False
    try:
        return bool(supported(model_name))
    except Exception:  # noqa: BLE001 - reporting must never fail a response
        return False


# ---------------------------------------------------------------------------
# FastAPI router (design section 11)
# ---------------------------------------------------------------------------

router = get_api_router()


@router.get("/text-generation/models")
def list_text_generation_models(
    runtime: Any = Depends(get_runtime),
) -> List[Dict[str, Any]]:
    """Every vLLM model known to the device with its serving state
    (name + state list, design section 11)."""
    return [
        _status_content(name, status)
        for name, status in sorted(runtime.list_models().items())
    ]


@router.post("/text-generation/{model_name}/generate")
async def generate_text(
    model_name: str,
    body: Dict[str, Any] = Body(default={}),
    runtime: Any = Depends(get_runtime),
):
    """Non-streaming text generation (Requirements 5.1, 5.2, 5.5-5.11).

    Validation failures return 422 with the complete finding list and
    never invoke the runtime; non-READY models return 409 with the state
    category; READY requests invoke generate with transient-error retry
    up to the configured limit under one wall-clock timeout. All request
    state is function-local, so concurrent requests are independent
    (Requirement 5.10).
    """
    effective = _validate(runtime, model_name, body)
    if isinstance(effective, JSONResponse):
        return effective

    not_ready = _not_ready_response(runtime, model_name)
    if not_ready is not None:
        return not_ready

    prompt = effective["prompt"]
    sampling_params = _sampling_params(effective)
    generate_kwargs = _generate_kwargs(effective)
    retry_limit = get_retry_limit()
    timeout_seconds = get_timeout_seconds()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    retries = 0

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return _timeout_response(model_name, timeout_seconds)
        try:
            text = await asyncio.wait_for(
                runtime.generate(
                    model_name, prompt, sampling_params, **generate_kwargs
                ),
                timeout=remaining,
            )
            response = {"model_name": model_name, "generated_text": text}
            if generate_kwargs:
                # Image-carrying requests report whether the model
                # consumed the image (edge-vlm-image-inference
                # Requirement 3.6); text-only responses stay
                # byte-identical (no new keys).
                response["image_used"] = _image_used(runtime, model_name)
            return response
        except asyncio.TimeoutError:
            # Wall-clock expiry over the whole call, retries included
            # (Requirement 5.11).
            return _timeout_response(model_name, timeout_seconds)
        except ModelUnavailableError as err:
            # The model left READY mid-request (e.g. a dead engine was
            # marked FAILED): report the state, not a backend error.
            return JSONResponse(
                status_code=409,
                content=_status_content(model_name, err.status),
            )
        except Exception as err:  # noqa: BLE001 - mapped, never a 500
            if is_transient_error(err) and retries < retry_limit:
                retries += 1
                logger.warning(
                    "Transient error generating with model '%s' "
                    "(retry %d/%d): %s",
                    model_name, retries, retry_limit, err,
                )
                continue
            # Exhausted retries or a non-transient failure: 502 with the
            # model name and the backend reason (Requirements 5.6, 5.7).
            return JSONResponse(
                status_code=502,
                content={
                    "model_name": model_name,
                    "reason": _failure_reason(err),
                },
            )


def _timeout_response(model_name: str, timeout_seconds: float) -> JSONResponse:
    """504 identifying the model and the timeout (Requirement 5.11)."""
    return JSONResponse(
        status_code=504,
        content={"model_name": model_name, "timeout_seconds": timeout_seconds},
    )


@router.post("/text-generation/{model_name}/generate-stream")
async def generate_text_stream(
    model_name: str,
    body: Dict[str, Any] = Body(default={}),
    runtime: Any = Depends(get_runtime),
):
    """SSE streaming text generation (Requirements 5.3, 5.4).

    Validation and the READY check run before the stream starts (so they
    keep their 422/409 mappings); the stream then forwards one
    ``{"token": ...}`` event per generated token in generation order and
    a terminal ``{"done": true}``. A mid-stream error stops delivery and
    emits exactly one ``{"error": {"reason": ...}}`` event — no retry,
    no retraction of already-delivered tokens.
    """
    effective = _validate(runtime, model_name, body)
    if isinstance(effective, JSONResponse):
        return effective

    not_ready = _not_ready_response(runtime, model_name)
    if not_ready is not None:
        return not_ready

    prompt = effective["prompt"]
    sampling_params = _sampling_params(effective)
    generate_kwargs = _generate_kwargs(effective)

    async def events() -> AsyncIterator[str]:
        try:
            async for token in runtime.generate_stream(
                model_name, prompt, sampling_params, **generate_kwargs
            ):
                yield _sse_event({"token": token})
        except Exception as err:  # noqa: BLE001 - one in-stream error event
            logger.error(
                "Streaming generation failed for model '%s': %s",
                model_name, err,
            )
            yield _sse_event({"error": {"reason": _failure_reason(err)}})
            return
        yield _sse_event({"done": True})

    return StreamingResponse(events(), media_type="text/event-stream")
