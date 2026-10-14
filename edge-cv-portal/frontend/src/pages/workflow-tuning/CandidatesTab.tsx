/**
 * Candidates tab of the Tuning_Session workspace
 * (quality-prompt-tuning, task 8.3 — Requirements 5.1-5.7).
 *
 * The Baseline_Candidate (the Prompt_Set of the Tunable_Node in the latest
 * Workflow_Definition version) is listed first and read-only (Req 5.1);
 * every other Candidate can be created, named, edited, duplicated and
 * deleted, with `max_tokens` held inside the node type's catalog bounds
 * (Req 5.2, 5.7). While a Candidate is edited the editor shows the exact
 * user message the Invocation_Builder will send — the prompt followed by the
 * Verdict_Instruction — and the exact system text (Req 5.3), with the
 * non-blocking warnings for a token budget below 64 and for an answer format
 * that omits `is_anomalous` (Req 5.4, 5.5). The starter Candidate for
 * two-image inspection is inserted only when the user asks for it (Req 5.6).
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  ColumnLayout,
  FormField,
  Header,
  Input,
  Modal,
  SpaceBetween,
  Spinner,
  Textarea,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import {
  STARTER_CANDIDATE,
  draftWarnings,
  projectMaxTokens,
  projectSystemText,
  projectUserMessage,
} from './promptPreview';
import type {
  CandidatePreviewResponse,
  CandidatePreviewWarning,
  TuningCandidate,
  TuningNodeView,
} from './types';

/** Shown while the editor holds unsaved changes (Requirement 5.3). */
export const DRAFT_PREVIEW_NOTE =
  'Unsaved draft — this is the text the invocation builder will send for it. '
  + 'Save the candidate to have the portal render the preview from the '
  + 'builder itself.';

/** Requirement 5.1: the baseline may not be edited. */
export const BASELINE_READ_ONLY_NOTE =
  'The baseline is the prompt set deployed in the latest workflow version. It '
  + 'is read-only: duplicate it to iterate on it.';

/** Requirement 5.7's confirmation. */
export const DELETE_CANDIDATE_WARNING =
  'Deleting this candidate also deletes its score runs. Every other '
  + 'candidate, score run, sample and label is left unchanged.';

interface Draft {
  name: string;
  prompt: string;
  systemPrompt: string;
  maxTokens: string;
}

const EMPTY_DRAFT: Draft = {
  name: '',
  prompt: '',
  systemPrompt: '',
  maxTokens: '',
};

function draftOf(candidate: TuningCandidate): Draft {
  return {
    name: candidate.name || '',
    prompt: candidate.prompt || '',
    systemPrompt: candidate.systemPrompt || '',
    maxTokens:
      candidate.maxTokens === null || candidate.maxTokens === undefined
        ? ''
        : `${candidate.maxTokens}`,
  };
}

interface CandidatesTabProps {
  sessionId: string;
  node: TuningNodeView | null;
  candidates: TuningCandidate[];
  /** Reload the session so the list, baseline and runs stay in step. */
  onChanged: () => void | Promise<void>;
}

export default function CandidatesTab({
  sessionId,
  node,
  candidates,
  onChanged,
}: CandidatesTabProps) {
  const [activeId, setActiveId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<CandidatePreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<TuningCandidate | null>(null);

  const active = candidates.find((c) => c.candidateId === activeId) || null;
  const editingBaseline = !!active?.isBaseline;

  // Open the first editable Candidate (or the baseline) on arrival.
  useEffect(() => {
    if (activeId || creating || !candidates.length) return;
    const first = candidates.find((c) => !c.isBaseline) || candidates[0];
    setActiveId(first.candidateId);
    setDraft(draftOf(first));
    setDirty(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidates]);

  const loadPreview = useCallback(
    async (candidateId: string) => {
      setPreviewLoading(true);
      try {
        setPreview(
          await apiService.getTuningCandidatePreview(candidateId, sessionId)
        );
      } catch (err) {
        setPreview(null);
        setError(getErrorMessage(err, 'Failed to load the prompt preview'));
      } finally {
        setPreviewLoading(false);
      }
    },
    [sessionId]
  );

  // The authoritative preview comes from the route for the saved Candidate.
  useEffect(() => {
    if (!activeId || creating) {
      setPreview(null);
      return;
    }
    loadPreview(activeId);
  }, [activeId, creating, loadPreview]);

  const select = (candidate: TuningCandidate) => {
    setCreating(false);
    setActiveId(candidate.candidateId);
    setDraft(draftOf(candidate));
    setDirty(false);
    setError(null);
  };

  const startNew = () => {
    setCreating(true);
    setActiveId(null);
    setDraft({ ...EMPTY_DRAFT, name: `Candidate ${candidates.length}` });
    setDirty(true);
    setPreview(null);
    setError(null);
  };

  /** Requirement 5.6: the starter is inserted only on this action. */
  const insertStarter = () => {
    setDraft((current) => ({
      ...current,
      name: current.name || STARTER_CANDIDATE.name,
      prompt: STARTER_CANDIDATE.prompt,
      maxTokens: `${STARTER_CANDIDATE.maxTokens}`,
    }));
    setDirty(true);
  };

  const bounds = node?.maxTokensBounds || { min: 1, max: null };
  const maxTokensValue = draft.maxTokens.trim()
    ? Number(draft.maxTokens.trim())
    : null;
  const maxTokensError = (() => {
    if (maxTokensValue === null) return null;
    if (!Number.isFinite(maxTokensValue) || !Number.isInteger(maxTokensValue)) {
      return 'max_tokens must be a whole number';
    }
    if (maxTokensValue < bounds.min) {
      return `max_tokens must be at least ${bounds.min} for this node type`;
    }
    if (bounds.max !== null && maxTokensValue > bounds.max) {
      return `max_tokens must be at most ${bounds.max} for this node type`;
    }
    return null;
  })();

  const save = async () => {
    if (maxTokensError) return;
    setSaving(true);
    try {
      const body = {
        name: draft.name.trim() || 'Candidate',
        prompt: draft.prompt,
        systemPrompt: draft.systemPrompt.trim() ? draft.systemPrompt : null,
        maxTokens: maxTokensValue,
      };
      if (creating || !activeId) {
        const { candidate } = await apiService.createTuningCandidate(
          sessionId,
          body
        );
        setCreating(false);
        setActiveId(candidate.candidateId);
        setDraft(draftOf(candidate));
      } else {
        const { candidate } = await apiService.updateTuningCandidate(
          sessionId,
          activeId,
          body
        );
        setDraft(draftOf(candidate));
        await loadPreview(candidate.candidateId);
      }
      setDirty(false);
      setError(null);
      await onChanged();
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to save the candidate'));
    } finally {
      setSaving(false);
    }
  };

  /** Requirement 5.2: duplication is a create with the source Prompt_Set. */
  const duplicate = async (candidate: TuningCandidate) => {
    setSaving(true);
    try {
      const { candidate: copy } = await apiService.createTuningCandidate(
        sessionId,
        {
          name: `${candidate.name} (copy)`,
          prompt: candidate.prompt,
          systemPrompt: candidate.systemPrompt,
          maxTokens: candidate.maxTokens,
        }
      );
      setCreating(false);
      setActiveId(copy.candidateId);
      setDraft(draftOf(copy));
      setDirty(false);
      setError(null);
      await onChanged();
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to duplicate the candidate'));
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setSaving(true);
    try {
      await apiService.deleteTuningCandidate(
        sessionId,
        deleteTarget.candidateId
      );
      if (activeId === deleteTarget.candidateId) {
        setActiveId(null);
        setDraft(EMPTY_DRAFT);
        setPreview(null);
      }
      setDeleteTarget(null);
      setError(null);
      await onChanged();
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to delete the candidate'));
    } finally {
      setSaving(false);
    }
  };

  // While the draft is unsaved the preview is projected from the builder's
  // own rules; once saved the route's answer replaces it (Requirement 5.3).
  const showDraftPreview = dirty || creating;
  const userMessage = showDraftPreview
    ? projectUserMessage(draft.prompt)
    : preview?.userMessage ?? '';
  const systemText = showDraftPreview
    ? projectSystemText(draft.systemPrompt)
    : preview?.systemText ?? null;
  const tokenBudget = showDraftPreview
    ? projectMaxTokens(maxTokensValue)
    : preview?.maxTokens ?? null;
  const warnings: CandidatePreviewWarning[] = showDraftPreview
    ? draftWarnings(
        draft.prompt,
        draft.systemPrompt.trim() ? draft.systemPrompt : null,
        maxTokensValue
      )
    : preview?.warnings ?? [];

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <ColumnLayout columns={2}>
        <Container
          header={
            <Header
              variant="h2"
              counter={`(${candidates.length})`}
              actions={
                <Button onClick={startNew} data-testid="new-candidate">
                  New candidate
                </Button>
              }
            >
              Candidates
            </Header>
          }
        >
          <SpaceBetween size="s">
            {!candidates.length && (
              <Box variant="p">This session has no candidate yet.</Box>
            )}
            {candidates.map((candidate) => (
              <Box key={candidate.candidateId}>
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant={
                      candidate.candidateId === activeId ? 'primary' : 'link'
                    }
                    onClick={() => select(candidate)}
                    data-testid={`candidate-${candidate.candidateId}`}
                  >
                    {candidate.name}
                  </Button>
                  {candidate.isBaseline && (
                    <Badge color="grey">
                      {`Baseline · v${candidate.baselineVersion ?? '?'}`}
                    </Badge>
                  )}
                  <Button
                    variant="inline-link"
                    disabled={saving}
                    onClick={() => duplicate(candidate)}
                    data-testid={`duplicate-${candidate.candidateId}`}
                  >
                    Duplicate
                  </Button>
                  {!candidate.isBaseline && (
                    <Button
                      variant="inline-link"
                      disabled={saving}
                      onClick={() => setDeleteTarget(candidate)}
                      data-testid={`delete-${candidate.candidateId}`}
                    >
                      Delete
                    </Button>
                  )}
                </SpaceBetween>
              </Box>
            ))}
          </SpaceBetween>
        </Container>

        <Container
          header={
            <Header
              variant="h2"
              description={
                node
                  ? `Node ${node.nodeId} · ${node.nodeType} · ${node.model || 'default model'}`
                  : undefined
              }
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    onClick={insertStarter}
                    disabled={editingBaseline}
                    data-testid="insert-starter"
                  >
                    Insert starter template
                  </Button>
                  <Button
                    variant="primary"
                    loading={saving}
                    disabled={editingBaseline || !dirty || !!maxTokensError}
                    onClick={save}
                    data-testid="save-candidate"
                  >
                    Save
                  </Button>
                </SpaceBetween>
              }
            >
              {creating ? 'New candidate' : active?.name || 'Editor'}
            </Header>
          }
        >
          <SpaceBetween size="m">
            {editingBaseline && (
              <Alert type="info" data-testid="baseline-read-only">
                {BASELINE_READ_ONLY_NOTE}
              </Alert>
            )}
            <FormField label="Name">
              <Input
                value={draft.name}
                readOnly={editingBaseline}
                onChange={({ detail }) => {
                  setDraft((c) => ({ ...c, name: detail.value }));
                  setDirty(true);
                }}
                data-testid="candidate-name"
              />
            </FormField>
            <FormField
              label={
                node?.nodeType === 'llm_inference'
                  ? 'Prompt template'
                  : 'Prompt'
              }
              description={
                node?.nodeType === 'llm_inference'
                  ? 'Placeholders are rendered against the run metadata on the device; the preview shows the template unrendered.'
                  : undefined
              }
            >
              <Textarea
                value={draft.prompt}
                readOnly={editingBaseline}
                rows={10}
                onChange={({ detail }) => {
                  setDraft((c) => ({ ...c, prompt: detail.value }));
                  setDirty(true);
                }}
                data-testid="candidate-prompt"
              />
            </FormField>
            <FormField
              label="System prompt"
              description="Optional; sent verbatim. Empty means no system text at all."
            >
              <Textarea
                value={draft.systemPrompt}
                readOnly={editingBaseline}
                rows={4}
                onChange={({ detail }) => {
                  setDraft((c) => ({ ...c, systemPrompt: detail.value }));
                  setDirty(true);
                }}
                data-testid="candidate-system-prompt"
              />
            </FormField>
            <FormField
              label="max_tokens"
              description={
                bounds.max === null
                  ? `At least ${bounds.min}; empty uses the node default.`
                  : `Between ${bounds.min} and ${bounds.max}; empty uses the node default.`
              }
              errorText={maxTokensError || undefined}
            >
              <Input
                value={draft.maxTokens}
                type="number"
                readOnly={editingBaseline}
                onChange={({ detail }) => {
                  setDraft((c) => ({ ...c, maxTokens: detail.value }));
                  setDirty(true);
                }}
                data-testid="candidate-max-tokens"
              />
            </FormField>
          </SpaceBetween>
        </Container>
      </ColumnLayout>

      {/* Requirements 5.3-5.5: the live preview and its warnings. */}
      <Container
        header={
          <Header
            variant="h2"
            description="Exactly what the invocation builder will send"
          >
            Preview
          </Header>
        }
      >
        <SpaceBetween size="m">
          {showDraftPreview && (
            <Alert type="info" data-testid="draft-preview-note">
              {DRAFT_PREVIEW_NOTE}
            </Alert>
          )}
          {previewLoading && !showDraftPreview && <Spinner />}
          {warnings.map((warning, index) => (
            <Alert
              key={`${warning.code}-${warning.field ?? index}`}
              type="warning"
              data-testid={`preview-warning-${warning.code}`}
            >
              {warning.message}
            </Alert>
          ))}
          <FormField label="Final user message">
            <Box variant="code" data-testid="preview-user-message">
              <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                {userMessage}
              </pre>
            </Box>
          </FormField>
          <FormField label="System text">
            <Box variant="code" data-testid="preview-system-text">
              <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                {systemText === null
                  ? 'No system text is sent.'
                  : systemText}
              </pre>
            </Box>
          </FormField>
          <ColumnLayout columns={3} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">max_tokens</Box>
              <Box variant="p" data-testid="preview-max-tokens">
                {tokenBudget === null ? '—' : `${tokenBudget}`}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Model</Box>
              <Box variant="p">{preview?.model || node?.model || '—'}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Template rendered</Box>
              <Box variant="p">
                {preview?.templateRendered === false ? 'No (device renders it)' : 'Yes'}
              </Box>
            </div>
          </ColumnLayout>
        </SpaceBetween>
      </Container>

      <Modal
        visible={!!deleteTarget}
        onDismiss={() => setDeleteTarget(null)}
        header="Delete candidate"
        footer={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => setDeleteTarget(null)}>Cancel</Button>
            <Button
              variant="primary"
              loading={saving}
              onClick={confirmDelete}
              data-testid="confirm-delete-candidate"
            >
              Delete
            </Button>
          </SpaceBetween>
        }
      >
        <SpaceBetween size="s">
          <Box variant="p">{`Delete "${deleteTarget?.name}"?`}</Box>
          <Alert type="warning">{DELETE_CANDIDATE_WARNING}</Alert>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
