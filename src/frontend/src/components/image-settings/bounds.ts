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
import { CameraFeatureBounds } from "../../api/CameraAPI";
import { EXPOSURE_MAX, EXPOSURE_MIN, GAIN_MAX, GAIN_MIN } from "./constants";

/**
 * Normalized gain/exposure ranges used by the edit-settings form. Values come
 * from the camera's GenICam feature map when available, and fall back to the
 * static constants (used for Nvidia CSI / ICam or when the device cannot be
 * queried) so behavior never regresses.
 */
export interface SettingsBounds {
  gainMin: number;
  gainMax: number;
  exposureMin: number;
  exposureMax: number;
  // Human-readable unit for the exposure field label.
  exposureUnit: string;
}

export const DEFAULT_SETTINGS_BOUNDS: SettingsBounds = {
  gainMin: GAIN_MIN,
  gainMax: GAIN_MAX,
  exposureMin: EXPOSURE_MIN,
  exposureMax: EXPOSURE_MAX,
  // The legacy constants are expressed for the Nvidia CSI path (nanoseconds).
  exposureUnit: "nanoseconds",
};

function unitLabel(unit: string | null | undefined): string {
  if (unit === "us") return "microseconds";
  if (unit === "ns") return "nanoseconds";
  return DEFAULT_SETTINGS_BOUNDS.exposureUnit;
}

/**
 * Convert the generic device feature-bounds response into the numeric ranges
 * the form needs. Min is rounded up and max rounded down so the slider/input
 * (which store integers) always stay within what the hardware accepts.
 */
export function toSettingsBounds(data?: CameraFeatureBounds): SettingsBounds {
  if (!data) return DEFAULT_SETTINGS_BOUNDS;

  const gain = data.gain;
  const exposure = data.exposure;

  return {
    gainMin: gain?.min != null ? Math.ceil(gain.min) : GAIN_MIN,
    gainMax: gain?.max != null ? Math.floor(gain.max) : GAIN_MAX,
    exposureMin: exposure?.min != null ? Math.ceil(exposure.min) : EXPOSURE_MIN,
    exposureMax: exposure?.max != null ? Math.floor(exposure.max) : EXPOSURE_MAX,
    exposureUnit: unitLabel(exposure?.unit),
  };
}
