/**
 * Registration prompt banner (custom-node-designer, task 12.5,
 * Requirement 4.6).
 *
 * Shown on the Plugin_Record detail once at least one architecture
 * build succeeded: prompts the user to declare the Custom_Node_Type
 * details for the plugin's element via the registration wizard. When
 * the plugin already backs a registered node type, the prompt offers
 * an update (the wizard saves a new version of the existing
 * registration) instead of a duplicate registration.
 */
import { useEffect, useState } from 'react';
import { Alert, Button } from '@cloudscape-design/components';
import { useNavigate } from 'react-router-dom';
import { nodeDesignerApi } from './api';
import { shouldPromptRegistration } from './registration';
import type { PluginArtifactEntry } from './types';

export default function RegistrationPrompt({
  pluginId,
  version,
  artifacts,
}: {
  pluginId: string;
  version: number;
  artifacts: Record<string, PluginArtifactEntry> | null | undefined;
}) {
  const navigate = useNavigate();
  const prompt = shouldPromptRegistration(artifacts);
  // node_type_id of the existing registration backed by this plugin,
  // null when none: the action switches to "Update node type".
  const [existingTypeId, setExistingTypeId] = useState<string | null>(null);

  useEffect(() => {
    if (!prompt) return;
    let cancelled = false;
    nodeDesignerApi
      .listNodeTypes(pluginId)
      .then(({ nodeTypes }) => {
        if (cancelled) return;
        const active = nodeTypes.find((nodeType) => !nodeType.deprecated);
        setExistingTypeId(active ? active.node_type_id : null);
      })
      .catch(() => {
        // Detection is best-effort; the wizard re-detects on load.
      });
    return () => {
      cancelled = true;
    };
  }, [pluginId, prompt]);

  if (!prompt) {
    return null;
  }
  const registered = Boolean(existingTypeId);
  return (
    <Alert
      type="success"
      header={
        registered
          ? 'Build succeeded — update the registered custom node type'
          : 'Build succeeded — register this plugin as a custom node type'
      }
      action={
        <Button
          onClick={() =>
            navigate(
              `/node-designer/plugins/${encodeURIComponent(pluginId)}/register?version=${version}`
            )
          }
        >
          {registered ? 'Update node type' : 'Register node type'}
        </Button>
      }
    >
      {registered ? (
        <>
          This plugin already backs the node type <code>{existingTypeId}</code>.
          Updating saves your changes as a new version of that node type.
        </>
      ) : (
        <>
          Declare the node's ports, parameters, and element mapping to make it
          available in the Workflow Builder palette once the plugin is promoted.
        </>
      )}
    </Alert>
  );
}
