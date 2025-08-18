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
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import { WorkflowSelector } from "../live-result/WorkflowSelector";
import {
  ContentLayout,
  SpaceBetween,
  TextContent,
} from "@cloudscape-design/components";
import ResultHistoryCardDisplay from "./ResultHistoryCardDisplay";
import { useNavigate, useParams } from "react-router-dom";
import { HistoryResultPageType } from "./types";

export default function ResultHistory(): JSX.Element {
  const selectWorkflowId = useParams().workflowId ?? "";
  const [workflowId, setWorkflowId] = React.useState(selectWorkflowId);
  const navigate = useNavigate();

  React.useEffect(() => {
    setWorkflowId(selectWorkflowId);
  }, [selectWorkflowId]);

  return (
    <ContentLayout header={<Header variant="h1">Inference results</Header>}>
      <SpaceBetween size={"l"}>
        <Container header={<Header variant="h2">Select workflow</Header>}>
          <TextContent>
            <h4>Workflow</h4>
          </TextContent>

          <WorkflowSelector
            setWorkflowId={setWorkflowId}
            workflowId={workflowId}
            onWorkflowChange={(wid): void => navigate(`/history/${wid}`)}
          />
        </Container>
        {
          // !! check appears to be necessary, without it the page shows React key error
          !!workflowId && <ResultHistoryCardDisplay workflowId={workflowId} historyResultPageType={HistoryResultPageType.INFERENCE_RESULT} />
        }
      </SpaceBetween>
    </ContentLayout>
  );
}
