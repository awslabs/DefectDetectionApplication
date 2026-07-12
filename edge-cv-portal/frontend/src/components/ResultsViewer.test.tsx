// Feature: object-detection-visualization
//
// Component tests for the Results_Viewer (Requirement 4). These exercise the
// rendered DOM in jsdom to verify the detection box overlay, per-box labels,
// the server-overlay toggle, the anomaly presentation, and the no-objects
// indicator.
//
// Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ResultsViewer, { type Capture, type Detection } from './ResultsViewer';

/**
 * The SVG box overlay only renders once the source `<img>` reports non-zero
 * natural + client dimensions (see `canScale` in ResultsViewer). In jsdom these
 * are `0` by default, so we install prototype getters that return fixed
 * dimensions and then dispatch the image `load` event so `measure()` runs.
 *
 * Source = 1000x800, displayed = 500x400 (a 0.5x scale on each axis), so
 * `scaleBox` produces non-zero boxes we can count and inspect.
 */
const NATURAL_W = 1000;
const NATURAL_H = 800;
const CLIENT_W = 500;
const CLIENT_H = 400;

const originalDescriptors: Record<string, PropertyDescriptor | undefined> = {};

function defineImgDimension(prop: string, value: number) {
  originalDescriptors[prop] = Object.getOwnPropertyDescriptor(
    HTMLImageElement.prototype,
    prop
  );
  Object.defineProperty(HTMLImageElement.prototype, prop, {
    configurable: true,
    get() {
      return value;
    },
  });
}

beforeAll(() => {
  defineImgDimension('naturalWidth', NATURAL_W);
  defineImgDimension('naturalHeight', NATURAL_H);
  defineImgDimension('clientWidth', CLIENT_W);
  defineImgDimension('clientHeight', CLIENT_H);
});

afterAll(() => {
  for (const [prop, descriptor] of Object.entries(originalDescriptors)) {
    if (descriptor) {
      Object.defineProperty(HTMLImageElement.prototype, prop, descriptor);
    } else {
      delete (HTMLImageElement.prototype as unknown as Record<string, unknown>)[prop];
    }
  }
});

/** Fire the load event on the source capture image so `measure()` runs. */
function loadSourceImage(captureId: string) {
  const img = screen.getByAltText(`Capture ${captureId}`) as HTMLImageElement;
  fireEvent.load(img);
  return img;
}

function makeDetection(overrides: Partial<Detection> = {}): Detection {
  return {
    class_index: '17',
    class_label: 'dog',
    bounding_box: [100, 200, 300, 400],
    confidence: 0.83,
    ...overrides,
  };
}

function makeDetectionCapture(overrides: Partial<Capture> = {}): Capture {
  const detections = overrides.detections ?? [makeDetection()];
  const base: Capture = {
    capture_id: 'cap-001',
    inference_result_type: 'Detection',
    detection_count: detections.length,
    detections,
    source_url: 'https://example.com/source.jpg',
    overlay_url: 'https://example.com/overlay.jpg',
    mask_url: null,
  };
  return { ...base, ...overrides, detections };
}

describe('ResultsViewer', () => {
  // Requirement 4.1: renders one <rect> per detection over the source image.
  it('renders N boxes for a detection capture with N detections', () => {
    const detections: Detection[] = [
      makeDetection({ class_label: 'dog', bounding_box: [10, 20, 110, 120] }),
      makeDetection({ class_label: 'cat', bounding_box: [200, 100, 400, 300] }),
      makeDetection({ class_label: 'person', bounding_box: [500, 50, 700, 250] }),
    ];
    const capture = makeDetectionCapture({ detections });
    const { container } = render(<ResultsViewer capture={capture} />);

    loadSourceImage(capture.capture_id);

    const rects = container.querySelectorAll('svg rect');
    expect(rects.length).toBe(detections.length);
  });

  // Requirement 4.2: each box shows its human-readable label + confidence.
  it('renders the class label and confidence percentage for each box', () => {
    const detections: Detection[] = [
      makeDetection({ class_label: 'dog', confidence: 0.83 }),
      makeDetection({ class_label: 'cat', confidence: 0.5, bounding_box: [400, 300, 600, 500] }),
    ];
    const capture = makeDetectionCapture({ detections });
    const { container } = render(<ResultsViewer capture={capture} />);

    loadSourceImage(capture.capture_id);

    const texts = Array.from(container.querySelectorAll('svg text')).map(
      (t) => t.textContent
    );
    expect(texts).toContain('dog 83%');
    expect(texts).toContain('cat 50%');
  });

  // Requirement 4.2 (fallback): falls back to class_index when label is empty.
  it('falls back to the class index when class_label is empty', () => {
    const capture = makeDetectionCapture({
      detections: [makeDetection({ class_label: '', class_index: '42', confidence: 0.6 })],
    });
    const { container } = render(<ResultsViewer capture={capture} />);

    loadSourceImage(capture.capture_id);

    const texts = Array.from(container.querySelectorAll('svg text')).map(
      (t) => t.textContent
    );
    expect(texts).toContain('42 60%');
  });

  // Requirement 4.3: the toggle switches to the server-rendered overlay image.
  it('switches to the server overlay image when the toggle is enabled', async () => {
    const user = userEvent.setup();
    const capture = makeDetectionCapture({
      overlay_url: 'https://example.com/overlay.jpg',
    });
    render(<ResultsViewer capture={capture} />);

    // Before toggling, the server overlay image is not shown.
    expect(
      screen.queryByAltText(`Server-rendered overlay for ${capture.capture_id}`)
    ).not.toBeInTheDocument();

    const toggle = screen.getByRole('checkbox', { name: /show server overlay/i });
    expect(toggle).not.toBeDisabled();
    await user.click(toggle);

    const overlayImg = screen.getByAltText(
      `Server-rendered overlay for ${capture.capture_id}`
    ) as HTMLImageElement;
    expect(overlayImg).toBeInTheDocument();
    expect(overlayImg.src).toBe(capture.overlay_url);
  });

  // Requirement 4.3: the overlay toggle is disabled when no overlay URL exists.
  it('disables the server-overlay toggle when overlay_url is null', () => {
    const capture = makeDetectionCapture({ overlay_url: null });
    render(<ResultsViewer capture={capture} />);

    const toggle = screen.getByRole('checkbox', { name: /show server overlay/i });
    expect(toggle).toBeDisabled();
  });

  // Requirement 4.4: anomaly captures show the mask overlay and no detection boxes.
  it('renders the anomaly presentation (mask overlay, no detection boxes) for a non-Detection capture', () => {
    const capture: Capture = {
      capture_id: 'cap-anom',
      inference_result_type: 'Anomaly',
      detection_count: 0,
      detections: [],
      source_url: 'https://example.com/source.jpg',
      overlay_url: null,
      mask_url: 'https://example.com/mask.png',
    };
    const { container } = render(<ResultsViewer capture={capture} />);

    // The mask overlay image is rendered on top of the source image.
    const maskImg = screen.getByAltText(
      `Anomaly mask for ${capture.capture_id}`
    ) as HTMLImageElement;
    expect(maskImg).toBeInTheDocument();
    expect(maskImg.src).toBe(capture.mask_url);

    // No detection SVG boxes are drawn for an anomaly capture.
    expect(container.querySelectorAll('svg rect').length).toBe(0);
    // And no detection/overlay toggle is present.
    expect(
      screen.queryByRole('checkbox', { name: /show server overlay/i })
    ).not.toBeInTheDocument();
  });

  // Requirement 4.5: zero-object detection captures show a "No objects detected" indicator.
  it('shows a "No objects detected" indicator and no boxes when detection_count is 0', () => {
    const capture = makeDetectionCapture({
      detections: [],
      detection_count: 0,
    });
    const { container } = render(<ResultsViewer capture={capture} />);

    loadSourceImage(capture.capture_id);

    expect(screen.getByText('No objects detected')).toBeInTheDocument();
    expect(container.querySelectorAll('svg rect').length).toBe(0);
  });

  // Requirement 4 (guard): nothing selected renders the placeholder prompt.
  it('shows a placeholder when no capture is selected', () => {
    render(<ResultsViewer capture={null} />);
    expect(
      screen.getByText('Select a capture to view its results.')
    ).toBeInTheDocument();
  });
});
