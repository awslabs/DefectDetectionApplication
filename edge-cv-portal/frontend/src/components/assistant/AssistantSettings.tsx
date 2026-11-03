/**
 * The Settings tab of the assistant (spec portal-ai-assistant Requirement 12;
 * design §12): the on/off toggle, the Actions_Switch (hidden until actions
 * are delivered), the retention period, the guardrail, the model and its
 * tool-use flag, the limits.
 */
import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, ColumnLayout, Container, FormField, Header, Input, SpaceBetween, StatusIndicator, Toggle,
} from '@cloudscape-design/components';
import { useAuth } from '../../contexts/AuthContext';
import { assistantApi } from './assistantApi';
import type { AssistantSettingsView } from './assistantApi';

function describeError(err: unknown): string {
  return (err as { message?: string }).message || 'The request failed';
}

export default function AssistantSettings() {
  const { user } = useAuth();
  const isPortalAdmin = user?.role === 'PortalAdmin';
  const [view, setView] = useState<AssistantSettingsView | null>(null);
  const [retention, setRetention] = useState('30');
  const [guardrailId, setGuardrailId] = useState('');
  const [guardrailVersion, setGuardrailVersion] = useState('');
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [saving, setSaving] = useState(false);

  const load = async () => {
    try {
      const settings = await assistantApi.settings();
      setView(settings);
      setRetention(String(settings.retention_days));
      setGuardrailId(settings.guardrail_id ?? '');
      setGuardrailVersion(settings.guardrail_version ?? '');
      setError('');
    } catch (err) {
      setError(describeError(err));
    }
  };

  useEffect(() => {
    if (isPortalAdmin) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPortalAdmin]);

  if (!isPortalAdmin) {
    return <Alert type="info">Only PortalAdmins can change the assistant settings.</Alert>;
  }

  const update = async (changes: Parameters<typeof assistantApi.updateSettings>[0], message: string) => {
    setSaving(true);
    setError('');
    setSuccess('');
    try {
      const updated = await assistantApi.updateSettings(changes);
      setView(updated);
      setRetention(String(updated.retention_days));
      setGuardrailId(updated.guardrail_id ?? '');
      setGuardrailVersion(updated.guardrail_version ?? '');
      setSuccess(message);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  };

  const retentionValue = Number(retention);
  const retentionValid = Number.isInteger(retentionValue) && retentionValue >= 1 && retentionValue <= 365;
  const guardrailValid = (guardrailId === '' || /^[a-z0-9]{1,64}$/.test(guardrailId))
    && (guardrailVersion === '' || /^(DRAFT|[0-9]{1,8})$/.test(guardrailVersion));

  return (
    <Container header={<Header variant="h2" description="The in-Portal assistant: who can use it and how long its conversations are kept.">Assistant</Header>}>
      <SpaceBetween size="l">
        {error && <Alert type="error">{error}</Alert>}
        {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}
        {view === null && !error && <StatusIndicator type="loading">Loading</StatusIndicator>}
        {view && (
          <>
            <FormField label="Assistant" description="Off hides the panel for every user within a minute and stops every running turn.">
              <Toggle
                checked={view.enabled}
                disabled={saving}
                onChange={({ detail }) => void update({ enabled: detail.checked }, detail.checked ? 'Assistant enabled' : 'Assistant disabled')}
                data-testid="assistant-enabled-toggle"
              >
                {view.enabled ? 'Enabled' : 'Disabled'}
              </Toggle>
            </FormField>
            {view.actions_delivered && (
              <FormField label="Actions" description="Lets the assistant propose state-changing tools; each proposal still needs the user's confirmation.">
                <Toggle
                  checked={view.actions_enabled}
                  disabled={saving}
                  onChange={({ detail }) => void update({ actions_enabled: detail.checked }, detail.checked ? 'Actions enabled' : 'Actions disabled')}
                  data-testid="assistant-actions-toggle"
                >
                  {view.actions_enabled ? 'Enabled' : 'Disabled'}
                </Toggle>
              </FormField>
            )}
            <ColumnLayout columns={2}>
              <FormField label="Conversation retention (days)" description="Inactive conversations are deleted after this many days (1 to 365)."
                errorText={retentionValid ? undefined : 'Enter a whole number between 1 and 365'}>
                <Input type="number" value={retention} onChange={({ detail }) => setRetention(detail.value)} data-testid="assistant-retention" />
              </FormField>
              <FormField label="Model" description="From the Bedrock configuration tab.">
                <Box>
                  <code>{view.model.model_id}</code>
                  <Box color="text-body-secondary" fontSize="body-s">{view.model.region}</Box>
                  {view.model.tool_use_supported === false && (
                    <StatusIndicator type="warning">This model rejected tool definitions: guidance only.</StatusIndicator>
                  )}
                  {view.model.tool_use_supported === true && <StatusIndicator type="success">Tool use verified</StatusIndicator>}
                  {view.model.tool_use_supported === null && <StatusIndicator type="pending">Tool use not checked yet</StatusIndicator>}
                </Box>
              </FormField>
              <FormField label="Bedrock guardrail id (optional)" description="Applied to every model call when set.">
                <Input value={guardrailId} onChange={({ detail }) => setGuardrailId(detail.value.trim())} placeholder="none" />
              </FormField>
              <FormField label="Guardrail version (optional)" description="DRAFT or a version number.">
                <Input value={guardrailVersion} onChange={({ detail }) => setGuardrailVersion(detail.value.trim())} placeholder="DRAFT" />
              </FormField>
            </ColumnLayout>
            <Button
              variant="primary"
              loading={saving}
              disabled={!retentionValid || !guardrailValid}
              onClick={() => void update({
                retention_days: retentionValue,
                guardrail_id: guardrailId || null,
                guardrail_version: guardrailVersion || null,
              }, 'Assistant settings saved')}
              data-testid="assistant-save"
            >
              Save
            </Button>
            <FormField label="Limits" description="Per user and per turn; set by the deployment (ASSISTANT_LIMITS_JSON).">
              <Box fontSize="body-s">
                {Object.entries(view.limits).map(([key, value]) => (
                  <div key={key}><code>{key}</code>: {value}</div>
                ))}
              </Box>
            </FormField>
            {view.updated_at && (
              <Box color="text-body-secondary" fontSize="body-s">
                Last changed {new Date(view.updated_at).toLocaleString()} by {view.updated_by}
              </Box>
            )}
          </>
        )}
      </SpaceBetween>
    </Container>
  );
}
