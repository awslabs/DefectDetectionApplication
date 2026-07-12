import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Container,
  Header,
  SpaceBetween,
  StatusIndicator,
  Toggle,
} from '@cloudscape-design/components';
import { scaleBox, type Box as SourceBox, type Dimensions } from '../utils/scaleBox';

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
}

export interface ResultsViewerProps {
  /** The capture to display, or null while nothing is selected. */
  capture: Capture | null;
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

export default function ResultsViewer({ capture }: ResultsViewerProps) {
  if (!capture) {
    return (
      <Container header={<Header variant="h2">Results</Header>}>
        <Box color="text-body-secondary">Select a capture to view its results.</Box>
      </Container>
    );
  }

  const isDetection = capture.inference_result_type === 'Detection';

  return (
    <Container header={<Header variant="h2">Capture {capture.capture_id}</Header>}>
      {isDetection ? (
        <DetectionView capture={capture} />
      ) : (
        <AnomalyView capture={capture} />
      )}
    </Container>
  );
}
