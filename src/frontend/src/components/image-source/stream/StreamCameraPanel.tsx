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
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  Header,
  SpaceBetween,
  StatusIndicator,
} from "@cloudscape-design/components";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  StreamConnectionTestResult,
  getStreamHealth,
  testStreamConnection,
} from "api/ImageSourceAPI";
import { ValueWithLabel } from "Common";
import { ImageSource } from "components/image-source/types";
import { apiErrorMessage } from "./streamForm";
import {
  STREAM_HEALTH_FAST_REFRESH_MS,
  STREAM_HEALTH_REFRESH_MS,
  StreamHealthSummary,
  categoryLabel,
  frameRate,
  resolution,
} from "./streamHealth";
import StreamLivePreview from "./StreamLivePreview";

/** The result card of a connection test (Requirement 4.3). */
export function StreamConnectionResult({
  result,
}: {
  result: StreamConnectionTestResult;
}): JSX.Element {
  if (!result.ok) {
    return (
      <Alert type="error" header={`Connection failed: ${categoryLabel(result.category)}`}>
        {result.message}
      </Alert>
    );
  }
  const health = result.streamHealth;
  return (
    <Alert type="success" header="Connected: the camera is streaming">
      <SpaceBetween size="m">
        <ColumnLayout columns={4} variant="text-grid">
          <ValueWithLabel label="Codec">{health?.codec?.toUpperCase() ?? "-"}</ValueWithLabel>
          <ValueWithLabel label="Resolution">{resolution(health)}</ValueWithLabel>
          <ValueWithLabel label="Frame rate">{frameRate(health)}</ValueWithLabel>
          <ValueWithLabel label="Decoder">{health?.decoder ?? "-"}</ValueWithLabel>
        </ColumnLayout>
        {result.image ? (
          <img
            src={`data:image/jpg;base64, ${result.image}`}
            alt="The first frame received from the camera"
            style={{ maxWidth: "100%", maxHeight: 360 }}
          />
        ) : (
          result.imageError && <Box color="text-status-warning">{result.imageError}</Box>
        )}
      </SpaceBetween>
    </Alert>
  );
}

/**
 * The stream camera sections of an Image_Source's details: its Stream_URL,
 * credentials flag, live Stream_Health (Requirement 16.1) and the Test
 * connection action (Requirement 4.3), then its preview and capture
 * (Requirement 4.4).
 */
export default function StreamCameraPanel({
  imageSource,
}: {
  imageSource: ImageSource;
}): JSX.Element {
  const { imageSourceId } = imageSource;
  const [livePreview, setLivePreview] = useState(true);
  const healthQuery = useQuery({
    queryKey: ["getStreamHealth", imageSourceId],
    queryFn: () => getStreamHealth(imageSourceId),
    // Faster while a live preview waits for the camera, so the preview's
    // state follows the session closely.
    refetchInterval: (data) =>
      livePreview && data?.state !== "streaming"
        ? STREAM_HEALTH_FAST_REFRESH_MS
        : STREAM_HEALTH_REFRESH_MS,
    initialData: imageSource.streamHealth ?? undefined,
  });
  const testMutation = useMutation({
    mutationFn: () => testStreamConnection(imageSourceId),
    onSettled: () => {
      healthQuery.refetch();
    },
  });
  const credentialsConfigured =
    healthQuery.data?.credentialsConfigured ?? imageSource.credentialsConfigured;
  return (
    <SpaceBetween size="xl">
      <Container
        header={
          <Header
            variant="h2"
            description="The camera connects when a workflow, preview or capture uses it."
            actions={
              <Button loading={testMutation.isLoading} onClick={(): void => testMutation.mutate()}>
                Test connection
              </Button>
            }
          >
            Stream camera
          </Header>
        }
      >
        <SpaceBetween size="l">
          <ColumnLayout columns={2} variant="text-grid">
            <ValueWithLabel label="Stream URL">{imageSource.location ?? "-"}</ValueWithLabel>
            <ValueWithLabel label="Credentials">
              {credentialsConfigured ? "Stored on this device" : "None"}
            </ValueWithLabel>
          </ColumnLayout>
          <StreamHealthSummary health={healthQuery.data} />
          {testMutation.isLoading && (
            <StatusIndicator type="loading">
              Connecting to the camera. This can take up to 20 seconds.
            </StatusIndicator>
          )}
          {testMutation.data && <StreamConnectionResult result={testMutation.data} />}
          {testMutation.isError && (
            <Alert type="error" header="The connection test could not run">
              {apiErrorMessage(testMutation.error)}
            </Alert>
          )}
        </SpaceBetween>
      </Container>
      <StreamLivePreview
        imageSource={imageSource}
        health={healthQuery.data}
        live={livePreview}
        onLiveChange={setLivePreview}
      />
    </SpaceBetween>
  );
}
