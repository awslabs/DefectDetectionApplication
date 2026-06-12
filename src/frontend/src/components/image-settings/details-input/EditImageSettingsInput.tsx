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
import { Container, Header, SpaceBetween } from "@cloudscape-design/components";
import FormSliderInput from "../../form/FormSliderInput";
import { SettingsBounds } from "../bounds";

interface EditImageSettingsInputProps {
  namePrefix: string;
  settingsBounds: SettingsBounds;
  readOnly?: boolean;
}

/**
 * simple component for modifying gain and exposure for an image source
 */
export default function EditImageSettingsInput({
  namePrefix,
  settingsBounds,
  readOnly,
}: EditImageSettingsInputProps) {
  const { gainMin, gainMax, exposureMin, exposureMax, exposureUnit } =
    settingsBounds;

  return (
    <Container header={<Header variant="h1">Image settings</Header>}>
      <SpaceBetween direction="vertical" size="l">
        <FormSliderInput
          name={namePrefix + "Gain"}
          min={gainMin}
          max={gainMax}
          disabled={readOnly}
          label="Gain (Primary Brightness Control)"
          constraintText={`Adjust gain (${gainMin} to ${gainMax}) to control image brightness. Higher values = brighter images.`}
        />

        <FormSliderInput
          name={namePrefix + "Exposure"}
          min={exposureMin}
          max={exposureMax}
          disabled={readOnly}
          label="Exposure (Secondary Control)"
          constraintText={`Exposure time in ${exposureUnit} (${exposureMin} to ${exposureMax}). Use gain for primary brightness adjustment.`}
        />
      </SpaceBetween>
    </Container>
  );
}
