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
import { FOLDER_PREFIX } from "../constants";

export const getEditPath = (fullPath?: string): string => {
  if (!fullPath) {
    return "";
  }

  if (fullPath?.startsWith(FOLDER_PREFIX)) {
    return fullPath.substring(FOLDER_PREFIX.length);
  }

  // Should not get to this case
  return fullPath;
};
