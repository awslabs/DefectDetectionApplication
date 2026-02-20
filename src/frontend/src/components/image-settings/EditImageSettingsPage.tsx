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
import { useWatch } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import {
  Button,
  Container,
  Form,
  Grid,
  Header,
  SpaceBetween,
  Spinner,
} from "@cloudscape-design/components";
import { SchemaType } from "./edit/schema";
import EditImageSettingsPane from "./EditImageSettingsPane";
import { useEffect, useMemo, useState } from "react";
import {
  GetPreviewImageResponse,
  getImagePreview,
} from "../../api/ImagePreviewAPI";
import { PREVIEW_REFRESH_INTERVAL_MS } from "./constants";
import { useQuery } from "@tanstack/react-query";
import { css } from "@emotion/css";
import InteractableImage from "components/live-result/InteractableImage";
import ImagePreviewError from "components/live-result/ImagePreviewError";
import { CameraStatus, RegionOfInterest } from "../image-source/types";
import ImagePlaceholder from "components/common/ImagePlaceholder";
import { CameraDisconnectedContent } from "components/common/ImagePlaceholder/PresetPlaceholderContents";
import useCameraConnection from "components/hook/useCameraConnection";

type EditImageSettingsPageProps = {
  id: string;
  initialPipelineString: string;
  cropSettings: RegionOfInterest;
  isLoading: boolean;
  isArvisCamera: boolean;
  cameraStatus?: CameraStatus;
  cameraId: string;
  recheckCameraStatusFn: () => void;
};

export default function EditImageSettingsPage(
  props: EditImageSettingsPageProps,
): JSX.Element {
  const navigate = useNavigate();
  const values = useWatch<SchemaType>();

  const initialPipelineValue = props.initialPipelineString;
  const [imageToRenderBase64, setImageToRender] = useState<string | null>();
  const [gstreamerPipelineToDownload, setGstreamerPipelineToDownload] =
    useState(props.initialPipelineString);
  const [imageLoadError, setImageLoadError] = useState<string | undefined>(
    undefined,
  );

  const {
    connect,
    isConnecting,
  } = useCameraConnection({ cameraId: props.cameraId, recheckStatusFn: props.recheckCameraStatusFn })

  useEffect(
    () => setGstreamerPipelineToDownload(props.initialPipelineString),
    [props.initialPipelineString],
  );

  const enablePreviewQuery = props.cameraStatus === CameraStatus.Connected || !props.isArvisCamera;

  const { data: imagePreview, isLoading: isLoadingPreview } = useQuery({
    queryKey: ["editImageSettingsPreview", props.id],
    queryFn: async () => {
      if (
        values.editGain &&
        values.editExposure &&
        values.editGstreamerPipeline
      ) {
        return await getImagePreview(props.id, {
          imageSourceConfiguration: {
            gain: values.editGain,
            exposure: values.editExposure,
            processingPipeline: gstreamerPipelineToDownload,
            imageCrop: props.cropSettings,
          },
        });
      }

      const defaultResponse: GetPreviewImageResponse = { image: "" };
      return defaultResponse;
    },
    onSuccess: (data) => {
      if (data.image) {
        setImageToRender(data.image);
        setImageLoadError(undefined);
      }
    },
    onError: (error: any) => {
      setImageLoadError(error?.response?.data?.message ?? "");
      setImageToRender(null);
    },
    refetchInterval: PREVIEW_REFRESH_INTERVAL_MS,
    enabled: enablePreviewQuery,
  });

  const editImageContent = useMemo(() => {
    if (props.isArvisCamera && props.cameraStatus !== CameraStatus.Connected) {
      return (
        <ImagePlaceholder
          placement="center"
          content={
            <CameraDisconnectedContent
              message="Camera disconnected. Connect to the camera to save image settings."
              loading={isConnecting}
              onConnect={connect}
            />
          }
        />
      )
    }
    if (imageLoadError !== undefined) {
      return <ImagePlaceholder content={<ImagePreviewError errorMsg={imageLoadError} />} />
    }
    if (!imageToRenderBase64) return <ImagePlaceholder placement="center" content={<Spinner size="big" />} />
    return (
      <InteractableImage
        imageSrc={`data:image/jpg;base64, ${imageToRenderBase64}`}
        imageStyleOverride={css`
        width: 100%;
        height: 100%;
      `}
        alt="Edit settings"
      />
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imageLoadError, imagePreview?.image, isConnecting, props.cameraStatus, imageToRenderBase64]);

  return (
    <>
      {props.isLoading || (isLoadingPreview && enablePreviewQuery) ? (
        <Spinner size="big" />
      ) : (
        <Form
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                formAction="none"
                variant="link"
                onClick={(): void => navigate(-1)}
              >
                Cancel
              </Button>
              <Button variant="primary" formAction="submit" disabled={props.cameraStatus !== CameraStatus.Connected}>
                Save
              </Button>
            </SpaceBetween>
          }
        >
          <Grid gridDefinition={[{ colspan: 3 }, { colspan: 9 }]}>
            <EditImageSettingsPane
              initialPipelineString={initialPipelineValue}
              setGstreamerPipelineToDownload={(newPipeline: string): void =>
                setGstreamerPipelineToDownload(newPipeline)
              }
            />
            <Container header={<Header variant={"h1"}>Image preview</Header>}>
              {editImageContent}
            </Container>
          </Grid>
        </Form>
      )}
    </>
  );
}