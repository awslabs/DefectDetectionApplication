/**
 * Frontend example tests for the subscribe-trigger designer support
 * (trigger-activation-runtime, task 5.4).
 *
 * Covers:
 *  - Node_Palette lists `mqtt_subscribe` and `opcua_subscribe` under the
 *    Triggers section when the served catalog includes the two mirror
 *    descriptors — Requirement 3.5
 *  - `types.ts` mirror descriptor content: type ids, trigger category,
 *    zero inputs / one EventSignal output, connection parameters
 *    mirroring `mqtt_publish` (names/types/defaults/constraints and the
 *    bool `aws_iot` gating), the `opcua_write` endpoint/security surface,
 *    and the shared policy family with its `"name=value"` `dependsOn`
 *    gating strings — Requirement 3.5
 *  - config-panel gating: `queue_depth` visible only under
 *    `concurrency_policy=queue`, `debounce_ms` only under `=debounce`,
 *    `poll_interval_ms` only under `mode=poll`, with the existing
 *    bool-based `aws_iot` gating unchanged — Requirement 3.6
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import NodePalette from './NodePalette';
import NodeConfigPanel, { isParameterVisible } from './NodeConfigPanel';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from './builderGraph';
import {
  CATEGORY_TRIGGER,
  MQTT_SUBSCRIBE_DESCRIPTOR,
  OPCUA_SUBSCRIBE_DESCRIPTOR,
  PORT_TYPE_EVENT_SIGNAL,
  type JsonValue,
  type NodeTypeDescriptor,
  type ParameterDescriptor,
} from './types';

const { listModels, listDevices, getDeviceCameras, useUsecaseMock } = vi.hoisted(() => ({
  listModels: vi.fn(),
  listDevices: vi.fn(),
  getDeviceCameras: vi.fn(),
  useUsecaseMock: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: { listModels, listDevices, getDeviceCameras },
}));

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: useUsecaseMock,
}));

beforeEach(() => {
  listModels.mockReset();
  listModels.mockResolvedValue({ models: [], count: 0, usecase_id: 'uc-1' });
  listDevices.mockReset();
  listDevices.mockResolvedValue({ devices: [], count: 0 });
  getDeviceCameras.mockReset();
  getDeviceCameras.mockResolvedValue({
    device_id: 'dev-1',
    state: 'synced',
    cameras: [],
    count: 0,
  });
  useUsecaseMock.mockReturnValue({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  });
});

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

function parameter(descriptor: NodeTypeDescriptor, name: string): ParameterDescriptor {
  const found = descriptor.parameters.find((entry) => entry.name === name);
  expect(found, `${descriptor.typeId} declares parameter '${name}'`).toBeDefined();
  return found!;
}

function builderNode(
  descriptor: NodeTypeDescriptor,
  parameters: Record<string, JsonValue> = {}
): BuilderNode {
  return {
    id: `${descriptor.typeId}_1`,
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: { descriptor, parameters, validationMessages: [] },
  };
}

/** isParameterVisible against a trigger descriptor's own parameter list. */
function visible(
  descriptor: NodeTypeDescriptor,
  name: string,
  parameters: Record<string, JsonValue>
): boolean {
  return isParameterVisible(parameter(descriptor, name), descriptor.parameters, parameters);
}

const POLICY_PARAMETER_NAMES = [
  'concurrency_policy',
  'queue_depth',
  'debounce_ms',
  'retry_limit',
  'priority',
];

const IOT_PARAMETER_NAMES = [
  'iot_thing_name',
  'iot_ca_cert_path',
  'iot_client_cert_path',
  'iot_private_key_path',
];

// --------------------------------------------------------------------------
// Palette grouping (Requirement 3.5)
// --------------------------------------------------------------------------

describe('palette lists the subscribe triggers under Triggers (Requirement 3.5)', () => {
  const CATALOG: NodeTypeDescriptor[] = [MQTT_SUBSCRIBE_DESCRIPTOR, OPCUA_SUBSCRIBE_DESCRIPTOR];

  it('lists mqtt_subscribe and opcua_subscribe in the Triggers section', () => {
    render(<NodePalette catalog={CATALOG} />);
    const triggersSection = screen.getByRole('region', { name: 'Triggers' });
    expect(
      within(triggersSection).getByText(MQTT_SUBSCRIBE_DESCRIPTOR.displayName)
    ).toBeInTheDocument();
    expect(
      within(triggersSection).getByText(OPCUA_SUBSCRIBE_DESCRIPTOR.displayName)
    ).toBeInTheDocument();
  });

  it('lists each trigger exactly once across the whole palette', () => {
    render(<NodePalette catalog={CATALOG} />);
    expect(screen.getAllByText(MQTT_SUBSCRIBE_DESCRIPTOR.displayName)).toHaveLength(1);
    expect(screen.getAllByText(OPCUA_SUBSCRIBE_DESCRIPTOR.displayName)).toHaveLength(1);
  });
});

// --------------------------------------------------------------------------
// Descriptor identity: category and ports (Requirement 3.5)
// --------------------------------------------------------------------------

describe('trigger descriptor identity (Requirement 3.5)', () => {
  it.each([
    ['mqtt_subscribe', MQTT_SUBSCRIBE_DESCRIPTOR],
    ['opcua_subscribe', OPCUA_SUBSCRIBE_DESCRIPTOR],
  ])('%s has the trigger category, zero inputs, and one EventSignal out port', (typeId, d) => {
    expect(d.typeId).toBe(typeId);
    expect(d.category).toBe(CATEGORY_TRIGGER);
    expect(d.inputs).toEqual([]);
    expect(d.outputs).toEqual([{ name: 'out', portType: PORT_TYPE_EVENT_SIGNAL }]);
    expect(d.hardwareDependent).toBe(true);
  });
});

// --------------------------------------------------------------------------
// mqtt_subscribe connection parameters (mirroring mqtt_publish)
// (Requirement 3.5)
// --------------------------------------------------------------------------

describe('mqtt_subscribe connection parameters (Requirement 3.5)', () => {
  it('declares topic as a required string with min length 1', () => {
    const topic = parameter(MQTT_SUBSCRIBE_DESCRIPTOR, 'topic');
    expect(topic.paramType).toBe('string');
    expect(topic.required).toBe(true);
    expect(topic.constraints).toEqual({ minLength: 1 });
  });

  it('declares qos as an optional enum defaulting to 0 with values 0/1/2', () => {
    const qos = parameter(MQTT_SUBSCRIBE_DESCRIPTOR, 'qos');
    expect(qos.paramType).toBe('enum');
    expect(qos.required).toBe(false);
    expect(qos.default).toBe(0);
    expect(qos.constraints).toEqual({ values: [0, 1, 2] });
  });

  it.each(['greengrass', 'aws_iot'])(
    'declares %s as an optional bool defaulting to false',
    (name) => {
      const p = parameter(MQTT_SUBSCRIBE_DESCRIPTOR, name);
      expect(p.paramType).toBe('bool');
      expect(p.required).toBe(false);
      expect(p.default).toBe(false);
    }
  );

  it.each(IOT_PARAMETER_NAMES)(
    'declares %s as an optional string (min length 1) gated on aws_iot',
    (name) => {
      const p = parameter(MQTT_SUBSCRIBE_DESCRIPTOR, name);
      expect(p.paramType).toBe('string');
      expect(p.required).toBe(false);
      expect(p.constraints).toEqual({ minLength: 1 });
      expect(p.dependsOn).toBe('aws_iot');
    }
  );

  it('declares broker_host as an optional string with min length 1', () => {
    const host = parameter(MQTT_SUBSCRIBE_DESCRIPTOR, 'broker_host');
    expect(host.paramType).toBe('string');
    expect(host.required).toBe(false);
    expect(host.constraints).toEqual({ minLength: 1 });
  });

  it('declares broker_port as an optional int defaulting to 1883 within 1-65535', () => {
    const port = parameter(MQTT_SUBSCRIBE_DESCRIPTOR, 'broker_port');
    expect(port.paramType).toBe('int');
    expect(port.required).toBe(false);
    expect(port.default).toBe(1883);
    expect(port.constraints).toEqual({ min: 1, max: 65535 });
  });
});

// --------------------------------------------------------------------------
// opcua_subscribe endpoint, sampling, and mode parameters
// (Requirement 3.5)
// --------------------------------------------------------------------------

describe('opcua_subscribe parameters (Requirement 3.5)', () => {
  it('declares endpoint as a required opc.tcp string', () => {
    const endpoint = parameter(OPCUA_SUBSCRIBE_DESCRIPTOR, 'endpoint');
    expect(endpoint.paramType).toBe('string');
    expect(endpoint.required).toBe(true);
    expect(endpoint.constraints).toEqual({ minLength: 1, regex: '^opc\\.tcp://.+' });
  });

  it('declares node_id as a required string with min length 1', () => {
    const nodeId = parameter(OPCUA_SUBSCRIBE_DESCRIPTOR, 'node_id');
    expect(nodeId.paramType).toBe('string');
    expect(nodeId.required).toBe(true);
    expect(nodeId.constraints).toEqual({ minLength: 1 });
  });

  it('declares sampling_interval_ms as an optional int defaulting to 100 within 10-60000', () => {
    const sampling = parameter(OPCUA_SUBSCRIBE_DESCRIPTOR, 'sampling_interval_ms');
    expect(sampling.paramType).toBe('int');
    expect(sampling.required).toBe(false);
    expect(sampling.default).toBe(100);
    expect(sampling.constraints).toEqual({ min: 10, max: 60000 });
  });

  it('declares mode as an optional enum defaulting to subscribe with values subscribe/poll', () => {
    const mode = parameter(OPCUA_SUBSCRIBE_DESCRIPTOR, 'mode');
    expect(mode.paramType).toBe('enum');
    expect(mode.required).toBe(false);
    expect(mode.default).toBe('subscribe');
    expect(mode.constraints).toEqual({ values: ['subscribe', 'poll'] });
  });

  it('gates poll_interval_ms on the poll selection (int, default 500, 10-60000)', () => {
    const poll = parameter(OPCUA_SUBSCRIBE_DESCRIPTOR, 'poll_interval_ms');
    expect(poll.paramType).toBe('int');
    expect(poll.required).toBe(false);
    expect(poll.default).toBe(500);
    expect(poll.constraints).toEqual({ min: 10, max: 60000 });
    expect(poll.dependsOn).toBe('mode=poll');
  });

  it.each([
    'username',
    'password',
    'security_policy',
    'security_mode',
    'client_cert_path',
    'client_key_path',
    'server_cert_path',
  ])('declares the optional %s security parameter as an ungated string', (name) => {
    const p = parameter(OPCUA_SUBSCRIBE_DESCRIPTOR, name);
    expect(p.paramType).toBe('string');
    expect(p.required).toBe(false);
    expect(p.dependsOn ?? null).toBeNull();
  });
});

// --------------------------------------------------------------------------
// Shared policy parameter family (Requirement 3.5)
// --------------------------------------------------------------------------

describe('shared trigger policy family (Requirement 3.5)', () => {
  it.each([
    ['mqtt_subscribe', MQTT_SUBSCRIBE_DESCRIPTOR],
    ['opcua_subscribe', OPCUA_SUBSCRIBE_DESCRIPTOR],
  ])('%s declares the full policy family with the documented shapes', (_typeId, d) => {
    const policy = parameter(d, 'concurrency_policy');
    expect(policy.paramType).toBe('enum');
    expect(policy.required).toBe(false);
    expect(policy.default).toBe('queue');
    expect(policy.constraints).toEqual({ values: ['queue', 'drop', 'debounce'] });

    const queueDepth = parameter(d, 'queue_depth');
    expect(queueDepth.paramType).toBe('int');
    expect(queueDepth.default).toBe(10);
    expect(queueDepth.constraints).toEqual({ min: 1, max: 1000 });
    expect(queueDepth.dependsOn).toBe('concurrency_policy=queue');

    const debounceMs = parameter(d, 'debounce_ms');
    expect(debounceMs.paramType).toBe('int');
    expect(debounceMs.default).toBe(500);
    expect(debounceMs.constraints).toEqual({ min: 1, max: 60000 });
    expect(debounceMs.dependsOn).toBe('concurrency_policy=debounce');

    const retryLimit = parameter(d, 'retry_limit');
    expect(retryLimit.paramType).toBe('int');
    expect(retryLimit.default).toBe(0);
    expect(retryLimit.constraints).toEqual({ min: 0, max: 1000 });

    const priority = parameter(d, 'priority');
    expect(priority.paramType).toBe('int');
    expect(priority.default).toBe(100);
    expect(priority.constraints).toEqual({ min: 0, max: 1000 });
  });

  it('declares an identical policy family on both trigger descriptors', () => {
    for (const name of POLICY_PARAMETER_NAMES) {
      expect(parameter(MQTT_SUBSCRIBE_DESCRIPTOR, name)).toEqual(
        parameter(OPCUA_SUBSCRIBE_DESCRIPTOR, name)
      );
    }
  });
});

// --------------------------------------------------------------------------
// Config-panel gating: isParameterVisible semantics (Requirement 3.6)
// --------------------------------------------------------------------------

describe('policy family gating via isParameterVisible (Requirement 3.6)', () => {
  it('shows queue_depth only while concurrency_policy is queue (incl. the default)', () => {
    // Default (unset) is queue.
    expect(visible(MQTT_SUBSCRIBE_DESCRIPTOR, 'queue_depth', {})).toBe(true);
    expect(
      visible(MQTT_SUBSCRIBE_DESCRIPTOR, 'queue_depth', { concurrency_policy: 'queue' })
    ).toBe(true);
    expect(
      visible(MQTT_SUBSCRIBE_DESCRIPTOR, 'queue_depth', { concurrency_policy: 'drop' })
    ).toBe(false);
    expect(
      visible(MQTT_SUBSCRIBE_DESCRIPTOR, 'queue_depth', { concurrency_policy: 'debounce' })
    ).toBe(false);
  });

  it('shows debounce_ms only while concurrency_policy is debounce', () => {
    expect(visible(MQTT_SUBSCRIBE_DESCRIPTOR, 'debounce_ms', {})).toBe(false);
    expect(
      visible(MQTT_SUBSCRIBE_DESCRIPTOR, 'debounce_ms', { concurrency_policy: 'debounce' })
    ).toBe(true);
    expect(
      visible(MQTT_SUBSCRIBE_DESCRIPTOR, 'debounce_ms', { concurrency_policy: 'queue' })
    ).toBe(false);
    expect(
      visible(MQTT_SUBSCRIBE_DESCRIPTOR, 'debounce_ms', { concurrency_policy: 'drop' })
    ).toBe(false);
  });

  it('applies the same policy gating on opcua_subscribe', () => {
    expect(visible(OPCUA_SUBSCRIBE_DESCRIPTOR, 'queue_depth', {})).toBe(true);
    expect(
      visible(OPCUA_SUBSCRIBE_DESCRIPTOR, 'debounce_ms', { concurrency_policy: 'debounce' })
    ).toBe(true);
    expect(
      visible(OPCUA_SUBSCRIBE_DESCRIPTOR, 'queue_depth', { concurrency_policy: 'debounce' })
    ).toBe(false);
  });

  it('shows poll_interval_ms only while mode is poll (default subscribe hides it)', () => {
    expect(visible(OPCUA_SUBSCRIBE_DESCRIPTOR, 'poll_interval_ms', {})).toBe(false);
    expect(
      visible(OPCUA_SUBSCRIBE_DESCRIPTOR, 'poll_interval_ms', { mode: 'subscribe' })
    ).toBe(false);
    expect(visible(OPCUA_SUBSCRIBE_DESCRIPTOR, 'poll_interval_ms', { mode: 'poll' })).toBe(true);
  });

  it('always shows the ungated policy parameters', () => {
    for (const name of ['concurrency_policy', 'retry_limit', 'priority']) {
      expect(visible(MQTT_SUBSCRIBE_DESCRIPTOR, name, {})).toBe(true);
      expect(visible(MQTT_SUBSCRIBE_DESCRIPTOR, name, { concurrency_policy: 'drop' })).toBe(true);
    }
  });

  it('keeps the existing bool aws_iot gating unchanged on mqtt_subscribe', () => {
    for (const name of IOT_PARAMETER_NAMES) {
      expect(visible(MQTT_SUBSCRIBE_DESCRIPTOR, name, {})).toBe(false);
      expect(visible(MQTT_SUBSCRIBE_DESCRIPTOR, name, { aws_iot: false })).toBe(false);
      expect(visible(MQTT_SUBSCRIBE_DESCRIPTOR, name, { aws_iot: true })).toBe(true);
    }
  });
});

// --------------------------------------------------------------------------
// Config-panel gating: rendered NodeConfigPanel (Requirement 3.6)
// --------------------------------------------------------------------------

describe('NodeConfigPanel shows/hides the gated companions (Requirement 3.6)', () => {
  it('renders queue_depth and hides debounce_ms for a fresh mqtt_subscribe node (default queue)', () => {
    const { container } = render(
      <NodeConfigPanel node={builderNode(MQTT_SUBSCRIBE_DESCRIPTOR)} onParametersChange={vi.fn()} />
    );
    expect(container.querySelector('input[aria-label="queue_depth"]')).not.toBeNull();
    expect(container.querySelector('input[aria-label="debounce_ms"]')).toBeNull();
  });

  it('renders debounce_ms and hides queue_depth when concurrency_policy is debounce', () => {
    const { container } = render(
      <NodeConfigPanel
        node={builderNode(MQTT_SUBSCRIBE_DESCRIPTOR, { concurrency_policy: 'debounce' })}
        onParametersChange={vi.fn()}
      />
    );
    expect(container.querySelector('input[aria-label="debounce_ms"]')).not.toBeNull();
    expect(container.querySelector('input[aria-label="queue_depth"]')).toBeNull();
  });

  it('hides both gated companions when concurrency_policy is drop', () => {
    const { container } = render(
      <NodeConfigPanel
        node={builderNode(OPCUA_SUBSCRIBE_DESCRIPTOR, { concurrency_policy: 'drop' })}
        onParametersChange={vi.fn()}
      />
    );
    expect(container.querySelector('input[aria-label="queue_depth"]')).toBeNull();
    expect(container.querySelector('input[aria-label="debounce_ms"]')).toBeNull();
  });

  it('hides poll_interval_ms in the default subscribe mode and shows it under poll', () => {
    const subscribe = render(
      <NodeConfigPanel
        node={builderNode(OPCUA_SUBSCRIBE_DESCRIPTOR)}
        onParametersChange={vi.fn()}
      />
    );
    expect(
      subscribe.container.querySelector('input[aria-label="poll_interval_ms"]')
    ).toBeNull();
    subscribe.unmount();

    const poll = render(
      <NodeConfigPanel
        node={builderNode(OPCUA_SUBSCRIBE_DESCRIPTOR, { mode: 'poll' })}
        onParametersChange={vi.fn()}
      />
    );
    expect(
      poll.container.querySelector('input[aria-label="poll_interval_ms"]')
    ).not.toBeNull();
  });

  it('keeps the existing aws_iot bool gating: iot_* fields appear only when checked', () => {
    const unchecked = render(
      <NodeConfigPanel
        node={builderNode(MQTT_SUBSCRIBE_DESCRIPTOR)}
        onParametersChange={vi.fn()}
      />
    );
    for (const name of IOT_PARAMETER_NAMES) {
      expect(unchecked.container.querySelector(`input[aria-label="${name}"]`)).toBeNull();
    }
    unchecked.unmount();

    const checked = render(
      <NodeConfigPanel
        node={builderNode(MQTT_SUBSCRIBE_DESCRIPTOR, { aws_iot: true })}
        onParametersChange={vi.fn()}
      />
    );
    for (const name of IOT_PARAMETER_NAMES) {
      expect(checked.container.querySelector(`input[aria-label="${name}"]`)).not.toBeNull();
    }
  });
});
