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

import { Box, BoxProps } from "@cloudscape-design/components";
import * as React from "react";
import { Connection } from "./config/Interface";

interface ValueWithLabelProps {
  label: any;
  children?: any;
  labelBoxVariant?: BoxProps.Variant
}

export const ValueWithLabel = (props: ValueWithLabelProps) => (
  <div>
    <Box variant={props.labelBoxVariant || "awsui-key-label"}>{props.label}</Box>
    <div>{props.children}</div>
  </div>
);

export function isMock() {
  return (
    Connection.ENDPOINT === undefined ||
    Connection.ENDPOINT === null ||
    Connection.ENDPOINT === ""
  );
}
