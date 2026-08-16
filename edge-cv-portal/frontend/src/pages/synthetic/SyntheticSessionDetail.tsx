/**
 * Generation_Session review workspace
 * (synthetic-defect-data-generation, task 7.3).
 *
 * - Polling-driven progress indicator while generating (Req 5.1)
 * - Incremental thumbnail grid from poll responses, no reload (Req 5.2)
 * - Inline prompt editor + regenerate with the edited prompt (Req 5.3)
 * - Pass-tagged comparison of regenerated results (Req 5.4)
 * - Full-size lightbox showing the prompt text used for that preview
 *   (Req 5.5, 5.6)
 * - Per-thumbnail and bulk approve/reject (Req 6.1, 6.2)
 * - Approval confirmation dialog with count / target dataset / Defect_Type
 *   (Req 6.4) and the zero-approved rejection message (Req 6.5)
 * - Integration banner with the manifest URI + appended count and a
 *   "Start retraining" action pre-populated with the manifest URI
 *   (Req 7.6, 8.1); training failures surface the reason while the
 *   manifest URI stays available for retry (Req 8.4)
 * - Session and per-variation failure reasons displayed (Req 1.4, 4.5)
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  Modal,
  ProgressBar,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Textarea,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import type {
  SyntheticIntegrateResponse,
  SyntheticPreview,
  SyntheticSession,
} from './types';

/** Zero-approved rejection message (Req 6.5). */
export const ZERO_APPROVED_MESSAGE = 'At least one approved image is required';

/** Poll cadence while the session is generating (Req 5.1, 5.2). */
export const POLL_INTERVAL_MS = 2000;

export default function SyntheticSessionDetail() {
  const { sessionId } = useParams<{ sessionId: string }>();

  const [session, setSession] = useState<SyntheticSession | null>(null);
  const [previews, setPreviews] = useState<SyntheticPreview[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Prompt editor (Req 5.3).
  const [promptText, setPromptText] = useState('');
  const promptInitialized = useRef(false);
  const [regenerating, setRegenerating] = useState(false);

  // Lightbox (Req 5.5, 5.6).
  const [lightbox, setLightbox] = useState<SyntheticPreview | null>(null);

  // Approval + integration (Req 6.1-6.5, 7.6).
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [integrating, setIntegrating] = useState(false);
  const [integrateResult, setIntegrateResult] =
    useState<SyntheticIntegrateResponse | null>(null);

  // Retraining (Req 8.1-8.4).
  const [retrainOpen, setRetrainOpen] = useState(false);
  const [retrainModelName, setRetrainModelName] = useState('');
  const [retrainBusy, setRetrainBusy] = useState(false);
  const [retrainError, setRetrainError] = useState<string | null>(null);
  const [retrainDone, setRetrainDone] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    try {
      const { session: meta, previews: items } =
        await apiService.getSyntheticSession(sessionId);
      setSession(meta);
      setPreviews(items);
      if (!promptInitialized.current && meta.prompt_template_text) {
        setPromptText(meta.prompt_template_text);
        promptInitialized.current = true;
      }
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to load session'));
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Poll while generating so thumbnails appear incrementally without a
  // page reload (Req 5.1, 5.2).
  const generating = session?.status === 'generating';
  useEffect(() => {
    if (!generating) return;
    const timer = setInterval(() => {
      refresh();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [generating, refresh]);

  const currentPass = session?.generation_pass ?? 0;
  const taskCount = session?.generation_plan?.length ?? 0;
  const currentPassPreviews = useMemo(
    () => previews.filter((p) => (p.generation_pass ?? 0) === currentPass),
    [previews, currentPass]
  );
  const progressPercent =
    taskCount > 0
      ? Math.min(100, Math.round((currentPassPreviews.length / taskCount) * 100))
      : 0;

  const completedPreviews = useMemo(
    () => previews.filter((p) => p.status === 'completed'),
    [previews]
  );
  const failedPreviews = useMemo(
    () => previews.filter((p) => p.status === 'failed'),
    [previews]
  );
  const approvedCount = completedPreviews.filter(
    (p) => p.approval_state === 'approved'
  ).length;

  // ------------------------------------------------------------- actions

  const regenerate = async () => {
    if (!sessionId) return;
    try {
      setRegenerating(true);
      setError(null);
      // Regeneration uses the edited prompt (Req 5.3).
      await apiService.generateSyntheticPreviews(sessionId, {
        prompt_template_text: promptText,
      });
      await refresh();
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to start generation'));
    } finally {
      setRegenerating(false);
    }
  };

  const setApproval = async (
    approval: 'approved' | 'rejected',
    previewIds?: string[]
  ) => {
    if (!sessionId) return;
    try {
      setApprovalBusy(true);
      setError(null);
      await apiService.setSyntheticPreviewApproval(
        sessionId,
        previewIds
          ? { approval_state: approval, preview_ids: previewIds }
          : { approval_state: approval, all: true }
      );
      await refresh();
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to update approval'));
    } finally {
      setApprovalBusy(false);
    }
  };

  const openIntegrateConfirmation = () => {
    // Zero approved previews rejects the confirmation (Req 6.5).
    if (approvedCount === 0) {
      setError(ZERO_APPROVED_MESSAGE);
      return;
    }
    setConfirmOpen(true);
  };

  const integrate = async () => {
    if (!sessionId) return;
    try {
      setIntegrating(true);
      setError(null);
      const result = await apiService.integrateSyntheticSession(sessionId);
      setIntegrateResult(result);
      setConfirmOpen(false);
      await refresh();
    } catch (err) {
      setError(getErrorMessage(err, 'Integration failed'));
      setConfirmOpen(false);
      await refresh();
    } finally {
      setIntegrating(false);
    }
  };

  const retrain = async () => {
    if (!sessionId || !retrainModelName.trim()) return;
    try {
      setRetrainBusy(true);
      setRetrainError(null);
      const result = await apiService.retrainSyntheticSession(sessionId, {
        model_name: retrainModelName.trim(),
        model_version: '1.0.0',
        model_type: 'classification',
        model_source: 'marketplace',
        instance_type: 'ml.g4dn.2xlarge',
        max_runtime_seconds: 14400,
      });
      setRetrainDone(result.training_job_id);
      setRetrainOpen(false);
    } catch (err) {
      // The failure reason is displayed while the manifest URI stays
      // available so the user can retry (Req 8.4).
      setRetrainError(getErrorMessage(err, 'Failed to create training job'));
    } finally {
      setRetrainBusy(false);
    }
  };

  // -------------------------------------------------------------- render

  if (loading && !session) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (!session) {
    return <Alert type="error">{error ?? 'Session not found'}</Alert>;
  }

  const manifestUri =
    integrateResult?.manifest_uri ?? session.integration_result?.manifest_uri;
  const appendedCount =
    integrateResult?.appended_count ?? session.integration_result?.appended_count;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description={`Model: ${session.generation_model_id || '—'} · Object: ${
              session.object_type || '—'
            } · Defect: ${session.defect_type || '—'} · Status: ${session.status}`}
          >
            Generation session {session.session_id.slice(0, 8)}
          </Header>
        }
      >
        <SpaceBetween size="m">
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}

          {/* Generation-request failure reason (Req 1.4). */}
          {session.last_failure && (
            <Alert type="error" header="Generation failure">
              {session.last_failure.reason}
            </Alert>
          )}

          {/* Progress indicator while generating (Req 5.1). */}
          {generating && (
            <ProgressBar
              value={progressPercent}
              label="Generating previews"
              description={`${currentPassPreviews.length} of ${taskCount} variations completed (pass ${currentPass})`}
              status="in-progress"
            />
          )}

          {/* Integration confirmation banner (Req 7.6) + retraining (Req 8.1). */}
          {manifestUri !== undefined && (
            <Alert
              type="success"
              header="Integration complete"
              action={
                <Button
                  onClick={() => {
                    setRetrainOpen(true);
                    setRetrainError(null);
                  }}
                >
                  Start retraining
                </Button>
              }
            >
              Appended {appendedCount} record{appendedCount === 1 ? '' : 's'} to{' '}
              <code>{manifestUri}</code>
            </Alert>
          )}
          {retrainError && (
            <Alert type="error" header="Training job creation failed">
              {retrainError} — the updated manifest{' '}
              <code>{manifestUri}</code> is retained; you can retry.
            </Alert>
          )}
          {retrainDone && (
            <Alert type="success">
              Training job {retrainDone} created from this session.
            </Alert>
          )}
        </SpaceBetween>
      </Container>

      {/* Inline prompt editor + regenerate (Req 5.3). */}
      <Container header={<Header variant="h2">Prompt</Header>}>
        <SpaceBetween size="m">
          <FormField
            label="Prompt template"
            description="Edit the prompt and regenerate to compare results against the prior pass"
            stretch
          >
            <Textarea
              value={promptText}
              onChange={({ detail }) => setPromptText(detail.value)}
              rows={4}
              ariaLabel="Prompt template"
            />
          </FormField>
          <Button
            variant="primary"
            onClick={regenerate}
            loading={regenerating}
            disabled={generating}
          >
            {session.status === 'draft' ? 'Start generation' : 'Regenerate'}
          </Button>
        </SpaceBetween>
      </Container>

      {/* Review workspace (Req 5.2, 5.4, 6.1, 6.2). */}
      <Container
        header={
          <Header
            variant="h2"
            counter={`(${completedPreviews.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  onClick={() => setApproval('approved')}
                  disabled={approvalBusy || completedPreviews.length === 0}
                >
                  Approve all
                </Button>
                <Button
                  onClick={() => setApproval('rejected')}
                  disabled={approvalBusy || completedPreviews.length === 0}
                >
                  Reject all
                </Button>
                <Button
                  variant="primary"
                  onClick={openIntegrateConfirmation}
                  disabled={approvalBusy || session.status === 'integrated'}
                >
                  Integrate approved images
                </Button>
              </SpaceBetween>
            }
          >
            Previews
          </Header>
        }
      >
        <SpaceBetween size="m">
          {completedPreviews.length === 0 && !generating && (
            <Box color="text-status-inactive">
              No previews yet — start generation above.
            </Box>
          )}

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px' }}>
            {completedPreviews.map((preview) => (
              <div
                key={preview.preview_id}
                data-testid={`preview-${preview.preview_id}`}
                style={{
                  border: '1px solid #d5dbdb',
                  borderRadius: 6,
                  padding: 6,
                  width: 172,
                }}
              >
                <SpaceBetween size="xxs">
                  <button
                    type="button"
                    aria-label={`View preview ${preview.preview_id}`}
                    onClick={() => setLightbox(preview)}
                    style={{
                      border: 'none',
                      background: 'none',
                      padding: 0,
                      cursor: 'pointer',
                    }}
                  >
                    {preview.thumbnail_url ? (
                      <img
                        src={preview.thumbnail_url}
                        alt={`Preview ${preview.preview_id}`}
                        style={{
                          width: 160,
                          height: 120,
                          objectFit: 'cover',
                          display: 'block',
                          borderRadius: 4,
                        }}
                      />
                    ) : (
                      <Box>Preview unavailable</Box>
                    )}
                  </button>
                  <SpaceBetween direction="horizontal" size="xxs">
                    {/* Pass tag: compare regenerated results against the
                        prior prompt (Req 5.4). */}
                    <Badge color="grey">Pass {preview.generation_pass ?? 1}</Badge>
                    {preview.approval_state === 'approved' && (
                      <Badge color="green">Approved</Badge>
                    )}
                    {preview.approval_state === 'rejected' && (
                      <Badge color="red">Rejected</Badge>
                    )}
                  </SpaceBetween>
                  <SpaceBetween direction="horizontal" size="xxs">
                    <Button
                      variant="inline-link"
                      onClick={() => setApproval('approved', [preview.preview_id])}
                      disabled={approvalBusy}
                      ariaLabel={`Approve preview ${preview.preview_id}`}
                    >
                      Approve
                    </Button>
                    <Button
                      variant="inline-link"
                      onClick={() => setApproval('rejected', [preview.preview_id])}
                      disabled={approvalBusy}
                      ariaLabel={`Reject preview ${preview.preview_id}`}
                    >
                      Reject
                    </Button>
                  </SpaceBetween>
                </SpaceBetween>
              </div>
            ))}
          </div>

          {/* Per-variation failure reasons (Req 4.5). */}
          {failedPreviews.length > 0 && (
            <Alert
              type="warning"
              header={`${failedPreviews.length} variation${
                failedPreviews.length === 1 ? '' : 's'
              } failed`}
            >
              <ul style={{ margin: 0, paddingLeft: '20px' }}>
                {failedPreviews.map((preview) => (
                  <li key={preview.preview_id}>
                    {preview.source_image_key} (variation{' '}
                    {preview.variation_index}):{' '}
                    {preview.failure_reason || 'Unknown failure'}
                  </li>
                ))}
              </ul>
            </Alert>
          )}
        </SpaceBetween>
      </Container>

      {/* Full-size lightbox with the retained prompt text (Req 5.5, 5.6). */}
      <Modal
        visible={lightbox !== null}
        onDismiss={() => setLightbox(null)}
        size="large"
        header="Preview"
      >
        {lightbox && (
          <SpaceBetween size="m">
            {lightbox.thumbnail_url && (
              <img
                src={lightbox.thumbnail_url}
                alt={`Full-size preview ${lightbox.preview_id}`}
                style={{ maxWidth: '100%', display: 'block' }}
              />
            )}
            <ColumnLayout columns={2} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Prompt used</Box>
                <Box data-testid="lightbox-prompt">{lightbox.resolved_prompt}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Details</Box>
                <Box>
                  Pass {lightbox.generation_pass ?? 1} · Variation{' '}
                  {lightbox.variation_index ?? 0} · Seed {lightbox.seed ?? '—'} ·{' '}
                  {lightbox.generation_method ?? '—'}
                </Box>
              </div>
            </ColumnLayout>
          </SpaceBetween>
        )}
      </Modal>

      {/* Approval confirmation summary (Req 6.4). Mounted only while open
          so the summary values are queryable iff the dialog is shown. */}
      {confirmOpen && (
      <Modal
        visible={confirmOpen}
        onDismiss={() => setConfirmOpen(false)}
        header="Confirm integration"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setConfirmOpen(false)} disabled={integrating}>
                Cancel
              </Button>
              <Button variant="primary" onClick={integrate} loading={integrating}>
                Integrate
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Box>
            <Box variant="awsui-key-label">Approved images</Box>
            <Box data-testid="confirm-approved-count">{approvedCount}</Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">Target dataset</Box>
            <Box data-testid="confirm-target-dataset">
              {session.target_dataset_prefix || '—'}
            </Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">Defect type</Box>
            <Box data-testid="confirm-defect-type">{session.defect_type || '—'}</Box>
          </Box>
          <Alert type="info">
            Rejected and pending previews are excluded from the dataset and the
            manifest.
          </Alert>
        </SpaceBetween>
      </Modal>
      )}

      {/* Retraining action pre-populated with the manifest URI (Req 8.1). */}
      {retrainOpen && (
      <Modal
        visible={retrainOpen}
        onDismiss={() => setRetrainOpen(false)}
        header="Start retraining"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setRetrainOpen(false)} disabled={retrainBusy}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={retrain}
                loading={retrainBusy}
                disabled={!retrainModelName.trim()}
              >
                Create training job
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <FormField
            label="Dataset manifest"
            description="Pre-populated with the updated Data_Manifest from this session"
            stretch
          >
            <Input
              value={manifestUri ?? ''}
              disabled
              ariaLabel="Dataset manifest"
            />
          </FormField>
          <FormField label="Model name" stretch>
            <Input
              value={retrainModelName}
              onChange={({ detail }) => setRetrainModelName(detail.value)}
              placeholder="e.g. defect-detector-synthetic"
              ariaLabel="Model name"
            />
          </FormField>
          <StatusIndicator type="info">
            The training job records this generation session as its origin.
          </StatusIndicator>
        </SpaceBetween>
      </Modal>
      )}
    </SpaceBetween>
  );
}
