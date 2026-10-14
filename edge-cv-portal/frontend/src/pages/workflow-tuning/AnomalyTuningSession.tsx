/**
 * Tuning_Session workspace (quality-prompt-tuning, task 8.3).
 *
 * The five tabs of one Tunable_Node's tuning loop, over the session the
 * overview's "Open session" created:
 *
 * - **Samples** — pair cards, labels, multi-select, filters, counts and the
 *   Synthetic_Negatives toggle (Requirements 4.1-4.8).
 * - **Candidates** — the editor with the live preview of the exact text the
 *   Invocation_Builder will send, its warnings and the starter template; the
 *   Baseline_Candidate is read-only (Requirements 5.1-5.7).
 * - **Score runs** — the start dialog stating the invocation count, the
 *   repeats and, for a VLM node, the device that will execute the run, plus
 *   progress, the running summary and cancellation (Requirements 6.6-6.13).
 * - **Compare** — the latest-run-per-candidate table, the outcome
 *   drill-down, the two-run diff, the parse-failure raw answers, the
 *   prominent false-pass count and the selection (Requirements 7.1-7.6).
 * - **Apply** — the confirmation carrying both Score_Summaries and the
 *   false-pass count; success states the new version (Requirements 8.2, 8.4).
 *
 * The page owns the session document (`GET .../sessions/{id}`) and hands it
 * plus a `reload` to the tabs, so a Label change, a Candidate edit, a
 * finished run or an apply is reflected everywhere without a page reload.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Badge,
  Button,
  ContentLayout,
  Header,
  SpaceBetween,
  Spinner,
  Tabs,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import SamplesTab from './SamplesTab';
import CandidatesTab from './CandidatesTab';
import ScoreRunsTab from './ScoreRunsTab';
import CompareTab from './CompareTab';
import ApplyTab from './ApplyTab';
import type { LabelCounts, TuningSessionResponse } from './types';

/** Requirement 1.6, restated where a run is started from. */
export const EXPORT_DISABLED_NOTE =
  'Tuning sample export is disabled for this use case, so devices are adding '
  + 'no new samples. Existing samples were exported earlier and are still '
  + 'usable.';

export default function AnomalyTuningSession() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();

  const [data, setData] = useState<TuningSessionResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshNote, setRefreshNote] = useState<string | null>(null);
  const [activeTabId, setActiveTabId] = useState('samples');
  const [devicesSeen, setDevicesSeen] = useState<string[]>([]);

  const reload = useCallback(async () => {
    if (!sessionId) return;
    try {
      const response = await apiService.getTuningSession(sessionId);
      setData(response);
      setError(null);
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to load the tuning session'));
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    reload();
  }, [reload]);

  /** Requirement 3.4: an additive re-index that keeps existing Labels. */
  const refreshSamples = async () => {
    if (!sessionId) return;
    setRefreshing(true);
    try {
      const response = await apiService.refreshTuningSession(sessionId);
      const skipped = Object.entries(response.refresh.skipped || {})
        .map(([reason, count]) => `${count} ${reason}`)
        .join(', ');
      setRefreshNote(
        `Indexed ${response.refresh.indexed} new sample`
        + `${response.refresh.indexed === 1 ? '' : 's'}`
        + (skipped ? `; skipped ${skipped}` : '')
        + (response.refresh.error ? `; ${response.refresh.error}` : '')
      );
      setError(null);
      await reload();
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to refresh the samples'));
    } finally {
      setRefreshing(false);
    }
  };

  const setCounts = useCallback(
    (counts: LabelCounts) =>
      setData((current) => (current ? { ...current, labelCounts: counts } : current)),
    []
  );

  const setSynthetic = useCallback(
    (enabled: boolean) =>
      setData((current) =>
        current
          ? {
              ...current,
              session: {
                ...current.session,
                syntheticNegativesEnabled: enabled,
              },
            }
          : current
      ),
    []
  );

  if (loading) {
    return (
      <ContentLayout header={<Header variant="h1">Anomaly tuning session</Header>}>
        <Spinner />
      </ContentLayout>
    );
  }

  if (!data) {
    return (
      <ContentLayout header={<Header variant="h1">Anomaly tuning session</Header>}>
        <SpaceBetween size="m">
          <Alert type="error" data-testid="session-load-error">
            {error || 'This tuning session could not be loaded.'}
          </Alert>
          <Button onClick={() => navigate('/workflow-tuning/anomaly')}>
            Back to the overview
          </Button>
        </SpaceBetween>
      </ContentLayout>
    );
  }

  const { session, node, labelCounts, candidates, nodeStillTunable } = data;

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={
            `Workflow ${session.workflowId} · node ${session.nodeId}`
            + (node ? ` · ${node.nodeType}` : '')
            + (node?.model ? ` · ${node.model}` : '')
          }
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                onClick={() => navigate('/workflow-tuning/anomaly')}
                data-testid="back-to-overview"
              >
                Overview
              </Button>
              <Button
                iconName="refresh"
                loading={refreshing}
                onClick={refreshSamples}
                data-testid="refresh-samples"
              >
                Refresh samples
              </Button>
            </SpaceBetween>
          }
        >
          <SpaceBetween direction="horizontal" size="xs">
            <span>Anomaly tuning</span>
            {data.latestVersion !== null && (
              <Badge color="grey">{`Latest version ${data.latestVersion}`}</Badge>
            )}
            {session.baselineVersion !== null && (
              <Badge color="blue">{`Baseline v${session.baselineVersion}`}</Badge>
            )}
          </SpaceBetween>
        </Header>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}
        {refreshNote && (
          <Alert
            type="info"
            dismissible
            onDismiss={() => setRefreshNote(null)}
            data-testid="refresh-note"
          >
            {refreshNote}
          </Alert>
        )}
        {!data.sampleExportEnabled && (
          <Alert type="warning" data-testid="session-export-disabled">
            {EXPORT_DISABLED_NOTE}
          </Alert>
        )}
        {!nodeStillTunable && (
          <Alert type="warning" data-testid="session-node-missing">
            The tuned node is not in the latest workflow version any more.
            Samples, candidates and past runs stay readable, but the prompt set
            cannot be applied.
          </Alert>
        )}
        {session.lastRefresh?.error && (
          <Alert type="warning" data-testid="session-refresh-error">
            {`The sample store could not be read on the last refresh: ${session.lastRefresh.error}`}
          </Alert>
        )}

        <Tabs
          activeTabId={activeTabId}
          onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
          data-testid="session-tabs"
          tabs={[
            {
              id: 'samples',
              label: 'Samples',
              content: sessionId ? (
                <SamplesTab
                  sessionId={sessionId}
                  syntheticEnabled={session.syntheticNegativesEnabled}
                  labelCounts={labelCounts}
                  onCountsChange={setCounts}
                  onDevicesSeen={setDevicesSeen}
                  onSyntheticChange={setSynthetic}
                />
              ) : null,
            },
            {
              id: 'candidates',
              label: 'Candidates',
              content: sessionId ? (
                <CandidatesTab
                  sessionId={sessionId}
                  node={node}
                  candidates={candidates}
                  onChanged={reload}
                />
              ) : null,
            },
            {
              id: 'runs',
              label: 'Score runs',
              content: sessionId ? (
                <ScoreRunsTab
                  sessionId={sessionId}
                  node={node}
                  candidates={candidates}
                  labelCounts={labelCounts}
                  devicesSeen={devicesSeen}
                  onChanged={reload}
                />
              ) : null,
            },
            {
              id: 'compare',
              label: 'Compare',
              content: sessionId ? (
                <CompareTab
                  sessionId={sessionId}
                  candidates={candidates}
                  selectedCandidateId={session.selectedCandidateId}
                  onChanged={reload}
                />
              ) : null,
            },
            {
              id: 'apply',
              label: 'Apply',
              content: sessionId ? (
                <ApplyTab
                  sessionId={sessionId}
                  session={session}
                  candidates={candidates}
                  nodeStillTunable={nodeStillTunable}
                  latestVersion={data.latestVersion}
                  onChanged={reload}
                />
              ) : null,
            },
          ]}
        />
      </SpaceBetween>
    </ContentLayout>
  );
}
