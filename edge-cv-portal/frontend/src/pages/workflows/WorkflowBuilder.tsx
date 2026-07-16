/**
 * Workflow_Builder canvas page (Requirements 1.1-1.6).
 *
 * React Flow canvas with the custom workflow node component, the
 * Node_Palette sidebar sourced from the node-catalog endpoint, HTML5
 * drag-and-drop node placement with default configuration, connection
 * rules enforced via `isValidConnection` -> `arePortsCompatible` (with
 * the rejection reason displayed), pan/zoom/reposition, and delete of
 * nodes/connections where deleting a node removes attached connections.
 *
 * Selecting a node opens the node configuration panel docked on the
 * right of the canvas (Requirement 1.7); edits update
 * `node.data.parameters` (validated inline by the panel, Requirement
 * 1.8). Inline validation markers (8.4) and the actions toolbar/API
 * wiring (8.5) build on top of this page.
 *
 * The Generate (10.x) and Test (12.x) panels live in a collapsible
 * right-hand side drawer beside the canvas: collapsed by default to a
 * slim vertical toggle strip so the canvas gets the full page height,
 * expanded to a fixed-width column with a Generate/Test segmented
 * control. Both panels stay mounted while the drawer is collapsed, so
 * chat sessions, typed prompts, dataset selections, and per-node test
 * results survive toggling.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type IsValidConnection,
  type OnConnectEnd,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Header from '@cloudscape-design/components/header';
import Icon from '@cloudscape-design/components/icon';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import { apiService } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { useUsecase } from '../../contexts/UsecaseContext';
import { BUILDER_NODE_TYPES } from './BuilderNodeComponent';
import {
  connectionRejectionReason,
  createBuilderNode,
  edgeIdFor,
  fromWorkflowDefinition,
  isSameConnection,
  toWorkflowDefinition,
  type BuilderNode,
} from './builderGraph';
import { CAMERA_BINDING_HINT_KEY, type CameraBindingHint } from './cameraReference';
import GenerateChatPanel from './GenerateChatPanel';
import NodeConfigPanel from './NodeConfigPanel';
import TestPanel from './TestPanel';
import NodePalette, { PALETTE_DRAG_MIME } from './NodePalette';
import WorkflowToolbar, { type WorkflowMeta } from './WorkflowToolbar';
import type { JsonValue, NodeTypeDescriptor } from './types';
import { applyValidationMarkers } from './validationMarkers';

/** The Generate/Test side drawer tabs. */
type SidePanelTab = 'generate' | 'test';

/** Width of the expanded side drawer. */
const SIDE_DRAWER_WIDTH = 384;

/** Width of the collapsed side drawer toggle strip. */
const SIDE_DRAWER_STRIP_WIDTH = 48;

/**
 * Toggle buttons shown while the side drawer is collapsed: an icon on
 * top of a HORIZONTAL abbreviated label (rotated vertical-rl text
 * proved hard to read). The strip stays slim (48px) by abbreviating
 * "Generate" to "Gen"; the button's title and aria-label carry the full
 * panel name for hover and assistive technologies.
 */
const STRIP_BUTTON_STYLE: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  gap: 4,
  border: '1px solid #d1d5db',
  borderRadius: 4,
  background: '#ffffff',
  cursor: 'pointer',
  padding: '10px 2px',
  color: '#1f2937',
};

/** The horizontal abbreviated label inside a collapsed-strip button. */
const STRIP_LABEL_STYLE: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  letterSpacing: '0.02em',
  lineHeight: '14px',
  color: '#1f2937',
};

function BuilderCanvas({
  catalog,
  initialWorkflowId,
}: {
  catalog: NodeTypeDescriptor[];
  initialWorkflowId?: string;
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<BuilderNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const { screenToFlowPosition } = useReactFlow();
  const { user } = useAuth();
  const { selectedUsecaseId } = useUsecase();

  // Actions toolbar state (task 8.5): the loaded workflow's identity and
  // the serialized definition as of the last save/load, used to compute
  // whether the canvas carries unsaved changes.
  const [workflow, setWorkflow] = useState<WorkflowMeta | null>(null);
  const [savedJson, setSavedJson] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Right-hand side drawer holding the Generate and Test panels:
  // collapsed by default (slim toggle strip) so the canvas gets the
  // full page height; expanded it takes a fixed width beside the canvas.
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState<SidePanelTab>('generate');
  const openDrawer = useCallback((tab: SidePanelTab) => {
    setDrawerTab(tab);
    setDrawerOpen(true);
  }, []);

  // The current canvas as a Workflow_Definition document. Nodes and
  // connections are id-sorted, so the same graph always serializes the
  // same way regardless of interaction order.
  const definition = useMemo(() => toWorkflowDefinition(nodes, edges), [nodes, edges]);
  const definitionJson = useMemo(() => JSON.stringify(definition), [definition]);
  const dirty = savedJson === null ? nodes.length > 0 || edges.length > 0 : definitionJson !== savedJson;

  const getDefinition = useCallback(() => toWorkflowDefinition(nodes, edges), [nodes, edges]);

  const onSaved = useCallback(
    (meta: WorkflowMeta) => {
      setWorkflow(meta);
      setSavedJson(definitionJson);
    },
    [definitionJson]
  );

  // Reset to a fresh, unsaved canvas: used after a delete and for the
  // toolbar's "New" action.
  const resetCanvas = useCallback(() => {
    setWorkflow(null);
    setSavedJson(null);
    setNodes([]);
    setEdges([]);
  }, [setNodes, setEdges]);

  // Open/load a saved workflow: render the saved nodes, positions,
  // configurations, and connections exactly as stored (Requirement 5.4).
  const loadWorkflow = useCallback(
    async (workflowId: string) => {
      const response = await apiService.getWorkflow(workflowId);
      const loaded = fromWorkflowDefinition(response.definition, catalog);
      setNodes(loaded.nodes);
      setEdges(loaded.edges);
      setWorkflow({
        workflowId: response.workflow.workflow_id,
        name: response.workflow.name,
        description: response.workflow.description ?? '',
        version: response.version,
      });
      setSavedJson(JSON.stringify(toWorkflowDefinition(loaded.nodes, loaded.edges)));
      setLoadError(null);
    },
    [catalog, setNodes, setEdges]
  );

  // Deep link: /workflows/builder/{workflowId} opens the workflow on mount.
  const initialLoadDone = useRef(false);
  useEffect(() => {
    if (initialWorkflowId && !initialLoadDone.current) {
      initialLoadDone.current = true;
      loadWorkflow(initialWorkflowId).catch((err: Error) => {
        setLoadError(err.message || 'Failed to open the workflow');
      });
    }
  }, [initialWorkflowId, loadWorkflow]);

  // Inline validation markers (Requirements 1.9, 1.10): run the TS V4/V5
  // mirror on every graph mutation (node add/remove, edge add/remove,
  // parameter change) and write each finding's message onto the offending
  // node's `data.validationMessages`; the node component renders these as
  // warning badges, cleared when the condition resolves.
  // `applyValidationMarkers` preserves node identity when messages are
  // unchanged and returns the input array when no node changed, so the
  // state update below is a no-op once markers are settled — no loops.
  useEffect(() => {
    setNodes((existing) => applyValidationMarkers(existing, edges, catalog));
  }, [nodes, edges, catalog, setNodes]);

  // Reason for the most recent incompatible connection attempt, captured
  // during isValidConnection and surfaced when the drag ends rejected.
  const rejectionRef = useRef<string | null>(null);
  const [rejectionMessage, setRejectionMessage] = useState<string | null>(null);

  const isValidConnection: IsValidConnection<Edge> = useCallback(
    (connection) => {
      const reason = connectionRejectionReason(connection, nodes);
      rejectionRef.current = reason;
      return reason === null;
    },
    [nodes]
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      setRejectionMessage(null);
      setEdges((existing) => {
        if (existing.some((edge) => isSameConnection(edge, connection))) {
          return existing;
        }
        return [
          ...existing,
          {
            id: edgeIdFor(connection),
            source: connection.source,
            sourceHandle: connection.sourceHandle,
            target: connection.target,
            targetHandle: connection.targetHandle,
          },
        ];
      });
    },
    [setEdges]
  );

  // Display the reason when a drag ended on a handle that rejected the
  // connection (Requirement 1.4).
  const onConnectEnd: OnConnectEnd = useCallback((_event, connectionState) => {
    if (connectionState.isValid === false && rejectionRef.current !== null) {
      setRejectionMessage(rejectionRef.current);
    }
    rejectionRef.current = null;
  }, []);

  // Deleting a node removes every connection attached to it
  // (Requirement 1.5). React Flow's delete handling already does this;
  // this handler guarantees it regardless of how deletion is triggered.
  const onNodesDelete = useCallback(
    (deleted: BuilderNode[]) => {
      const removed = new Set(deleted.map((node) => node.id));
      setEdges((existing) =>
        existing.filter((edge) => !removed.has(edge.source) && !removed.has(edge.target))
      );
    },
    [setEdges]
  );

  // The node the configuration panel edits: the selected node
  // (Requirement 1.7). With a multi-selection the first selected node
  // is shown.
  const selectedNode = useMemo(() => nodes.find((node) => node.selected) ?? null, [nodes]);

  // Close the configuration panel by deselecting every node.
  const closeConfigPanel = useCallback(() => {
    setNodes((existing) =>
      existing.map((node) => (node.selected ? { ...node, selected: false } : node))
    );
  }, [setNodes]);

  // Apply a parameter edit from the configuration panel to the node's
  // canvas state (Requirement 1.8). Port-type parameter changes on
  // custom Python nodes flow into the node component's resolvedPorts,
  // updating the rendered port handles.
  const onNodeParametersChange = useCallback(
    (nodeId: string, parameters: Record<string, JsonValue>) => {
      setNodes((existing) =>
        existing.map((node) =>
          node.id === nodeId ? { ...node, data: { ...node.data, parameters } } : node
        )
      );
    },
    [setNodes]
  );

  // Apply a camera reference selection (camera-registry-sync Requirement
  // 7.2): the updated parameters plus the advisory binding hint stored in
  // the node's advisory data (`nodes[].data.cameraBindingHint` in the
  // serialized definition), preserved through save/load round trips
  // without making the definition device-specific (Requirement 7.5).
  const onNodeCameraSelection = useCallback(
    (nodeId: string, parameters: Record<string, JsonValue>, hint: CameraBindingHint) => {
      setNodes((existing) =>
        existing.map((node) =>
          node.id === nodeId
            ? {
                ...node,
                data: {
                  ...node.data,
                  parameters,
                  advisoryData: { ...node.data.advisoryData, [CAMERA_BINDING_HINT_KEY]: { ...hint } },
                },
              }
            : node
        )
      );
    },
    [setNodes]
  );

  // Render a generated workflow onto the canvas (Requirement 10.3). The
  // chat panel calls this only after the client-side parse succeeded, so
  // a failed generation never reaches here and the canvas stays
  // unchanged (Requirement 10.4).
  const onApplyGenerated = useCallback(
    (generated: { nodes: BuilderNode[]; edges: Edge[] }) => {
      setNodes(generated.nodes);
      setEdges(generated.edges);
    },
    [setNodes, setEdges]
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    if (event.dataTransfer.types.includes(PALETTE_DRAG_MIME)) {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
    }
  }, []);

  // Drop from the Node_Palette: place a new node instance at the drop
  // position with the type's default configuration (Requirement 1.2).
  const onDrop = useCallback(
    (event: React.DragEvent) => {
      const typeId = event.dataTransfer.getData(PALETTE_DRAG_MIME);
      if (!typeId) {
        return;
      }
      event.preventDefault();
      const descriptor = catalog.find((entry) => entry.typeId === typeId);
      if (descriptor === undefined) {
        return;
      }
      const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      setNodes((existing) => [
        ...existing,
        createBuilderNode(
          descriptor,
          position,
          existing.map((node) => node.id)
        ),
      ]);
    },
    [catalog, screenToFlowPosition, setNodes]
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flexGrow: 1, minHeight: 0 }}>
      <Box padding={{ bottom: 's' }}>
        <WorkflowToolbar
          role={user?.role}
          usecaseId={selectedUsecaseId}
          workflow={workflow}
          dirty={dirty}
          getDefinition={getDefinition}
          onSaved={onSaved}
          onOpenWorkflow={loadWorkflow}
          onDeleted={resetCanvas}
          onNew={resetCanvas}
        />
      </Box>
      {loadError !== null && (
        <Alert
          type="error"
          header="Failed to open the workflow"
          dismissible
          onDismiss={() => setLoadError(null)}
        >
          {loadError}
        </Alert>
      )}
      <div style={{ display: 'flex', flexGrow: 1, minHeight: 0 }}>
      <NodePalette catalog={catalog} />
      <div style={{ flexGrow: 1, position: 'relative' }} onDragOver={onDragOver} onDrop={onDrop}>
        {rejectionMessage !== null && (
          <div style={{ position: 'absolute', top: 8, left: 8, right: 8, zIndex: 10 }}>
            <Alert
              type="warning"
              dismissible
              onDismiss={() => setRejectionMessage(null)}
              header="Connection rejected"
            >
              {rejectionMessage}
            </Alert>
          </div>
        )}
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={BUILDER_NODE_TYPES}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onConnectEnd={onConnectEnd}
          onNodesDelete={onNodesDelete}
          isValidConnection={isValidConnection}
          deleteKeyCode={['Backspace', 'Delete']}
          fitView
          proOptions={{ hideAttribution: true }}
        >
          <Background />
          <Controls />
          <MiniMap pannable zoomable />
        </ReactFlow>
      </div>
      <NodeConfigPanel
        node={selectedNode}
        onParametersChange={onNodeParametersChange}
        onCameraSelection={onNodeCameraSelection}
        onClose={closeConfigPanel}
      />
      {/* Generate/Test side drawer. Both panels stay mounted (hidden via
          display:none) so their state survives collapsing and tab switches. */}
      <aside
        aria-label="Workflow side panels"
        style={{ display: 'flex', flexShrink: 0, minHeight: 0 }}
      >
        {!drawerOpen && (
          <div
            style={{
              width: SIDE_DRAWER_STRIP_WIDTH,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'stretch',
              gap: 6,
              padding: '8px 4px',
              background: '#fafafa',
              borderLeft: '1px solid #d1d5db',
            }}
          >
            <button
              type="button"
              aria-label="Open the Generate workflow panel"
              title="Generate"
              onClick={() => openDrawer('generate')}
              style={STRIP_BUTTON_STYLE}
            >
              <Icon name="gen-ai" size="small" />
              <span style={STRIP_LABEL_STYLE}>Gen</span>
            </button>
            <button
              type="button"
              aria-label="Open the Test workflow panel"
              title="Test"
              onClick={() => openDrawer('test')}
              style={STRIP_BUTTON_STYLE}
            >
              <Icon name="status-pending" size="small" />
              <span style={STRIP_LABEL_STYLE}>Test</span>
            </button>
          </div>
        )}
        <div
          style={{
            width: SIDE_DRAWER_WIDTH,
            display: drawerOpen ? 'flex' : 'none',
            flexDirection: 'column',
            minHeight: 0,
            background: '#ffffff',
            borderLeft: '1px solid #d1d5db',
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 8,
              padding: '4px 8px',
              borderBottom: '1px solid #e5e7eb',
              flexShrink: 0,
            }}
          >
            <SegmentedControl
              selectedId={drawerTab}
              onChange={({ detail }) => setDrawerTab(detail.selectedId as SidePanelTab)}
              label="Workflow side panel"
              options={[
                { text: 'Generate', id: 'generate' },
                { text: 'Test', id: 'test' },
              ]}
            />
            <Button
              iconName="angle-right"
              variant="icon"
              ariaLabel="Collapse the side panel"
              onClick={() => setDrawerOpen(false)}
            />
          </div>
          <div style={{ flexGrow: 1, minHeight: 0, overflowY: 'auto', padding: 12 }}>
            <div style={{ display: drawerTab === 'generate' ? undefined : 'none' }}>
              <GenerateChatPanel
                role={user?.role}
                usecaseId={selectedUsecaseId}
                catalog={catalog}
                getDefinition={getDefinition}
                onApplyGenerated={onApplyGenerated}
              />
            </div>
            <div style={{ display: drawerTab === 'test' ? undefined : 'none' }}>
              <TestPanel
                role={user?.role}
                usecaseId={selectedUsecaseId}
                workflow={workflow}
                getDefinition={getDefinition}
                active={drawerOpen && drawerTab === 'test'}
              />
            </div>
          </div>
        </div>
      </aside>
      </div>
    </div>
  );
}

export default function WorkflowBuilder() {
  const [catalog, setCatalog] = useState<NodeTypeDescriptor[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { workflowId } = useParams<{ workflowId: string }>();
  const { selectedUsecaseId } = useUsecase();

  useEffect(() => {
    let cancelled = false;
    // The Use_Case id merges the registered Custom_Node_Types into the
    // palette catalog (test/prod backed only, 8.2/9.2); without it the
    // endpoint serves just the built-in node types.
    apiService
      .getWorkflowNodeCatalog(selectedUsecaseId || undefined)
      .then((response) => {
        if (!cancelled) {
          setCatalog(response.nodeTypes);
        }
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setError(err.message || 'Failed to load the node catalog');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedUsecaseId]);

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        // Fill the viewport minus the app chrome only (top navigation,
        // bottom version banner). The AppLayout content paddings are
        // disabled on this route (Layout.tsx) so the builder gets the
        // full content width; the minimal padding below keeps the header
        // and toolbar off the side navigation without reintroducing the
        // dead gutter. The Generate/Test panels live in the side drawer,
        // so the canvas row gets all of the remaining height below the
        // compact header and toolbar.
        height: 'calc(100vh - 96px)',
        minHeight: 480,
        // The 48px left inset keeps the header clear of the AppLayout's
        // floating navigation-toggle (hamburger) button, which overlays
        // the top-left corner while the side navigation is collapsed on
        // this route.
        padding: '8px 12px 0 48px',
        boxSizing: 'border-box',
      }}
    >
      <Box padding={{ bottom: 's' }}>
        {/* Compact header (h3, ~half the h1 size) so the canvas gets the
            vertical space; the description drops to the small body font. */}
        <Header
          variant="h3"
          description={
            <Box fontSize="body-s" color="text-body-secondary">
              Compose a video pipeline by dragging nodes onto the canvas and connecting their
              ports.
            </Box>
          }
        >
          Workflow Builder
        </Header>
      </Box>
      {error !== null && (
        <Alert type="error" header="Failed to load the node catalog">
          {error}
        </Alert>
      )}
      {error === null && catalog === null && (
        <SpaceBetween direction="horizontal" size="xs">
          <Spinner />
          <span>Loading node catalog...</span>
        </SpaceBetween>
      )}
      {catalog !== null && (
        <ReactFlowProvider>
          <BuilderCanvas catalog={catalog} initialWorkflowId={workflowId} />
        </ReactFlowProvider>
      )}
    </div>
  );
}
