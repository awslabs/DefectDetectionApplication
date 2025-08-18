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

import { Container, ContentLayout, Header, SpaceBetween, Spinner, TextContent } from "@cloudscape-design/components";
import { WorkflowSelector } from "components/live-result/WorkflowSelector";
import ImageCaptureWorkflowContent from "./ImageCaptureWorkflowContent";
import { useQuery } from "@tanstack/react-query";
import { getWorkflow } from "api/WorkflowAPI";
import { useNavigate, useParams } from "react-router-dom";
import CapturedCards from "./CapturedCards";

export default function ImageCapturePage(): JSX.Element {
  const workflowId = useParams().workflowId || "";
  const navigate = useNavigate();

  const { data: workflow, isLoading: isLoadingWorkflow } = useQuery({
    queryKey: ["getWorkflow", workflowId],
    queryFn: () => getWorkflow(workflowId),
    cacheTime: 0,
    enabled: !!workflowId,
  });

  return (
    <ContentLayout
      header={<Header variant="h1">Capture images</Header>}
    >
      <SpaceBetween direction="vertical" size="l">
        <Container header={<Header variant="h2">Capture preview</Header>}>
          <TextContent>
            <h4>Workflow</h4>
          </TextContent>
          <SpaceBetween size={"l"}>
            <WorkflowSelector workflowId={workflowId} setWorkflowId={(wid): void => navigate(`/capture/${wid}`)} />
            {
              !!workflowId && isLoadingWorkflow && <Spinner size="normal" />
            }
            {
              !!workflowId && !isLoadingWorkflow && !!workflow && <ImageCaptureWorkflowContent workflow={workflow} />
            }
          </SpaceBetween>
        </Container>
        {
          !!workflowId && (
            <CapturedCards
              captureResultsHref={`/capture-results/${workflowId}`}
              workflowId={workflowId}
            />
          )
        }
      </SpaceBetween>
    </ContentLayout>
  );
}