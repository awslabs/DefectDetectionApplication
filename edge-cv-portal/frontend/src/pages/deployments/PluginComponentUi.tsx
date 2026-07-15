/**
 * Plugin_Component UI pieces for the deployment screen
 * (custom-node-designer task 12.6, Requirements 16.2, 16.3, 16.6).
 *
 * - ArchitectureChips: supported Target_Architecture chips for a
 *   `dda.plugin.*` component listing entry (16.2).
 * - PluginGateRejectionAlert: pre-submit gate rejection display
 *   identifying each Plugin_Component and the unsupported architecture
 *   (16.6) or lifecycle violation (16.3).
 */
import { Alert, Badge, SpaceBetween } from '@cloudscape-design/components';
import {
  PluginGateRejection,
  architectureLabel,
  describeArchUnsupported,
  describeLifecycleViolation,
} from './pluginComponents';

/** Supported Target_Architecture chips for a Plugin_Component (16.2). */
export function ArchitectureChips({ architectures }: { architectures: string[] }) {
  if (!architectures || architectures.length === 0) {
    return <span>—</span>;
  }
  return (
    <SpaceBetween direction="horizontal" size="xxs">
      {architectures.map((arch) => (
        <Badge key={arch} color="grey">
          {architectureLabel(arch)}
        </Badge>
      ))}
    </SpaceBetween>
  );
}

/**
 * Pre-submit gate rejection alert: itemizes every violation, each
 * identifying the Plugin_Component and the lifecycle violation (16.3)
 * or the unsupported device architecture (16.6).
 */
export function PluginGateRejectionAlert({
  rejection,
  onDismiss,
}: {
  rejection: PluginGateRejection;
  onDismiss: () => void;
}) {
  const isLifecycle = rejection.code === 'PLUGIN_LIFECYCLE_VIOLATION';
  return (
    <Alert
      type="error"
      dismissible
      onDismiss={onDismiss}
      header={
        isLifecycle
          ? 'Deployment rejected: plugin lifecycle violation'
          : 'Deployment rejected: unsupported device architecture'
      }
    >
      <SpaceBetween size="xs">
        <span>{rejection.message}</span>
        <ul style={{ margin: 0, paddingLeft: '20px' }}>
          {rejection.lifecycleViolations.map((v, i) => (
            <li key={`${v.pluginComponent}-${i}`}>{describeLifecycleViolation(v)}</li>
          ))}
          {rejection.archUnsupported.map((u, i) => (
            <li key={`${u.pluginComponent}-${u.device}-${i}`}>{describeArchUnsupported(u)}</li>
          ))}
        </ul>
      </SpaceBetween>
    </Alert>
  );
}
