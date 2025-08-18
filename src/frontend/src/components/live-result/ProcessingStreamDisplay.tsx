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

import { Alert, Button, Spinner } from "@cloudscape-design/components";
import { useQuery } from "@tanstack/react-query";
import { getImageSource } from "api/ImageSourceAPI";
import { getWorkflow } from "api/WorkflowAPI";
import ManualDisplay from "./ManualDisplay";
import RefreshDisplay from "./RefreshDisplay";
import { isWorkflowModelAttached } from "components/utils";
import { useNavigate } from "react-router-dom";

interface ProcessingStreamDisplayProps {
  workflowId: string;
}

export default function ProcessingStreamDisplay({
  workflowId,
}: ProcessingStreamDisplayProps): JSX.Element {

  const navigate = useNavigate();

  const getQuery = useQuery({
    queryKey: ["getLiveWorkflow", workflowId],
    queryFn: async () => {
      const workflow = await getWorkflow(workflowId);
      if (!workflow.imageSources) {
        return {
          workflow: undefined,
          imageSource: undefined,
        };
      }

      const imageSource = await getImageSource(
        workflow.imageSources[0].imageSourceId
      );
      return {
        workflow,
        imageSource,
      };
    },
    cacheTime: 0,
  });

  if (getQuery.isLoading || !getQuery.data) {
    return <Spinner size="big" />;
  }

  if (!getQuery.data.workflow || !getQuery.data.imageSource) {
    return <Alert type="info">This workflow isn't configured.</Alert>;
  }

  if (!isWorkflowModelAttached(getQuery.data.workflow)) {
    return (
      <Alert
        type="info"
        action={(
          <Button variant="normal" onClick={(): void => navigate(`/workflows/${workflowId}/edit`)}>
            Edit workflow
          </Button>
        )}
      >
        This workflow does not use a model. Add a model to this workflow to run inference.
      </Alert>
    )
  }

  const isRestApi = getQuery.data.workflow.inputConfigurations.length === 0;

  return isRestApi ? (
    <ManualDisplay
      key={workflowId}
      workflow={getQuery.data.workflow}
      imageSource={getQuery.data.imageSource}
    />
  ) : (
    <RefreshDisplay key={workflowId} workflow={getQuery.data.workflow} />
  );
}
