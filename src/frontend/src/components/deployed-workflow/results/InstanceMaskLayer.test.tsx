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
 * rfdetr-seg-support Requirement 10.6, DD14: the instance layer paints the
 * decodable masks under the boxes on a canvas the size of the frame, with a
 * `Masks + boxes | Boxes` toggle. jsdom has no canvas: the 2D context is a
 * recording stub and `ImageData` a plain holder, so the painting is asserted
 * from the calls the layer makes.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import InstanceMaskLayer, {
  BOXES_ONLY_LABEL,
  MASKS_AND_BOXES_LABEL,
  MASK_ALPHA,
  instanceSummary,
  paintInstances,
} from "./InstanceMaskLayer";
import type { InstanceDetectionEntry } from "../instanceDetections";

// A 4x4 frame; two masks (border, a 2x2 centre) from the shared vectors.
const WIDTH = 4;
const HEIGHT = 4;
const BORDER_RLE = "0 5 2 2 2 5"; // border_touching
const CENTRE_RLE = "5 2 2 2 5"; // the 2x2 centre of a 4x4 frame

const INSTANCES: InstanceDetectionEntry[] = [
  { class_index: "0", class_label: "frame", bounding_box: [0, 0, 4, 4], confidence: 0.6, mask_rle: BORDER_RLE, mask_area: 12 },
  { class_index: "1", class_label: "core", bounding_box: [1, 1, 3, 3], confidence: 0.9, mask_rle: CENTRE_RLE, mask_area: 4 },
  { class_index: "2", class_label: "shed", bounding_box: [2, 2, 4, 4], confidence: 0.5, mask_area: 3, mask_truncated: true },
];

interface RecordingContext {
  calls: Array<{ name: string; args: unknown[] }>;
  putImageData: jest.Mock;
  strokeRect: jest.Mock;
  fillText: jest.Mock;
  clearRect: jest.Mock;
  lineWidth: number;
  font: string;
  textBaseline: string;
  strokeStyle: string;
  fillStyle: string;
}

function recordingContext(): RecordingContext {
  const calls: Array<{ name: string; args: unknown[] }> = [];
  const record = (name: string): jest.Mock =>
    jest.fn((...args: unknown[]) => {
      calls.push({ name, args });
    });
  return {
    calls,
    putImageData: record("putImageData"),
    strokeRect: record("strokeRect"),
    fillText: record("fillText"),
    clearRect: record("clearRect"),
    lineWidth: 0,
    font: "",
    textBaseline: "",
    strokeStyle: "",
    fillStyle: "",
  };
}

class FakeImageData {
  constructor(
    public data: Uint8ClampedArray,
    public width: number,
    public height: number,
  ) {}
}

let originalImageData: unknown;
let originalGetContext: typeof HTMLCanvasElement.prototype.getContext;
let context: RecordingContext;

beforeEach(() => {
  originalImageData = (globalThis as Record<string, unknown>).ImageData;
  (globalThis as Record<string, unknown>).ImageData = FakeImageData;
  context = recordingContext();
  originalGetContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = jest.fn(
    () => context as unknown as CanvasRenderingContext2D,
  ) as unknown as typeof HTMLCanvasElement.prototype.getContext;
});

afterEach(() => {
  HTMLCanvasElement.prototype.getContext = originalGetContext;
  if (originalImageData === undefined) {
    delete (globalThis as Record<string, unknown>).ImageData;
  } else {
    (globalThis as Record<string, unknown>).ImageData = originalImageData;
  }
});

function pixel(data: Uint8ClampedArray, x: number, y: number): number[] {
  const offset = (y * WIDTH + x) * 4;
  return Array.from(data.slice(offset, offset + 4));
}

describe("paintInstances", () => {
  it("paints every decodable mask into one translucent layer, more confident on top, then the boxes", () => {
    const ctx = context as unknown as CanvasRenderingContext2D;
    const summary = paintInstances(ctx, INSTANCES, WIDTH, HEIGHT, true);
    expect(summary).toEqual({ masksPainted: 2, masksSkipped: 0, boxesDrawn: 3 });
    expect(context.putImageData).toHaveBeenCalledTimes(1);
    const layer = context.putImageData.mock.calls[0][0] as FakeImageData;
    expect([layer.width, layer.height]).toEqual([WIDTH, HEIGHT]);
    const alpha = Math.round(MASK_ALPHA * 255);
    // Border pixels carry class 0's colour; the centre carries class 1's;
    // the overlap-free interior corners (1,1) belong to the centre mask only.
    expect(pixel(layer.data, 0, 0).slice(3)).toEqual([alpha]);
    expect(pixel(layer.data, 1, 1).slice(3)).toEqual([alpha]);
    expect(pixel(layer.data, 0, 0).slice(0, 3)).not.toEqual(pixel(layer.data, 1, 1).slice(0, 3));
    // The masks are painted before any box is stroked.
    const names = context.calls.map((call) => call.name);
    expect(names.indexOf("putImageData")).toBeLessThan(names.indexOf("strokeRect"));
    expect(context.strokeRect.mock.calls.map((call) => call.slice(0, 4))).toEqual([
      [0, 0, 4, 4],
      [1, 1, 2, 2],
      [2, 2, 2, 2],
    ]);
    expect(context.fillText.mock.calls.map((call) => call[0])).toEqual([
      "frame 60%",
      "core 90%",
      "shed 50%",
    ]);
  });

  it("draws boxes only when masks are off, and for entries without a mask", () => {
    const ctx = context as unknown as CanvasRenderingContext2D;
    const summary = paintInstances(ctx, INSTANCES, WIDTH, HEIGHT, false);
    expect(summary).toEqual({ masksPainted: 0, masksSkipped: 0, boxesDrawn: 3 });
    expect(context.putImageData).not.toHaveBeenCalled();
    expect(context.strokeRect).toHaveBeenCalledTimes(3);
  });

  it("skips a mask that does not decode at the frame size and keeps its box", () => {
    const ctx = context as unknown as CanvasRenderingContext2D;
    const summary = paintInstances(
      ctx,
      [{ ...INSTANCES[1], mask_rle: "1 2 3" }, INSTANCES[0]],
      WIDTH,
      HEIGHT,
      true,
    );
    expect(summary).toEqual({ masksPainted: 1, masksSkipped: 1, boxesDrawn: 2 });
  });

  it("skips an entry without a complete numeric box", () => {
    const ctx = context as unknown as CanvasRenderingContext2D;
    const summary = paintInstances(
      ctx,
      [{ ...INSTANCES[2], bounding_box: [] }, { ...INSTANCES[2], bounding_box: [1, 2, Number.NaN, 4] }],
      WIDTH,
      HEIGHT,
      false,
    );
    expect(summary.boxesDrawn).toBe(0);
  });
});

describe("instanceSummary", () => {
  it("counts the masks and the ones the device did not store", () => {
    expect(instanceSummary(INSTANCES)).toBe(
      "2 of 3 detected objects with an instance mask; 1 mask was not stored by the device",
    );
    expect(instanceSummary([INSTANCES[0]])).toBe("1 of 1 detected object with an instance mask");
  });
});

describe("InstanceMaskLayer", () => {
  function loadImage(): void {
    const image = screen.getByTestId("instance-layer-image") as HTMLImageElement;
    Object.defineProperty(image, "naturalWidth", { value: WIDTH, configurable: true });
    Object.defineProperty(image, "naturalHeight", { value: HEIGHT, configurable: true });
    act(() => {
      fireEvent.load(image);
    });
  }

  it("renders the toggle, the frame and a canvas sized to the frame once the image loads", () => {
    render(<InstanceMaskLayer imageSrc="/output/exec-1" instances={INSTANCES} />);
    const segments = createWrapper(document.body).findSegmentedControl();
    expect(segments?.findSegments().map((segment) => segment.getElement().textContent)).toEqual([
      MASKS_AND_BOXES_LABEL,
      BOXES_ONLY_LABEL,
    ]);
    expect(segments?.findSelectedSegment()?.getElement().textContent).toBe(MASKS_AND_BOXES_LABEL);
    expect(screen.getByTestId("instance-layer-image")).toHaveAttribute("src", "/output/exec-1");
    expect(context.putImageData).not.toHaveBeenCalled();
    loadImage();
    const canvas = screen.getByTestId("instance-mask-canvas") as HTMLCanvasElement;
    expect([canvas.width, canvas.height]).toEqual([WIDTH, HEIGHT]);
    expect(context.putImageData).toHaveBeenCalledTimes(1);
    expect(context.strokeRect).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId("instance-layer-summary")).toHaveTextContent(
      "2 of 3 detected objects with an instance mask; 1 mask was not stored by the device",
    );
  });

  it("repaints without masks when Boxes is chosen", () => {
    render(<InstanceMaskLayer imageSrc="/output/exec-1" instances={INSTANCES} />);
    loadImage();
    context.putImageData.mockClear();
    context.strokeRect.mockClear();
    const segments = createWrapper(document.body).findSegmentedControl();
    act(() => {
      segments?.findSegments()[1].click();
    });
    expect(segments?.findSelectedSegment()?.getElement().textContent).toBe(BOXES_ONLY_LABEL);
    expect(context.putImageData).not.toHaveBeenCalled();
    expect(context.strokeRect).toHaveBeenCalledTimes(3);
  });

  it("draws boxes only for a list whose entries carry no mask", () => {
    const plain = INSTANCES.map(({ mask_rle, mask_area, mask_truncated, ...entry }) => entry);
    render(<InstanceMaskLayer imageSrc="/output/exec-1" instances={plain} />);
    loadImage();
    expect(context.putImageData).not.toHaveBeenCalled();
    expect(context.strokeRect).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId("instance-layer-summary")).toHaveTextContent(
      "0 of 3 detected objects with an instance mask",
    );
  });
});
