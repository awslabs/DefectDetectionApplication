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

export const HIDE_SIDE_NAV_ROUTES = ["add", "create", "edit", "edit-settings", "result", "capture"];

export const styleConstants = {
  IMAGE_CONTENT_CONTAINER_MIN_WIDTH: 960,
  PARAGRAPH_MAX_WIDTH: 840,
  MAX_CONTENT_WIDTH: 1460,
}

export enum DynamicRouterHashKey {
  IMAGE_SOURCE_NAME = "imageSourceName",
  WORKFLOW_NAME = "workflowName",
}

export enum TableTypes {
  RESULT_HISTORY = "resultHistory",
  CAPTURE_HISTORY = "captureHistory",
}