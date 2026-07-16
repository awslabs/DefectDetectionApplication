/**
 * Component tests for the node configuration panel (Requirements 1.7,
 * 1.8, 2.6, 2.7): form controls per parameter type with current values,
 * inline validation errors from the constraint predicate, parameter
 * update propagation, the model_ref select populated from the model
 * registry filtered by Use_Case, and the Custom_Python_Node code editor
 * plus port-type pickers.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import NodeConfigPanel, { cameraOption } from './NodeConfigPanel';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from './builderGraph';
import type { CameraSourceEntry } from './cameraReference';
import { PORT_TYPES, type JsonValue, type NodeTypeDescriptor } from './types';

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

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const CAMERA: NodeTypeDescriptor = {
  typeId: 'camera_source',
  category: 'input',
  displayName: 'Camera source',
  inputs: [],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [
    { name: 'device', paramType: 'string', required: true, default: null, constraints: {} },
    {
      name: 'gain',
      paramType: 'int',
      required: false,
      default: 4,
      constraints: { min: 0, max: 100 },
    },
    { name: 'flip', paramType: 'bool', required: false, default: false, constraints: {} },
    {
      name: 'mode',
      paramType: 'enum',
      required: true,
      default: 'auto',
      constraints: { values: ['auto', 'manual'] },
    },
  ],
  mappings: [],
  hardwareDependent: true,
};

const MODEL_INFERENCE: NodeTypeDescriptor = {
  typeId: 'model_inference',
  category: 'inference',
  displayName: 'Model inference',
  inputs: [{ name: 'in', portType: 'VideoFrames' }],
  outputs: [{ name: 'out', portType: 'InferenceMeta' }],
  parameters: [
    { name: 'modelName', paramType: 'model_ref', required: true, default: null, constraints: {} },
  ],
  mappings: [],
  hardwareDependent: false,
};

const MQTT_PUBLISH: NodeTypeDescriptor = {
  typeId: 'mqtt_publish',
  category: 'output',
  displayName: 'MQTT Publish',
  inputs: [{ name: 'in', portType: 'InferenceMeta' }],
  outputs: [],
  parameters: [
    { name: 'broker_host', paramType: 'string', required: true, default: null, constraints: {} },
    { name: 'topic', paramType: 'string', required: true, default: null, constraints: {} },
    {
      name: 'payload_template',
      paramType: 'string',
      required: false,
      default: '{inference_json}',
      constraints: {},
    },
    { name: 'aws_iot', paramType: 'bool', required: false, default: false, constraints: {} },
    {
      name: 'iot_thing_name',
      paramType: 'string',
      required: false,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: 'aws_iot',
    },
    {
      name: 'iot_ca_cert_path',
      paramType: 'string',
      required: false,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: 'aws_iot',
    },
    {
      name: 'iot_client_cert_path',
      paramType: 'string',
      required: false,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: 'aws_iot',
    },
    {
      name: 'iot_private_key_path',
      paramType: 'string',
      required: false,
      default: null,
      constraints: { minLength: 1 },
      dependsOn: 'aws_iot',
    },
  ],
  mappings: [],
  hardwareDependent: true,
};

const IOT_PARAMETER_NAMES = [
  'iot_thing_name',
  'iot_ca_cert_path',
  'iot_client_cert_path',
  'iot_private_key_path',
];

const CUSTOM_PYTHON: NodeTypeDescriptor = {
  typeId: 'custom_python',
  category: 'post_processing',
  displayName: 'Custom Python',
  inputs: [{ name: 'in', portType: 'VideoFrames' }],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [
    { name: 'code', paramType: 'code', required: true, default: null, constraints: {} },
    {
      name: 'input_port_type',
      paramType: 'enum',
      required: true,
      default: 'VideoFrames',
      constraints: {},
    },
    {
      name: 'output_port_type',
      paramType: 'enum',
      required: true,
      default: 'VideoFrames',
      constraints: {},
    },
  ],
  mappings: [],
  hardwareDependent: false,
};

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
// Tests
// --------------------------------------------------------------------------

describe('NodeConfigPanel', () => {
  it('renders nothing when no node is selected', () => {
    const { container } = render(<NodeConfigPanel node={null} onParametersChange={vi.fn()} />);
    expect(container.querySelector('aside')).toBeNull();
  });

  it('shows a compact header (no heading element) with a close button', () => {
    const onClose = vi.fn();
    const node = builderNode(CAMERA, { device: '/dev/video0', mode: 'auto' });
    render(<NodeConfigPanel node={node} onParametersChange={vi.fn()} onClose={onClose} />);

    // The title renders as compact text, not a large heading.
    expect(screen.getByText('Camera source')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Camera source' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Close node configuration' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('omits the close button when onClose is not provided', () => {
    render(<NodeConfigPanel node={builderNode(CAMERA)} onParametersChange={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Close node configuration' })).toBeNull();
  });

  it('renders a form control per parameter type with the current values (Requirement 1.7)', () => {
    const node = builderNode(CAMERA, {
      device: '/dev/video0',
      gain: 7,
      flip: true,
      mode: 'manual',
    });
    const { container } = render(<NodeConfigPanel node={node} onParametersChange={vi.fn()} />);

    expect(screen.getByRole('complementary', { name: 'Node configuration' })).toBeInTheDocument();
    expect(screen.getByText('Camera source')).toBeInTheDocument();

    const deviceInput = container.querySelector('input[aria-label="device"]');
    expect(deviceInput).toHaveValue('/dev/video0');

    const gainInput = container.querySelector('input[aria-label="gain"]');
    expect(gainInput).toHaveValue(7);

    const toggle = container.querySelector('input[type="checkbox"]');
    expect(toggle).toBeChecked();

    const select = createWrapper(container).findSelect();
    expect(select!.findTrigger().getElement().textContent).toContain('manual');
  });

  it('shows the declared default as the current value when the parameter is unset', () => {
    const node = builderNode(CAMERA, { device: '/dev/video0' });
    const { container } = render(<NodeConfigPanel node={node} onParametersChange={vi.fn()} />);
    const gainInput = container.querySelector('input[aria-label="gain"]');
    expect(gainInput).toHaveValue(4);
  });

  it('displays inline validation errors from the constraint predicate (Requirement 1.8)', () => {
    const node = builderNode(CAMERA, { gain: 500, mode: 'auto' });
    render(<NodeConfigPanel node={node} onParametersChange={vi.fn()} />);

    // Required string with no value.
    expect(screen.getByText("Required parameter 'device' has no value")).toBeInTheDocument();
    // Int above its declared maximum.
    expect(
      screen.getByText("Parameter 'gain' value 500 is above the maximum 100")
    ).toBeInTheDocument();
  });

  it('propagates edits through onParametersChange', () => {
    const onParametersChange = vi.fn();
    const node = builderNode(CAMERA, { device: '/dev/video0', mode: 'auto' });
    const { container } = render(
      <NodeConfigPanel node={node} onParametersChange={onParametersChange} />
    );

    const deviceInput = container.querySelector('input[aria-label="device"]')!;
    fireEvent.change(deviceInput, { target: { value: '/dev/video1' } });
    expect(onParametersChange).toHaveBeenCalledWith('camera_source_1', {
      device: '/dev/video1',
      mode: 'auto',
    });

    const gainInput = container.querySelector('input[aria-label="gain"]')!;
    fireEvent.change(gainInput, { target: { value: '12' } });
    expect(onParametersChange).toHaveBeenCalledWith('camera_source_1', {
      device: '/dev/video0',
      gain: 12,
      mode: 'auto',
    });
  });

  it('populates the model_ref select from the model registry filtered by Use_Case (Requirement 2.6)', async () => {
    listModels.mockResolvedValue({
      models: [
        { name: 'widget-anomaly-v3', version: '3', stage: 'production' },
        { name: 'widget-classifier', version: '1', stage: 'staging' },
      ],
      count: 2,
      usecase_id: 'uc-1',
    });
    const onParametersChange = vi.fn();
    const node = builderNode(MODEL_INFERENCE);
    const { container } = render(
      <NodeConfigPanel node={node} onParametersChange={onParametersChange} />
    );

    await waitFor(() => expect(listModels).toHaveBeenCalledWith({ usecase_id: 'uc-1' }));

    const select = createWrapper(container).findSelect()!;
    await waitFor(() => {
      select.openDropdown();
      expect(select.findDropdown().findOptions()).toHaveLength(2);
    });

    expect(select.findDropdown().getElement().textContent).toContain('widget-anomaly-v3');
    expect(select.findDropdown().getElement().textContent).toContain('widget-classifier');

    select.selectOptionByValue('widget-anomaly-v3');
    expect(onParametersChange).toHaveBeenCalledWith('model_inference_1', {
      modelName: 'widget-anomaly-v3',
    });
  });

  it('does not query the model registry for nodes without model_ref parameters', () => {
    render(<NodeConfigPanel node={builderNode(CAMERA)} onParametersChange={vi.fn()} />);
    expect(listModels).not.toHaveBeenCalled();
  });

  it('renders the Custom_Python_Node code editor and port-type pickers (Requirement 2.7)', () => {
    const onParametersChange = vi.fn();
    const node = builderNode(CUSTOM_PYTHON, {
      code: 'def handle(frame): return frame',
      input_port_type: 'VideoFrames',
      output_port_type: 'VideoFrames',
    });
    const { container } = render(
      <NodeConfigPanel node={node} onParametersChange={onParametersChange} />
    );

    // Code editor with the current code.
    const codeEditor = container.querySelector('textarea[aria-label="code"]');
    expect(codeEditor).toHaveValue('def handle(frame): return frame');
    fireEvent.change(codeEditor!, { target: { value: 'def handle(frame): return None' } });
    expect(onParametersChange).toHaveBeenCalledWith(
      'custom_python_1',
      expect.objectContaining({ code: 'def handle(frame): return None' })
    );

    // Port-type pickers offer the declared port types (PORT_TYPES).
    const selects = createWrapper(container).findAllSelects();
    expect(selects).toHaveLength(2);

    const outputPicker = selects[1];
    outputPicker.openDropdown();
    const optionLabels = outputPicker
      .findDropdown()
      .findOptions()
      .map((option) => option.getElement().textContent);
    expect(optionLabels).toEqual([...PORT_TYPES]);

    outputPicker.selectOption(2); // InferenceMeta
    expect(onParametersChange).toHaveBeenCalledWith(
      'custom_python_1',
      expect.objectContaining({ output_port_type: 'InferenceMeta' })
    );
  });

  describe('mqtt_publish AWS IoT support (dependsOn visibility)', () => {
    it('renders the "AWS IoT support" checkbox', () => {
      const { container } = render(
        <NodeConfigPanel node={builderNode(MQTT_PUBLISH)} onParametersChange={vi.fn()} />
      );
      expect(screen.getByText('AWS IoT support')).toBeInTheDocument();
      const checkbox = createWrapper(container).findCheckbox();
      expect(checkbox).not.toBeNull();
      expect(checkbox!.findNativeInput().getElement()).not.toBeChecked();
    });

    it('hides the iot_* fields while aws_iot is unchecked (default)', () => {
      const { container } = render(
        <NodeConfigPanel node={builderNode(MQTT_PUBLISH)} onParametersChange={vi.fn()} />
      );
      for (const name of IOT_PARAMETER_NAMES) {
        expect(container.querySelector(`input[aria-label="${name}"]`)).toBeNull();
      }
      expect(screen.queryByText('IoT thing name')).toBeNull();
    });

    it('shows the iot_* fields with their labels when aws_iot is checked', () => {
      const { container } = render(
        <NodeConfigPanel
          node={builderNode(MQTT_PUBLISH, { aws_iot: true })}
          onParametersChange={vi.fn()}
        />
      );
      for (const name of IOT_PARAMETER_NAMES) {
        expect(container.querySelector(`input[aria-label="${name}"]`)).not.toBeNull();
      }
      expect(screen.getByText('IoT thing name')).toBeInTheDocument();
      expect(screen.getByText('Root CA certificate path (on device)')).toBeInTheDocument();
      expect(screen.getByText('Client certificate path (on device)')).toBeInTheDocument();
      expect(screen.getByText('Private key path (on device)')).toBeInTheDocument();
    });

    it('propagates checking the checkbox as aws_iot: true', () => {
      const onParametersChange = vi.fn();
      const { container } = render(
        <NodeConfigPanel node={builderNode(MQTT_PUBLISH)} onParametersChange={onParametersChange} />
      );
      const checkbox = createWrapper(container).findCheckbox()!;
      fireEvent.click(checkbox.findNativeInput().getElement());
      expect(onParametersChange).toHaveBeenCalledWith('mqtt_publish_1', { aws_iot: true });
    });
  });

  describe('per-parameter help (PARAMETER_HELP)', () => {
    const INFERENCE_FILTER: NodeTypeDescriptor = {
      typeId: 'inference_filter',
      category: 'post_processing',
      displayName: 'Inference Filter',
      inputs: [{ name: 'in', portType: 'InferenceMeta' }],
      outputs: [{ name: 'out', portType: 'InferenceMeta' }],
      parameters: [
        { name: 'condition', paramType: 'string', required: true, default: null, constraints: {} },
      ],
      mappings: [],
      hardwareDependent: false,
    };

    const CONDITIONAL: NodeTypeDescriptor = {
      typeId: 'conditional',
      category: 'post_processing',
      displayName: 'Conditional',
      inputs: [{ name: 'in', portType: 'InferenceMeta' }],
      outputs: [
        { name: 'true', portType: 'InferenceMeta' },
        { name: 'false', portType: 'InferenceMeta' },
      ],
      parameters: [
        { name: 'condition', paramType: 'string', required: true, default: null, constraints: {} },
      ],
      mappings: [],
      hardwareDependent: false,
    };

    it('documents the inference_filter condition grammar with examples', () => {
      render(
        <NodeConfigPanel node={builderNode(INFERENCE_FILTER)} onParametersChange={vi.fn()} />
      );

      // The description documents the metadata fields and operators the
      // condition evaluator actually supports.
      const description = screen.getByText(/is_anomalous/, { selector: 'div, span' });
      expect(description.textContent).toContain('confidence');
      expect(description.textContent).toContain('==');
      expect(description.textContent).toContain('&&');
      expect(description.textContent).toContain('||');
      expect(description.textContent).toContain('!');

      // Working examples in the expandable "Examples" section.
      fireEvent.click(screen.getByText('Show examples'));
      const examples = screen.getByRole('list', { name: 'Examples for condition' });
      expect(examples.textContent).toContain('is_anomalous == true');
      expect(examples.textContent).toContain('is_anomalous == true && confidence >= 0.8');
    });

    it('documents the conditional condition two-path routing with examples', () => {
      render(<NodeConfigPanel node={builderNode(CONDITIONAL)} onParametersChange={vi.fn()} />);

      // The description explains which output receives the metadata.
      expect(screen.getByText(/"true" output receives the metadata/)).toBeInTheDocument();

      fireEvent.click(screen.getByText('Show examples'));
      const examples = screen.getByRole('list', { name: 'Examples for condition' });
      expect(examples.textContent).toContain('is_anomalous == true');
      expect(examples.textContent).toContain('"true" path');
    });

    it('documents mqtt_publish payload_template placeholders', () => {
      render(
        <NodeConfigPanel
          node={builderNode(MQTT_PUBLISH, {
            broker_host: 'b',
            topic: 't',
            payload_template: '{inference_json}',
          })}
          onParametersChange={vi.fn()}
        />
      );

      // The description names the supported placeholders.
      expect(
        screen.getByText(/Placeholders in curly braces are replaced/)
      ).toBeInTheDocument();
      fireEvent.click(screen.getByText('Show examples'));
      const examples = screen.getByRole('list', {
        name: 'Examples for payload_template',
      });
      expect(examples.textContent).toContain('{inference_json}');
    });

    it('renders no help for parameters without a PARAMETER_HELP entry', () => {
      render(<NodeConfigPanel node={builderNode(CAMERA)} onParametersChange={vi.fn()} />);
      expect(screen.queryByText('Show examples')).toBeNull();
    });
  });

  describe('catalog-served parameter descriptions', () => {
    it('renders the descriptor description as the field help under the label', () => {
      const withDescription: NodeTypeDescriptor = {
        ...CAMERA,
        parameters: [
          {
            name: 'device',
            paramType: 'string',
            required: true,
            default: null,
            constraints: {},
            description: 'Camera device path on the edge device, e.g. /dev/video0.',
          },
        ],
      };
      render(<NodeConfigPanel node={builderNode(withDescription)} onParametersChange={vi.fn()} />);
      expect(
        screen.getByText('Camera device path on the edge device, e.g. /dev/video0.')
      ).toBeInTheDocument();
    });

    it('prefers the catalog description over the PARAMETER_HELP fallback', () => {
      const conditionWithCatalogDescription: NodeTypeDescriptor = {
        typeId: 'inference_filter',
        category: 'post_processing',
        displayName: 'Inference Filter',
        inputs: [{ name: 'in', portType: 'InferenceMeta' }],
        outputs: [{ name: 'out', portType: 'InferenceMeta' }],
        parameters: [
          {
            name: 'condition',
            paramType: 'string',
            required: true,
            default: null,
            constraints: {},
            description: 'Catalog-served condition help.',
          },
        ],
        mappings: [],
        hardwareDependent: false,
      };
      render(
        <NodeConfigPanel
          node={builderNode(conditionWithCatalogDescription)}
          onParametersChange={vi.fn()}
        />
      );
      expect(screen.getByText('Catalog-served condition help.')).toBeInTheDocument();
      // The "Show examples" section still comes from PARAMETER_HELP.
      fireEvent.click(screen.getByText('Show examples'));
      expect(
        screen.getByRole('list', { name: 'Examples for condition' })
      ).toBeInTheDocument();
    });

    it('renders bool parameter descriptions below the checkbox', () => {
      const withBoolDescription: NodeTypeDescriptor = {
        ...MQTT_PUBLISH,
        parameters: [
          {
            name: 'aws_iot',
            paramType: 'bool',
            required: false,
            default: false,
            constraints: {},
            description: 'Publish through AWS IoT Core instead of a plain broker.',
          },
        ],
      };
      const { container } = render(
        <NodeConfigPanel node={builderNode(withBoolDescription)} onParametersChange={vi.fn()} />
      );
      const description = screen.getByText(
        'Publish through AWS IoT Core instead of a plain broker.'
      );
      expect(description).toBeInTheDocument();
      // The description follows the checkbox in document order.
      const checkbox = container.querySelector('input[type="checkbox"]')!;
      expect(
        checkbox.compareDocumentPosition(description) & Node.DOCUMENT_POSITION_FOLLOWING
      ).toBeTruthy();
    });

    it('renders long descriptions fully inside a collapsible "Syntax help" section', () => {
      const longDescription =
        'An expression over the inference metadata fields is_anomalous and ' +
        'confidence evaluated by the workflow engine on every result. ' +
        'Supports the comparisons ==, !=, >=, <=, >, <, the logic operators ' +
        '&& (and), || (or), ! (not), and parentheses; a bare field name is ' +
        'tested for truth. The expression must be valid before the workflow can run.';
      expect(longDescription.length).toBeGreaterThan(200);
      const withLongDescription: NodeTypeDescriptor = {
        typeId: 'inference_filter',
        category: 'post_processing',
        displayName: 'Inference Filter',
        inputs: [{ name: 'in', portType: 'InferenceMeta' }],
        outputs: [{ name: 'out', portType: 'InferenceMeta' }],
        parameters: [
          {
            name: 'condition',
            paramType: 'string',
            required: true,
            default: null,
            constraints: {},
            description: longDescription,
          },
        ],
        mappings: [],
        hardwareDependent: false,
      };
      render(
        <NodeConfigPanel node={builderNode(withLongDescription)} onParametersChange={vi.fn()} />
      );
      // Collapsible section instead of the always-visible field description.
      fireEvent.click(screen.getByText('Syntax help'));
      expect(screen.getByText(longDescription)).toBeInTheDocument();
    });
  });

  describe('catalog-served parameter examples (clickable chips)', () => {
    const DEVICE_WITH_EXAMPLES: NodeTypeDescriptor = {
      ...CAMERA,
      parameters: [
        {
          name: 'device',
          paramType: 'string',
          required: true,
          default: null,
          constraints: {},
          examples: ['/dev/video0', '/dev/video1'],
        },
      ],
    };

    it('renders an "Examples:" row of chips for catalog-served examples', () => {
      render(
        <NodeConfigPanel node={builderNode(DEVICE_WITH_EXAMPLES)} onParametersChange={vi.fn()} />
      );
      const group = screen.getByRole('group', { name: 'Examples for device' });
      expect(group.textContent).toContain('Examples:');
      expect(screen.getByRole('button', { name: 'Use example /dev/video0' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Use example /dev/video1' })).toBeInTheDocument();
    });

    it('fills the field with the example value when a chip is clicked', () => {
      const onParametersChange = vi.fn();
      render(
        <NodeConfigPanel
          node={builderNode(DEVICE_WITH_EXAMPLES)}
          onParametersChange={onParametersChange}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: 'Use example /dev/video1' }));
      expect(onParametersChange).toHaveBeenCalledWith('camera_source_1', {
        device: '/dev/video1',
      });
    });

    it('truncates long example chips for display but inserts the full value', () => {
      const longExample =
        'is_anomalous == true && confidence >= 0.8 && !(confidence >= 0.99)';
      const withLongExample: NodeTypeDescriptor = {
        typeId: 'inference_filter',
        category: 'post_processing',
        displayName: 'Inference Filter',
        inputs: [{ name: 'in', portType: 'InferenceMeta' }],
        outputs: [{ name: 'out', portType: 'InferenceMeta' }],
        parameters: [
          {
            name: 'condition',
            paramType: 'string',
            required: true,
            default: null,
            constraints: {},
            examples: [longExample],
          },
        ],
        mappings: [],
        hardwareDependent: false,
      };
      const onParametersChange = vi.fn();
      render(
        <NodeConfigPanel node={builderNode(withLongExample)} onParametersChange={onParametersChange} />
      );

      const chip = screen.getByRole('button', { name: `Use example ${longExample}` });
      // Display text is truncated with an ellipsis; the full value is in
      // the accessible name and title.
      expect(chip.textContent!.length).toBeLessThan(longExample.length);
      expect(chip.textContent!.endsWith('\u2026')).toBe(true);
      expect(chip).toHaveAttribute('title', longExample);

      fireEvent.click(chip);
      expect(onParametersChange).toHaveBeenCalledWith('inference_filter_1', {
        condition: longExample,
      });
    });

    it('renders no examples row for parameters without catalog examples', () => {
      render(<NodeConfigPanel node={builderNode(CAMERA)} onParametersChange={vi.fn()} />);
      expect(screen.queryByRole('group', { name: /Examples for/ })).toBeNull();
    });
  });

  // ------------------------------------------------------------------------
  // Camera reference control (camera-registry-sync task 9.3,
  // Requirements 7.1, 7.3, 7.4, 7.5)
  // ------------------------------------------------------------------------

  describe('camera reference control (camera-registry-sync)', () => {
    /** camera_source with only the device parameter, so the picker's two
     * selects (reference device + camera) are the only selects rendered. */
    const CAMERA_DEVICE_ONLY: NodeTypeDescriptor = {
      ...CAMERA,
      parameters: [
        { name: 'device', paramType: 'string', required: true, default: null, constraints: {} },
      ],
    };

    const DEVICES = [
      { device_id: 'dev-1', usecase_id: 'uc-1', thing_name: 'edge-thing-1', status: 'HEALTHY' },
      { device_id: 'dev-2', usecase_id: 'uc-1', thing_name: 'edge-thing-2', status: 'UNHEALTHY' },
    ];

    const REGISTRY_CAMERAS: CameraSourceEntry[] = [
      {
        camera_source_id: 'cfg-a1b2',
        name: 'Line 1 inspection cam',
        type: 'Camera',
        params: { devicePath: '/dev/video2', gain: 8, exposure: 16000000 },
        origin: 'edge-configured',
        sync_status: 'synced',
        stale: false,
        absent: false,
      },
      {
        camera_source_id: 'disc-9f',
        name: 'Rear dock cam',
        type: 'Camera',
        params: { devicePath: '/dev/video5' },
        origin: 'edge-discovered',
        sync_status: 'synced',
        stale: true,
        absent: false,
      },
    ];

    /** A camera_source node with the device parameter unset (and no
     * hand-typed value), so the control starts on the reference picker. */
    function pickerNode(advisoryData?: Record<string, JsonValue>): BuilderNode {
      const node = builderNode(CAMERA_DEVICE_ONLY);
      if (advisoryData !== undefined) {
        node.data = { ...node.data, advisoryData };
      }
      return node;
    }

    beforeEach(() => {
      listDevices.mockResolvedValue({ devices: DEVICES, count: DEVICES.length });
      getDeviceCameras.mockResolvedValue({
        device_id: 'dev-1',
        state: 'synced',
        cameras: REGISTRY_CAMERAS,
        count: REGISTRY_CAMERAS.length,
      });
    });

    describe('cameraOption display fields (Requirement 7.4)', () => {
      it('shows name, device path, type, and sync status', () => {
        const option = cameraOption(REGISTRY_CAMERAS[0]);
        expect(option.value).toBe('cfg-a1b2');
        expect(option.label).toBe('Line 1 inspection cam');
        expect(option.description).toBe('/dev/video2');
        expect(option.tags).toEqual(['Camera', 'synced']);
        // Non-stale sources carry no staleness badge.
        expect(option.labelTag).toBeUndefined();
      });

      it('badges stale sources and tags absent ones', () => {
        expect(cameraOption(REGISTRY_CAMERAS[1]).labelTag).toBe('Stale');
        const absent = cameraOption({ ...REGISTRY_CAMERAS[1], absent: true });
        expect(absent.tags).toContain('absent');
      });

      it('falls back to the id for nameless sources and the url for pathless ones', () => {
        const rtsp = cameraOption({
          camera_source_id: 'cfg-rtsp',
          params: { url: 'rtsp://10.0.0.5/stream' },
        });
        expect(rtsp.label).toBe('cfg-rtsp');
        expect(rtsp.description).toBe('rtsp://10.0.0.5/stream');
      });
    });

    it('populates the device selector from the use case devices and the camera dropdown from the registry (Requirement 7.1)', async () => {
      const { container } = render(
        <NodeConfigPanel node={pickerNode()} onParametersChange={vi.fn()} />
      );

      await waitFor(() => expect(listDevices).toHaveBeenCalledWith('uc-1'));

      const [deviceSelect, cameraSelect] = createWrapper(container).findAllSelects();
      await waitFor(() => {
        deviceSelect.openDropdown();
        expect(deviceSelect.findDropdown().findOptions()).toHaveLength(2);
      });
      expect(deviceSelect.findDropdown().getElement().textContent).toContain('edge-thing-1');
      expect(deviceSelect.findDropdown().getElement().textContent).toContain('edge-thing-2');

      // The camera dropdown stays disabled until a device is chosen.
      expect(cameraSelect.isDisabled()).toBe(true);

      deviceSelect.selectOptionByValue('dev-1');
      await waitFor(() => expect(getDeviceCameras).toHaveBeenCalledWith('dev-1', 'uc-1'));

      await waitFor(() => {
        cameraSelect.openDropdown();
        expect(cameraSelect.findDropdown().findOptions()).toHaveLength(2);
      });
      // Each option shows the source's name, path, type, and sync status.
      const dropdownText = cameraSelect.findDropdown().getElement().textContent!;
      expect(dropdownText).toContain('Line 1 inspection cam');
      expect(dropdownText).toContain('/dev/video2');
      expect(dropdownText).toContain('Camera');
      expect(dropdownText).toContain('synced');
    });

    it('renders the staleness badge on stale cameras in the dropdown (Requirement 7.4)', async () => {
      const { container } = render(
        <NodeConfigPanel node={pickerNode()} onParametersChange={vi.fn()} />
      );
      await waitFor(() => expect(listDevices).toHaveBeenCalled());

      const [deviceSelect, cameraSelect] = createWrapper(container).findAllSelects();
      await waitFor(() => {
        deviceSelect.openDropdown();
        expect(deviceSelect.findDropdown().findOptions()).toHaveLength(2);
      });
      deviceSelect.selectOptionByValue('dev-1');

      await waitFor(() => {
        cameraSelect.openDropdown();
        expect(cameraSelect.findDropdown().findOptions()).toHaveLength(2);
      });
      const options = cameraSelect.findDropdown().findOptions();
      // The stale source (disc-9f) carries the badge; the fresh one does not.
      expect(options[1].getElement().textContent).toContain('Stale');
      expect(options[0].getElement().textContent).not.toContain('Stale');
    });

    describe('manual entry toggle (Requirement 7.3)', () => {
      it('switches from the picker to the plain text input and back', () => {
        const onParametersChange = vi.fn();
        const { container } = render(
          <NodeConfigPanel node={pickerNode()} onParametersChange={onParametersChange} />
        );

        // Starts on the reference picker: two selects, no plain input.
        expect(createWrapper(container).findAllSelects()).toHaveLength(2);
        expect(container.querySelector('input[aria-label="device"]')).toBeNull();

        // Toggling manual entry shows the plain text input.
        const toggle = container.querySelector(
          'input[aria-label="Manual entry for device"]'
        )!;
        fireEvent.click(toggle);
        const deviceInput = container.querySelector('input[aria-label="device"]')!;
        expect(deviceInput).toBeInTheDocument();
        expect(createWrapper(container).findAllSelects()).toHaveLength(0);

        // Typed values propagate as ordinary parameter edits.
        fireEvent.change(deviceInput, { target: { value: '/dev/video9' } });
        expect(onParametersChange).toHaveBeenCalledWith('camera_source_1', {
          device: '/dev/video9',
        });

        // Toggling back restores the picker.
        fireEvent.click(toggle);
        expect(container.querySelector('input[aria-label="device"]')).toBeNull();
        expect(createWrapper(container).findAllSelects()).toHaveLength(2);
      });

      it('starts in manual mode for a hand-typed device value without a hint', () => {
        const node = builderNode(CAMERA_DEVICE_ONLY, { device: '/dev/video7' });
        const { container } = render(
          <NodeConfigPanel node={node} onParametersChange={vi.fn()} />
        );
        expect(container.querySelector('input[aria-label="device"]')).toHaveValue(
          '/dev/video7'
        );
        expect(createWrapper(container).findAllSelects()).toHaveLength(0);
      });
    });

    describe('binding hint persistence (Requirement 7.5)', () => {
      const HINT = {
        cameraSourceId: 'cfg-a1b2',
        cameraName: 'Line 1 inspection cam',
        sourceDeviceId: 'dev-1',
      };

      it('reports the selection through onCameraSelection with the updated parameters and the hint', async () => {
        const onParametersChange = vi.fn();
        const onCameraSelection = vi.fn();
        const { container } = render(
          <NodeConfigPanel
            node={pickerNode()}
            onParametersChange={onParametersChange}
            onCameraSelection={onCameraSelection}
          />
        );
        await waitFor(() => expect(listDevices).toHaveBeenCalled());

        const [deviceSelect, cameraSelect] = createWrapper(container).findAllSelects();
        await waitFor(() => {
          deviceSelect.openDropdown();
          expect(deviceSelect.findDropdown().findOptions()).toHaveLength(2);
        });
        deviceSelect.selectOptionByValue('dev-1');
        await waitFor(() => {
          cameraSelect.openDropdown();
          expect(cameraSelect.findDropdown().findOptions()).toHaveLength(2);
        });
        cameraSelect.selectOptionByValue('cfg-a1b2');

        // The selection populates the node's parameters from the source
        // and records the advisory hint; plain parameter edits are not
        // routed through onParametersChange.
        expect(onCameraSelection).toHaveBeenCalledWith(
          'camera_source_1',
          { device: '/dev/video2', gain: 8, exposure: 16000000 },
          HINT
        );
        expect(onParametersChange).not.toHaveBeenCalled();
      });

      it('reads the hint back from node.data.advisoryData: picker pre-selected and link shown', async () => {
        // A hand-set device value plus a hint starts on the picker (not
        // manual entry), pre-selected to the hint's device and camera.
        const node = builderNode(CAMERA_DEVICE_ONLY, { device: '/dev/video2' });
        node.data = { ...node.data, advisoryData: { cameraBindingHint: HINT } };
        const { container } = render(
          <NodeConfigPanel node={node} onParametersChange={vi.fn()} />
        );

        expect(container.querySelector('input[aria-label="device"]')).toBeNull();
        expect(
          screen.getByText(/linked to Line 1 inspection cam on dev-1/)
        ).toBeInTheDocument();

        // The hint's device drives the camera fetch without user input.
        await waitFor(() => expect(getDeviceCameras).toHaveBeenCalledWith('dev-1', 'uc-1'));

        const [deviceSelect, cameraSelect] = createWrapper(container).findAllSelects();
        await waitFor(() =>
          expect(deviceSelect.findTrigger().getElement().textContent).toContain('edge-thing-1')
        );
        await waitFor(() =>
          expect(cameraSelect.findTrigger().getElement().textContent).toContain(
            'Line 1 inspection cam'
          )
        );
      });
    });
  });
});
