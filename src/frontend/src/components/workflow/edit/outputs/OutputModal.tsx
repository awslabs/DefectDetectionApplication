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

import Modal from "@cloudscape-design/components/modal";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import { FormProvider, useForm } from "react-hook-form";
import { Form } from "@cloudscape-design/components";
import { yupResolver } from "@hookform/resolvers/yup";
import schema, { SchemaType } from "./schema";
import FormInput from "../../../form/FormInput";
import FormRadioGroup from "../../../form/FormRadioGroup";
import FormSelect from "../../../form/FormSelect";
import { Rule, SignalType } from "../../types";

interface OutputModalProps {
  isVisible: boolean;
  onCancel: () => void;
  onSubmit: (values: SchemaType) => void;
  // If editOutput is provided, then this modal becomes edit instead of add
  editOutput?: SchemaType;
}

export default function OutputModal({
  isVisible,
  onCancel,
  onSubmit,
  editOutput,
}: OutputModalProps): JSX.Element {
  const form = useForm<SchemaType>({
    resolver: yupResolver(schema),
    mode: "onSubmit",
    values: {
      signal: editOutput?.signal ?? SignalType.RisingEdge,
      // @ts-ignore: Ignore a conflict between react-hook-form and yup default
      // numeric values. Our yup schema has .required() to prevent null as a
      // value, but we want this field to start as null knowing that that will
      // be a validation error.
      pin: editOutput?.pin ?? null,
      // @ts-ignore
      debounce: editOutput?.debounce ?? null,
      rule: editOutput?.rule ?? null,
    },
  });

  const onFormSubmit = form.handleSubmit((values: SchemaType) => {
    onSubmit(values);
    form.reset();
  });

  return (
    <Modal
      onDismiss={(): void => {
        onCancel();
        form.reset();
      }}
      visible={isVisible}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              variant="link"
              onClick={(): void => {
                onCancel();
                form.reset();
              }}
            >
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={(): void => {
                onFormSubmit();
              }}
            >
              Save
            </Button>
          </SpaceBetween>
        </Box>
      }
      header={editOutput ? "Edit output" : "Add output"}
    >
      <FormProvider {...form}>
        <form onSubmit={onFormSubmit}>
          <Form variant="embedded">
            <SpaceBetween direction="vertical" size="l">
              <FormRadioGroup
                name="signal"
                label="Signal type"
                description="Event type that sends the signal."
                items={[
                  {
                    value: SignalType.RisingEdge,
                    label: "Rising edge",
                  },
                  {
                    value: SignalType.FallingEdge,
                    label: "Falling edge",
                  },
                ]}
              />

              <FormInput
                name="pin"
                label="Pin value"
                description="Device output pin value."
                placeholder="1"
                constraintText="Numeric values only."
                type="number"
              />

              <FormInput
                name="debounce"
                label="Pulse width"
                description="Amount of time in milliseconds to send the signal."
                placeholder="750"
                constraintText="Numeric values only."
                type="number"
              />

              <FormSelect
                name="rule"
                label="Rule"
                description="Rule that triggers sending a signal through this output."
                placeholder="Choose a rule"
                options={[
                  {
                    value: Rule.AllResults,
                    label: "All results",
                  },
                  {
                    value: Rule.Anomaly,
                    label: "Anomaly",
                  },
                  {
                    value: Rule.Normal,
                    label: "Normal",
                  },
                ]}
              />
            </SpaceBetween>
          </Form>
        </form>
      </FormProvider>
    </Modal>
  );
}
