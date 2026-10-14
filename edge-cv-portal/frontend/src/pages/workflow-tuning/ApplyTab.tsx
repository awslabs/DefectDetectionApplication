/**
 * Apply tab of the Tuning_Session workspace
 * (quality-prompt-tuning, task 8.3 — Requirements 7.6, 8.2, 8.4, 8.5).
 *
 * Applying is a confirmation showing the selected Candidate's and the
 * Baseline_Candidate's Score_Summaries side by side and, prominently, the
 * selection's false-pass count (Req 7.6, 8.2). A Candidate without a
 * completed Score_Run cannot be applied (Req 8.2), and a target node that is
 * no longer a Tunable_Node of the latest version is refused with the reason
 * (Req 8.4). On success the new Workflow_Definition version is stated, with
 * the note that nothing was validated, packaged or deployed (Req 8.5).
 */
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  Header,
  Modal,
  SpaceBetween,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import { falsePassWarning } from './CompareTab';
import type {
  ApplyCandidateResponse,
  ScoreSummary,
  TuningCandidate,
  TuningSession,
} from './types';

/** Requirement 8.2: what an apply needs before it is offered. */
export const NEEDS_COMPLETED_RUN =
  'The selected candidate needs at least one completed score run before it '
  + 'can be applied. Start a run on the Score runs tab.';

/** Requirement 8.4's wording when the node is gone or out of Anomaly_Mode. */
export const NODE_NOT_TUNABLE_MESSAGE =
  'The target node no longer exists in the latest workflow version, or is no '
  + 'longer an anomaly-mode Bedrock or VLM inspection node, so the prompt set '
  + 'cannot be applied.';

/** Requirement 8.5: applying is a save, nothing more. */
export const NOT_DEPLOYED_NOTE =
  'Applying saves a new workflow version with exactly the prompt, system '
  + 'prompt and max_tokens of the candidate. It does not validate, package or '
  + 'deploy anything: deploy the new version from the Deployments page when '
  + 'you are ready.';

function summaryPanel(title: string, summary: ScoreSummary | null | undefined) {
  return (
    <SpaceBetween size="xxs">
      <Box variant="awsui-key-label">{title}</Box>
      {summary ? (
        <SpaceBetween size="xxs">
          <Box variant="p">{`Invocations: ${summary.invocations}`}</Box>
          <Box variant="p">{`Correct: ${summary.correct}`}</Box>
          <Box variant="p">
            {`Accuracy: ${
              summary.accuracy === null || summary.accuracy === undefined
                ? '—'
                : `${Math.round(summary.accuracy * 1000) / 10}%`
            }`}
          </Box>
          <Box variant="p">{`False passes: ${summary.falsePass}`}</Box>
          <Box variant="p">{`False fails: ${summary.falseFail}`}</Box>
          <Box variant="p">{`Parse failures: ${summary.parseFailure}`}</Box>
          <Box variant="p">{`Invocation errors: ${summary.invocationError}`}</Box>
          <Box variant="p">{`Unstable samples: ${summary.unstable}`}</Box>
        </SpaceBetween>
      ) : (
        <Box variant="p">Never scored</Box>
      )}
    </SpaceBetween>
  );
}

interface ApplyTabProps {
  sessionId: string;
  session: TuningSession | null;
  candidates: TuningCandidate[];
  nodeStillTunable: boolean;
  latestVersion: number | null;
  onChanged: () => void | Promise<void>;
}

export default function ApplyTab({
  sessionId,
  session,
  candidates,
  nodeStillTunable,
  latestVersion,
  onChanged,
}: ApplyTabProps) {
  const navigate = useNavigate();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applied, setApplied] = useState<ApplyCandidateResponse | null>(null);

  const selected =
    candidates.find((c) => c.candidateId === session?.selectedCandidateId)
    || null;
  const baseline =
    candidates.find(
      (c) => c.isBaseline || c.candidateId === session?.baselineCandidateId
    ) || null;

  const selectedRun = selected?.latestRun ?? null;
  const hasCompletedRun = selectedRun?.status === 'completed';
  const falsePasses = selectedRun?.summary?.falsePass ?? 0;

  const apply = async () => {
    if (!selected) return;
    setApplying(true);
    try {
      const response = await apiService.applyTuningCandidate(sessionId, {
        candidateId: selected.candidateId,
        ...(selectedRun?.runId ? { runId: selectedRun.runId } : {}),
      });
      setApplied(response);
      setConfirmOpen(false);
      setError(null);
      await onChanged();
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to apply the candidate'));
      setConfirmOpen(false);
    } finally {
      setApplying(false);
    }
  };

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {applied && (
        <Alert
          type="success"
          header={`Version ${applied.newVersion} saved`}
          data-testid="apply-success"
          action={
            <Button
              onClick={() =>
                navigate(`/workflows/builder/${applied.workflowId}`)
              }
            >
              Open in the designer
            </Button>
          }
        >
          <SpaceBetween size="xs">
            <Box variant="p">
              {`Workflow ${applied.workflowId} version ${applied.newVersion} `
                + `carries the prompt set of "${applied.candidate.name}" on node `
                + `${applied.nodeId}; version ${applied.previousVersion} is `
                + 'unchanged.'}
            </Box>
            <Box variant="p">{NOT_DEPLOYED_NOTE}</Box>
          </SpaceBetween>
        </Alert>
      )}

      {!nodeStillTunable && (
        <Alert type="error" data-testid="node-not-tunable">
          {NODE_NOT_TUNABLE_MESSAGE}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h2"
            description={
              latestVersion
                ? `Applying saves version ${latestVersion + 1} of the workflow`
                : undefined
            }
            actions={
              <Button
                variant="primary"
                disabled={!selected || !hasCompletedRun || !nodeStillTunable}
                onClick={() => setConfirmOpen(true)}
                data-testid="open-apply-confirmation"
              >
                Apply selected candidate
              </Button>
            }
          >
            Apply
          </Header>
        }
      >
        <SpaceBetween size="m">
          {!selected && (
            <Alert type="info" data-testid="no-selection">
              Select a candidate on the Compare tab to apply it.
            </Alert>
          )}
          {selected && !hasCompletedRun && (
            <Alert type="warning" data-testid="needs-completed-run">
              {NEEDS_COMPLETED_RUN}
            </Alert>
          )}
          {selected && falsePasses > 0 && (
            <Alert type="warning" data-testid="apply-false-passes">
              {falsePassWarning(falsePasses, selected.name)}
            </Alert>
          )}
          {selected && (
            <ColumnLayout columns={2} variant="text-grid">
              {summaryPanel(
                `Selected: ${selected.name}`,
                selected.latestRun?.summary
              )}
              {summaryPanel(
                `Baseline: ${baseline?.name ?? 'deployed prompt'}`,
                baseline?.latestRun?.summary
              )}
            </ColumnLayout>
          )}
          <Box variant="small">{NOT_DEPLOYED_NOTE}</Box>
          {session?.latestTuningResult && (
            <Alert type="info" data-testid="latest-tuning-result">
              {`The last apply of this session saved version `
                + `${session.latestTuningResult.newVersion} from candidate `
                + `${session.latestTuningResult.candidateName
                  || session.latestTuningResult.candidateId}.`}
            </Alert>
          )}
        </SpaceBetween>
      </Container>

      {/* Requirements 7.6, 8.2: the confirmation with both summaries. */}
      <Modal
        visible={confirmOpen}
        onDismiss={() => setConfirmOpen(false)}
        header="Apply candidate as a new workflow version"
        data-testid="apply-confirmation"
        footer={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => setConfirmOpen(false)}>Cancel</Button>
            <Button
              variant="primary"
              loading={applying}
              onClick={apply}
              data-testid="confirm-apply"
            >
              {latestVersion
                ? `Save version ${latestVersion + 1}`
                : 'Save new version'}
            </Button>
          </SpaceBetween>
        }
      >
        <SpaceBetween size="m">
          <Box variant="p">
            {`Version ${latestVersion ? latestVersion + 1 : 'N+1'} will carry `
              + `the prompt, system prompt and max_tokens of `
              + `"${selected?.name}" on node ${session?.nodeId}. Every other `
              + 'node, parameter and connection stays byte-identical.'}
          </Box>
          {falsePasses > 0 ? (
            <Alert type="warning" data-testid="confirm-false-passes">
              {falsePassWarning(falsePasses, selected?.name ?? 'The candidate')}
            </Alert>
          ) : (
            <Box variant="p" data-testid="confirm-false-passes">
              This candidate&apos;s latest run has no false passes.
            </Box>
          )}
          <ColumnLayout columns={2} variant="text-grid">
            {summaryPanel(
              `Selected: ${selected?.name ?? '—'}`,
              selected?.latestRun?.summary
            )}
            {summaryPanel(
              `Baseline: ${baseline?.name ?? 'deployed prompt'}`,
              baseline?.latestRun?.summary
            )}
          </ColumnLayout>
          <Alert type="info">{NOT_DEPLOYED_NOTE}</Alert>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
