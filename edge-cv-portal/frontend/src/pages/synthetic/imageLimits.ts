/**
 * Source image size limits of the generation models
 * (fix-synthetic-image-size-limits). Mirrors the `oversize_handling`
 * parameter and the `image_limit_violation` / `describe_image_limits`
 * helpers of `synthetic_core.py`, with the same wording.
 */
import type { RadioGroupProps } from '@cloudscape-design/components';
import type { SyntheticImageLimits, SyntheticOversizeHandling } from './types';

export const DEFAULT_OVERSIZE_HANDLING: SyntheticOversizeHandling = 'resize_restore';

/** Choices of the size-limit handling control. */
export const OVERSIZE_HANDLING_ITEMS: RadioGroupProps.RadioButtonDefinition[] = [
  {
    value: 'resize_restore',
    label: 'Resize to fit the model, then restore the original size',
    description:
      'Recommended. The image (and its mask) is scaled to fit the limits before generation and the result is scaled back to the original dimensions.',
  },
  {
    value: 'fail',
    label: 'Do not resize (variations from these images fail)',
    description:
      'Images outside the limits are not sent; each of their variations is recorded as failed with the reason. Other images still generate.',
  },
];

function formatCount(n: number): string {
  return n.toLocaleString('en-US');
}

/**
 * Human-readable form of the limits, e.g. "images between 4,096 and
 * 9,437,184 pixels in total, with each side at least 64 px".
 */
export function describeImageLimits(limits: SyntheticImageLimits): string {
  const { min_pixels, max_pixels, min_side, max_side } = limits;

  let pixelPart: string | null = null;
  if (min_pixels != null && max_pixels != null) {
    pixelPart = `between ${formatCount(min_pixels)} and ${formatCount(max_pixels)} pixels in total`;
  } else if (max_pixels != null) {
    pixelPart = `up to ${formatCount(max_pixels)} pixels in total`;
  } else if (min_pixels != null) {
    pixelPart = `at least ${formatCount(min_pixels)} pixels in total`;
  }

  let sidePart: string | null = null;
  if (min_side != null && max_side != null) {
    sidePart = `each side between ${formatCount(min_side)} and ${formatCount(max_side)} px`;
  } else if (min_side != null) {
    sidePart = `each side at least ${formatCount(min_side)} px`;
  } else if (max_side != null) {
    sidePart = `the longest side at most ${formatCount(max_side)} px`;
  }

  if (pixelPart && sidePart) return `images ${pixelPart}, with ${sidePart}`;
  if (pixelPart) return `images ${pixelPart}`;
  if (sidePart) return `images with ${sidePart}`;
  return 'images of any size';
}

/**
 * Why a `width` x `height` image violates `limits`, or null when it is
 * within them. Bounds are checked in the order max_pixels, max_side,
 * min_pixels, min_side; the first violated one is reported.
 */
export function imageLimitViolation(
  width: number,
  height: number,
  limits: SyntheticImageLimits
): string | null {
  const pixels = width * height;
  const longest = Math.max(width, height);
  const shortest = Math.min(width, height);
  const { min_pixels, max_pixels, min_side, max_side } = limits;

  if (max_pixels != null && pixels > max_pixels) {
    return `${formatCount(pixels)} pixels exceeds the maximum of ${formatCount(max_pixels)} pixels`;
  }
  if (max_side != null && longest > max_side) {
    return `the longest side (${longest} px) exceeds the maximum of ${formatCount(max_side)} px`;
  }
  if (min_pixels != null && pixels < min_pixels) {
    return `${formatCount(pixels)} pixels is below the minimum of ${formatCount(min_pixels)} pixels`;
  }
  if (min_side != null && shortest < min_side) {
    return `the shortest side (${shortest} px) is below the minimum of ${min_side} px`;
  }
  return null;
}
