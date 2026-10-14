/**
 * Example tests for the AnnotationCanvas Clear_Prelabels_Control
 * (labeling-job-cleanup-work-stealing-and-podium task 2.13,
 * Requirements 9.1, 9.7, 9.8).
 *
 * Covers, by example:
 * - the Clear_Prelabels_Control is offered exactly when the presented task
 *   carries a non-empty Pre_Label (Req 9.1);
 * - a submission after clearing carries exactly the on-canvas state through
 *   the existing completeness validation: an ObjectDetection submission
 *   holds only the user-drawn box once the prelabel box is cleared, and a
 *   Segmentation task cleared to empty submits an empty regions list
 *   (Req 9.7);
 * - a presigned-URL refresh after clearing preserves the cleared state and
 *   the Restore_Control, exactly as annotation state is already preserved
 *   across URL swaps (Req 9.8).
 *
 * jsdom scaffolding: images never load in jsdom, so `fireEvent.load` with
 * `naturalWidth`/`naturalHeight` defined on the element stands in for a real
 * decode; the overlay canvas gets a minimal 2D-context stand-in (jsdom has
 * none) and a fixed `getBoundingClientRect` so pointer coordinates map 1:1
 * onto image pixels; pointer capture is stubbed (jsdom does not implement
 * it). The 2D-context stand-in follows the recording stub precedent in
 * `PromptTuningPreview.property.test.tsx`.
 */
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import AnnotationCanvas, { type LabelingModality } from './AnnotationCanvas';
import type { DdaAnnotation } from '../../services/api';

const INITIAL_URL = 'https://images.example/task.png?sig=initial';

/* ------------------------------------------------------------------ */
/* jsdom scaffolding                                                   */
/* ------------------------------------------------------------------ */

beforeAll(() => {
  // jsdom (29.x) implements neither pointer capture nor PointerEvent
  // capture transfer; the canvas pointer handlers call both.
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.releasePointerCapture = vi.fn();
});

beforeEach(() => {
  // jsdom has no 2D context; the overlay effect guards on a null context
  // but a minimal stand-in keeps the render path quiet and exercised.
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
/* Render / interaction helpers                                        */
/* ------------------------------------------------------------------ */

interface RenderOverrides {
  taskType?: LabelingModality;
  labelSet?: string[];
  prelabel?: DdaAnnotation;
  onImageUrlRefresh?: () => Promise<{
    image_url: string;
    image_url_expires_at?: number;
  }>;
}

function renderCanvas(overrides: RenderOverrides = {}) {
  const onSubmit = vi.fn();
  const onImageUrlRefresh =
    overrides.onImageUrlRefresh ??
    vi.fn(async () => ({ image_url: INITIAL_URL }));
  render(
    <AnnotationCanvas
      imageUrl={INITIAL_URL}
      taskType={overrides.taskType ?? 'Classification'}
      labelSet={overrides.labelSet ?? ['normal', 'anomaly']}
      prelabel={overrides.prelabel}
      onSubmit={onSubmit}
      onImageUrlRefresh={onImageUrlRefresh}
    />
  );
  return { onSubmit, onImageUrlRefresh };
}

function taskImage(): HTMLImageElement {
  return screen.getByAltText('Image to label') as HTMLImageElement;
}

/**
 * Simulate the image decode jsdom never performs: define the natural
 * dimensions on the element and fire its load event.
 */
function loadImage(width: number, height: number): HTMLImageElement {
  const img = taskImage();
  Object.defineProperty(img, 'naturalWidth', {
    value: width,
    configurable: true,
  });
  Object.defineProperty(img, 'naturalHeight', {
    value: height,
    configurable: true,
  });
  fireEvent.load(img);
  return img;
}

/**
 * Pin the overlay canvas's client rect to the image's pixel size so
 * `toImageCoords` maps pointer client coordinates 1:1 onto image pixels
 * (jsdom rects are otherwise all-zero, which aborts pointer handling).
 */
function fixOverlayRect(width: number, height: number): HTMLElement {
  const canvas = screen.getByTestId('annotation-overlay');
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

/** Drag-draw one ObjectDetection box on the overlay canvas. */
function drawBox(
  canvas: HTMLElement,
  from: { x: number; y: number },
  to: { x: number; y: number }
): void {
  fireEvent.pointerDown(canvas, {
    pointerId: 1,
    clientX: from.x,
    clientY: from.y,
  });
  fireEvent.pointerMove(canvas, { pointerId: 1, clientX: to.x, clientY: to.y });
  fireEvent.pointerUp(canvas, { pointerId: 1, clientX: to.x, clientY: to.y });
}

const submitButton = () => screen.getByRole('button', { name: 'Submit label' });

/* ------------------------------------------------------------------ */
/* Req 9.1 — control offered exactly for a non-empty Pre_Label         */
/* ------------------------------------------------------------------ */

describe('Clear_Prelabels_Control presence (Req 9.1)', () => {
  it('offers no clear control for a task without a Pre_Label (Req 9.1)', () => {
    renderCanvas({ taskType: 'Classification' });
    expect(screen.queryByTestId('clear-prelabels')).not.toBeInTheDocument();
    expect(screen.queryByTestId('restore-prelabels')).not.toBeInTheDocument();
  });

  it('offers no clear control for an empty Pre_Label (Req 9.1)', () => {
    renderCanvas({
      taskType: 'ObjectDetection',
      labelSet: ['scratch', 'dent'],
      prelabel: { modality: 'ObjectDetection', boxes: [] },
    });
    expect(screen.queryByTestId('clear-prelabels')).not.toBeInTheDocument();
    expect(screen.queryByTestId('restore-prelabels')).not.toBeInTheDocument();
  });

  it('offers the clear control when the task carries a non-empty Pre_Label (Req 9.1)', () => {
    renderCanvas({
      taskType: 'Classification',
      prelabel: { modality: 'Classification', label: 'anomaly' },
    });
    expect(screen.getByTestId('clear-prelabels')).toBeInTheDocument();
    expect(screen.queryByTestId('restore-prelabels')).not.toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/* Req 9.7 — post-clear submission carries the on-canvas state         */
/* ------------------------------------------------------------------ */

describe('post-clear submission carries exactly the on-canvas state (Req 9.7)', () => {
  it('submits only the user-drawn box after clearing an ObjectDetection Pre_Label (Req 9.7)', () => {
    const { onSubmit } = renderCanvas({
      taskType: 'ObjectDetection',
      labelSet: ['scratch', 'dent'],
      prelabel: {
        modality: 'ObjectDetection',
        boxes: [{ class: 'dent', left: 5, top: 5, width: 20, height: 20 }],
      },
    });
    loadImage(200, 100);
    const canvas = fixOverlayRect(200, 100);

    // One user-drawn box beside the prelabel box; new boxes take the
    // selected class (labelSet[0] = 'scratch').
    drawBox(canvas, { x: 10, y: 10 }, { x: 50, y: 40 });

    fireEvent.click(screen.getByTestId('clear-prelabels'));
    fireEvent.click(submitButton());

    // The submission went through the existing validation path (the
    // user-drawn box carries a class, so nothing blocks it) and carries
    // exactly the on-canvas state: the prelabel box is gone, the
    // user-drawn box survives untouched.
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const annotation = onSubmit.mock.calls[0][0] as DdaAnnotation;
    expect(annotation.modality).toBe('ObjectDetection');
    expect(annotation.boxes).toEqual([
      { class: 'scratch', left: 10, top: 10, width: 40, height: 30 },
    ]);
  });

  it('submits an empty regions list after clearing a Segmentation task to empty (Req 9.7)', () => {
    const { onSubmit } = renderCanvas({
      taskType: 'Segmentation',
      labelSet: ['scratch', 'dent'],
      prelabel: {
        // "0 16" paints every pixel of the 4x4 image with class scratch.
        modality: 'Segmentation',
        regions: [{ class: 'scratch', rle: '0 16' }],
      },
    });
    loadImage(4, 4);

    fireEvent.click(screen.getByTestId('clear-prelabels'));
    fireEvent.click(submitButton());

    // Segmentation validation only blocks classless proposals, so the
    // cleared-to-empty canvas submits cleanly with zero regions.
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const annotation = onSubmit.mock.calls[0][0] as DdaAnnotation;
    expect(annotation.modality).toBe('Segmentation');
    expect(annotation.regions).toEqual([]);
    expect(annotation.image_width).toBe(4);
    expect(annotation.image_height).toBe(4);
  });
});

/* ------------------------------------------------------------------ */
/* Req 9.8 — URL refresh after clearing preserves the cleared state    */
/* ------------------------------------------------------------------ */

describe('URL refresh after clearing (Req 9.8)', () => {
  it('preserves the cleared state and the Restore_Control across a presigned-URL refresh (Req 9.8)', async () => {
    const REFRESHED_URL = 'https://images.example/task.png?sig=refreshed';
    const onImageUrlRefresh = vi.fn(async () => ({
      image_url: REFRESHED_URL,
    }));
    const { onSubmit } = renderCanvas({
      taskType: 'Segmentation',
      labelSet: ['scratch', 'dent'],
      prelabel: {
        modality: 'Segmentation',
        regions: [{ class: 'scratch', rle: '0 16' }],
      },
      onImageUrlRefresh,
    });
    const img = loadImage(4, 4);

    fireEvent.click(screen.getByTestId('clear-prelabels'));
    expect(screen.getByTestId('restore-prelabels')).toBeInTheDocument();

    // A load error is the simplest refresh trigger: the canvas asks
    // `onImageUrlRefresh` for a fresh presigned URL and swaps the image
    // source without touching annotation state.
    fireEvent.error(img);
    await waitFor(() =>
      expect(taskImage()).toHaveAttribute('src', REFRESHED_URL)
    );
    expect(onImageUrlRefresh).toHaveBeenCalledTimes(1);

    // The refreshed image loads; segmentation state must not
    // re-initialize (the initialization guard), so the cleared bitmap
    // and the control state survive the swap.
    fireEvent.load(img);
    expect(screen.getByTestId('restore-prelabels')).toBeInTheDocument();
    expect(screen.queryByTestId('clear-prelabels')).not.toBeInTheDocument();

    // The cleared annotation state survives: submission still carries an
    // empty regions list.
    fireEvent.click(submitButton());
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect((onSubmit.mock.calls[0][0] as DdaAnnotation).regions).toEqual([]);

    // And the surviving Restore_Control still works: it reinstates the
    // Prelabel_Snapshot exactly and re-offers the clear control.
    fireEvent.click(screen.getByTestId('restore-prelabels'));
    expect(screen.getByTestId('clear-prelabels')).toBeInTheDocument();
    fireEvent.click(submitButton());
    expect(onSubmit).toHaveBeenCalledTimes(2);
    expect((onSubmit.mock.calls[1][0] as DdaAnnotation).regions).toEqual([
      { class: 'scratch', rle: '0 16' },
    ]);
  });
});
