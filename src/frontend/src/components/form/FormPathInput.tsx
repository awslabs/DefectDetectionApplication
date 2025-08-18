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
import { useController } from "react-hook-form";
import {
  Box,
  FormField,
  FormFieldProps,
  Input,
  InputProps,
} from "@cloudscape-design/components";
import { ControlledFormProps, NameProp } from "./types";
import { getErrorProp } from "./helpers";

// Omit props like value because these will automatically be handled by react-hook-form.
type FormPathInputProps = Omit<
  InputProps & FormFieldProps,
  ControlledFormProps
> &
  NameProp & {
    // We want to hardcode path inputs with a given prefix (e.g. /aws_dda/)
    prefix: string;
  };

export default function FormPathInput({
  name,
  prefix,
  ...props
}: FormPathInputProps): JSX.Element {
  const { field, fieldState } = useController({ name });
  return (
    <FormField {...props} {...getErrorProp(fieldState)}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
        }}
      >
        <Box margin={{ right: "xxs" }}>{prefix}</Box>
        <div style={{ flexGrow: 1 }}>
          <Input
            {...props}
            {...field}
            onChange={(event): void => {
              props.onChange?.(event);
              field.onChange(event.detail.value);
            }}
            onBlur={(event): void => {
              props.onBlur?.(event);
              field.onBlur();
            }}
          />
        </div>
      </div>
    </FormField>
  );
}
