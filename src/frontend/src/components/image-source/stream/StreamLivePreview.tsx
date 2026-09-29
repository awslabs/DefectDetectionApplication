/*
 *  Copyright 2025 Amazon Web Services, Inc.
 *
 *  Licensed under the Apache License, Version 2.0 (the "License");
 *  you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the License for the specific language governing permissions and
 *  limitations under the License.
 */
/**
 * The live preview and single-frame capture of a stream camera
 * (rtsp-rtmp-stream-cameras Requirements 4.4, 16.1), through the existing
 * preview and capture actions (`POST /image-sources/{id}/preview` and
 * `/capture`). Both take the Latest_Frame of the camera's shared session,
 * so the preview keeps the camera connected while it is on.
 *
 * While the camera is not streaming, the preview shows the session state:
 * in place of the image before a first frame arrives, and above the last
 * frame received after that, so a frozen frame never passes for a live one.
 * Capture waits for the camera to stream, since it would otherwise save that
 * last frame again.
 *
 * Classic Pipeline_Configuration workflows reject stream cameras
 * (Requirement 4.8), so this is the one place the LocalServer UI previews
 * and captures them; the classic live results and capture pages never see a
 * stream Image_Source.
 */
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  Header,
  SpaceBetween,
  Spinner,
  Toggle,
} from "@cloudscape-design/components";
import { yupResolver } from "@hookform/resolvers/yup";
import { useMutation, useQuery } from "@tanstack/react-query";
import { captureImage, previewImage } from "api/ImageAPI";
import { ValueWithLabel } from "Common";
import ImagePlaceholder from "components/common/ImagePlaceholder";
import FormInput from "components/form/FormInput";
import { PREVIEW_REFRESH_INTERVAL_MS } from "components/image-settings/constants";
import { SchemaType, schema } from "components/image-source/image-capture/schema";
import { ImageSource, StreamHealth } from "components/image-source/types";
import InteractableImage from "components/live-result/InteractableImage";
import { FormProvider, useForm } from "react-hook-form";
import { apiErrorMessage } from "./streamForm";
import {
  StreamStateIndicator,
  isNotStreaming,
  notStreamingDetail,
  streamStateLabel,
} from "./streamHealth";

interface StreamLivePreviewProps {
  imageSource: ImageSource;
  /** The camera's Stream_Health, which the caller keeps refreshed. */
  health: StreamHealth | null | undefined;
  live: boolean;
  onLiveChange: (live: boolean) => void;
}

/** The session state in place of a preview image (Requirement 16.1). */
export function StreamStateOverlay({
  health,
  previewError,
}: {
  health: StreamHealth | null | undefined;
  previewError?: string | null;
}): JSX.Element {
  const detail = notStreamingDetail(health) || previewError || "";
  return (
    <SpaceBetween size="xs" alignItems="center">
      <StreamStateIndicator state={health?.state} />
      {detail && (
        <Box color="text-body-secondary" textAlign="center">
          {detail}
        </Box>
      )}
    </SpaceBetween>
  );
}

/** Why the image on screen is not a live one, when it is not. */
function StaleFrameNotice({
  health,
  previewError,
}: {
  health: StreamHealth | null | undefined;
  previewError: string | null;
}): JSX.Element | null {
  if (isNotStreaming(health)) {
    const detail = notStreamingDetail(health);
    return (
      <Alert
        type={health?.state === "failed" ? "error" : "warning"}
        header={`The camera is not streaming: ${streamStateLabel(health?.state)}`}
      >
        The preview shows the last frame received.
        {detail && ` ${detail}`}
      </Alert>
    );
  }
  if (previewError !== null) {
    return (
      <Alert type="error" header="The preview could not be refreshed">
        {previewError}
      </Alert>
    );
  }
  return null;
}

export default function StreamLivePreview({
  imageSource,
  health,
  live,
  onLiveChange,
}: StreamLivePreviewProps): JSX.Element {
  const { imageSourceId, imageCapturePath } = imageSource;
  const form = useForm<SchemaType>({
    resolver: yupResolver(schema),
    mode: "onChange",
    defaultValues: { folderName: "" },
  });
  const capture = useMutation({
    mutationFn: (filePrefix?: string) => captureImage(imageSourceId, filePrefix),
  });
  const preview = useQuery({
    queryKey: ["previewImage", imageSourceId],
    queryFn: () => previewImage(imageSourceId),
    // Off, the preview neither loads nor refreshes on its own. Preview and
    // capture both read the shared session, so a capture needs no pause.
    enabled: live,
    // A failure shows at once; the next refresh is the retry.
    retry: false,
    refetchInterval: PREVIEW_REFRESH_INTERVAL_MS,
  });
  // The last frame received stays on screen after a failed refresh.
  const frame = preview.data?.image || null;
  const previewError = preview.isError ? apiErrorMessage(preview.error) : null;
  const notStreaming = isNotStreaming(health);

  const refreshButton = !live && (
    <Button
      iconName="refresh"
      loading={preview.isFetching}
      onClick={(): void => {
        preview.refetch();
      }}
    >
      Refresh preview
    </Button>
  );

  let content: JSX.Element;
  if (frame) {
    content = (
      <SpaceBetween size="s">
        <StaleFrameNotice health={health} previewError={previewError} />
        <InteractableImage
          imageSrc={`data:image/jpg;base64, ${frame}`}
          alt="The latest frame from the camera"
          extraActions={
            <SpaceBetween direction="horizontal" size="xs">
              {refreshButton}
              <Button
                variant="primary"
                loading={capture.isLoading}
                disabled={notStreaming}
                onClick={(): void => {
                  form.handleSubmit(({ folderName }) =>
                    capture.mutate(folderName || undefined),
                  )();
                }}
              >
                Capture image
              </Button>
            </SpaceBetween>
          }
        />
      </SpaceBetween>
    );
  } else if (notStreaming || previewError !== null) {
    content = (
      <ImagePlaceholder
        placement="center"
        content={
          <SpaceBetween size="m" alignItems="center">
            <StreamStateOverlay health={health} previewError={previewError} />
            {refreshButton}
          </SpaceBetween>
        }
      />
    );
  } else if (preview.isFetching) {
    content = (
      <ImagePlaceholder
        placement="center"
        content={
          <SpaceBetween size="m" alignItems="center">
            <Spinner size="big" />
            <Box>Loading preview</Box>
          </SpaceBetween>
        }
      />
    );
  } else {
    content = (
      <ImagePlaceholder
        placement="center"
        content={
          <SpaceBetween size="m" alignItems="center">
            <Box color="text-status-inactive">
              Live preview is off. Turn it on, or refresh the preview, to see the camera.
            </Box>
            {refreshButton}
          </SpaceBetween>
        }
      />
    );
  }

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Frames from the camera's shared session. While live preview is on, it keeps the camera connected."
          actions={
            <Toggle
              checked={live}
              onChange={({ detail }): void => onLiveChange(detail.checked)}
            >
              Live preview
            </Toggle>
          }
        >
          Preview and capture
        </Header>
      }
    >
      <SpaceBetween size="l">
        {content}
        <ColumnLayout columns={2}>
          <ValueWithLabel label="Capture path">{imageCapturePath || "-"}</ValueWithLabel>
          <FormProvider {...form}>
            <form
              onSubmit={(event): void => {
                event.preventDefault();
              }}
            >
              <FormInput
                name="folderName"
                label="File prefix"
                description="A hyphen will be added after the prefix."
                placeholder="normal"
                constraintText="Valid characters are a-z, A-Z, 0-9, _ (underscore), spaces, and - (hyphen)."
              />
            </form>
          </FormProvider>
        </ColumnLayout>
        {notStreaming && frame && (
          <Box color="text-body-secondary">
            Capture is available while the camera is streaming.
          </Box>
        )}
        {capture.isSuccess && (
          <Alert type="success" dismissible onDismiss={(): void => capture.reset()}>
            Captured an image to {imageCapturePath || "the capture path"}.
          </Alert>
        )}
        {capture.isError && (
          <Alert
            type="error"
            header="The image could not be captured"
            dismissible
            onDismiss={(): void => capture.reset()}
          >
            {apiErrorMessage(capture.error)}
          </Alert>
        )}
      </SpaceBetween>
    </Container>
  );
}
