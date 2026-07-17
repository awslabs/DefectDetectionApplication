/**
 * The nodes × target-devices Camera_Binding matrix of the
 * CreateDeployment page (camera-registry-sync task 12.1 — Requirements
 * 8.1, 8.4, 8.5, 8.7, 8.8, 8.9, 9.2, 9.3).
 *
 * Rendered only when the selected workflow version has
 * Camera_Input_Nodes (`binding_required`, 8.9). Each cell offers the
 * target device's registered Camera_Sources as a dropdown (8.1), with
 * the node's binding hint pre-selected but visibly marked as suggested
 * until the user confirms or changes it (8.5), and a manual-override
 * entry accepting an explicit device path (8.4). Never-synced targets
 * show a warning and permit manual override only (8.8). Warnings carry
 * confirmation checkboxes feeding `confirmed_warnings` (9.3), and
 * submission rejections are surfaced next to the matrix identifying the
 * node and device (8.7, 9.2).
 */
import {
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  Header,
  Input,
  Select,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';
import type { SelectProps, TableProps } from '@cloudscape-design/components';
import {
  BindingCell,
  BindingContextNode,
  CameraBindingContext,
  CameraBindingIssue,
  CameraBindingWarning,
  cameraOptionDescription,
  cameraOptionLabel,
  cameraOptionTags,
  describeBindingIssue,
  getBindingCell,
  BindingSelections,
} from './cameraBindings';
import { isAravisCompatibleCamera } from '../workflows/cameraReference';

export interface CameraBindingMatrixProps {
  context: CameraBindingContext;
  selections: BindingSelections;
  onCellChange: (device: string, nodeId: string, cell: BindingCell) => void;
  /** Warnings requiring confirmation (expected + server-reported). */
  warnings: CameraBindingWarning[];
  confirmedWarningIds: ReadonlySet<string>;
  onToggleWarning: (warningId: string, confirmed: boolean) => void;
  /** Validation errors from a rejected submission (409). */
  errors: CameraBindingIssue[];
}

function BindingCellControl({
  context,
  device,
  node,
  cell,
  onChange,
}: {
  context: CameraBindingContext;
  device: string;
  node: BindingContextNode;
  cell: BindingCell;
  onChange: (cell: BindingCell) => void;
}) {
  const target = context.targets[device];
  const neverSynced = target?.state === 'never-synced';
  const allCameras = target?.cameras ?? [];
  // An aravis_camera_source row offers only Aravis-compatible sources —
  // the same predicate the Workflow_Builder picker uses — so users are
  // not offered bindings the validator would reject (aravis-camera-input
  // Requirement 5.1). Hint pre-selection is unaffected: the cell state
  // is seeded upstream by initialBindingSelections.
  const cameras =
    node.node_type === 'aravis_camera_source'
      ? allCameras.filter(isAravisCompatibleCamera)
      : allCameras;

  // Never-synced targets are restricted to manual override (8.8).
  if (neverSynced) {
    return (
      <SpaceBetween size="xxs">
        <Box color="text-status-warning" fontSize="body-s">
          Never synced — manual override only
        </Box>
        <Input
          value={cell.mode === 'override' ? cell.device : ''}
          onChange={({ detail }) => onChange({ mode: 'override', device: detail.value })}
          placeholder="Device path, e.g. /dev/video0"
          ariaLabel={`Manual override device path for node ${node.node_id} on device ${device}`}
        />
      </SpaceBetween>
    );
  }

  if (cell.mode === 'override') {
    return (
      <SpaceBetween size="xxs">
        <Input
          value={cell.device}
          onChange={({ detail }) => onChange({ mode: 'override', device: detail.value })}
          placeholder="Device path, e.g. /dev/video0"
          ariaLabel={`Manual override device path for node ${node.node_id} on device ${device}`}
        />
        <Button
          variant="inline-link"
          onClick={() => onChange({ mode: 'unbound' })}
        >
          Use registered camera
        </Button>
      </SpaceBetween>
    );
  }

  const options: SelectProps.Option[] = cameras.map((camera) => ({
    value: camera.camera_source_id,
    label: cameraOptionLabel(camera),
    description: cameraOptionDescription(camera),
    tags: cameraOptionTags(camera),
  }));
  const selectedOption =
    cell.mode === 'camera'
      ? options.find((o) => o.value === cell.cameraSourceId) ?? {
          value: cell.cameraSourceId,
          label: cell.cameraSourceId,
        }
      : null;

  return (
    <SpaceBetween size="xxs">
      {cell.mode === 'camera' && cell.suggested && (
        <Badge color="blue">Suggested from workflow hint</Badge>
      )}
      <Select
        selectedOption={selectedOption}
        onChange={({ detail }) => {
          const value = detail.selectedOption?.value;
          if (value) {
            // A user-made (or user-confirmed) choice is no longer a
            // suggestion (8.5).
            onChange({ mode: 'camera', cameraSourceId: value, suggested: false });
          }
        }}
        options={options}
        placeholder={cameras.length === 0 ? 'No cameras registered' : 'Select camera'}
        disabled={cameras.length === 0}
        empty="No cameras registered for this device"
        ariaLabel={`Camera binding for node ${node.node_id} on device ${device}`}
      />
      <Button
        variant="inline-link"
        onClick={() => onChange({ mode: 'override', device: '' })}
      >
        Manual override
      </Button>
    </SpaceBetween>
  );
}

export function CameraBindingMatrix({
  context,
  selections,
  onCellChange,
  warnings,
  confirmedWarningIds,
  onToggleWarning,
  errors,
}: CameraBindingMatrixProps) {
  const devices = Object.keys(context.targets);
  const neverSyncedDevices = devices.filter(
    (d) => context.targets[d].state === 'never-synced'
  );

  const columnDefinitions: TableProps.ColumnDefinition<BindingContextNode>[] = [
    {
      id: 'node',
      header: 'Camera input node',
      cell: (node) => (
        <SpaceBetween size="xxs">
          <Box fontWeight="bold">{node.node_id}</Box>
          <Box color="text-body-secondary" fontSize="body-s">
            {node.node_type}
          </Box>
        </SpaceBetween>
      ),
    },
    ...devices.map((device) => ({
      id: `device-${device}`,
      header: device,
      cell: (node: BindingContextNode) => (
        <BindingCellControl
          context={context}
          device={device}
          node={node}
          cell={getBindingCell(selections, device, node.node_id)}
          onChange={(cell) => onCellChange(device, node.node_id, cell)}
        />
      ),
    })),
  ];

  return (
    <SpaceBetween size="m">
      {errors.length > 0 && (
        <Alert type="error" header="Camera binding validation failed">
          <ul style={{ margin: 0, paddingLeft: '20px' }}>
            {errors.map((issue, index) => (
              <li key={`${issue.code}-${issue.device}-${issue.nodeId ?? ''}-${index}`}>
                {describeBindingIssue(issue)}
              </li>
            ))}
          </ul>
        </Alert>
      )}

      {neverSyncedDevices.length > 0 && (
        <Alert type="warning" header="Devices that have never synced their camera registry">
          The following target device(s) have never completed a camera registry
          synchronization, so no registered cameras are available for them.
          Camera bindings for these devices are restricted to manual override:{' '}
          <strong>{neverSyncedDevices.join(', ')}</strong>
        </Alert>
      )}

      <Table
        resizableColumns
        header={
          <Header
            variant="h3"
            description="Bind each camera input node of the workflow to a camera registered on each target device, or supply a manual override."
          >
            Camera bindings — {context.workflow_id} v{context.workflow_version}
          </Header>
        }
        items={context.camera_input_nodes}
        columnDefinitions={columnDefinitions}
        empty={<Box textAlign="center">No camera input nodes</Box>}
      />

      {warnings.length > 0 && (
        <Alert type="warning" header="Camera binding warnings require confirmation">
          <SpaceBetween size="xs">
            <Box>
              Review and confirm each warning below to allow the deployment to
              be created.
            </Box>
            {warnings.map((warning) => (
              <Checkbox
                key={warning.id}
                checked={confirmedWarningIds.has(warning.id)}
                onChange={({ detail }) => onToggleWarning(warning.id, detail.checked)}
              >
                {warning.message}
              </Checkbox>
            ))}
          </SpaceBetween>
        </Alert>
      )}
    </SpaceBetween>
  );
}
