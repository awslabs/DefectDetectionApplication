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

import { Spinner } from "@cloudscape-design/components";
import { useQuery } from "@tanstack/react-query";
import { getWorkflow } from "api/WorkflowAPI";
import ResultCardContent from "./ResultCardContent";
import { HistoryResultPageType } from "./types";

interface ResultHistoryCardDisplayProps {
  workflowId: string;
  historyResultPageType: HistoryResultPageType;
}

export default function ResultHistoryCardDisplay({
  workflowId,
  historyResultPageType,
}: ResultHistoryCardDisplayProps): JSX.Element {
  const { data: workflow, isLoading: isLoadingWorkflow } = useQuery({
    queryKey: ["getHistoryWorkflow", workflowId],
    queryFn: () => getWorkflow(workflowId),
  });

  if (isLoadingWorkflow || !workflow) {
    return <Spinner size="big" />;
  }

  return (
    <ResultCardContent
      workflowId={workflowId}
      workflow={workflow}
      historyResultPageType={historyResultPageType}
    />
  );
}
