/**
 * PreviewResultCanvas — read-only renderer for one successful
 * Preview_Result of a Prompt_Tuning_Preview run
 * (llm-autolabel-prompt-tuning Requirements 4.1–4.4).
 *
 * Display-only by construction: no drawing tools, no pointer handlers, no
 * mutation callbacks. It renders the modality Pre_Label shapes that
 * `dda_llm_guidance.guidance_to_prelabel` produces:
 *
 * - ObjectDetection: `{boxes: [{class, left, top, width, height}]}` drawn
 *   as overlays positioned proportionally (percentage geometry) over the
 *   displayed image, each with its Label_Set class name adjacent (req 4.1).
 * - Segmentation: `{regions: [{class, rle}]}` decoded through the shared
 *   RLE helpers and painted as translucent per-class fills, with each
 *   region's class name associated in a legend beside the image (req 4.2).
 * - Classification: `{label: 'normal' | 'anomaly'}` shown as label text
 *   beside the image (req 4.3).
 *
 * Geometry, RLE decoding and class colors come from `AnnotationCanvas`'s
 * exported pure helpers (`clampBoxToImage`, `parseRleCounts`,
 * `decodeRleColumnMajor`, `CLASS_PALETTE`), so preview overlays use the
 * same logic and the same palette as the labeler workspace without adding
 * a read-only mode to the editing component.
 *
 * A zero-detection Pre_Label renders an explicit empty-result indication
 * that is visually distinct from a populated result (which carries box /
 * region overlays and their class names) and from a failed result (which
 * `PromptTuningPreview` renders with a failure category and reason). For
 * Classification, `guidance_to_prelabel` maps zero detections to the
 * `normal` label, so a `normal` result shows both its label text (req 4.3)
 * and the empty-result indication (req 4.4).
 */
import { useEffect, useMemo, useRef } from 'react';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import type { DdaAnnotation } from '../../services/api';
import {
  CLASS_PALETTE,
  clampBoxToImage,
  decodeRleColumnMajor,
  parseRleCounts,
  type LabelingModality,
} from './AnnotationCanvas';

/** Color for geometry whose class is not a Label_Set member. */
const UNCLASSIFIED_COLOR = '#7A7A7A';
/** Mask fill opacity, matching the labeler workspace overlay. */
const MASK_ALPHA = 140;
/** Zero-detection Classification outcome (guidance_to_prelabel). */
const CLASSIFICATION_NORMAL = 'normal';

function hexToRgb(hex: string): [number, number, number] {
  const value = parseInt(hex.slice(1), 16);
  return [(value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff];
}

export interface PreviewResultCanvasProps {
  /** Presigned URL of the Sample_Image the Pre_Label was generated from. */
  imageUrl: string;
  /** The configured Labeling_Modality; only its shapes are rendered. */
  taskType: LabelingModality;
  /** Ordered Label_Set (fixed ['normal','anomaly'] for Classification). */
  labelSet: string[];
  /** The Pre_Label produced for this Sample_Image. */
  prelabel: DdaAnnotation;
  /** Sample_Image pixel width the Pre_Label coordinates are expressed in. */
  imageWidth: number;
  /** Sample_Image pixel height the Pre_Label coordinates are expressed in. */
  imageHeight: number;
  /** Accessible image description; defaults to the modality wording. */
  alt?: string;
}

export default function PreviewResultCanvas({
  imageUrl,
  taskType,
  labelSet,
  prelabel,
  imageWidth,
  imageHeight,
  alt,
}: PreviewResultCanvasProps) {
  const hasDimensions = imageWidth > 0 && imageHeight > 0;

  /** Palette color for a Pre_Label class name. */
  const colorFor = useMemo(
    () => (className: string | null | undefined): string => {
      const index = className ? labelSet.indexOf(className) : -1;
      return index >= 0
        ? CLASS_PALETTE[index % CLASS_PALETTE.length]
        : UNCLASSIFIED_COLOR;
    },
    [labelSet]
  );

  /* ---------------- ObjectDetection: proportional overlays ---------- */
  const boxes = useMemo(() => {
    if (taskType !== 'ObjectDetection' || !hasDimensions) return [];
    return (prelabel.boxes ?? []).map((box) => ({
      class: box.class,
      ...clampBoxToImage(box, imageWidth, imageHeight),
    }));
  }, [taskType, prelabel.boxes, imageWidth, imageHeight, hasDimensions]);

  /* ---------------- Segmentation: translucent per-class fills -------- */
  const regions = useMemo(
    () => (taskType === 'Segmentation' ? (prelabel.regions ?? []) : []),
    [taskType, prelabel.regions]
  );

  const maskCanvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = maskCanvasRef.current;
    if (!canvas || !hasDimensions) return;
    if (canvas.width !== imageWidth) canvas.width = imageWidth;
    if (canvas.height !== imageHeight) canvas.height = imageHeight;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, imageWidth, imageHeight);
    if (regions.length === 0) return;

    const imageData = ctx.createImageData(imageWidth, imageHeight);
    const data = imageData.data;
    for (const region of regions) {
      const [r, g, b] = hexToRgb(colorFor(region.class));
      const mask = decodeRleColumnMajor(
        parseRleCounts(region.rle),
        imageWidth,
        imageHeight
      );
      for (let p = 0; p < mask.length; p++) {
        if (mask[p]) {
          const o = p * 4;
          data[o] = r;
          data[o + 1] = g;
          data[o + 2] = b;
          data[o + 3] = MASK_ALPHA;
        }
      }
    }
    ctx.putImageData(imageData, 0, 0);
  }, [regions, imageWidth, imageHeight, hasDimensions, colorFor]);

  /* ---------------- emptiness (req 4.4) ----------------------------- */
  const classificationLabel = prelabel.label;
  const isEmptyResult =
    taskType === 'ObjectDetection'
      ? (prelabel.boxes ?? []).length === 0
      : taskType === 'Segmentation'
        ? regions.length === 0
        : classificationLabel === undefined ||
          classificationLabel === null ||
          classificationLabel === CLASSIFICATION_NORMAL;

  const imageAlt = alt ?? `Preview result (${taskType})`;

  const imageLayer = (
    <div
      data-testid="preview-result-image"
      style={{ position: 'relative', display: 'inline-block', maxWidth: '100%' }}
    >
      <img
        src={imageUrl}
        alt={imageAlt}
        style={{ display: 'block', maxWidth: '100%' }}
        draggable={false}
      />
      {taskType === 'Segmentation' && (
        <canvas
          ref={maskCanvasRef}
          data-testid="preview-mask-overlay"
          aria-hidden="true"
          style={{
            position: 'absolute',
            inset: 0,
            width: '100%',
            height: '100%',
            pointerEvents: 'none',
          }}
        />
      )}
      {/* Boxes are positioned in percentages of the image box, so they
          scale proportionally with whatever size the image renders at. */}
      {boxes.map((box, i) => {
        const color = colorFor(box.class);
        return (
          <div
            key={`preview-box-${i}`}
            data-testid="preview-box"
            style={{
              position: 'absolute',
              left: `${(box.left / imageWidth) * 100}%`,
              top: `${(box.top / imageHeight) * 100}%`,
              width: `${(box.width / imageWidth) * 100}%`,
              height: `${(box.height / imageHeight) * 100}%`,
              border: `2px solid ${color}`,
              boxSizing: 'border-box',
              pointerEvents: 'none',
            }}
          >
            <span
              data-testid="preview-box-class"
              style={{
                position: 'absolute',
                left: 0,
                bottom: '100%',
                padding: '0 2px',
                background: color,
                color: '#ffffff',
                fontSize: '12px',
                whiteSpace: 'nowrap',
              }}
            >
              {box.class ?? 'unclassified'}
            </span>
          </div>
        );
      })}
    </div>
  );

  return (
    <SpaceBetween size="xs">
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: '12px',
          flexWrap: 'wrap',
        }}
      >
        {imageLayer}
        <SpaceBetween size="xxs">
          {taskType === 'Classification' &&
            classificationLabel !== undefined &&
            classificationLabel !== null && (
              <div data-testid="preview-classification-label">
                <Box variant="awsui-key-label">Classification</Box>
                <Badge
                  color={
                    classificationLabel === CLASSIFICATION_NORMAL
                      ? 'green'
                      : 'red'
                  }
                >
                  {classificationLabel}
                </Badge>
              </div>
            )}
          {/* Each mask region's class name, associated with its fill color
              (req 4.2). */}
          {regions.length > 0 && (
            <div data-testid="preview-region-legend">
              <Box variant="awsui-key-label">Mask regions</Box>
              <SpaceBetween size="xxs">
                {regions.map((region, i) => (
                  <div
                    key={`preview-region-${i}`}
                    data-testid="preview-region-class"
                    style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
                  >
                    <span
                      aria-hidden="true"
                      style={{
                        display: 'inline-block',
                        width: '12px',
                        height: '12px',
                        background: colorFor(region.class),
                      }}
                    />
                    <Box variant="span">{region.class ?? 'unclassified'}</Box>
                  </div>
                ))}
              </SpaceBetween>
            </div>
          )}
        </SpaceBetween>
      </div>

      {isEmptyResult && (
        <div data-testid="preview-empty-result">
          <StatusIndicator type="info">
            No detections — the model returned an empty result for this image
          </StatusIndicator>
        </div>
      )}
    </SpaceBetween>
  );
}
