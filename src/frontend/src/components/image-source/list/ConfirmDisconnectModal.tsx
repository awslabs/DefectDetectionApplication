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
  Alert,
  Box,
  Button,
  SpaceBetween,
  Header,
  Table,
} from "@cloudscape-design/components";

import FormField from "@cloudscape-design/components/form-field";
import Modal from "@cloudscape-design/components/modal";

import { useCollection } from "@cloudscape-design/collection-hooks";
import { WorkflowTriggerType } from "components/image-source/types";
import useCameraConnection from "components/hook/useCameraConnection";


interface ConfirmDisconnectModalProps {
  isVisible: boolean;
  cameraId: string;
  onCancel: () => void;
  filteredWorkflows?: FilteredWorkflowTableItem[];
  showError?: boolean;
  onCameraDisconnect?: () => void;
}

export default function ConfirmDisconnectModal({
  isVisible,
  cameraId,
  onCancel,
  filteredWorkflows,
  showError,
  onCameraDisconnect,
}: ConfirmDisconnectModalProps): JSX.Element {
  const {
    disconnect,
    isDisconnecting,
  } = useCameraConnection({
    cameraId,
    recheckStatusFn: () => onCameraDisconnect?.()
  });

  return (
    <Modal
      onDismiss={(): void => onCancel()}
      visible={isVisible}
      closeAriaLabel="Close modal"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={(): void => onCancel()}>Cancel</Button>
            <Button
              variant="primary"
              onClick={disconnect}
              loading={isDisconnecting}
            >
              Confirm
            </Button>
          </SpaceBetween>
        </Box>
      }
      header={<>Confirm disconnect</>}
    >
      <FormField>
        Disconnecting <strong>{cameraId}</strong> will impact the following image sources and workflows.
        <FilteredWorkflows filteredWorkflows={filteredWorkflows} />
        {
          showError ? (
            <Alert type="error">
              Failed to fetch the impacted image sources and workflows
            </Alert>
          ) : (
            <Alert statusIconAriaLabel="Info" type="warning">
              Workflows that are configured with a digital input trigger will automatically attempt to reconnect to this camera when a signal is received.
            </Alert>
          )
        }
      </FormField>
    </Modal>
  );
}



export type FilteredWorkflowTableItem = {
  imageSourceName: string;
  workflowName: string;
  trigger: WorkflowTriggerType;
};


export function FilteredWorkflows({ filteredWorkflows }: { filteredWorkflows?: FilteredWorkflowTableItem[] }): JSX.Element {

  const workflows = filteredWorkflows ?? [];
  const { items, collectionProps } =
    useCollection(workflows, {});

  return (
    <Table

      loadingText="Loading deployed models"
      header={<Header></Header>}
      columnDefinitions={[
        {
          id: "imageSource",
          header: "Image source",
          cell: (item): string =>
            item?.imageSourceName,
        },
        {
          id: "workflow",
          header: "Workflow",
          cell: (item): string =>
            item?.workflowName,
        },
        {
          id: "trigger",
          header: "Trigger",
          cell: (item): string => item?.trigger,
        },
      ]}
      sortingDisabled
      items={items}
      variant="borderless"
      {...collectionProps}
    />
  );
}

