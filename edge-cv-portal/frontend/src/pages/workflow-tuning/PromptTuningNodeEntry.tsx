/**
 * "Prompt tuning" entry point on a Tunable_Node's configuration panel
 * (quality-prompt-tuning, task 8.4 — Requirement 1.4).
 *
 * WHEN a Tunable_Node is selected in the designer, its configuration panel
 * shows a "Prompt tuning" link that opens the node's Tuning_Session,
 * together with the node's latest applied Tuning_Result summary when one
 * exists.
 *
 * The panel is mounted only for a Tunable_Node of a SAVED workflow
 * (`NodeConfigPanel` decides that with `isTunableWorkflowNode` and the
 * loaded workflow id), so this component always has a `(workflowId, nodeId)`
 * pair to work with.
 *
 * Reading the Tuning_Result takes two GETs because no route answers "the
 * result for this node" directly: the overview
 * (`GET /workflow-tuning/anomaly/workflows?workflow_id=`) reports whether the
 * node already has a Tuning_Session and its id, and the session view carries
 * `latestTuningResult` (written by apply, Requirement 8.3). Both are reads;
 * neither creates anything, so merely selecting a node in the designer
 * mutates nothing. A session is created only when the link is followed and
 * the node has none yet — the same create-or-get call the overview page's
 * "Open session" makes (Requirement 10.2 keeps it at most one per pair).
 *
 * Every failure degrades to the plain link: the entry point is a shortcut,
 * so a Use_Case without the tuning surface, a stale designer tab, or a
 * caller the Portal answers 404 to must never break the designer panel.
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { apiService } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import type { ScoreSummary, TuningResult } from './types';

/** Where the section's overview lives, with the workflow preselected. */
export function anomalyTuningHref(workflowId: string): string {
  return `/workflow-tuning/anomaly?workflowId=${encodeURIComponent(workflowId)}`;
}

/** Where a known Tuning_Session's workspace lives. */
export function tuningSessionHref(sessionId: string): string {
  return `/workflow-tuning/anomaly/sessions/${encodeURIComponent(sessionId)}`;
}

/** Shown while the node has never had a Prompt_Set applied. */
export const NEVER_APPLIED_MESSAGE = 'No tuned prompt has been applied to this node yet.';

/** `accuracy` as a percentage, or an em dash for a run with no outcomes. */
function accuracyText(summary: ScoreSummary | null | undefined): string {
  const accuracy = summary?.accuracy;
  if (summary === null || summary === undefined || accuracy === null || accuracy === undefined) {
    return '—';
  }
  return `${Math.round(accuracy * 1000) / 10}%`;
}

/**
 * The Tuning_Result headline: which version the apply produced, from which
 * Candidate, when and by whom (Requirements 1.4, 8.3).
 */
export function formatAppliedLine(result: TuningResult): string {
  const candidate = result.candidateName || result.candidateId;
  const when =
    typeof result.appliedAt === 'number' && Number.isFinite(result.appliedAt)
      ? new Date(result.appliedAt * 1000).toISOString().slice(0, 10)
      : 'an earlier date';
  const by = result.appliedBy ? ` by ${result.appliedBy}` : '';
  return `Applied as version ${result.newVersion} on ${when}${by} from candidate '${candidate}'.`;
}

/**
 * The applied Candidate's score against the Baseline_Candidate's, the one
 * comparison that says whether the applied prompt was actually better
 * (Requirement 7.6's false passes included).
 */
export function formatScoreLine(result: TuningResult): string {
  const applied = result.summary ?? null;
  const baseline = result.baselineSummary ?? null;
  if (applied === null) {
    return 'The applied candidate carries no score summary.';
  }
  const parts = [
    `accuracy ${accuracyText(applied)}`,
    `${applied.falsePass} false pass${applied.falsePass === 1 ? '' : 'es'}`,
    `${applied.invocations} invocation${applied.invocations === 1 ? '' : 's'}`,
  ];
  const versus =
    baseline === null
      ? ''
      : ` — baseline: accuracy ${accuracyText(baseline)}, ${baseline.falsePass} false pass${
          baseline.falsePass === 1 ? '' : 'es'
        }`;
  return `Scored ${parts.join(', ')}${versus}.`;
}

export interface PromptTuningNodeEntryProps {
  /** The loaded workflow's id (the panel mounts this only when saved). */
  workflowId: string;
  /** The selected Tunable_Node's id. */
  nodeId: string;
  /** The selected Use_Case, needed by the overview read. */
  usecaseId: string | null;
}

export default function PromptTuningNodeEntry({
  workflowId,
  nodeId,
  usecaseId,
}: PromptTuningNodeEntryProps) {
  const navigate = useNavigate();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [result, setResult] = useState<TuningResult | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setSessionId(null);
    setResult(null);
    setLoaded(false);
    if (!usecaseId) {
      return undefined;
    }
    (async () => {
      try {
        const overview = await apiService.listTuningWorkflows(usecaseId, workflowId);
        const node = (overview.workflows ?? [])
          .filter((workflow) => workflow.workflowId === workflowId)
          .flatMap((workflow) => workflow.nodes ?? [])
          .find((candidate) => candidate.nodeId === nodeId);
        const existing = node?.sessionId ?? null;
        if (cancelled) return;
        setSessionId(existing);
        setLoaded(true);
        if (existing === null) {
          return;
        }
        const session = await apiService.getTuningSession(existing);
        if (cancelled) return;
        setResult(session.session?.latestTuningResult ?? null);
      } catch {
        // The link stays usable; nothing about the node's configuration
        // depends on the tuning surface being reachable.
        if (!cancelled) {
          setLoaded(true);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workflowId, nodeId, usecaseId]);

  /**
   * Open the node's Tuning_Session: navigate straight to it when the node
   * already has one, else create-or-get it first (Requirement 10.2). A
   * failure (e.g. the node is no longer tunable in the latest SAVED version,
   * which the canvas cannot know) falls back to the section overview with
   * this workflow preselected, so the user is never stranded.
   */
  const openSession = async () => {
    setError(null);
    if (sessionId !== null) {
      navigate(tuningSessionHref(sessionId));
      return;
    }
    setOpening(true);
    try {
      const { session } = await apiService.createTuningSession({
        workflow_id: workflowId,
        node_id: nodeId,
      });
      setSessionId(session.sessionId);
      navigate(tuningSessionHref(session.sessionId));
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to open the tuning session'));
      navigate(anomalyTuningHref(workflowId));
    } finally {
      setOpening(false);
    }
  };

  return (
    <div data-testid="prompt-tuning-entry">
      <SpaceBetween size="xxs">
        <Button variant="link" onClick={openSession} loading={opening}>
          Prompt tuning
        </Button>
        {result !== null ? (
          <Box fontSize="body-s" color="text-body-secondary">
            <SpaceBetween size="xxxs">
              <div data-testid="prompt-tuning-applied">{formatAppliedLine(result)}</div>
              <div data-testid="prompt-tuning-score">{formatScoreLine(result)}</div>
            </SpaceBetween>
          </Box>
        ) : (
          loaded && (
            <Box fontSize="body-s" color="text-body-secondary">
              {NEVER_APPLIED_MESSAGE}
            </Box>
          )
        )}
        {error !== null && (
          <Box fontSize="body-s" color="text-status-error">
            {error}
          </Box>
        )}
      </SpaceBetween>
    </div>
  );
}
