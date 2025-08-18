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

export const schema = yup.object({
  editGain: yup
    .number()
    .typeError("A gain is required.")
    .required("A gain is required.")
    .min(
      GAIN_MIN,
      `Gain is invalid. A gain must be greater than or equal to ${GAIN_MIN}.`,
    )
    .max(
      GAIN_MAX,
      `Gain is invalid. A gain must be less than or equal to ${GAIN_MAX}.`,
    ),
  editExposure: yup
    .number()
    .typeError("An exposure is required.")
    .required("An exposure is required.")
    .min(
      EXPOSURE_MIN,
      `Exposure is invalid. An exposure must be greater than or equal to ${EXPOSURE_MIN}.`,
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
export type SchemaType = yup.InferType<typeof schema>;
