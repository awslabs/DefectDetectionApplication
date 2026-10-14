/**
 * Unit tests for the stream camera additions to the deploy-time binding
 * helpers (rtsp-rtmp-stream-cameras tasks 11.6/11.7 — Requirements 9.3,
 * 9.4, 9.5, 16.4):
 *
 * - the per-node-type override identity parameter (`url` for the two
 *   Stream_Camera_Source_Node types, `device` for everything else),
 * - `buildCameraBindings` emitting `{override: {url}}` for stream nodes
 *   and `{override: {device}}` unchanged for every other node,
 * - Python-parity stripping of a typed Stream_URL, so the matrix checks
 *   exactly the value deployments.py checks,
 * - `invalidOverrideCells` reporting the backend's Stream_URL messages,
 * - the `stream-failed` degraded condition and its warning id, mirroring
 *   deployments.py `_degraded_source_conditions`, and
 * - the stream tags of a dropdown option.
 */
import { describe, expect, it } from 'vitest';
import type { CameraSourceEntry } from '../workflows/cameraReference';
import { checkStreamUrl } from '../workflows/streamUrl';
import {
  BindingCell,
  BindingContextNode,
  BindingSelections,
  CameraBindingContext,
  buildCameraBindings,
  cameraOptionTags,
  degradedConditions,
  expectedBindingWarnings,
  invalidOverrideCells,
  isStreamBindingNode,
  overrideIdentityParameter,
  overrideValue,
  streamOverrideProblem,
  unboundCells,
} from './cameraBindings';

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const RTSP_NODE: BindingContextNode = { node_id: 'dock_cam', node_type: 'rtsp_camera_source' };
const RTMP_NODE: BindingContextNode = { node_id: 'line_feed', node_type: 'rtmp_stream_source' };
const CAMERA_NODE: BindingContextNode = { node_id: 'cam_in', node_type: 'icam_source' };

const RTSP_URL = 'rtsp://10.0.4.21:554/Streaming/Channels/101';
const RTMP_URL = 'rtmp://media.local/live/line1';

function streamEntry(
  overrides: Partial<CameraSourceEntry> & { stream?: Record<string, unknown> } = {}
): CameraSourceEntry {
  const { stream, ...rest } = overrides;
  return {
    camera_source_id: 'rtsp-dock',
    name: 'Dock 3 overview',
    type: 'RTSP',
    params: { url: RTSP_URL },
    capabilities: stream === undefined ? {} : { stream: stream as never },
    sync_status: 'synced',
    stale: false,
    absent: false,
    ...rest,
  };
}

function context(cameras: CameraSourceEntry[]): CameraBindingContext {
  return {
    workflow_id: 'wf-stream',
    workflow_version: 2,
    has_binding_points: true,
    binding_required: true,
    camera_input_nodes: [RTSP_NODE, RTMP_NODE, CAMERA_NODE],
    targets: { 'thing-1': { state: 'synced', cameras, preselected: {} } },
  };
}

const override = (device: string): BindingCell => ({ mode: 'override', device });

// --------------------------------------------------------------------------
// Override identity parameter
// --------------------------------------------------------------------------

describe('overrideIdentityParameter', () => {
  it('is url for both Stream_Camera_Source_Node types (Requirement 9.4)', () => {
    expect(overrideIdentityParameter('rtsp_camera_source')).toBe('url');
    expect(overrideIdentityParameter('rtmp_stream_source')).toBe('url');
    expect(isStreamBindingNode('rtsp_camera_source')).toBe(true);
    expect(isStreamBindingNode('rtmp_stream_source')).toBe(true);
  });

  it('stays device for every other node type, including prototype member names', () => {
    for (const nodeType of [
      'camera_source',
      'icam_source',
      'csi_camera_source',
      'aravis_camera_source',
      'custom.my_camera',
      'toString',
      'constructor',
      '__proto__',
      'hasOwnProperty',
      '',
      undefined,
      null,
    ]) {
      expect(overrideIdentityParameter(nodeType)).toBe('device');
      expect(isStreamBindingNode(nodeType)).toBe(false);
    }
  });
});

// --------------------------------------------------------------------------
// Override values: Python whitespace for Stream_URLs, trim() for paths
// --------------------------------------------------------------------------

describe('overrideValue', () => {
  it("strips a Stream_URL with Python's whitespace set", () => {
    // U+0085 (NEL) and U+001C-U+001F are Python whitespace but not
    // JavaScript's; the backend's str.strip() removes them.
    expect(overrideValue('rtsp_camera_source', override(`\u0085 ${RTSP_URL}\u001f\u2028`))).toBe(
      RTSP_URL
    );
    // U+FEFF is JavaScript whitespace but not Python's: it stays, exactly
    // as the backend keeps it, and the URL is then invalid there too.
    const withBom = `\ufeff${RTSP_URL}`;
    expect(overrideValue('rtsp_camera_source', override(withBom))).toBe(withBom);
    expect(streamOverrideProblem('rtsp_camera_source', withBom)).toBe(
      checkStreamUrl(withBom, ['rtsp', 'rtsps'])!.message
    );
  });

  it('keeps trim() for device paths and is empty for non-override cells', () => {
    expect(overrideValue('icam_source', override('  /dev/video3  '))).toBe('/dev/video3');
    expect(overrideValue(undefined, override('\t/dev/video0\n'))).toBe('/dev/video0');
    expect(overrideValue('rtsp_camera_source', { mode: 'unbound' })).toBe('');
    expect(
      overrideValue('rtsp_camera_source', {
        mode: 'camera',
        cameraSourceId: 'rtsp-dock',
        suggested: false,
      })
    ).toBe('');
  });
});

// --------------------------------------------------------------------------
// camera_bindings payload
// --------------------------------------------------------------------------

describe('buildCameraBindings with node types', () => {
  it('emits {override: {url}} for stream nodes and {override: {device}} for the rest', () => {
    const selections: BindingSelections = {
      'thing-1': {
        dock_cam: override(`  ${RTSP_URL} `),
        line_feed: override(RTMP_URL),
        cam_in: override(' /dev/video2 '),
      },
      'thing-2': {
        dock_cam: { mode: 'camera', cameraSourceId: 'rtsp-dock', suggested: true },
      },
    };
    expect(buildCameraBindings(selections, [RTSP_NODE, RTMP_NODE, CAMERA_NODE])).toEqual({
      'thing-1': {
        dock_cam: { override: { url: RTSP_URL } },
        line_feed: { override: { url: RTMP_URL } },
        cam_in: { override: { device: '/dev/video2' } },
      },
      // A registered-source selection is unchanged for stream nodes.
      'thing-2': { dock_cam: { cameraSourceId: 'rtsp-dock' } },
    });
  });

  it('keeps the pre-feature payload without node types, or for an unknown node', () => {
    const selections: BindingSelections = {
      'thing-1': { dock_cam: override(RTSP_URL), mystery: override('/dev/video7') },
    };
    expect(buildCameraBindings(selections)).toEqual({
      'thing-1': {
        dock_cam: { override: { device: RTSP_URL } },
        mystery: { override: { device: '/dev/video7' } },
      },
    });
    expect(buildCameraBindings(selections, [RTSP_NODE])).toEqual({
      'thing-1': {
        dock_cam: { override: { url: RTSP_URL } },
        mystery: { override: { device: '/dev/video7' } },
      },
    });
  });

  it('omits a stream override holding only Python whitespace, and reports it unbound', () => {
    const selections: BindingSelections = {
      'thing-1': {
        dock_cam: override('\u0085\u001c '),
        line_feed: override(RTMP_URL),
        cam_in: override('/dev/video0'),
      },
    };
    expect(buildCameraBindings(selections, [RTSP_NODE, RTMP_NODE, CAMERA_NODE])).toEqual({
      'thing-1': {
        line_feed: { override: { url: RTMP_URL } },
        cam_in: { override: { device: '/dev/video0' } },
      },
    });
    expect(unboundCells(context([]), selections)).toEqual([
      { device: 'thing-1', nodeId: 'dock_cam' },
    ]);
  });
});

// --------------------------------------------------------------------------
// Stream_URL override validation (Requirement 9.4)
// --------------------------------------------------------------------------

describe('invalidOverrideCells', () => {
  it("reports each invalid stream override with the backend's Stream_URL message", () => {
    const selections: BindingSelections = {
      'thing-1': {
        dock_cam: override('rtsp://admin:hunter2@10.0.4.21/stream'),
        line_feed: override('rtsp://10.0.4.21/stream'),
        cam_in: override('rtsp://admin:hunter2@10.0.4.21/stream'),
      },
    };
    const invalid = invalidOverrideCells(context([]), selections);
    expect(invalid).toEqual([
      {
        device: 'thing-1',
        nodeId: 'dock_cam',
        message: checkStreamUrl('rtsp://admin:hunter2@10.0.4.21/stream', ['rtsp', 'rtsps'])!
          .message,
      },
      {
        device: 'thing-1',
        nodeId: 'line_feed',
        message: checkStreamUrl('rtsp://10.0.4.21/stream', ['rtmp', 'rtmps'])!.message,
      },
    ]);
    // The user information is named as the reason, and never echoed.
    expect(invalid[0].message).toContain("credentials belong in the camera's configuration");
    expect(invalid[0].message).not.toContain('hunter2');
    // The wrong protocol is named by its scheme.
    expect(invalid[1].message).toContain("scheme 'rtsp' is not allowed");
  });

  it('flags a Secret_Query_Parameter and accepts valid, empty, and camera-node values', () => {
    expect(streamOverrideProblem('rtsp_camera_source', `${RTSP_URL}?password=x`)).toContain(
      "query parameter 'password' carries credentials"
    );
    expect(streamOverrideProblem('rtsp_camera_source', RTSP_URL)).toBeNull();
    expect(streamOverrideProblem('rtsp_camera_source', 'rtsps://cam.local/live')).toBeNull();
    expect(streamOverrideProblem('rtmp_stream_source', 'rtmps://media.local/live/k')).toBeNull();
    expect(streamOverrideProblem('rtsp_camera_source', '')).toBeNull();
    expect(streamOverrideProblem('rtsp_camera_source', ' \u0085 ')).toBeNull();
    // Device paths are validated by the node's declared constraints on
    // the backend, not by the Stream_URL rules.
    expect(streamOverrideProblem('icam_source', 'rtsp://u:p@h/x')).toBeNull();
    const selections: BindingSelections = {
      'thing-1': {
        dock_cam: override(RTSP_URL),
        line_feed: override(''),
        cam_in: override('/dev/video0'),
      },
    };
    expect(invalidOverrideCells(context([]), selections)).toEqual([]);
  });
});

// --------------------------------------------------------------------------
// Degraded sources (Requirement 9.5) — deployments.py parity
// --------------------------------------------------------------------------

describe('degradedConditions and the stream-failed warning', () => {
  it('appends stream-failed last for a failed Stream_Health state', () => {
    expect(degradedConditions(streamEntry({ stream: { state: 'failed' } }))).toEqual([
      'stream-failed',
    ]);
    expect(
      degradedConditions(
        streamEntry({
          absent: true,
          stale: true,
          sync_status: 'failed',
          stream: { state: 'failed', reason: 'unreachable' },
        })
      )
    ).toEqual(['absent', 'stale', 'failed', 'stream-failed']);
  });

  it('adds nothing for the other states or a malformed capability', () => {
    for (const state of ['streaming', 'reconnecting', 'idle', 'FAILED', '', 5, null]) {
      expect(degradedConditions(streamEntry({ stream: { state } }))).toEqual([]);
    }
    expect(degradedConditions(streamEntry())).toEqual([]);
    for (const capabilities of [
      { stream: 'failed' },
      { stream: null },
      { stream: ['failed'] },
      null,
    ]) {
      expect(
        degradedConditions(streamEntry({ capabilities: capabilities as CameraSourceEntry['capabilities'] }))
      ).toEqual([]);
    }
  });

  it('keeps the warning id of every entry without a failed stream', () => {
    const pending = streamEntry({ camera_source_id: 'rtsp-pending', sync_status: 'pending' });
    expect(degradedConditions(pending)).toEqual(['pending']);
    const camera: CameraSourceEntry = {
      camera_source_id: 'cfg-1',
      type: 'Camera',
      params: { devicePath: '/dev/video0' },
      stale: true,
    };
    expect(degradedConditions(camera)).toEqual(['stale']);
  });

  it('produces the backend warning id for a bound failed stream camera', () => {
    const failed = streamEntry({ stream: { state: 'failed' } });
    const ctx = context([failed]);
    const warnings = expectedBindingWarnings(ctx, {
      'thing-1': { dock_cam: { mode: 'camera', cameraSourceId: 'rtsp-dock', suggested: false } },
    });
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toMatchObject({
      id: 'camera-degraded:thing-1:dock_cam:rtsp-dock:stream-failed',
      code: 'CAMERA_SOURCE_DEGRADED',
      device: 'thing-1',
      nodeId: 'dock_cam',
      cameraSourceId: 'rtsp-dock',
      conditions: ['stream-failed'],
    });
    expect(warnings[0].message).toBe(
      "Camera source 'rtsp-dock' bound to node 'dock_cam' on device 'thing-1' is stream-failed"
    );
  });
});

// --------------------------------------------------------------------------
// Dropdown tags (Requirement 16.4)
// --------------------------------------------------------------------------

describe('cameraOptionTags for stream cameras', () => {
  it('adds the reported codec, resolution, and health after the status tags', () => {
    expect(
      cameraOptionTags(
        streamEntry({ stream: { codec: 'h265', width: 1920, height: 1080, state: 'streaming' } })
      )
    ).toEqual(['H.265', '1920\u00d71080', 'streaming']);
    expect(
      cameraOptionTags(
        streamEntry({
          stale: true,
          sync_status: 'pending',
          stream: { codec: 'h264', state: 'failed' },
        })
      )
    ).toEqual(['stale', 'pending', 'H.264', 'stream failed']);
  });

  it('shows an unknown codec or state as written, and adds nothing when unreported', () => {
    expect(
      cameraOptionTags(streamEntry({ stream: { codec: 'constructor', state: 'buffering' } }))
    ).toEqual(['constructor', 'buffering']);
    expect(cameraOptionTags(streamEntry())).toEqual([]);
    // A non-positive or partial resolution is not shown.
    expect(cameraOptionTags(streamEntry({ stream: { width: 0, height: 1080 } }))).toEqual([]);
  });
});
