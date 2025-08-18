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
import { SignalType } from "../../types";

const schema = yup.object({
  signal: yup.string().oneOf(Object.values(SignalType)),
  pin: yup
    .number()
    .nullable()
    .typeError("A pin value is required.")
    .required("A pin value is required.")
    .integer("Pin value is invalid. A pin value cannot have decimal values.")
    .min(
      0,
      "Pin value is invalid. A pin value must be greater than or equal to zero.",
    ),
  debounce: yup
    .number()
    .nullable()
    .typeError("A pulse width is required.")
    .required("A pulse width is required.")
    .integer(
      "Pulse width is invalid. A pulse width cannot have decimal values.",
    )
    .min(
      0,
      "Pulse width is invalid. A pulse width must be greater than or equal to zero.",
    ),
  rule: yup
    .object({ value: yup.string() })
    .nullable()
    .test("required", "A rule is required.", (select) => !!select?.value),
});
export default schema;

// Infer the type so we can use for submit parameter
export type SchemaType = yup.InferType<typeof schema>;
