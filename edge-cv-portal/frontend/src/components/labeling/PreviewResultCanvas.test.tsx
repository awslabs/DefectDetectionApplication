/**
 * Vitest unit tests for `PreviewResultCanvas` (llm-autolabel-prompt-tuning
 * task 12.11, Requirements 4.1, 4.2, 4.3, 4.4).
 *
 * Covers, by example:
 * - ObjectDetection boxes positioned proportionally to the displayed image
 *   (percentage geometry), clamped through `clampBoxToImage`, each with its
 *   Label_Set class name adjacent and an unclassified fallback (Req 4.1);
 * - Segmentation mask regions decoded through the shared RLE helpers
 *   (`parseRleCounts` / `decodeRleColumnMajor`) and painted with the shared
 *   `CLASS_PALETTE` color per class, with each region's class name
 *   associated in the legend (Req 4.2);
 * - the Classification label shown beside the image (Req 4.3);
 * - the zero-detection indication, present for an empty Pre_Label in every
 *   modality and absent for a populated one (Req 4.4).
 *
 * The regression guard at the end (llm-model-token-and-image-sizing task
 * 11.5, Requirement 7.7) pins the component to its pre-sizing-feature
 * surface: the props interface gained no new props, the source references
 * none of the sizing fields, and the `PromptTuningPreview` call site still
 * hands it `payload.image_width` / `image_height` — the Source_Dimensions —
 * unchanged, so the new `sent_*` fields stay display-only.
 */
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import PreviewResultCanvas from './PreviewResultCanvas';
import type { PreviewResultPayload } from '../../services/api';
// Verbatim component sources for the regression guard (Vite raw imports).
import canvasSource from './PreviewResultCanvas.tsx?raw';
import previewSource from './PromptTuningPreview.tsx?raw';
import {
  CLASS_PALETTE,
  decodeRleColumnMajor,
  parseRleCounts,
} from './AnnotationCanvas';

const IMAGE_URL = 'https://s3.example/sample.jpg';
const LABEL_SET = ['scratch', 'dent'];

/** `rgb(r, g, b)` for a palette hex, the form jsdom serializes colors in. */
function rgbOf(hex: string): [number, number, number] {
  const value = parseInt(hex.slice(1), 16);
  return [(value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff];
}

/* ------------------------------------------------------------------ */
/* ObjectDetection (Req 4.1)                                           */
/* ------------------------------------------------------------------ */

describe('PreviewResultCanvas — ObjectDetection boxes', () => {
  it('positions each box proportionally to the image and labels it with its class', () => {
    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="ObjectDetection"
        labelSet={LABEL_SET}
        imageWidth={200}
        imageHeight={100}
        prelabel={{
          modality: 'ObjectDetection',
          boxes: [
            { class: 'scratch', left: 50, top: 25, width: 100, height: 50 },
            { class: 'dent', left: 0, top: 0, width: 20, height: 10 },
          ],
        }}
      />
    );

    const boxes = screen.getAllByTestId('preview-box');
    expect(boxes).toHaveLength(2);

    // 50/200, 25/100, 100/200, 50/100 — percentages, so the overlay scales
    // with whatever size the image is displayed at.
    expect(boxes[0].style.left).toBe('25%');
    expect(boxes[0].style.top).toBe('25%');
    expect(boxes[0].style.width).toBe('50%');
    expect(boxes[0].style.height).toBe('50%');

    expect(boxes[1].style.left).toBe('0%');
    expect(boxes[1].style.top).toBe('0%');
    expect(boxes[1].style.width).toBe('10%');
    expect(boxes[1].style.height).toBe('10%');

    const classNames = screen
      .getAllByTestId('preview-box-class')
      .map((node) => node.textContent);
    expect(classNames).toEqual(['scratch', 'dent']);
  });

  it('clamps a box that overruns the image to the image bounds', () => {
    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="ObjectDetection"
        labelSet={LABEL_SET}
        imageWidth={200}
        imageHeight={100}
        prelabel={{
          modality: 'ObjectDetection',
          boxes: [{ class: 'dent', left: 180, top: 90, width: 100, height: 100 }],
        }}
      />
    );

    const box = screen.getByTestId('preview-box');
    expect(box.style.left).toBe('90%');
    expect(box.style.top).toBe('90%');
    expect(box.style.width).toBe('10%');
    expect(box.style.height).toBe('10%');
  });

  it('renders an unclassified box without a Label_Set class name', () => {
    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="ObjectDetection"
        labelSet={LABEL_SET}
        imageWidth={100}
        imageHeight={100}
        prelabel={{
          modality: 'ObjectDetection',
          boxes: [{ class: null, left: 10, top: 10, width: 10, height: 10 }],
        }}
      />
    );

    expect(screen.getByTestId('preview-box-class')).toHaveTextContent(
      'unclassified'
    );
  });

  it('renders the sample image with the provided accessible description', () => {
    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="ObjectDetection"
        labelSet={LABEL_SET}
        imageWidth={100}
        imageHeight={100}
        prelabel={{ modality: 'ObjectDetection', boxes: [] }}
        alt="Preview result for images/one.jpg"
      />
    );

    const image = screen.getByAltText('Preview result for images/one.jpg');
    expect(image).toHaveAttribute('src', IMAGE_URL);
    expect(screen.getByTestId('preview-result-image')).toContainElement(image);
  });
});

/* ------------------------------------------------------------------ */
/* Segmentation (Req 4.2)                                              */
/* ------------------------------------------------------------------ */

describe('PreviewResultCanvas — Segmentation masks', () => {
  let painted: { data: Uint8ClampedArray; width: number; height: number }[] = [];

  beforeEach(() => {
    painted = [];
    // jsdom has no 2D context, so stand one in that records the pixels the
    // component paints from the decoded RLE.
    const context = {
      clearRect: vi.fn(),
      createImageData: (width: number, height: number) => ({
        data: new Uint8ClampedArray(width * height * 4),
        width,
        height,
        colorSpace: 'srgb' as const,
      }),
      putImageData: (imageData: {
        data: Uint8ClampedArray;
        width: number;
        height: number;
      }) => {
        painted.push({
          data: new Uint8ClampedArray(imageData.data),
          width: imageData.width,
          height: imageData.height,
        });
      },
    };
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
      ((kind: string) =>
        kind === '2d'
          ? (context as unknown as CanvasRenderingContext2D)
          : null) as unknown as HTMLCanvasElement['getContext']
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('decodes the region RLE through the shared helper and fills it with the class color', () => {
    // Column-major counts over a 2x2 image: one zero, one one, one zero,
    // one one — the bottom row of the image.
    const rle = '1 1 1 1';
    const expectedMask = decodeRleColumnMajor(parseRleCounts(rle), 2, 2);
    expect(Array.from(expectedMask)).toEqual([0, 0, 1, 1]);

    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="Segmentation"
        labelSet={LABEL_SET}
        imageWidth={2}
        imageHeight={2}
        prelabel={{ modality: 'Segmentation', regions: [{ class: 'dent', rle }] }}
      />
    );

    const canvas = screen.getByTestId('preview-mask-overlay') as HTMLCanvasElement;
    expect(canvas.width).toBe(2);
    expect(canvas.height).toBe(2);

    expect(painted).toHaveLength(1);
    const { data } = painted[0];
    // 'dent' is Label_Set index 1, so it takes CLASS_PALETTE[1].
    const [r, g, b] = rgbOf(CLASS_PALETTE[1]);
    for (let pixel = 0; pixel < expectedMask.length; pixel++) {
      const offset = pixel * 4;
      if (expectedMask[pixel]) {
        expect([
          data[offset],
          data[offset + 1],
          data[offset + 2],
          data[offset + 3],
        ]).toEqual([r, g, b, 140]);
      } else {
        expect(data[offset + 3]).toBe(0);
      }
    }
  });

  it('associates every region class name with its fill in the legend', () => {
    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="Segmentation"
        labelSet={LABEL_SET}
        imageWidth={2}
        imageHeight={2}
        prelabel={{
          modality: 'Segmentation',
          regions: [
            { class: 'scratch', rle: '0 1 3' },
            { class: 'dent', rle: '1 1 1 1' },
          ],
        }}
      />
    );

    expect(screen.getByTestId('preview-region-legend')).toBeInTheDocument();
    expect(
      screen.getAllByTestId('preview-region-class').map((n) => n.textContent)
    ).toEqual(['scratch', 'dent']);
    expect(screen.queryByTestId('preview-empty-result')).not.toBeInTheDocument();
  });

  it('shows the empty-result indication and paints nothing for zero regions', () => {
    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="Segmentation"
        labelSet={LABEL_SET}
        imageWidth={2}
        imageHeight={2}
        prelabel={{ modality: 'Segmentation', regions: [] }}
      />
    );

    expect(screen.getByTestId('preview-empty-result')).toBeInTheDocument();
    expect(screen.queryByTestId('preview-region-legend')).not.toBeInTheDocument();
    expect(painted).toHaveLength(0);
  });
});

/* ------------------------------------------------------------------ */
/* Classification (Req 4.3) and emptiness (Req 4.4)                    */
/* ------------------------------------------------------------------ */

describe('PreviewResultCanvas — Classification and emptiness', () => {
  it('shows the anomaly label beside the image without an empty-result indication', () => {
    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="Classification"
        labelSet={['normal', 'anomaly']}
        imageWidth={64}
        imageHeight={64}
        prelabel={{ modality: 'Classification', label: 'anomaly' }}
      />
    );

    expect(screen.getByTestId('preview-classification-label')).toHaveTextContent(
      'anomaly'
    );
    expect(screen.queryByTestId('preview-empty-result')).not.toBeInTheDocument();
    expect(screen.queryByTestId('preview-box')).not.toBeInTheDocument();
  });

  it('shows the normal label together with the empty-result indication', () => {
    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="Classification"
        labelSet={['normal', 'anomaly']}
        imageWidth={64}
        imageHeight={64}
        prelabel={{ modality: 'Classification', label: 'normal' }}
      />
    );

    expect(screen.getByTestId('preview-classification-label')).toHaveTextContent(
      'normal'
    );
    expect(screen.getByTestId('preview-empty-result')).toBeInTheDocument();
  });

  it('indicates zero detections for an ObjectDetection Pre_Label with no boxes', () => {
    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="ObjectDetection"
        labelSet={LABEL_SET}
        imageWidth={100}
        imageHeight={100}
        prelabel={{ modality: 'ObjectDetection', boxes: [] }}
      />
    );

    const empty = screen.getByTestId('preview-empty-result');
    expect(empty).toHaveTextContent(/no detections/i);
    expect(screen.queryByTestId('preview-box')).not.toBeInTheDocument();
  });

  it('omits the empty-result indication when boxes are present', () => {
    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="ObjectDetection"
        labelSet={LABEL_SET}
        imageWidth={100}
        imageHeight={100}
        prelabel={{
          modality: 'ObjectDetection',
          boxes: [{ class: 'scratch', left: 1, top: 1, width: 5, height: 5 }],
        }}
      />
    );

    expect(screen.queryByTestId('preview-empty-result')).not.toBeInTheDocument();
    expect(screen.getAllByTestId('preview-box')).toHaveLength(1);
  });
});

/* ------------------------------------------------------------------ */
/* Sizing regression guard (llm-model-token-and-image-sizing Req 7.7)  */
/* ------------------------------------------------------------------ */

describe('PreviewResultCanvas — sizing regression guard', () => {
  it('declares exactly the pre-feature props — the canvas gained no new props', () => {
    const interfaceMatch = canvasSource.match(
      /export interface PreviewResultCanvasProps \{([\s\S]*?)\n\}/
    );
    expect(interfaceMatch).not.toBeNull();
    const propNames = Array.from(
      interfaceMatch![1].matchAll(/^ {2}(\w+)\??:/gm),
      (m) => m[1]
    );
    expect(propNames).toEqual([
      'imageUrl',
      'taskType',
      'labelSet',
      'prelabel',
      'imageWidth',
      'imageHeight',
      'alt',
    ]);
  });

  it('references none of the sizing fields anywhere in its source', () => {
    for (const forbidden of [
      'sent_width',
      'sent_height',
      'sentWidth',
      'sentHeight',
      'source_width',
      'source_height',
      'downscale',
      'token_budget',
      'tokenBudget',
    ]) {
      expect(canvasSource).not.toContain(forbidden);
    }
  });

  it('still receives payload.image_width / image_height unchanged from the preview', () => {
    // The one call site keeps feeding the payload's Source_Dimensions to
    // the canvas, exactly as before the sizing feature (Req 7.7).
    expect(previewSource).toMatch(
      /imageWidth=\{result\.payload\.image_width \?\? 0\}/
    );
    expect(previewSource).toMatch(
      /imageHeight=\{result\.payload\.image_height \?\? 0\}/
    );
    // And never the Sent_Dimensions.
    expect(previewSource).not.toMatch(/imageWidth=\{[^}]*sent_/);
    expect(previewSource).not.toMatch(/imageHeight=\{[^}]*sent_/);
  });

  it('positions geometry against the Source_Dimensions even when the payload carries divergent sent dimensions', () => {
    // A payload in the extended result shape: Source_Dimensions 200×100,
    // Sent_Dimensions 100×50. The canvas is handed the source pair through
    // the unchanged call-site expression, so the box lands at percentages
    // of the source image, not of the downscaled one.
    const payload: PreviewResultPayload = {
      sample_key: 'images/one.jpg',
      state: 'Succeeded',
      prelabel: {
        modality: 'ObjectDetection',
        boxes: [{ class: 'scratch', left: 50, top: 25, width: 100, height: 50 }],
      },
      image_width: 200,
      image_height: 100,
      source_width: 200,
      source_height: 100,
      sent_width: 100,
      sent_height: 50,
      downscale_max_edge: 512,
    };

    render(
      <PreviewResultCanvas
        imageUrl={IMAGE_URL}
        taskType="ObjectDetection"
        labelSet={LABEL_SET}
        prelabel={payload.prelabel!}
        imageWidth={payload.image_width ?? 0}
        imageHeight={payload.image_height ?? 0}
      />
    );

    const box = screen.getByTestId('preview-box');
    // 50/200 and 25/100 — the source space. Sent-space rendering would put
    // the box at 50% / 50% instead.
    expect(box.style.left).toBe('25%');
    expect(box.style.top).toBe('25%');
    expect(box.style.width).toBe('50%');
    expect(box.style.height).toBe('50%');
  });
});
