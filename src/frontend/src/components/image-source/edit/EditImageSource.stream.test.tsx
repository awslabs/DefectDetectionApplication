/*
 * Editing an RTSP or RTMP camera (rtsp-rtmp-stream-cameras Requirement 4.1):
 * the stored settings fill the form, blank credential fields keep the stored
 * credentials, and the remove option clears them.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import * as ImageSourceAPI from "api/ImageSourceAPI";
import { ImageSource, ImageSourceType } from "components/image-source/types";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import EditImageSource from "./EditImageSource";

jest.mock("api/ImageSourceAPI");

const IMAGE_SOURCE_ID = "src-1";
const RTSP_URL = "rtsp://192.168.1.64/live";
const STORED_SETTINGS = {
  transport: "udp",
  latencyMs: 100,
  decoder: "software",
  maxFrameDimension: 1280,
  stallTimeoutS: 20,
} as const;

function streamSource(overrides: Partial<ImageSource> = {}): ImageSource {
  return {
    imageSourceId: IMAGE_SOURCE_ID,
    name: "dock-cam",
    description: "Dock door",
    type: ImageSourceType.RTSP,
    location: RTSP_URL,
    imageCapturePath: "/aws_dda/image-capture/src-1",
    imageSourceConfiguration: { streamSettings: { ...STORED_SETTINGS } },
    creationTime: 1700000000000,
    lastUpdateTime: 1700000000000,
    credentialsConfigured: true,
    ...overrides,
  } as ImageSource;
}

function renderEdit(): void {
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
      <MemoryRouter initialEntries={[`/image-sources/${IMAGE_SOURCE_ID}/edit`]}>
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
            addSuccess: jest.fn(),
            addError: jest.fn(),
          }}
        >
          <Routes>
            <Route path="/image-sources/:imageSourceId/edit" element={<EditImageSource />} />
            <Route path="/image-sources/:imageSourceId" element={<div>Image source details</div>} />
          </Routes>
        </AppLayoutContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function loaded(): Promise<void> {
  await waitFor(() => expect(screen.getByLabelText("Stream URL")).toHaveValue(RTSP_URL));
}

beforeEach(() => {
  (ImageSourceAPI.getImageSource as jest.Mock).mockResolvedValue(streamSource());
  (ImageSourceAPI.editImageSource as jest.Mock).mockResolvedValue({ imageSourceId: IMAGE_SOURCE_ID });
});

describe("editing a stream camera", () => {
  it("fills the form with the stored settings and never shows the credentials", async () => {
    renderEdit();
    await loaded();
    expect(screen.getByLabelText("Latency (ms)")).toHaveValue("100");
    expect(screen.getByLabelText("Maximum frame dimension (pixels)")).toHaveValue("1280");
    expect(screen.getByLabelText("Stall timeout (seconds)")).toHaveValue("20");
    expect(screen.getByRole("radio", { name: "UDP" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Software only" })).toBeChecked();
    expect(screen.getByLabelText("Username")).toHaveValue("");
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(
      screen.getByText(/Leave all fields blank to keep them; entering any field replaces them\./),
    ).toBeInTheDocument();
  });

  it("keeps the stored credentials when the fields are left blank", async () => {
    renderEdit();
    await loaded();
    userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(ImageSourceAPI.editImageSource).toHaveBeenCalledTimes(1));
    expect(ImageSourceAPI.editImageSource).toHaveBeenCalledWith(IMAGE_SOURCE_ID, {
      streamSettings: { ...STORED_SETTINGS },
    });
  });

  it("replaces the credentials and settings that were changed", async () => {
    renderEdit();
    await loaded();
    const latency = screen.getByLabelText("Latency (ms)");
    userEvent.clear(latency);
    userEvent.type(latency, "300");
    userEvent.type(screen.getByLabelText("Password"), "new-pw");
    userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(ImageSourceAPI.editImageSource).toHaveBeenCalledTimes(1));
    expect(ImageSourceAPI.editImageSource).toHaveBeenCalledWith(IMAGE_SOURCE_ID, {
      streamSettings: { ...STORED_SETTINGS, latencyMs: 300 },
      credentials: { password: "new-pw" },
    });
  });

  it("removes the stored credentials when asked", async () => {
    renderEdit();
    await loaded();
    userEvent.click(screen.getByRole("checkbox", { name: /Remove the stored credentials/ }));
    userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(ImageSourceAPI.editImageSource).toHaveBeenCalledTimes(1));
    expect(ImageSourceAPI.editImageSource).toHaveBeenCalledWith(IMAGE_SOURCE_ID, {
      streamSettings: { ...STORED_SETTINGS },
      clearCredentials: true,
    });
  });

  it("offers no remove option when the device holds no credentials", async () => {
    (ImageSourceAPI.getImageSource as jest.Mock).mockResolvedValue(
      streamSource({ credentialsConfigured: false }),
    );
    renderEdit();
    await loaded();
    expect(screen.queryByRole("checkbox", { name: /Remove the stored credentials/ })).toBeNull();
    expect(screen.getByText("Optional. Stored on this device only, and never shown again.")).toBeInTheDocument();
  });
});
