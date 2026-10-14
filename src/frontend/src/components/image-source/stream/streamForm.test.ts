/*
 * The stream Image_Source form rules and request parts
 * (rtsp-rtmp-stream-cameras Requirements 4.1, 4.2): the yup rules the add
 * and edit forms share, and the create/edit payloads built from them.
 */
import { ValidationError } from "yup";
import { ImageSourceType } from "components/image-source/types";
import { schema as addSchema } from "../add/schema";
import { schema as editSchema } from "../edit/schema";
import {
  STREAM_DEFAULTS,
  apiErrorMessage,
  buildStreamCredentials,
  buildStreamEdit,
  buildStreamSettings,
  isStreamType,
  streamFormDefaults,
} from "./streamForm";

const RTSP_URL = "rtsp://192.168.1.64:554/Streaming/Channels/101";
const RTMP_URL = "rtmp://media.local/live/line1";

function addValues(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: ImageSourceType.RTSP,
    streamName: "dock-cam",
    streamDescription: "",
    ...streamFormDefaults(RTSP_URL),
    ...overrides,
  };
}

/** `{path: message}` of every validation error, or `{}` when valid. */
async function errorsOf(
  schema: typeof addSchema | typeof editSchema,
  values: Record<string, unknown>,
): Promise<Record<string, string>> {
  try {
    await schema.validate(values, { abortEarly: false });
    return {};
  } catch (error) {
    if (!(error instanceof ValidationError)) {
      throw error;
    }
    const errors: Record<string, string> = {};
    // The first error of each field, which is the one the form shows.
    for (const inner of error.inner) {
      const path = inner.path ?? "";
      if (!(path in errors)) {
        errors[path] = inner.message;
      }
    }
    return errors;
  }
}

describe("isStreamType", () => {
  it("is true for RTSP and RTMP only", () => {
    expect(isStreamType(ImageSourceType.RTSP)).toBe(true);
    expect(isStreamType(ImageSourceType.RTMP)).toBe(true);
    for (const other of [
      ImageSourceType.Camera,
      ImageSourceType.Folder,
      ImageSourceType.ICam,
      ImageSourceType.NvidiaCSI,
      undefined,
      "rtsp",
      7,
    ]) {
      expect(isStreamType(other)).toBe(false);
    }
  });
});

describe("the stream form rules (4.1, 4.2)", () => {
  it("accepts an RTSP camera with the defaults", async () => {
    expect(await errorsOf(addSchema, addValues())).toEqual({});
  });

  it("accepts an RTMP stream, whose transport and latency are not validated", async () => {
    expect(
      await errorsOf(
        addSchema,
        addValues({
          type: ImageSourceType.RTMP,
          streamUrl: RTMP_URL,
          streamTransport: "carrier-pigeon",
          streamLatencyMs: "99999",
        }),
      ),
    ).toEqual({});
  });

  it("requires a name", async () => {
    expect((await errorsOf(addSchema, addValues({ streamName: "" }))).streamName).toBe(
      "An image source name is required.",
    );
  });

  it("rejects credentials embedded in the URL with the Stream_URL message", async () => {
    const errors = await errorsOf(
      addSchema,
      addValues({ streamUrl: "rtsp://admin:secret@192.168.1.64/stream" }),
    );
    expect(errors.streamUrl).toMatch(/must not contain embedded user information/);
    // The message never echoes the secret.
    expect(errors.streamUrl).not.toContain("secret");
  });

  it("rejects a scheme of the other stream type", async () => {
    const errors = await errorsOf(
      addSchema,
      addValues({ type: ImageSourceType.RTMP, streamUrl: RTSP_URL }),
    );
    expect(errors.streamUrl).toMatch(/scheme 'rtsp' is not allowed here/);
  });

  it("requires a URL", async () => {
    expect((await errorsOf(addSchema, addValues({ streamUrl: "" }))).streamUrl).toMatch(
      /Stream URL is required/,
    );
  });

  it.each([
    ["streamLatencyMs", "-1", "Latency must be from 0 to 5000."],
    ["streamLatencyMs", "5001", "Latency must be from 0 to 5000."],
    ["streamLatencyMs", "1.5", "Latency must be a whole number."],
    ["streamLatencyMs", "fast", "Latency must be a whole number."],
    ["streamMaxFrameDimension", "319", "Maximum frame dimension must be from 320 to 4096."],
    ["streamMaxFrameDimension", "4097", "Maximum frame dimension must be from 320 to 4096."],
    ["streamStallTimeoutS", "1", "Stall timeout must be from 2 to 60."],
    ["streamStallTimeoutS", "61", "Stall timeout must be from 2 to 60."],
  ])("rejects %s = %s with the field's range", async (field, value, message) => {
    expect((await errorsOf(addSchema, addValues({ [field]: value })))[field]).toBe(message);
  });

  it.each([
    ["streamLatencyMs", "0"],
    ["streamLatencyMs", "5000"],
    ["streamMaxFrameDimension", "320"],
    ["streamMaxFrameDimension", "4096"],
    ["streamStallTimeoutS", "2"],
    ["streamStallTimeoutS", "60"],
  ])("accepts the bound %s = %s", async (field, value) => {
    expect(await errorsOf(addSchema, addValues({ [field]: value }))).toEqual({});
  });

  it("rejects an unknown transport and decoder policy", async () => {
    const errors = await errorsOf(
      addSchema,
      addValues({ streamTransport: "sctp", streamDecoder: "gpu" }),
    );
    expect(errors.streamTransport).toBe("Choose a transport.");
    expect(errors.streamDecoder).toBe("Choose a decoder policy.");
  });

  it("rejects control characters in a credential field", async () => {
    const errors = await errorsOf(
      addSchema,
      addValues({ streamPassword: "pass\nword" }),
    );
    expect(errors.streamPassword).toBe(
      "The password must not contain control characters.",
    );
  });

  it("ignores the stream fields for the other image source types", async () => {
    expect(
      await errorsOf(addSchema, {
        type: ImageSourceType.Folder,
        folderName: "folder-a",
        path: "images/",
        streamUrl: "not a url",
        streamLatencyMs: "",
        streamMaxFrameDimension: "",
        streamStallTimeoutS: "",
      }),
    ).toEqual({});
  });

  it("applies the same rules in the edit form", async () => {
    const errors = await errorsOf(editSchema, {
      type: ImageSourceType.RTSP,
      editName: "dock-cam",
      editDescription: "",
      ...streamFormDefaults("rtsp://user:pw@10.0.0.2/live"),
      streamStallTimeoutS: "0",
    });
    expect(Object.keys(errors).sort()).toEqual(["streamStallTimeoutS", "streamUrl"]);
  });
});

describe("streamFormDefaults", () => {
  it("starts from the device defaults with empty credentials", () => {
    expect(streamFormDefaults()).toEqual({
      streamUrl: "",
      streamTransport: STREAM_DEFAULTS.transport,
      streamLatencyMs: "200",
      streamDecoder: "auto",
      streamMaxFrameDimension: "1920",
      streamStallTimeoutS: "10",
      streamUsername: "",
      streamPassword: "",
      streamUrlSecret: "",
      streamClearCredentials: false,
    });
  });

  it("starts from the stored settings when editing", () => {
    expect(
      streamFormDefaults(RTSP_URL, {
        transport: "udp",
        latencyMs: 0,
        decoder: "software",
        maxFrameDimension: 1280,
        stallTimeoutS: 30,
      }),
    ).toMatchObject({
      streamUrl: RTSP_URL,
      streamTransport: "udp",
      streamLatencyMs: "0",
      streamDecoder: "software",
      streamMaxFrameDimension: "1280",
      streamStallTimeoutS: "30",
    });
  });
});

describe("buildStreamSettings", () => {
  it("sends transport and latency for RTSP", () => {
    expect(
      buildStreamSettings(ImageSourceType.RTSP, {
        streamTransport: "udp",
        streamLatencyMs: "150",
        streamDecoder: "hardware",
        streamMaxFrameDimension: "1280",
        streamStallTimeoutS: "5",
      }),
    ).toEqual({
      transport: "udp",
      latencyMs: 150,
      decoder: "hardware",
      maxFrameDimension: 1280,
      stallTimeoutS: 5,
    });
  });

  it("leaves transport and latency out for RTMP", () => {
    expect(
      buildStreamSettings(ImageSourceType.RTMP, {
        streamTransport: "udp",
        streamLatencyMs: 150,
        streamDecoder: "auto",
        streamMaxFrameDimension: 1920,
        streamStallTimeoutS: 10,
      }),
    ).toEqual({ decoder: "auto", maxFrameDimension: 1920, stallTimeoutS: 10 });
  });

  it("falls back to the defaults for missing numbers", () => {
    expect(buildStreamSettings(ImageSourceType.RTSP, {})).toEqual({
      transport: "tcp",
      latencyMs: 200,
      decoder: "auto",
      maxFrameDimension: 1920,
      stallTimeoutS: 10,
    });
  });
});

describe("buildStreamCredentials", () => {
  it("is undefined when every field is blank, which keeps the stored ones", () => {
    expect(
      buildStreamCredentials({ streamUsername: "", streamPassword: "", streamUrlSecret: "" }),
    ).toBeUndefined();
    expect(buildStreamCredentials({})).toBeUndefined();
  });

  it("carries only the fields that were filled in", () => {
    expect(
      buildStreamCredentials({ streamUsername: "viewer", streamPassword: "pw", streamUrlSecret: "" }),
    ).toEqual({ username: "viewer", password: "pw" });
    expect(buildStreamCredentials({ streamUrlSecret: "key-1" })).toEqual({ urlSecret: "key-1" });
  });
});

describe("buildStreamEdit", () => {
  const stored = {
    name: "dock-cam",
    description: "Dock door",
    location: RTSP_URL,
    type: ImageSourceType.RTSP,
  };
  const unchanged = {
    type: ImageSourceType.RTSP,
    editName: "dock-cam",
    editDescription: "Dock door",
    ...streamFormDefaults(RTSP_URL),
  };

  it("sends only the settings when nothing else changed", () => {
    expect(buildStreamEdit(unchanged, stored)).toEqual({
      streamSettings: {
        transport: "tcp",
        latencyMs: 200,
        decoder: "auto",
        maxFrameDimension: 1920,
        stallTimeoutS: 10,
      },
    });
  });

  it("sends the changed name, description and trimmed URL", () => {
    const body = buildStreamEdit(
      {
        ...unchanged,
        editName: "dock-cam-2",
        editDescription: "",
        streamUrl: "  rtsp://192.168.1.65/live  ",
      },
      stored,
    );
    expect(body).toMatchObject({
      name: "dock-cam-2",
      description: "",
      location: "rtsp://192.168.1.65/live",
    });
  });

  it("replaces the credentials when any field is filled in", () => {
    const body = buildStreamEdit(
      { ...unchanged, streamPassword: "new-pw", streamClearCredentials: true },
      stored,
    );
    expect(body.credentials).toEqual({ password: "new-pw" });
    // New credentials win over the remove checkbox.
    expect(body.clearCredentials).toBeUndefined();
  });

  it("removes the stored credentials only when asked and none are entered", () => {
    expect(
      buildStreamEdit({ ...unchanged, streamClearCredentials: true }, stored).clearCredentials,
    ).toBe(true);
    expect(buildStreamEdit(unchanged, stored)).not.toHaveProperty("clearCredentials");
    expect(buildStreamEdit(unchanged, stored)).not.toHaveProperty("credentials");
  });
});

describe("apiErrorMessage", () => {
  it("prefers the API's message, which names the invalid field", () => {
    const error = Object.assign(new Error("Request failed with status code 400"), {
      response: { data: { message: "Stream settings 'latencyMs' must be from 0 to 5000." } },
    });
    expect(apiErrorMessage(error)).toBe("Stream settings 'latencyMs' must be from 0 to 5000.");
  });

  it("falls back to the error's own message", () => {
    expect(apiErrorMessage(new Error("Network Error"))).toBe("Network Error");
    const emptyMessage = Object.assign(new Error("Request failed with status code 500"), {
      response: { data: { message: "" } },
    });
    expect(apiErrorMessage(emptyMessage)).toBe("Request failed with status code 500");
    expect(apiErrorMessage("boom")).toBe("boom");
  });
});
