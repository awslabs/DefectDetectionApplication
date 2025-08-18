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
  Button,
  ColumnLayout,
  ExpandableSection,
  Header,
  SpaceBetween,
} from "@cloudscape-design/components";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ValueWithLabel } from "Common";
import {
  getInferenceResultSummary,
  resetInferenceResultActiveCounter,
} from "api/InferenceResultAPI";
import { useNavigate } from "react-router-dom";
import { convertTimestampToLocalTime } from "./helpers";
import { useContext } from "react";
import { AppLayoutContext } from "components/layout/AppLayoutContext";

interface ResultAnalyticsSummaryProps {
  workflowId: string;
}

export default function ResultAnalyticsSummary({
  workflowId,
}: ResultAnalyticsSummaryProps): JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const workflowResultUrl = `/history/${workflowId}`;

  const getResultSummary = useQuery({
    queryKey: ["getResultSummary", workflowId],
    queryFn: () => getInferenceResultSummary(workflowId),
  });
  const { data: resultsSummaryStats } = getResultSummary;
  const { totalInference, normal, anomaly } = resultsSummaryStats?.stats || {};

  const { addSuccess, addError } = useContext(AppLayoutContext);
  const resetMutation = useMutation({
    mutationFn: () => resetInferenceResultActiveCounter(workflowId),
    onSuccess: () => {
      addSuccess({
        content: "You successfully reset active counter.",
      });
      queryClient.clear();
    },
    onError: () => {
      addError({
        content: "Failed to reset active counter.",
      });
    },
  });

  return (
    <ExpandableSection
      defaultExpanded
      variant="container"
      headerText="Results analytics"
      headerActions={
        <SpaceBetween direction="horizontal" size="xs">
          <Button onClick={(): void => resetMutation.mutate()}>
            Reset active counter
          </Button>
          <Button
            onClick={(): void => {
              queryClient.invalidateQueries({
                queryKey: ["getInferenceResults"],
              });
              navigate(workflowResultUrl);
            }}
          >
            View all workflow results
          </Button>
        </SpaceBetween>
      }
    >
      <SpaceBetween size="s">
        <Header>Active counter</Header>
        <ColumnLayout columns={4} variant="text-grid">
          <ValueWithLabel label="Total inferences">
            {totalInference ?? "-"}
          </ValueWithLabel>
          <ValueWithLabel label="Anomalous results">
            {anomaly ?? "-"}
          </ValueWithLabel>
          <ValueWithLabel label="Normal results">
            {normal ?? "-"}
          </ValueWithLabel>
          <ValueWithLabel label="Last reset date">
            {getResultSummary.data
              ? convertTimestampToLocalTime(getResultSummary.data.lastResetTime)
              : "-"}
          </ValueWithLabel>
        </ColumnLayout>
      </SpaceBetween>
    </ExpandableSection>
  );
}
