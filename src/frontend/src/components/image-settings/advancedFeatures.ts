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
import { CameraFeatureBound, CameraFeatureBounds } from "../../api/CameraAPI";

/**
 * Display metadata for the advanced (Tier 2 / Tier 3) device controls. The
 * order here is the order they render in. Only features the device actually
 * reports (and that carry the `advanced` flag) are shown, so this list can
 * safely describe more than any single camera supports.
 */
interface AdvancedFeatureMeta {
  key: string;
  label: string;
  description?: string;
}

const ADVANCED_FEATURE_META: AdvancedFeatureMeta[] = [
  {
    key: "balanceWhiteAuto",
    label: "Auto white balance",
    description: "Automatic white balance mode (color cameras).",
  },
  { key: "reverseX", label: "Flip horizontal" },
  { key: "reverseY", label: "Flip vertical" },
  {
    key: "pixelFormat",
    label: "Pixel format",
    description:
      "Changing the pixel format can affect the processing pipeline and model input.",
  },
  { key: "width", label: "Width (px)" },
  { key: "height", label: "Height (px)" },
  { key: "offsetX", label: "Offset X (px)" },
  { key: "offsetY", label: "Offset Y (px)" },
];

export interface AdvancedFeature extends AdvancedFeatureMeta {
  bound: CameraFeatureBound;
}

/**
 * Extract the advanced controls a given camera actually supports, in display
 * order. Returns an empty array when the camera reports none (so the section
 * can be hidden entirely).
 */
export function getSupportedAdvancedFeatures(
  bounds?: CameraFeatureBounds,
): AdvancedFeature[] {
  if (!bounds) return [];
  const supported: AdvancedFeature[] = [];
  for (const meta of ADVANCED_FEATURE_META) {
    const bound = bounds[meta.key];
    if (bound && bound.advanced && bound.available !== false) {
      supported.push({ ...meta, bound });
    }
  }
  return supported;
}

// Advanced controls live in the same react-hook-form as gain/exposure under
// these field names, so the preview/save include them and apply them through
// the acquisition path (rather than a separate live-apply call).
export const ADV_FIELD_PREFIX = "adv_";

export function advFieldName(key: string): string {
  return `${ADV_FIELD_PREFIX}${key}`;
}

type FeatureValue = string | number | boolean;

/**
 * Build the advanced portion of an imageSourceConfiguration from current form
 * values, keyed by the config keys the backend expects (reverseX, pixelFormat,
 * width, ...). Only includes features the connected camera supports.
 */
export function buildAdvancedConfig(
  bounds: CameraFeatureBounds | undefined,
  formValues: Record<string, any> | undefined,
): Record<string, FeatureValue> {
  const out: Record<string, FeatureValue> = {};
  if (!bounds || !formValues) return out;
  for (const f of getSupportedAdvancedFeatures(bounds)) {
    const v = formValues[advFieldName(f.key)];
    if (v !== undefined && v !== null && v !== "") out[f.key] = v;
  }
  return out;
}
