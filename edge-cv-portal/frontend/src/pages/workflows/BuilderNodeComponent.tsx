/**
 * Custom React Flow node for the Workflow_Builder canvas.
 *
 * Renders the category color strip, the node title, typed port handles
 * (inputs on the left, outputs on the right, each labeled with its port
 * name and type), and a warning badge when inline validation messages
 * are present on the node (populated by the inline validation task).
 */

import { memo, useState } from 'react';
import { Handle, Position, useReactFlow, type NodeProps, type NodeTypes } from '@xyflow/react';
import { categoryMeta, toWorkflowNode, WORKFLOW_NODE_TYPE, type BuilderNode } from './builderGraph';
import { resolvedPorts } from './inlineChecks';

const NODE_WIDTH = 220;

const HANDLE_STYLE: React.CSSProperties = {
  width: 10,
  height: 10,
  background: '#414d5c',
  border: '2px solid #ffffff',
  position: 'absolute',
  top: '50%',
  transform: 'translateY(-50%)',
};

function PortRow(props: {
  nodeId: string;
  name: string;
  portType: string;
  kind: 'input' | 'output';
}) {
  const { nodeId, name, portType, kind } = props;
  const isInput = kind === 'input';
  return (
    <div
      style={{
        position: 'relative',
        display: 'flex',
        justifyContent: isInput ? 'flex-start' : 'flex-end',
        padding: isInput ? '2px 8px 2px 14px' : '2px 14px 2px 8px',
        fontSize: 11,
        lineHeight: '16px',
      }}
    >
      <Handle
        type={isInput ? 'target' : 'source'}
        position={isInput ? Position.Left : Position.Right}
        id={name}
        style={{ ...HANDLE_STYLE, [isInput ? 'left' : 'right']: -5 }}
        aria-label={`${nodeId} ${kind} port ${name} (${portType})`}
      />
      <span>
        <strong>{name}</strong>
        <span style={{ color: '#5f6b7a' }}> : {portType}</span>
      </span>
    </div>
  );
}

function BuilderNodeComponentInner({ id, data, selected }: NodeProps<BuilderNode>) {
  const { descriptor, validationMessages } = data;
  const meta = categoryMeta(descriptor.category);
  const { deleteElements } = useReactFlow();
  // The delete affordance is revealed on hover, selection, or keyboard
  // focus, and is always clickable while shown (Requirement 1.5 via the
  // canvas deletion path: deleteElements removes the node and its
  // attached connections through onNodesDelete).
  const [hovered, setHovered] = useState(false);
  const [deleteFocused, setDeleteFocused] = useState(false);
  const showDelete = hovered || selected || deleteFocused;
  const ports = resolvedPorts(
    toWorkflowNode({ id, type: WORKFLOW_NODE_TYPE, position: { x: 0, y: 0 }, data }),
    descriptor
  );
  const hasWarnings = validationMessages.length > 0;

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        width: NODE_WIDTH,
        background: '#ffffff',
        border: `2px solid ${selected ? '#0972d3' : '#d1d5db'}`,
        borderRadius: 6,
        boxShadow: selected ? '0 0 0 2px rgba(9, 114, 211, 0.25)' : '0 1px 2px rgba(0,0,0,0.1)',
        fontFamily: 'inherit',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          background: meta.color,
          color: '#ffffff',
          borderRadius: '4px 4px 0 0',
          padding: '4px 8px',
        }}
      >
        <span style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: 0.5 }}>
          {meta.label}
        </span>
        <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 4 }}>
          {hasWarnings && (
            <span
              role="img"
              aria-label={`Validation warnings on ${id}`}
              title={validationMessages.join('\n')}
              style={{
                background: '#ffffff',
                color: '#8a6116',
                borderRadius: '50%',
                width: 16,
                height: 16,
                fontSize: 11,
                fontWeight: 700,
                lineHeight: '16px',
                textAlign: 'center',
              }}
            >
              !
            </span>
          )}
          <button
            type="button"
            aria-label="Delete node"
            title="Delete node"
            className="nodrag"
            onClick={(event) => {
              event.stopPropagation();
              void deleteElements({ nodes: [{ id }] });
            }}
            onFocus={() => setDeleteFocused(true)}
            onBlur={() => setDeleteFocused(false)}
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: 16,
              height: 16,
              padding: 0,
              border: 'none',
              borderRadius: 3,
              background: 'rgba(255, 255, 255, 0.25)',
              color: '#ffffff',
              cursor: 'pointer',
              opacity: showDelete ? 1 : 0,
            }}
          >
            <svg
              aria-hidden="true"
              focusable="false"
              width="10"
              height="11"
              viewBox="0 0 10 11"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.2"
              strokeLinecap="round"
            >
              {/* Tiny trash can: lid, handle, body, and two bin lines. */}
              <path d="M0.8 2.6h8.4" />
              <path d="M3.4 2.6V1.4h3.2v1.2" />
              <path d="M1.8 2.6l0.6 7.2h5.2l0.6-7.2" />
              <path d="M3.9 4.6v3.4M6.1 4.6v3.4" />
            </svg>
          </button>
        </span>
      </div>
      <div style={{ padding: '6px 8px', fontSize: 13, fontWeight: 700 }}>
        {descriptor.displayName}
      </div>
      <div style={{ paddingBottom: 6 }}>
        {descriptor.inputs.map((port) => (
          <PortRow
            key={`in-${port.name}`}
            nodeId={id}
            name={port.name}
            portType={ports.inputs[port.name] ?? port.portType}
            kind="input"
          />
        ))}
        {descriptor.outputs.map((port) => (
          <PortRow
            key={`out-${port.name}`}
            nodeId={id}
            name={port.name}
            portType={ports.outputs[port.name] ?? port.portType}
            kind="output"
          />
        ))}
      </div>
    </div>
  );
}

export const BuilderNodeComponent = memo(BuilderNodeComponentInner);

/** React Flow `nodeTypes` map (stable module-level reference). */
export const BUILDER_NODE_TYPES: NodeTypes = {
  [WORKFLOW_NODE_TYPE]: BuilderNodeComponent,
};
