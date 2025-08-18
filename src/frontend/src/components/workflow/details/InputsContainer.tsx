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
} from "@cloudscape-design/components";
import { ValueWithLabel } from "Common";
import { InputConfiguration, SignalType } from "../types";

interface InputsContainerProps {
  inputConfigurations: InputConfiguration[];
  isFolderSrc: boolean;
}

export default function InputsContainer(
  props: InputsContainerProps,
): JSX.Element {
  return (
    <Container header={<Header variant="h2">Workflow trigger</Header>}>
      {
        <ColumnLayout
          columns={props.inputConfigurations.length === 0 ? 1 : 3}
          borders="vertical"
        >
          {props.inputConfigurations.length
            ? <span>Digital input</span>
            : <span>Line operator or API call</span>}
          {props.inputConfigurations.length !== 0 && (
            <>
              <ValueWithLabel label="Signal type">
                {props.inputConfigurations[0].triggerState ===
                  SignalType.RisingEdge
                  ? "Rising edge"
                  : props.inputConfigurations[0].triggerState ===
                    SignalType.FallingEdge
                    ? "Falling edge"
                    : "-"}
              </ValueWithLabel>
              <ValueWithLabel label="Pin value">
                {props.inputConfigurations[0].pin}
              </ValueWithLabel>
              <ValueWithLabel label="Debounce time">
                {props.inputConfigurations[0].debounceTime}
              </ValueWithLabel>
              <br />
            </>
          )}
        </ColumnLayout>
      }
    </Container>
  );
}
