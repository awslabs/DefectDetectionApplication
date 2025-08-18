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
  Header,
  SpaceBetween,
  Button,
  Container,
  Table,
  Box,
} from "@cloudscape-design/components";
import OutputModal from "./OutputModal";
import { useState } from "react";
import { SchemaType as OutputSchemaType } from "./schema";
import { useFieldArray } from "react-hook-form";
import { SchemaType as EditWorkflowSchemaType } from "../schema";
import { Rule, SignalType } from "components/workflow/types";

type OutputWithId = OutputSchemaType & {
  id: string;
};

export default function Outputs(): JSX.Element {
  const [isModalVisible, setIsModalVisible] = useState(false);
  const { fields, append, remove, update } =
    useFieldArray<EditWorkflowSchemaType>({
      name: "outputs",
    });
  const [selectIndex, setSelectIndex] = useState<number>(-1);
  const [editOutput, setEditOutput] = useState<OutputWithId>();

  return (
    <Container>
      <Table<OutputWithId>
        variant="embedded"
        selectionType="single"
        header={
          <Header
            variant="h2"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  formAction="none"
                  disabled={selectIndex < 0}
                  onClick={(): void => {
                    setEditOutput(fields[selectIndex]);
                    setIsModalVisible(true);
                  }}
                >
                  Edit
                </Button>
                <Button
                  formAction="none"
                  disabled={selectIndex < 0}
                  onClick={(): void => {
                    const index = fields.findIndex(
                      (field) => field.id === fields[selectIndex]?.id,
                    );
                    if (index >= 0) {
                      remove(index);
                      setSelectIndex(-1);
                    }
                  }}
                >
                  Delete
                </Button>
                <Button
                  formAction="none"
                  onClick={(): void => {
                    setEditOutput(undefined);
                    setIsModalVisible(true);
                  }}
                >
                  Add output
                </Button>
              </SpaceBetween>
            }
          >
            Outputs - <em>optional</em>
          </Header>
        }
        items={fields}
        columnDefinitions={[
          {
            id: "signal",
            header: "Signal type",
            cell: (item) =>
              item.signal === SignalType.RisingEdge
                ? "Rising edge"
                : item.signal === SignalType.FallingEdge
                  ? "Falling edge"
                  : "-",
          },
          {
            id: "pin",
            header: "Pin value",
            cell: (item) => item.pin,
          },
          {
            id: "debounce",
            header: "Pulse width",
            cell: (item) => item.debounce,
          },
          {
            id: "rule",
            header: "Rule",
            cell: (item) =>
              item.rule?.value === Rule.AllResults
                ? "All results"
                : item.rule?.value === Rule.Normal
                  ? "Normal"
                  : item.rule?.value === Rule.Anomaly
                    ? "Anomaly"
                    : "-",
          },
        ]}
        selectedItems={selectIndex < 0 ? [] : [fields[selectIndex]]}
        trackBy="id"
        onSelectionChange={(event): void =>
          setSelectIndex(
            fields.findIndex(
              (field) => field.id === event.detail.selectedItems[0].id,
            ),
          )
        }
        empty={
          <Box textAlign="center" color="inherit">
            <b>No outputs defined</b>
            <Box padding={{ bottom: "s" }} variant="p" color="inherit">
              Edit the workflow to add outputs.
            </Box>
            <Button
              formAction="none"
              onClick={(): void => {
                setEditOutput(undefined);
                setIsModalVisible(true);
              }}
            >
              Add output
            </Button>
          </Box>
        }
      />

      <OutputModal
        isVisible={isModalVisible}
        editOutput={editOutput}
        onCancel={(): void => setIsModalVisible(false)}
        onSubmit={(values: OutputSchemaType): void => {
          setIsModalVisible(false);
          if (editOutput) {
            const index = fields.findIndex(
              (field) => field.id === editOutput.id,
            );
            update(index, values);
          } else {
            append(values);
          }
        }}
      />
    </Container>
  );
}
