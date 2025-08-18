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
import { SpaceBetween } from "@cloudscape-design/components";
import EditImageSettingsInput from "./details-input/EditImageSettingsInput";
import EditImageAdvancedSettingsInput from "./details-input/EditImageAdvancedSettingsInput";

type EditImageSettingsPaneProps = {
  initialPipelineString: string;
  setGstreamerPipelineToDownload: (newPipeline: string) => void;
};
export default function EditImageSettingsPane(
  props: EditImageSettingsPaneProps,
) {
  return (
    <>
      <SpaceBetween direction="vertical" size="l">
        <EditImageSettingsInput namePrefix="edit" />
        <EditImageAdvancedSettingsInput
          namePrefix="edit"
          initialPipelineString={props.initialPipelineString}
          setGstreamerPipelineToDownload={props.setGstreamerPipelineToDownload}
        />
      </SpaceBetween>
    </>
  );
}
