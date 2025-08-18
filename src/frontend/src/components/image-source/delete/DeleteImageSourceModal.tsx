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
} from "@cloudscape-design/components";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { deleteImageSource } from "api/ImageSourceAPI";
import { useContext } from "react";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import { useNavigate } from "react-router-dom";

interface DeleteImageSourceModalProps {
  isVisible: boolean;
  imgSrcId: string;
  imgSrcName: string;
  isFolderSrc: boolean;
  onCancel: () => void;
}
export default function DeleteImageSourceModal({
  isVisible,
  imgSrcId,
  imgSrcName,
  isFolderSrc,
  onCancel,
}: DeleteImageSourceModalProps): JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { addSuccess, addError } = useContext(AppLayoutContext);
  const deleteMutation = useMutation({
    mutationFn: () => deleteImageSource(imgSrcId),
    onSuccess: () => {
      const path = `/image-sources`;
      addSuccess({
        content: (
          <>
            You successfully deleted <strong>{imgSrcName}</strong>.
          </>
        ),
        relevantPath: path,
      });
      navigate(path);
      queryClient.clear();
    },
    onError: () => {
      addError({
        content: (
          <>
            Failed to delete <strong>{imgSrcName}</strong>.
          </>
        ),
        action: (
          <Button onClick={(): void => deleteMutation.mutate()}>Retry</Button>
        ),
      });
    },
  });

  const [removeSrcName, setRemoveSrcName] = React.useState("");
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
              onClick={(): void => deleteMutation.mutate()}
              loading={deleteMutation.isLoading}
              disabled={removeSrcName !== imgSrcName}
            >
              Delete
            </Button>
          </SpaceBetween>
        </Box>
      }
      header={<>Delete {imgSrcName}</>}
    >
      <FormField>
        Remove <strong>{imgSrcName}</strong> from this station? This action
        cannot be undone.
        <Box padding={{ top: "m", bottom: "xxxs" }}>
          To confirm removal, enter the image source name.
        </Box>
        <Input
          onChange={({ detail }): void => setRemoveSrcName(detail.value)}
          value={removeSrcName}
          placeholder={imgSrcName}
        />
        {isFolderSrc && (
          <Box padding={{ top: "m" }}>
            <Alert>The folder and its contents will not be deleted.</Alert>
          </Box>
        )}
      </FormField>
    </Modal>
  );
}
