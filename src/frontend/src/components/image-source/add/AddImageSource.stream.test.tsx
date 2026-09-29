/*
 * Adding an RTSP or RTMP camera (rtsp-rtmp-stream-cameras Requirements 4.1,
 * 4.2): the stream fields per type, the create request, and the rejection
 * of an invalid Stream_URL before and after it reaches the API.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import * as CameraAPI from "api/CameraAPI";
import * as ImageSourceAPI from "api/ImageSourceAPI";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import AddImageSource from "./AddImageSource";

jest.mock("api/CameraAPI");
jest.mock("api/ImageSourceAPI");

const RTSP_URL = "rtsp://192.168.1.64:554/Streaming/Channels/101";
const RTMP_URL = "rtmp://media.local/live/line1";

function renderAdd(): { addError: jest.Mock; addSuccess: jest.Mock } {
  const addError = jest.fn();
  const addSuccess = jest.fn();
  const queryClient = new QueryClient({
    // No garbage-collection timers outlive a test.
    defaultOptions: {
      queries: { retry: false, cacheTime: Infinity },
      mutations: { retry: false, cacheTime: Infinity },
    },
    logger: { log: () => undefined, warn: () => undefined, error: () => undefined },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/image-sources/add"]}>
        <AppLayoutContext.Provider
          value={{
            openNavigation: false,
            setOpenNavigation: jest.fn(),
            notifications: [],
            tablesInfo: {},
            tablesPref: {},
            setTableInfo: jest.fn(),
            setTableTypePref: jest.fn(),
            addNotification: jest.fn(),
            removeNotification: jest.fn(),
            addSuccess,
            addError,
          }}
        >
          <Routes>
            <Route path="/image-sources/add" element={<AddImageSource />} />
            <Route path="/image-sources/:imageSourceId" element={<div>Image source details</div>} />
          </Routes>
        </AppLayoutContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { addError, addSuccess };
}

/** Wait for camera discovery, which resets the form when it lands. */
async function chooseType(label: string): Promise<void> {
  await waitFor(() => expect(CameraAPI.listCameras).toHaveBeenCalled());
  await screen.findAllByText("No cameras discovered");
  userEvent.click(screen.getByRole("radio", { name: label }));
  await screen.findByLabelText("Stream URL");
}

beforeEach(() => {
  (CameraAPI.listCameras as jest.Mock).mockResolvedValue([]);
  (ImageSourceAPI.createImageSource as jest.Mock).mockResolvedValue({ imageSourceId: "src-9" });
});

describe("adding a stream camera", () => {
  it("creates an RTSP camera with its settings and write-only credentials (4.1)", async () => {
    const { addSuccess } = renderAdd();
    await chooseType("RTSP camera");
    expect(screen.getByText("Transport")).toBeInTheDocument();
    expect(screen.getByLabelText("Latency (ms)")).toHaveValue("200");

    userEvent.type(screen.getByLabelText("Stream URL"), RTSP_URL);
    userEvent.type(screen.getByLabelText("Image source name"), "dock-cam");
    userEvent.type(screen.getByLabelText("Username"), "viewer");
    userEvent.type(screen.getByLabelText("Password"), "pw");
    userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(ImageSourceAPI.createImageSource).toHaveBeenCalledTimes(1));
    expect(ImageSourceAPI.createImageSource).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "RTSP",
        name: "dock-cam",
        location: RTSP_URL,
        streamSettings: {
          transport: "tcp",
          latencyMs: 200,
          decoder: "auto",
          maxFrameDimension: 1920,
          stallTimeoutS: 10,
        },
        credentials: { username: "viewer", password: "pw" },
      }),
    );
    expect(await screen.findByText("Image source details")).toBeInTheDocument();
    expect(addSuccess).toHaveBeenCalledTimes(1);
  });

  it("creates an RTMP stream with a stream key and no RTSP-only settings", async () => {
    renderAdd();
    await chooseType("RTMP stream");
    expect(screen.queryByText("Transport")).toBeNull();
    expect(screen.queryByLabelText("Latency (ms)")).toBeNull();

    userEvent.type(screen.getByLabelText("Stream URL"), RTMP_URL);
    userEvent.type(screen.getByLabelText("Image source name"), "line-1");
    userEvent.type(screen.getByLabelText("Stream key"), "key-1");
    userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(ImageSourceAPI.createImageSource).toHaveBeenCalledTimes(1));
    const request = (ImageSourceAPI.createImageSource as jest.Mock).mock.calls[0][0];
    expect(request).toMatchObject({
      type: "RTMP",
      location: RTMP_URL,
      streamSettings: { decoder: "auto", maxFrameDimension: 1920, stallTimeoutS: 10 },
      credentials: { urlSecret: "key-1" },
    });
    expect(request.streamSettings).not.toHaveProperty("transport");
    expect(request.streamSettings).not.toHaveProperty("latencyMs");
  });

  it("sends no credentials when none are entered", async () => {
    renderAdd();
    await chooseType("RTSP camera");
    userEvent.type(screen.getByLabelText("Stream URL"), RTSP_URL);
    userEvent.type(screen.getByLabelText("Image source name"), "dock-cam");
    userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(ImageSourceAPI.createImageSource).toHaveBeenCalledTimes(1));
    expect(
      (ImageSourceAPI.createImageSource as jest.Mock).mock.calls[0][0],
    ).not.toHaveProperty("credentials");
  });

  it("rejects credentials in the URL and an out-of-range setting before sending (4.2)", async () => {
    renderAdd();
    await chooseType("RTSP camera");
    userEvent.type(screen.getByLabelText("Stream URL"), "rtsp://admin:secret@192.168.1.64/live");
    userEvent.type(screen.getByLabelText("Image source name"), "dock-cam");
    const stall = screen.getByLabelText("Stall timeout (seconds)");
    userEvent.clear(stall);
    userEvent.type(stall, "90");
    userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText(/must not contain embedded user information/),
    ).toBeInTheDocument();
    expect(screen.getByText("Stall timeout must be from 2 to 60.")).toBeInTheDocument();
    expect(ImageSourceAPI.createImageSource).not.toHaveBeenCalled();
  });

  it("surfaces the API's message, which names the field, when the device rejects it (4.2)", async () => {
    (ImageSourceAPI.createImageSource as jest.Mock).mockRejectedValue(
      Object.assign(new Error("Request failed with status code 400"), {
        response: { data: { message: "Stream settings 'decoder': the device has no hardware decoder." } },
      }),
    );
    const { addError } = renderAdd();
    await chooseType("RTSP camera");
    userEvent.type(screen.getByLabelText("Stream URL"), RTSP_URL);
    userEvent.type(screen.getByLabelText("Image source name"), "dock-cam");
    userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(addError).toHaveBeenCalledTimes(1));
    const { container } = render(<div>{addError.mock.calls[0][0].content}</div>);
    expect(container.textContent).toContain(
      "Stream settings 'decoder': the device has no hardware decoder.",
    );
  });
});
