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
  Header,
  Spinner,
  Table,
} from "@cloudscape-design/components";
import StatusIndicator, {
  StatusIndicatorProps,
} from "@cloudscape-design/components/status-indicator";
import ParagraphWrapper from "components/common/ParagraphWrapper";
import { GreengrassComponent } from "./types";

interface GreengrassComponentsContainerProps {
  components?: GreengrassComponent[];
  loading?: boolean;
}

export default function GreengrassComponentsContainer(
  props: GreengrassComponentsContainerProps,
): JSX.Element {
  const header = (
    <Header
      variant="h2"
      counter={
        props.components ? `(${props.components.length})` : undefined
      }
      description={
        <ParagraphWrapper>
          AWS IoT Greengrass components installed on this edge device, including
          their deployed version and current lifecycle state.
        </ParagraphWrapper>
      }
    >
      Greengrass components
    </Header>
  );

  if (props.loading && !props.components) {
    return (
      <Container header={header}>
        <Spinner />
      </Container>
    );
  }

  return (
    <Container header={header}>
      <Table
        variant="borderless"
        items={props.components || []}
        trackBy="componentName"
        columnDefinitions={[
          {
            id: "componentName",
            header: "Component name",
            cell: (item: GreengrassComponent) => item.componentName,
            sortingField: "componentName",
          },
          {
            id: "version",
            header: "Version",
            cell: (item: GreengrassComponent) => item.version,
            sortingField: "version",
          },
          {
            id: "state",
            header: "State",
            cell: (item: GreengrassComponent) => (
              <StatusIndicator {...getComponentStateProps(item.state)}>
                {item.state}
              </StatusIndicator>
            ),
            sortingField: "state",
          },
        ]}
        empty={
          <Box textAlign="center" color="inherit">
            No components found
          </Box>
        }
      />
    </Container>
  );
}

function getComponentStateProps(state: string): StatusIndicatorProps {
  switch (state) {
    case "RUNNING":
    case "FINISHED":
      return { type: "success" };
    case "STARTING":
    case "INSTALLED":
    case "STOPPING":
      return { type: "in-progress" };
    case "NEW":
      return { type: "pending" };
    case "BROKEN":
    case "ERRORED":
      return { type: "error" };
    case "STOPPED":
      return { type: "stopped" };
    default:
      return { type: "info" };
  }
}
