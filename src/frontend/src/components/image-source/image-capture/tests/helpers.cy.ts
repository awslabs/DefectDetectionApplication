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
import { getCapturedImageName, getCapturedImageTime } from "../helpers";

it("gets name", () => {
  expect(getCapturedImageName("test/file.jpg")).to.be.equal("file.jpg");
});

it("returns default if name invalid", () => {
  expect(getCapturedImageName("")).to.be.equal("-");
});

it("gets time", () => {
  const time = getCapturedImageTime("test/prefix-id-1685059159785.jpg");
  expect(Date.parse(time)).to.be.equal(1685059140000);
});

it("returns default if time is invalid", () => {
  const time = getCapturedImageTime("");
  expect(time).to.be.equal("-");
});
