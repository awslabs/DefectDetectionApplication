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

import { Button, Header, SpaceBetween } from "@cloudscape-design/components";
import React from "react";
import { useNavigate } from "react-router-dom";

interface DetailsHeaderProps {
  editWorkflowUrl: string;
  workflowName: string;
  onClickDelete: () => void;
}

export default function DetailsHeader({
  editWorkflowUrl,
  workflowName,
  onClickDelete
}: DetailsHeaderProps): JSX.Element {
  const navigate = useNavigate();

  return (
    <Header
      variant="h1"
      actions={
        <SpaceBetween direction="horizontal" size="xs">
          <Button onClick={onClickDelete}>
            Delete workflow
          </Button>
          <Button
            variant="primary"
            onClick={(): void => navigate(editWorkflowUrl || "")}
          >
            Edit workflow
          </Button>
        </SpaceBetween>

      }
    >
      {workflowName}
    </Header>
  );
}
