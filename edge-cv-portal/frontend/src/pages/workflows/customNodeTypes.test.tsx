/**
 * Component tests for Custom_Node_Type display in the Workflow_Builder
 * (custom-node-designer task 12.7, Requirements 8.3, 8.4, 9.6): custom
 * types appear in the Node_Palette under their declared category (8.3),
 * test-state entries carry a visible test-state marker (9.6), and the
 * node configuration panel provides the same behavior as for built-in
 * types — parameter validation, field-level descriptions, and example
 * values (8.4).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import NodePalette from './NodePalette';
import NodeConfigPanel from './NodeConfigPanel';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from './builderGraph';
import type { JsonValue, NodeTypeDescriptor } from './types';

const { useUsecaseMock } = vi.hoisted(() => ({
  useUsecaseMock: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: { listModels: vi.fn() },
}));

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: useUsecaseMock,
}));

// --------------------------------------------------------------------------
// Fixtures: one built-in type plus a registered Custom_Node_Type as the
// merged catalog serves them (custom entries carry lifecycleState when
// their backing Plugin_Record is in the test state).
// --------------------------------------------------------------------------

const CAMERA: NodeTypeDescriptor = {
  typeId: 'camera_source',
  category: 'input',
  displayName: 'Camera source',
  inputs: [],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [],
  mappings: [],
  hardwareDependent: false,
};

const BLUR_REGIONS: NodeTypeDescriptor = {
  typeId: 'custom.blur_regions',
  category: 'preprocessing',
  displayName: 'Blur Regions',
  inputs: [{ name: 'in', portType: 'VideoFrames' }],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [
    {
      name: 'radius',
      paramType: 'int',
      required: true,
      default: 5,
      constraints: { min: 1, max: 100 },
      description: 'Blur radius in pixels.',
      examples: [5, 15],
    },
  ],
  mappings: [],
  hardwareDependent: false,
  lifecycleState: 'test',
};

const PROD_CUSTOM: NodeTypeDescriptor = {
  ...BLUR_REGIONS,
  typeId: 'custom.edge_filter',
  displayName: 'Edge Filter',
  category: 'post_processing',
  lifecycleState: null,
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
  useUsecaseMock.mockReturnValue({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  });
});

describe('NodePalette with Custom_Node_Types', () => {
  it('displays custom types in their declared category (8.3)', () => {
    render(<NodePalette catalog={[CAMERA, BLUR_REGIONS, PROD_CUSTOM]} />);

    const preprocessing = screen.getByRole('region', { name: 'Preprocessing' });
    expect(within(preprocessing).getByText('Blur Regions')).toBeInTheDocument();

    const postProcessing = screen.getByRole('region', { name: 'Post-processing' });
    expect(within(postProcessing).getByText('Edge Filter')).toBeInTheDocument();
  });

  it('marks test-state custom types with a visible test-state marker (9.6)', () => {
    render(<NodePalette catalog={[CAMERA, BLUR_REGIONS, PROD_CUSTOM]} />);

    const testItem = screen.getByRole('listitem', { name: 'Blur Regions node type' });
    expect(within(testItem).getByLabelText('test state')).toHaveTextContent('test');

    // No marker on built-in types or custom types outside the test state.
    const builtIn = screen.getByRole('listitem', { name: 'Camera source node type' });
    expect(within(builtIn).queryByLabelText('test state')).toBeNull();
    const prodItem = screen.getByRole('listitem', { name: 'Edge Filter node type' });
    expect(within(prodItem).queryByLabelText('test state')).toBeNull();
  });
});

describe('NodeConfigPanel with Custom_Node_Types', () => {
  it('provides built-in configuration panel behavior: descriptions, examples, and validation (8.4)', () => {
    const onParametersChange = vi.fn();
    const { container } = render(
      <NodeConfigPanel
        node={builderNode(BLUR_REGIONS, { radius: 500 })}
        onParametersChange={onParametersChange}
      />
    );

    // The panel renders the custom type like any built-in node.
    expect(screen.getByText('Blur Regions')).toBeInTheDocument();

    // Field-level description from the declared parameter descriptor.
    expect(screen.getByText('Blur radius in pixels.')).toBeInTheDocument();

    // Declared example values render as clickable chips.
    expect(screen.getByRole('button', { name: 'Use example 5' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Use example 15' })).toBeInTheDocument();

    // Constraint validation surfaces inline like built-ins.
    expect(
      screen.getByText("Parameter 'radius' value 500 is above the maximum 100")
    ).toBeInTheDocument();

    // Edits propagate through the same parameter-change path.
    const radiusInput = container.querySelector('input[aria-label="radius"]')!;
    fireEvent.change(radiusInput, { target: { value: '12' } });
    expect(onParametersChange).toHaveBeenCalledWith('custom.blur_regions_1', {
      radius: 12,
    });
  });
});
