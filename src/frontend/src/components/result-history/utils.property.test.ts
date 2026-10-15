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

/*
 * Result-history export-link building, wide-input property
 * (localserver-input-handling-hardening Item 3c, owner decision D5 (b)). The
 * unit property in utils.test.ts draws fc.string(), which in the installed
 * fast-check defaults to grapheme-ascii. This file widens the inputs to the
 * range design Property 10 names: full-Unicode strings (fc.fullUnicodeString
 * never produces lone surrogates, which encodeURIComponent rejects and neither
 * input can hold) and strings dense in the reserved characters & = # + % / ?
 * and space. For any such captureIdPath and credential, parsing the built link
 * returns exactly the captureIdPath and (with auth on) exactly the credential.
 */
import fc from "fast-check";

import { buildExportUrl } from "./utils";

const ENDPOINT = "https://host.invalid:5000/workflows";

// A server-issued, URL-safe id; the route path segment is unchanged, so it is
// not encoded by the helper.
const workflowIdArb = fc
  .array(fc.constantFrom(..."abcdefghijklmnopqrstuvwxyz0123456789-_".split("")))
  .map((chars) => chars.join(""));

// Dense in the characters that are reserved in a URL query.
const reservedDenseArb = fc
  .array(fc.constantFrom("&", "=", "#", "+", "%", "/", "?", " "))
  .map((chars) => chars.join(""));

// The untrusted values the helper must encode: full Unicode or reserved-dense.
const untrustedArb = fc.oneof(fc.fullUnicodeString(), reservedDenseArb);

describe("buildExportUrl (wide inputs, property)", () => {
  it("round-trips full-Unicode and reserved-dense path and credential", () => {
    fc.assert(
      fc.property(
        workflowIdArb,
        untrustedArb,
        untrustedArb,
        fc.boolean(),
        (workflowId, captureIdPath, token, authEnabled) => {
          const url = buildExportUrl({
            workflowsEndpoint: ENDPOINT,
            workflowId,
            captureIdPath,
            authEnabled,
            token,
          });
          const parsed = new URL(url);
          expect(parsed.pathname).toBe(`/workflows/${workflowId}/results/export`);
          expect(parsed.searchParams.get("captureIdPath")).toBe(captureIdPath);
          if (authEnabled) {
            expect(parsed.searchParams.get("token")).toBe(token);
          } else {
            expect(parsed.searchParams.has("token")).toBe(false);
          }
        },
      ),
      { numRuns: 200 },
    );
  });
});
