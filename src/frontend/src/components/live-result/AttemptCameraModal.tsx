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

import * as React from "react";
import {
  Box,
  Button,
  SpaceBetween,
} from "@cloudscape-design/components";

import FormField from "@cloudscape-design/components/form-field";
import Modal from "@cloudscape-design/components/modal";


interface AttemptCameraModalProps {
  isVisible: boolean;
  cameraId: string;
  onCancel: () => void;
  path?: string;
  onReconnect?: (cameraId: string) => void;
  isLoading?: boolean;
}

export default function AttemptCameraModal({
  isVisible,
  cameraId,
  onCancel,
  onReconnect,
  isLoading
}: AttemptCameraModalProps): JSX.Element {

  return (
    <Modal
      onDismiss={(): void => onCancel()}
      visible={isVisible}
      closeAriaLabel="Close modal"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={(): void => {
              onCancel();
            }}>Cancel</Button>
            <Button
              variant="primary"
              onClick={(): void => {
                onReconnect?.(cameraId);
              }}
              loading={isLoading}
            >
              Connect
            </Button>
          </SpaceBetween>
        </Box>
      }
      header={<>{cameraId} disconnected</>}
    >
      <FormField>
        To run this workflow you need to establish a connection between the camera and this station.
      </FormField>
    </Modal>
  );
}
