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

import { DATE_FORMAT } from "components/date-time-format";
import format from "date-fns/format";

export const getCapturedImageName = (imageFilePath: string): string =>
  imageFilePath.split("/").pop() || "-";

export const getCapturedImageTime = (path: string): string => {
  const fileName = path.split("/").pop();
  if (!fileName) return "-";

  const ending = fileName.split("-").pop();
  if (!ending) return "-";

  const time = ending.split(".")[0];
  if (!time) return "-";

  const num = parseInt(time);
  if (!num) return "-";

  return format(new Date(num), DATE_FORMAT);
};
