/**
 * rfdetr-seg-support task 11.4 (Requirements 11.2, 11.3, 11.5; design §11
 * `ResultsViewer.tsx`, DD20): the prop-driven instance presentation of the
 * Results_Viewer for a `'Detection'` capture with `has_masks`.
 *
 * - `'loading'`: boxes from the page at once, a spinner beside the toggle, no
 *   mask canvas.
 * - `'ready'`: the mask canvas under the SVG boxes, painted once per capture
 *   from `rleToImageData` (jsdom has no canvas: `getContext` is a recording
 *   stub and the painted layer is asserted from `putImageData`'s argument);
 *   `buildInstanceMaskLayer` composites the decodable masks, most confident
 *   on top, in each instance's palette colour at 40 % alpha, and skips an
 *   RLE that does not decode at the source size.
 * - `'unavailable'`: the warning and the boxes.
 * - the truncated-instances note whenever `detections_truncated` is set,
 *   detail loaded or not; an all-truncated capture keeps it.
 * - the three-way toggle: masks + boxes | boxes only | server overlay
 *   (disabled when `overlay_url` is null).
 * - the detection-only and anomaly presentations are unchanged (the existing
 *   ResultsViewer.test.tsx passes unmodified; a `'Detection'` capture without
 *   `has_masks` still gets the Toggle, not the SegmentedControl).
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import ResultsViewer, {
  BOXES_ONLY_LABEL,
  INSTANCE_MASK_ALPHA,
  INSTANCE_PALETTE,
  MASKS_AND_BOXES_LABEL,
  MASKS_UNAVAILABLE_TEXT,
  SERVER_OVERLAY_LABEL,
  buildInstanceMaskLayer,
  instanceColor,
  truncatedInstancesNote,
  type Capture,
  type Detection,
} from './ResultsViewer';

// Source 4x4 (the shared RLE vectors' frame), displayed 400x400.
const NATURAL_W = 4;
const NATURAL_H = 4;
const CLIENT_W = 400;
const CLIENT_H = 400;
const BORDER_RLE = '0 5 2 2 2 5'; // vector `border_touching`
const CENTRE_RLE = '5 2 2 2 5'; // the 2x2 centre of a 4x4 frame
const ALPHA = Math.round(INSTANCE_MASK_ALPHA * 255);

const originalDescriptors: Record<string, PropertyDescriptor | undefined> = {};

function defineImgDimension(prop: string, value: number) {
  originalDescriptors[prop] = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, prop);
  Object.defineProperty(HTMLImageElement.prototype, prop, {
    configurable: true,
    get() {
      return value;
    },
  });
}

interface RecordingContext {
  putImageData: ReturnType<typeof vi.fn>;
  clearRect: ReturnType<typeof vi.fn>;
}

let context: RecordingContext;
let originalGetContext: typeof HTMLCanvasElement.prototype.getContext;

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

beforeEach(() => {
  context = { putImageData: vi.fn(), clearRect: vi.fn() };
  originalGetContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = vi.fn(
    () => context as unknown as CanvasRenderingContext2D
  ) as unknown as typeof HTMLCanvasElement.prototype.getContext;
});

afterEach(() => {
  HTMLCanvasElement.prototype.getContext = originalGetContext;
});

function loadSourceImage(captureId: string) {
  const img = screen.getByAltText(`Capture ${captureId}`) as HTMLImageElement;
  fireEvent.load(img);
  return img;
}

/** The page's light entries: boxes, mask_area, mask_truncated, no RLE. */
const PAGE_ENTRIES: Detection[] = [
  { class_index: '0', class_label: 'frame', bounding_box: [0, 0, 4, 4], confidence: 0.6, mask_area: 12 },
  { class_index: '1', class_label: 'core', bounding_box: [1, 1, 3, 3], confidence: 0.9, mask_area: 4 },
];
/** The detail's entries: the same instances with their RLE. */
const DETAIL_ENTRIES: Detection[] = [
  { ...PAGE_ENTRIES[0], mask_rle: BORDER_RLE },
  { ...PAGE_ENTRIES[1], mask_rle: CENTRE_RLE },
];

function makeInstanceCapture(overrides: Partial<Capture> = {}): Capture {
  const detections = overrides.detections ?? PAGE_ENTRIES;
  return {
    capture_id: 'cap-inst',
    inference_result_type: 'Detection',
    detection_count: detections.length,
    source_url: 'https://example.com/source.jpg',
    overlay_url: 'https://example.com/overlay.jpg',
    mask_url: 'https://example.com/mask.png',
    has_masks: true,
    detections_truncated: false,
    jsonl_key: 'dev/thing/cap-inst.jsonl',
    ...overrides,
    detections,
  };
}

function segmentedControl() {
  const control = createWrapper(document.body).findSegmentedControl();
  expect(control).not.toBeNull();
  return control!;
}

function segmentTexts() {
  return segmentedControl()
    .findSegments()
    .map((segment) => segment.getElement().textContent);
}

function pixel(data: Uint8ClampedArray, x: number, y: number): number[] {
  const offset = (y * NATURAL_W + x) * 4;
  return Array.from(data.slice(offset, offset + 4));
}

describe('buildInstanceMaskLayer', () => {
  it('composites every decodable mask, most confident on top, in each instance colour at 40 % alpha', () => {
    const layer = buildInstanceMaskLayer('cap', DETAIL_ENTRIES, NATURAL_W, NATURAL_H);
    expect(layer.captureId).toBe('cap');
    expect(layer.painted).toBe(2);
    expect(layer.skipped).toBe(0);
    // Corner: the border instance (#0) only.
    expect(pixel(layer.data, 0, 0)).toEqual([...instanceColor(0), ALPHA]);
    // Centre: the core instance (#1, more confident) lies on top of nothing else.
    expect(pixel(layer.data, 1, 1)).toEqual([...instanceColor(1), ALPHA]);
    expect(pixel(layer.data, 2, 2)).toEqual([...instanceColor(1), ALPHA]);
    // Nothing elsewhere? Every 4x4 pixel is covered here; check the alpha count.
    let painted = 0;
    for (let i = 3; i < layer.data.length; i += 4) {
      if (layer.data[i]) painted += 1;
    }
    expect(painted).toBe(16);
  });

  it('puts the more confident instance on top where masks overlap', () => {
    const overlapping: Detection[] = [
      { ...DETAIL_ENTRIES[0], confidence: 0.95 }, // border, now the most confident
      { class_index: '1', class_label: 'full', bounding_box: [0, 0, 4, 4], confidence: 0.2, mask_rle: '0 16' },
    ];
    const layer = buildInstanceMaskLayer('cap', overlapping, NATURAL_W, NATURAL_H);
    expect(layer.painted).toBe(2);
    expect(pixel(layer.data, 0, 0)).toEqual([...instanceColor(0), ALPHA]); // border wins the corner
    expect(pixel(layer.data, 1, 1)).toEqual([...instanceColor(1), ALPHA]); // only `full` covers the centre
  });

  it('skips an RLE that does not decode at the source size and entries without a mask', () => {
    const entries: Detection[] = [
      { ...DETAIL_ENTRIES[0], mask_rle: '0 5 2 2 2 5 7' }, // sums to 23, not 16
      { ...DETAIL_ENTRIES[1] },
      { class_index: '2', class_label: 'shed', bounding_box: [2, 2, 4, 4], confidence: 0.5, mask_area: 3, mask_truncated: true },
    ];
    const layer = buildInstanceMaskLayer('cap', entries, NATURAL_W, NATURAL_H);
    expect(layer.painted).toBe(1);
    expect(layer.skipped).toBe(1);
    expect(pixel(layer.data, 0, 0)).toEqual([0, 0, 0, 0]);
    expect(pixel(layer.data, 1, 1)).toEqual([...instanceColor(1), ALPHA]);
  });

  it('is empty for a zero-sized frame', () => {
    const layer = buildInstanceMaskLayer('cap', DETAIL_ENTRIES, 0, 0);
    expect(layer.painted).toBe(0);
    expect(layer.data.length).toBe(0);
  });

  it('wraps the palette per instance index', () => {
    expect(instanceColor(0)).toEqual(INSTANCE_PALETTE[0]);
    expect(instanceColor(INSTANCE_PALETTE.length)).toEqual(INSTANCE_PALETTE[0]);
    expect(instanceColor(-1)).toEqual(INSTANCE_PALETTE[0]);
  });
});

describe('truncatedInstancesNote', () => {
  it('lists #index label for the entries whose mask the device omitted', () => {
    const entries: Detection[] = [
      PAGE_ENTRIES[0],
      { ...PAGE_ENTRIES[1], mask_truncated: true },
      { class_index: '7', class_label: '', bounding_box: [0, 0, 1, 1], confidence: 0.3, mask_truncated: true },
    ];
    expect(truncatedInstancesNote(entries)).toBe('Mask omitted on device (payload limit): #1 core, #2 7');
  });

  it('still explains the marker when no entry carries mask_truncated', () => {
    expect(truncatedInstancesNote(PAGE_ENTRIES)).toBe(
      'Some instance masks were omitted on device (payload limit).'
    );
  });
});

describe('ResultsViewer instance presentation', () => {
  it("'loading': boxes from the page at once, a spinner beside the toggle, no mask canvas", () => {
    const capture = makeInstanceCapture();
    const { container } = render(
      <ResultsViewer capture={capture} instanceDetections={null} instanceDetailState="loading" />
    );
    loadSourceImage(capture.capture_id);

    expect(container.querySelectorAll('svg rect').length).toBe(2);
    expect(createWrapper(container).findSpinner()).not.toBeNull();
    expect(screen.queryByTestId('instance-mask-canvas')).not.toBeInTheDocument();
    expect(screen.queryByText(MASKS_UNAVAILABLE_TEXT)).not.toBeInTheDocument();
    expect(segmentTexts()).toEqual([MASKS_AND_BOXES_LABEL, BOXES_ONLY_LABEL, SERVER_OVERLAY_LABEL]);
    expect(segmentedControl().findSelectedSegment()?.getElement().textContent).toBe(
      MASKS_AND_BOXES_LABEL
    );
  });

  it("'ready': paints the detail's masks on the canvas under the boxes, once", () => {
    const capture = makeInstanceCapture();
    const { container } = render(
      <ResultsViewer
        capture={capture}
        instanceDetections={DETAIL_ENTRIES}
        instanceDetailState="ready"
      />
    );
    loadSourceImage(capture.capture_id);

    const canvas = screen.getByTestId('instance-mask-canvas') as HTMLCanvasElement;
    expect(canvas.width).toBe(NATURAL_W);
    expect(canvas.height).toBe(NATURAL_H);
    expect(canvas.style.width).toBe(`${CLIENT_W}px`);
    expect(canvas.style.height).toBe(`${CLIENT_H}px`);
    // Canvas under the SVG box layer inside the positioned container.
    const positioned = canvas.parentElement!;
    const children = Array.from(positioned.children);
    expect(children.indexOf(canvas)).toBeLessThan(children.indexOf(positioned.querySelector('svg')!));
    expect(container.querySelectorAll('svg rect').length).toBe(2);

    expect(context.putImageData).toHaveBeenCalledTimes(1);
    const [image] = context.putImageData.mock.calls[0] as [ImageData, number, number];
    expect(image.width).toBe(NATURAL_W);
    expect(pixel(image.data, 0, 0)).toEqual([...instanceColor(0), ALPHA]);
    expect(pixel(image.data, 1, 1)).toEqual([...instanceColor(1), ALPHA]);
    expect(createWrapper(container).findSpinner()).toBeNull();
    expect(screen.queryByText(MASKS_UNAVAILABLE_TEXT)).not.toBeInTheDocument();
  });

  it('draws each box in its instance colour', () => {
    const capture = makeInstanceCapture();
    const { container } = render(
      <ResultsViewer capture={capture} instanceDetections={DETAIL_ENTRIES} instanceDetailState="ready" />
    );
    loadSourceImage(capture.capture_id);
    const strokes = Array.from(container.querySelectorAll('svg rect')).map((rect) =>
      rect.getAttribute('stroke')
    );
    const [r0, g0, b0] = instanceColor(0);
    const [r1, g1, b1] = instanceColor(1);
    expect(strokes).toEqual([`rgb(${r0}, ${g0}, ${b0})`, `rgb(${r1}, ${g1}, ${b1})`]);
    const texts = Array.from(container.querySelectorAll('svg text')).map((t) => t.textContent);
    expect(texts).toEqual(['frame 60%', 'core 90%']);
  });

  it("'unavailable': the warning and the boxes, no canvas", () => {
    const capture = makeInstanceCapture();
    const { container } = render(
      <ResultsViewer capture={capture} instanceDetections={null} instanceDetailState="unavailable" />
    );
    loadSourceImage(capture.capture_id);

    expect(screen.getByText(MASKS_UNAVAILABLE_TEXT)).toBeInTheDocument();
    expect(container.querySelectorAll('svg rect').length).toBe(2);
    expect(screen.queryByTestId('instance-mask-canvas')).not.toBeInTheDocument();
    expect(createWrapper(container).findSpinner()).toBeNull();
  });

  it('shows the truncated-instances note with the detail and without it', () => {
    const detections: Detection[] = [
      PAGE_ENTRIES[0],
      { ...PAGE_ENTRIES[1], mask_truncated: true },
    ];
    const capture = makeInstanceCapture({ detections, detections_truncated: true });

    const withoutDetail = render(
      <ResultsViewer capture={capture} instanceDetections={null} instanceDetailState="loading" />
    );
    expect(
      screen.getByText('Mask omitted on device (payload limit): #1 core')
    ).toBeInTheDocument();
    withoutDetail.unmount();

    render(
      <ResultsViewer
        capture={capture}
        instanceDetections={[{ ...DETAIL_ENTRIES[0] }, { ...PAGE_ENTRIES[1], mask_truncated: true }]}
        instanceDetailState="ready"
      />
    );
    loadSourceImage(capture.capture_id);
    expect(
      screen.getByText('Mask omitted on device (payload limit): #1 core')
    ).toBeInTheDocument();
    expect(context.putImageData).toHaveBeenCalledTimes(1); // the one remaining mask
  });

  it('keeps the instance presentation and the note for an all-truncated capture', () => {
    const detections: Detection[] = PAGE_ENTRIES.map((det) => ({ ...det, mask_truncated: true }));
    const capture = makeInstanceCapture({ detections, detections_truncated: true });
    render(
      <ResultsViewer capture={capture} instanceDetections={detections} instanceDetailState="ready" />
    );
    loadSourceImage(capture.capture_id);

    expect(
      screen.getByText('Mask omitted on device (payload limit): #0 frame, #1 core')
    ).toBeInTheDocument();
    expect(segmentTexts()).toEqual([MASKS_AND_BOXES_LABEL, BOXES_ONLY_LABEL, SERVER_OVERLAY_LABEL]);
    // Nothing to paint: no putImageData, boxes still drawn.
    expect(context.putImageData).not.toHaveBeenCalled();
  });

  it('does not show the note when detections_truncated is unset', () => {
    const capture = makeInstanceCapture({ detections_truncated: false });
    render(<ResultsViewer capture={capture} instanceDetections={DETAIL_ENTRIES} instanceDetailState="ready" />);
    expect(screen.queryByText(/omitted on device/)).not.toBeInTheDocument();
  });

  it('toggles masks + boxes -> boxes only -> server overlay', () => {
    const capture = makeInstanceCapture();
    const { container } = render(
      <ResultsViewer capture={capture} instanceDetections={DETAIL_ENTRIES} instanceDetailState="ready" />
    );
    loadSourceImage(capture.capture_id);
    expect(screen.getByTestId('instance-mask-canvas')).toBeInTheDocument();

    segmentedControl().findSegmentById('boxes')!.click();
    expect(segmentedControl().findSelectedSegment()?.getElement().textContent).toBe(BOXES_ONLY_LABEL);
    expect(screen.queryByTestId('instance-mask-canvas')).not.toBeInTheDocument();
    expect(container.querySelectorAll('svg rect').length).toBe(2);
    expect(
      screen.queryByAltText(`Server-rendered overlay for ${capture.capture_id}`)
    ).not.toBeInTheDocument();

    segmentedControl().findSegmentById('overlay')!.click();
    expect(segmentedControl().findSelectedSegment()?.getElement().textContent).toBe(SERVER_OVERLAY_LABEL);
    const overlayImg = screen.getByAltText(
      `Server-rendered overlay for ${capture.capture_id}`
    ) as HTMLImageElement;
    expect(overlayImg.src).toBe(capture.overlay_url);
    expect(container.querySelectorAll('svg rect').length).toBe(0);
    expect(screen.queryByTestId('instance-mask-canvas')).not.toBeInTheDocument();

    segmentedControl().findSegmentById('masks')!.click();
    loadSourceImage(capture.capture_id);
    expect(screen.getByTestId('instance-mask-canvas')).toBeInTheDocument();
  });

  it('disables the server overlay segment when overlay_url is null', () => {
    const capture = makeInstanceCapture({ overlay_url: null });
    render(<ResultsViewer capture={capture} instanceDetections={DETAIL_ENTRIES} instanceDetailState="ready" />);
    loadSourceImage(capture.capture_id);

    const overlay = segmentedControl().findSegmentById('overlay')!.getElement();
    // Cloudscape marks a segment with a disabledReason aria-disabled (focusable for the tooltip).
    expect(overlay.hasAttribute('disabled') || overlay.getAttribute('aria-disabled') === 'true').toBe(true);
    const boxes = segmentedControl().findSegmentById('boxes')!.getElement();
    expect(boxes.hasAttribute('disabled') || boxes.getAttribute('aria-disabled') === 'true').toBe(false);
    // Clicking the disabled segment changes nothing.
    segmentedControl().findSegmentById('overlay')!.click();
    expect(segmentedControl().findSelectedSegment()?.getElement().textContent).toBe(MASKS_AND_BOXES_LABEL);
    expect(screen.getByTestId('instance-mask-canvas')).toBeInTheDocument();
  });

  it('shows the source-image warning when source_url is null', () => {
    const capture = makeInstanceCapture({ source_url: null });
    render(<ResultsViewer capture={capture} instanceDetections={DETAIL_ENTRIES} instanceDetailState="ready" />);
    expect(screen.getByText('Source image unavailable')).toBeInTheDocument();
    expect(createWrapper(document.body).findSegmentedControl()).toBeNull();
  });

  it("renders a 'Detection' capture without has_masks through the unchanged detection presentation", () => {
    const capture = makeInstanceCapture({ has_masks: false });
    render(<ResultsViewer capture={capture} />);
    expect(screen.getByRole('checkbox', { name: /show server overlay/i })).toBeInTheDocument();
    expect(createWrapper(document.body).findSegmentedControl()).toBeNull();
  });

  it('renders an anomaly capture through the unchanged anomaly presentation (semantic Seg = anomaly)', () => {
    const capture: Capture = {
      capture_id: 'cap-sem',
      inference_result_type: 'Anomaly',
      detection_count: 0,
      detections: [],
      source_url: 'https://example.com/source.jpg',
      overlay_url: null,
      mask_url: 'https://example.com/mask.png',
    };
    render(<ResultsViewer capture={capture} instanceDetections={null} instanceDetailState="ready" />);
    expect(screen.getByAltText(`Anomaly mask for ${capture.capture_id}`)).toBeInTheDocument();
    expect(createWrapper(document.body).findSegmentedControl()).toBeNull();
  });
});
