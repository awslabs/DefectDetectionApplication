/*
 *  Copyright 2025 Amazon Web Services, Inc.
 *
 *  Licensed under the Apache License, Version 2.0 (the "License");
 *  you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the License for the specific language governing permissions and
 *  limitations under the License.
 */
import {
  Checkbox,
  Container,
  FormField,
  Header,
  SpaceBetween,
} from "@cloudscape-design/components";
import { useController } from "react-hook-form";
import FormInput from "components/form/FormInput";
import FormRadioGroup from "components/form/FormRadioGroup";
import { ImageSourceType } from "components/image-source/types";
import {
  LATENCY_MS_RANGE,
  MAX_FRAME_DIMENSION_RANGE,
  STALL_TIMEOUT_S_RANGE,
} from "./streamForm";

interface StreamFieldsProps {
  type: ImageSourceType.RTSP | ImageSourceType.RTMP;
  /** Editing an existing camera (shows the keep/remove credential rules). */
  editing?: boolean;
  /** Whether the device already holds credentials for the camera. */
  credentialsConfigured?: boolean;
}

const EXAMPLE_URLS = {
  [ImageSourceType.RTSP]: "rtsp://192.168.1.64:554/Streaming/Channels/101",
  [ImageSourceType.RTMP]: "rtmp://media.local/live/line1",
};

function ClearCredentialsCheckbox(): JSX.Element {
  const { field } = useController({ name: "streamClearCredentials" });
  return (
    <Checkbox
      checked={!!field.value}
      onChange={(event): void => field.onChange(event.detail.checked)}
      description="The camera then connects without credentials."
    >
      Remove the stored credentials
    </Checkbox>
  );
}

/**
 * The stream and credential fields of an RTSP/RTMP Image_Source
 * (rtsp-rtmp-stream-cameras Requirement 4.1). Credentials are write-only: the
 * device never returns them, so the fields always start empty.
 */
export default function StreamFields({
  type,
  editing = false,
  credentialsConfigured = false,
}: StreamFieldsProps): JSX.Element {
  const isRtsp = type === ImageSourceType.RTSP;
  const [latencyMin, latencyMax] = LATENCY_MS_RANGE;
  const [dimensionMin, dimensionMax] = MAX_FRAME_DIMENSION_RANGE;
  const [stallMin, stallMax] = STALL_TIMEOUT_S_RANGE;
  return (
    <>
      <Container header={<Header variant="h2">Stream</Header>}>
        <SpaceBetween direction="vertical" size="l">
          <FormInput
            name="streamUrl"
            stretch
            label="Stream URL"
            description={
              isRtsp
                ? "The camera's RTSP address (rtsp:// or rtsps://). Enter credentials below, never in the URL."
                : "The RTMP address of the stream (rtmp:// or rtmps://). Enter a stream key below, never in the URL."
            }
            placeholder={EXAMPLE_URLS[type]}
          />
          {isRtsp && (
            <FormRadioGroup
              name="streamTransport"
              label="Transport"
              description="TCP is the most reliable over real networks; auto tries UDP, then TCP."
              items={[
                { value: "tcp", label: "TCP" },
                { value: "udp", label: "UDP" },
                { value: "auto", label: "Auto" },
              ]}
            />
          )}
          {isRtsp && (
            <FormInput
              name="streamLatencyMs"
              inputMode="numeric"
              label="Latency (ms)"
              constraintText={`From ${latencyMin} to ${latencyMax}. Buffering that absorbs network jitter.`}
            />
          )}
          <FormRadioGroup
            name="streamDecoder"
            label="Decoder"
            description="Auto uses the hardware decoder when the device has one, and falls back to software."
            items={[
              { value: "auto", label: "Auto" },
              { value: "hardware", label: "Hardware only" },
              { value: "software", label: "Software only" },
            ]}
          />
          <FormInput
            name="streamMaxFrameDimension"
            inputMode="numeric"
            label="Maximum frame dimension (pixels)"
            constraintText={`From ${dimensionMin} to ${dimensionMax}. Larger frames are scaled down to fit, keeping the aspect ratio.`}
          />
          <FormInput
            name="streamStallTimeoutS"
            inputMode="numeric"
            label="Stall timeout (seconds)"
            constraintText={`From ${stallMin} to ${stallMax}. The camera reconnects when no frame arrives for this long.`}
          />
        </SpaceBetween>
      </Container>
      <Container
        header={
          <Header
            variant="h2"
            description={
              editing && credentialsConfigured
                ? "Credentials are stored on this device and never shown. Leave all fields blank to keep them; entering any field replaces them."
                : "Optional. Stored on this device only, and never shown again."
            }
          >
            Credentials
          </Header>
        }
      >
        <SpaceBetween direction="vertical" size="l">
          <FormInput name="streamUsername" autoComplete={false} label="Username" />
          <FormInput
            name="streamPassword"
            type="password"
            autoComplete={false}
            label="Password"
          />
          <FormInput
            name="streamUrlSecret"
            type="password"
            autoComplete={false}
            label={isRtsp ? "URL secret" : "Stream key"}
            description={
              isRtsp
                ? "A secret appended to the URL when connecting, such as ?token=… Leave blank if the camera takes a username and password."
                : "Appended to the stream URL when connecting."
            }
          />
          {editing && credentialsConfigured && (
            <FormField>
              <ClearCredentialsCheckbox />
            </FormField>
          )}
        </SpaceBetween>
      </Container>
    </>
  );
}
