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
import * as yup from "yup";
import {
  EXPOSURE_MAX,
  EXPOSURE_MIN,
  GAIN_MAX,
  GAIN_MIN,
  PROCESSING_PIPELINE_MAX,
} from "../constants";

export interface EditSettingsSchemaBounds {
  gainMin: number;
  gainMax: number;
  exposureMin: number;
  exposureMax: number;
}

const DEFAULT_SCHEMA_BOUNDS: EditSettingsSchemaBounds = {
  gainMin: GAIN_MIN,
  gainMax: GAIN_MAX,
  exposureMin: EXPOSURE_MIN,
  exposureMax: EXPOSURE_MAX,
};

/**
 * Build the edit-settings validation schema. Gain/exposure limits are passed in
 * so they can reflect the connected camera's actual GenICam ranges; they
 * default to the static constants for non-Aravis sources or when the device
 * cannot be queried.
 */
export function makeSchema(bounds: EditSettingsSchemaBounds = DEFAULT_SCHEMA_BOUNDS) {
  return yup.object({
    editGain: yup
      .number()
      .typeError("A gain is required.")
      .required("A gain is required.")
      .min(
        bounds.gainMin,
        `Gain is invalid. A gain must be greater than or equal to ${bounds.gainMin}.`,
      )
      .max(
        bounds.gainMax,
        `Gain is invalid. A gain must be less than or equal to ${bounds.gainMax}.`,
      ),
    editExposure: yup
      .number()
      .typeError("An exposure is required.")
      .required("An exposure is required.")
      .min(
        bounds.exposureMin,
        `Exposure is invalid. An exposure must be greater than or equal to ${bounds.exposureMin}.`,
      )
      .max(
        bounds.exposureMax,
        `Exposure is invalid. An exposure must be less than or equal to ${bounds.exposureMax}.`,
      ),
    editGstreamerPipeline: yup
      .string()
      .required("A gstreamer pipeline is required.")
      .defined("A gstreamer pipeline is required.")
      .max(
        PROCESSING_PIPELINE_MAX,
        `Invalid gstreamer pipeline. A gstreamer pipeline must be no longer than ${PROCESSING_PIPELINE_MAX} characters`,
      ),
  });
}

export const schema = makeSchema();
export type SchemaType = yup.InferType<typeof schema>;
