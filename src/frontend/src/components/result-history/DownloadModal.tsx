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

interface DownloadModalProps {
  showModal: boolean;
  onClose: () => void;
  onDownload: (markAsDownload: boolean) => void;
  isDownloading: boolean;
}

export default function DownloadModal({
  showModal,
  onClose,
  onDownload,
  isDownloading,
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