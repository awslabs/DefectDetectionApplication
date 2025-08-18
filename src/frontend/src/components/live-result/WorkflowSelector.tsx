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
import { Select, SelectProps } from "@cloudscape-design/components";
import { getWorkflowOptionList } from "api/WorkflowAPI";
import { useQuery } from "@tanstack/react-query";

interface WorkflowSelectorProps {
  setWorkflowId: (workflowId: string) => void;
  workflowId?: string;
  onWorkflowChange?: (workflowId: string) => void;
}

export function WorkflowSelector({
  setWorkflowId,
  workflowId,
  onWorkflowChange,
}: WorkflowSelectorProps): JSX.Element {
  const [selectedOption, setSelectedOption] =
    React.useState<SelectProps.Option | null>(
      workflowId ? { label: workflowId, value: workflowId } : null
    );

  const { data, isLoading } = useQuery({
    queryKey: ["getWorkflowOptionList"],
    queryFn: () => getWorkflowOptionList(),
  });

  /** update the selected option label from id to name once data is fetched */
  React.useEffect(() => {
    if (data && selectedOption && selectedOption.label === selectedOption.value) {
      setSelectedOption(data.find(item => item.value === selectedOption.value) || selectedOption)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data])

  return (
    <Select
      selectedOption={selectedOption}
      onChange={({ detail: { selectedOption } }): void => {
        setSelectedOption(selectedOption);
        setWorkflowId(selectedOption.value || "");
        onWorkflowChange?.(selectedOption.value || "");
      }}
      options={data ?? []}
      selectedAriaLabel="Selected"
      placeholder="Choose a workflow"
      loadingText="Loading workflows"
      statusType={isLoading ? "loading" : "finished"}
      triggerVariant="option"
    />
  );
}
