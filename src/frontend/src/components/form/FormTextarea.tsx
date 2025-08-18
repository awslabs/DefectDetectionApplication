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
import { ControlledFormProps, NameProp } from "./types";
import { getErrorProp } from "./helpers";
import {
  FormField,
  FormFieldProps,
  TextContent,
  Textarea,
  TextareaProps,
} from "@cloudscape-design/components";

type FormTextareaProps = Omit<
  TextareaProps & FormFieldProps,
  ControlledFormProps
> &
  NameProp & {
    // Per UX design, we want textareas to have a counter below them
    max: number;
  };

export default function FormTextarea({
  name,
  max,
  ...props
}: FormTextareaProps) {
  const { field, fieldState } = useController({ name });
  const value = useWatch({ name });
  return (
    <FormField {...props} {...getErrorProp(fieldState)}>
      <Textarea
        {...props}
        {...field}
        onChange={(event) => field.onChange(event.detail.value)}
      />

      <TextContent>
        <small>{`${value?.length ?? 0}/${max} characters remaining`}</small>
      </TextContent>
    </FormField>
  );
}
