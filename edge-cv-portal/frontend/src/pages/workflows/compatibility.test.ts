import { describe, it, expect } from 'vitest';
import { arePortsCompatible, incompatibilityReason, PORT_TYPE_COERCIONS } from './compatibility';
import {
  PORT_TYPE_EVENT_SIGNAL,
  PORT_TYPE_INFERENCE_META,
  PORT_TYPE_VIDEO_FRAMES,
  PORT_TYPES,
} from './types';

/**
 * Unit tests for the TypeScript mirror of
 * `workflow_core.catalog.compatibility` (Requirement 1.4).
 */

describe('arePortsCompatible', () => {
  it('accepts exact type matches for every port type', () => {
    for (const portType of PORT_TYPES) {
      expect(arePortsCompatible(portType, portType)).toBe(true);
    }
  });

  it('accepts the declared InferenceMeta -> VideoFrames coercion', () => {
    expect(arePortsCompatible(PORT_TYPE_INFERENCE_META, PORT_TYPE_VIDEO_FRAMES)).toBe(true);
  });

  it('rejects the reverse coercion VideoFrames -> InferenceMeta', () => {
    expect(arePortsCompatible(PORT_TYPE_VIDEO_FRAMES, PORT_TYPE_INFERENCE_META)).toBe(false);
  });

  it('rejects all other cross-type pairs', () => {
    for (const source of PORT_TYPES) {
      for (const target of PORT_TYPES) {
        const coerced = PORT_TYPE_COERCIONS[source]?.has(target) ?? false;
        expect(arePortsCompatible(source, target)).toBe(source === target || coerced);
      }
    }
  });

  it('rejects unknown port types', () => {
    expect(arePortsCompatible('Bogus', PORT_TYPE_VIDEO_FRAMES)).toBe(false);
    expect(arePortsCompatible(PORT_TYPE_VIDEO_FRAMES, 'Bogus')).toBe(false);
  });
});

describe('incompatibilityReason', () => {
  it('returns null for compatible pairs', () => {
    expect(incompatibilityReason(PORT_TYPE_VIDEO_FRAMES, PORT_TYPE_VIDEO_FRAMES)).toBeNull();
    expect(incompatibilityReason(PORT_TYPE_INFERENCE_META, PORT_TYPE_VIDEO_FRAMES)).toBeNull();
  });

  it('explains incompatible pairs with source and target types', () => {
    expect(incompatibilityReason(PORT_TYPE_VIDEO_FRAMES, PORT_TYPE_EVENT_SIGNAL)).toBe(
      'Cannot connect VideoFrames output to EventSignal input'
    );
  });

  it('identifies unknown source and target port types', () => {
    expect(incompatibilityReason('Bogus', PORT_TYPE_VIDEO_FRAMES)).toBe(
      "Unknown source port type 'Bogus'"
    );
    expect(incompatibilityReason(PORT_TYPE_VIDEO_FRAMES, 'Bogus')).toBe(
      "Unknown target port type 'Bogus'"
    );
  });
});
