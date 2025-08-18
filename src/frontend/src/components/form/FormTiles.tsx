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
  FormField,
  FormFieldProps,
  Tiles,
  TilesProps,
} from "@cloudscape-design/components";
import { ControlledFormProps, NameProp } from "./types";
import { getErrorProp } from "./helpers";

type FormTilesProps = Omit<TilesProps & FormFieldProps, ControlledFormProps> &
  NameProp;

export default function FormTiles({ name, ...props }: FormTilesProps) {
  const { field, fieldState } = useController({ name });
  return (
    <FormField {...props} {...getErrorProp(fieldState)}>
      <Tiles
        {...props}
        {...field}
        onChange={(event) => {
          props.onChange?.(event);
          field.onChange(event.detail.value);
        }}
      />
    </FormField>
  );
}
