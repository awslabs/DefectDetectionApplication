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
export async function mockGetCameraList() {
  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
  await sleep(2 * 1000);
  return [
    { label: "Camera 1", value: "1" },
    { label: "Camera 2", value: "2" },
    { label: "Camera 3", value: "3" },
    { label: "Camera 4", value: "4" },
    { label: "Camera 5", value: "5" },
  ];
}

export function mockGetPreviewImage() {
  return require("../static/SamplePic1.png");
}
