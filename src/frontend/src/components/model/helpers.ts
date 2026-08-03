/*
 *
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

import { FeatureConfigurationType } from "components/workflow/types";

export function modelNameString(version: string | null | undefined) {
  if (!version) return "-";
  return `v${version}`;
}

/**
 * Human-friendly label for the model's runtime/target type.
 * The on-device feature-configuration type distinguishes the legacy LFV
 * (Neo/DLR) models from Triton-served models.
 */
export function modelTypeLabel(
  type: FeatureConfigurationType | string | null | undefined,
): string {
  switch (type) {
    case FeatureConfigurationType.LFVModel:
      return "LFV (Neo/DLR)";
    case FeatureConfigurationType.TritonModel:
      return "Triton";
    case FeatureConfigurationType.VllmModel:
      return "vLLM";
    default:
      return type ? String(type) : "-";
  }
}

/**
 * Best-effort extraction of the model input shape from the model metadata.
 *
 * `modelMetaData` is a free-form string that, when the model is loaded, holds
 * the Triton model metadata JSON (which includes an `inputs` array with each
 * input's `name`, `datatype` and `shape`). Older/unloaded models may store an
 * empty value or a plain description string, so every step is defensive and we
 * fall back to "-" rather than throwing.
 *
 * Returns a formatted string like `input: [1,3,224,224]` (or a comma-joined
 * list when there are multiple inputs), or "-" when no shape is available.
 */
export function modelShapeString(
  modelMetaData: string | null | undefined,
): string {
  if (!modelMetaData) return "-";

  let meta: unknown;
  try {
    meta = JSON.parse(modelMetaData);
  } catch {
    // Not JSON (e.g. a plain description) — nothing to extract.
    return "-";
  }

  if (!meta || typeof meta !== "object") return "-";
  const inputs = (meta as { inputs?: unknown }).inputs;
  if (!Array.isArray(inputs) || inputs.length === 0) return "-";

  const parts: string[] = [];
  for (const input of inputs) {
    if (!input || typeof input !== "object") continue;
    const shape = (input as { shape?: unknown }).shape;
    const name = (input as { name?: unknown }).name;
    if (!Array.isArray(shape)) continue;
    const dims = shape
      .map((d) => (d === -1 || d === "-1" ? "?" : String(d)))
      .join(",");
    parts.push(name ? `${name}: [${dims}]` : `[${dims}]`);
  }

  return parts.length > 0 ? parts.join("  ") : "-";
}
