/*
 * The stream camera sections of an Image_Source's details
 * (rtsp-rtmp-stream-cameras Requirements 4.3, 4.4, 16.1): the health and
 * Test connection panel, then the preview and capture with its state
 * overlay while the camera is not streaming.
 *
 * The APIs are mocked at the module boundary, and InteractableImage is a
 * plain image that renders its actions.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as ImageAPI from "api/ImageAPI";
import * as ImageSourceAPI from "api/ImageSourceAPI";
import { ImageSource, ImageSourceType, StreamHealth } from "components/image-source/types";
import StreamCameraPanel from "./StreamCameraPanel";

jest.mock("api/ImageAPI");
jest.mock("api/ImageSourceAPI");
jest.mock("components/live-result/InteractableImage", () => ({
  __esModule: true,
  default: (props: { imageSrc: string; alt?: string; extraActions?: JSX.Element }): JSX.Element => (
    <div data-testid="interactable-image">
      <img src={props.imageSrc} alt={props.alt} />
      {props.extraActions ?? null}
    </div>
  ),
}));

const IMAGE_SOURCE_ID = "src-1";
const CAPTURE_PATH = "/aws_dda/image-capture/src-1";

const STREAMING: StreamHealth = {
  state: "streaming",
  codec: "h264",
  width: 1280,
  height: 720,
  sourceFps: 15,
  decoder: "software",
  reconnects: 0,
  leases: 1,
};

function imageSource(overrides: Partial<ImageSource> = {}): ImageSource {
  return {
    imageSourceId: IMAGE_SOURCE_ID,
    name: "dock-cam",
    type: ImageSourceType.RTSP,
    location: "rtsp://192.168.1.64/live",
    imageCapturePath: CAPTURE_PATH,
    imageSourceConfiguration: {},
    creationTime: 1700000000000,
    lastUpdateTime: 1700000000000,
    streamHealth: STREAMING,
    credentialsConfigured: false,
    ...overrides,
  } as ImageSource;
}

function renderPanel(source: ImageSource): void {
  const queryClient = new QueryClient({
    // staleTime as in the app (createQueryClient), so data refreshes only on
    // the panel's own intervals; no garbage-collection timers outlive a test.
    defaultOptions: {
      queries: { retry: false, cacheTime: Infinity, staleTime: Infinity },
      mutations: { retry: false, cacheTime: Infinity },
    },
    logger: { log: () => undefined, warn: () => undefined, error: () => undefined },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <StreamCameraPanel imageSource={source} />
    </QueryClientProvider>,
  );
}

function withHealth(health: StreamHealth): void {
  (ImageSourceAPI.getStreamHealth as jest.Mock).mockResolvedValue(health);
}

beforeEach(() => {
  withHealth(STREAMING);
  (ImageAPI.previewImage as jest.Mock).mockResolvedValue({ image: "ZnJhbWU=" });
  (ImageAPI.captureImage as jest.Mock).mockResolvedValue(undefined);
});

describe("the stream camera panel", () => {
  it("shows the Stream_URL, the credentials flag and the health (16.1)", async () => {
    renderPanel(imageSource({ credentialsConfigured: true }));
    expect(screen.getByText("rtsp://192.168.1.64/live")).toBeInTheDocument();
    expect(screen.getByText("Stored on this device")).toBeInTheDocument();
    expect(screen.getByText("Streaming")).toBeInTheDocument();
    expect(screen.getByText("H264")).toBeInTheDocument();
    expect(screen.getByText("1280 × 720")).toBeInTheDocument();
  });

  it("reports a successful connection test with the first frame (4.3)", async () => {
    (ImageSourceAPI.testStreamConnection as jest.Mock).mockResolvedValue({
      ok: true,
      category: null,
      message: "The camera is streaming.",
      streamHealth: { ...STREAMING, codec: "h265", width: 1920, height: 1080 },
      image: "Zmlyc3Q=",
    });
    renderPanel(imageSource());
    userEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(
      await screen.findByText("Connected: the camera is streaming"),
    ).toBeInTheDocument();
    expect(ImageSourceAPI.testStreamConnection).toHaveBeenCalledWith(IMAGE_SOURCE_ID);
    // The result card reports what the test detected (the summary above it
    // shows the session's own health).
    expect(screen.getByText("H265")).toBeInTheDocument();
    expect(screen.getByText("1920 × 1080")).toBeInTheDocument();
    expect(
      screen.getByAltText("The first frame received from the camera"),
    ).toHaveAttribute("src", "data:image/jpg;base64, Zmlyc3Q=");
  });

  it("reports a failed connection test with its category and redacted detail (4.3)", async () => {
    (ImageSourceAPI.testStreamConnection as jest.Mock).mockResolvedValue({
      ok: false,
      category: "authentication_failed",
      message: "The camera rejected the credentials (401 Unauthorized).",
      streamHealth: { state: "failed" },
    });
    renderPanel(imageSource());
    userEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(
      await screen.findByText("Connection failed: Authentication failed"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("The camera rejected the credentials (401 Unauthorized)."),
    ).toBeInTheDocument();
  });

  it("shows the API's message when the test cannot run", async () => {
    (ImageSourceAPI.testStreamConnection as jest.Mock).mockRejectedValue(
      Object.assign(new Error("Request failed with status code 400"), {
        response: { data: { message: "The image source src-1 is not a stream camera." } },
      }),
    );
    renderPanel(imageSource());
    userEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("The connection test could not run")).toBeInTheDocument();
    expect(screen.getByText("The image source src-1 is not a stream camera.")).toBeInTheDocument();
  });
});

describe("the stream preview and capture", () => {
  it("previews the camera and captures a frame while it streams (4.4)", async () => {
    renderPanel(imageSource());
    const frame = await screen.findByAltText("The latest frame from the camera");
    expect(frame).toHaveAttribute("src", "data:image/jpg;base64, ZnJhbWU=");
    expect(ImageAPI.previewImage).toHaveBeenCalledWith(IMAGE_SOURCE_ID);
    expect(screen.getByText(CAPTURE_PATH)).toBeInTheDocument();

    userEvent.type(screen.getByLabelText("File prefix"), "line1");
    userEvent.click(screen.getByRole("button", { name: "Capture image" }));

    await waitFor(() =>
      expect(ImageAPI.captureImage).toHaveBeenCalledWith(IMAGE_SOURCE_ID, "line1"),
    );
    expect(
      await screen.findByText(`Captured an image to ${CAPTURE_PATH}.`),
    ).toBeInTheDocument();
  });

  it("captures without a prefix when none is entered", async () => {
    renderPanel(imageSource());
    await screen.findByAltText("The latest frame from the camera");
    userEvent.click(screen.getByRole("button", { name: "Capture image" }));
    await waitFor(() =>
      expect(ImageAPI.captureImage).toHaveBeenCalledWith(IMAGE_SOURCE_ID, undefined),
    );
  });

  it("shows the API's reason when a capture fails", async () => {
    (ImageAPI.captureImage as jest.Mock).mockRejectedValue(
      Object.assign(new Error("Request failed with status code 503"), {
        response: { data: { message: "The stream camera has no frame to show (state reconnecting)." } },
      }),
    );
    renderPanel(imageSource());
    await screen.findByAltText("The latest frame from the camera");
    userEvent.click(screen.getByRole("button", { name: "Capture image" }));
    expect(await screen.findByText("The image could not be captured")).toBeInTheDocument();
    expect(
      screen.getByText("The stream camera has no frame to show (state reconnecting)."),
    ).toBeInTheDocument();
  });

  it("shows the session state in place of the preview before a first frame (16.1)", async () => {
    const failed: StreamHealth = {
      state: "failed",
      lastError: { category: "authentication_failed", message: "401 Unauthorized" },
      nextAttemptInS: 290,
    };
    withHealth(failed);
    (ImageAPI.previewImage as jest.Mock).mockRejectedValue(
      Object.assign(new Error("Request failed with status code 503"), {
        response: { data: { message: "The stream camera has no frame to show (state failed)." } },
      }),
    );
    renderPanel(imageSource({ streamHealth: failed }));
    expect(
      await screen.findAllByText("401 Unauthorized; next attempt in 290 s"),
    ).not.toHaveLength(0);
    // The health summary and the preview both show the state.
    expect(screen.getAllByText("Failed").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByTestId("interactable-image")).toBeNull();
    expect(screen.queryByRole("button", { name: "Capture image" })).toBeNull();
  });

  it("marks the last frame stale while the camera reconnects, and holds capture (16.1)", async () => {
    // The shared session still holds the frame from before the outage.
    const reconnecting: StreamHealth = {
      state: "reconnecting",
      lastError: { category: "stall", message: "No frame for 10 s" },
      nextAttemptInS: 2,
    };
    withHealth(reconnecting);
    renderPanel(imageSource({ streamHealth: reconnecting }));
    await screen.findByAltText("The latest frame from the camera");
    expect(
      screen.getByText("The camera is not streaming: Reconnecting"),
    ).toBeInTheDocument();
    expect(screen.getByText(/The preview shows the last frame received\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Capture image" })).toBeDisabled();
    expect(
      screen.getByText("Capture is available while the camera is streaming."),
    ).toBeInTheDocument();
  });

  it("follows the session closely while the preview waits for the camera (16.1)", async () => {
    // The page opened on a connecting camera; the device reports streaming next.
    renderPanel(imageSource({ streamHealth: { state: "connecting" } }));
    expect(screen.getAllByText(/Connecting/).length).toBeGreaterThan(0);
    // Well within the 5 s refresh of a streaming camera.
    expect(await screen.findByText("Streaming", {}, { timeout: 2500 })).toBeInTheDocument();
    expect(screen.queryByText(/The camera is not streaming/)).toBeNull();
  });

  it("stops polling when live preview is off and refreshes on demand", async () => {
    renderPanel(imageSource());
    await screen.findByAltText("The latest frame from the camera");
    userEvent.click(screen.getByRole("checkbox", { name: "Live preview" }));
    const refresh = await screen.findByRole("button", { name: "Refresh preview" });
    const callsBefore = (ImageAPI.previewImage as jest.Mock).mock.calls.length;
    userEvent.click(refresh);
    await waitFor(() =>
      expect((ImageAPI.previewImage as jest.Mock).mock.calls.length).toBe(callsBefore + 1),
    );
    // Off means no background refresh (the preview otherwise polls every 500 ms).
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1200));
    });
    expect((ImageAPI.previewImage as jest.Mock).mock.calls.length).toBe(callsBefore + 1);
  });
});
