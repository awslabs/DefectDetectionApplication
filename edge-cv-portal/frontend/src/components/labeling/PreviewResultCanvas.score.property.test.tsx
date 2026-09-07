/**
 * Property-based test for `PreviewResultCanvas`'s Region_Score display
 * (grounded-sam-prompt-tuning-preview task 2.5, design Property 11).
 *
 * Feature: grounded-sam-prompt-tuning-preview, Property 11: Region_Score
 * renders exactly when a region or box carries one
 *
 * **Validates: Requirements 5.3, 9.5**
 *
 * The generator produces successful Segmentation / ObjectDetection preview
 * payloads whose regions / boxes *independently* carry or omit a numeric
 * `score` — the shapes the Preview_Executor writes for grounded-sam runs
 * (score sometimes present) and the llm executor writes always (score
 * absent). The property asserts:
 *
 * - each score-carrying region/box renders exactly one score element
 *   (`preview-region-score` / `preview-box-score`) inside its own class
 *   entry, formatted to two decimals — ` (X.XX)` beside the class name;
 * - each scoreless region/box renders no score element, and the total
 *   score-element count equals the number of score-carrying shapes;
 * - a payload with no scores anywhere renders zero score elements; and,
 *   stronger, the score display is *purely additive*: removing the score
 *   elements from any render yields markup byte-identical to rendering the
 *   same payload with every `score` key stripped — so every scoreless
 *   payload (every llm payload) renders byte-identically to the
 *   pre-feature component (Req 9.5).
 *
 * Scaffolding follows the shipped suites: `PreviewResultCanvas.test.tsx`
 * for the jsdom 2D-context stand-in (Segmentation paints decoded RLE masks
 * into a canvas) and `PromptTuningPreview.property.test.tsx` for the
 * fast-check + vitest + testing-library pattern (`cleanup()` per run,
 * `{ numRuns: 100 }`).
 */
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import * as fc from 'fast-check';

import PreviewResultCanvas from './PreviewResultCanvas';
import type {
  DdaAnnotation,
  DdaBoundingBox,
  DdaMaskRegion,
} from '../../services/api';

const IMAGE_URL = 'https://s3.example/sample.jpg';
/** Ordered Label_Set the generated classes are drawn from. */
const LABEL_SET = ['scratch', 'dent', 'cookie_gap'];

/* ------------------------------------------------------------------ */
/* jsdom canvas stand-in (Segmentation paints RLE masks into a 2D ctx) */
/* ------------------------------------------------------------------ */

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    ((kind: string) =>
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
          } as unknown as CanvasRenderingContext2D)
        : null) as unknown as HTMLCanvasElement['getContext']
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

/* ------------------------------------------------------------------ */
/* Generators                                                          */
/* ------------------------------------------------------------------ */

/**
 * One generated region/box, as constraint-free spec data: fractions are
 * mapped onto concrete in-bounds geometry / RLE once the image dimensions
 * are known, and `score: null` means the shape omits the key entirely.
 */
interface ShapeSpec {
  className: string;
  /** Fraction of the image area preceding the region's one-run (RLE). */
  coverage: number;
  /** Box geometry fractions, mapped into the image bounds. */
  leftFrac: number;
  topFrac: number;
  extentFrac: number;
  /** Region_Score carried by this shape, or null to omit it. */
  score: number | null;
}

const fracArb = fc.double({ min: 0, max: 1, noNaN: true });

const shapeSpecArb: fc.Arbitrary<ShapeSpec> = fc.record({
  className: fc.constantFrom(...LABEL_SET),
  coverage: fracArb,
  leftFrac: fracArb,
  topFrac: fracArb,
  extentFrac: fracArb,
  // Worker scores are confidences in [0, 1]; each shape independently
  // carries one or omits it (null → no `score` key on the shape at all).
  score: fc.option(fracArb, { nil: null }),
});

interface GeneratedInput {
  taskType: 'Segmentation' | 'ObjectDetection';
  width: number;
  height: number;
  specs: ShapeSpec[];
}

const inputArb: fc.Arbitrary<GeneratedInput> = fc.record({
  taskType: fc.constantFrom<'Segmentation' | 'ObjectDetection'>(
    'Segmentation',
    'ObjectDetection'
  ),
  width: fc.integer({ min: 1, max: 8 }),
  height: fc.integer({ min: 1, max: 8 }),
  // Zero shapes is a valid successful payload (the no-detections state).
  specs: fc.array(shapeSpecArb, { minLength: 0, maxLength: 4 }),
});

/** Build the Segmentation regions a preview success payload carries. */
function buildRegions(
  specs: ShapeSpec[],
  width: number,
  height: number
): DdaMaskRegion[] {
  const area = width * height;
  return specs.map((spec) => {
    // Canonical column-major counts string: a zero-run then a one-run
    // covering the rest of the image — always a decodable, in-area RLE.
    const zeroRun = Math.min(area, Math.round(spec.coverage * area));
    const region: DdaMaskRegion = {
      class: spec.className,
      rle: `${zeroRun} ${area - zeroRun}`,
    };
    if (spec.score !== null) region.score = spec.score;
    return region;
  });
}

/** Build the ObjectDetection boxes a preview success payload carries. */
function buildBoxes(
  specs: ShapeSpec[],
  width: number,
  height: number
): DdaBoundingBox[] {
  return specs.map((spec) => {
    // In-bounds geometry with positive extent — the executor validates
    // exactly this before writing a success payload (design Req 4.2).
    const left = Math.min(width - 1, Math.floor(spec.leftFrac * width));
    const top = Math.min(height - 1, Math.floor(spec.topFrac * height));
    const box: DdaBoundingBox = {
      class: spec.className,
      left,
      top,
      width: Math.max(1, Math.round(spec.extentFrac * (width - left))),
      height: Math.max(1, Math.round(spec.extentFrac * (height - top))),
    };
    if (spec.score !== null) box.score = spec.score;
    return box;
  });
}

/** Deep-copy shapes with every `score` key removed (the llm payload form). */
function withoutScores<T extends { score?: number }>(shapes: T[]): T[] {
  return shapes.map((shape) => {
    const { score: _ignored, ...rest } = shape;
    return rest as unknown as T;
  });
}

/* ------------------------------------------------------------------ */
/* Property 11 (task 2.5)                                              */
/* ------------------------------------------------------------------ */

describe('Feature: grounded-sam-prompt-tuning-preview, Property 11: Region_Score renders exactly when a region or box carries one', () => {
  /**
   * *For any* successful result payload (Segmentation regions or
   * ObjectDetection boxes, each independently carrying or omitting a
   * numeric `score`), the rendered result SHALL display each region/box's
   * score beside its class name exactly when the payload carries one — a
   * payload with no scores (every llm payload) rendering byte-identically
   * to before this feature.
   *
   * **Validates: Requirements 5.3, 9.5**
   */
  it('renders a two-decimal score element exactly for the score-carrying shapes, scoreless markup byte-identical', () => {
    fc.assert(
      fc.property(inputArb, (input) => {
        cleanup();
        const { taskType, width, height, specs } = input;

        const shapes: Array<DdaMaskRegion | DdaBoundingBox> =
          taskType === 'Segmentation'
            ? buildRegions(specs, width, height)
            : buildBoxes(specs, width, height);
        const prelabel: DdaAnnotation =
          taskType === 'Segmentation'
            ? { modality: taskType, regions: shapes as DdaMaskRegion[] }
            : { modality: taskType, boxes: shapes as DdaBoundingBox[] };

        const classTestId =
          taskType === 'Segmentation'
            ? 'preview-region-class'
            : 'preview-box-class';
        const scoreTestId =
          taskType === 'Segmentation'
            ? 'preview-region-score'
            : 'preview-box-score';
        const anyScoreSelector =
          '[data-testid="preview-region-score"], [data-testid="preview-box-score"]';

        const renderCanvas = (label: DdaAnnotation) =>
          render(
            <PreviewResultCanvas
              imageUrl={IMAGE_URL}
              taskType={taskType}
              labelSet={LABEL_SET}
              prelabel={label}
              imageWidth={width}
              imageHeight={height}
            />
          );

        /* ---- the payload as generated (scores mixed in) ------------ */
        const scored = renderCanvas(prelabel);

        // One class entry per shape, in payload order.
        const classNodes = Array.from(
          scored.container.querySelectorAll(`[data-testid="${classTestId}"]`)
        );
        expect(classNodes).toHaveLength(shapes.length);

        // Each shape's entry carries a two-decimal score element exactly
        // when the shape carries a numeric score (Req 5.3).
        shapes.forEach((shape, i) => {
          const scoreNodes = classNodes[i].querySelectorAll(
            `[data-testid="${scoreTestId}"]`
          );
          if (typeof shape.score === 'number') {
            expect(scoreNodes).toHaveLength(1);
            expect(scoreNodes[0].textContent).toBe(
              ` (${shape.score.toFixed(2)})`
            );
          } else {
            expect(scoreNodes).toHaveLength(0);
          }
        });

        // No score element renders anywhere else: the total (across both
        // modality testids) is exactly the score-carrying shape count —
        // zero for an all-scoreless payload.
        const scoredCount = shapes.filter(
          (shape) => typeof shape.score === 'number'
        ).length;
        expect(
          scored.container.querySelectorAll(anyScoreSelector)
        ).toHaveLength(scoredCount);

        // The score display is purely additive: with its elements removed,
        // the remaining markup must be what a scoreless payload renders.
        scored.container
          .querySelectorAll(anyScoreSelector)
          .forEach((node) => node.remove());
        const scoreStrippedHtml = scored.container.innerHTML;
        scored.unmount();

        /* ---- the same payload with every score stripped ------------ */
        const strippedShapes = withoutScores(shapes);
        const strippedPrelabel: DdaAnnotation =
          taskType === 'Segmentation'
            ? { modality: taskType, regions: strippedShapes as DdaMaskRegion[] }
            : { modality: taskType, boxes: strippedShapes as DdaBoundingBox[] };
        const stripped = renderCanvas(strippedPrelabel);

        // A payload with no scores renders zero score elements (Req 9.5)…
        expect(
          stripped.container.querySelectorAll(anyScoreSelector)
        ).toHaveLength(0);
        // …and byte-identical markup to the scored render minus its score
        // elements — the pre-feature rendering, unchanged.
        expect(stripped.container.innerHTML).toBe(scoreStrippedHtml);
        stripped.unmount();
      }),
      { numRuns: 100 }
    );
  }, 600_000);
});
