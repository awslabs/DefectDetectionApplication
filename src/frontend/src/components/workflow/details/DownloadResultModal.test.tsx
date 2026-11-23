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
/**
 * rfdetr-seg-support Requirement 11.4: the time-range export modal gains a
 * "Manifest format" select. The default keeps today's request (no
 * manifestFormat, so `retrainInputImages` builds today's URL byte for byte);
 * choosing the instance option passes `manifestFormat: "instance"`, which the
 * API appends as `&manifest_format=instance`.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import axios from "axios";
import * as WorkflowAPI from "api/WorkflowAPI";
import { PredictionResult, retrainInputImages } from "api/WorkflowAPI";
import { APIList } from "config/Interface";
import DownloadResultModal, { MANIFEST_FORMAT_LABEL, manifestFormatOptions } from "./DownloadResultModal";

jest.mock("components/auth/authHook", () => ({
  __esModule: true,
  default: (): { token: string; authEnabled: boolean } => ({ token: "", authEnabled: false }),
}));

const WORKFLOW_ID = "wf-7";

describe("retrainInputImages", () => {
  const originalCreateElement = document.createElement.bind(document);
  let clickedHrefs: string[];

  beforeEach(() => {
    clickedHrefs = [];
    jest.spyOn(axios, "get").mockResolvedValue({ data: {} });
    jest.spyOn(document, "createElement").mockImplementation(((tagName: string) => {
      const element = originalCreateElement(tagName);
      if (tagName === "a") {
        element.click = (): void => {
          clickedHrefs.push((element as HTMLAnchorElement).href);
        };
      }
      return element;
    }) as typeof document.createElement);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  const request = {
    startTime: 1700000000,
    endTime: 1700003600,
    inputImageLimit: 25,
    predictionResult: PredictionResult.ALL,
  };

  it("builds today's URL when no manifest format is given", async () => {
    await retrainInputImages(WORKFLOW_ID, request);
    const [probe] = (axios.get as jest.Mock).mock.calls[0];
    expect(probe).toBe(
      `${APIList.workflows}/${WORKFLOW_ID}/results/export?startTime=1700000000&endTime=1700003600&predictionResult=all&inputImageLimit=1`,
    );
    expect(clickedHrefs).toHaveLength(1);
    expect(clickedHrefs[0]).toContain("&inputImageLimit=25");
    expect(clickedHrefs[0]).not.toContain("manifest_format");
  });

  it("appends manifest_format=instance for the instance manifest only", async () => {
    await retrainInputImages(WORKFLOW_ID, { ...request, manifestFormat: "instance" });
    const [probe] = (axios.get as jest.Mock).mock.calls[0];
    expect(probe).toContain("&manifest_format=instance&inputImageLimit=1");
    expect(clickedHrefs[0]).toContain("&manifest_format=instance&inputImageLimit=25");

    clickedHrefs.length = 0;
    (axios.get as jest.Mock).mockClear();
    await retrainInputImages(WORKFLOW_ID, { ...request, manifestFormat: "classification" });
    expect((axios.get as jest.Mock).mock.calls[0][0]).not.toContain("manifest_format");
    expect(clickedHrefs[0]).not.toContain("manifest_format");
  });
});

describe("DownloadResultModal", () => {
  let retrainSpy: jest.SpyInstance;

  beforeEach(() => {
    retrainSpy = jest.spyOn(WorkflowAPI, "retrainInputImages").mockResolvedValue(0);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  function pickRange(): void {
    const picker = createWrapper(document.body).findDateRangePicker();
    expect(picker).not.toBeNull();
    act(() => {
      picker!.openDropdown();
    });
    const dropdown = picker!.findDropdown();
    expect(dropdown).not.toBeNull();
    act(() => {
      dropdown!.findStartDateInput()!.setInputValue("2025/01/01");
      dropdown!.findStartTimeInput()!.setInputValue("00:00:00");
      dropdown!.findEndDateInput()!.setInputValue("2025/01/02");
      dropdown!.findEndTimeInput()!.setInputValue("00:00:00");
    });
    act(() => {
      dropdown!.findApplyButton().click();
    });
  }

  function downloadButton(): HTMLElement {
    return screen.getByRole("button", { name: "Download result images" });
  }

  it("offers the two manifest formats with Classification as the default", () => {
    render(<DownloadResultModal visible onClose={jest.fn()} workflowId={WORKFLOW_ID} />);
    expect(screen.getByText(MANIFEST_FORMAT_LABEL)).toBeInTheDocument();
    const select = createWrapper(document.body).findSelect('[data-testid="manifest-format-select"]');
    expect(select).not.toBeNull();
    expect(select!.findTrigger().getElement()).toHaveTextContent("Classification (default)");
    expect(manifestFormatOptions.map((option) => option.value)).toEqual(["classification", "instance"]);
  });

  it("requests no manifest format by default (today's download)", async () => {
    render(<DownloadResultModal visible onClose={jest.fn()} workflowId={WORKFLOW_ID} />);
    pickRange();
    await waitFor(() => expect(downloadButton()).toBeEnabled());
    // The click awaits the mocked API's promise and then clears the loading
    // state; flushing it inside act keeps the test free of act() warnings.
    await act(async () => {
      downloadButton().click();
      await Promise.resolve();
    });
    await waitFor(() => expect(retrainSpy).toHaveBeenCalledTimes(1));
    const [workflowId, config] = retrainSpy.mock.calls[0];
    expect(workflowId).toBe(WORKFLOW_ID);
    expect(config).toEqual({
      startTime: expect.any(Number),
      endTime: expect.any(Number),
      inputImageLimit: 1,
      predictionResult: PredictionResult.ALL,
    });
    expect("manifestFormat" in config).toBe(false);
  });

  it("passes manifestFormat=instance when the instance option is chosen", async () => {
    render(<DownloadResultModal visible onClose={jest.fn()} workflowId={WORKFLOW_ID} />);
    const select = createWrapper(document.body).findSelect('[data-testid="manifest-format-select"]');
    act(() => {
      select!.openDropdown();
    });
    act(() => {
      select!.selectOptionByValue("instance");
    });
    expect(select!.findTrigger().getElement()).toHaveTextContent("Instance segmentation (RF-DETR-Seg masks)");
    pickRange();
    await waitFor(() => expect(downloadButton()).toBeEnabled());
    // The click awaits the mocked API's promise and then clears the loading
    // state; flushing it inside act keeps the test free of act() warnings.
    await act(async () => {
      downloadButton().click();
      await Promise.resolve();
    });
    await waitFor(() => expect(retrainSpy).toHaveBeenCalledTimes(1));
    expect(retrainSpy.mock.calls[0][1]).toMatchObject({ manifestFormat: "instance" });
  });
});
