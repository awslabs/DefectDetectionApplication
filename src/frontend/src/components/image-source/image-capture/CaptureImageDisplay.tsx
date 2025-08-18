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

import {
  SpaceBetween,
  Box,
  ColumnLayout,
  Popover,
  StatusIndicator,
  Button,
  Spinner,
  Form,
} from "@cloudscape-design/components";
import { yupResolver } from "@hookform/resolvers/yup";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ValueWithLabel } from "Common";
import { captureImage, previewImage } from "api/ImageAPI";
import FormInput from "components/form/FormInput";
import { PREVIEW_REFRESH_INTERVAL_MS } from "components/image-settings/constants";
import { useForm, FormProvider } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { SchemaType, schema } from "./schema";
import { useEffect, useState } from "react";
import InteractableImage from "components/live-result/InteractableImage";
import ImagePreviewError from "components/live-result/ImagePreviewError";
import ImagePlaceholder from "components/common/ImagePlaceholder";

interface CaptureImageDisplayProps {
  imgSrcId: string;
  capturePath: string;
  isLivePreviewChecked: boolean;
}

export default function CaptureImageDisplay({
  imgSrcId,
  capturePath,
  isLivePreviewChecked,
}: CaptureImageDisplayProps): JSX.Element {
  const navigate = useNavigate();
  const editImageSettingsUrl = `/image-sources/${imgSrcId}/edit-settings`;
  const queryClient = useQueryClient();
  const [isCapturing, setIsCapturing] = useState(false);
  const [imageLoadError, setImageLoadError] = useState<string | null>(null);


  const captureMutation = useMutation({
    mutationFn: (filePrefix?: string) => captureImage(imgSrcId, filePrefix),
    onSuccess: () => {
      setIsCapturing(false);
      queryClient.invalidateQueries({
        queryKey: ["getLastCaptureImages", capturePath],
      });
    },
    onError: () => {
      setIsCapturing(false);
    },
  });

  useEffect(() => {
    if (isCapturing) {
      form.handleSubmit((values) =>
        captureMutation.mutate(values.folderName),
      )();
    }
  }, [isCapturing]);

  const getQuery = useQuery({
    queryKey: ["previewImage", imgSrcId],
    queryFn: () => previewImage(imgSrcId),
    onSuccess: (data) => {
      if (data.image) {
        setImageLoadError(null);
      } else {
        setImageLoadError("");
      }
    },
    onError: (error: any) => {
      setImageLoadError(error?.response?.data?.message ?? "");
    },
    // Don't refetch if capture is in progress. Real camera has issue when
    // trying to capture and preview at the same time.
    refetchInterval: isCapturing ? false : PREVIEW_REFRESH_INTERVAL_MS,
    enabled: isLivePreviewChecked,
  });
  const image = getQuery.data?.image;

  const form = useForm<SchemaType>({
    resolver: yupResolver(schema),
    mode: "onChange",
    values: {
      folderName: "",
    },
  });

  if (getQuery.isLoading && imageLoadError === null) {
    return <Spinner size="big" />;
  }

  return (
    <SpaceBetween size="l">
      {imageLoadError !== null ? (
        <ImagePlaceholder content={<ImagePreviewError errorMsg={imageLoadError} />} />
      ) : <Box margin={{ top: "l" }}>
        <InteractableImage
          imageSrc={`data:image/jpg;base64,${image}`}
          extraActions={(
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={(): void => navigate(editImageSettingsUrl)}>
                Edit image settings
              </Button>
              {!isLivePreviewChecked && (
                <Button
                  disabled={isCapturing}
                  iconAlign="left"
                  iconName="refresh"
                  onClick={(): void => {
                    getQuery.refetch();
                  }}
                >
                  Refresh preview
                </Button>
              )}
              <Button
                onClick={(): void => {
                  setIsCapturing(true);
                }}
                loading={isCapturing}
              >
                Capture image
              </Button>
            </SpaceBetween>
          )}
        />
      </Box>}
      <ColumnLayout columns={2}>
        <Box float="left">
          <SpaceBetween size="l">
            <ValueWithLabel label="Capture path">
              <Popover
                size="small"
                position="top"
                triggerType="custom"
                dismissButton={false}
                content={
                  <StatusIndicator type="success">Copied</StatusIndicator>
                }
              >
                <Button
                  variant="inline-icon"
                  iconName="copy"
                  onClick={(): void => {
                    navigator.clipboard.writeText(capturePath);
                  }}
                />
              </Popover>
              {capturePath}
            </ValueWithLabel>
            <FormProvider {...form}>
              <form>
                <Form>
                  <FormInput
                    name="folderName"
                    label="File prefix"
                    description="A hyphen will be added after the prefix."
                    placeholder="normal"
                    constraintText="Valid characters are a-z, A-Z, 0-9, _ (underscore), spaces, and - (hyphen)."
                  />
                </Form>
              </form>
            </FormProvider>
          </SpaceBetween>
        </Box>
      </ColumnLayout>
    </SpaceBetween>
  );
}
