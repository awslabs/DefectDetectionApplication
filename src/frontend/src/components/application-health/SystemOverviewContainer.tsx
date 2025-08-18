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
  Box,
  ColumnLayout,
  Container,
  Header,
  Spinner,
  TextContent
} from "@cloudscape-design/components";
import StatusIndicator, { StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";
import { ValueWithLabel } from "../../Common";
import { SystemHealth } from "./types";
import ParagraphWrapper from "components/common/ParagraphWrapper";

interface SystemActivityContainerProps {
  systemHealth?: SystemHealth
}

export default function SystemOverviewContainer(
  props: SystemActivityContainerProps,
): JSX.Element {

  const systemActivityHeader = (
    <Header
      variant="h2"
      description={
        <ParagraphWrapper>
          Review the allocated disk space, CPU usage, and memory metrics for the edge device hosting the
          station. The metrics can include consumption that is unrelated to the running of the station.
        </ParagraphWrapper>
      }
    >
      Overview
    </Header>
  );

  if (!props.systemHealth) {
    return (
      <Container header={systemActivityHeader}>
        <Spinner />
      </Container>
    );
  }

  const diskUsageProps = getSystemStatsUsageProps(props.systemHealth?.diskUsagePercent)
  const cpuUsageProps = getSystemStatsUsageProps(props.systemHealth?.cpuUsagePercent)
  const memoryUsageProps = getSystemStatsUsageProps(props.systemHealth?.memoryUsagePercent)

  return (
    <Container header={systemActivityHeader}>
      <ColumnLayout columns={3} borders="vertical">
        <ValueWithLabel label="Station disk space">
          <TextContent>
            <small>
              Mounted disk space allocated to this station.
              This value might be less than the total disk space available to the edge device.
            </small>
          </TextContent>
          <Box variant="h2">
            <StatusIndicator {...diskUsageProps}>
              {`${props.systemHealth?.diskUsedSize} / ${props.systemHealth?.diskTotalSize} used`}
            </StatusIndicator>
          </Box>
        </ValueWithLabel>
        <ValueWithLabel label="Device CPU">
          <TextContent>
            <small>
              Total CPU utilization across the edge device.
            </small>
          </TextContent>
          <Box variant="h2">
            <StatusIndicator {...cpuUsageProps}>{`${props.systemHealth?.cpuUsagePercent}%`}</StatusIndicator>
          </Box>
        </ValueWithLabel>
        <ValueWithLabel label="Device memory">
          <TextContent>
            <small>
              Total memory load across edge device.
            </small>
          </TextContent>
          <Box variant="h2">
            <StatusIndicator {...memoryUsageProps}>{`${props.systemHealth?.memoryUsagePercent}%`}</StatusIndicator>
          </Box>
        </ValueWithLabel>
        <ValueWithLabel label="Nvidia CUDA Version">
          <TextContent> {props.systemHealth?.cudaVersion === "NOT_INSTALLED" ? "Not found" : props.systemHealth?.cudaVersion}</TextContent>
        </ValueWithLabel>
        <ValueWithLabel label="Nvidia TensorRT Version">
          <TextContent> {props.systemHealth?.tensorRTVersion === "NOT_INSTALLED" ? "Not found" : props.systemHealth?.tensorRTVersion}</TextContent>
        </ValueWithLabel>
        <ValueWithLabel label="OpenCV Version">
          <TextContent> {props.systemHealth?.opencvVersion === "NOT_INSTALLED" ? "Not found" : props.systemHealth?.opencvVersion}</TextContent>
        </ValueWithLabel>
      </ColumnLayout>
    </Container>
  );
}

function getSystemStatsUsageProps(usageValue: number): StatusIndicatorProps {
  const warningProps = {
    colorOverride: "red",
    type: "warning",
  } as StatusIndicatorProps
  const successProps = {
    type: "success",
  } as StatusIndicatorProps
  return usageValue <= 80 ? successProps : warningProps;
}