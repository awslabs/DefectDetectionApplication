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
import { ImageSourceType } from "../types";
import { NAME_REGEX } from "../../regex";
import { NAME_MAX, DESCRIPTION_MAX } from "components/form/constants";
import { PATH_MAX } from "../constants";

export const schema = yup.object({
  type: yup.mixed<ImageSourceType>().oneOf(Object.values(ImageSourceType)),
  cameraName: yup.string().when("type", {
    is: ImageSourceType.Camera,
    then: (schema) =>
      schema
        .required("An image source name is required.")
        .matches(NAME_REGEX, "Image source name contains invalid characters.")
        .max(
          NAME_MAX,
          `Image source name is too long. An image source name can have a maximum of ${NAME_MAX} characters.`,
        ),
  }),
  cameraDescription: yup.string().when("type", {
    is: ImageSourceType.Camera,
    then: (schema) =>
      schema.max(
        DESCRIPTION_MAX,
        `Description is too long. A description can have a maximum of ${DESCRIPTION_MAX} characters.`,
      ),
  }),
  folderName: yup.string().when("type", {
    is: ImageSourceType.Folder,
    then: (schema) =>
      schema
        .required("An image source name is required.")
        .matches(NAME_REGEX, "Image source name contains invalid characters.")
        .max(
          NAME_MAX,
          `Image source name is too long. An image source name can have a maximum of ${NAME_MAX} characters.`,
        ),
  }),
  folderDescription: yup.string().when("type", {
    is: ImageSourceType.Folder,
    then: (schema) =>
      schema.max(
        DESCRIPTION_MAX,
        `Description is too long. A description can have a maximum of ${DESCRIPTION_MAX} characters.`,
      ),
  }),
  nvidiaCSIName: yup.string().when("type", {
    is: ImageSourceType.NvidiaCSI,
    then: (schema) =>
      schema
        .required("An image source name is required.")
        .matches(NAME_REGEX, "Image source name contains invalid characters.")
        .max(
          NAME_MAX,
          `Image source name is too long. An image source name can have a maximum of ${NAME_MAX} characters.`,
        ),
  }),
  nvidiaCSIDescription: yup.string().when("type", {
    is: ImageSourceType.NvidiaCSI,
    then: (schema) =>
      schema.max(
        DESCRIPTION_MAX,
        `Description is too long. A description can have a maximum of ${DESCRIPTION_MAX} characters.`,
      ),
  }),
  // TODO: Maybe we need some regex on this
  path: yup.string().when("type", {
    is: ImageSourceType.Folder,
    then: (schema) =>
      schema
        .required("A folder path is required.")
        .max(
          PATH_MAX,
          `Folder path is too long. A folder path can have a maximum of ${PATH_MAX} characters.`,
        ),
  }),
});
export type SchemaType = yup.InferType<typeof schema>;
