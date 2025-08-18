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
import CapturedCards from "components/image-source/image-capture/CapturedCards";

const workflowId = "workflowId";
const path = "path";
const image = "image";

it("shows empty message", () => {
  cy.intercept("**/captured-images**", []);
  cy.mountWithProviders(<CapturedCards captureResultsHref="" workflowId={workflowId} />);

  cy.findByText("No captured images").should("exist");
  cy.findByText("No images have been captured.").should("exist");
});

it("shows an image", () => {
  cy.intercept("**/captured-images**", [{ path, image }]);
  cy.mountWithProviders(<CapturedCards captureResultsHref="" workflowId={workflowId} />);

  cy.findAllByRole("radio").should("have.length", 1);
  cy.findAllByRole("radio", { name: `Item selection select ${path}` }).should(
    "have.length",
    1,
  );
});

it("shows 4 cards per page", () => {
  cy.intercept(
    "**/captured-images**",
    Array(4)
      .fill(0)
      .map((_value, index) => ({ path: `path${index}`, image })),
  );
  cy.mountWithProviders(<CapturedCards captureResultsHref="" workflowId={workflowId} />);

  cy.findAllByRole("radio").should("have.length", 4);
});

it("shows 2 pages for 5 images", () => {
  cy.intercept(
    "**/captured-images**",
    Array(5)
      .fill(0)
      .map((_value, index) => ({ path: `path${index}`, image })),
  );
  cy.mountWithProviders(<CapturedCards captureResultsHref="" workflowId={workflowId} />);

  cy.findByRole("button", { name: "Page 2 of all pages" }).click();

  cy.findAllByRole("radio", { name: `Item selection select ${path}4` }).should(
    "have.length",
    1,
  );
});

it("deletes image", () => {
  cy.intercept("**/captured-images**", { method: "GET", times: 1 }, []);
  cy.intercept("**/captured-images**", { method: "GET", times: 1 }, [
    { path, image },
  ]);
  cy.intercept("DELETE", "**/captured-images**", { imageFilePath: path });
  cy.mountWithProviders(<CapturedCards captureResultsHref="" workflowId={workflowId} />);

  cy.findByRole("radio", { name: `Item selection select ${path}` }).click();
  cy.findByRole("button", { name: "Delete image" }).click();

  cy.findByText("No captured images").should("exist");
});
