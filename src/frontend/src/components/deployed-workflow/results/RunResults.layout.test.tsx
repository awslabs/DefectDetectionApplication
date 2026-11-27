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
/**
 * car-assembly-check Requirement 7.1: the run page shows a section per
 * Layout Check node after the scene-analytics sections, and hands the
 * instance layer the flagged keys (others dimmed) and the outlines of the
 * pieces that are not ok when a check fails, and the ignored keys whatever
 * the verdict. A run without a `layout` section is unchanged.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type {
  WorkflowExecutionOverlay,
  WorkflowExecutionResults,
} from "api/WorkflowRegistrationAPI";
import RunResults from "./RunResults";

const mockLayerProps: Array<Record<string, unknown>> = [];

jest.mock("api/WorkflowRegistrationAPI");
jest.mock("components/auth/authHook", () => ({
  __esModule: true,
  default: (): { token: string; authEnabled: boolean } => ({ token: "", authEnabled: false }),
}));
jest.mock("components/live-result/InteractableImage", () => ({
  __esModule: true,
  default: (): JSX.Element => <div data-testid="interactable-image" />,
}));
jest.mock("./InstanceMaskLayer", () => {
  const actual = jest.requireActual("./InstanceMaskLayer");
  return {
    __esModule: true,
    ...actual,
    default: (props: Record<string, unknown>): JSX.Element => {
      mockLayerProps.push(props);
      return <div data-testid="instance-mask-layer-stub" />;
    },
  };
});

const EXECUTION_ID = "exec-layout-1";
const RESULTS: WorkflowExecutionResults = {
  hasImageResults: true,
  captureId: "cap-1",
  images: [{ kind: "output", hasOverlay: true, hasOverlayImage: true }],
};
const OVERLAY: WorkflowExecutionOverlay = {
  maskImage: "bWFzay1iYXNlNjQ=",
  maskBackground: { "class-name": "background", "rgb-color": [255, 255, 255], "total-percentage-area": 0.9 },
  instances: [
    { class_index: "1", class_label: "rear_fender", bounding_box: [10, 20, 30, 40], confidence: 0.95, mask_rle: "0 100" },
    { class_index: "1", class_label: "rear_fender", bounding_box: [0, 50, 5, 60], confidence: 0.81, mask_rle: "0 100" },
    { class_index: "0", class_label: "front_fender", bounding_box: [50, 20, 90, 40], confidence: 0.97, mask_rle: "0 100" },
  ],
  detectionsTruncated: false,
};

function layoutSection(pass: boolean) {
  return {
    pass,
    pieces: {
      front_fender: {
        status: "ok",
        iou: 0.91,
        detections: [{ id: "f1", class_label: "front_fender", confidence: 0.97, bounding_box: [50, 20, 90, 40], iou: 0.91, outline_overlap: 1 }],
      },
      rear_fender: {
        status: pass ? "ok" : "misplaced",
        iou: pass ? 0.9 : 0.12,
        detections: [{ id: "r1", class_label: "rear_fender", confidence: 0.95, bounding_box: [10, 20, 30, 40], iou: pass ? 0.9 : 0.12, outline_overlap: 0.8 }],
      },
      roof: pass
        ? { status: "ok", iou: 0.8, detections: [{ id: "t1", class_label: "roof", confidence: 0.9, bounding_box: [30, 0, 70, 15], iou: 0.8, outline_overlap: 1 }] }
        : { status: "missing", iou: null, detections: [] },
    },
    flagged: pass ? [] : ["r1"],
    ignored: [{ id: "s1", class_label: "rear_fender", confidence: 0.81, bounding_box: [0, 50, 5, 60], outline_overlap: 0 }],
    unexpected_classes: [],
    expected_regions: {
      front_fender: [[0.5, 0.2], [0.9, 0.2], [0.9, 0.4]],
      rear_fender: [[0.1, 0.2], [0.3, 0.2], [0.3, 0.4]],
      roof: [[0.3, 0.0], [0.7, 0.0], [0.7, 0.15]],
    },
    frame: { width: 100, height: 100 },
    min_iou: 0.7,
    min_confidence: 0.5,
    min_outline_overlap: 0.1,
  };
}

const COUNTER = { count_1: { counts: { rear_fender: 2 }, total: 2, labels: { rear_fender: "rear_fender" } } };

function renderResults(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    logger: { log: () => undefined, warn: () => undefined, error: () => undefined },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/deployed-workflows/reg-1/executions/${EXECUTION_ID}/results`]}>
        <Routes>
          <Route
            path="/deployed-workflows/:registrationId/executions/:executionId/results"
            element={<RunResults />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function lastLayerProps(): Record<string, unknown> {
  return mockLayerProps[mockLayerProps.length - 1];
}

beforeEach(() => {
  jest.clearAllMocks();
  mockLayerProps.length = 0;
  (RegistrationAPI.workflowExecutionOutputImageUrl as jest.Mock).mockImplementation(
    (id: string) => `/output-image/${id}`,
  );
  (RegistrationAPI.workflowExecutionOverlayImageUrl as jest.Mock).mockImplementation(
    (id: string) => `/overlay-image/${id}`,
  );
  (RegistrationAPI.getWorkflowExecutionResults as jest.Mock).mockResolvedValue(RESULTS);
  (RegistrationAPI.getWorkflowExecutionOverlay as jest.Mock).mockResolvedValue(OVERLAY);
  (RegistrationAPI.getWorkflowExecutionNodeStatus as jest.Mock).mockResolvedValue({
    lay: { status: "success", detail: "FAIL: rear_fender misplaced (IoU 0.12), roof missing" },
  });
});

describe("RunResults with a failing Layout Check", () => {
  it("shows the section after the analytics and highlights the flagged instance", async () => {
    (RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock).mockResolvedValue({
      counter: COUNTER,
      layout: { lay: layoutSection(false) },
    });
    renderResults();
    const section = await screen.findByTestId("layout-section-lay");
    const counter = screen.getByTestId("counter-section-count_1");
    expect(counter.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await screen.findByText("FAIL: rear_fender misplaced (IoU 0.12), roof missing");
    await waitFor(() => {
      expect(lastLayerProps()?.highlight).toBeDefined();
    });
    const props = lastLayerProps();
    const highlight = props.highlight as { keys: Set<string>; dimOthers: boolean };
    expect(Array.from(highlight.keys)).toEqual(["rear_fender|10.0|20.0|30.0|40.0"]);
    expect(highlight.dimOthers).toBe(true);
    const outlines = props.outlines as { label: string; points: [number, number][] }[];
    expect(outlines.map((outline) => outline.label)).toEqual(["rear_fender", "roof"]);
    expect(outlines[0].points[0][0]).toBeCloseTo(10);
    expect(outlines[0].points[0][1]).toBeCloseTo(20);
    expect(Array.from(props.muted as Set<string>)).toEqual(["rear_fender|0.0|50.0|5.0|60.0"]);
    expect(RegistrationAPI.getWorkflowExecutionNodeStatus).toHaveBeenCalled();
  });
});

describe("RunResults with a passing Layout Check", () => {
  it("mutes the ignored instance without a highlight or outlines", async () => {
    (RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock).mockResolvedValue({
      layout: { lay: layoutSection(true) },
    });
    renderResults();
    await screen.findByTestId("layout-section-lay");
    await waitFor(() => {
      expect(lastLayerProps()?.muted).toBeDefined();
    });
    const props = lastLayerProps();
    expect(props.highlight).toBeUndefined();
    expect(props.outlines).toBeUndefined();
    expect(Array.from(props.muted as Set<string>)).toEqual(["rear_fender|0.0|50.0|5.0|60.0"]);
    expect(screen.getByText("PASS", { selector: "span" })).toBeInTheDocument();
  });
});

describe("RunResults without a layout section", () => {
  it("passes the instance layer only its usual props and shows no layout section", async () => {
    (RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock).mockResolvedValue({});
    renderResults();
    await screen.findByTestId("instance-mask-layer-stub");
    await waitFor(() => {
      expect(RegistrationAPI.getWorkflowExecutionMetadata).toHaveBeenCalled();
    });
    expect(Object.keys(lastLayerProps()).sort()).toEqual(["imageSrc", "instances"]);
    expect(screen.queryByTestId("layout-section-lay")).toBeNull();
    expect(RegistrationAPI.getWorkflowExecutionNodeStatus).not.toHaveBeenCalled();
  });
});
