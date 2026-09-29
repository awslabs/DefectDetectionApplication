/**
 * Component tests for stream nodes in the CreateDeployment binding matrix
 * (rtsp-rtmp-stream-cameras task 11.7 — Requirements 9.3, 9.4, 9.5).
 *
 * - A Stream_Camera_Source_Node row offers only the Camera_Sources of its
 *   own protocol, and names the protocol when there are none (9.3).
 * - Its manual override collects a Stream_URL, checks it as it is typed
 *   with the backend's Stream_URL rules, and — driven through the matrix
 *   exactly as CreateDeployment drives it — produces `{override: {url}}`
 *   in the `camera_bindings` payload, while a camera node's override
 *   keeps producing `{override: {device}}` (9.4).
 * - A bound stream camera whose last reported health is `failed` raises
 *   the degraded-source warning the matrix asks the user to confirm (9.5).
 */
import { useEffect, useState } from 'react';
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { CameraBindingMatrix } from './CameraBindingMatrix';
import {
  BindingContextNode,
  BindingSelections,
  CameraBindingContext,
  buildCameraBindings,
  expectedBindingWarnings,
  initialBindingSelections,
  withBindingCell,
} from './cameraBindings';
import type { CameraSourceEntry } from '../workflows/cameraReference';

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const DOCK_URL = 'rtsp://10.0.4.21:554/Streaming/Channels/101';

const RTSP_DOCK: CameraSourceEntry = {
  camera_source_id: 'rtsp-dock',
  name: 'Dock 3 overview',
  type: 'RTSP',
  params: { url: DOCK_URL, credentialsConfigured: true },
  capabilities: { stream: { state: 'streaming', codec: 'h265', width: 1920, height: 1080 } },
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const RTSP_FAILED: CameraSourceEntry = {
  camera_source_id: 'rtsp-yard',
  name: 'Yard',
  type: 'RTSP',
  params: { url: 'rtsps://yard.local/live' },
  capabilities: { stream: { state: 'failed', codec: 'h264' } },
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const RTMP_LINE: CameraSourceEntry = {
  camera_source_id: 'rtmp-line',
  name: 'Line 1 encoder',
  type: 'RTMP',
  params: { url: 'rtmp://media.local/live/line1' },
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const USB_CAM: CameraSourceEntry = {
  camera_source_id: 'cfg-usb',
  name: 'USB cam',
  type: 'Camera',
  params: { devicePath: '/dev/video0' },
  sync_status: 'synced',
  stale: false,
  absent: false,
};

const RTSP_NODE: BindingContextNode = { node_id: 'dock_cam', node_type: 'rtsp_camera_source' };
const RTMP_NODE: BindingContextNode = { node_id: 'line_feed', node_type: 'rtmp_stream_source' };
const CAMERA_NODE: BindingContextNode = { node_id: 'cam_in', node_type: 'icam_source' };

function streamContext(overrides: Partial<CameraBindingContext> = {}): CameraBindingContext {
  return {
    workflow_id: 'wf-stream',
    workflow_version: 2,
    has_binding_points: true,
    binding_required: true,
    camera_input_nodes: [RTSP_NODE, RTMP_NODE, CAMERA_NODE],
    targets: {
      'line-a': {
        state: 'synced',
        cameras: [USB_CAM, RTSP_DOCK, RTMP_LINE, RTSP_FAILED],
        preselected: {},
      },
      'line-b': { state: 'never-synced', cameras: [], preselected: {} },
    },
    ...overrides,
  };
}

/**
 * The matrix with its selections held in state and updated through
 * `onCellChange`, as CreateDeployment holds them; the latest selections
 * are reported to the test after every change.
 */
function StatefulMatrix({
  context,
  onSelections,
}: {
  context: CameraBindingContext;
  onSelections: (selections: BindingSelections) => void;
}) {
  const [selections, setSelections] = useState(() => initialBindingSelections(context));
  useEffect(() => onSelections(selections), [selections, onSelections]);
  return (
    <CameraBindingMatrix
      context={context}
      selections={selections}
      onCellChange={(device, nodeId, cell) =>
        setSelections((previous) => withBindingCell(previous, device, nodeId, cell))
      }
      warnings={expectedBindingWarnings(context, selections)}
      confirmedWarningIds={new Set<string>()}
      onToggleWarning={() => undefined}
      errors={[]}
    />
  );
}

function renderStateful(context: CameraBindingContext) {
  const latest: { selections: BindingSelections } = { selections: {} };
  const view = render(
    <StatefulMatrix
      context={context}
      onSelections={(selections) => {
        latest.selections = selections;
      }}
    />
  );
  return { ...view, latest };
}

function cell(container: HTMLElement, row: number, column: number) {
  return createWrapper(container).findTable()!.findBodyCell(row, column)!.getElement();
}

function overrideInput(container: HTMLElement, nodeId: string, device: string) {
  return container.querySelector(
    `input[aria-label="Manual override Stream URL for node ${nodeId} on device ${device}"]`
  ) as HTMLInputElement | null;
}

// --------------------------------------------------------------------------
// Row filtering (Requirement 9.3)
// --------------------------------------------------------------------------

describe('stream rows of the binding matrix (Requirement 9.3)', () => {
  it("offer only the Camera_Sources of the row's protocol", () => {
    const { container } = renderStateful(streamContext());
    const optionTexts = (row: number) => {
      const select = createWrapper(cell(container, row, 2)).findSelect()!;
      select.openDropdown();
      const texts = select
        .findDropdown({ expandToViewport: true })
        .findOptions()
        .map((option) => option.getElement().textContent ?? '');
      select.closeDropdown();
      return texts;
    };

    const rtsp = optionTexts(1);
    expect(rtsp).toHaveLength(2);
    expect(rtsp[0]).toContain('Dock 3 overview');
    expect(rtsp[0]).toContain(DOCK_URL);
    expect(rtsp[0]).toContain('H.265');
    expect(rtsp[1]).toContain('Yard');
    expect(rtsp[1]).toContain('stream failed');

    const rtmp = optionTexts(2);
    expect(rtmp).toHaveLength(1);
    expect(rtmp[0]).toContain('Line 1 encoder');

    // The icam_source row keeps its own V4L2 filter: no stream camera.
    const camera = optionTexts(3);
    expect(camera).toHaveLength(1);
    expect(camera[0]).toContain('USB cam');
  });

  it('names the protocol when the device registers no camera of it', () => {
    const context = streamContext({
      camera_input_nodes: [RTMP_NODE],
      targets: { 'line-a': { state: 'synced', cameras: [USB_CAM, RTSP_DOCK], preselected: {} } },
    });
    const { container } = renderStateful(context);
    const select = createWrapper(cell(container, 1, 2)).findSelect()!;
    expect(select.findTrigger().getElement().textContent).toContain('No RTMP cameras registered');
  });
});

// --------------------------------------------------------------------------
// Stream_URL overrides emit {url} (Requirement 9.4)
// --------------------------------------------------------------------------

describe('stream overrides (Requirement 9.4)', () => {
  it('collect a Stream_URL that the payload sends as {override: {url}}', () => {
    const context = streamContext();
    const { container, latest } = renderStateful(context);

    // Synced device: switch the RTSP and the camera row to manual override.
    fireEvent.click(
      createWrapper(cell(container, 1, 2)).findButton()!.getElement()
    );
    fireEvent.click(
      createWrapper(cell(container, 3, 2)).findButton()!.getElement()
    );
    const rtspInput = overrideInput(container, 'dock_cam', 'line-a')!;
    expect(rtspInput).not.toBeNull();
    expect(rtspInput.placeholder).toBe(
      'Stream URL, e.g. rtsp://192.168.1.64:554/Streaming/Channels/101'
    );
    fireEvent.change(rtspInput, { target: { value: `  ${DOCK_URL} ` } });
    const cameraInput = container.querySelector(
      'input[aria-label="Manual override device path for node cam_in on device line-a"]'
    ) as HTMLInputElement;
    fireEvent.change(cameraInput, { target: { value: '/dev/video2' } });

    // Never-synced device: the stream row takes a Stream_URL directly.
    const neverSynced = overrideInput(container, 'line_feed', 'line-b')!;
    expect(neverSynced.placeholder).toBe('Stream URL, e.g. rtmp://media.local/live/line1');
    fireEvent.change(neverSynced, { target: { value: 'rtmps://media.local/live/line1' } });

    expect(buildCameraBindings(latest.selections, context.camera_input_nodes)).toEqual({
      'line-a': {
        dock_cam: { override: { url: DOCK_URL } },
        cam_in: { override: { device: '/dev/video2' } },
      },
      'line-b': { line_feed: { override: { url: 'rtmps://media.local/live/line1' } } },
    });
  });

  it('show the Stream_URL message as it is typed, without echoing credentials', () => {
    const { container } = renderStateful(streamContext());
    const input = overrideInput(container, 'dock_cam', 'line-b')!;
    const errorOf = () =>
      createWrapper(cell(container, 1, 3)).findFormField()?.findError()?.getElement().textContent ??
      null;

    expect(errorOf()).toBeNull();
    fireEvent.change(input, { target: { value: 'rtsp://admin:hunter2@10.0.4.21/stream' } });
    expect(errorOf()).toContain("credentials belong in the camera's configuration");
    expect(errorOf()).not.toContain('hunter2');
    expect(input).toHaveAttribute('aria-invalid', 'true');

    fireEvent.change(input, { target: { value: 'rtmp://media.local/live' } });
    expect(errorOf()).toContain("Stream URL scheme 'rtmp' is not allowed here");

    fireEvent.change(input, { target: { value: DOCK_URL } });
    expect(errorOf()).toBeNull();
  });
});

// --------------------------------------------------------------------------
// Failed stream health warning (Requirement 9.5)
// --------------------------------------------------------------------------

describe('a failed stream camera (Requirement 9.5)', () => {
  it('raises the degraded-source warning for confirmation', () => {
    const context = streamContext({ camera_input_nodes: [RTSP_NODE] });
    const { container } = renderStateful(context);
    const select = createWrapper(cell(container, 1, 2)).findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('rtsp-yard', { expandToViewport: true });

    expect(
      screen.getByText(
        "Camera source 'rtsp-yard' bound to node 'dock_cam' on device 'line-a' is stream-failed"
      )
    ).toBeInTheDocument();
    expect(createWrapper(container).findCheckbox()).not.toBeNull();
  });
});
