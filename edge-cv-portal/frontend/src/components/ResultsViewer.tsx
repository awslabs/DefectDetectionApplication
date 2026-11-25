import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Container,
  Header,
  SegmentedControl,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Toggle,
} from '@cloudscape-design/components';
import { scaleBox, type Box as SourceBox, type Dimensions } from '../utils/scaleBox';
import { pixelsToImageData, rleToImageData } from '../utils/rle';

/**
 * Results_Viewer (Requirement 4).
 *
 * Renders a single capture's inference result. This is a *presentational*,
 * prop-driven component: the parent (a page/route) fetches the capture from the
 * portal-backend captures endpoint (see `api.getCaptures`) and passes it in.
 * Keeping data-fetching out of this component makes the box-scaling / overlay /
 * anomaly-delegation logic trivially testable and lets the same viewer be
 * embedded anywhere a capture is opened.
 *
 * Behavior:
 *  - Detection capture (`inference_result_type === 'Detection'`) with objects:
 *    draws the source image with an absolutely-positioned `<svg>` overlay of
 *    `<rect>` + `<text>` per detection, scaled from source pixel coordinates to
 *    the rendered element size via `scaleBox` (Req 4.1, 4.2, 4.6). A toggle
 *    switches to the server-rendered `overlay.jpg` (Design Decision 3, Req 4.3);
 *    the toggle is disabled when no `overlay_url` is available.
 *  - Zero objects: shows the source image with a "No objects detected"
 *    indicator (Req 4.5).
 *  - Anomaly capture (`inference_result_type !== 'Detection'`): renders the
 *    source image with the `mask.png` overlay composited on top, consistent with
 *    the existing segmentation-mask presentation (Req 4.4).
 *  - Missing artifacts (null `source_url` / `overlay_url` / `mask_url`) degrade
 *    gracefully (Error Handling — "Portal — missing artifacts").
 *  - Instance capture (`'Detection'` with `has_masks`, rfdetr-seg-support
 *    Requirement 11.2, DD20): the boxes come from the page's light entries
 *    at once; the masks come from the per-capture detail `ResultsTab` fetches
 *    (`instanceDetections`, entries carrying `mask_rle`) and are decoded
 *    client-side onto a canvas under the SVG boxes in each instance's colour,
 *    with a three-way toggle (masks + boxes | boxes | server overlay), a
 *    spinner while the detail loads, a warning when it is unavailable, and
 *    the list of instances whose mask the device omitted. The detection-only
 *    and anomaly presentations are unchanged (semantic Seg captures are
 *    anomaly captures to the viewer, Req 11.3).
 */

/** A single detected object as returned by the captures endpoint. */
export interface Detection {
  /** Original numeric class identifier (retained), e.g. the COCO index `"17"`. */
  class_index: string;
  /** Human-readable class name resolved on-device, e.g. `"dog"`. */
  class_label: string;
  /** Bounding box in source-image pixel coordinates `[x_min, y_min, x_max, y_max]`. */
  bounding_box: [number, number, number, number];
  /** Detection confidence in `[0, 1]`. */
  confidence: number;
  /** Canonical_RLE of the instance mask at source resolution; detail response only. */
  mask_rle?: string;
  /** The instance mask's foreground pixel count (instance captures). */
  mask_area?: number;
  /** True when the device omitted this instance's mask at its payload ceiling. */
  mask_truncated?: boolean;
}

/** The inference-result type as typed by the Marshal in the capture metadata. */
export type InferenceResultType = 'Detection' | 'Anomaly' | 'Normal' | null;

/** A capture as returned by the portal-backend captures endpoint. */
export interface Capture {
  capture_id: string;
  inference_result_type: InferenceResultType;
  detection_count: number;
  detections: Detection[];
  /** Presigned URL for the source capture image (null if missing). */
  source_url: string | null;
  /** Presigned URL for the server-rendered overlay image (null if missing). */
  overlay_url: string | null;
  /** Presigned URL for the anomaly mask image (null if missing / not applicable). */
  mask_url: string | null;
  /** True when the device dropped instances from the Detection_List (Req 10.3). */
  detections_truncated?: boolean;
  /** True when any entry carried a mask on the device (instance capture). */
  has_masks?: boolean;
  /** The capture's `.jsonl` object key, the `key` of the per-capture detail request. */
  jsonl_key?: string;
}

/** Where the per-capture detail (the entries with `mask_rle`) stands. */
export type InstanceDetailState = 'loading' | 'ready' | 'unavailable';

export interface ResultsViewerProps {
  /** The capture to display, or null while nothing is selected. */
  capture: Capture | null;
  /**
   * The detail response's entries for an instance capture (carry `mask_rle`),
   * supplied by `ResultsTab`; absent = today's rendering.
   */
  instanceDetections?: Detection[] | null;
  /** The detail request's state for the shown capture; absent = today's rendering. */
  instanceDetailState?: InstanceDetailState;
}

/** Segment ids of the instance presentation's three-way toggle. */
export type InstanceLayerMode = 'masks' | 'boxes' | 'overlay';
export const MASKS_AND_BOXES_LABEL = 'Masks + boxes';
export const BOXES_ONLY_LABEL = 'Boxes only';
export const SERVER_OVERLAY_LABEL = 'Server overlay';
export const MASKS_UNAVAILABLE_TEXT = 'Masks unavailable for this capture';
/** Opacity of the painted masks over the source image. */
export const INSTANCE_MASK_ALPHA = 0.4;

/** A fixed palette, one colour per instance (wrapping), shared by its mask and its box. */
export const INSTANCE_PALETTE: ReadonlyArray<[number, number, number]> = [
  [230, 25, 75],
  [60, 180, 75],
  [255, 225, 25],
  [0, 130, 200],
  [245, 130, 48],
  [145, 30, 180],
  [70, 240, 240],
  [240, 50, 230],
  [210, 245, 60],
  [250, 190, 212],
  [0, 128, 128],
  [220, 190, 255],
];

/** The palette colour of the instance at `index` in the Detection_List. */
export function instanceColor(index: number): [number, number, number] {
  const n = INSTANCE_PALETTE.length;
  const safe = Number.isInteger(index) && index >= 0 ? index % n : 0;
  return INSTANCE_PALETTE[safe];
}

function cssColor([r, g, b]: [number, number, number]): string {
  return `rgb(${r}, ${g}, ${b})`;
}

/** The composite of every decodable mask of a capture, one RGBA layer. */
export interface InstanceMaskLayer {
  /** The capture the layer was decoded for. */
  captureId: string;
  data: Uint8ClampedArray<ArrayBuffer>;
  width: number;
  height: number;
  /** Entries whose `mask_rle` decoded at the source size and was painted. */
  painted: number;
  /** Entries whose `mask_rle` did not decode at the source size (box kept). */
  skipped: number;
}

/**
 * Decode every entry's `mask_rle` with `rleToImageData` at the source size
 * (`width x height` = the loaded image's natural dimensions) in the instance's
 * palette colour at 40 % alpha and composite them into one layer, ascending
 * confidence so the most confident instance shows on top where masks overlap.
 * Entries without a mask (truncated, or detection-only) and masks that do not
 * decode at this size are skipped.
 */
export function buildInstanceMaskLayer(
  captureId: string,
  detections: Detection[],
  width: number,
  height: number
): InstanceMaskLayer {
  const data = new Uint8ClampedArray(Math.max(0, width * height * 4));
  const layer: InstanceMaskLayer = { captureId, data, width, height, painted: 0, skipped: 0 };
  if (width <= 0 || height <= 0) {
    return layer;
  }
  const alpha = Math.round(INSTANCE_MASK_ALPHA * 255);
  const ordered = detections
    .map((det, index) => ({ det, index }))
    .filter(({ det }) => typeof det.mask_rle === 'string' && det.mask_rle.length > 0)
    .sort((a, b) => (a.det.confidence ?? 0) - (b.det.confidence ?? 0));
  for (const { det, index } of ordered) {
    let pixels: ImageData;
    try {
      pixels = rleToImageData(det.mask_rle as string, width, height, [
        ...instanceColor(index),
        alpha,
      ]);
    } catch {
      layer.skipped += 1;
      continue;
    }
    const src = pixels.data;
    for (let offset = 0; offset < src.length; offset += 4) {
      if (src[offset + 3]) {
        data[offset] = src[offset];
        data[offset + 1] = src[offset + 1];
        data[offset + 2] = src[offset + 2];
        data[offset + 3] = src[offset + 3];
      }
    }
    layer.painted += 1;
  }
  return layer;
}

/**
 * The instance masks of a capture on a `<canvas>` under the SVG box layer:
 * the backing store is the source frame (the RLE's resolution), the element
 * is sized to the rendered image so it scales with it. The layer is
 * memoised per capture so toggling does not re-decode.
 */
function InstanceMaskCanvas({
  captureId,
  detections,
  srcDims,
  dispDims,
}: {
  captureId: string;
  detections: Detection[];
  srcDims: Dimensions;
  dispDims: Dimensions;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const layer = useMemo(
    () => buildInstanceMaskLayer(captureId, detections, srcDims.w, srcDims.h),
    [captureId, detections, srcDims.w, srcDims.h]
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }
    canvas.width = layer.width;
    canvas.height = layer.height;
    const ctx = canvas.getContext('2d');
    if (!ctx) {
      return;
    }
    ctx.clearRect(0, 0, layer.width, layer.height);
    if (layer.painted > 0) {
      ctx.putImageData(pixelsToImageData(layer.data, layer.width, layer.height), 0, 0);
    }
  }, [layer]);

  return (
    <canvas
      ref={canvasRef}
      data-testid="instance-mask-canvas"
      aria-hidden="true"
      style={{
        position: 'absolute',
        top: 0,
        left: 0,
        width: dispDims.w,
        height: dispDims.h,
        pointerEvents: 'none',
      }}
    />
  );
}

/**
 * The instances whose mask the device omitted at its payload ceiling
 * (`mask_truncated` on the page's light entries), listed as `#index label`
 * whenever the capture reports `detections_truncated` (Req 11.2).
 */
export function truncatedInstancesNote(detections: Detection[]): string {
  const omitted = detections
    .map((det, index) => ({ det, index }))
    .filter(({ det }) => det.mask_truncated === true)
    .map(({ det, index }) => `#${index} ${det.class_label || det.class_index}`.trim());
  if (omitted.length === 0) {
    return 'Some instance masks were omitted on device (payload limit).';
  }
  return `Mask omitted on device (payload limit): ${omitted.join(', ')}`;
}

/** Format a `[0, 1]` confidence as a whole-number percentage. */
function formatConfidence(confidence: number): string {
  if (!Number.isFinite(confidence)) {
    return '';
  }
  return `${Math.round(confidence * 100)}%`;
}

/**
 * Hook that tracks the rendered (displayed) size of an `<img>` and its intrinsic
 * (natural) source dimensions. Recomputes on image load and on resize so the
 * overlay stays aligned with what the user sees (Req 4.6).
 */
function useImageDimensions(imgRef: React.RefObject<HTMLImageElement>) {
  const [srcDims, setSrcDims] = useState<Dimensions>({ w: 0, h: 0 });
  const [dispDims, setDispDims] = useState<Dimensions>({ w: 0, h: 0 });

  const measure = useCallback(() => {
    const img = imgRef.current;
    if (!img) {
      return;
    }
    setSrcDims({ w: img.naturalWidth, h: img.naturalHeight });
    setDispDims({ w: img.clientWidth, h: img.clientHeight });
  }, [imgRef]);

  // Recompute when the element resizes (responsive layout, window resize, etc.).
  useLayoutEffect(() => {
    const img = imgRef.current;
    if (!img || typeof ResizeObserver === 'undefined') {
      return;
    }
    const observer = new ResizeObserver(() => measure());
    observer.observe(img);
    return () => observer.disconnect();
  }, [imgRef, measure]);

  return { srcDims, dispDims, measure };
}

/**
 * Detection presentation: source image + SVG box overlay, with a toggle to the
 * server-rendered overlay image.
 */
function DetectionView({ capture }: { capture: Capture }) {
  const imgRef = useRef<HTMLImageElement>(null);
  const { srcDims, dispDims, measure } = useImageDimensions(imgRef);
  const [showServerOverlay, setShowServerOverlay] = useState(false);

  const hasServerOverlay = !!capture.overlay_url;
  const hasObjects = capture.detection_count > 0 && capture.detections.length > 0;

  // If the server overlay disappears (e.g. capture changes), fall back to boxes.
  useEffect(() => {
    if (!hasServerOverlay && showServerOverlay) {
      setShowServerOverlay(false);
    }
  }, [hasServerOverlay, showServerOverlay]);

  if (!capture.source_url) {
    return (
      <Alert type="warning" header="Source image unavailable">
        The source capture image for <b>{capture.capture_id}</b> could not be loaded.
      </Alert>
    );
  }

  const canScale = srcDims.w > 0 && srcDims.h > 0 && dispDims.w > 0 && dispDims.h > 0;

  return (
    <SpaceBetween size="m">
      <SpaceBetween direction="horizontal" size="s">
        <Badge color="blue">Detection</Badge>
        <Badge color={hasObjects ? 'green' : 'grey'}>
          {capture.detection_count} object{capture.detection_count === 1 ? '' : 's'}
        </Badge>
        <Toggle
          checked={showServerOverlay}
          disabled={!hasServerOverlay}
          onChange={({ detail }) => setShowServerOverlay(detail.checked)}
        >
          Show server overlay
        </Toggle>
      </SpaceBetween>

      {!hasObjects && (
        <StatusIndicator type="info">No objects detected</StatusIndicator>
      )}

      {showServerOverlay && hasServerOverlay ? (
        <img
          src={capture.overlay_url!}
          alt={`Server-rendered overlay for ${capture.capture_id}`}
          style={{ maxWidth: '100%', maxHeight: '70vh', objectFit: 'contain' }}
        />
      ) : (
        // Positioned container so the SVG overlay can sit exactly over the image.
        <div style={{ position: 'relative', display: 'inline-block', lineHeight: 0 }}>
          <img
            ref={imgRef}
            src={capture.source_url}
            alt={`Capture ${capture.capture_id}`}
            style={{ maxWidth: '100%', maxHeight: '70vh', objectFit: 'contain', display: 'block' }}
            onLoad={measure}
          />
          {canScale && hasObjects && (
            <svg
              width={dispDims.w}
              height={dispDims.h}
              viewBox={`0 0 ${dispDims.w} ${dispDims.h}`}
              style={{ position: 'absolute', top: 0, left: 0, pointerEvents: 'none' }}
            >
              {capture.detections.map((det, index) => {
                const scaled = scaleBox(
                  det.bounding_box as SourceBox,
                  srcDims,
                  dispDims
                );
                const label = `${det.class_label || det.class_index} ${formatConfidence(
                  det.confidence
                )}`.trim();
                // Keep the label inside the image: drop it below the top edge
                // when the box starts at the very top.
                const labelY = scaled.y > 14 ? scaled.y - 4 : scaled.y + 14;
                return (
                  <g key={`${capture.capture_id}-det-${index}`}>
                    <rect
                      x={scaled.x}
                      y={scaled.y}
                      width={scaled.w}
                      height={scaled.h}
                      fill="none"
                      stroke="#00b894"
                      strokeWidth={2}
                    />
                    <text
                      x={scaled.x + 2}
                      y={labelY}
                      fontSize={12}
                      fontFamily="sans-serif"
                      fill="#00b894"
                      stroke="#000000"
                      strokeWidth={0.4}
                      paintOrder="stroke"
                    >
                      {label}
                    </text>
                  </g>
                );
              })}
            </svg>
          )}
        </div>
      )}
    </SpaceBetween>
  );
}

/**
 * Instance presentation (rfdetr-seg-support Req 11.2): a `'Detection'`
 * capture with `has_masks`. The boxes come from the page's light entries
 * (`capture.detections`) at once, each in its instance's colour; the masks
 * come from the detail's entries (`instanceDetections`) once the detail is
 * `'ready'`, painted by `InstanceMaskCanvas` under the SVG box layer. The
 * three-way toggle picks masks + boxes (default), boxes only, or the server
 * overlay (disabled when the capture has none). While the detail is
 * `'loading'` a spinner sits beside the toggle and the boxes show alone; on
 * `'unavailable'` a warning says so and the boxes stay. Whenever the capture
 * reports `detections_truncated`, the instances whose mask the device omitted
 * are listed under the toggle, detail loaded or not.
 */
function InstanceDetectionView({
  capture,
  instanceDetections,
  instanceDetailState,
}: {
  capture: Capture;
  instanceDetections?: Detection[] | null;
  instanceDetailState?: InstanceDetailState;
}) {
  const imgRef = useRef<HTMLImageElement>(null);
  const { srcDims, dispDims, measure } = useImageDimensions(imgRef);
  const [mode, setMode] = useState<InstanceLayerMode>('masks');

  const hasServerOverlay = !!capture.overlay_url;
  const hasObjects = capture.detection_count > 0 && capture.detections.length > 0;
  const masksReady =
    instanceDetailState === 'ready' && !!instanceDetections && instanceDetections.length > 0;

  // If the server overlay disappears (e.g. capture changes), fall back to masks.
  useEffect(() => {
    if (!hasServerOverlay && mode === 'overlay') {
      setMode('masks');
    }
  }, [hasServerOverlay, mode]);

  if (!capture.source_url) {
    return (
      <Alert type="warning" header="Source image unavailable">
        The source capture image for <b>{capture.capture_id}</b> could not be loaded.
      </Alert>
    );
  }

  const canScale = srcDims.w > 0 && srcDims.h > 0 && dispDims.w > 0 && dispDims.h > 0;

  return (
    <SpaceBetween size="m">
      <SpaceBetween direction="horizontal" size="s" alignItems="center">
        <Badge color="blue">Detection</Badge>
        <Badge color={hasObjects ? 'green' : 'grey'}>
          {capture.detection_count} object{capture.detection_count === 1 ? '' : 's'}
        </Badge>
        <SegmentedControl
          label="Instance layer"
          selectedId={mode}
          onChange={({ detail }) => setMode(detail.selectedId as InstanceLayerMode)}
          options={[
            { id: 'masks', text: MASKS_AND_BOXES_LABEL },
            { id: 'boxes', text: BOXES_ONLY_LABEL },
            {
              id: 'overlay',
              text: SERVER_OVERLAY_LABEL,
              disabled: !hasServerOverlay,
              disabledReason: hasServerOverlay ? undefined : 'No server overlay for this capture',
            },
          ]}
        />
        {instanceDetailState === 'loading' && <Spinner />}
      </SpaceBetween>

      {instanceDetailState === 'unavailable' && (
        <StatusIndicator type="warning">{MASKS_UNAVAILABLE_TEXT}</StatusIndicator>
      )}

      {capture.detections_truncated && (
        <StatusIndicator type="info">{truncatedInstancesNote(capture.detections)}</StatusIndicator>
      )}

      {!hasObjects && (
        <StatusIndicator type="info">No objects detected</StatusIndicator>
      )}

      {mode === 'overlay' && hasServerOverlay ? (
        <img
          src={capture.overlay_url!}
          alt={`Server-rendered overlay for ${capture.capture_id}`}
          style={{ maxWidth: '100%', maxHeight: '70vh', objectFit: 'contain' }}
        />
      ) : (
        // Positioned container: the mask canvas and the SVG box layer sit
        // exactly over the image, canvas under boxes.
        <div style={{ position: 'relative', display: 'inline-block', lineHeight: 0 }}>
          <img
            ref={imgRef}
            src={capture.source_url}
            alt={`Capture ${capture.capture_id}`}
            style={{ maxWidth: '100%', maxHeight: '70vh', objectFit: 'contain', display: 'block' }}
            onLoad={measure}
          />
          {canScale && mode === 'masks' && masksReady && (
            <InstanceMaskCanvas
              captureId={capture.capture_id}
              detections={instanceDetections as Detection[]}
              srcDims={srcDims}
              dispDims={dispDims}
            />
          )}
          {canScale && hasObjects && (
            <svg
              width={dispDims.w}
              height={dispDims.h}
              viewBox={`0 0 ${dispDims.w} ${dispDims.h}`}
              style={{ position: 'absolute', top: 0, left: 0, pointerEvents: 'none' }}
            >
              {capture.detections.map((det, index) => {
                const scaled = scaleBox(det.bounding_box as SourceBox, srcDims, dispDims);
                const label = `${det.class_label || det.class_index} ${formatConfidence(
                  det.confidence
                )}`.trim();
                const labelY = scaled.y > 14 ? scaled.y - 4 : scaled.y + 14;
                const color = cssColor(instanceColor(index));
                return (
                  <g key={`${capture.capture_id}-inst-${index}`}>
                    <rect
                      x={scaled.x}
                      y={scaled.y}
                      width={scaled.w}
                      height={scaled.h}
                      fill="none"
                      stroke={color}
                      strokeWidth={2}
                    />
                    <text
                      x={scaled.x + 2}
                      y={labelY}
                      fontSize={12}
                      fontFamily="sans-serif"
                      fill={color}
                      stroke="#000000"
                      strokeWidth={0.4}
                      paintOrder="stroke"
                    >
                      {label}
                    </text>
                  </g>
                );
              })}
            </svg>
          )}
        </div>
      )}
    </SpaceBetween>
  );
}

/**
 * Anomaly presentation (Req 4.4): source image with the segmentation mask
 * composited on top, consistent with the existing mask-overlay pattern. This
 * path is used for any non-detection capture and leaves detection rendering
 * untouched.
 */
function AnomalyView({ capture }: { capture: Capture }) {
  const resultLabel = capture.inference_result_type ?? 'Result';

  if (!capture.source_url) {
    return (
      <Alert type="warning" header="Source image unavailable">
        The source capture image for <b>{capture.capture_id}</b> could not be loaded.
      </Alert>
    );
  }

  return (
    <SpaceBetween size="m">
      <Badge color={capture.inference_result_type === 'Anomaly' ? 'red' : 'green'}>
        {resultLabel}
      </Badge>
      <div style={{ position: 'relative', display: 'inline-block', lineHeight: 0 }}>
        <img
          src={capture.source_url}
          alt={`Capture ${capture.capture_id}`}
          style={{ maxWidth: '100%', maxHeight: '70vh', objectFit: 'contain', display: 'block' }}
        />
        {capture.mask_url && (
          <img
            src={capture.mask_url}
            alt={`Anomaly mask for ${capture.capture_id}`}
            style={{
              position: 'absolute',
              top: 0,
              left: 0,
              width: '100%',
              height: '100%',
              objectFit: 'contain',
              opacity: 0.5,
              pointerEvents: 'none',
            }}
          />
        )}
      </div>
    </SpaceBetween>
  );
}

export default function ResultsViewer({
  capture,
  instanceDetections,
  instanceDetailState,
}: ResultsViewerProps) {
  if (!capture) {
    return (
      <Container header={<Header variant="h2">Results</Header>}>
        <Box color="text-body-secondary">Select a capture to view its results.</Box>
      </Container>
    );
  }

  const isDetection = capture.inference_result_type === 'Detection';
  // Instance capture: the device summary still says 'Detection' (Req 11.1);
  // `has_masks` selects the instance presentation.
  const isInstance = isDetection && capture.has_masks === true;

  return (
    <Container header={<Header variant="h2">Capture {capture.capture_id}</Header>}>
      {isInstance ? (
        <InstanceDetectionView
          capture={capture}
          instanceDetections={instanceDetections}
          instanceDetailState={instanceDetailState}
        />
      ) : isDetection ? (
        <DetectionView capture={capture} />
      ) : (
        <AnomalyView capture={capture} />
      )}
    </Container>
  );
}
