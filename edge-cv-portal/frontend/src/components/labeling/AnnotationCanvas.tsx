/**
 * AnnotationCanvas — the labeling surface of the DDA Labeler_Interface
 * (dda-data-labeling Requirements 7.3–7.6, 7.8, 8.3, 12.7).
 *
 * An HTML5 canvas layered over the presigned task image. Exactly one
 * modality's tools render at a time (req 7.6):
 *
 * - Classification: a normal/anomaly segmented control (req 7.3).
 * - ObjectDetection: drag-to-draw bounding boxes in image pixel
 *   coordinates, clamped to the image bounds; every box must carry a
 *   class from the Label_Set before submit (req 7.5).
 * - Segmentation: brush/eraser painting per selected class with an
 *   adjustable brush size; regions are held as a label-indexed bitmap and
 *   RLE-encoded (COCO-style column-major counts, convertible to the
 *   backend's space-separated counts string) for submission (req 7.4).
 *
 * Pre_Labels initialize the editable annotation state and can be approved
 * as-is or corrected (req 8.3). Classless SAM proposals render as neutral
 * geometry and must be classified or deleted before submit. Incomplete
 * submissions are blocked client-side with the missing element identified
 * (req 7.8). When the presigned image URL expires (load error or expiry
 * timestamp) the component invokes `onImageUrlRefresh` and swaps the image
 * without touching annotation state (req 12.7).
 *
 * Consumers should remount per Task_Assignment (e.g. `key={task.task_id}`)
 * so annotation state resets between tasks.
 */
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import Select from '@cloudscape-design/components/select';
import Slider from '@cloudscape-design/components/slider';
import SpaceBetween from '@cloudscape-design/components/space-between';
import type {
  DdaAnnotation,
  DdaBoundingBox,
  DdaMaskRegion,
} from '../../services/api';

/** Task-type identifiers used by DDA labeling jobs. */
export type LabelingModality =
  | 'Classification'
  | 'Segmentation'
  | 'ObjectDetection';

/**
 * Fixed class palette (mirrors the job-wide mask palette used by the
 * backend manifest generator: `#23A436, #1E90FF, #FF8C00, ...`).
 */
export const CLASS_PALETTE = [
  '#23A436',
  '#1E90FF',
  '#FF8C00',
  '#DC143C',
  '#8A2BE2',
  '#00CED1',
  '#B8860B',
  '#FF69B4',
  '#556B2F',
  '#4682B4',
] as const;

const PROPOSAL_COLOR = '#7A7A7A';
const OVERLAY_ALPHA = 140;
/** Refresh the presigned URL this many ms before its expiry timestamp. */
const EXPIRY_REFRESH_MARGIN_MS = 30_000;
const MAX_CONSECUTIVE_LOAD_FAILURES = 3;

/* ------------------------------------------------------------------ */
/* RLE helpers (COCO-style column-major counts)                        */
/* ------------------------------------------------------------------ */

/**
 * Encode the pixels of `bitmap` equal to `value` as COCO-style RLE counts:
 * pixels are visited in column-major order (down each column, left to
 * right) and counts alternate starting with the count of zeros. Joining
 * the returned counts with spaces yields the backend's canonical
 * space-separated counts string.
 */
export function encodeRleColumnMajor(
  bitmap: Uint8Array,
  width: number,
  height: number,
  value: number
): number[] {
  const counts: number[] = [];
  let current = 0;
  let run = 0;
  for (let x = 0; x < width; x++) {
    const columnBase = x;
    for (let y = 0; y < height; y++) {
      const v = bitmap[y * width + columnBase] === value ? 1 : 0;
      if (v === current) {
        run++;
      } else {
        counts.push(run);
        current = v;
        run = 1;
      }
    }
  }
  counts.push(run);
  return counts;
}

/**
 * Decode COCO-style column-major RLE counts into a binary mask stored in
 * row-major order (1 where the region is present). Inverse of
 * {@link encodeRleColumnMajor}. Accepts either a counts array or the
 * backend's canonical space-separated counts string (the shape
 * `DdaMaskRegion.rle` actually carries at runtime).
 */
export function decodeRleColumnMajor(
  rle: number[] | string,
  width: number,
  height: number
): Uint8Array {
  const counts =
    typeof rle === 'string'
      ? rle
          .split(/\s+/)
          .filter((token) => token.length > 0)
          .map(Number)
      : rle;
  const mask = new Uint8Array(width * height);
  let value = 0;
  let index = 0; // column-major pixel index
  const total = width * height;
  for (const count of counts) {
    if (value === 1) {
      for (let k = 0; k < count && index + k < total; k++) {
        const p = index + k;
        const x = Math.floor(p / height);
        const y = p % height;
        mask[y * width + x] = 1;
      }
    }
    index += count;
    value = 1 - value;
  }
  return mask;
}

/**
 * Clamp a box (image pixel coordinates) to lie within
 * `[0, width] x [0, height]`, rounding to integers. Boxes fully outside
 * the image collapse to zero-area boxes on the nearest edge.
 */
export function clampBoxToImage(
  box: { left: number; top: number; width: number; height: number },
  width: number,
  height: number
): { left: number; top: number; width: number; height: number } {
  const left = Math.min(Math.max(Math.round(box.left), 0), width);
  const top = Math.min(Math.max(Math.round(box.top), 0), height);
  const right = Math.min(Math.max(Math.round(box.left + box.width), left), width);
  const bottom = Math.min(
    Math.max(Math.round(box.top + box.height), top),
    height
  );
  return { left, top, width: right - left, height: bottom - top };
}

function hexToRgb(hex: string): [number, number, number] {
  const value = parseInt(hex.slice(1), 16);
  return [(value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff];
}

/* ------------------------------------------------------------------ */
/* Component types                                                     */
/* ------------------------------------------------------------------ */

interface EditableBox extends DdaBoundingBox {
  id: string;
}

interface SegProposal {
  id: string;
  /** Decoded binary mask (row-major) of the classless SAM proposal. */
  mask: Uint8Array;
}

export interface AnnotationCanvasHandle {
  /**
   * Returns the list of missing elements blocking submission (empty when
   * the annotation is complete for the modality) — req 7.8.
   */
  validate: () => string[];
  /** Current annotation payload in the canonical DdaAnnotation shape. */
  getAnnotation: () => DdaAnnotation;
}

export interface AnnotationCanvasProps {
  /** Presigned URL of the task image (15-minute grant). */
  imageUrl: string;
  /** Epoch seconds when the presigned URL expires, when known. */
  imageUrlExpiresAt?: number;
  /** The job's Labeling_Modality; only this modality's tools render. */
  taskType: LabelingModality;
  /** Ordered Label_Set (fixed ['normal','anomaly'] for Classification). */
  labelSet: string[];
  /** Pre_Label to render as the editable starting layer (req 8.3). */
  prelabel?: DdaAnnotation;
  /** Disables the submit controls while a submission is in flight. */
  submitting?: boolean;
  /** Receives the DdaAnnotation payload of a complete submission. */
  onSubmit: (annotation: DdaAnnotation) => void | Promise<void>;
  /**
   * Called when the presigned URL expires or the image fails to load;
   * must return a fresh presigned URL (req 12.7). Annotation state is
   * preserved across the swap.
   */
  onImageUrlRefresh: () => Promise<{
    image_url: string;
    image_url_expires_at?: number;
  }>;
  /**
   * Called when the image cannot be presented even after URL refreshes
   * (req 7.12 hand-off to the workspace's presentation-failure flow).
   */
  onPresentationFailure?: (reason: string) => void;
}

/* ------------------------------------------------------------------ */
/* Component                                                           */
/* ------------------------------------------------------------------ */

const AnnotationCanvas = forwardRef<AnnotationCanvasHandle, AnnotationCanvasProps>(
  function AnnotationCanvas(props, ref) {
    const {
      imageUrl,
      imageUrlExpiresAt,
      taskType,
      labelSet,
      prelabel,
      submitting,
      onSubmit,
      onImageUrlRefresh,
      onPresentationFailure,
    } = props;

    /* ---------------- image / presigned URL state ---------------- */
    const [currentUrl, setCurrentUrl] = useState(imageUrl);
    const [expiresAt, setExpiresAt] = useState<number | undefined>(
      imageUrlExpiresAt
    );
    const [imageSize, setImageSize] = useState<{
      width: number;
      height: number;
    } | null>(null);
    const [imageFailed, setImageFailed] = useState(false);
    const refreshingRef = useRef(false);
    const loadFailureCountRef = useRef(0);

    /* ---------------- annotation state (survives URL swaps) ------- */
    const [classification, setClassification] = useState<string | null>(
      prelabel?.label ?? null
    );
    const [boxes, setBoxes] = useState<EditableBox[]>(() =>
      (prelabel?.boxes ?? []).map((b, i) => ({
        id: `prelabel-box-${i}`,
        class: b.class,
        left: b.left,
        top: b.top,
        width: b.width,
        height: b.height,
      }))
    );
    const [draftBox, setDraftBox] = useState<{
      left: number;
      top: number;
      width: number;
      height: number;
    } | null>(null);
    const segBitmapRef = useRef<Uint8Array | null>(null);
    const [segVersion, setSegVersion] = useState(0);
    const [proposals, setProposals] = useState<SegProposal[]>([]);
    const segInitializedRef = useRef(false);

    /* ---------------- tool state ---------------------------------- */
    const [selectedClassIndex, setSelectedClassIndex] = useState(0);
    const [segTool, setSegTool] = useState<'brush' | 'eraser'>('brush');
    const [brushSize, setBrushSize] = useState(24);
    const [edited, setEdited] = useState(false);
    const [validationErrors, setValidationErrors] = useState<string[]>([]);

    const overlayRef = useRef<HTMLCanvasElement | null>(null);
    const paintingRef = useRef(false);
    const lastPaintPosRef = useRef<{ x: number; y: number } | null>(null);
    const dragStartRef = useRef<{ x: number; y: number } | null>(null);

    /* ---------------- presigned URL refresh (req 12.7) ------------ */
    const refreshImageUrl = useCallback(async () => {
      if (refreshingRef.current) return;
      refreshingRef.current = true;
      try {
        const res = await onImageUrlRefresh();
        setCurrentUrl(res.image_url);
        setExpiresAt(res.image_url_expires_at);
        setImageFailed(false);
      } catch {
        setImageFailed(true);
        onPresentationFailure?.('Failed to refresh the image URL');
      } finally {
        refreshingRef.current = false;
      }
    }, [onImageUrlRefresh, onPresentationFailure]);

    // Proactive refresh shortly before the known expiry timestamp.
    useEffect(() => {
      if (!expiresAt) return;
      const delay = Math.max(
        0,
        expiresAt * 1000 - Date.now() - EXPIRY_REFRESH_MARGIN_MS
      );
      const timer = window.setTimeout(() => {
        void refreshImageUrl();
      }, delay);
      return () => window.clearTimeout(timer);
    }, [expiresAt, refreshImageUrl]);

    const handleImageError = useCallback(() => {
      loadFailureCountRef.current += 1;
      if (loadFailureCountRef.current > MAX_CONSECUTIVE_LOAD_FAILURES) {
        setImageFailed(true);
        onPresentationFailure?.('The image could not be loaded');
        return;
      }
      void refreshImageUrl();
    }, [refreshImageUrl, onPresentationFailure]);

    const handleImageLoad = useCallback(
      (event: React.SyntheticEvent<HTMLImageElement>) => {
        const img = event.currentTarget;
        const width = img.naturalWidth;
        const height = img.naturalHeight;
        loadFailureCountRef.current = 0;
        setImageFailed(false);
        setImageSize({ width, height });

        // Initialize segmentation state once; a URL refresh re-fires
        // onLoad but must not clobber in-progress painting (req 12.7).
        if (taskType === 'Segmentation' && !segInitializedRef.current) {
          segInitializedRef.current = true;
          const bitmap = new Uint8Array(width * height);
          const classless: SegProposal[] = [];
          (prelabel?.regions ?? []).forEach((region: DdaMaskRegion, i) => {
            const classIndex =
              region.class !== null && region.class !== undefined
                ? labelSet.indexOf(region.class)
                : -1;
            const mask = decodeRleColumnMajor(region.rle, width, height);
            if (classIndex >= 0) {
              for (let p = 0; p < mask.length; p++) {
                if (mask[p]) bitmap[p] = classIndex + 1;
              }
            } else {
              classless.push({ id: `proposal-${i}`, mask });
            }
          });
          segBitmapRef.current = bitmap;
          setProposals(classless);
          setSegVersion((v) => v + 1);
        }
      },
      [taskType, prelabel, labelSet]
    );

    /* ---------------- coordinate mapping -------------------------- */
    const toImageCoords = useCallback(
      (event: React.PointerEvent<HTMLCanvasElement>) => {
        const canvas = overlayRef.current;
        if (!canvas || !imageSize) return null;
        const rect = canvas.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return null;
        const x = ((event.clientX - rect.left) / rect.width) * imageSize.width;
        const y = ((event.clientY - rect.top) / rect.height) * imageSize.height;
        return {
          x: Math.min(Math.max(x, 0), imageSize.width),
          y: Math.min(Math.max(y, 0), imageSize.height),
        };
      },
      [imageSize]
    );

    /* ---------------- segmentation painting ----------------------- */
    const stampCircle = useCallback(
      (cx: number, cy: number, value: number) => {
        const bitmap = segBitmapRef.current;
        if (!bitmap || !imageSize) return;
        const { width, height } = imageSize;
        const r = brushSize / 2;
        const minY = Math.max(0, Math.floor(cy - r));
        const maxY = Math.min(height - 1, Math.ceil(cy + r));
        const minX = Math.max(0, Math.floor(cx - r));
        const maxX = Math.min(width - 1, Math.ceil(cx + r));
        for (let y = minY; y <= maxY; y++) {
          for (let x = minX; x <= maxX; x++) {
            const dx = x - cx;
            const dy = y - cy;
            if (dx * dx + dy * dy <= r * r) {
              bitmap[y * width + x] = value;
            }
          }
        }
      },
      [brushSize, imageSize]
    );

    const paintTo = useCallback(
      (pos: { x: number; y: number }) => {
        const value = segTool === 'eraser' ? 0 : selectedClassIndex + 1;
        const last = lastPaintPosRef.current;
        if (last) {
          const dist = Math.hypot(pos.x - last.x, pos.y - last.y);
          const step = Math.max(1, brushSize / 4);
          const steps = Math.ceil(dist / step);
          for (let s = 1; s <= steps; s++) {
            stampCircle(
              last.x + ((pos.x - last.x) * s) / steps,
              last.y + ((pos.y - last.y) * s) / steps,
              value
            );
          }
        } else {
          stampCircle(pos.x, pos.y, value);
        }
        lastPaintPosRef.current = pos;
        setSegVersion((v) => v + 1);
      },
      [segTool, selectedClassIndex, brushSize, stampCircle]
    );

    /* ---------------- pointer handlers ---------------------------- */
    const handlePointerDown = useCallback(
      (event: React.PointerEvent<HTMLCanvasElement>) => {
        const pos = toImageCoords(event);
        if (!pos) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        if (taskType === 'ObjectDetection') {
          dragStartRef.current = pos;
          setDraftBox({ left: pos.x, top: pos.y, width: 0, height: 0 });
        } else if (taskType === 'Segmentation') {
          paintingRef.current = true;
          lastPaintPosRef.current = null;
          paintTo(pos);
          setEdited(true);
        }
      },
      [taskType, toImageCoords, paintTo]
    );

    const handlePointerMove = useCallback(
      (event: React.PointerEvent<HTMLCanvasElement>) => {
        const pos = toImageCoords(event);
        if (!pos) return;
        if (taskType === 'ObjectDetection' && dragStartRef.current) {
          const start = dragStartRef.current;
          setDraftBox({
            left: Math.min(start.x, pos.x),
            top: Math.min(start.y, pos.y),
            width: Math.abs(pos.x - start.x),
            height: Math.abs(pos.y - start.y),
          });
        } else if (taskType === 'Segmentation' && paintingRef.current) {
          paintTo(pos);
        }
      },
      [taskType, toImageCoords, paintTo]
    );

    const handlePointerUp = useCallback(
      (event: React.PointerEvent<HTMLCanvasElement>) => {
        event.currentTarget.releasePointerCapture(event.pointerId);
        if (taskType === 'ObjectDetection' && dragStartRef.current) {
          dragStartRef.current = null;
          if (draftBox && imageSize) {
            const clamped = clampBoxToImage(
              draftBox,
              imageSize.width,
              imageSize.height
            );
            if (clamped.width >= 3 && clamped.height >= 3) {
              setBoxes((prev) => [
                ...prev,
                {
                  id: `box-${Date.now()}-${prev.length}`,
                  class: labelSet[selectedClassIndex] ?? null,
                  ...clamped,
                },
              ]);
              setEdited(true);
            }
          }
          setDraftBox(null);
        } else if (taskType === 'Segmentation') {
          paintingRef.current = false;
          lastPaintPosRef.current = null;
        }
      },
      [taskType, draftBox, imageSize, labelSet, selectedClassIndex]
    );

    /* ---------------- box / proposal editing ----------------------- */
    const setBoxClass = useCallback((id: string, className: string) => {
      setBoxes((prev) =>
        prev.map((b) => (b.id === id ? { ...b, class: className } : b))
      );
      setEdited(true);
    }, []);

    const deleteBox = useCallback((id: string) => {
      setBoxes((prev) => prev.filter((b) => b.id !== id));
      setEdited(true);
    }, []);

    const assignProposalClass = useCallback(
      (id: string, classIndex: number) => {
        const bitmap = segBitmapRef.current;
        setProposals((prev) => {
          const proposal = prev.find((p) => p.id === id);
          if (proposal && bitmap) {
            for (let p = 0; p < proposal.mask.length; p++) {
              if (proposal.mask[p]) bitmap[p] = classIndex + 1;
            }
          }
          return prev.filter((p) => p.id !== id);
        });
        setSegVersion((v) => v + 1);
        setEdited(true);
      },
      []
    );

    const deleteProposal = useCallback((id: string) => {
      setProposals((prev) => prev.filter((p) => p.id !== id));
      setSegVersion((v) => v + 1);
      setEdited(true);
    }, []);

    /* ---------------- overlay rendering ---------------------------- */
    useEffect(() => {
      const canvas = overlayRef.current;
      if (!canvas || !imageSize) return;
      const { width, height } = imageSize;
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.clearRect(0, 0, width, height);

      if (taskType === 'Segmentation') {
        const bitmap = segBitmapRef.current;
        if (bitmap) {
          const imageData = ctx.createImageData(width, height);
          const data = imageData.data;
          const classColors = labelSet.map((_, i) =>
            hexToRgb(CLASS_PALETTE[i % CLASS_PALETTE.length])
          );
          const proposalRgb = hexToRgb(PROPOSAL_COLOR);
          for (let p = 0; p < bitmap.length; p++) {
            const v = bitmap[p];
            if (v > 0) {
              const [r, g, b] = classColors[(v - 1) % classColors.length];
              const o = p * 4;
              data[o] = r;
              data[o + 1] = g;
              data[o + 2] = b;
              data[o + 3] = OVERLAY_ALPHA;
            }
          }
          for (const proposal of proposals) {
            for (let p = 0; p < proposal.mask.length; p++) {
              if (proposal.mask[p]) {
                const o = p * 4;
                data[o] = proposalRgb[0];
                data[o + 1] = proposalRgb[1];
                data[o + 2] = proposalRgb[2];
                data[o + 3] = OVERLAY_ALPHA;
              }
            }
          }
          ctx.putImageData(imageData, 0, 0);
        }
      } else if (taskType === 'ObjectDetection') {
        const lineWidth = Math.max(2, Math.round(width / 400));
        ctx.lineWidth = lineWidth;
        ctx.font = `${Math.max(12, Math.round(width / 60))}px sans-serif`;
        for (const box of boxes) {
          const classIndex = box.class ? labelSet.indexOf(box.class) : -1;
          const color =
            classIndex >= 0
              ? CLASS_PALETTE[classIndex % CLASS_PALETTE.length]
              : PROPOSAL_COLOR;
          ctx.strokeStyle = color;
          ctx.setLineDash(classIndex >= 0 ? [] : [8, 6]);
          ctx.strokeRect(box.left, box.top, box.width, box.height);
          ctx.setLineDash([]);
          ctx.fillStyle = color;
          ctx.fillText(
            box.class ?? 'unclassified',
            box.left + lineWidth,
            Math.max(box.top - 4, 12)
          );
        }
        if (draftBox) {
          ctx.strokeStyle = '#0972D3';
          ctx.setLineDash([6, 4]);
          ctx.strokeRect(
            draftBox.left,
            draftBox.top,
            draftBox.width,
            draftBox.height
          );
          ctx.setLineDash([]);
        }
      }
      // Classification renders no overlay.
    }, [taskType, imageSize, boxes, draftBox, proposals, labelSet, segVersion]);

    /* ---------------- annotation assembly / validation ------------- */
    const buildAnnotation = useCallback((): DdaAnnotation => {
      if (taskType === 'Classification') {
        return { label: classification ?? undefined };
      }
      if (taskType === 'ObjectDetection') {
        return {
          boxes: boxes.map(({ id: _id, ...box }) =>
            imageSize
              ? { class: box.class, ...clampBoxToImage(box, imageSize.width, imageSize.height) }
              : box
          ),
          image_width: imageSize?.width,
          image_height: imageSize?.height,
        };
      }
      // Segmentation: RLE-encode the label-indexed bitmap per class.
      const regions: DdaMaskRegion[] = [];
      const bitmap = segBitmapRef.current;
      if (bitmap && imageSize) {
        const present = new Set<number>();
        for (let p = 0; p < bitmap.length; p++) {
          if (bitmap[p] > 0) present.add(bitmap[p]);
        }
        labelSet.forEach((className, i) => {
          if (present.has(i + 1)) {
            regions.push({
              class: className,
              // The backend validates rle as a non-empty space-separated
              // counts string, so join the counts for submission.
              rle: encodeRleColumnMajor(
                bitmap,
                imageSize.width,
                imageSize.height,
                i + 1
              ).join(' '),
            });
          }
        });
      }
      return {
        regions,
        image_width: imageSize?.width,
        image_height: imageSize?.height,
      };
    }, [taskType, classification, boxes, imageSize, labelSet]);

    const validate = useCallback((): string[] => {
      const missing: string[] = [];
      if (taskType === 'Classification') {
        if (!classification) {
          missing.push('No selection made: choose normal or anomaly.');
        }
      } else if (taskType === 'ObjectDetection') {
        boxes.forEach((box, i) => {
          if (!box.class || !labelSet.includes(box.class)) {
            missing.push(
              `Box ${i + 1} has no class assigned: pick a class or delete the box.`
            );
          }
        });
      } else {
        proposals.forEach((_, i) => {
          missing.push(
            `Proposed region ${i + 1} has no class assigned: assign a class or delete it.`
          );
        });
      }
      return missing;
    }, [taskType, classification, boxes, proposals, labelSet]);

    useImperativeHandle(
      ref,
      () => ({ validate, getAnnotation: buildAnnotation }),
      [validate, buildAnnotation]
    );

    const handleSubmit = useCallback(() => {
      const missing = validate();
      setValidationErrors(missing);
      if (missing.length > 0) return;
      void onSubmit(buildAnnotation());
    }, [validate, buildAnnotation, onSubmit]);

    /* ---------------- derived UI state ----------------------------- */
    const hasPrelabel =
      !!prelabel &&
      ((prelabel.label !== undefined && prelabel.label !== null) ||
        (prelabel.boxes?.length ?? 0) > 0 ||
        (prelabel.regions?.length ?? 0) > 0);
    const prelabelHasClasslessItems =
      (prelabel?.boxes ?? []).some((b) => b.class === null) ||
      (prelabel?.regions ?? []).some((r) => r.class === null);
    const showApproveAsIs = hasPrelabel && !edited;

    const classOptions = useMemo(
      () =>
        labelSet.map((name, i) => ({
          label: name,
          value: String(i),
        })),
      [labelSet]
    );

    const classSelectOptions = useMemo(
      () => labelSet.map((name) => ({ label: name, value: name })),
      [labelSet]
    );

    /* ---------------- render --------------------------------------- */
    return (
      <SpaceBetween size="m">
        {hasPrelabel && (
          <Alert
            type="info"
            header={
              prelabelHasClasslessItems
                ? 'Model proposals need classification'
                : 'Pre-label loaded'
            }
          >
            {prelabelHasClasslessItems
              ? 'The model proposed regions without classes. Assign a class to each proposal or delete it before submitting.'
              : 'A model-generated pre-label is shown. Approve it as-is or correct it with the tools below.'}
          </Alert>
        )}

        {/* Modality-exclusive tools (req 7.6) */}
        {taskType === 'Classification' && (
          <FormField label="Image label">
            <SegmentedControl
              selectedId={classification}
              onChange={({ detail }) => {
                setClassification(detail.selectedId);
                setEdited(true);
                setValidationErrors([]);
              }}
              label="Classification selection"
              options={[
                { text: 'Normal', id: 'normal' },
                { text: 'Anomaly', id: 'anomaly' },
              ]}
            />
          </FormField>
        )}

        {taskType === 'ObjectDetection' && (
          <FormField
            label="Box class"
            description="Drag on the image to draw a box. New boxes get the selected class."
          >
            <Select
              selectedOption={
                classOptions[selectedClassIndex] ?? classOptions[0] ?? null
              }
              onChange={({ detail }) =>
                setSelectedClassIndex(Number(detail.selectedOption.value))
              }
              options={classOptions}
            />
          </FormField>
        )}

        {taskType === 'Segmentation' && (
          <SpaceBetween size="s" direction="horizontal">
            <FormField label="Tool">
              <SegmentedControl
                selectedId={segTool}
                onChange={({ detail }) =>
                  setSegTool(detail.selectedId as 'brush' | 'eraser')
                }
                label="Painting tool"
                options={[
                  { text: 'Brush', id: 'brush' },
                  { text: 'Eraser', id: 'eraser' },
                ]}
              />
            </FormField>
            <FormField label="Class">
              <Select
                selectedOption={
                  classOptions[selectedClassIndex] ?? classOptions[0] ?? null
                }
                onChange={({ detail }) =>
                  setSelectedClassIndex(Number(detail.selectedOption.value))
                }
                options={classOptions}
                disabled={segTool === 'eraser'}
              />
            </FormField>
            <FormField label={`Brush size (${brushSize}px)`}>
              <Slider
                value={brushSize}
                onChange={({ detail }) => setBrushSize(detail.value)}
                min={2}
                max={128}
                step={2}
              />
            </FormField>
          </SpaceBetween>
        )}

        {/* Image with layered canvas */}
        {imageFailed ? (
          <Alert type="error" header="Image unavailable">
            The task image could not be loaded.
          </Alert>
        ) : (
          <div style={{ position: 'relative', display: 'inline-block', maxWidth: '100%' }}>
            <img
              src={currentUrl}
              alt="Image to label"
              onLoad={handleImageLoad}
              onError={handleImageError}
              style={{ display: 'block', maxWidth: '100%' }}
              draggable={false}
            />
            <canvas
              ref={overlayRef}
              data-testid="annotation-overlay"
              onPointerDown={handlePointerDown}
              onPointerMove={handlePointerMove}
              onPointerUp={handlePointerUp}
              style={{
                position: 'absolute',
                inset: 0,
                width: '100%',
                height: '100%',
                cursor:
                  taskType === 'Classification'
                    ? 'default'
                    : taskType === 'ObjectDetection'
                      ? 'crosshair'
                      : segTool === 'brush'
                        ? 'crosshair'
                        : 'cell',
                touchAction: 'none',
              }}
            />
          </div>
        )}

        {/* Box list (Object_Detection only) */}
        {taskType === 'ObjectDetection' && boxes.length > 0 && (
          <SpaceBetween size="xs">
            {boxes.map((box, i) => (
              <SpaceBetween key={box.id} size="xs" direction="horizontal">
                <Box variant="span">
                  Box {i + 1} ({box.left}, {box.top}) {box.width}×{box.height}
                </Box>
                <Select
                  selectedOption={
                    box.class ? { label: box.class, value: box.class } : null
                  }
                  placeholder="Choose a class"
                  onChange={({ detail }) => {
                    if (detail.selectedOption.value) {
                      setBoxClass(box.id, detail.selectedOption.value);
                      setValidationErrors([]);
                    }
                  }}
                  options={classSelectOptions}
                  invalid={!box.class}
                />
                <Button
                  variant="icon"
                  iconName="remove"
                  ariaLabel={`Delete box ${i + 1}`}
                  onClick={() => deleteBox(box.id)}
                />
              </SpaceBetween>
            ))}
          </SpaceBetween>
        )}

        {/* Classless SAM proposals (Segmentation only) */}
        {taskType === 'Segmentation' && proposals.length > 0 && (
          <SpaceBetween size="xs">
            {proposals.map((proposal, i) => (
              <SpaceBetween key={proposal.id} size="xs" direction="horizontal">
                <Box variant="span">Proposed region {i + 1} (unclassified)</Box>
                <Select
                  selectedOption={null}
                  placeholder="Assign a class"
                  onChange={({ detail }) => {
                    const index = labelSet.indexOf(
                      detail.selectedOption.value ?? ''
                    );
                    if (index >= 0) {
                      assignProposalClass(proposal.id, index);
                      setValidationErrors([]);
                    }
                  }}
                  options={classSelectOptions}
                />
                <Button
                  variant="icon"
                  iconName="remove"
                  ariaLabel={`Delete proposed region ${i + 1}`}
                  onClick={() => deleteProposal(proposal.id)}
                />
              </SpaceBetween>
            ))}
          </SpaceBetween>
        )}

        {/* Client-side incomplete-submission blocking (req 7.8) */}
        {validationErrors.length > 0 && (
          <Alert type="error" header="The label is incomplete">
            <ul style={{ margin: 0, paddingInlineStart: '1.2em' }}>
              {validationErrors.map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
          </Alert>
        )}

        <SpaceBetween size="xs" direction="horizontal">
          {showApproveAsIs && (
            <Button
              onClick={handleSubmit}
              loading={submitting}
              disabled={!imageSize}
            >
              Approve pre-label as-is
            </Button>
          )}
          <Button
            variant="primary"
            onClick={handleSubmit}
            loading={submitting}
            disabled={!imageSize && taskType !== 'Classification'}
          >
            Submit label
          </Button>
        </SpaceBetween>
      </SpaceBetween>
    );
  }
);

export default AnnotationCanvas;
