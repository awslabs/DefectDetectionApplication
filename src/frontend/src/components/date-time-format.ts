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

export const DATE_WITHOUT_TZ = "MMMM d, y, HH:mm";
export const DATE_SECOND_WITHOUT_TZ = "MMMM d, y, HH:mm:ss";
export const DATE_TZ_OFFSET = "'(UTC'xxx')'";
export const DATE_FORMAT = `${DATE_WITHOUT_TZ} ${DATE_TZ_OFFSET}`;
export const DATE_SECOND_FORMAT = `${DATE_SECOND_WITHOUT_TZ} ${DATE_TZ_OFFSET}`;