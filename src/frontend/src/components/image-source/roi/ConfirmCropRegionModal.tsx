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

import Modal from "@cloudscape-design/components/modal";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ImageSource,
  ImageSourceConfiguration,
  RegionOfInterest,
} from "../types";
import { editImageSource } from "../../../api/ImageSourceAPI";
import * as React from "react";
import { useContext, useEffect } from "react";
import { AppLayoutContext } from "../../layout/AppLayoutContext";
import { useNavigate } from "react-router-dom";
import { Box, Button, SpaceBetween } from "@cloudscape-design/components";

interface ConfirmCropRegionModalProps {
  isVisible: boolean;
  imgSrcId: string;
  initialImageSource: ImageSource | undefined;
  updatedCropRoI: RegionOfInterest;
  saveAction: boolean;
  onCancel: () => void;
}

export default function ConfirmCropRegionModal({
  isVisible,
  imgSrcId,
  initialImageSource,
  updatedCropRoI,
  onCancel,
}: ConfirmCropRegionModalProps): JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { addSuccess, addError } = useContext(AppLayoutContext);

  const editMutation = useMutation({
    mutationFn: () => {
      if (!initialImageSource?.imageSourceConfiguration) {
        throw new Error("Image source configuration not found");
      }

      const imageSourceConfiguration: ImageSourceConfiguration = {
        ...initialImageSource.imageSourceConfiguration,
        imageCrop: {
          top: Math.floor(updatedCropRoI.top),
          bottom: Math.floor(updatedCropRoI.bottom),
          left: Math.floor(updatedCropRoI.left),
          right: Math.floor(updatedCropRoI.right),
        },
      };

      return editImageSource(imgSrcId, {
        // Use spread operator so that we only include if value has changed
        imageSourceConfiguration: imageSourceConfiguration,
      });
    },
    onSuccess: (data, values) => {
      const path = `/image-sources/${imgSrcId}`;
      addSuccess({
        content: (
          <>
            You successfully edited <strong>{initialImageSource?.name}</strong>.
          </>
        ),
        relevantPath: path,
      });
      navigate(path);
      // Clears cache so queries are reloaded
      queryClient.clear();
    },
    onError: (error: Error, values) => {
      addError({
        content: (
          <>
            Failed to edit <strong>{imgSrcId}</strong>. {error.message}
          </>
        ),
      });
    },
  });

  return (
    <Modal
      visible={isVisible}
      header={"Confirm new region"}
      onDismiss={onCancel}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={(): void => onCancel()}>Cancel</Button>
            <Button
              variant="primary"
              onClick={(): void => editMutation.mutate()}
              loading={editMutation.isLoading}
            >
              Save
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <div>
        Editing the region of interest will require that any existing datasets
        will need to be rebuilt and models will need to be retrained.
      </div>
    </Modal>
  );
}
