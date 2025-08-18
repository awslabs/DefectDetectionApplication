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
import { useController, useWatch } from "react-hook-form";
import {
  FormField,
  FormFieldProps,
  Input,
  InputProps,
  SpaceBetween,
} from "@cloudscape-design/components";
import Slider from "rc-slider";
import {
  colorBackgroundButtonPrimaryDefault,
  colorDragPlaceholderActive,
} from "@cloudscape-design/design-tokens";
import styled from "styled-components";
import { ControlledFormProps, SliderInputProp } from "./types";
import { getErrorProp } from "./helpers";

// custom style for the slider component click-and-drag
const CustomStyleDiv = styled.div`
  .rc-slider-handle-dragging.rc-slider-handle-dragging.rc-slider-handle-dragging {
    border: none;
    box-shadow: none;
  }
`;

const RAIL_HEIGHT = "10px";
const TRACK_HEIGHT = "10px";
const HANDLE_DIAMETER = "17px";
const HANDLE_TOP_OFFSET = "-4px";

// Omit props like value because these will automatically be handled by react-hook-form.
type FormSliderInputProps = Omit<
  InputProps & FormFieldProps,
  ControlledFormProps
> &
  SliderInputProp;

export default function FormSliderInput({
  name,
  ...props
}: FormSliderInputProps) {
  const { field, fieldState } = useController({ name });
  const sliderValue = useWatch({ name });

  const onValueChange = (newValue: string | number): void => {
    const newIntValue = Number.parseInt(`${newValue}`);

    field.onChange(newIntValue);
  };

  const onValueBlur: (newValue: string | number) => void = (
    newValue: string | number,
  ) => {
    onValueChange(newValue);
    field.onBlur();
  };

  return (
    <FormField {...props} {...getErrorProp(fieldState)}>
      <SpaceBetween size={"s"}>
        <CustomStyleDiv>
          <Slider
            min={props.min}
            max={props.max}
            value={sliderValue}
            onChange={(newValue) => {
              onValueChange(newValue as number);
            }}
            onAfterChange={(newValue) => {
              onValueBlur(newValue as number);
            }}
            railStyle={{
              backgroundColor: colorDragPlaceholderActive,
              height: RAIL_HEIGHT,
            }}
            trackStyle={{
              backgroundColor: colorDragPlaceholderActive,
              height: TRACK_HEIGHT,
            }}
            handleStyle={{
              border: "none",
              backgroundColor: colorBackgroundButtonPrimaryDefault,
              width: HANDLE_DIAMETER,
              height: HANDLE_DIAMETER,
              marginTop: HANDLE_TOP_OFFSET,
            }}
            dotStyle={{
              border: "none",
              boxShadow: "none",
            }}
            activeDotStyle={{
              border: "none",
              boxShadow: "none",
            }}
          />
        </CustomStyleDiv>
        <Input
          {...props}
          {...field}
          type="number"
          value={`${sliderValue}`}
          onChange={(event) => {
            onValueChange(event.detail.value);
          }}
          onBlur={(event) => {
            onValueBlur(sliderValue);
          }}
        />
      </SpaceBetween>
    </FormField>
  );
}
