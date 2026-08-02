/**
 * Unit tests for the portScan.ts helper boundaries
 * (port-guidance-and-pad-prepopulation task 7.4, Requirements 6.1, 6.9).
 *
 * Concrete cases for `isUntouchedDefaults` and `removalBlockReason`,
 * plus boundary examples for `applySuggestions` complementing the
 * property tests (Properties 10 and 11).
 */
import { describe, expect, it } from 'vitest';
import type { PortForm } from './declaration';
import {
  applySuggestions,
  isUntouchedDefaults,
  removalBlockReason,
  type PortSuggestion,
} from './portScan';
import {
  untouchedDefaultInputs,
  untouchedDefaultOutputs,
} from './portScanArbitraries';

const suggestion = (
  overrides: Partial<PortSuggestion> = {}
): PortSuggestion => ({
  name: 'sink',
  direction: 'input',
  portType: 'VideoFrames',
  confident: true,
  caps: 'video/x-raw, format=(string)RGB',
  capsTruncated: false,
  reason: "the pad's caps begin with video/x-raw",
  ...overrides,
});

describe('isUntouchedDefaults (6.1)', () => {
  it('accepts exactly the wizard-supplied defaults', () => {
    expect(
      isUntouchedDefaults(untouchedDefaultInputs(), untouchedDefaultOutputs())
    ).toBe(true);
  });

  it('rejects a renamed input', () => {
    expect(
      isUntouchedDefaults(
        [{ name: 'video_in', portType: 'VideoFrames' }],
        untouchedDefaultOutputs()
      )
    ).toBe(false);
  });

  it('rejects a renamed output', () => {
    expect(
      isUntouchedDefaults(untouchedDefaultInputs(), [
        { name: 'result', portType: 'VideoFrames' },
      ])
    ).toBe(false);
  });

  it('rejects a retyped input', () => {
    expect(
      isUntouchedDefaults(
        [{ name: 'in', portType: 'InferenceMeta' }],
        untouchedDefaultOutputs()
      )
    ).toBe(false);
  });

  it('rejects a retyped output', () => {
    expect(
      isUntouchedDefaults(untouchedDefaultInputs(), [
        { name: 'out', portType: 'EventSignal' },
      ])
    ).toBe(false);
  });

  it('rejects an added row on either side', () => {
    expect(
      isUntouchedDefaults(
        [...untouchedDefaultInputs(), { name: 'in2', portType: 'VideoFrames' }],
        untouchedDefaultOutputs()
      )
    ).toBe(false);
    expect(
      isUntouchedDefaults(untouchedDefaultInputs(), [
        ...untouchedDefaultOutputs(),
        { name: 'out2', portType: 'VideoFrames' },
      ])
    ).toBe(false);
  });

  it(
    "treats a removal landing on another category's defaults as untouched " +
      '(generalized detection, workflow-designer-bugfixes Bug 2)',
    () => {
      // Untouched_Defaults is generalized to the default arrangement of
      // any palette category (isDefaultPortArrangement): no inputs + one
      // VideoFrames "out" equals the input category's defaults, and one
      // VideoFrames "in" + no outputs equals the output category's —
      // both count as untouched so Port_Scan's replace-over-defaults
      // semantics stay coherent with the category-driven seeding.
      expect(isUntouchedDefaults([], untouchedDefaultOutputs())).toBe(true);
      expect(isUntouchedDefaults(untouchedDefaultInputs(), [])).toBe(true);

      // A removal landing on no category's defaults stays user-edited.
      expect(isUntouchedDefaults([], [])).toBe(false);
    }
  );
});

describe('removalBlockReason (6.9)', () => {
  const declaration: Record<string, unknown> = {
    inputs: [{ name: 'in', portType: 'VideoFrames' }],
    outputs: [{ name: 'out', portType: 'VideoFrames' }],
  };

  it('blocks removing a port the registered declaration depends on, with a reason', () => {
    const reason = removalBlockReason('inputs', 'in', declaration);
    expect(reason).not.toBeNull();
    expect(reason).toContain('"in"');
    expect(reason).toContain('input');
  });

  it('names the output side in the reason for output ports', () => {
    const reason = removalBlockReason('outputs', 'out', declaration);
    expect(reason).not.toBeNull();
    expect(reason).toContain('"out"');
    expect(reason).toContain('output');
  });

  it('allows removing a port the declaration does not mention', () => {
    expect(removalBlockReason('inputs', 'extra', declaration)).toBeNull();
  });

  it('matches on trimmed names', () => {
    expect(removalBlockReason('inputs', '  in ', declaration)).not.toBeNull();
  });

  it('checks only the same side of the declaration', () => {
    expect(removalBlockReason('outputs', 'in', declaration)).toBeNull();
    expect(removalBlockReason('inputs', 'out', declaration)).toBeNull();
  });

  it('never blocks with a null declaration (initial registration)', () => {
    expect(removalBlockReason('inputs', 'in', null)).toBeNull();
    expect(removalBlockReason('outputs', 'out', null)).toBeNull();
  });

  it('never blocks a whitespace-only port name', () => {
    expect(removalBlockReason('inputs', '   ', declaration)).toBeNull();
  });

  it('never blocks when the declaration side is missing or not a list', () => {
    expect(removalBlockReason('inputs', 'in', {})).toBeNull();
    expect(
      removalBlockReason('inputs', 'in', { inputs: 'not-a-list' })
    ).toBeNull();
  });
});

describe('applySuggestions boundaries (6.1, 6.2, 6.10)', () => {
  it('replaces the untouched defaults with the suggestions partitioned by direction', () => {
    const result = applySuggestions(
      untouchedDefaultInputs(),
      untouchedDefaultOutputs(),
      [
        suggestion({ name: 'sink', direction: 'input' }),
        suggestion({ name: 'src', direction: 'output' }),
      ],
      true
    );
    expect(result.inputs).toEqual([{ name: 'sink', portType: 'VideoFrames' }]);
    expect(result.outputs).toEqual([{ name: 'src', portType: 'VideoFrames' }]);
    expect(result.applied).toEqual(['sink', 'src']);
    expect(result.alreadyDeclared).toEqual([]);
    expect(result.unconfirmed).toEqual([]);
  });

  it('leaves the lists unchanged for an empty suggestion list, untouched or not', () => {
    const inputs = untouchedDefaultInputs();
    const outputs = untouchedDefaultOutputs();
    for (const untouched of [true, false]) {
      const result = applySuggestions(inputs, outputs, [], untouched);
      expect(result.inputs).toEqual(inputs);
      expect(result.outputs).toEqual(outputs);
      expect(result.applied).toEqual([]);
      expect(result.alreadyDeclared).toEqual([]);
      expect(result.unconfirmed).toEqual([]);
    }
  });

  it('merges additively when edited: exact trimmed-name matches stay declared, the rest append', () => {
    const inputs: PortForm[] = [{ name: ' sink ', portType: 'InferenceMeta' }];
    const outputs: PortForm[] = [{ name: 'result', portType: 'EventSignal' }];
    const result = applySuggestions(
      inputs,
      outputs,
      [
        suggestion({ name: 'sink', direction: 'input' }),
        suggestion({ name: 'src', direction: 'output' }),
      ],
      false
    );
    // Existing ports unchanged and in place, including the collided one.
    expect(result.inputs[0]).toEqual({ name: ' sink ', portType: 'InferenceMeta' });
    expect(result.outputs[0]).toEqual({ name: 'result', portType: 'EventSignal' });
    expect(result.alreadyDeclared).toEqual(['sink']);
    expect(result.applied).toEqual(['src']);
    expect(result.outputs).toHaveLength(2);
    expect(result.outputs[1]).toEqual({ name: 'src', portType: 'VideoFrames' });
  });

  it('treats name collisions case-sensitively in the merge', () => {
    const result = applySuggestions(
      [{ name: 'Sink', portType: 'VideoFrames' }],
      [],
      [suggestion({ name: 'sink', direction: 'input' })],
      false
    );
    expect(result.alreadyDeclared).toEqual([]);
    expect(result.applied).toEqual(['sink']);
    expect(result.inputs).toHaveLength(2);
  });

  it('reports non-confident applied names as unconfirmed (6.5)', () => {
    const result = applySuggestions(
      untouchedDefaultInputs(),
      untouchedDefaultOutputs(),
      [
        suggestion({ name: 'sink', direction: 'input' }),
        suggestion({
          name: 'meta',
          direction: 'output',
          confident: false,
          caps: 'ANY',
          reason:
            'InferenceMeta and EventSignal are DDA semantic concepts ' +
            'GStreamer caps cannot express; confirm the Port_Type',
        }),
      ],
      true
    );
    expect(result.applied).toEqual(['sink', 'meta']);
    expect(result.unconfirmed).toEqual(['meta']);
  });
});
