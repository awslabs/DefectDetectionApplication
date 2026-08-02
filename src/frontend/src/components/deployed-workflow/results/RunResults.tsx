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
  Box,
  Container,
  ContentLayout,
  Header,
  SpaceBetween,
  Spinner,
  StatusIndicator,
} from "@cloudscape-design/components";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";

import {
  getWorkflowExecutionOverlay,
  getWorkflowExecutionResults,
  workflowExecutionOutputImageUrl,
} from "api/WorkflowRegistrationAPI";
import useAuth from "components/auth/authHook";
import InteractableImage from "components/live-result/InteractableImage";
import RefreshDisplayActions from "components/live-result/RefreshDisplayActions";
import {
  getMaskBackgroundColor,
  getMaskImageProp,
} from "components/live-result/helpers";
import { resultLayoutStyle } from "components/live-result/styles";

/**
 * Run results screen (deployed-workflow-run-observability, Requirement 5).
 *
 * Mirrors `result-history/ResultDetailsCardDisplay.tsx`: it renders the run's
 * base output image via the shared `InteractableImage` component and reuses the
 * exact overlay show/hide toggle (`RefreshDisplayActions`) and mask helpers
 * (`getMaskImageProp` / `getMaskBackgroundColor`) the "run inference" results
 * screen uses (R5.4, R5.6). When the run produced a mask overlay the toggle is
 * shown; otherwise the plain base image is displayed with no toggle (R5.5).
 * A load failure / no-results run renders a clear Cloudscape state rather than
 * a broken image or crash (R5.7).
 */
export default function RunResults(): JSX.Element {
  const { executionId = "" } = useParams();
  const [showMask, setShowMask] = useState(true);
  const { token, authEnabled } = useAuth();

  const resultsQuery = useQuery({
    queryKey: ["getWorkflowExecutionResults", executionId],
    queryFn: () => getWorkflowExecutionResults(executionId),
  });

  const results = resultsQuery.data;
  // The output image carries the overlay availability; only fetch the overlay
  // (base64 mask + background) when the run actually produced one.
  const hasOverlay = !!results?.images?.some((image) => image.hasOverlay);

  const overlayQuery = useQuery({
    queryKey: ["getWorkflowExecutionOverlay", executionId],
    queryFn: () => getWorkflowExecutionOverlay(executionId),
    enabled: hasOverlay,
  });

  const header = <Header variant="h1">Run results</Header>;

  const renderState = (content: JSX.Element): JSX.Element => (
    <ContentLayout header={header}>
      <Container header={<Header variant="h2">Output images</Header>}>
        {content}
      </Container>
    </ContentLayout>
  );

  if (resultsQuery.isLoading) {
    return renderState(<Spinner size="big" />);
  }

  // Load failure or a run with no viewable image results: clear, non-crashing
  // state (R5.7 / R5.2).
  if (resultsQuery.isError || !results || !results.hasImageResults) {
    return renderState(
      <StatusIndicator type={resultsQuery.isError ? "error" : "info"}>
        {resultsQuery.isError
          ? "Results unavailable for this run."
          : "This run produced no viewable image results."}
      </StatusIndicator>,
    );
  }

  const imageSrc = workflowExecutionOutputImageUrl(
    executionId,
    authEnabled ? token : undefined,
  );

  // Reuse the exact overlay pipeline: base64 mask + background -> mask prop.
  const maskImage = overlayQuery.data?.maskImage ?? null;
  const backgroundColorProp = getMaskBackgroundColor(
    overlayQuery.data?.maskBackground ?? null,
  );
  const maskImageProp = getMaskImageProp(maskImage, backgroundColorProp);

  const extraActions = (
    <RefreshDisplayActions
      showAnomalyMaskToggle={!!maskImage}
      onClickAnomalyMaskToggle={(checked): void => setShowMask(checked)}
      anomalyMaskToggleChecked={!!showMask}
      // Feedback flag toggle is not applicable to deployed-workflow runs.
      showFlagForReviewToggle={false}
    />
  );

  return (
    <ContentLayout header={header}>
      <Container header={<Header variant="h2">Output images</Header>}>
        <SpaceBetween size="l">
          {overlayQuery.isError && (
            <Box variant="p" color="text-status-warning">
              The overlay could not be loaded; showing the base image.
            </Box>
          )}
          <div className={resultLayoutStyle}>
            <InteractableImage
              imageSrc={imageSrc}
              {...maskImageProp}
              showMask={!!showMask}
              extraActions={extraActions}
            />
          </div>
        </SpaceBetween>
      </Container>
    </ContentLayout>
  );
}
