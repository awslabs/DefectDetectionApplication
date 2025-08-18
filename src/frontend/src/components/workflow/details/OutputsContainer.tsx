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

import { useCollection } from "@cloudscape-design/collection-hooks";
import { Table, Header, Box } from "@cloudscape-design/components";
import { OutputConfiguration, SignalType, Rule } from "../types";

interface OutputsContainerProps {
  outputConfigurations: OutputConfiguration[];
}

export default function OutputsContainer(
  props: OutputsContainerProps,
): JSX.Element {
  const { items, collectionProps } = useCollection(props.outputConfigurations, {
    sorting: {},
  });
  return (
    <Table
      {...collectionProps}
      header={<Header variant="h2">Outputs</Header>}
      columnDefinitions={[
        {
          id: "signalType",
          header: "Signal type",
          cell: (item) =>
            item.signalType === SignalType.RisingEdge
              ? "Rising edge"
              : item.signalType === SignalType.FallingEdge
              ? "Falling edge"
              : "-",
          sortingField: "signalType",
        },
        {
          id: "pin",
          header: "Pin value",
          cell: (item) => item.pin || "-",
          sortingField: "pin",
        },
        {
          id: "pulseWidth",
          header: "Pulse width",
          cell: (item) => item.pulseWidth || "-",
          sortingField: "pulseWidth",
        },
        {
          id: "rule",
          header: "Rule",
          cell: (item) =>
            item.rule === Rule.AllResults
              ? "All results"
              : item.rule !== Rule.Anomaly && item.rule !== Rule.Normal
              ? "-"
              : item.rule,
          sortingField: "rule",
        },
      ]}
      items={items}
      loadingText="Loading outputs"
      empty={
        <Box textAlign="center" color="inherit">
          <strong>No outputs defined</strong>
          <Box padding={{ bottom: "s" }} variant="p" color="inherit">
            Edit the workflow to add outputs.
          </Box>
        </Box>
      }
    />
  );
}
