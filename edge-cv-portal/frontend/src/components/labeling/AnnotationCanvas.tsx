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
 * - InstanceSegmentation (rfdetr-seg-support Requirements 2.2, 2.3; DD9):
 *   a polygon tool — click to place vertices, click the first vertex or
 *   double-click to close (refused below three distinct vertices), drag a
 *   vertex to edit, select / reclass / delete / undo per instance. Each
 *   instance is drawn with its class fill at 40 % alpha and an outline
 *   from a second, instance-indexed palette so touching instances of one
 *   class stay distinguishable; instances may overlap. The polygon is
 *   client state only: on save it is rasterised by an even-odd scanline
 *   fill and RLE-encoded, with a client-side tight box sent as advisory
 *   (the server recomputes it). Pre-label instances arrive as RLE without
 *   vertices: they render from the decoded mask and can be reclassed or
 *   deleted, not reshaped. The brush/eraser stay Segmentation-only.
 *
 * Pre_Labels initialize the editable annotation state and can be approved
 * as-is or corrected (req 8.3). Classless SAM proposals render as neutral
 * geometry and must be classified or deleted before submit. Incomplete
 * submissions are blocked client-side with the missing element identified
 * (req 7.8). When the presigned image URL expires (load error or expiry
 * timestamp) the component invokes `onImageUrlRefresh` and swaps the image
 * without touching annotation state (req 12.7).
 *
 * A task with a non-empty Pre_Label additionally offers the client-only
 * Clear_Prelabels_Control (testid `clear-prelabels`): it removes exactly
 * the Prelabel_Origin state — `prelabel-box-` boxes even when re-classed,
 * still-intact prelabel-painted pixels (tracked against a bitmap copy
 * taken at initialization), classless proposals, and an untouched
 * prelabel classification — while every user edit survives. Clearing
 * takes a full-state snapshot first and swaps the control for the
 * Restore_Control (testid `restore-prelabels`), a one-level session
 * undo. No API call is involved; the stored Pre_Label is never modified
 * (labeling-job-cleanup-work-stealing-and-podium req 9.1–9.8).
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
  DdaInstance,
  DdaMaskRegion,
} from '../../services/api';
import {
  decodeRleColumnMajor,
  encodeRleColumnMajor,
  parseRleCounts,
} from '../../utils/rle';

/**
 * The Canonical_RLE codec lives in `utils/rle.ts` (rfdetr-seg-support
 * DD20) and is re-exported here so existing callers and tests of the
 * canvas module are unchanged.
 */
export { decodeRleColumnMajor, encodeRleColumnMajor, parseRleCounts };

/** Task-type identifiers used by DDA labeling jobs. */
export type LabelingModality =
  | 'Classification'
  | 'Segmentation'
  | 'ObjectDetection'
  | 'InstanceSegmentation';

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

/**
 * Instance_Segmentation fill alpha: 40 % (Requirement 2.3), applied both
 * to the rasterised pre-label masks and to the polygon fills.
 */
const INSTANCE_FILL_ALPHA = 102;
/**
 * Second palette for instance outlines, indexed by instance position, so
 * two touching instances of one class remain distinguishable on screen
 * (Requirement 2.3). Deliberately disjoint from CLASS_PALETTE.
 */
export const INSTANCE_OUTLINE_PALETTE = [
  '#FFFFFF',
  '#000000',
  '#FFFF00',
  '#00FFFF',
  '#FF00FF',
  '#FFA500',
  '#7FFF00',
  '#FF4500',
  '#1E1E8F',
  '#A0522D',
] as const;
/** Minimum distinct vertices before a polygon can be closed (Req 2.2). */
export const POLYGON_MIN_VERTICES = 3;
const POLYGON_TOO_FEW_VERTICES_HINT =
  'A polygon needs at least three distinct vertices before it can be closed.';
const POLYGON_DRAW_HINT =
  'Click on the image to place vertices; click the first vertex or double-click to close the polygon.';
const PRELABEL_INSTANCE_HINT =
  'Pre-labelled instances are shown from their masks and keep their outline; assign or change their class, or delete them.';

/* ------------------------------------------------------------------ */
/* Polygon helpers (Instance_Segmentation)                             */
/* ------------------------------------------------------------------ */

export type PolygonVertex = [number, number];

/**
 * Number of distinct vertices after rounding to integer pixels — the
 * "fewer than three distinct vertices" rule of Requirement 2.2 counts a
 * repeated click on one spot once.
 */
export function distinctVertexCount(vertices: PolygonVertex[]): number {
  const seen = new Set<string>();
  for (const [x, y] of vertices) {
    seen.add(`${Math.round(x)},${Math.round(y)}`);
  }
  return seen.size;
}

/**
 * Rasterise a closed polygon (image pixel coordinates) into a row-major
 * binary mask with an even-odd scanline fill (DD9): a pixel is inside when
 * its centre crosses an odd number of edges along its row. Self-touching
 * rings therefore leave holes rather than double-filling, and vertices on
 * or beyond the image edge are clipped to it.
 */
export function rasterizePolygonEvenOdd(
  vertices: PolygonVertex[],
  width: number,
  height: number
): Uint8Array {
  const mask = new Uint8Array(width * height);
  const n = vertices.length;
  if (n < POLYGON_MIN_VERTICES) return mask;
  for (let y = 0; y < height; y++) {
    const cy = y + 0.5;
    const crossings: number[] = [];
    for (let i = 0; i < n; i++) {
      const [x0, y0] = vertices[i];
      const [x1, y1] = vertices[(i + 1) % n];
      if ((y0 <= cy && y1 > cy) || (y1 <= cy && y0 > cy)) {
        crossings.push(x0 + ((cy - y0) * (x1 - x0)) / (y1 - y0));
      }
    }
    if (crossings.length < 2) continue;
    crossings.sort((a, b) => a - b);
    for (let k = 0; k + 1 < crossings.length; k += 2) {
      // Pixel x is inside when its centre x + 0.5 lies in [start, end).
      const start = Math.max(0, Math.ceil(crossings[k] - 0.5));
      const end = Math.min(width - 1, Math.ceil(crossings[k + 1] - 0.5) - 1);
      for (let x = start; x <= end; x++) {
        mask[y * width + x] = 1;
      }
    }
  }
  return mask;
}

/**
 * The tight bounding box of a row-major binary mask, or null when the mask
 * has no foreground pixel. Sent with each instance as advisory — the
 * server derives the authoritative box from the RLE (Requirement 2.2).
 */
export function tightBoxFromMask(
  mask: Uint8Array,
  width: number,
  height: number
): { left: number; top: number; width: number; height: number } | null {
  let left = width;
  let top = height;
  let right = -1;
  let bottom = -1;
  for (let y = 0; y < height; y++) {
    const row = y * width;
    for (let x = 0; x < width; x++) {
      if (mask[row + x]) {
        if (x < left) left = x;
        if (x > right) right = x;
        if (y < top) top = y;
        if (y > bottom) bottom = y;
      }
    }
  }
  if (right < 0) return null;
  return { left, top, width: right - left + 1, height: bottom - top + 1 };
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

/**
 * One Instance_Segmentation instance in canvas state (rfdetr-seg-support
 * Requirement 2.3, DD9). A user-drawn instance carries its polygon
 * `vertices` (rasterised to RLE only on save); a pre-label instance
 * carries `rle` and no vertices, so it renders from `mask` and can be
 * reclassed or deleted but not reshaped. `origin` is what the
 * Clear_Prelabels_Control removes by (`'prelabel'`), unchanged by a reclass.
 */
export interface EditableInstance {
  id: string;
  class: string | null;
  vertices: PolygonVertex[];
  rle?: string;
  box?: { left: number; top: number; width: number; height: number };
  origin: 'prelabel' | 'user';
  /** Decoded `rle` of a pre-label instance (row-major), for display. */
  mask?: Uint8Array;
}

/**
 * Prelabel_Snapshot — the full annotation state captured at the moment the
 * Clear_Prelabels_Control is activated, backing the one-level restore
 * (labeling-job-cleanup-work-stealing-and-podium req 9.5).
 */
interface PrelabelSnapshot {
  classification: string | null;
  boxes: EditableBox[];
  /** Deep copy of the label-indexed segmentation bitmap, when one exists. */
  bitmap: Uint8Array | null;
  proposals: SegProposal[];
  instances: EditableInstance[];
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

    /* ---------------- instance (polygon) state (rfdetr-seg Req 2.3) --- */
    const [instances, setInstances] = useState<EditableInstance[]>([]);
    const instancesInitializedRef = useRef(false);
    /** Vertices of the polygon being drawn, in click order. */
    const [draftVertices, setDraftVertices] = useState<PolygonVertex[]>([]);
    const [selectedInstanceId, setSelectedInstanceId] = useState<
      string | null
    >(null);
    const [polygonHint, setPolygonHint] = useState<string | null>(null);
    // Undo stack over committed instance edits (add, reclass, delete,
    // vertex drag): the state before each edit, newest last.
    const instanceHistoryRef = useRef<EditableInstance[][]>([]);
    const [instanceHistoryDepth, setInstanceHistoryDepth] = useState(0);
    // The vertex being dragged: its instance, index, and the instances
    // array as it was before the drag started (pushed to history on drop).
    const vertexDragRef = useRef<{
      instanceId: string;
      vertexIndex: number;
      before: EditableInstance[];
      moved: boolean;
    } | null>(null);
    const instanceCounterRef = useRef(0);

    /* ---------------- clear pre-labels state (req 9.1–9.5) --------- */
    // Copy of the label bitmap taken right after the Segmentation
    // Pre_Label painting in handleImageLoad — the provenance record that
    // makes "pixel still holds the class the Pre_Label initialized it
    // to" a comparable fact (req 9.3).
    const prelabelBitmapRef = useRef<Uint8Array | null>(null);
    // Prelabel_Snapshot backing the one-level restore (req 9.5).
    const prelabelSnapshotRef = useRef<PrelabelSnapshot | null>(null);
    const [prelabelsCleared, setPrelabelsCleared] = useState(false);

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
            const mask = decodeRleColumnMajor(
              parseRleCounts(region.rle), width, height);
            if (classIndex >= 0) {
              for (let p = 0; p < mask.length; p++) {
                if (mask[p]) bitmap[p] = classIndex + 1;
              }
            } else {
              classless.push({ id: `proposal-${i}`, mask });
            }
          });
          segBitmapRef.current = bitmap;
          // Provenance record for clear-prelabels: the bitmap exactly as
          // the Pre_Label painted it, before any user stroke (req 9.3).
          prelabelBitmapRef.current = bitmap.slice();
          setProposals(classless);
          setSegVersion((v) => v + 1);
        }

        // Pre-label instances need the image dimensions to decode their
        // RLE, so they are initialized here, once, like Segmentation
        // (rfdetr-seg-support Req 2.4: one region -> one instance, class
        // carried and null kept).
        if (
          taskType === 'InstanceSegmentation' &&
          !instancesInitializedRef.current
        ) {
          instancesInitializedRef.current = true;
          const fromPrelabel: EditableInstance[] = [];
          (prelabel?.instances ?? []).forEach((instance: DdaInstance, i) => {
            if (!instance.rle) return;
            const mask = decodeRleColumnMajor(
              parseRleCounts(instance.rle), width, height);
            fromPrelabel.push({
              id: `prelabel-instance-${i}`,
              class: instance.class ?? null,
              vertices: [],
              rle: instance.rle,
              box: instance.box ?? tightBoxFromMask(mask, width, height) ?? undefined,
              origin: 'prelabel',
              mask,
            });
          });
          setInstances(fromPrelabel);
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

    /* ---------------- instance (polygon) editing (Req 2.2, 2.3) ----- */
    /**
     * Vertex hit radius in image pixels (1 % of the longer image side,
     * at least 2): large enough to grab on a scaled display, small enough
     * not to swallow neighbouring vertices.
     */
    const vertexHitRadius = imageSize
      ? Math.max(2, Math.round(Math.max(imageSize.width, imageSize.height) / 100))
      : 2;

    /**
     * A user polygon with its display box re-derived from the rasterised
     * ring (the same derivation the save path applies; the server
     * recomputes it from the RLE regardless, Req 2.2).
     */
    const withDerivedBox = useCallback(
      (instance: EditableInstance): EditableInstance => {
        if (instance.vertices.length === 0 || !imageSize) return instance;
        const box = tightBoxFromMask(
          rasterizePolygonEvenOdd(
            instance.vertices, imageSize.width, imageSize.height),
          imageSize.width,
          imageSize.height
        );
        return { ...instance, box: box ?? undefined };
      },
      [imageSize]
    );

    /** Replace the instance list, recording the previous state for undo. */
    const commitInstances = useCallback(
      (next: EditableInstance[], previous: EditableInstance[]) => {
        instanceHistoryRef.current.push(previous);
        setInstanceHistoryDepth(instanceHistoryRef.current.length);
        setInstances(next);
        setEdited(true);
        setValidationErrors([]);
      },
      []
    );

    const closeDraftPolygon = useCallback((): boolean => {
      if (distinctVertexCount(draftVertices) < POLYGON_MIN_VERTICES) {
        // Req 2.2: the close affordance is refused below three distinct
        // vertices and the labeler is told why.
        setPolygonHint(POLYGON_TOO_FEW_VERTICES_HINT);
        return false;
      }
      instanceCounterRef.current += 1;
      const id = `instance-${Date.now()}-${instanceCounterRef.current}`;
      commitInstances(
        [
          ...instances,
          withDerivedBox({
            id,
            class: labelSet[selectedClassIndex] ?? null,
            vertices: draftVertices,
            origin: 'user',
          }),
        ],
        instances
      );
      setDraftVertices([]);
      setSelectedInstanceId(id);
      setPolygonHint(null);
      return true;
    }, [
      draftVertices,
      instances,
      labelSet,
      selectedClassIndex,
      commitInstances,
      withDerivedBox,
    ]);

    const setInstanceClass = useCallback(
      (id: string, className: string) => {
        commitInstances(
          instances.map((instance) =>
            instance.id === id ? { ...instance, class: className } : instance
          ),
          instances
        );
      },
      [instances, commitInstances]
    );

    const deleteInstance = useCallback(
      (id: string) => {
        commitInstances(
          instances.filter((instance) => instance.id !== id),
          instances
        );
        setSelectedInstanceId((current) => (current === id ? null : current));
      },
      [instances, commitInstances]
    );

    /**
     * Undo: an in-progress draft loses its last vertex first; otherwise the
     * most recent committed instance edit is reverted.
     */
    const undoInstanceEdit = useCallback(() => {
      if (draftVertices.length > 0) {
        setDraftVertices(draftVertices.slice(0, -1));
        setPolygonHint(null);
        return;
      }
      const previous = instanceHistoryRef.current.pop();
      setInstanceHistoryDepth(instanceHistoryRef.current.length);
      if (previous) {
        setInstances(previous);
        setSelectedInstanceId((current) =>
          previous.some((instance) => instance.id === current) ? current : null
        );
        setValidationErrors([]);
      }
    }, [draftVertices]);

    /** The user-polygon vertex under `pos`, if any (selected instance first). */
    const hitVertex = useCallback(
      (pos: { x: number; y: number }) => {
        const ordered = [
          ...instances.filter((instance) => instance.id === selectedInstanceId),
          ...instances.filter((instance) => instance.id !== selectedInstanceId),
        ];
        for (const instance of ordered) {
          for (let index = 0; index < instance.vertices.length; index++) {
            const [vx, vy] = instance.vertices[index];
            if (Math.hypot(pos.x - vx, pos.y - vy) <= vertexHitRadius) {
              return { instanceId: instance.id, vertexIndex: index };
            }
          }
        }
        return null;
      },
      [instances, selectedInstanceId, vertexHitRadius]
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
        } else if (taskType === 'InstanceSegmentation') {
          // 1. A vertex of an existing polygon under the pointer: select
          //    its instance and start dragging it (Req 2.3 drag-to-edit).
          if (draftVertices.length === 0) {
            const hit = hitVertex(pos);
            if (hit) {
              vertexDragRef.current = { ...hit, before: instances, moved: false };
              setSelectedInstanceId(hit.instanceId);
              return;
            }
          }
          // 2. Clicking the first vertex of the draft closes the ring.
          if (draftVertices.length > 0) {
            const [fx, fy] = draftVertices[0];
            if (Math.hypot(pos.x - fx, pos.y - fy) <= vertexHitRadius) {
              closeDraftPolygon();
              return;
            }
            // A repeated click on the last vertex (the first half of a
            // double-click) adds no duplicate vertex.
            const [lx, ly] = draftVertices[draftVertices.length - 1];
            if (Math.round(lx) === Math.round(pos.x)
                && Math.round(ly) === Math.round(pos.y)) {
              return;
            }
          }
          // 3. Otherwise add a vertex to the draft polygon.
          setDraftVertices([...draftVertices, [pos.x, pos.y]]);
          setPolygonHint(null);
          setSelectedInstanceId(null);
        }
      },
      [
        taskType,
        toImageCoords,
        paintTo,
        draftVertices,
        instances,
        hitVertex,
        vertexHitRadius,
        closeDraftPolygon,
      ]
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
        } else if (taskType === 'InstanceSegmentation' && vertexDragRef.current) {
          const drag = vertexDragRef.current;
          drag.moved = true;
          setInstances((prev) =>
            prev.map((instance) =>
              instance.id === drag.instanceId
                ? {
                    ...instance,
                    vertices: instance.vertices.map((vertex, index) =>
                      index === drag.vertexIndex ? [pos.x, pos.y] : vertex
                    ),
                  }
                : instance
            )
          );
        }
      },
      [taskType, toImageCoords, paintTo]
    );

    const handleDoubleClick = useCallback(() => {
      if (taskType !== 'InstanceSegmentation') return;
      if (draftVertices.length === 0) return;
      closeDraftPolygon();
    }, [taskType, draftVertices, closeDraftPolygon]);

    const handlePointerUp = useCallback(
      (event: React.PointerEvent<HTMLCanvasElement>) => {
        event.currentTarget.releasePointerCapture(event.pointerId);
        if (taskType === 'InstanceSegmentation' && vertexDragRef.current) {
          const drag = vertexDragRef.current;
          vertexDragRef.current = null;
          if (drag.moved) {
            // One undo step per completed drag; the display box follows
            // the new ring.
            instanceHistoryRef.current.push(drag.before);
            setInstanceHistoryDepth(instanceHistoryRef.current.length);
            setInstances((prev) =>
              prev.map((instance) =>
                instance.id === drag.instanceId
                  ? withDerivedBox(instance)
                  : instance
              )
            );
            setEdited(true);
            setValidationErrors([]);
          }
          return;
        }
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
      [taskType, draftBox, imageSize, labelSet, selectedClassIndex, withDerivedBox]
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

    /* ---------------- clear / restore pre-labels (req 9.2–9.5) ----- */
    const handleClearPrelabels = useCallback(() => {
      // Prelabel_Snapshot first, so restore is an exact one-level undo
      // (req 9.5). Boxes and proposals are only ever replaced immutably,
      // so holding the current arrays is safe; the segmentation bitmap
      // is mutated in place and needs a deep copy.
      prelabelSnapshotRef.current = {
        classification,
        boxes,
        bitmap: segBitmapRef.current ? segBitmapRef.current.slice() : null,
        proposals,
        instances,
      };
      if (taskType === 'InstanceSegmentation') {
        // Prelabel_Origin instances keep `origin: 'prelabel'` through class
        // edits, so a re-classed proposal is removed too; user-drawn
        // polygons survive (rfdetr-seg-support design section 2).
        setInstances((prev) =>
          prev.filter((instance) => instance.origin !== 'prelabel')
        );
        setSelectedInstanceId(null);
      } else if (taskType === 'Classification') {
        // Deselect only a selection still equal to the Pre_Label's
        // label; a selection the labeler changed is their own work
        // (req 9.4).
        if (classification !== null && classification === prelabel?.label) {
          setClassification(null);
        }
      } else if (taskType === 'ObjectDetection') {
        // Prelabel_Origin boxes keep their id through class edits, so
        // the id prefix drops them even when re-classed; user-drawn
        // boxes (`box-{timestamp}-{n}`) survive (req 9.2).
        setBoxes((prev) =>
          prev.filter((b) => !b.id.startsWith('prelabel-box-'))
        );
      } else {
        // Segmentation: zero exactly the pixels that still hold the
        // class the Pre_Label initialized them to (the provenance copy
        // makes this comparable); pixels the labeler painted or
        // repainted differ from the copy and survive. Remaining
        // classless proposals are removed (req 9.3).
        const bitmap = segBitmapRef.current;
        const prelabelBitmap = prelabelBitmapRef.current;
        if (bitmap && prelabelBitmap) {
          for (let p = 0; p < bitmap.length; p++) {
            if (prelabelBitmap[p] !== 0 && bitmap[p] === prelabelBitmap[p]) {
              bitmap[p] = 0;
            }
          }
        }
        setProposals([]);
        setSegVersion((v) => v + 1);
      }
      setPrelabelsCleared(true);
    }, [taskType, classification, boxes, proposals, instances, prelabel]);

    const handleRestorePrelabels = useCallback(() => {
      const snapshot = prelabelSnapshotRef.current;
      if (!snapshot) return;
      // Reinstate the Prelabel_Snapshot wholesale (req 9.5).
      setClassification(snapshot.classification);
      setBoxes(snapshot.boxes);
      if (snapshot.bitmap) {
        segBitmapRef.current = snapshot.bitmap.slice();
      }
      setProposals(snapshot.proposals);
      setInstances(snapshot.instances);
      setSegVersion((v) => v + 1);
      setPrelabelsCleared(false);
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
      } else if (taskType === 'InstanceSegmentation') {
        const lineWidth = Math.max(2, Math.round(width / 400));
        const handle = Math.max(3, Math.round(width / 200));
        const fillFor = (className: string | null) => {
          const classIndex = className ? labelSet.indexOf(className) : -1;
          const [r, g, b] = hexToRgb(
            classIndex >= 0
              ? CLASS_PALETTE[classIndex % CLASS_PALETTE.length]
              : PROPOSAL_COLOR
          );
          return [r, g, b] as [number, number, number];
        };
        // Pre-label instances (masks, no vertices): one ImageData with
        // the class fill at 40 % alpha and the boundary pixels in the
        // instance's outline colour (Req 2.3).
        const maskInstances = instances.filter((instance) => instance.mask);
        if (maskInstances.length > 0) {
          const imageData = ctx.createImageData(width, height);
          const data = imageData.data;
          for (const instance of maskInstances) {
            const index = instances.indexOf(instance);
            const [fr, fg, fb] = fillFor(instance.class);
            const [or, og, ob] = hexToRgb(
              INSTANCE_OUTLINE_PALETTE[index % INSTANCE_OUTLINE_PALETTE.length]
            );
            const mask = instance.mask!;
            for (let p = 0; p < mask.length; p++) {
              if (!mask[p]) continue;
              const x = p % width;
              const y = (p - x) / width;
              const boundary =
                x === 0 || y === 0 || x === width - 1 || y === height - 1 ||
                !mask[p - 1] || !mask[p + 1] || !mask[p - width] || !mask[p + width];
              const o = p * 4;
              if (boundary) {
                data[o] = or;
                data[o + 1] = og;
                data[o + 2] = ob;
                data[o + 3] = 255;
              } else {
                data[o] = fr;
                data[o + 1] = fg;
                data[o + 2] = fb;
                data[o + 3] = INSTANCE_FILL_ALPHA;
              }
            }
          }
          ctx.putImageData(imageData, 0, 0);
        }
        // User polygons: even-odd fill at 40 % alpha, outline from the
        // instance-indexed palette, vertex handles on the selection.
        ctx.lineWidth = lineWidth;
        instances.forEach((instance, index) => {
          if (instance.vertices.length === 0) return;
          const [fr, fg, fb] = fillFor(instance.class);
          const outline =
            INSTANCE_OUTLINE_PALETTE[index % INSTANCE_OUTLINE_PALETTE.length];
          ctx.beginPath();
          instance.vertices.forEach(([vx, vy], vertexIndex) => {
            if (vertexIndex === 0) ctx.moveTo(vx, vy);
            else ctx.lineTo(vx, vy);
          });
          ctx.closePath();
          ctx.fillStyle = `rgba(${fr}, ${fg}, ${fb}, ${INSTANCE_FILL_ALPHA / 255})`;
          ctx.fill('evenodd');
          ctx.strokeStyle = outline;
          ctx.lineWidth =
            instance.id === selectedInstanceId ? lineWidth * 2 : lineWidth;
          ctx.setLineDash(instance.class ? [] : [8, 6]);
          ctx.stroke();
          ctx.setLineDash([]);
          if (instance.id === selectedInstanceId) {
            ctx.fillStyle = outline;
            for (const [vx, vy] of instance.vertices) {
              ctx.fillRect(vx - handle, vy - handle, handle * 2, handle * 2);
            }
          }
        });
        // The draft ring: dashed edges, handles, the first vertex marked
        // once the ring can be closed.
        if (draftVertices.length > 0) {
          ctx.lineWidth = lineWidth;
          ctx.strokeStyle = '#0972D3';
          ctx.setLineDash([6, 4]);
          ctx.beginPath();
          draftVertices.forEach(([vx, vy], vertexIndex) => {
            if (vertexIndex === 0) ctx.moveTo(vx, vy);
            else ctx.lineTo(vx, vy);
          });
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = '#0972D3';
          draftVertices.forEach(([vx, vy], vertexIndex) => {
            const size =
              vertexIndex === 0 &&
              distinctVertexCount(draftVertices) >= POLYGON_MIN_VERTICES
                ? handle * 2
                : handle;
            ctx.fillRect(vx - size, vy - size, size * 2, size * 2);
          });
        }
      }
      // Classification renders no overlay.
    }, [
      taskType,
      imageSize,
      boxes,
      draftBox,
      proposals,
      labelSet,
      segVersion,
      instances,
      draftVertices,
      selectedInstanceId,
    ]);

    /* ---------------- annotation assembly / validation ------------- */
    const buildAnnotation = useCallback((): DdaAnnotation => {
      // Every submission is modality-tagged: the server rejects
      // annotations whose modality does not match the job's task type
      // (Req 7.8), so taskType always rides along.
      if (taskType === 'Classification') {
        return { modality: taskType, label: classification ?? undefined };
      }
      if (taskType === 'ObjectDetection') {
        return {
          modality: taskType,
          boxes: boxes.map(({ id: _id, ...box }) =>
            imageSize
              ? { class: box.class, ...clampBoxToImage(box, imageSize.width, imageSize.height) }
              : box
          ),
          // The server validates box bounds against image_size.
          ...(imageSize
            ? { image_size: { width: imageSize.width, height: imageSize.height } }
            : {}),
          image_width: imageSize?.width,
          image_height: imageSize?.height,
        };
      }
      if (taskType === 'InstanceSegmentation') {
        // Each polygon is rasterised by the even-odd scanline fill and
        // RLE-encoded on save (DD9); pre-label instances keep their RLE.
        // The tight box rides along as advisory — the server recomputes it.
        const serialized: DdaInstance[] = [];
        if (imageSize) {
          const { width, height } = imageSize;
          for (const instance of instances) {
            const mask =
              instance.vertices.length > 0
                ? rasterizePolygonEvenOdd(instance.vertices, width, height)
                : instance.mask ??
                  decodeRleColumnMajor(
                    parseRleCounts(instance.rle ?? ''), width, height);
            const rle =
              instance.vertices.length > 0 || !instance.rle
                ? encodeRleColumnMajor(mask, width, height, 1).join(' ')
                : instance.rle;
            const box = tightBoxFromMask(mask, width, height);
            serialized.push({
              class: instance.class,
              rle,
              ...(box ? { box } : {}),
            });
          }
        }
        return {
          modality: taskType,
          instances: serialized,
          ...(imageSize
            ? { image_size: { width: imageSize.width, height: imageSize.height } }
            : {}),
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
              // The submit validator requires the canonical non-empty
              // space-separated counts string (not a counts array), so
              // join the counts for submission.
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
        modality: taskType,
        regions,
        image_width: imageSize?.width,
        image_height: imageSize?.height,
      };
    }, [taskType, classification, boxes, imageSize, labelSet, instances]);

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
      } else if (taskType === 'InstanceSegmentation') {
        if (draftVertices.length > 0) {
          missing.push(
            'An unfinished polygon is open: close it (three or more vertices) or undo its vertices.'
          );
        }
        instances.forEach((instance, i) => {
          if (!instance.class || !labelSet.includes(instance.class)) {
            missing.push(
              `Instance ${i + 1} has no class assigned: pick a class or delete the instance.`
            );
          }
          if (
            instance.vertices.length > 0 &&
            imageSize &&
            tightBoxFromMask(
              rasterizePolygonEvenOdd(
                instance.vertices, imageSize.width, imageSize.height),
              imageSize.width,
              imageSize.height
            ) === null
          ) {
            missing.push(
              `Instance ${i + 1} covers no pixel: reshape or delete the instance.`
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
    }, [
      taskType,
      classification,
      boxes,
      proposals,
      labelSet,
      instances,
      draftVertices,
      imageSize,
    ]);

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
        (prelabel.regions?.length ?? 0) > 0 ||
        (prelabel.instances?.length ?? 0) > 0);
    const prelabelHasClasslessItems =
      (prelabel?.boxes ?? []).some((b) => b.class === null) ||
      (prelabel?.regions ?? []).some((r) => r.class === null) ||
      (prelabel?.instances ?? []).some((i) => i.class === null);
    const showApproveAsIs = hasPrelabel && !edited;
    const hasPrelabelInstances = instances.some(
      (instance) => instance.origin === 'prelabel'
    );
    const draftDistinctCount = distinctVertexCount(draftVertices);
    const canCloseDraft = draftDistinctCount >= POLYGON_MIN_VERTICES;
    const canUndoInstances =
      draftVertices.length > 0 || instanceHistoryDepth > 0;

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

        {taskType === 'InstanceSegmentation' && (
          <SpaceBetween size="s">
            <SpaceBetween size="s" direction="horizontal">
              <FormField
                label="Instance class"
                description="New polygons get the selected class. Click to place vertices; click the first vertex or double-click to close."
              >
                <Select
                  data-testid="instance-class-select"
                  selectedOption={
                    classOptions[selectedClassIndex] ?? classOptions[0] ?? null
                  }
                  onChange={({ detail }) =>
                    setSelectedClassIndex(Number(detail.selectedOption.value))
                  }
                  options={classOptions}
                />
              </FormField>
              <FormField label="Polygon">
                <SpaceBetween size="xs" direction="horizontal">
                  <Button
                    data-testid="close-polygon"
                    onClick={() => closeDraftPolygon()}
                    disabled={!canCloseDraft}
                  >
                    Close polygon
                  </Button>
                  <Button
                    data-testid="undo-instance-edit"
                    onClick={undoInstanceEdit}
                    disabled={!canUndoInstances}
                  >
                    Undo
                  </Button>
                </SpaceBetween>
              </FormField>
            </SpaceBetween>
            <Box
              variant="small"
              color="text-body-secondary"
              data-testid="polygon-hint"
            >
              {polygonHint ??
                (draftVertices.length > 0 && !canCloseDraft
                  ? `${draftDistinctCount} of ${POLYGON_MIN_VERTICES} distinct vertices placed. ${POLYGON_TOO_FEW_VERTICES_HINT}`
                  : POLYGON_DRAW_HINT)}
            </Box>
            {hasPrelabelInstances && (
              <Box
                variant="small"
                color="text-body-secondary"
                data-testid="prelabel-instance-hint"
              >
                {PRELABEL_INSTANCE_HINT}
              </Box>
            )}
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
              onDoubleClick={handleDoubleClick}
              style={{
                position: 'absolute',
                inset: 0,
                width: '100%',
                height: '100%',
                cursor:
                  taskType === 'Classification'
                    ? 'default'
                    : taskType === 'ObjectDetection' ||
                        taskType === 'InstanceSegmentation'
                      ? 'crosshair'
                      : segTool === 'brush'
                        ? 'crosshair'
                        : 'cell',
                touchAction: 'none',
              }}
            />
          </div>
        )}

        {/* Instance list (Instance_Segmentation only, Req 2.3) */}
        {taskType === 'InstanceSegmentation' && instances.length > 0 && (
          <SpaceBetween size="xs">
            {instances.map((instance, i) => (
              <SpaceBetween
                key={instance.id}
                size="xs"
                direction="horizontal"
              >
                <Button
                  variant={
                    instance.id === selectedInstanceId ? 'primary' : 'normal'
                  }
                  data-testid={`select-instance-${i + 1}`}
                  onClick={() => setSelectedInstanceId(instance.id)}
                >
                  {`Instance ${i + 1}`}
                </Button>
                <Box variant="span">
                  {instance.vertices.length > 0
                    ? `${instance.vertices.length} vertices`
                    : 'pre-label mask'}
                  {instance.box
                    ? ` (${instance.box.left}, ${instance.box.top}) ${instance.box.width}×${instance.box.height}`
                    : ''}
                </Box>
                <Select
                  selectedOption={
                    instance.class
                      ? { label: instance.class, value: instance.class }
                      : null
                  }
                  placeholder="Choose a class"
                  onChange={({ detail }) => {
                    if (detail.selectedOption.value) {
                      setInstanceClass(instance.id, detail.selectedOption.value);
                    }
                  }}
                  options={classSelectOptions}
                  invalid={!instance.class}
                />
                <Button
                  variant="icon"
                  iconName="remove"
                  ariaLabel={`Delete instance ${i + 1}`}
                  onClick={() => deleteInstance(instance.id)}
                />
              </SpaceBetween>
            ))}
          </SpaceBetween>
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
          {/* Clear/restore pre-labels (req 9.1, 9.5): Clear renders
              exactly while the task carries a non-empty Pre_Label and it
              has not been cleared; after clearing, the Restore_Control
              takes its slot until restore re-offers Clear. Disabled
              before the image loads so a Segmentation clear cannot race
              the prelabel painting (same guard as Submit). */}
          {hasPrelabel && !prelabelsCleared && (
            <Button
              data-testid="clear-prelabels"
              onClick={handleClearPrelabels}
              disabled={!imageSize && taskType !== 'Classification'}
            >
              Clear pre-labels
            </Button>
          )}
          {hasPrelabel && prelabelsCleared && (
            <Button
              data-testid="restore-prelabels"
              onClick={handleRestorePrelabels}
            >
              Restore pre-labels
            </Button>
          )}
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
