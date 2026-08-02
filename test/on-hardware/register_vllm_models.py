#!/usr/bin/env python3
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
"""Registration seed script for the JP6 vLLM on-hardware validation.

Feature: jp6-vllm-enablement (Requirement 5.7). Registers the two
validation models from ``test/on-hardware/jp6_vllm_validation.md`` through
the portal Register LLM API (``POST /api/v1/models/vllm``) with exactly the
engine configurations documented for the 64 GB AGX Orin target:

    +-----------------+----------------------------+------------------------+
    |                 | Smoke_Model                | Realistic_Model        |
    +-----------------+----------------------------+------------------------+
    | Model name      | opt125m-smoke              | qwen25-7b-instruct     |
    | HF model ID     | facebook/opt-125m          | Qwen/Qwen2.5-7B-       |
    |                 |                            | Instruct               |
    | gpu_memory_util | 0.3                        | 0.55                   |
    | max_model_len   | 2048                       | 8192                   |
    | other settings  | documented defaults (dtype=auto,                    |
    |                 | tensor_parallel_size=1, enforce_eager=true)         |
    +-----------------+----------------------------+------------------------+

Running this script replaces Stage 2's manual form entry in the validation
procedure. It is idempotent: model names already present in the use case's
model list (``GET /api/v1/models?usecase_id=...``) are skipped, so re-runs
after a partial failure register only what is missing.

Usage:

    ./register_vllm_models.py \
        --portal-api https://<api-id>.execute-api.<region>.amazonaws.com/prod \
        --token "$TOKEN" --usecase-id <usecase_id>

    # or via environment variables
    PORTAL_API=https://.../prod PORTAL_TOKEN=... PORTAL_USECASE_ID=... \
        ./register_vllm_models.py

    # print the request payloads without calling the portal
    ./register_vllm_models.py --dry-run ...

``--portal-api`` is the API base WITHOUT the ``/api/v1`` suffix (the same
``$PORTAL_API`` convention as the curl examples in the validation doc).
``--token`` is a portal bearer token for a user holding the DataScientist
role on the use case. Standard library only — no extra dependencies on the
build server or a tester workstation.

Exit code 0 when every model ends up registered or already present;
non-zero on any failure.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# The two validation models (jp6_vllm_validation.md, Stage 2). Engine
# settings are supplied in full so the stored configuration matches the
# documented values exactly (omitted keys would receive the same documented
# defaults from the portal's engine spec).
MODELS = [
    {
        "label": "Smoke_Model",
        "model_name": "opt125m-smoke",
        "model_version": "1.0",
        "huggingface_model_id": "facebook/opt-125m",
        "engine_configuration": {
            "dtype": "auto",
            "gpu_memory_utilization": 0.3,
            "max_model_len": 2048,
            "tensor_parallel_size": 1,
            "enforce_eager": True,
        },
        "description": (
            "jp6-vllm-enablement on-hardware validation Smoke_Model: fast "
            "end-to-end pipeline smoke (register->package->publish->deploy->"
            "generate). gpu_memory_utilization=0.3 leaves headroom for the "
            "vision-coexistence stage."
        ),
    },
    {
        "label": "Realistic_Model",
        "model_name": "qwen25-7b-instruct",
        "model_version": "1.0",
        "huggingface_model_id": "Qwen/Qwen2.5-7B-Instruct",
        "engine_configuration": {
            "dtype": "auto",
            "gpu_memory_utilization": 0.55,
            "max_model_len": 8192,
            "tensor_parallel_size": 1,
            "enforce_eager": True,
        },
        "description": (
            "jp6-vllm-enablement on-hardware validation Realistic_Model: "
            "mid-size instruction-following workload sized for the 64 GB "
            "AGX Orin (gpu_memory_utilization=0.55, max_model_len=8192)."
        ),
    },
]


def _request(method, url, token, body=None, timeout=30):
    """Issue one portal API request. Returns (status_code, parsed_json).

    HTTP error statuses are returned (not raised) so callers can surface
    the portal's structured error payloads (e.g. 400 validation findings).
    """
    data = None
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            payload = {"error": raw}
        return err.code, payload


def list_registered_names(portal_api, token, usecase_id):
    """Return {model_name: model_type} for the use case's model list."""
    url = "{}/api/v1/models?{}".format(
        portal_api, urllib.parse.urlencode({"usecase_id": usecase_id})
    )
    status, payload = _request("GET", url, token)
    if status != 200:
        raise RuntimeError(
            "listing models failed: HTTP {} {}".format(
                status, json.dumps(payload)
            )
        )
    return {
        model.get("name"): model.get("model_type")
        for model in payload.get("models", [])
    }


def register_model(portal_api, token, usecase_id, model):
    """POST one registration. Returns the training_id on success."""
    body = {
        "usecase_id": usecase_id,
        "model_name": model["model_name"],
        "model_version": model["model_version"],
        "huggingface_model_id": model["huggingface_model_id"],
        "engine_configuration": model["engine_configuration"],
        "description": model["description"],
    }
    url = portal_api + "/api/v1/models/vllm"
    status, payload = _request("POST", url, token, body=body)
    if status != 201:
        detail = json.dumps(payload, indent=2)
        if status == 400 and "findings" in payload:
            detail = "validation findings:\n" + "\n".join(
                "  - {field}: {reason} (value: {value})".format(**finding)
                for finding in payload["findings"]
            )
        elif status == 403:
            detail += (
                "\n  (the portal user needs the DataScientist role on the "
                "use case)"
            )
        raise RuntimeError(
            "registering {} failed: HTTP {} {}".format(
                model["model_name"], status, detail
            )
        )
    return payload.get("training_id")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Register the jp6-vllm-enablement validation models "
            "(opt125m-smoke, qwen25-7b-instruct) through the portal "
            "Register LLM API. Idempotent: already-registered names are "
            "skipped."
        )
    )
    parser.add_argument(
        "--portal-api",
        default=os.environ.get("PORTAL_API"),
        help=(
            "portal API base URL without the /api/v1 suffix "
            "(env: PORTAL_API)"
        ),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("PORTAL_TOKEN"),
        help="portal bearer token, DataScientist role (env: PORTAL_TOKEN)",
    )
    parser.add_argument(
        "--usecase-id",
        default=os.environ.get("PORTAL_USECASE_ID"),
        help="target use case id (env: PORTAL_USECASE_ID)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the registration payloads and exit without calling "
        "the portal",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        for model in MODELS:
            body = {
                "usecase_id": args.usecase_id or "<usecase_id>",
                "model_name": model["model_name"],
                "model_version": model["model_version"],
                "huggingface_model_id": model["huggingface_model_id"],
                "engine_configuration": model["engine_configuration"],
                "description": model["description"],
            }
            print("# {} -> POST /api/v1/models/vllm".format(model["label"]))
            print(json.dumps(body, indent=2))
        return 0

    missing = [
        name
        for name, value in (
            ("--portal-api / PORTAL_API", args.portal_api),
            ("--token / PORTAL_TOKEN", args.token),
            ("--usecase-id / PORTAL_USECASE_ID", args.usecase_id),
        )
        if not value
    ]
    if missing:
        parser.error("missing required settings: " + ", ".join(missing))

    portal_api = args.portal_api.rstrip("/")

    try:
        registered = list_registered_names(
            portal_api, args.token, args.usecase_id
        )
    except (RuntimeError, urllib.error.URLError) as err:
        print("ERROR: {}".format(err), file=sys.stderr)
        return 1

    failures = 0
    for model in MODELS:
        name = model["model_name"]
        if name in registered:
            model_type = registered[name]
            note = "" if model_type == "vllm" else (
                " (existing record has model_type={!r}, not 'vllm' — "
                "verify it is the intended record)".format(model_type)
            )
            print(
                "SKIP  {} ({}): already registered{}".format(
                    name, model["label"], note
                )
            )
            continue
        try:
            training_id = register_model(
                portal_api, args.token, args.usecase_id, model
            )
        except (RuntimeError, urllib.error.URLError) as err:
            print("ERROR: {}".format(err), file=sys.stderr)
            failures += 1
            continue
        print(
            "OK    {} ({}): registered, training_id={}".format(
                name, model["label"], training_id
            )
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
