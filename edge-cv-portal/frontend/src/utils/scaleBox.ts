/**
 * Bounding-box coordinate scaling for the Results_Viewer.
 *
 * Detection bounding boxes arrive from the device in *source-image pixel
 * coordinates* (the dimensions of the capture the model actually ran on).
 * When the Portal renders the capture, the `<img>` is usually displayed at a
 * different size than the source. `scaleBox` converts a source-pixel box into
 * coordinates for the displayed image so an absolutely-positioned overlay
 * (`<svg>` `<rect>` + `<text>`) lines up with what the user sees.
 *
 * This module is intentionally framework-free (no React/DOM imports) so it is
 * a pure function that is trivially unit- and property-testable.
 */

/** A bounding box in source-image pixel coordinates: `[x_min, y_min, x_max, y_max]`. */
export type Box = [number, number, number, number];

/** Image dimensions (width/height) in pixels. */
export interface Dimensions {
  w: number;
  h: number;
}

/** A rendered box: top-left corner `(x, y)` plus width/height, in display pixels. */
export interface ScaledBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * Scale a bounding box from source-image pixel coordinates to displayed image
 * dimensions.
 *
 * The box is scaled independently on each axis by the width ratio
 * (`disp.w / src.w`) and height ratio (`disp.h / src.h`):
 *
 * ```
 * x = x_min * rw
 * y = y_min * rh
 * w = (x_max - x_min) * rw
 * h = (y_max - y_min) * rh
 * ```
 *
 * When the display dimensions equal the source dimensions the box is returned
 * unchanged (identity).
 *
 * Guard: the source dimensions come from the image's reported
 * `naturalWidth`/`naturalHeight`, which are `0` until the image has finished
 * loading (and can be `undefined`/`NaN` in edge cases). If either source
 * dimension is not a finite number greater than zero, we cannot compute a
 * meaningful ratio, so we return a zeroed no-op box `{ x: 0, y: 0, w: 0, h: 0 }`.
 * This ensures boxes are only drawn once the image reports valid natural
 * dimensions rather than being placed with a divide-by-zero / `NaN` position.
 *
 * @param box  Source-pixel bounding box `[x_min, y_min, x_max, y_max]`.
 * @param src  Source image dimensions (typically `naturalWidth`/`naturalHeight`).
 * @param disp Displayed image dimensions (typically `clientWidth`/`clientHeight`).
 * @returns The scaled box in display pixels, or a zeroed box if `src` is invalid.
 */
export function scaleBox(box: Box, src: Dimensions, disp: Dimensions): ScaledBox {
  // Guard against zero/undefined/NaN natural dimensions: without valid source
  // dimensions the scale ratio is undefined (divide-by-zero / NaN), so we
  // no-op until the image reports real dimensions.
  if (
    !Number.isFinite(src.w) ||
    !Number.isFinite(src.h) ||
    src.w <= 0 ||
    src.h <= 0
  ) {
    return { x: 0, y: 0, w: 0, h: 0 };
  }

  const [xMin, yMin, xMax, yMax] = box;
  const rw = disp.w / src.w;
  const rh = disp.h / src.h;

  return {
    x: xMin * rw,
    y: yMin * rh,
    w: (xMax - xMin) * rw,
    h: (yMax - yMin) * rh,
  };
}
