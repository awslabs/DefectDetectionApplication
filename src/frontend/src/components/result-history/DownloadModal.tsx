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

import { Box, Button, Checkbox, Header, Modal, SpaceBetween, TextContent } from "@cloudscape-design/components";
import { useEffect, useState } from "react";
import type { ExportManifestFormat } from "api/WorkflowAPI";

/** The second download, offered when a selected result stores instance masks. */
export const INSTANCE_MANIFEST_DOWNLOAD_LABEL = "Download with instance-segmentation manifest";

interface DownloadModalProps {
  showModal: boolean;
  onClose: () => void;
  /**
   * `manifestFormat` is given only by the instance-manifest download
   * (rfdetr-seg-support Requirement 11.4); the existing download passes none.
   */
  onDownload: (markAsDownload: boolean, manifestFormat?: ExportManifestFormat) => void;
  isDownloading: boolean;
  /** Whether a selected result carries an instance mask (`mask_rle`). */
  instanceManifestAvailable?: boolean;
}

export default function DownloadModal({
  showModal,
  onClose,
  onDownload,
  isDownloading,
  instanceManifestAvailable = false,
}: DownloadModalProps): JSX.Element {
  const [markAsDownloaded, setMarkAsDownloaded] = useState(true);

  useEffect(() => {
    /**
     * reset state on component unmount
     */
    return (() => {
      setMarkAsDownloaded(true);
    })
  }, [])

  return (
    <Modal
      visible={showModal}
      onDismiss={onClose}
      header={
        <Header variant="h1">
          Download results
        </Header>
      }
      footer={(
        <Box float="right">
          <SpaceBetween size="xs" direction="horizontal">
            <Button variant="link" onClick={onClose}>
              Cancel
            </Button>
            {instanceManifestAvailable && (
              <Button
                variant="normal"
                loading={isDownloading}
                data-testid="download-instance-manifest"
                onClick={(): void => {
                  onDownload(markAsDownloaded, "instance");
                }}
              >
                {INSTANCE_MANIFEST_DOWNLOAD_LABEL}
              </Button>
            )}
            <Button
              variant="primary"
              loading={isDownloading}
              onClick={(): void => {
                onDownload(markAsDownloaded);
              }}
            >
              Download
            </Button>
          </SpaceBetween>
        </Box>
      )}
    >
      <SpaceBetween direction="vertical" size="xs">
        <TextContent>
          <small>
            All selected images, masks and their meta-data will be downloaded. This zip file can be uploaded back into the cloud application dataset by selecting the classification method "Station workflow metadata".
          </small>
        </TextContent>
        <Checkbox
          checked={markAsDownloaded}
          onChange={({ detail }): void => setMarkAsDownloaded(detail.checked)}
          description={"Track what has been previously downloaded and avoid duplicates in your dataset."}
        >
          Mark images as downloaded
        </Checkbox>
      </SpaceBetween>
    </Modal>
  );
}