/**
 * Port-type compatibility rules: exact match plus declared coercions.
 *
 * TypeScript mirror of `workflow_core.catalog.compatibility` — keep the
 * two in sync. A connection joins an output port (source) to an input
 * port (target). Compatibility is exact type match, plus an explicit
 * coercion table (Requirements 1.4, 4.2). The only declared coercion:
 * `InferenceMeta` flows over the same GStreamer buffer stream as
 * `VideoFrames` with attached metadata, so an `InferenceMeta` output may
 * feed a `VideoFrames` input (e.g. `capture` accepts both).
 */

import {
  PORT_TYPE_INFERENCE_META,
  PORT_TYPE_VIDEO_FRAMES,
  PORT_TYPES,
} from './types';

/**
 * Declared coercions: (source output type) -> set of additionally
 * acceptable target input types.
 */
export const PORT_TYPE_COERCIONS: Readonly<Record<string, ReadonlySet<string>>> = {
  [PORT_TYPE_INFERENCE_META]: new Set([PORT_TYPE_VIDEO_FRAMES]),
};

/**
 * True when an output of `sourceType` may connect to an input of
 * `targetType`: exact match or a declared coercion.
 */
export function arePortsCompatible(sourceType: string, targetType: string): boolean {
  if (sourceType === targetType) {
    return true;
  }
  return PORT_TYPE_COERCIONS[sourceType]?.has(targetType) ?? false;
}

/**
 * A human-readable rejection reason, or null when compatible.
 *
 * Used by the Workflow_Builder to explain rejected connections
 * (Requirement 1.4).
 */
export function incompatibilityReason(sourceType: string, targetType: string): string | null {
  if (!(PORT_TYPES as readonly string[]).includes(sourceType)) {
    return `Unknown source port type '${sourceType}'`;
  }
  if (!(PORT_TYPES as readonly string[]).includes(targetType)) {
    return `Unknown target port type '${targetType}'`;
  }
  if (arePortsCompatible(sourceType, targetType)) {
    return null;
  }
  return `Cannot connect ${sourceType} output to ${targetType} input`;
}
