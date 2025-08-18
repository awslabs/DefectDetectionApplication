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
  Container,
  Header,
  ColumnLayout,
  Link,
} from "@cloudscape-design/components";
import { ValueWithLabel } from "Common";
import { DATE_FORMAT } from "components/date-time-format";
import { useNavigate } from "react-router-dom";
import format from "date-fns/format";
import { getModelNameWithVersion } from "components/utils";

interface DetailsContainerProps {
  workflowName: string;
  workflowDescription?: string;
  lastUpdateTime: number;
  imageSourceId: string;
  imageSourceName: string;
  modelName: string;
  workflowId: string;
  modelVersion: string;
}

export default function DetailsContainer(
  {
    imageSourceId,
    imageSourceName,
    workflowId,
    workflowName,
    workflowDescription,
    modelName,
    modelVersion,
    lastUpdateTime,
  }: DetailsContainerProps,
): JSX.Element {
  const navigate = useNavigate();
  const imageSourceUrl = `/image-sources/${imageSourceId}`;

  return (
    <Container header={<Header variant="h2">Workflow details</Header>}>
      <ColumnLayout columns={3} borders="vertical">
        <ValueWithLabel label="Name">{workflowName}</ValueWithLabel>
        <ValueWithLabel label="Description">
          {workflowDescription || "-"}
        </ValueWithLabel>
        <ValueWithLabel label="Image source">
          <Link
            onFollow={(event): void => {
              event.preventDefault();
              navigate(imageSourceUrl);
            }}
            href={imageSourceUrl}
          >
            {imageSourceName}
          </Link>
        </ValueWithLabel>
        <ValueWithLabel label="Model">{getModelNameWithVersion(modelName, modelVersion)}</ValueWithLabel>
        <ValueWithLabel label="ID">{workflowId}</ValueWithLabel>
        <ValueWithLabel label="Date modified">
          {format(lastUpdateTime, DATE_FORMAT)}
        </ValueWithLabel>
      </ColumnLayout>
    </Container>
  );
}
