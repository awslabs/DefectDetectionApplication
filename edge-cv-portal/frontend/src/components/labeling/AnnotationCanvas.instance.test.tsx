/**
 * AnnotationCanvas polygon tool for the `InstanceSegmentation` modality
 * (rfdetr-seg-support task 2.4; Requirements 2.2, 2.3, 2.7; DD9, DD20).
 *
 * Covers, by example:
 * - the modality option is present and wired: the polygon tool renders
 *   (instance class select, polygon hint, Close/Undo controls) and the
 *   Segmentation brush/eraser do not (Req 2.3);
 * - add / edit / close: click-to-add vertices, close on the first vertex and
 *   by double-click, drag a vertex to edit (Req 2.3);
 * - select / reclass / delete / undo through the instance rows and the
 *   undo stack (Req 2.3);
 * - polygon -> RLE rasterisation and the round trip through
 *   `parseRleCounts` / `encodeRleColumnMajor`, including an instance
 *   touching the image border; the tight box derived from the RLE
 *   (Req 2.2, 2.7);
 * - pre-label instances are kept separate (two touching instances of one
 *   class), reclassed or deleted individually, never merged (Req 2.4);
 * - submit rejection of an instance covering no pixel and refusal of a
 *   two-vertex polygon (Req 2.2, 2.7);
 * - the RLE codec is re-exported from `utils/rle.ts` unchanged (DD20).
 *
 * jsdom scaffolding follows `AnnotationCanvas.clearprelabels.test.tsx`
 * (image decode stand-in, 1:1 overlay rect, stubbed pointer capture), with
 * the 2D-context stand-in extended by the path and rectangle methods the
 * polygon renderer calls.
 */
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { createRef } from 'react';

import AnnotationCanvas, {
  INSTANCE_OUTLINE_PALETTE,
  CLASS_PALETTE,
  POLYGON_MIN_VERTICES,
  decodeRleColumnMajor,
  distinctVertexCount,
  encodeRleColumnMajor,
  parseRleCounts,
  rasterizePolygonEvenOdd,
  tightBoxFromMask,
  type AnnotationCanvasHandle,
  type PolygonVertex,
} from './AnnotationCanvas';
import * as rleModule from '../../utils/rle';
import type { DdaAnnotation } from '../../services/api';

const IMAGE_URL = 'https://images.example/task.png?sig=instance';
const LABEL_SET = ['scratch', 'dent'];
const WIDTH = 16;
const HEIGHT = 12;

/* ------------------------------------------------------------------ */
/* jsdom scaffolding                                                   */
/* ------------------------------------------------------------------ */

beforeAll(() => {
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.releasePointerCapture = vi.fn();
});

beforeEach(() => {
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
          fillRect: () => undefined,
          fillText: () => undefined,
          setLineDash: () => undefined,
          beginPath: () => undefined,
          moveTo: () => undefined,
          lineTo: () => undefined,
          closePath: () => undefined,
          fill: () => undefined,
          stroke: () => undefined,
        } as unknown as CanvasRenderingContext2D)
      : null) as unknown as HTMLCanvasElement['getContext'];
});

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

function renderCanvas(prelabel?: DdaAnnotation) {
  const onSubmit = vi.fn();
  const ref = createRef<AnnotationCanvasHandle>();
  const view = render(
    <AnnotationCanvas
      ref={ref}
      imageUrl={IMAGE_URL}
      taskType="InstanceSegmentation"
      labelSet={LABEL_SET}
      prelabel={prelabel}
      onSubmit={onSubmit}
      onImageUrlRefresh={vi.fn(async () => ({ image_url: IMAGE_URL }))}
    />
  );
  const img = screen.getByAltText('Image to label') as HTMLImageElement;
  Object.defineProperty(img, 'naturalWidth', { value: WIDTH, configurable: true });
  Object.defineProperty(img, 'naturalHeight', { value: HEIGHT, configurable: true });
  fireEvent.load(img);
  const canvas = screen.getByTestId('annotation-overlay');
  canvas.getBoundingClientRect = () =>
    ({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: WIDTH,
      bottom: HEIGHT,
      width: WIDTH,
      height: HEIGHT,
      toJSON: () => ({}),
    }) as DOMRect;
  return { ...view, onSubmit, ref, canvas };
}

function click(canvas: HTMLElement, x: number, y: number) {
  fireEvent.pointerDown(canvas, { pointerId: 1, clientX: x, clientY: y });
  fireEvent.pointerUp(canvas, { pointerId: 1, clientX: x, clientY: y });
}

function drag(
  canvas: HTMLElement,
  from: [number, number],
  to: [number, number]
) {
  fireEvent.pointerDown(canvas, { pointerId: 1, clientX: from[0], clientY: from[1] });
  fireEvent.pointerMove(canvas, { pointerId: 1, clientX: to[0], clientY: to[1] });
  fireEvent.pointerUp(canvas, { pointerId: 1, clientX: to[0], clientY: to[1] });
}

/** Place the vertices and close the ring by clicking the first vertex. */
function drawPolygon(canvas: HTMLElement, vertices: PolygonVertex[]) {
  for (const [x, y] of vertices) click(canvas, x, y);
  click(canvas, vertices[0][0], vertices[0][1]);
}

function rectMask(left: number, top: number, w: number, h: number): Uint8Array {
  const mask = new Uint8Array(WIDTH * HEIGHT);
  for (let y = top; y < top + h; y++) {
    for (let x = left; x < left + w; x++) mask[y * WIDTH + x] = 1;
  }
  return mask;
}

const rleOf = (mask: Uint8Array) =>
  encodeRleColumnMajor(mask, WIDTH, HEIGHT, 1).join(' ');

const instanceRows = () => screen.queryAllByText(/^Instance \d+$/);
const hint = () => screen.getByTestId('polygon-hint').textContent ?? '';
const closeButton = () => screen.getByTestId('close-polygon');
const undoButton = () => screen.getByTestId('undo-instance-edit');

/** The Select of the i-th (0-based) instance row. */
function instanceRowSelect(container: HTMLElement, index: number) {
  // The toolbar's instance-class select comes first in DOM order.
  return createWrapper(container).findAllSelects()[1 + index];
}

/* ------------------------------------------------------------------ */
/* Pure helpers                                                        */
/* ------------------------------------------------------------------ */

describe('polygon helpers (DD9, DD20)', () => {
  it('re-exports the RLE codec from utils/rle unchanged (DD20)', () => {
    expect(encodeRleColumnMajor).toBe(rleModule.encodeRleColumnMajor);
    expect(decodeRleColumnMajor).toBe(rleModule.decodeRleColumnMajor);
    expect(parseRleCounts).toBe(rleModule.parseRleCounts);
  });

  it('rasterises an axis-aligned rectangle to exactly its pixels (even-odd fill)', () => {
    const mask = rasterizePolygonEvenOdd(
      [[2, 2], [10, 2], [10, 8], [2, 8]], WIDTH, HEIGHT);
    expect(Array.from(mask)).toEqual(Array.from(rectMask(2, 2, 8, 6)));
    expect(tightBoxFromMask(mask, WIDTH, HEIGHT)).toEqual({
      left: 2, top: 2, width: 8, height: 6,
    });
  });

  it('clips a polygon reaching past the image edge and fills a self-crossing ring lobe by lobe', () => {
    const full = rasterizePolygonEvenOdd(
      [[-5, -5], [40, -5], [40, 40], [-5, 40]], WIDTH, HEIGHT);
    expect(rleOf(full)).toBe(`0 ${WIDTH * HEIGHT}`);
    // A figure-eight (edges crossing at the image centre): both lobes are
    // filled and the pinch between them stays empty.
    const figureEight = rasterizePolygonEvenOdd(
      [[0, 0], [16, 12], [16, 0], [0, 12]], WIDTH, HEIGHT);
    expect(figureEight[6 * WIDTH + 1]).toBe(1);
    expect(figureEight[6 * WIDTH + 14]).toBe(1);
    expect(figureEight[6 * WIDTH + 8]).toBe(0);
    expect(tightBoxFromMask(new Uint8Array(WIDTH * HEIGHT), WIDTH, HEIGHT)).toBeNull();
  });

  it('counts distinct vertices after rounding (Req 2.2)', () => {
    expect(distinctVertexCount([[1, 1], [1.2, 0.9], [5, 5]])).toBe(2);
    expect(distinctVertexCount([[1, 1], [5, 1], [5, 5]])).toBe(3);
    expect(POLYGON_MIN_VERTICES).toBe(3);
  });

  it('keeps the outline palette disjoint from the class palette (Req 2.3)', () => {
    for (const outline of INSTANCE_OUTLINE_PALETTE) {
      expect(CLASS_PALETTE as readonly string[]).not.toContain(outline);
    }
  });
});

/* ------------------------------------------------------------------ */
/* Tool presence (Req 2.3)                                             */
/* ------------------------------------------------------------------ */

describe('InstanceSegmentation tool presence (Req 2.3)', () => {
  it('renders the polygon tool and not the brush/eraser', () => {
    const { container } = renderCanvas();
    expect(screen.getByText('Instance class')).toBeInTheDocument();
    expect(hint()).toContain('Click on the image to place vertices');
    expect(closeButton()).toBeDisabled();
    expect(undoButton()).toBeDisabled();
    expect(screen.queryByText('Painting tool')).toBeNull();
    expect(screen.queryByText(/Brush size/)).toBeNull();
    expect(screen.queryByText('Box class')).toBeNull();
    // Toolbar select only, no instance rows yet.
    expect(createWrapper(container).findAllSelects()).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Submit label' })).toBeEnabled();
  });
});

/* ------------------------------------------------------------------ */
/* Add / close / edit (Req 2.2, 2.3)                                   */
/* ------------------------------------------------------------------ */

describe('polygon add, close and edit (Req 2.2, 2.3)', () => {
  it('adds vertices by click, closes on the first vertex, and rasterises to RLE with the tight box', () => {
    const { canvas, ref } = renderCanvas();
    click(canvas, 2, 2);
    click(canvas, 10, 2);
    expect(hint()).toContain('2 of 3 distinct vertices placed');
    click(canvas, 10, 8);
    expect(closeButton()).toBeEnabled();
    click(canvas, 2, 8);
    expect(instanceRows()).toHaveLength(0);
    // Closing: click the first vertex.
    click(canvas, 2, 2);
    expect(instanceRows()).toHaveLength(1);
    expect(screen.getByText(/4 vertices \(2, 2\) 8×6/)).toBeInTheDocument();

    const annotation = ref.current!.getAnnotation();
    expect(annotation.modality).toBe('InstanceSegmentation');
    expect(annotation.image_size).toEqual({ width: WIDTH, height: HEIGHT });
    expect(annotation.instances).toHaveLength(1);
    const [instance] = annotation.instances!;
    expect(instance.class).toBe('scratch');
    expect(instance.box).toEqual({ left: 2, top: 2, width: 8, height: 6 });
    // The RLE is the canonical column-major counts string of the
    // rasterised polygon, and round-trips through the codec (Req 2.7).
    const expectedMask = rectMask(2, 2, 8, 6);
    expect(instance.rle).toBe(rleOf(expectedMask));
    const counts = parseRleCounts(instance.rle);
    expect(counts.reduce((a, b) => a + b, 0)).toBe(WIDTH * HEIGHT);
    const decoded = decodeRleColumnMajor(counts, WIDTH, HEIGHT);
    expect(Array.from(decoded)).toEqual(Array.from(expectedMask));
    expect(encodeRleColumnMajor(decoded, WIDTH, HEIGHT, 1)).toEqual(counts);
    expect(ref.current!.validate()).toEqual([]);
  });

  it('closes by double-click and encodes a border-touching instance', () => {
    const { canvas, ref } = renderCanvas();
    // A triangle hugging the top-left corner: vertices on the image edge.
    click(canvas, 0, 0);
    click(canvas, 8, 0);
    click(canvas, 0, 6);
    fireEvent.dblClick(canvas);
    expect(instanceRows()).toHaveLength(1);
    const [instance] = ref.current!.getAnnotation().instances!;
    const expected = rasterizePolygonEvenOdd([[0, 0], [8, 0], [0, 6]], WIDTH, HEIGHT);
    expect(instance.rle).toBe(rleOf(expected));
    // The first pixel is foreground, so the canonical form leads with 0.
    expect(instance.rle.startsWith('0 ')).toBe(true);
    expect(instance.box).toEqual(tightBoxFromMask(expected, WIDTH, HEIGHT));
    expect(instance.box!.left).toBe(0);
    expect(instance.box!.top).toBe(0);
    const decoded = decodeRleColumnMajor(parseRleCounts(instance.rle), WIDTH, HEIGHT);
    expect(Array.from(decoded)).toEqual(Array.from(expected));
  });

  it('drags a vertex of the selected polygon to edit it (one undo step per drag)', () => {
    const { canvas, ref } = renderCanvas();
    drawPolygon(canvas, [[2, 2], [10, 2], [10, 8], [2, 8]]);
    // Drag the bottom-right vertex (10, 8) to (14, 11): the instance is
    // selected right after closing, so its handles are live.
    drag(canvas, [10, 8], [14, 11]);
    expect(screen.getByText(/4 vertices \(2, 2\) 12×9/)).toBeInTheDocument();
    const [instance] = ref.current!.getAnnotation().instances!;
    expect(instance.box).toEqual({ left: 2, top: 2, width: 12, height: 9 });
    expect(instance.rle).toBe(
      rleOf(rasterizePolygonEvenOdd([[2, 2], [10, 2], [14, 11], [2, 8]], WIDTH, HEIGHT))
    );
    // Undo reverts the drag as a whole.
    fireEvent.click(undoButton());
    expect(ref.current!.getAnnotation().instances![0].box).toEqual({
      left: 2, top: 2, width: 8, height: 6,
    });
  });

  it('refuses to close below three distinct vertices (Req 2.2)', () => {
    const { canvas, ref, onSubmit } = renderCanvas();
    click(canvas, 3, 3);
    click(canvas, 9, 3);
    expect(closeButton()).toBeDisabled();
    fireEvent.dblClick(canvas);
    expect(instanceRows()).toHaveLength(0);
    expect(hint()).toContain('at least three distinct vertices');
    // A third click on the last vertex's spot adds no distinct vertex.
    click(canvas, 9, 3.2);
    expect(closeButton()).toBeDisabled();
    fireEvent.dblClick(canvas);
    expect(instanceRows()).toHaveLength(0);
    // Submitting with the open draft is blocked, nothing is sent.
    fireEvent.click(screen.getByRole('button', { name: 'Submit label' }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText(/An unfinished polygon is open/)).toBeInTheDocument();
    expect(ref.current!.getAnnotation().instances).toEqual([]);
    // Undo removes draft vertices one at a time.
    fireEvent.click(undoButton());
    expect(hint()).toContain('1 of 3 distinct vertices placed');
  });
});

/* ------------------------------------------------------------------ */
/* Select / reclass / delete / undo (Req 2.3)                          */
/* ------------------------------------------------------------------ */

describe('instance select, reclass, delete and undo (Req 2.3)', () => {
  it('reclasses and deletes through the instance rows, with undo reverting each edit', () => {
    const { canvas, ref, container } = renderCanvas();
    drawPolygon(canvas, [[1, 1], [6, 1], [6, 5], [1, 5]]);
    drawPolygon(canvas, [[8, 6], [14, 6], [14, 11], [8, 11]]);
    expect(instanceRows()).toHaveLength(2);
    expect(ref.current!.getAnnotation().instances!.map((i) => i.class)).toEqual([
      'scratch', 'scratch',
    ]);

    // Select instance 1 explicitly, then reclass instance 2.
    fireEvent.click(screen.getByTestId('select-instance-1'));
    const row2 = instanceRowSelect(container, 1);
    row2.openDropdown();
    row2.selectOptionByValue('dent');
    expect(ref.current!.getAnnotation().instances!.map((i) => i.class)).toEqual([
      'scratch', 'dent',
    ]);

    // Undo the reclass.
    fireEvent.click(undoButton());
    expect(ref.current!.getAnnotation().instances!.map((i) => i.class)).toEqual([
      'scratch', 'scratch',
    ]);

    // Delete instance 1; the other instance keeps its geometry.
    fireEvent.click(screen.getByRole('button', { name: 'Delete instance 1' }));
    const remaining = ref.current!.getAnnotation().instances!;
    expect(remaining).toHaveLength(1);
    expect(remaining[0].box).toEqual({ left: 8, top: 6, width: 6, height: 5 });

    // Undo the delete, then undo both additions back to an empty canvas.
    fireEvent.click(undoButton());
    expect(ref.current!.getAnnotation().instances).toHaveLength(2);
    fireEvent.click(undoButton());
    fireEvent.click(undoButton());
    expect(ref.current!.getAnnotation().instances).toEqual([]);
    expect(undoButton()).toBeDisabled();
  });

  it('blocks submission of a classless instance and of a zero-area polygon (Req 2.2)', () => {
    const { canvas, ref, onSubmit } = renderCanvas();
    // Three distinct but collinear vertices: closable, but covers no pixel.
    click(canvas, 2, 5);
    click(canvas, 8, 5);
    click(canvas, 14, 5);
    fireEvent.click(closeButton());
    expect(instanceRows()).toHaveLength(1);
    const errors = ref.current!.validate();
    expect(errors).toEqual([
      'Instance 1 covers no pixel: reshape or delete the instance.',
    ]);
    fireEvent.click(screen.getByRole('button', { name: 'Submit label' }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText('The label is incomplete')).toBeInTheDocument();
    // An empty rasterisation carries no box in the (unsent) annotation.
    expect(ref.current!.getAnnotation().instances![0].box).toBeUndefined();
  });

  it('submits the serialised instances when complete', () => {
    const { canvas, onSubmit } = renderCanvas();
    drawPolygon(canvas, [[2, 2], [10, 2], [10, 8], [2, 8]]);
    fireEvent.click(screen.getByRole('button', { name: 'Submit label' }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const sent = onSubmit.mock.calls[0][0] as DdaAnnotation;
    expect(sent.modality).toBe('InstanceSegmentation');
    expect(sent.instances).toEqual([
      {
        class: 'scratch',
        rle: rleOf(rectMask(2, 2, 8, 6)),
        box: { left: 2, top: 2, width: 8, height: 6 },
      },
    ]);
    expect(sent.image_size).toEqual({ width: WIDTH, height: HEIGHT });
  });
});

/* ------------------------------------------------------------------ */
/* Pre-label instances (Req 2.4)                                       */
/* ------------------------------------------------------------------ */

describe('pre-label instances stay separate (Req 2.4)', () => {
  const left = rleOf(rectMask(0, 0, 4, 12));
  const right = rleOf(rectMask(4, 0, 4, 12)); // touches `left`, same class
  const prelabel: DdaAnnotation = {
    modality: 'InstanceSegmentation',
    image_size: { width: WIDTH, height: HEIGHT },
    instances: [
      { class: 'scratch', rle: left, box: { left: 0, top: 0, width: 4, height: 12 } },
      { class: 'scratch', rle: right },
      { class: null, rle: rleOf(rectMask(10, 2, 3, 3)) },
    ],
  };

  it('renders one row per instance, keeps their RLE verbatim and derives missing boxes', () => {
    const { ref } = renderCanvas(prelabel);
    expect(instanceRows()).toHaveLength(3);
    expect(screen.getAllByText(/pre-label mask/)).toHaveLength(3);
    expect(screen.getByTestId('prelabel-instance-hint').textContent).toContain(
      'keep their outline'
    );
    expect(screen.getByText('Model proposals need classification')).toBeInTheDocument();
    const instances = ref.current!.getAnnotation().instances!;
    expect(instances.map((i) => i.rle)).toEqual([left, right, prelabel.instances![2].rle]);
    expect(instances[1].box).toEqual({ left: 4, top: 0, width: 4, height: 12 });
    expect(instances[2].class).toBeNull();
    // The classless proposal blocks submission until classified.
    expect(ref.current!.validate()).toEqual([
      'Instance 3 has no class assigned: pick a class or delete the instance.',
    ]);
  });

  it('reclasses and deletes a pre-label instance individually, never merging neighbours', () => {
    const { ref, container } = renderCanvas(prelabel);
    const row3 = instanceRowSelect(container, 2);
    row3.openDropdown();
    row3.selectOptionByValue('dent');
    expect(ref.current!.getAnnotation().instances!.map((i) => i.class)).toEqual([
      'scratch', 'scratch', 'dent',
    ]);
    fireEvent.click(screen.getByRole('button', { name: 'Delete instance 1' }));
    const remaining = ref.current!.getAnnotation().instances!;
    expect(remaining.map((i) => i.rle)).toEqual([right, prelabel.instances![2].rle]);
    expect(ref.current!.validate()).toEqual([]);
    // Clearing pre-labels removes every prelabel-origin instance, the
    // reclassed one included; restore brings all three back.
    fireEvent.click(screen.getByTestId('clear-prelabels'));
    expect(ref.current!.getAnnotation().instances).toEqual([]);
    fireEvent.click(screen.getByTestId('restore-prelabels'));
    expect(ref.current!.getAnnotation().instances!.map((i) => i.rle)).toEqual([
      right, prelabel.instances![2].rle,
    ]);
  });

  it('keeps user polygons apart from pre-label instances across a clear', () => {
    const { ref, canvas } = renderCanvas(prelabel);
    drawPolygon(canvas, [[10, 8], [15, 8], [15, 11], [10, 11]]);
    expect(within(document.body).getAllByText(/^Instance \d+$/)).toHaveLength(4);
    fireEvent.click(screen.getByTestId('clear-prelabels'));
    const instances = ref.current!.getAnnotation().instances!;
    expect(instances).toHaveLength(1);
    expect(instances[0].box).toEqual({ left: 10, top: 8, width: 5, height: 3 });
  });
});
