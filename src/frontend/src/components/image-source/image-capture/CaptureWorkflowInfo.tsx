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

import { Form, SpaceBetween, TextContent } from "@cloudscape-design/components";
import { ValueWithLabel } from "Common";
import FormInput from "components/form/FormInput";
import { WorkflowTriggerType } from "components/image-source/types";
import { getModelNameWithVersion, getWorkflowMetadata } from "components/utils";
import { Workflow } from "components/workflow/types";
import { FormProvider, UseFormReturn } from "react-hook-form";
import { CaptureConfigSchemaType } from "./captureConfigSchema";
import { CAPTURE_COUNT_MAX, CAPTURE_COUNT_MIN, CAPTURE_INTERVAL_MAX, CAPTURE_INTERVAL_MIN } from "./constants";

interface CaptureWorkflowInfoProps {
  workflow: Workflow;
  form: UseFormReturn<CaptureConfigSchemaType>;
  disabled?: boolean;
}

export default function CaptureWorkflowInfo({ workflow, form, disabled }: CaptureWorkflowInfoProps): JSX.Element {

  const {
    modelVersion,
    modelName,
    hasModel,
    workflowTriggerType
  } = getWorkflowMetadata(workflow);
  const disableIntervalInput = form.watch("count") <= 1 || hasModel || disabled;
  const disableCountInput = hasModel || disabled;
  const disableFilePrefix = hasModel || disabled;
  return (
    <SpaceBetween direction="vertical" size="s">
      <SpaceBetween direction="vertical" size="xs">
        <TextContent>
          <h4>Workflow details</h4>
        </TextContent>
        <ValueWithLabel label="Model">
          {hasModel ? getModelNameWithVersion(modelName, modelVersion || "1") : "No model"}
        </ValueWithLabel>
        <ValueWithLabel label="Workflow trigger">
          {workflowTriggerType || "-"}
        </ValueWithLabel>
      </SpaceBetween>

      {
        workflowTriggerType === WorkflowTriggerType.RESTAPI && (
          <SpaceBetween direction="vertical" size="xs">
            <TextContent>
              <h4>Capture settings</h4>
            </TextContent>
            <FormProvider {...form}>
              <form>
                <Form>
                  <SpaceBetween direction="vertical" size="xs">
                    <SpaceBetween direction="vertical" size="xxs">
                      <FormInput
                        label="Set capture count"
                        description="Images to be captured."
                        name="count"
                        type="number"
                        disabled={disableCountInput}
                      />
                      <TextContent>
                        <small>{`Between ${CAPTURE_COUNT_MIN} and ${CAPTURE_COUNT_MAX}. Multiple images need capture interval.`}</small>
                      </TextContent>
                    </SpaceBetween>
                    <SpaceBetween direction="vertical" size="xxs">
                      <FormInput
                        label="Set capture interval"
                        description="Time in between image auto-capture."
                        name="interval"
                        type="number"
                        disabled={disableIntervalInput}
                      />
                      <TextContent>
                        <small>{`In seconds. Between ${CAPTURE_INTERVAL_MIN} and ${CAPTURE_INTERVAL_MAX}.`}</small>
                      </TextContent>
                    </SpaceBetween>
                    <FormInput
                      label={<span>Set file prefix - <em>optional</em></span>}
                      description="Prefix will be hyphenated."
                      name="filePrefix"
                      type="text"
                      placeholder="normal"
                      disabled={disableFilePrefix}
                    />
                  </SpaceBetween>
                </Form>
              </form>
            </FormProvider>
          </SpaceBetween>
        )
      }
    </SpaceBetween>
  );
}