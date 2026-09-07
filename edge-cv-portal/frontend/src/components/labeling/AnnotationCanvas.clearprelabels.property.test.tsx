/**
 * Property-based test for the AnnotationCanvas Clear_Prelabels_Control /
 * Restore_Control session (labeling-job-cleanup-work-stealing-and-podium
 * task 2.7, design Property 10).
 *
 * Feature: labeling-job-cleanup-work-stealing-and-podium, Property 10:
 * Clear removes exactly Prelabel_Origin state and restore is its inverse
 *
 * **Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6**
 *
 * *For any* modality, any Pre_Label payload (absent, empty, or populated),
 * and any scripted user-edit sequence (drawn boxes via pointer events, box
 * class edits, brush and eraser strokes, classification changes):
 *
 * - the Clear_Prelabels_Control is offered exactly when the Pre_Label is
 *   non-empty (Req 9.1);
 * - activating it removes exactly the Prelabel_Origin state — prelabel
 *   boxes including re-classed ones (Req 9.2), still-intact prelabel
 *   pixels plus the remaining classless proposals (Req 9.3), and the
 *   untouched prelabel classification (Req 9.4) — while every user-created
 *   edit survives;
 * - the Restore_Control then reinstates the exact pre-clear state,
 *   `restore(clear(s)) = s`, and re-offers the clear control (Req 9.5);
 * - the whole session issues zero API mutations (Req 9.6).
 *
 * Oracles are relational: the pre-clear state `s` is observed through the
 * component's imperative handle (`getAnnotation` / `validate`), so the
 * post-clear expectation is derived from what the canvas itself reported
 * before the clear — never from a re-implementation of the painting or
 * drawing pipeline. Only the Segmentation pixel-provenance record (the
 * bitmap exactly as the Pre_Label painted it) is recomputed, using the
 * component's own exported RLE helpers, because Req 9.3 is stated in terms
 * of that record.
 *
 * jsdom scaffolding follows `AnnotationCanvas.clearprelabels.test.tsx`
 * (image decode stand-in via `fireEvent.load` with defined natural
 * dimensions, a 1:1 overlay `getBoundingClientRect`, stubbed pointer
 * capture, minimal 2D context) and the fast-check + testing-library
 * pattern of `PreviewResultCanvas.score.property.test.tsx` (`cleanup()`
 * per run, `{ numRuns: 100 }`). The apiService spy is a recording Proxy
 * per `PromptTuningPreview.property.test.tsx`.
 */
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import * as fc from 'fast-check';
import { createRef } from 'react';

import AnnotationCanvas, {
  decodeRleColumnMajor,
  encodeRleColumnMajor,
  parseRleCounts,
  type AnnotationCanvasHandle,
} from './AnnotationCanvas';
import type {
  DdaAnnotation,
  DdaBoundingBox,
  DdaMaskRegion,
} from '../../services/api';

/* ------------------------------------------------------------------ */
/* apiService spy (Req 9.6)                                            */
/* ------------------------------------------------------------------ */

// Recording Proxy over the API service: any invoked method lands in
// `apiCalls`. The canvas is callback-driven and must never reach the API
// during a clear/restore session — the assertion is that this stays empty.
const { apiCalls } = vi.hoisted(() => ({ apiCalls: [] as string[] }));
vi.mock('../../services/api', () => {
  const apiService = new Proxy({} as Record<string, unknown>, {
    get(_target, prop) {
      if (typeof prop !== 'string') return undefined;
      return (..._args: unknown[]) => {
        apiCalls.push(prop);
        return Promise.resolve({});
      };
    },
  });
  return { apiService, default: apiService };
});

const IMAGE_URL = 'https://images.example/task.png?sig=property';
/** Ordered Label_Set for ObjectDetection / Segmentation runs. */
const LABEL_SET = ['scratch', 'dent', 'crack'];
/** The fixed Classification label set (req 7.3 segmented control). */
const CLASSIFICATION_LABELS = ['normal', 'anomaly'];

/* ------------------------------------------------------------------ */
/* jsdom scaffolding                                                   */
/* ------------------------------------------------------------------ */

beforeAll(() => {
  // jsdom (29.x) does not implement pointer capture; the canvas pointer
  // handlers call both methods on every gesture.
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.releasePointerCapture = vi.fn();
});

beforeEach(() => {
  // Minimal 2D-context stand-in: the overlay effect composes ImageData
  // (Segmentation) and strokes boxes (ObjectDetection); nothing painted
  // is asserted — annotation state is observed through the handle.
  HTMLCanvasElement.prototype.getContext = ((kind: string) =>
    kind === '2d'
      ? ({
          clearRect: () => undefined,
          createImageData: (width: number, height: number) => ({
            data: new Uint8ClampedArray(width * height * 4),
            width,
            height,
            colorSpace: 'srgb' as const,
          }),
          putImageData: () => undefined,
          strokeRect: () => undefined,
          fillText: () => undefined,
          setLineDash: () => undefined,
        } as unknown as CanvasRenderingContext2D)
      : null) as unknown as HTMLCanvasElement['getContext'];
});

/* ------------------------------------------------------------------ */
/* Generators                                                          */
/* ------------------------------------------------------------------ */

type Presence = 'absent' | 'empty' | 'populated';

const sizeArb = fc.integer({ min: 8, max: 16 });
const fracArb = fc.double({ min: 0, max: 1, noNaN: true });
const presenceArb = fc.constantFrom<Presence>('absent', 'empty', 'populated');

interface ClassificationRun {
  modality: 'Classification';
  width: number;
  height: number;
  presence: Presence;
  /** Pre_Label label, used when presence is 'populated'. */
  prelabelLabel: string;
  /** Scripted classification changes (segmented-control clicks). */
  clicks: string[];
}

const classificationRunArb: fc.Arbitrary<ClassificationRun> = fc.record({
  modality: fc.constant('Classification' as const),
  width: sizeArb,
  height: sizeArb,
  presence: presenceArb,
  prelabelLabel: fc.constantFrom('normal', 'anomaly'),
  clicks: fc.array(fc.constantFrom('normal', 'anomaly'), { maxLength: 2 }),
});

/** Pre_Label box spec: classIdx -1 means a classless proposal box. */
interface OdBoxSpec {
  classIdx: number;
  lf: number;
  tf: number;
  wf: number;
  hf: number;
}

/** One drag-drawn user box; extents map to >= 3px so every draw lands. */
interface OdDrawSpec {
  xf: number;
  yf: number;
  wf: number;
  hf: number;
  /** Toolbar class picked before drawing, or null to keep the current. */
  toolbarClassIdx: number | null;
}

/** One box class edit through the box row's Select (any box, including
 *  Prelabel_Origin ones — the re-classed-still-cleared case of Req 9.2). */
interface OdClassEditSpec {
  targetf: number;
  classIdx: number;
}

interface ObjectDetectionRun {
  modality: 'ObjectDetection';
  width: number;
  height: number;
  presence: Presence;
  prelabelBoxes: OdBoxSpec[];
  draws: OdDrawSpec[];
  classEdits: OdClassEditSpec[];
}

const objectDetectionRunArb: fc.Arbitrary<ObjectDetectionRun> = fc.record({
  modality: fc.constant('ObjectDetection' as const),
  width: sizeArb,
  height: sizeArb,
  presence: presenceArb,
  prelabelBoxes: fc.array(
    fc.record({
      classIdx: fc.integer({ min: -1, max: LABEL_SET.length - 1 }),
      lf: fracArb,
      tf: fracArb,
      wf: fracArb,
      hf: fracArb,
    }),
    { minLength: 1, maxLength: 3 }
  ),
  draws: fc.array(
    fc.record({
      xf: fracArb,
      yf: fracArb,
      wf: fracArb,
      hf: fracArb,
      toolbarClassIdx: fc.option(
        fc.integer({ min: 0, max: LABEL_SET.length - 1 }),
        { nil: null }
      ),
    }),
    { maxLength: 2 }
  ),
  classEdits: fc.array(
    fc.record({
      targetf: fracArb,
      classIdx: fc.integer({ min: 0, max: LABEL_SET.length - 1 }),
    }),
    { maxLength: 2 }
  ),
});

/** Pre_Label region spec: 1-2 rectangles rasterized into a valid RLE;
 *  classIdx -1 makes the region a classless SAM proposal. */
interface SegRegionSpec {
  classIdx: number;
  rects: Array<{ xf: number; yf: number; wf: number; hf: number }>;
}

/** One brush or eraser stroke (pointer down / optional move / up). */
interface SegStrokeSpec {
  tool: 'brush' | 'eraser';
  classIdx: number;
  xf: number;
  yf: number;
  x2f: number | null;
  y2f: number;
}

interface SegmentationRun {
  modality: 'Segmentation';
  width: number;
  height: number;
  presence: Presence;
  regions: SegRegionSpec[];
  brushSize: number;
  strokes: SegStrokeSpec[];
}

const segmentationRunArb: fc.Arbitrary<SegmentationRun> = fc.record({
  modality: fc.constant('Segmentation' as const),
  width: sizeArb,
  height: sizeArb,
  presence: presenceArb,
  regions: fc.array(
    fc.record({
      classIdx: fc.integer({ min: -1, max: LABEL_SET.length - 1 }),
      rects: fc.array(
        fc.record({ xf: fracArb, yf: fracArb, wf: fracArb, hf: fracArb }),
        { minLength: 1, maxLength: 2 }
      ),
    }),
    { minLength: 1, maxLength: 3 }
  ),
  brushSize: fc.constantFrom(2, 4, 8),
  strokes: fc.array(
    fc.record({
      tool: fc.constantFrom('brush' as const, 'eraser' as const),
      classIdx: fc.integer({ min: 0, max: LABEL_SET.length - 1 }),
      xf: fracArb,
      yf: fracArb,
      x2f: fc.option(fracArb, { nil: null }),
      y2f: fracArb,
    }),
    { maxLength: 3 }
  ),
});

type RunSpec = ClassificationRun | ObjectDetectionRun | SegmentationRun;

const runArb: fc.Arbitrary<RunSpec> = fc.oneof(
  classificationRunArb,
  objectDetectionRunArb,
  segmentationRunArb
);

/* ------------------------------------------------------------------ */
/* Pre_Label payload builders                                          */
/* ------------------------------------------------------------------ */

/** Integer in-bounds boxes so the payload is exactly representable. */
function buildPrelabelBoxes(
  specs: OdBoxSpec[],
  width: number,
  height: number
): DdaBoundingBox[] {
  return specs.map((spec) => {
    const left = Math.min(width - 2, Math.floor(spec.lf * (width - 1)));
    const top = Math.min(height - 2, Math.floor(spec.tf * (height - 1)));
    const w = Math.max(
      1,
      Math.min(width - left, 1 + Math.floor(spec.wf * (width - left - 1)))
    );
    const h = Math.max(
      1,
      Math.min(height - top, 1 + Math.floor(spec.hf * (height - top - 1)))
    );
    return {
      class: spec.classIdx >= 0 ? LABEL_SET[spec.classIdx] : null,
      left,
      top,
      width: w,
      height: h,
    };
  });
}

/** Rasterize a region spec's rectangles into a binary mask. */
function rasterizeRegion(
  spec: SegRegionSpec,
  width: number,
  height: number
): Uint8Array {
  const mask = new Uint8Array(width * height);
  for (const rect of spec.rects) {
    const x0 = Math.min(width - 1, Math.floor(rect.xf * width));
    const y0 = Math.min(height - 1, Math.floor(rect.yf * height));
    const x1 = Math.min(width - 1, x0 + Math.floor(rect.wf * (width - x0)));
    const y1 = Math.min(height - 1, y0 + Math.floor(rect.hf * (height - y0)));
    for (let y = y0; y <= y1; y++) {
      for (let x = x0; x <= x1; x++) {
        mask[y * width + x] = 1;
      }
    }
  }
  return mask;
}

/** Valid canonical RLE regions (space-separated column-major counts). */
function buildPrelabelRegions(
  specs: SegRegionSpec[],
  width: number,
  height: number
): DdaMaskRegion[] {
  return specs.map((spec) => ({
    class: spec.classIdx >= 0 ? LABEL_SET[spec.classIdx] : null,
    rle: encodeRleColumnMajor(
      rasterizeRegion(spec, width, height),
      width,
      height,
      1
    ).join(' '),
  }));
}

function buildPrelabel(run: RunSpec): DdaAnnotation | undefined {
  if (run.presence === 'absent') return undefined;
  if (run.modality === 'Classification') {
    return run.presence === 'empty'
      ? { modality: run.modality }
      : { modality: run.modality, label: run.prelabelLabel };
  }
  if (run.modality === 'ObjectDetection') {
    return {
      modality: run.modality,
      boxes:
        run.presence === 'empty'
          ? []
          : buildPrelabelBoxes(run.prelabelBoxes, run.width, run.height),
    };
  }
  return {
    modality: run.modality,
    regions:
      run.presence === 'empty'
        ? []
        : buildPrelabelRegions(run.regions, run.width, run.height),
  };
}

/* ------------------------------------------------------------------ */
/* Oracles                                                             */
/* ------------------------------------------------------------------ */

/**
 * Compose classed RLE regions into the label-indexed bitmap, in payload
 * order (later regions overwrite earlier, exactly as the canvas paints a
 * Pre_Label at image load). Classless regions carry no bitmap pixels —
 * they become proposals. Also decodes `getAnnotation()` output, whose
 * per-class regions are disjoint by construction.
 */
function composeBitmap(
  regions: DdaMaskRegion[] | undefined,
  width: number,
  height: number
): Uint8Array {
  const bitmap = new Uint8Array(width * height);
  for (const region of regions ?? []) {
    const classIndex =
      region.class !== null && region.class !== undefined
        ? LABEL_SET.indexOf(region.class)
        : -1;
    if (classIndex < 0) continue;
    const mask = decodeRleColumnMajor(parseRleCounts(region.rle), width, height);
    for (let p = 0; p < mask.length; p++) {
      if (mask[p]) bitmap[p] = classIndex + 1;
    }
  }
  return bitmap;
}

/* ------------------------------------------------------------------ */
/* Interaction helpers                                                 */
/* ------------------------------------------------------------------ */

/** Image decode stand-in (jsdom never loads real images). */
function loadImage(container: HTMLElement, width: number, height: number) {
  const img = container.querySelector('img');
  if (!img) throw new Error('task image not rendered');
  Object.defineProperty(img, 'naturalWidth', {
    value: width,
    configurable: true,
  });
  Object.defineProperty(img, 'naturalHeight', {
    value: height,
    configurable: true,
  });
  fireEvent.load(img);
}

/**
 * Pin the overlay canvas's client rect to the image pixel size so pointer
 * client coordinates map 1:1 onto image pixels (jsdom rects are all-zero,
 * which would abort the canvas's pointer handling).
 */
function fixOverlayRect(
  container: HTMLElement,
  width: number,
  height: number
): HTMLElement {
  const canvas = within(container).getByTestId('annotation-overlay');
  canvas.getBoundingClientRect = () =>
    ({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: width,
      bottom: height,
      width,
      height,
      toJSON: () => ({}),
    }) as DOMRect;
  return canvas;
}

/** Click a segmented-control segment by its option id. */
function clickSegment(container: HTMLElement, id: string) {
  const segment = createWrapper(container)
    .findSegmentedControl()
    ?.findSegmentById(id);
  if (!segment) throw new Error(`segmented-control segment '${id}' not found`);
  fireEvent.click(segment.getElement());
}

/**
 * The canvas's own Selects in DOM order (toolbar first, then box/proposal
 * rows). Cloudscape SegmentedControl renders an internal narrow-viewport
 * fallback Select that `findAllSelects` also matches; exclude it.
 */
function annotationSelects(container: HTMLElement) {
  const segmentedRoot = createWrapper(container)
    .findSegmentedControl()
    ?.getElement();
  return createWrapper(container)
    .findAllSelects()
    .filter(
      (select) => !segmentedRoot || !segmentedRoot.contains(select.getElement())
    );
}

/** Pick an option (by value) in the index-th canvas Select. */
function pickSelectValue(
  container: HTMLElement,
  selectIndex: number,
  value: string
) {
  const select = annotationSelects(container)[selectIndex];
  if (!select) throw new Error(`select #${selectIndex} not found`);
  select.openDropdown();
  select.selectOptionByValue(value);
}

/** Drag-draw one ObjectDetection box; extents are >= 3px so it lands. */
function drawBox(
  canvas: HTMLElement,
  spec: OdDrawSpec,
  width: number,
  height: number
) {
  const x1 = Math.min(width - 4, Math.floor(spec.xf * (width - 3)));
  const y1 = Math.min(height - 4, Math.floor(spec.yf * (height - 3)));
  const x2 = Math.min(width, x1 + 3 + Math.floor(spec.wf * (width - x1 - 3)));
  const y2 = Math.min(height, y1 + 3 + Math.floor(spec.hf * (height - y1 - 3)));
  fireEvent.pointerDown(canvas, { pointerId: 1, clientX: x1, clientY: y1 });
  fireEvent.pointerMove(canvas, { pointerId: 1, clientX: x2, clientY: y2 });
  fireEvent.pointerUp(canvas, { pointerId: 1, clientX: x2, clientY: y2 });
}

/** Apply the run's scripted user edits. */
function applyEdits(container: HTMLElement, run: RunSpec) {
  if (run.modality === 'Classification') {
    for (const id of run.clicks) clickSegment(container, id);
    return;
  }
  const canvas = fixOverlayRect(container, run.width, run.height);
  if (run.modality === 'ObjectDetection') {
    for (const draw of run.draws) {
      if (draw.toolbarClassIdx !== null) {
        // Toolbar "Box class" select (index 0) uses stringified indices.
        pickSelectValue(container, 0, String(draw.toolbarClassIdx));
      }
      drawBox(canvas, draw, run.width, run.height);
    }
    for (const edit of run.classEdits) {
      // Box-row selects follow the toolbar select in DOM order.
      const boxCount = annotationSelects(container).length - 1;
      if (boxCount <= 0) continue;
      const target = Math.min(boxCount - 1, Math.floor(edit.targetf * boxCount));
      pickSelectValue(container, 1 + target, LABEL_SET[edit.classIdx]);
    }
    return;
  }
  // Segmentation: set the brush size once, then apply the strokes.
  const slider = createWrapper(container).findSlider();
  if (slider) {
    fireEvent.change(slider.findNativeInput().getElement(), {
      target: { value: String(run.brushSize) },
    });
  }
  let currentTool: 'brush' | 'eraser' = 'brush';
  for (const stroke of run.strokes) {
    if (stroke.tool !== currentTool) {
      clickSegment(container, stroke.tool);
      currentTool = stroke.tool;
    }
    if (stroke.tool === 'brush') {
      // Toolbar "Class" select (index 0, disabled while erasing).
      pickSelectValue(container, 0, String(stroke.classIdx));
    }
    const x = Math.min(run.width - 1, Math.floor(stroke.xf * run.width));
    const y = Math.min(run.height - 1, Math.floor(stroke.yf * run.height));
    fireEvent.pointerDown(canvas, { pointerId: 1, clientX: x, clientY: y });
    if (stroke.x2f !== null) {
      const x2 = Math.min(run.width - 1, Math.floor(stroke.x2f * run.width));
      const y2 = Math.min(run.height - 1, Math.floor(stroke.y2f * run.height));
      fireEvent.pointerMove(canvas, { pointerId: 1, clientX: x2, clientY: y2 });
    }
    fireEvent.pointerUp(canvas, { pointerId: 1, clientX: x, clientY: y });
  }
}

/* ------------------------------------------------------------------ */
/* Property 10 (task 2.7)                                              */
/* ------------------------------------------------------------------ */

describe('Feature: labeling-job-cleanup-work-stealing-and-podium, Property 10: Clear removes exactly Prelabel_Origin state and restore is its inverse', () => {
  it('offers clear iff the Pre_Label is non-empty; clear removes exactly Prelabel_Origin state; restore(clear(s)) = s; zero API calls', () => {
    fc.assert(
      fc.property(runArb, (run) => {
        cleanup();
        apiCalls.length = 0;

        const prelabel = buildPrelabel(run);
        const expectedOffered = run.presence === 'populated';
        const labelSet =
          run.modality === 'Classification' ? CLASSIFICATION_LABELS : LABEL_SET;

        const onSubmit = vi.fn();
        const onImageUrlRefresh = vi.fn(async () => ({
          image_url: IMAGE_URL,
        }));
        const ref = createRef<AnnotationCanvasHandle>();
        const { container } = render(
          <AnnotationCanvas
            ref={ref}
            imageUrl={IMAGE_URL}
            taskType={run.modality}
            labelSet={labelSet}
            prelabel={prelabel}
            onSubmit={onSubmit}
            onImageUrlRefresh={onImageUrlRefresh}
          />
        );
        loadImage(container, run.width, run.height);

        // Req 9.1 — the control is offered exactly when the Pre_Label is
        // non-empty, and the Restore_Control is never offered pre-clear.
        expect(!!within(container).queryByTestId('clear-prelabels')).toBe(
          expectedOffered
        );
        expect(within(container).queryByTestId('restore-prelabels')).toBeNull();

        applyEdits(container, run);

        if (!expectedOffered) {
          // User edits never conjure the control for an absent or empty
          // Pre_Label (Req 9.1), and the session touches no API (Req 9.6).
          expect(within(container).queryByTestId('clear-prelabels')).toBeNull();
          expect(
            within(container).queryByTestId('restore-prelabels')
          ).toBeNull();
          expect(apiCalls).toEqual([]);
          expect(onSubmit).not.toHaveBeenCalled();
          expect(onImageUrlRefresh).not.toHaveBeenCalled();
          return;
        }

        // The pre-clear state s, observed through the imperative handle.
        const before = ref.current!.getAnnotation();
        // Segmentation validate() lists exactly the classless proposals.
        const proposalsBefore =
          run.modality === 'Segmentation' ? ref.current!.validate().length : 0;

        fireEvent.click(within(container).getByTestId('clear-prelabels'));

        // Req 9.5 — clearing swaps the control for the Restore_Control.
        expect(within(container).queryByTestId('clear-prelabels')).toBeNull();
        const restoreControl =
          within(container).getByTestId('restore-prelabels');

        const afterClear = ref.current!.getAnnotation();

        if (run.modality === 'Classification') {
          // Req 9.4 — a selection still equal to the Pre_Label's label is
          // deselected; a selection the labeler changed is retained.
          const expectedLabel =
            before.label !== undefined && before.label === prelabel!.label
              ? undefined
              : before.label;
          expect(afterClear.label).toBe(expectedLabel);
        } else if (run.modality === 'ObjectDetection') {
          // Req 9.2 — exactly the Prelabel_Origin boxes are removed (the
          // first N of the state array, even when re-classed); every
          // user-drawn box survives in order.
          const prelabelBoxCount = prelabel!.boxes!.length;
          expect(afterClear.boxes).toEqual(
            (before.boxes ?? []).slice(prelabelBoxCount)
          );
        } else {
          // Req 9.3 — each pixel still holding the class the Pre_Label
          // initialized it to is cleared; pixels the labeler painted or
          // repainted (they differ from the provenance record) survive.
          const bitmapBefore = composeBitmap(
            before.regions,
            run.width,
            run.height
          );
          const prelabelBitmap = composeBitmap(
            prelabel!.regions,
            run.width,
            run.height
          );
          const expectedBitmap = new Uint8Array(bitmapBefore);
          for (let p = 0; p < expectedBitmap.length; p++) {
            if (
              prelabelBitmap[p] !== 0 &&
              bitmapBefore[p] === prelabelBitmap[p]
            ) {
              expectedBitmap[p] = 0;
            }
          }
          expect(
            Array.from(
              composeBitmap(afterClear.regions, run.width, run.height)
            )
          ).toEqual(Array.from(expectedBitmap));
          // ...and the remaining classless proposals are removed.
          expect(ref.current!.validate()).toEqual([]);
        }

        fireEvent.click(restoreControl);

        // Req 9.5 — restore reinstates the Prelabel_Snapshot exactly and
        // re-offers the Clear_Prelabels_Control.
        expect(within(container).queryByTestId('restore-prelabels')).toBeNull();
        expect(
          within(container).queryByTestId('clear-prelabels')
        ).not.toBeNull();
        const afterRestore = ref.current!.getAnnotation();
        expect(afterRestore).toEqual(before);
        if (run.modality === 'Segmentation') {
          expect(ref.current!.validate().length).toBe(proposalsBefore);
        }

        // Req 9.6 — the whole session issued zero API mutations and never
        // reached the workspace callbacks.
        expect(apiCalls).toEqual([]);
        expect(onSubmit).not.toHaveBeenCalled();
        expect(onImageUrlRefresh).not.toHaveBeenCalled();
      }),
      { numRuns: 100 }
    );
  }, 600_000);
});
