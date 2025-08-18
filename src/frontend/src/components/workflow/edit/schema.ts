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
import { WorkflowTrigger, SignalType } from "../types";
import { NAME_REGEX } from "../../regex";
import { ImageSourceType } from "../../image-source/types";
import { SelectProps } from "@cloudscape-design/components";
import outputSchema from "./outputs/schema";
import { NAME_MAX, DESCRIPTION_MAX } from "components/form/constants";

// Being generous with types since any possible value may be entered
const emptyStringToNull = (value: any, originalValue: any): any => {
  if (typeof originalValue === "string" && originalValue === "") {
    return null;
  }
  return value;
};

// Helper for common check
const isValidDigitalInput = (
  trigger: WorkflowTrigger,
  source: SelectProps.Option,
): boolean => trigger === WorkflowTrigger.DigitalInput;

const schema = yup.object({
  name: yup
    .string()
    .required("A workflow name is required.")
    .matches(NAME_REGEX, "Workflow name contains invalid characters.")
    .max(
      NAME_MAX,
      `Workflow name is too long. A workflow name can have a maximum of ${NAME_MAX} characters.`,
    ),
  description: yup
    .string()
    .max(
      DESCRIPTION_MAX,
      `Description is too long. A description can have a maximum of ${DESCRIPTION_MAX} characters.`,
    ),
  source: yup
    .object({
      description: yup.string(),
      value: yup.string(),
      label: yup.string(),
    })
    .nullable()
    .test(
      "required",
      "An image source is required.",
      (select) => !!select?.value,
    ),
  model: yup
    .object({
      value: yup.string(),
      label: yup.string(),
    })
    .nullable(),
  trigger: yup.string().oneOf(Object.values(WorkflowTrigger)),
  signal: yup.string().when(["trigger", "source"], {
    is: isValidDigitalInput,
    then: (schema) => schema.oneOf(Object.values(SignalType)),
  }),
  pin: yup.number().when(["trigger", "source"], {
    is: isValidDigitalInput,
    then: (schema) =>
      schema
        .typeError("A pin value is required.")
        .integer(
          "Pin value is invalid. A pin value cannot have decimal values.",
        )
        .min(
          0,
          "Pin value is invalid. A pin value must be greater than or equal to zero.",
        )
        .required("A pin value is required."),
    // Not great, but handles empty string and prevents type error. For more
    // info: https://github.com/jquense/yup/issues/298#issuecomment-990816641
    otherwise: (schema) => schema.transform(emptyStringToNull).nullable(),
  }),
  debounce: yup.number().when(["trigger", "source"], {
    is: isValidDigitalInput,
    then: (schema) =>
      schema
        .typeError("A debounce time is required.")
        .integer(
          "Debounce time is invalid. A debounce time cannot have decimal values.",
        )
        .min(
          0,
          "Debounce time is invalid. A debounce time must be greater than or equal to zero.",
        )
        .required("A debounce time is required."),
    // Not great, but handles empty string and prevents type error. For more
    // info: https://github.com/jquense/yup/issues/298#issuecomment-990816641
    otherwise: (schema) => schema.transform(emptyStringToNull).nullable(),
  }),
  outputs: yup.array().of(outputSchema),
});
export default schema;
export type SchemaType = yup.InferType<typeof schema>;
