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

import { Box } from "@cloudscape-design/components";
import { AnomalyLabel } from "api/WorkflowAPI";

export default function AnomalyLabels({
  labelInfo,
}: {
  labelInfo: AnomalyLabel;
}): JSX.Element {
  const color = labelInfo["hex-color"];
  const label = labelInfo["class-name"];
  const labelStyle = {
    height: "14px",
    width: "14px",
    background: color,
    alignItems: "center",
  };
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
      }}
    >
      <Box margin={{ right: "xs" }}>
        <div style={labelStyle}></div>
      </Box>
      <div style={{ flexGrow: 1 }}>{label}</div>
    </div>
  );
}
