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
 * car-assembly-check Requirement 7.1, DD8, DD23: the instance layer's
 * optional Layout Check styling. Flagged instances are painted in the
 * "misplaced" colour with the others dimmed, ignored instances in a neutral
 * grey (never the misplaced colour), Expected_Region outlines as dashed
 * labelled polygons; without the new props the painting is the plain one.
 * jsdom has no canvas, so the 2D context is a recording stub.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import InstanceMaskLayer, {
  DIMMED_ALPHA,
  MASK_ALPHA,
  MISPLACED_ALPHA,
  MISPLACED_COLOR,
  MUTED_ALPHA,
  MUTED_COLOR,
  paintInstances,
} from "./InstanceMaskLayer";
import { instanceColor } from "../instanceDetections";
import type { InstanceDetectionEntry } from "../instanceDetections";
import { instanceKey } from "../layoutVerdicts";

const WIDTH = 4;
const HEIGHT = 4;
const BORDER_RLE = "0 5 2 2 2 5";
const CENTRE_RLE = "5 2 2 2 5";
const INSTANCES: InstanceDetectionEntry[] = [
  { class_index: "0", class_label: "frame", bounding_box: [0, 0, 4, 4], confidence: 0.6, mask_rle: BORDER_RLE, mask_area: 12 },
  { class_index: "1", class_label: "core", bounding_box: [1, 1, 3, 3], confidence: 0.9, mask_rle: CENTRE_RLE, mask_area: 4 },
];
const CORE_KEY = instanceKey({ classLabel: "core", box: [1, 1, 3, 3] });
const FRAME_KEY = instanceKey({ classLabel: "frame", box: [0, 0, 4, 4] });

interface Stub {
  calls: Array<{ name: string; args: unknown[]; strokeStyle: string; fillStyle: string }>;
  strokes: string[];
  layer(): FakeImageData | undefined;
  strokeStyle: string;
  fillStyle: string;
  lineWidth: number;
  font: string;
  textBaseline: string;
}

class FakeImageData {
  constructor(
    public data: Uint8ClampedArray,
    public width: number,
    public height: number,
  ) {}
}

function stubContext(): Stub & Record<string, unknown> {
  const stub: Stub & Record<string, unknown> = {
    calls: [],
    strokes: [],
    strokeStyle: "",
    fillStyle: "",
    lineWidth: 0,
    font: "",
    textBaseline: "",
    layer(): FakeImageData | undefined {
      const call = stub.calls.find((entry) => entry.name === "putImageData");
      return call?.args[0] as FakeImageData | undefined;
    },
  };
  for (const name of ["clearRect", "putImageData", "strokeRect", "fillText"]) {
    stub[name] = jest.fn((...args: unknown[]) => {
      stub.calls.push({ name, args, strokeStyle: stub.strokeStyle, fillStyle: stub.fillStyle });
      if (name === "strokeRect") {
        stub.strokes.push(stub.strokeStyle);
      }
    });
  }
  return stub;
}

function pixel(data: Uint8ClampedArray, x: number, y: number): number[] {
  const offset = (y * WIDTH + x) * 4;
  return Array.from(data.slice(offset, offset + 4));
}

let originalImageData: unknown;
let originalGetContext: typeof HTMLCanvasElement.prototype.getContext;
let context: ReturnType<typeof stubContext>;

beforeEach(() => {
  originalImageData = (globalThis as Record<string, unknown>).ImageData;
  (globalThis as Record<string, unknown>).ImageData = FakeImageData;
  context = stubContext();
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

function paint(style?: Parameters<typeof paintInstances>[5]): ReturnType<typeof stubContext> {
  const ctx = stubContext();
  paintInstances(ctx as unknown as CanvasRenderingContext2D, INSTANCES, WIDTH, HEIGHT, true, style);
  return ctx;
}

function snapshot(ctx: ReturnType<typeof stubContext>): unknown {
  return ctx.calls.map((call) => ({
    ...call,
    args: call.args.map((arg) => (arg instanceof FakeImageData ? Array.from(arg.data) : arg)),
  }));
}

describe("paintInstances with a Layout Check style", () => {
  it("paints exactly the plain layer without a style", () => {
    expect(snapshot(paint(undefined))).toEqual(snapshot(paint()));
    const plain = paint();
    const alpha = Math.round(MASK_ALPHA * 255);
    expect(pixel(plain.layer()!.data, 1, 1)).toEqual([...instanceColor(INSTANCES[1]), alpha]);
    expect(plain.strokes).toEqual(INSTANCES.map((entry) => `rgb(${instanceColor(entry).join(", ")})`));
  });

  it("paints the highlighted keys in the misplaced colour and dims the others", () => {
    const ctx = paint({ highlight: { keys: new Set([CORE_KEY]), dimOthers: true } });
    const layer = ctx.layer()!;
    expect(pixel(layer.data, 1, 1)).toEqual([...MISPLACED_COLOR, Math.round(MISPLACED_ALPHA * 255)]);
    expect(pixel(layer.data, 0, 0)).toEqual([...instanceColor(INSTANCES[0]), Math.round(DIMMED_ALPHA * 255)]);
    const [r, g, b] = instanceColor(INSTANCES[0]);
    expect(ctx.strokes).toEqual([`rgba(${r}, ${g}, ${b}, 0.4)`, `rgb(${MISPLACED_COLOR.join(", ")})`]);
  });

  it("keeps the others at full strength when not dimming", () => {
    const ctx = paint({ highlight: { keys: new Set([CORE_KEY]), dimOthers: false } });
    expect(pixel(ctx.layer()!.data, 0, 0)).toEqual([...instanceColor(INSTANCES[0]), Math.round(MASK_ALPHA * 255)]);
  });

  it("highlights a less confident instance and dims the more confident one", () => {
    const ctx = paint({ highlight: { keys: new Set([FRAME_KEY]), dimOthers: true } });
    const layer = ctx.layer()!;
    expect(pixel(layer.data, 0, 0).slice(0, 3)).toEqual(MISPLACED_COLOR);
    expect(pixel(layer.data, 1, 1)).toEqual([...instanceColor(INSTANCES[1]), Math.round(DIMMED_ALPHA * 255)]);
  });

  it("draws muted keys in grey, never in the misplaced colour", () => {
    const ctx = paint({
      highlight: { keys: new Set([CORE_KEY]), dimOthers: true },
      muted: new Set([CORE_KEY]),
    });
    expect(pixel(ctx.layer()!.data, 1, 1)).toEqual([...MUTED_COLOR, Math.round(MUTED_ALPHA * 255)]);
    expect(ctx.strokes[1]).toBe(`rgba(${MUTED_COLOR.join(", ")}, 0.5)`);
  });
});

describe("InstanceMaskLayer outlines", () => {
  function loadImage(): void {
    const image = screen.getByTestId("instance-layer-image") as HTMLImageElement;
    Object.defineProperty(image, "naturalWidth", { value: WIDTH, configurable: true });
    Object.defineProperty(image, "naturalHeight", { value: HEIGHT, configurable: true });
    act(() => {
      fireEvent.load(image);
    });
  }

  it("draws dashed labelled polygons over the frame", () => {
    render(
      <InstanceMaskLayer
        imageSrc="/output/exec-1"
        instances={INSTANCES}
        highlight={{ keys: new Set([CORE_KEY]), dimOthers: true }}
        outlines={[{ label: "roof", points: [[0, 0], [2, 0], [2, 2]] }]}
      />,
    );
    expect(screen.queryByTestId("instance-outlines")).toBeNull();
    loadImage();
    const svg = screen.getByTestId("instance-outlines");
    expect(svg).toHaveAttribute("viewBox", `0 0 ${WIDTH} ${HEIGHT}`);
    const polygon = svg.querySelector("polygon");
    expect(polygon).toHaveAttribute("points", "0,0 2,0 2,2");
    expect(polygon).toHaveAttribute("stroke-dasharray", "6 4");
    expect(screen.getByTestId("instance-outline-roof")).toHaveTextContent("roof");
    // The canvas was painted with the highlight.
    expect(pixel(context.layer()!.data, 1, 1).slice(0, 3)).toEqual(MISPLACED_COLOR);
  });

  it("renders the plain layer without the new props", () => {
    render(<InstanceMaskLayer imageSrc="/output/exec-1" instances={INSTANCES} />);
    loadImage();
    expect(screen.queryByTestId("instance-outlines")).toBeNull();
    expect(snapshot(context)).toEqual(snapshot(paint()));
  });
});
