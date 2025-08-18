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
import { EXPOSURE_MAX, EXPOSURE_MIN, GAIN_MAX, GAIN_MIN } from "../constants";
import FormSliderInput from "../../form/FormSliderInput";

interface EditImageSettingsInputProps {
  namePrefix: string;
}

/**
 * simple component for modifying gain and exposure for an image source
 */
export default function EditImageSettingsInput({
  namePrefix,
}: EditImageSettingsInputProps) {
  return (
    <Container header={<Header variant="h1">Image settings</Header>}>
      <SpaceBetween direction="vertical" size="l">
        <FormSliderInput
          name={namePrefix + "Gain"}
          min={GAIN_MIN}
          max={GAIN_MAX}
          label="Gain"
          constraintText="Numeric values only. Between 1 to 100."
        />

        <FormSliderInput
          name={namePrefix + "Exposure"}
          min={EXPOSURE_MIN}
          max={EXPOSURE_MAX}
          label="Exposure"
          constraintText="Positive numeric values only."
        />
      </SpaceBetween>
    </Container>
  );
}
