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
import { PATH_MAX } from "../constants";
import { NAME_REGEX } from "../../regex";

export const schema = yup.object({
  folderName: yup
    .string()
    .matches(NAME_REGEX, {
      message: "File prefix contains invalid characters.",
      excludeEmptyString: true,
    })
    .max(
      PATH_MAX,
      `File prefix is too long. A file prefix must be no longer than ${PATH_MAX} characters.`,
    ),
});
export type SchemaType = yup.InferType<typeof schema>;
