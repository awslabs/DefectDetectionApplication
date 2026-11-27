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
import * as React from "react";
import { Box, SegmentedControl, SpaceBetween } from "@cloudscape-design/components";
import {
  decodeMaskRle,
  hasMask,
  instanceColor,
  maskToImageData,
} from "../instanceDetections";
import type { InstanceDetectionEntry } from "../instanceDetections";
import { instanceKey } from "../layoutVerdicts";

/** Segment ids of the layer's display toggle. */
export const MASKS_AND_BOXES_ID = "masks";
export const BOXES_ONLY_ID = "boxes";
export const MASKS_AND_BOXES_LABEL = "Masks + boxes";
export const BOXES_ONLY_LABEL = "Boxes";

/** Opacity of the painted masks over the frame (the device's overlay fill). */
export const MASK_ALPHA = 0.4;

/** Header of the container the pages put the layer in. */
export const INSTANCE_MASKS_HEADER = "Instance masks";

/**
 * Layout Check styling (car-assembly-check Requirement 7.1, design DD8,
 * DD23): a flagged ("misplaced") instance is painted in its own colour,
 * the others dimmed when asked, and an instance the check ignored (outside
 * the car outline) in a neutral grey at reduced opacity.
 */
export const MISPLACED_COLOR: [number, number, number] = [236, 0, 140];
export const MISPLACED_ALPHA = 0.6;
export const DIMMED_ALPHA = 0.15;
export const MUTED_COLOR: [number, number, number] = [128, 128, 128];
export const MUTED_ALPHA = 0.2;
/** Stroke of the dashed Expected_Region outlines. */
export const OUTLINE_COLOR = "rgb(255, 196, 0)";

/** Which instances (by `instanceKey`) to highlight, and whether to dim the rest. */
export interface InstanceHighlight {
  keys: Set<string>;
  dimOthers: boolean;
}

/** A labelled polygon in frame pixels. */
export interface InstanceOutline {
  label: string;
  points: [number, number][];
}

/** The optional Layout Check styling of {@link paintInstances}. */
export interface PaintStyle {
  highlight?: InstanceHighlight;
  muted?: Set<string>;
}

type EntryLook = "plain" | "highlighted" | "dimmed" | "muted";

/** The key of an entry with a complete numeric box, else null. */
function entryKey(entry: InstanceDetectionEntry): string | null {
  const box = entry.bounding_box;
  if (
    !Array.isArray(box) ||
    box.length !== 4 ||
    !box.every((value) => typeof value === "number" && Number.isFinite(value))
  ) {
    return null;
  }
  return instanceKey({ classLabel: entry.class_label, box });
}

function entryLook(entry: InstanceDetectionEntry, style: PaintStyle | undefined): EntryLook {
  if (!style) {
    return "plain";
  }
  const key = entryKey(entry);
  if (key !== null && style.muted?.has(key)) {
    return "muted";
  }
  if (key !== null && style.highlight?.keys.has(key)) {
    return "highlighted";
  }
  return style.highlight?.dimOthers ? "dimmed" : "plain";
}

/** Highlighted masks are painted last, so they show on top. */
function lookRank(look: EntryLook): number {
  return look === "highlighted" ? 1 : 0;
}

/** The painting the layer did, for the summary line and the tests. */
export interface PaintSummary {
  /** Entries whose mask was decoded and painted. */
  masksPainted: number;
  /** Entries whose `mask_rle` did not decode at the frame size (skipped). */
  masksSkipped: number;
  /** Entries with a complete box, drawn. */
  boxesDrawn: number;
}

/**
 * Paint the entries on a canvas context whose backing store is the source
 * frame (`width × height`): every decodable mask first (ascending confidence,
 * so the most confident instance shows on top, as the device's overlay fill
 * does), one translucent colour per class, then every complete box with its
 * label above it. Masks are skipped when `showMasks` is false or an entry
 * carries none (a truncated or detection-only entry keeps its box).
 * `style` (optional) applies the Layout Check's highlight, dimming and
 * muting; without it the painting is exactly the plain one.
 */
export function paintInstances(
  ctx: CanvasRenderingContext2D,
  instances: InstanceDetectionEntry[],
  width: number,
  height: number,
  showMasks: boolean,
  style?: PaintStyle,
): PaintSummary {
  const summary: PaintSummary = { masksPainted: 0, masksSkipped: 0, boxesDrawn: 0 };
  ctx.clearRect(0, 0, width, height);
  if (width <= 0 || height <= 0) {
    return summary;
  }
  if (showMasks && typeof ImageData !== "undefined") {
    // One RGBA buffer for every mask: a later (more confident) instance
    // overwrites an earlier one where they overlap.
    const layer = new Uint8ClampedArray(width * height * 4);
    const alpha = Math.round(MASK_ALPHA * 255);
    const ordered = instances
      .filter(hasMask)
      .slice()
      .sort(
        (a, b) =>
          lookRank(entryLook(a, style)) - lookRank(entryLook(b, style)) ||
          (a.confidence ?? 0) - (b.confidence ?? 0),
      );
    for (const entry of ordered) {
      let bits: Uint8Array;
      try {
        bits = decodeMaskRle(entry.mask_rle as string, width, height);
      } catch {
        summary.masksSkipped += 1;
        continue;
      }
      const look = entryLook(entry, style);
      let [r, g, b] = instanceColor(entry);
      let entryAlpha = alpha;
      if (look === "highlighted") {
        [r, g, b] = MISPLACED_COLOR;
        entryAlpha = Math.round(MISPLACED_ALPHA * 255);
      } else if (look === "muted") {
        [r, g, b] = MUTED_COLOR;
        entryAlpha = Math.round(MUTED_ALPHA * 255);
      } else if (look === "dimmed") {
        entryAlpha = Math.round(DIMMED_ALPHA * 255);
      }
      const pixels = maskToImageData(bits, width, height, [r, g, b, entryAlpha]);
      for (let i = 0, n = bits.length; i < n; i++) {
        if (bits[i]) {
          const offset = i * 4;
          layer[offset] = pixels.data[offset];
          layer[offset + 1] = pixels.data[offset + 1];
          layer[offset + 2] = pixels.data[offset + 2];
          layer[offset + 3] = pixels.data[offset + 3];
        }
      }
      summary.masksPainted += 1;
    }
    if (summary.masksPainted > 0) {
      ctx.putImageData(new ImageData(layer, width, height), 0, 0);
    }
  }
  const lineWidth = Math.max(1, Math.round(Math.min(width, height) / 400));
  const fontSize = Math.max(10, Math.round(Math.min(width, height) / 45));
  ctx.lineWidth = lineWidth;
  ctx.font = `${fontSize}px sans-serif`;
  ctx.textBaseline = "bottom";
  for (const entry of instances) {
    const box = entry.bounding_box;
    if (
      !Array.isArray(box) ||
      box.length !== 4 ||
      !box.every((value) => typeof value === "number" && Number.isFinite(value))
    ) {
      continue;
    }
    const [xMin, yMin, xMax, yMax] = box;
    const look = entryLook(entry, style);
    const [r, g, b] = instanceColor(entry);
    let color = `rgb(${r}, ${g}, ${b})`;
    if (look === "highlighted") {
      color = `rgb(${MISPLACED_COLOR.join(", ")})`;
    } else if (look === "muted") {
      color = `rgba(${MUTED_COLOR.join(", ")}, 0.5)`;
    } else if (look === "dimmed") {
      color = `rgba(${r}, ${g}, ${b}, 0.4)`;
    }
    ctx.strokeStyle = color;
    ctx.strokeRect(xMin, yMin, xMax - xMin, yMax - yMin);
    const label = `${entry.class_label || entry.class_index || "object"} ${Math.round(
      (entry.confidence ?? 0) * 100,
    )}%`;
    ctx.fillStyle = color;
    ctx.fillText(label, xMin, Math.max(fontSize, yMin - lineWidth));
    summary.boxesDrawn += 1;
  }
  return summary;
}

/** The layer's one-line summary of what the Detection_List carries. */
export function instanceSummary(instances: InstanceDetectionEntry[]): string {
  const withMasks = instances.filter(hasMask).length;
  const truncated = instances.filter((entry) => entry.mask_truncated === true).length;
  const parts = [
    `${withMasks} of ${instances.length} detected object${instances.length === 1 ? "" : "s"} ` +
      `with an instance mask`,
  ];
  if (truncated > 0) {
    parts.push(
      `${truncated} mask${truncated === 1 ? " was" : "s were"} not stored by the device`,
    );
  }
  return parts.join("; ");
}

/**
 * Dashed, labelled polygons over the frame: an SVG whose view box is the
 * frame, stretched with the displayed image, so the outlines scale with it.
 */
function OutlineOverlay({
  outlines,
  width,
  height,
}: {
  outlines: InstanceOutline[];
  width: number;
  height: number;
}): JSX.Element {
  const fontSize = Math.max(10, Math.round(Math.min(width, height) / 45));
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      style={{
        position: "absolute",
        left: 0,
        top: 0,
        width: "100%",
        height: "100%",
        pointerEvents: "none",
      }}
      data-testid="instance-outlines"
    >
      {outlines.map((outline, index) => {
        const count = Math.max(1, outline.points.length);
        const cx = outline.points.reduce((sum, [x]) => sum + x, 0) / count;
        const cy = outline.points.reduce((sum, [, y]) => sum + y, 0) / count;
        return (
          <g key={`${outline.label}-${index}`} data-testid={`instance-outline-${outline.label}`}>
            <polygon
              points={outline.points.map(([x, y]) => `${x},${y}`).join(" ")}
              fill="none"
              stroke={OUTLINE_COLOR}
              strokeWidth={2}
              strokeDasharray="6 4"
              vectorEffect="non-scaling-stroke"
            />
            <text x={cx} y={cy} fill={OUTLINE_COLOR} fontSize={fontSize} textAnchor="middle">
              {outline.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/**
 * The run's instance masks and boxes drawn over the output frame
 * (rfdetr-seg-support Requirement 10.6, DD14): a canvas the size of the
 * frame, scaled with the image, with the masks under the boxes and a
 * `Masks + boxes | Boxes` toggle. Entries without a `mask_rle` (truncated
 * at the device's ceiling, or plain detections) draw their box only.
 *
 * car-assembly-check Requirement 7.1 adds three optional props: `highlight`
 * (the flagged instances in the "misplaced" colour, the others dimmed when
 * `dimOthers`), `outlines` (dashed, labelled Expected_Region polygons in
 * frame pixels) and `muted` (instances drawn in a neutral grey). Without
 * them the layer renders as before.
 */
export default function InstanceMaskLayer({
  imageSrc,
  instances,
  alt,
  highlight,
  outlines,
  muted,
}: {
  imageSrc: string;
  instances: InstanceDetectionEntry[];
  alt?: string;
  highlight?: InstanceHighlight;
  outlines?: InstanceOutline[];
  muted?: Set<string>;
}): JSX.Element {
  const [mode, setMode] = React.useState<string>(MASKS_AND_BOXES_ID);
  const [frameSize, setFrameSize] = React.useState<{ width: number; height: number } | null>(
    null,
  );
  const canvasRef = React.useRef<HTMLCanvasElement>(null);

  React.useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !frameSize) {
      return;
    }
    canvas.width = frameSize.width;
    canvas.height = frameSize.height;
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      return;
    }
    const style = highlight || muted ? { highlight, muted } : undefined;
    paintInstances(
      ctx,
      instances,
      frameSize.width,
      frameSize.height,
      mode === MASKS_AND_BOXES_ID,
      style,
    );
  }, [instances, mode, frameSize, highlight, muted]);

  return (
    <SpaceBetween size="s">
      <SegmentedControl
        selectedId={mode}
        onChange={({ detail }): void => setMode(detail.selectedId)}
        label="Instance layer"
        options={[
          { text: MASKS_AND_BOXES_LABEL, id: MASKS_AND_BOXES_ID },
          { text: BOXES_ONLY_LABEL, id: BOXES_ONLY_ID },
        ]}
      />
      <div style={{ position: "relative", width: "100%" }} data-testid="instance-mask-layer">
        <img
          src={imageSrc}
          alt={alt ?? "Run output with the detected instances drawn over it"}
          style={{ display: "block", width: "100%" }}
          onLoad={(event): void => {
            const { naturalWidth, naturalHeight } = event.currentTarget;
            if (naturalWidth > 0 && naturalHeight > 0) {
              setFrameSize({ width: naturalWidth, height: naturalHeight });
            }
          }}
          data-testid="instance-layer-image"
        />
        <canvas
          ref={canvasRef}
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            width: "100%",
            height: "100%",
            pointerEvents: "none",
          }}
          data-testid="instance-mask-canvas"
        />
        {frameSize && outlines && outlines.length > 0 && (
          <OutlineOverlay outlines={outlines} width={frameSize.width} height={frameSize.height} />
        )}
      </div>
      <Box variant="small" data-testid="instance-layer-summary">
        {instanceSummary(instances)}
      </Box>
    </SpaceBetween>
  );
}
