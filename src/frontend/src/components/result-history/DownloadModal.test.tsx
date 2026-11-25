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
 * rfdetr-seg-support Requirement 11.4: the selected-rows download offers
 * "Download with instance-segmentation manifest" beside the existing download
 * only when a selected result stores an instance mask; the existing download
 * still requests no manifest format.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import DownloadModal, { INSTANCE_MANIFEST_DOWNLOAD_LABEL } from "./DownloadModal";

describe("DownloadModal", () => {
  it("offers only today's download when no selected result stores a mask", () => {
    const onDownload = jest.fn();
    render(
      <DownloadModal showModal onClose={jest.fn()} onDownload={onDownload} isDownloading={false} />,
    );
    expect(screen.queryByText(INSTANCE_MANIFEST_DOWNLOAD_LABEL)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(onDownload).toHaveBeenCalledTimes(1);
    expect(onDownload.mock.calls[0]).toEqual([true]);
  });

  it("offers the instance-manifest download beside it when a selected result stores a mask", () => {
    const onDownload = jest.fn();
    render(
      <DownloadModal
        showModal
        onClose={jest.fn()}
        onDownload={onDownload}
        isDownloading={false}
        instanceManifestAvailable
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: INSTANCE_MANIFEST_DOWNLOAD_LABEL }));
    expect(onDownload.mock.calls[0]).toEqual([true, "instance"]);
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(onDownload.mock.calls[1]).toEqual([true]);
  });

  it("passes the mark-as-downloaded choice to both downloads", () => {
    const onDownload = jest.fn();
    render(
      <DownloadModal
        showModal
        onClose={jest.fn()}
        onDownload={onDownload}
        isDownloading={false}
        instanceManifestAvailable
      />,
    );
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: INSTANCE_MANIFEST_DOWNLOAD_LABEL }));
    expect(onDownload.mock.calls[0]).toEqual([false, "instance"]);
  });
});
