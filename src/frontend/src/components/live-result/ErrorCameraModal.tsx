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
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";


interface ErrorCameraModalProps {
  isVisible: boolean;
  cameraId: string;
  onCancel: () => void;
  onRetry?: () => void;
  isLiveResult?: boolean
}

export default function ErrorCameraModal({
  isVisible,
  cameraId,
  onCancel,
  onRetry,
  isLiveResult = false,
}: ErrorCameraModalProps): JSX.Element {

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
                onCancel();
                onRetry?.();
              }}
            >
              Try again
            </Button>
          </SpaceBetween>
        </Box>
      }
      header={<>Connection failed</>}
    >
      {ErrorFormField(isLiveResult, cameraId)}
    </Modal>
  );
}

function ErrorFormField(
  isLiveResult: boolean,
  cameraId: string
): JSX.Element {
  if (isLiveResult) {
    return (
      <FormField>
        This station was unable to connect to {cameraId}. Verify that your camera is powered on and not already connected to another station or device.
      </FormField>
    )
  }
  else {
    return (
      <FormField>
        This station was unable to connect to <strong>{cameraId}</strong>.
      </FormField>
    )
  }
}
