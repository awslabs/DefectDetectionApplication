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
import { CAPTURE_COUNT_MAX, CAPTURE_COUNT_MIN, CAPTURE_INTERVAL_MAX, CAPTURE_INTERVAL_MIN } from "./constants";

export const captureConfigSchema = yup.object({
  count: yup
    .number()
    .typeError("Capture count is required.")
    .integer("Capture count should be an integer.")
    .required("Capture count is required.")
    .min(CAPTURE_COUNT_MIN, `Capture count cannot be less than ${CAPTURE_COUNT_MIN}.`)
    .max(CAPTURE_COUNT_MAX, `Capture count cannot be more than ${CAPTURE_COUNT_MAX}.`),
  interval: yup
    .number()
    .integer("Capture interval should be an integer.")
    .when("count", {
      is: (value: number) => value > 1,
      then: (schema) =>
        schema
          .typeError("Capture interval is required.")
          .required("Capture interval is required.")
          .max(CAPTURE_INTERVAL_MAX, "Capture interval is too long.")
          .min(CAPTURE_INTERVAL_MIN, `Capture interval cannot be less than ${CAPTURE_INTERVAL_MIN} second.`),
      otherwise: (schema) =>
        schema.notRequired(),
    }),
  filePrefix: yup.string(),
});
export type CaptureConfigSchemaType = yup.InferType<typeof captureConfigSchema>;