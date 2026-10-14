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
/**
 * The RTSP/RTMP Image_Source form fields (rtsp-rtmp-stream-cameras
 * Requirements 4.1, 4.2): the yup rules the add and edit forms share, their
 * defaults, and the request parts built from them. The rules mirror the
 * LocalServer API (`model/stream_source.py`), so a request the form accepts
 * is one the API accepts.
 */
import * as yup from "yup";
import {
  ImageSourceType,
  StreamCredentials,
  StreamSettings,
} from "components/image-source/types";
import { isStreamImageSource } from "components/utils";
import { SCHEMES_BY_SOURCE_TYPE, checkStreamUrl } from "../streamUrl";

export const STREAM_TYPES: readonly ImageSourceType[] = [
  ImageSourceType.RTSP,
  ImageSourceType.RTMP,
];

export function isStreamType(type: unknown): boolean {
  return typeof type === "string" && isStreamImageSource(type);
}

export const TRANSPORTS = ["tcp", "udp", "auto"] as const;
export const DECODERS = ["auto", "hardware", "software"] as const;

/** The device defaults (Requirement 4.1). */
export const STREAM_DEFAULTS = {
  transport: "tcp",
  latencyMs: 200,
  decoder: "auto",
  maxFrameDimension: 1920,
  stallTimeoutS: 10,
} as const;

export const LATENCY_MS_RANGE = [0, 5000] as const;
export const MAX_FRAME_DIMENSION_RANGE = [320, 4096] as const;
export const STALL_TIMEOUT_S_RANGE = [2, 60] as const;
export const CREDENTIAL_MAX_LENGTH = 1024;

// Control characters are refused by the API (and could hide in a paste).
// eslint-disable-next-line no-control-regex
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;

function integerField(
  label: string,
  [min, max]: readonly [number, number],
): yup.NumberSchema {
  return yup
    .number()
    .transform((value, original) =>
      original === "" || original === null || original === undefined
        ? undefined
        : value,
    )
    .typeError(`${label} must be a whole number.`)
    .integer(`${label} must be a whole number.`)
    .min(min, `${label} must be from ${min} to ${max}.`)
    .max(max, `${label} must be from ${min} to ${max}.`);
}

function credentialField(label: string): yup.StringSchema {
  return yup
    .string()
    .max(
      CREDENTIAL_MAX_LENGTH,
      `${label} can have a maximum of ${CREDENTIAL_MAX_LENGTH} characters.`,
    )
    .test(
      "no-control-characters",
      `${label} must not contain control characters.`,
      (value) => !value || !CONTROL_CHARACTERS.test(value),
    );
}

const whenStream = {
  is: (type: unknown): boolean => isStreamType(type),
};

/** A number field that is lenient while its type does not show it. */
function hiddenNumber(): yup.NumberSchema {
  return yup
    .number()
    .transform((value, original) =>
      original === "" ||
      original === null ||
      original === undefined ||
      Number.isNaN(value)
        ? undefined
        : value,
    );
}

/** The stream fields, gated on the form's `type`; spread into a schema. */
export const streamSchemaFields = {
  streamUrl: yup.string().when("type", {
    ...whenStream,
    then: (schema) =>
      schema.test("stream-url", "", function validate(value) {
        const schemes = SCHEMES_BY_SOURCE_TYPE[this.parent.type as string] ?? [];
        const problem = checkStreamUrl(value ?? "", schemes);
        return problem === null
          ? true
          : this.createError({ message: problem.message });
      }),
  }),
  streamTransport: yup.string().when("type", {
    is: ImageSourceType.RTSP,
    then: (schema) =>
      schema.oneOf([...TRANSPORTS], "Choose a transport."),
  }),
  streamLatencyMs: hiddenNumber().when("type", {
    is: ImageSourceType.RTSP,
    then: () => integerField("Latency", LATENCY_MS_RANGE),
  }),
  streamDecoder: yup.string().when("type", {
    ...whenStream,
    then: (schema) => schema.oneOf([...DECODERS], "Choose a decoder policy."),
  }),
  streamMaxFrameDimension: hiddenNumber().when("type", {
    ...whenStream,
    then: () =>
      integerField("Maximum frame dimension", MAX_FRAME_DIMENSION_RANGE),
  }),
  streamStallTimeoutS: hiddenNumber().when("type", {
    ...whenStream,
    then: () => integerField("Stall timeout", STALL_TIMEOUT_S_RANGE),
  }),
  streamUsername: yup.string().when("type", {
    ...whenStream,
    then: () => credentialField("The username"),
  }),
  streamPassword: yup.string().when("type", {
    ...whenStream,
    then: () => credentialField("The password"),
  }),
  streamUrlSecret: yup.string().when("type", {
    ...whenStream,
    then: () => credentialField("The URL secret"),
  }),
  streamClearCredentials: yup.boolean(),
};

/** The stream fields' values as the form holds them. */
export interface StreamFormValues {
  streamUrl?: string;
  streamTransport?: string;
  streamLatencyMs?: number | string;
  streamDecoder?: string;
  streamMaxFrameDimension?: number | string;
  streamStallTimeoutS?: number | string;
  streamUsername?: string;
  streamPassword?: string;
  streamUrlSecret?: string;
  streamClearCredentials?: boolean;
}

/** The form's initial stream values: the stored settings or the defaults. */
export function streamFormDefaults(
  url?: string,
  settings?: StreamSettings | null,
): StreamFormValues {
  const stored = settings ?? {};
  return {
    streamUrl: url ?? "",
    streamTransport: stored.transport ?? STREAM_DEFAULTS.transport,
    streamLatencyMs: String(stored.latencyMs ?? STREAM_DEFAULTS.latencyMs),
    streamDecoder: stored.decoder ?? STREAM_DEFAULTS.decoder,
    streamMaxFrameDimension: String(
      stored.maxFrameDimension ?? STREAM_DEFAULTS.maxFrameDimension,
    ),
    streamStallTimeoutS: String(
      stored.stallTimeoutS ?? STREAM_DEFAULTS.stallTimeoutS,
    ),
    streamUsername: "",
    streamPassword: "",
    streamUrlSecret: "",
    streamClearCredentials: false,
  };
}

function numberOr(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isInteger(parsed) ? parsed : fallback;
}

/** The request's `streamSettings`; transport and latency for RTSP only. */
export function buildStreamSettings(
  type: ImageSourceType,
  values: StreamFormValues,
): StreamSettings {
  const settings: StreamSettings = {
    decoder: (values.streamDecoder ??
      STREAM_DEFAULTS.decoder) as StreamSettings["decoder"],
    maxFrameDimension: numberOr(
      values.streamMaxFrameDimension,
      STREAM_DEFAULTS.maxFrameDimension,
    ),
    stallTimeoutS: numberOr(
      values.streamStallTimeoutS,
      STREAM_DEFAULTS.stallTimeoutS,
    ),
  };
  if (type === ImageSourceType.RTSP) {
    settings.transport = (values.streamTransport ??
      STREAM_DEFAULTS.transport) as StreamSettings["transport"];
    settings.latencyMs = numberOr(
      values.streamLatencyMs,
      STREAM_DEFAULTS.latencyMs,
    );
  }
  return settings;
}

/**
 * The request's write-only `credentials`: the filled-in fields, or undefined
 * when every field is blank (which keeps what the device stores). Entering
 * any field replaces the stored credentials as a whole.
 */
export function buildStreamCredentials(
  values: StreamFormValues,
): StreamCredentials | undefined {
  const credentials: StreamCredentials = {};
  if (values.streamUsername) credentials.username = values.streamUsername;
  if (values.streamPassword) credentials.password = values.streamPassword;
  if (values.streamUrlSecret) credentials.urlSecret = values.streamUrlSecret;
  return Object.keys(credentials).length > 0 ? credentials : undefined;
}

/**
 * The API's message for a failed request (`{message}` on every LocalServer
 * error, naming the invalid field), else the error's own message.
 */
export function apiErrorMessage(error: unknown): string {
  const response = (error as { response?: { data?: { message?: unknown } } })
    ?.response;
  const message = response?.data?.message;
  if (typeof message === "string" && message.length > 0) {
    return message;
  }
  return error instanceof Error ? error.message : String(error);
}

/** The edit form values a stream camera update is built from. */
export interface StreamEditValues extends StreamFormValues {
  type?: ImageSourceType;
  editName?: string;
  editDescription?: string;
}

/** What the edit form compares against: the stored camera. */
interface StoredStreamSource {
  name?: string;
  description?: string;
  location?: string;
  type?: ImageSourceType;
}

/**
 * The PATCH body of a stream camera edit: changed name, description and URL
 * only, every setting (they merge over the stored ones), credentials only
 * when a field was filled in, and `clearCredentials` when asked.
 */
export function buildStreamEdit(
  values: StreamEditValues,
  stored?: StoredStreamSource,
): {
  name?: string;
  description?: string;
  location?: string;
  streamSettings: StreamSettings;
  credentials?: StreamCredentials;
  clearCredentials?: boolean;
} {
  const type = (values.type ?? stored?.type ?? ImageSourceType.RTSP) as ImageSourceType;
  const location = (values.streamUrl ?? "").trim();
  const credentials = buildStreamCredentials(values);
  return {
    ...(values.editName !== stored?.name && { name: values.editName }),
    ...(values.editDescription !== stored?.description && {
      description: values.editDescription,
    }),
    ...(location !== stored?.location && { location }),
    streamSettings: buildStreamSettings(type, values),
    ...(credentials && { credentials }),
    ...(!credentials && values.streamClearCredentials && { clearCredentials: true }),
  };
}
