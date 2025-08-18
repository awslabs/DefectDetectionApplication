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
  SpaceBetween,
} from "@cloudscape-design/components";
import FormTiles from "../../form/FormTiles";
import { SignalType, WorkflowTrigger } from "../types";
import { useWatch } from "react-hook-form";
import FormInput from "../../form/FormInput";
import FormRadioGroup from "../../form/FormRadioGroup";

export default function Inputs(): JSX.Element {
  const trigger = useWatch({ name: "trigger" });

  return (
    <Container header={<Header variant="h2">Workflow trigger</Header>}>
      <SpaceBetween direction="vertical" size="l">
        {
          <>
            <FormTiles
              name="trigger"
              description="Method that triggers the running of your workflow."
              stretch
              items={[
                {
                  label: "Line operator or API call",
                  description:
                    "A line operator, or an API call, triggers the running of the workflow.",
                  value: WorkflowTrigger.RestApi,
                },
                {
                  label: "Digital input",
                  description:
                    "The workflow will execute when a device pin signal is received.",
                  value: WorkflowTrigger.DigitalInput,
                },
              ]}
            />

            {trigger === WorkflowTrigger.DigitalInput && (
              <SpaceBetween direction="vertical" size="l">
                <FormRadioGroup
                  name="signal"
                  label="Signal type"
                  description="Event that sends the signal."
                  stretch
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
                  description="Value or string for the input pin that transmits the signal."
                  placeholder="1"
                  constraintText="Numeric values only."
                  type="number"
                  stretch
                />

                <FormInput
                  name="debounce"
                  label="Debounce time"
                  description="Number of milliseconds to ignore repeated signals."
                  placeholder="750"
                  constraintText="Numeric values only."
                  type="number"
                  stretch
                />
              </SpaceBetween>
            )}
          </>
        }
      </SpaceBetween>
    </Container>
  );
}
