/**
 * Unit tests for the deploy-time Camera_Binding pure helpers
 * (camera-registry-sync task 12.2 — Requirements 8.1, 8.5, 8.8, 8.9,
 * 9.3): workflow component recognition and version parsing, the initial
 * matrix state (hint pre-selection as suggested, never-synced targets
 * forced to manual override, user choices retained on refresh), the
 * `camera_bindings` payload shapes, unbound-cell detection, expected
 * warning ids matching validate_camera_bindings, and the structured
 * submission-rejection parser.
 */

import { describe, expect, it } from 'vitest';
import type { CameraSourceEntry } from '../workflows/cameraReference';
import {
  BindingContextNode,
  BindingContextTarget,
  BindingSelections,
  CameraBindingContext,
  buildCameraBindings,
  cameraOptionDescription,
  cameraOptionLabel,
  cameraOptionTags,
  describeBindingIssue,
  expectedBindingWarnings,
  getBindingCell,
  initialBindingSelections,
  isWorkflowComponent,
  parseCameraBindingRejection,
  parseWorkflowComponent,
  unboundCells,
  withBindingCell,
} from './cameraBindings';

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const HEALTHY: CameraSourceEntry = {
  camera_source_id: 'cfg-1',
  name: 'Line 1 inspection cam',
  type: 'Camera',
  params: { devicePath: '/dev/video0' },
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const STALE_ABSENT: CameraSourceEntry = {
  camera_source_id: 'cfg-2',
  name: 'Rear dock cam',
  type: 'Camera',
  params: { devicePath: '/dev/video2' },
  sync_status: 'synced',
  stale: true,
  absent: true,
};

const PENDING: CameraSourceEntry = {
  camera_source_id: 'cfg-3',
  name: 'RTSP feed',
  type: 'RTSP',
  params: { url: 'rtsp://10.0.0.5/stream' },
  sync_status: 'pending',
  stale: false,
  absent: false,
};

const NODE_HINTED: BindingContextNode = {
  node_id: 'cam_in_1',
  node_type: 'camera_source',
  binding_hint: { cameraSourceId: 'cfg-1', cameraName: 'Line 1 inspection cam', sourceDeviceId: 'dev-ref' },
};

const NODE_PLAIN: BindingContextNode = {
  node_id: 'cam_in_2',
  node_type: 'camera_source',
};

const SYNCED_TARGET: BindingContextTarget = {
  state: 'synced',
  cameras: [HEALTHY, STALE_ABSENT, PENDING],
  // Hint-matching pre-selection computed by the binding context endpoint (8.5).
  preselected: { cam_in_1: 'cfg-1' },
};

const NEVER_SYNCED_TARGET: BindingContextTarget = {
  state: 'never-synced',
  cameras: [],
  preselected: {},
};

function bindingContext(overrides: Partial<CameraBindingContext> = {}): CameraBindingContext {
  return {
    workflow_id: 'wf-1',
    workflow_version: 3,
    has_binding_points: true,
    binding_required: true,
    camera_input_nodes: [NODE_HINTED, NODE_PLAIN],
    targets: { 'thing-1': SYNCED_TARGET, 'thing-2': NEVER_SYNCED_TARGET },
    ...overrides,
  };
}

// --------------------------------------------------------------------------
// Workflow component recognition and version parsing
// --------------------------------------------------------------------------

describe('parseWorkflowComponent', () => {
  it('recognizes dda.workflow.* components and parses the workflow version from the major', () => {
    expect(isWorkflowComponent('dda.workflow.wf-1')).toBe(true);
    expect(parseWorkflowComponent('dda.workflow.wf-1', '3.0.0')).toEqual({
      workflowId: 'wf-1',
      workflowVersion: 3,
    });
  });

  it('returns null for non-workflow components and the bare prefix', () => {
    expect(parseWorkflowComponent('com.example.HelloWorld', '1.0.0')).toBeNull();
    expect(parseWorkflowComponent('dda.workflow.', '1.0.0')).toBeNull();
    expect(parseWorkflowComponent(null)).toBeNull();
    expect(parseWorkflowComponent(undefined)).toBeNull();
  });

  it('resolves unpinned or unparseable versions to a null workflowVersion', () => {
    for (const version of [undefined, null, '', '0.0.0', 'unknown', 'latest', 'abc.0.0']) {
      expect(parseWorkflowComponent('dda.workflow.wf-1', version)).toEqual({
        workflowId: 'wf-1',
        workflowVersion: null,
      });
    }
  });
});

// --------------------------------------------------------------------------
// Initial matrix state (8.5, 8.8)
// --------------------------------------------------------------------------

describe('initialBindingSelections', () => {
  it('pre-fills hint pre-selections as suggested camera cells awaiting confirmation (Requirement 8.5)', () => {
    const selections = initialBindingSelections(bindingContext());
    expect(getBindingCell(selections, 'thing-1', 'cam_in_1')).toEqual({
      mode: 'camera',
      cameraSourceId: 'cfg-1',
      suggested: true,
    });
    // No pre-selection for the node without a matching hint.
    expect(getBindingCell(selections, 'thing-1', 'cam_in_2')).toEqual({ mode: 'unbound' });
  });

  it('forces never-synced targets into manual override mode (Requirement 8.8)', () => {
    const selections = initialBindingSelections(bindingContext());
    expect(getBindingCell(selections, 'thing-2', 'cam_in_1')).toEqual({
      mode: 'override',
      device: '',
    });
    expect(getBindingCell(selections, 'thing-2', 'cam_in_2')).toEqual({
      mode: 'override',
      device: '',
    });
  });

  it('retains user-made choices on a context refresh but re-derives suggestions', () => {
    const previous: BindingSelections = {
      'thing-1': {
        // A user-confirmed camera choice and a typed override survive.
        cam_in_1: { mode: 'camera', cameraSourceId: 'cfg-2', suggested: false },
        cam_in_2: { mode: 'override', device: '/dev/video7' },
      },
    };
    const selections = initialBindingSelections(bindingContext(), previous);
    expect(getBindingCell(selections, 'thing-1', 'cam_in_1')).toEqual({
      mode: 'camera',
      cameraSourceId: 'cfg-2',
      suggested: false,
    });
    expect(getBindingCell(selections, 'thing-1', 'cam_in_2')).toEqual({
      mode: 'override',
      device: '/dev/video7',
    });
  });

  it('replaces stale suggestions with the fresh context pre-selection', () => {
    const previous: BindingSelections = {
      'thing-1': {
        // An unconfirmed suggestion is re-derived, not retained.
        cam_in_1: { mode: 'camera', cameraSourceId: 'cfg-old', suggested: true },
      },
    };
    const selections = initialBindingSelections(bindingContext(), previous);
    expect(getBindingCell(selections, 'thing-1', 'cam_in_1')).toEqual({
      mode: 'camera',
      cameraSourceId: 'cfg-1',
      suggested: true,
    });
  });

  it('drops previous cells whose device left the context', () => {
    const previous: BindingSelections = {
      'thing-gone': { cam_in_1: { mode: 'override', device: '/dev/video9' } },
    };
    const selections = initialBindingSelections(bindingContext(), previous);
    expect(selections['thing-gone']).toBeUndefined();
  });

  it('produces empty per-device cell maps for a version without Camera_Input_Nodes (Requirement 8.9)', () => {
    const context = bindingContext({
      binding_required: false,
      has_binding_points: false,
      camera_input_nodes: [],
    });
    const selections = initialBindingSelections(context);
    expect(selections).toEqual({ 'thing-1': {}, 'thing-2': {} });
    expect(buildCameraBindings(selections)).toEqual({});
    expect(unboundCells(context, selections)).toEqual([]);
  });
});

describe('withBindingCell', () => {
  it('updates one cell immutably', () => {
    const before = initialBindingSelections(bindingContext());
    const after = withBindingCell(before, 'thing-1', 'cam_in_2', {
      mode: 'camera',
      cameraSourceId: 'cfg-3',
      suggested: false,
    });
    expect(getBindingCell(after, 'thing-1', 'cam_in_2')).toEqual({
      mode: 'camera',
      cameraSourceId: 'cfg-3',
      suggested: false,
    });
    // The original state and untouched cells are unchanged.
    expect(getBindingCell(before, 'thing-1', 'cam_in_2')).toEqual({ mode: 'unbound' });
    expect(getBindingCell(after, 'thing-1', 'cam_in_1')).toEqual(
      getBindingCell(before, 'thing-1', 'cam_in_1')
    );
  });
});

// --------------------------------------------------------------------------
// camera_bindings payload (8.5 suggested counts, unbound omitted)
// --------------------------------------------------------------------------

describe('buildCameraBindings', () => {
  it('counts a suggested pre-selection as a selection (Requirement 8.5)', () => {
    const payload = buildCameraBindings({
      'thing-1': { cam_in_1: { mode: 'camera', cameraSourceId: 'cfg-1', suggested: true } },
    });
    expect(payload).toEqual({
      'thing-1': { cam_in_1: { cameraSourceId: 'cfg-1' } },
    });
  });

  it('emits overrides with their trimmed device path and omits unbound and empty cells', () => {
    const payload = buildCameraBindings({
      'thing-1': {
        cam_in_1: { mode: 'override', device: '  /dev/video3  ' },
        cam_in_2: { mode: 'unbound' },
      },
      'thing-2': {
        cam_in_1: { mode: 'override', device: '   ' },
        cam_in_2: { mode: 'camera', cameraSourceId: '', suggested: false },
      },
    });
    expect(payload).toEqual({
      'thing-1': { cam_in_1: { override: { device: '/dev/video3' } } },
    });
  });
});

describe('unboundCells', () => {
  it('identifies every cell without a camera selection or override path by node and device', () => {
    const context = bindingContext();
    const selections: BindingSelections = {
      'thing-1': {
        cam_in_1: { mode: 'camera', cameraSourceId: 'cfg-1', suggested: true },
        // cam_in_2 missing entirely — defaults to unbound.
      },
      'thing-2': {
        cam_in_1: { mode: 'override', device: '/dev/video0' },
        cam_in_2: { mode: 'override', device: '' },
      },
    };
    expect(unboundCells(context, selections)).toEqual([
      { device: 'thing-1', nodeId: 'cam_in_2' },
      { device: 'thing-2', nodeId: 'cam_in_2' },
    ]);
  });

  it('is empty when every cell is bound', () => {
    const context = bindingContext();
    let selections = initialBindingSelections(context);
    selections = withBindingCell(selections, 'thing-1', 'cam_in_2', {
      mode: 'camera',
      cameraSourceId: 'cfg-3',
      suggested: false,
    });
    selections = withBindingCell(selections, 'thing-2', 'cam_in_1', {
      mode: 'override',
      device: '/dev/video0',
    });
    selections = withBindingCell(selections, 'thing-2', 'cam_in_2', {
      mode: 'override',
      device: '/dev/video1',
    });
    expect(unboundCells(context, selections)).toEqual([]);
  });
});

// --------------------------------------------------------------------------
// Expected warnings feeding confirmed_warnings (8.8, 9.3)
// --------------------------------------------------------------------------

describe('expectedBindingWarnings', () => {
  it('emits never-synced:{device} for a never-synced target with an empty registry (Requirement 8.8)', () => {
    const context = bindingContext();
    const warnings = expectedBindingWarnings(context, initialBindingSelections(context));
    const neverSynced = warnings.filter((w) => w.code === 'DEVICE_NEVER_SYNCED');
    expect(neverSynced).toHaveLength(1);
    expect(neverSynced[0].id).toBe('never-synced:thing-2');
    expect(neverSynced[0].device).toBe('thing-2');
    expect(neverSynced[0].message).toContain('thing-2');
    expect(neverSynced[0].message).toContain('manual override');
  });

  it('emits camera-degraded ids matching the backend format for bound degraded sources (Requirement 9.3)', () => {
    const context = bindingContext();
    const selections: BindingSelections = {
      'thing-1': {
        // absent + stale, in the backend's condition order.
        cam_in_1: { mode: 'camera', cameraSourceId: 'cfg-2', suggested: false },
        // sync_status pending.
        cam_in_2: { mode: 'camera', cameraSourceId: 'cfg-3', suggested: true },
      },
    };
    const warnings = expectedBindingWarnings(context, selections);
    const degraded = warnings.filter((w) => w.code === 'CAMERA_SOURCE_DEGRADED');
    expect(degraded.map((w) => w.id)).toEqual([
      'camera-degraded:thing-1:cam_in_1:cfg-2:absent+stale',
      'camera-degraded:thing-1:cam_in_2:cfg-3:pending',
    ]);
    expect(degraded[0].conditions).toEqual(['absent', 'stale']);
    expect(degraded[0].message).toContain("'cfg-2'");
    expect(degraded[0].message).toContain("'cam_in_1'");
    expect(degraded[0].message).toContain("'thing-1'");
  });

  it('emits no warnings for healthy selections, overrides, or missing sources', () => {
    const context = bindingContext({ targets: { 'thing-1': SYNCED_TARGET } });
    const selections: BindingSelections = {
      'thing-1': {
        cam_in_1: { mode: 'camera', cameraSourceId: 'cfg-1', suggested: true },
        cam_in_2: { mode: 'override', device: '/dev/video9' },
      },
    };
    expect(expectedBindingWarnings(context, selections)).toEqual([]);
    // A selection of a source missing from the registry is an error
    // (9.2), never a degraded warning.
    const missing: BindingSelections = {
      'thing-1': { cam_in_1: { mode: 'camera', cameraSourceId: 'cfg-nope', suggested: false } },
    };
    expect(expectedBindingWarnings(context, missing)).toEqual([]);
  });
});

// --------------------------------------------------------------------------
// Submission rejection parsing (deployments.py error envelope)
// --------------------------------------------------------------------------

describe('parseCameraBindingRejection', () => {
  it('parses each of the four binding rejection codes', () => {
    for (const code of [
      'CAMERA_BINDINGS_INVALID',
      'CAMERA_WARNINGS_UNCONFIRMED',
      'REGISTRY_UNAVAILABLE',
      'BINDING_DELIVERY_FAILED',
    ] as const) {
      const rejection = parseCameraBindingRejection(code, 'boom', {});
      expect(rejection).not.toBeNull();
      expect(rejection!.code).toBe(code);
      expect(rejection!.message).toBe('boom');
    }
  });

  it('returns null for non-binding codes', () => {
    expect(parseCameraBindingRejection('VALIDATION_ERROR', 'boom', {})).toBeNull();
    expect(parseCameraBindingRejection(undefined, 'boom', {})).toBeNull();
  });

  it('maps structured errors and warnings from the rejection details', () => {
    const rejection = parseCameraBindingRejection('CAMERA_BINDINGS_INVALID', 'invalid', {
      errors: [
        {
          code: 'CAMERA_SOURCE_NOT_FOUND',
          device: 'thing-1',
          nodeId: 'cam_in_1',
          cameraSourceId: 'cfg-9',
          message: "Camera source 'cfg-9' is not registered",
        },
      ],
      warnings: [
        {
          id: 'never-synced:thing-2',
          code: 'DEVICE_NEVER_SYNCED',
          device: 'thing-2',
          message: 'never synced',
        },
        // Warnings without an id cannot be confirmed and are dropped.
        { code: 'MYSTERY', device: 'thing-3', message: 'no id' },
      ],
    });
    expect(rejection!.errors).toEqual([
      {
        code: 'CAMERA_SOURCE_NOT_FOUND',
        device: 'thing-1',
        nodeId: 'cam_in_1',
        cameraSourceId: 'cfg-9',
        parameter: undefined,
        message: "Camera source 'cfg-9' is not registered",
      },
    ]);
    expect(rejection!.warnings).toHaveLength(1);
    expect(rejection!.warnings[0].id).toBe('never-synced:thing-2');
  });

  it('tolerates missing or malformed details', () => {
    const rejection = parseCameraBindingRejection('REGISTRY_UNAVAILABLE', 'down', undefined);
    expect(rejection).toEqual({
      code: 'REGISTRY_UNAVAILABLE',
      message: 'down',
      errors: [],
      warnings: [],
    });
    const junk = parseCameraBindingRejection('CAMERA_BINDINGS_INVALID', 'x', {
      errors: 'not-a-list',
      warnings: [null, 42],
    });
    expect(junk!.errors).toEqual([]);
    expect(junk!.warnings).toEqual([]);
  });
});

describe('describeBindingIssue', () => {
  it('names the node and device (Requirement 8.7 surfacing)', () => {
    expect(
      describeBindingIssue({
        code: 'CAMERA_INPUT_UNBOUND',
        device: 'thing-1',
        nodeId: 'cam_in_1',
        message: 'no binding supplied',
      })
    ).toBe("Node 'cam_in_1' on device 'thing-1': no binding supplied");
    expect(
      describeBindingIssue({
        code: 'DEVICE_NEVER_SYNCED',
        device: 'thing-2',
        message: 'never synced',
      })
    ).toBe("Device 'thing-2': never synced");
  });
});

// --------------------------------------------------------------------------
// Dropdown option display (8.1)
// --------------------------------------------------------------------------

describe('camera option display', () => {
  it('labels options with the display name and describes type and path', () => {
    expect(cameraOptionLabel(HEALTHY)).toBe('Line 1 inspection cam');
    expect(cameraOptionDescription(HEALTHY)).toBe('Camera • /dev/video0');
    expect(cameraOptionDescription(PENDING)).toBe('RTSP • rtsp://10.0.0.5/stream');
    expect(cameraOptionLabel({ camera_source_id: 'cfg-x' })).toBe('cfg-x');
  });

  it('tags degraded statuses', () => {
    expect(cameraOptionTags(HEALTHY)).toEqual([]);
    expect(cameraOptionTags(STALE_ABSENT)).toEqual(['absent', 'stale']);
    expect(cameraOptionTags(PENDING)).toEqual(['pending']);
  });
});
