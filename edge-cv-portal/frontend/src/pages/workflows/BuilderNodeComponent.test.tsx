/**
 * Component tests for the canvas node widget: the inline validation
 * badge (Requirements 1.9, 1.10) — a warning badge is rendered when
 * `data.validationMessages` is non-empty (with the messages as its
 * tooltip) and absent when the messages are cleared —, the delete
 * affordance (a small trash button wired to the canvas deletion path,
 * Requirement 1.5), and multi-output rendering (the conditional node's
 * two labeled, typed output handles).
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ReactFlowProvider, type NodeProps } from '@xyflow/react';
import { BuilderNodeComponent } from './BuilderNodeComponent';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from './builderGraph';
import type { NodeTypeDescriptor } from './types';

const CAMERA: NodeTypeDescriptor = {
  typeId: 'camera_source',
  category: 'input',
  displayName: 'Camera source',
  inputs: [],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [
    { name: 'device', paramType: 'string', required: true, default: null, constraints: {} },
  ],
  mappings: [],
  hardwareDependent: true,
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

function nodeProps(
  validationMessages: string[],
  descriptor: NodeTypeDescriptor = CAMERA,
  id = 'cam',
  selected = false
): NodeProps<BuilderNode> {
  return {
    id,
    type: WORKFLOW_NODE_TYPE,
    data: { descriptor, parameters: {}, validationMessages },
    selected,
    dragging: false,
    draggable: true,
    selectable: true,
    deletable: true,
    isConnectable: true,
    zIndex: 0,
    positionAbsoluteX: 0,
    positionAbsoluteY: 0,
  };
}

function renderNode(
  validationMessages: string[],
  descriptor: NodeTypeDescriptor = CAMERA,
  id = 'cam',
  selected = false
) {
  return render(
    <ReactFlowProvider>
      <BuilderNodeComponent {...nodeProps(validationMessages, descriptor, id, selected)} />
    </ReactFlowProvider>
  );
}

describe('BuilderNodeComponent validation badge', () => {
  it('shows a warning badge with the messages when validation messages are present (Requirement 1.9)', () => {
    const messages = ["Node 'cam': Required parameter 'device' has no value"];
    renderNode(messages);

    const badge = screen.getByRole('img', { name: 'Validation warnings on cam' });
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute('title', messages[0]);
  });

  it('renders no badge when the validation messages are cleared (Requirement 1.10)', () => {
    renderNode([]);
    expect(screen.queryByRole('img', { name: 'Validation warnings on cam' })).toBeNull();
  });
});

describe('BuilderNodeComponent delete affordance (Requirement 1.5)', () => {
  it('renders a keyboard-accessible "Delete node" button in the header', () => {
    renderNode([]);
    const button = screen.getByRole('button', { name: 'Delete node' });
    expect(button).toBeInTheDocument();
    expect(button).toHaveAttribute('title', 'Delete node');
    // Hidden until hover/selection/focus, but always present and
    // clickable when shown (never display:none).
    expect(button).toHaveStyle({ opacity: '0' });
  });

  it('reveals the delete button while the node is selected', () => {
    renderNode([], CAMERA, 'cam', true);
    expect(screen.getByRole('button', { name: 'Delete node' })).toHaveStyle({ opacity: '1' });
  });
});

describe('BuilderNodeComponent multi-output rendering (conditional node)', () => {
  it('renders both output handles labeled with their port names and types', () => {
    const { container } = renderNode([], CONDITIONAL, 'cond');

    // Two distinguishable source handles, one per output port.
    const trueHandle = container.querySelector('[data-handleid="true"]');
    const falseHandle = container.querySelector('[data-handleid="false"]');
    expect(trueHandle).not.toBeNull();
    expect(falseHandle).not.toBeNull();
    expect(trueHandle!.getAttribute('aria-label')).toBe('cond output port true (InferenceMeta)');
    expect(falseHandle!.getAttribute('aria-label')).toBe('cond output port false (InferenceMeta)');

    // The port names render as visible row labels beside the handles.
    expect(screen.getByText('true')).toBeInTheDocument();
    expect(screen.getByText('false')).toBeInTheDocument();
  });
});

describe('BuilderNodeComponent conditional output handles', () => {
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

  it('renders both output port handles from the catalog descriptor', () => {
    render(
      <ReactFlowProvider>
        <BuilderNodeComponent
          {...{
            ...nodeProps([]),
            id: 'br',
            data: { descriptor: CONDITIONAL, parameters: {}, validationMessages: [] },
          }}
        />
      </ReactFlowProvider>
    );

    expect(screen.getByLabelText('br input port in (InferenceMeta)')).toBeInTheDocument();
    expect(screen.getByLabelText('br output port true (InferenceMeta)')).toBeInTheDocument();
    expect(screen.getByLabelText('br output port false (InferenceMeta)')).toBeInTheDocument();
  });
});

describe('BuilderNodeComponent multi-input rendering (bedrock_inference node)', () => {
  const BEDROCK: NodeTypeDescriptor = {
    typeId: 'bedrock_inference',
    category: 'inference',
    displayName: 'Bedrock Inference',
    inputs: [
      { name: 'in', portType: 'VideoFrames' },
      { name: 'reference', portType: 'VideoFrames' },
    ],
    outputs: [{ name: 'out', portType: 'InferenceMeta' }],
    parameters: [
      {
        name: 'model',
        paramType: 'enum',
        required: false,
        default: 'us.amazon.nova-lite-v1:0',
        constraints: {
          values: [
            'us.amazon.nova-pro-v1:0',
            'us.amazon.nova-lite-v1:0',
            'qwen.qwen3-vl-235b-a22b',
            'moonshotai.kimi-k2.5',
          ],
        },
      },
      { name: 'prompt', paramType: 'string', required: true, default: null, constraints: {} },
    ],
    mappings: [],
    hardwareDependent: true,
  };

  it('renders both input handles labeled with their port names and types', () => {
    const { container } = renderNode([], BEDROCK, 'br');

    const inHandle = container.querySelector('[data-handleid="in"]');
    const referenceHandle = container.querySelector('[data-handleid="reference"]');
    expect(inHandle).not.toBeNull();
    expect(referenceHandle).not.toBeNull();
    expect(inHandle!.getAttribute('aria-label')).toBe('br input port in (VideoFrames)');
    expect(referenceHandle!.getAttribute('aria-label')).toBe(
      'br input port reference (VideoFrames)'
    );
    expect(screen.getByLabelText('br output port out (InferenceMeta)')).toBeInTheDocument();

    // Both port names render as visible row labels beside the handles.
    expect(screen.getByText('in')).toBeInTheDocument();
    expect(screen.getByText('reference')).toBeInTheDocument();
  });
});

describe('BuilderNodeComponent multi-input rendering (llm_inference node)', () => {
  // Mirrors the catalog descriptor after vlm-anomaly-reference-parity:
  // the VLM/LLM node carries `in` + `reference` VideoFrames inputs and
  // renders both handles via the same catalog-driven generic path as
  // bedrock_inference (Requirement 2.3 — no type-specific frontend code).
  const LLM: NodeTypeDescriptor = {
    typeId: 'llm_inference',
    category: 'inference',
    displayName: 'VLM/LLM Inference',
    inputs: [
      { name: 'in', portType: 'VideoFrames' },
      { name: 'reference', portType: 'VideoFrames' },
    ],
    outputs: [{ name: 'out', portType: 'InferenceMeta' }],
    parameters: [
      {
        name: 'modelName',
        paramType: 'model_ref',
        required: true,
        default: null,
        constraints: { minLength: 1 },
      },
      {
        name: 'prompt_template',
        paramType: 'string',
        required: true,
        default: null,
        constraints: { minLength: 1 },
      },
      {
        name: 'anomaly_mode',
        paramType: 'bool',
        required: false,
        default: false,
        constraints: {},
      },
    ],
    mappings: [],
    hardwareDependent: true,
  };

  it('renders both input handles labeled with their port names and types (Requirement 2.3)', () => {
    const { container } = renderNode([], LLM, 'vlm');

    const inHandle = container.querySelector('[data-handleid="in"]');
    const referenceHandle = container.querySelector('[data-handleid="reference"]');
    expect(inHandle).not.toBeNull();
    expect(referenceHandle).not.toBeNull();
    expect(inHandle!.getAttribute('aria-label')).toBe('vlm input port in (VideoFrames)');
    expect(referenceHandle!.getAttribute('aria-label')).toBe(
      'vlm input port reference (VideoFrames)'
    );
    expect(screen.getByLabelText('vlm output port out (InferenceMeta)')).toBeInTheDocument();

    // Both port names render as visible row labels beside the handles.
    expect(screen.getByText('in')).toBeInTheDocument();
    expect(screen.getByText('reference')).toBeInTheDocument();
  });
});
