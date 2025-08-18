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
  Button,
  Container,
  ExpandableSection,
  Grid,
  Header,
  SpaceBetween,
} from "@cloudscape-design/components";
import FormTextarea from "../../form/FormTextarea";
import { PROCESSING_PIPELINE_MAX } from "../constants";
import { useController, useWatch } from "react-hook-form";

interface EditImageAdvancedSettingsInputProps {
  /**
   * Name prefix for the form that this input is connected with
   */
  namePrefix: string;
  /**
   * This is the original pipeline string that the "Reset" button rolls back to.
   * An original copy is left in-tact
   */
  initialPipelineString: string;
  /**
   * Callback - Intended to update the pipeline string used in the Preview API
   * @param newPipeline - the new pipeline string
   */
  setGstreamerPipelineToDownload: (newPipeline: string) => void;
}

export default function EditImageAdvancedSettingsInput(
  props: EditImageAdvancedSettingsInputProps,
) {
  const { namePrefix, initialPipelineString } = props;
  const gstreamerFieldName = `${namePrefix}GstreamerPipeline`;
  const { field } = useController({ name: gstreamerFieldName });
  const value = useWatch({ name: gstreamerFieldName });
  return (
    <ExpandableSection
      headerText="Advanced settings"
      variant="container"
      headingTagOverride="h3"
    >
      <SpaceBetween size={"m"}>
        <FormTextarea
          name={gstreamerFieldName}
          max={PROCESSING_PIPELINE_MAX}
          label="Gstreamer pipeline"
        />
        <Grid
          gridDefinition={[
            { colspan: { default: 5 } },
            { colspan: { default: 7 } },
          ]}
          disableGutters={true}
        >
          <Button
            formAction="none"
            iconAlign="left"
            iconName="refresh"
            variant="normal"
            onClick={(event) => {
              field.onChange(initialPipelineString);
            }}
          >
            Reset
          </Button>
          <Button
            formAction="none"
            variant="normal"
            onClick={(event) => {
              props.setGstreamerPipelineToDownload(value);
            }}
          >
            Apply
          </Button>
        </Grid>
      </SpaceBetween>
    </ExpandableSection>
  );
}
