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
import { WorkflowSelector } from "./WorkflowSelector";
import {
  Box,
  ContentLayout,
  SpaceBetween,
  TextContent,
} from "@cloudscape-design/components";
import ProcessingStreamDisplay from "./ProcessingStreamDisplay";
import OverflowScrollBox from "components/common/OverflowScrollBox";
import { styleConstants } from "components/layout/constants";
import ResultAnalyticsSummary from "./ResultAnalyticsSummary";

export const LiveResultContext = React.createContext<{
  image: Element | null;
  onImageUpdate: ((image: any) => void) | undefined;
}>({
  image: null,
  onImageUpdate: undefined,
});

export default function LiveResults(): JSX.Element {
  const [workflowId, setWorkflowId] = React.useState("");
  const [image, setImage] = React.useState<Element | null>(null);

  return (
    <LiveResultContext.Provider
      value={{
        image,
        onImageUpdate: (newImg: Element | null): void => {
          setImage(newImg);
        },
      }}
    >
      <ContentLayout
        header={<Header variant="h1">Run inference</Header>}
      >
        <Container header={<Header variant="h2">Review results</Header>}>
          <TextContent>
            <h4>Workflow</h4>
          </TextContent>
          <SpaceBetween size={"l"}>
            <WorkflowSelector setWorkflowId={setWorkflowId} />
            {
              // !! check appears to be necessary, without it the page shows React key error
              !!workflowId && (
                <OverflowScrollBox
                  contentMinWidth={
                    styleConstants.IMAGE_CONTENT_CONTAINER_MIN_WIDTH
                  }
                >
                  <ProcessingStreamDisplay workflowId={workflowId} />
                </OverflowScrollBox>
              )
            }
          </SpaceBetween>
        </Container>
        <Box padding={{ top: "s" }}>
          {!!workflowId && <ResultAnalyticsSummary workflowId={workflowId} />}
        </Box>
      </ContentLayout>
    </LiveResultContext.Provider>
  );
}
