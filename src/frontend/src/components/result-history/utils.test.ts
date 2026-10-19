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
 * Result-history export-link building (localserver-input-handling-hardening
 * Item 3c, owner decision D5 (b)). buildExportUrl percent-encodes
 * captureIdPath and joins the credential as its own "&token=" parameter only
 * when auth is on, so the server receives the path and (with auth on) the
 * credential as two separate query parameters. The call-site check pins that
 * ResultCardContent builds the download link through the helper and no longer
 * carries the mis-joined "token=" template. Authored on the unfixed tree,
 * where it fails (no helper, and the template is present), and frozen.
 */
import * as fs from "fs";
import * as path from "path";
import fc from "fast-check";

import { buildExportUrl } from "./utils";

const ENDPOINT = "https://host.invalid:5000/workflows";
const WORKFLOW_ID = "wf-7";
const WRITER_PATH = "/aws_dda/system/capture-id-path-2025-01-01-00-00-00";

describe("buildExportUrl", () => {
  it("percent-encodes captureIdPath and adds no token when auth is off", () => {
    const url = buildExportUrl({
      workflowsEndpoint: ENDPOINT,
      workflowId: WORKFLOW_ID,
      captureIdPath: WRITER_PATH,
      authEnabled: false,
      token: "ignored",
    });
    const parsed = new URL(url);
    expect(parsed.pathname).toBe(`/workflows/${WORKFLOW_ID}/results/export`);
    expect(parsed.searchParams.get("captureIdPath")).toBe(WRITER_PATH);
    expect(parsed.searchParams.has("token")).toBe(false);
  });

  it("joins the credential as its own token parameter when auth is on", () => {
    const token = "local-session.token-ABC/+=&value";
    const url = buildExportUrl({
      workflowsEndpoint: ENDPOINT,
      workflowId: WORKFLOW_ID,
      captureIdPath: WRITER_PATH,
      authEnabled: true,
      token,
    });
    const parsed = new URL(url);
    expect(parsed.searchParams.get("captureIdPath")).toBe(WRITER_PATH);
    expect(parsed.searchParams.get("token")).toBe(token);
  });

  it("keeps reserved characters in captureIdPath intact", () => {
    const tricky = "/aws_dda/system/weird &=#?+ name";
    const url = buildExportUrl({
      workflowsEndpoint: ENDPOINT,
      workflowId: WORKFLOW_ID,
      captureIdPath: tricky,
      authEnabled: false,
      token: "",
    });
    expect(new URL(url).searchParams.get("captureIdPath")).toBe(tricky);
  });

  // Property 10: for any workflowId, captureIdPath and credential, the parsed
  // query holds exactly the path and (iff auth is on) exactly the credential
  // as token, under the .../results/export path.
  it("round-trips the path and the credential for any input (property)", () => {
    // workflowId is a server-issued, URL-safe id (kept unencoded, as the
    // route path is unchanged); captureIdPath and token are the untrusted
    // values the helper must encode, so they range over arbitrary strings.
    const workflowIdArb = fc
      .array(fc.constantFrom(..."abcdefghijklmnopqrstuvwxyz0123456789-_".split("")))
      .map((chars) => chars.join(""));
    fc.assert(
      fc.property(
        workflowIdArb,
        fc.string(),
        fc.string(),
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

describe("the result-history download link", () => {
  it("is built through buildExportUrl with no mis-joined token template", () => {
    const source = fs.readFileSync(
      path.resolve(__dirname, "ResultCardContent.tsx"),
      "utf8",
    );
    expect(source).toContain("buildExportUrl(");
    // The shipped template appended token= with no '&' before it; it is gone.
    expect(source).not.toContain('token=${encodeURIComponent(token)}`');
    expect(source).not.toContain("results/export?captureIdPath=");
  });
});
