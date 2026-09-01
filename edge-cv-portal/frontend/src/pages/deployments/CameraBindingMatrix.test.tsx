/**
 * Component tests for the CreateDeployment binding matrix
 * (camera-registry-sync task 12.2 — Requirements 8.1, 8.5, 8.8, 8.9,
 * 9.3): one row per Camera_Input_Node with one cell per target device,
 * hint pre-selections marked as suggested until the user re-selects,
 * manual override entry, the never-synced warning restricting a device
 * to manual override, warning confirmation checkboxes feeding
 * `confirmed_warnings`, validation errors naming node and device, and
 * the empty state backing the skip when a version has no
 * Camera_Input_Nodes.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { CameraBindingMatrix } from './CameraBindingMatrix';
import type { CameraBindingMatrixProps } from './CameraBindingMatrix';
import {
  BindingContextNode,
  BindingContextTarget,
  BindingSelections,
  CameraBindingContext,
  initialBindingSelections,
} from './cameraBindings';
import type { CameraSourceEntry } from '../workflows/cameraReference';

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

const STALE: CameraSourceEntry = {
  camera_source_id: 'cfg-2',
  name: 'Rear dock cam',
  type: 'Camera',
  params: { devicePath: '/dev/video2' },
  sync_status: 'synced',
  stale: true,
  absent: false,
};

const ARAVIS_DISCOVERED: CameraSourceEntry = {
  camera_source_id: 'arv-1',
  name: 'Basler acA1920',
  type: 'AravisDiscovered',
  params: { cameraId: 'Basler-12345678' },
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const ARAVIS_CONFIGURED: CameraSourceEntry = {
  camera_source_id: 'cfg-3',
  name: 'Inspection GigE cam',
  type: 'Camera',
  params: { cameraId: 'Aravis-Fake-GV01' },
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const V4L2_DISCOVERED: CameraSourceEntry = {
  camera_source_id: 'disc-1',
  name: 'USB webcam',
  type: 'V4L2Discovered',
  params: { devicePath: '/dev/video9' },
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const NODE_HINTED: BindingContextNode = {
  node_id: 'cam_in_1',
  node_type: 'camera_source',
  binding_hint: {
    cameraSourceId: 'cfg-1',
    cameraName: 'Line 1 inspection cam',
    sourceDeviceId: 'dev-ref',
  },
};

const NODE_PLAIN: BindingContextNode = {
  node_id: 'cam_in_2',
  node_type: 'camera_source',
};

const SYNCED_TARGET: BindingContextTarget = {
  state: 'synced',
  cameras: [HEALTHY, STALE],
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

function renderMatrix(overrides: Partial<CameraBindingMatrixProps> = {}) {
  const context = overrides.context ?? bindingContext();
  const props: CameraBindingMatrixProps = {
    context,
    selections: initialBindingSelections(context),
    onCellChange: vi.fn(),
    warnings: [],
    confirmedWarningIds: new Set<string>(),
    onToggleWarning: vi.fn(),
    errors: [],
    ...overrides,
  };
  const view = render(<CameraBindingMatrix {...props} />);
  return { ...view, props };
}

/** The Cloudscape table cell (1-indexed row/column) as a wrapper. */
function bodyCell(container: HTMLElement, row: number, column: number) {
  const table = createWrapper(container).findTable()!;
  return table.findBodyCell(row, column)!;
}

// --------------------------------------------------------------------------
// Tests
// --------------------------------------------------------------------------

describe('CameraBindingMatrix', () => {
  it('renders one row per Camera_Input_Node with one cell per target device (Requirement 8.1)', () => {
    const { container } = renderMatrix();
    const table = createWrapper(container).findTable()!;

    // One column for the node identity plus one per target device.
    const headers = table.findColumnHeaders().map((h) => h.getElement().textContent);
    expect(headers).toEqual(['Camera input node', 'thing-1', 'thing-2']);

    // One row per node, identified by node id and type.
    expect(table.findRows()).toHaveLength(2);
    expect(bodyCell(container, 1, 1).getElement().textContent).toContain('cam_in_1');
    expect(bodyCell(container, 1, 1).getElement().textContent).toContain('camera_source');
    expect(bodyCell(container, 2, 1).getElement().textContent).toContain('cam_in_2');

    // Each synced-device cell offers the registered cameras as a dropdown.
    const select = createWrapper(bodyCell(container, 2, 2).getElement()).findSelect()!;
    select.openDropdown();
    const options = select.findDropdown({ expandToViewport: true }).findOptions().map((o) => o.getElement().textContent);
    expect(options).toHaveLength(2);
    expect(options[0]).toContain('Line 1 inspection cam');
    expect(options[0]).toContain('/dev/video0');
    expect(options[1]).toContain('Rear dock cam');
    expect(options[1]).toContain('stale');
  });

  it('marks a hint pre-selection as suggested and clears the mark when the user re-selects (Requirement 8.5)', () => {
    const { container, props } = renderMatrix();

    // The hinted cell shows the pre-selected camera with the suggestion badge.
    const hintedCell = bodyCell(container, 1, 2).getElement();
    expect(hintedCell.textContent).toContain('Suggested from workflow hint');
    expect(hintedCell.textContent).toContain('Line 1 inspection cam');

    // The unhinted cell carries no suggestion badge.
    expect(bodyCell(container, 2, 2).getElement().textContent).not.toContain(
      'Suggested from workflow hint'
    );

    // Re-selecting emits a user-made (non-suggested) camera cell.
    const select = createWrapper(hintedCell).findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('cfg-2', { expandToViewport: true });
    expect(props.onCellChange).toHaveBeenCalledWith('thing-1', 'cam_in_1', {
      mode: 'camera',
      cameraSourceId: 'cfg-2',
      suggested: false,
    });
  });

  it('offers manual override entry that feeds an override cell', () => {
    const context = bindingContext();
    const { props } = renderMatrix({ context });

    // Switching a cell to manual override.
    fireEvent.click(screen.getAllByRole('button', { name: 'Manual override' })[1]);
    expect(props.onCellChange).toHaveBeenCalledWith('thing-1', 'cam_in_2', {
      mode: 'override',
      device: '',
    });

    // With the cell in override mode, the input feeds the device path.
    const selections: BindingSelections = {
      ...initialBindingSelections(context),
      'thing-1': {
        ...initialBindingSelections(context)['thing-1'],
        cam_in_2: { mode: 'override', device: '' },
      },
    };
    const second = renderMatrix({ context, selections });
    const input = second.container.querySelector(
      'input[aria-label="Manual override device path for node cam_in_2 on device thing-1"]'
    )!;
    fireEvent.change(input, { target: { value: '/dev/video5' } });
    expect(second.props.onCellChange).toHaveBeenCalledWith('thing-1', 'cam_in_2', {
      mode: 'override',
      device: '/dev/video5',
    });
    // An override cell offers the way back to a registered camera.
    expect(
      second.container.textContent
    ).toContain('Use registered camera');
  });

  it('warns for never-synced devices and restricts their cells to manual override (Requirement 8.8)', () => {
    const { container } = renderMatrix();

    // The banner names the never-synced device.
    const banner = screen.getByText(/never completed a camera registry/i);
    expect(banner.parentElement!.textContent).toContain('thing-2');

    // Both cells of the never-synced column show the warning text and
    // only the override input — no camera dropdown, no mode toggle.
    for (const row of [1, 2]) {
      const cell = bodyCell(container, row, 3).getElement();
      expect(cell.textContent).toContain('Never synced — manual override only');
      expect(createWrapper(cell).findSelect()).toBeNull();
      expect(cell.querySelector('input[aria-label^="Manual override device path"]')).not.toBeNull();
      expect(createWrapper(cell).findButton()).toBeNull();
    }
  });

  it('feeds the never-synced override input into an override cell', () => {
    const { container, props } = renderMatrix();
    const input = container.querySelector(
      'input[aria-label="Manual override device path for node cam_in_1 on device thing-2"]'
    )!;
    fireEvent.change(input, { target: { value: '/dev/video1' } });
    expect(props.onCellChange).toHaveBeenCalledWith('thing-2', 'cam_in_1', {
      mode: 'override',
      device: '/dev/video1',
    });
  });

  it('renders warning confirmation checkboxes that toggle confirmed ids (Requirement 9.3)', () => {
    const warnings = [
      {
        id: 'never-synced:thing-2',
        code: 'DEVICE_NEVER_SYNCED',
        device: 'thing-2',
        message: "Device 'thing-2' has never completed a camera registry synchronization",
      },
      {
        id: 'camera-degraded:thing-1:cam_in_1:cfg-2:stale',
        code: 'CAMERA_SOURCE_DEGRADED',
        device: 'thing-1',
        nodeId: 'cam_in_1',
        cameraSourceId: 'cfg-2',
        conditions: ['stale'],
        message: "Camera source 'cfg-2' bound to node 'cam_in_1' on device 'thing-1' is stale",
      },
    ];
    const { container, props } = renderMatrix({
      warnings,
      confirmedWarningIds: new Set(['never-synced:thing-2']),
    });

    expect(screen.getByText('Camera binding warnings require confirmation')).toBeInTheDocument();
    const checkboxes = createWrapper(container).findAllCheckboxes();
    expect(checkboxes).toHaveLength(2);

    // Already-confirmed ids render checked.
    expect(checkboxes[0].findNativeInput().getElement()).toBeChecked();
    expect(checkboxes[1].findNativeInput().getElement()).not.toBeChecked();

    // Toggling reports the warning id and the new state.
    fireEvent.click(checkboxes[1].findNativeInput().getElement());
    expect(props.onToggleWarning).toHaveBeenCalledWith(
      'camera-degraded:thing-1:cam_in_1:cfg-2:stale',
      true
    );
    fireEvent.click(checkboxes[0].findNativeInput().getElement());
    expect(props.onToggleWarning).toHaveBeenCalledWith('never-synced:thing-2', false);
  });

  it('renders no warning section without warnings', () => {
    renderMatrix();
    expect(screen.queryByText('Camera binding warnings require confirmation')).toBeNull();
  });

  it('lists validation errors naming the node and device', () => {
    renderMatrix({
      errors: [
        {
          code: 'CAMERA_INPUT_UNBOUND',
          device: 'thing-1',
          nodeId: 'cam_in_2',
          message: 'no binding supplied',
        },
        {
          code: 'DEVICE_NEVER_SYNCED',
          device: 'thing-2',
          message: 'never synced',
        },
      ],
    });
    expect(screen.getByText('Camera binding validation failed')).toBeInTheDocument();
    expect(
      screen.getByText("Node 'cam_in_2' on device 'thing-1': no binding supplied")
    ).toBeInTheDocument();
    expect(screen.getByText("Device 'thing-2': never synced")).toBeInTheDocument();
  });

  it('offers only Aravis-compatible sources for an aravis_camera_source row with hint pre-selection (aravis-camera-input Requirement 5.1)', () => {
    // Mixed registry: two Aravis-compatible entries (a discovered bus
    // camera and a configured Camera-type source with a cameraId), a
    // V4L2Discovered entry, and a Camera-type entry without a cameraId —
    // the last two must not be offered to the Aravis node.
    const aravisNode: BindingContextNode = {
      node_id: 'arv_in_1',
      node_type: 'aravis_camera_source',
      binding_hint: {
        cameraSourceId: 'arv-1',
        cameraName: 'Basler acA1920',
        sourceDeviceId: 'dev-ref',
      },
    };
    const context = bindingContext({
      camera_input_nodes: [aravisNode, NODE_PLAIN],
      targets: {
        'thing-1': {
          state: 'synced',
          cameras: [HEALTHY, ARAVIS_DISCOVERED, ARAVIS_CONFIGURED, V4L2_DISCOVERED],
          preselected: { arv_in_1: 'arv-1' },
        },
      },
    });
    const { container } = renderMatrix({ context });

    // The Aravis row's dropdown contains exactly the compatible entries.
    const aravisCell = bodyCell(container, 1, 2).getElement();
    const aravisSelect = createWrapper(aravisCell).findSelect()!;
    aravisSelect.openDropdown();
    const aravisOptions = aravisSelect
      .findDropdown({ expandToViewport: true })
      .findOptions()
      .map((o) => o.getElement().textContent);
    expect(aravisOptions).toHaveLength(2);
    expect(aravisOptions[0]).toContain('Basler acA1920');
    expect(aravisOptions[1]).toContain('Inspection GigE cam');
    aravisSelect.closeDropdown();

    // Hint pre-selection is unchanged: the hinted compatible entry is
    // pre-selected and marked as suggested.
    expect(aravisCell.textContent).toContain('Suggested from workflow hint');
    expect(aravisCell.textContent).toContain('Basler acA1920');

    // The camera_source row still offers every registered entry.
    const cameraSelect = createWrapper(bodyCell(container, 2, 2).getElement()).findSelect()!;
    cameraSelect.openDropdown();
    expect(cameraSelect.findDropdown({ expandToViewport: true }).findOptions()).toHaveLength(4);
  });

  it('shows the empty state for a context without Camera_Input_Nodes (Requirement 8.9 contract)', () => {
    // CreateDeployment renders the matrix only for contexts with
    // binding_required; a nodeless context degrades to the empty state.
    const context = bindingContext({ camera_input_nodes: [], targets: { 'thing-1': SYNCED_TARGET } });
    const { container } = renderMatrix({ context, selections: {} });
    expect(createWrapper(container).findTable()!.findRows()).toHaveLength(0);
    expect(screen.getByText('No camera input nodes')).toBeInTheDocument();
  });
});
