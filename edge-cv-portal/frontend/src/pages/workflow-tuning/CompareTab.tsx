/**
 * Compare tab of the Tuning_Session workspace
 * (quality-prompt-tuning, task 8.3 — Requirements 7.1-7.6).
 *
 * One row per Candidate's most recent Score_Run with the Score_Summary
 * fields, the Baseline_Candidate's row among them once it has been scored
 * (Req 7.1); a drill-down listing every Sample_Outcome with the sample's
 * images, Label, category, verdict, confidence and raw answer, filterable by
 * category and sortable by confidence (Req 7.2); a two-run diff naming the
 * samples whose categories differ (Req 7.3); the raw answer and rejection
 * reason of each `parse_failure` character-for-character (Req 7.4); and the
 * single selection, whose false passes are stated prominently because a
 * false pass ships a defective part (Req 7.5, 7.6).
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Select,
  SelectProps,
  SpaceBetween,
  Spinner,
  Table,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import type {
  OutcomeCategory,
  SampleOutcomeView,
  ScoreRunDiffResponse,
  ScoreRunOutcomesResponse,
  ScoreRunView,
  TuningCandidate,
  TuningSampleView,
} from './types';

/** Requirement 7.6: why the false-pass count is stated prominently. */
export function falsePassWarning(count: number, candidateName: string): string {
  return (
    `${candidateName} has ${count} false pass${count === 1 ? '' : 'es'}: `
    + 'samples you labelled NOK that the candidate called normal. A false '
    + 'pass ships a defective part.'
  );
}

export const CATEGORY_OPTIONS: SelectProps.Option[] = [
  { label: 'All categories', value: '' },
  { label: 'Correct', value: 'correct' },
  { label: 'False pass', value: 'false_pass' },
  { label: 'False fail', value: 'false_fail' },
  { label: 'Parse failure', value: 'parse_failure' },
  { label: 'Invocation error', value: 'invocation_error' },
];

const SORT_OPTIONS: SelectProps.Option[] = [
  { label: 'By sample', value: '' },
  { label: 'Confidence, highest first', value: 'desc' },
  { label: 'Confidence, lowest first', value: 'asc' },
];

function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${Math.round(value * 1000) / 10}%`;
}

interface CompareTabProps {
  sessionId: string;
  candidates: TuningCandidate[];
  selectedCandidateId: string | null;
  onChanged: () => void | Promise<void>;
}

export default function CompareTab({
  sessionId,
  candidates,
  selectedCandidateId,
  onChanged,
}: CompareTabProps) {
  const [error, setError] = useState<string | null>(null);
  const [selecting, setSelecting] = useState(false);
  const [falsePasses, setFalsePasses] = useState<number | null>(null);

  const [drillRunId, setDrillRunId] = useState<string | null>(null);
  const [outcomes, setOutcomes] = useState<ScoreRunOutcomesResponse | null>(null);
  const [outcomesLoading, setOutcomesLoading] = useState(false);
  const [category, setCategory] = useState<SelectProps.Option>(
    CATEGORY_OPTIONS[0]
  );
  const [sort, setSort] = useState<SelectProps.Option>(SORT_OPTIONS[0]);

  const [diffA, setDiffA] = useState<SelectProps.Option | null>(null);
  const [diffB, setDiffB] = useState<SelectProps.Option | null>(null);
  const [diff, setDiff] = useState<ScoreRunDiffResponse | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);

  const scored = useMemo(
    () => candidates.filter((candidate) => !!candidate.latestRun),
    [candidates]
  );

  const runOptions: SelectProps.Option[] = scored.map((candidate) => ({
    label: `${candidate.name} · ${candidate.latestRun?.status}`,
    value: candidate.latestRun!.runId,
  }));

  const selectedCandidate =
    candidates.find((c) => c.candidateId === selectedCandidateId) || null;

  useEffect(() => {
    // The selection's false passes come with the selection response; on
    // arrival — and after a reload — they are read from the selected
    // Candidate's latest run. A response value is never cleared by a
    // reload that has not caught up with the selection yet.
    if (selectedCandidate) {
      setFalsePasses(selectedCandidate.latestRun?.summary?.falsePass ?? null);
    }
  }, [selectedCandidate]);

  const loadOutcomes = useCallback(
    async (runId: string) => {
      setOutcomesLoading(true);
      try {
        const response = await apiService.listTuningScoreRunOutcomes(runId, {
          ...(category.value
            ? { category: category.value as OutcomeCategory }
            : {}),
          ...(sort.value
            ? { sort: 'confidence' as const, order: sort.value as 'asc' | 'desc' }
            : {}),
        });
        setOutcomes(response);
        setError(null);
      } catch (err) {
        setError(getErrorMessage(err, 'Failed to load the run outcomes'));
      } finally {
        setOutcomesLoading(false);
      }
    },
    [category, sort]
  );

  useEffect(() => {
    if (!drillRunId) {
      setOutcomes(null);
      return;
    }
    loadOutcomes(drillRunId);
  }, [drillRunId, loadOutcomes]);

  /** Requirement 7.5: exactly one Candidate is selected, and persisted. */
  const select = async (candidateId: string | null) => {
    setSelecting(true);
    try {
      const response = await apiService.setTuningSelection(
        sessionId,
        candidateId
      );
      setFalsePasses(response.falsePasses ?? null);
      setError(null);
      await onChanged();
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to save the selection'));
    } finally {
      setSelecting(false);
    }
  };

  const runDiff = async () => {
    if (!diffA?.value || !diffB?.value) return;
    setDiffLoading(true);
    try {
      setDiff(
        await apiService.diffTuningScoreRuns(
          String(diffA.value),
          String(diffB.value)
        )
      );
      setError(null);
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to compare the two runs'));
    } finally {
      setDiffLoading(false);
    }
  };

  const sampleOf = (sampleId: string): TuningSampleView | undefined =>
    outcomes?.samples?.[sampleId];

  const outcomeRow = (outcome: SampleOutcomeView) => {
    const sample = sampleOf(outcome.sampleId);
    return (
      <Container
        key={`${outcome.sampleId}-${outcome.repeat}`}
        data-testid={`outcome-${outcome.sampleId}-${outcome.repeat}`}
        header={
          <Header
            variant="h3"
            description={`Label ${outcome.label ?? '—'} · repeat ${outcome.repeat}`}
          >
            <SpaceBetween direction="horizontal" size="xs">
              <span>{sample?.executionId || outcome.sampleId}</span>
              <Badge
                color={
                  outcome.category === 'correct'
                    ? 'green'
                    : outcome.category === 'false_pass'
                      ? 'red'
                      : 'grey'
                }
              >
                {String(outcome.category)}
              </Badge>
            </SpaceBetween>
          </Header>
        }
      >
        <SpaceBetween size="s">
          <ColumnLayout columns={2}>
            {sample?.input?.url ? (
              <img
                src={sample.input.url}
                alt={`Input image of ${outcome.sampleId}`}
                style={{ maxWidth: '100%', maxHeight: 200, objectFit: 'contain' }}
              />
            ) : (
              <Box variant="p">Input image unavailable</Box>
            )}
            {sample && !sample.singleImage && sample.reference?.url ? (
              <img
                src={sample.reference.url}
                alt={`Reference image of ${outcome.sampleId}`}
                style={{ maxWidth: '100%', maxHeight: 200, objectFit: 'contain' }}
              />
            ) : (
              <Box variant="p">
                {sample?.singleImage
                  ? 'Single-image inspection'
                  : 'Reference image unavailable'}
              </Box>
            )}
          </ColumnLayout>
          <ColumnLayout columns={3} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Verdict</Box>
              <Box variant="p">
                {outcome.isAnomalous === null || outcome.isAnomalous === undefined
                  ? '—'
                  : outcome.isAnomalous
                    ? 'Anomalous'
                    : 'Normal'}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Confidence</Box>
              <Box variant="p">
                {outcome.confidence === null || outcome.confidence === undefined
                  ? '—'
                  : `${outcome.confidence}`}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Latency</Box>
              <Box variant="p">
                {outcome.latencyMs === null || outcome.latencyMs === undefined
                  ? '—'
                  : `${Math.round(outcome.latencyMs)} ms`}
              </Box>
            </div>
          </ColumnLayout>
          {/* Requirement 7.4: the parser's rejection reason. */}
          {outcome.parseError && (
            <Alert
              type="warning"
              data-testid={`parse-error-${outcome.sampleId}-${outcome.repeat}`}
            >
              {`The verdict parser rejected this answer: ${outcome.parseError}`}
            </Alert>
          )}
          {outcome.error && (
            <Alert type="error">{`Invocation error: ${outcome.error}`}</Alert>
          )}
          {/* Requirements 7.2, 7.4: the raw answer, character for character. */}
          <FormField label="Raw answer">
            <Box
              variant="code"
              data-testid={`raw-${outcome.sampleId}-${outcome.repeat}`}
            >
              <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                {outcome.rawAnswer ?? 'No answer was returned.'}
              </pre>
            </Box>
          </FormField>
        </SpaceBetween>
      </Container>
    );
  };

  const summaryColumns = [
    {
      id: 'candidate',
      header: 'Candidate',
      cell: (candidate: TuningCandidate) => (
        <SpaceBetween direction="horizontal" size="xs">
          <span>{candidate.name}</span>
          {candidate.isBaseline && <Badge color="grey">Baseline</Badge>}
          {candidate.candidateId === selectedCandidateId && (
            <Badge color="green">Selected</Badge>
          )}
        </SpaceBetween>
      ),
    },
    {
      id: 'status',
      header: 'Run',
      cell: (candidate: TuningCandidate) => candidate.latestRun?.status ?? '—',
    },
    {
      id: 'invocations',
      header: 'Invocations',
      cell: (candidate: TuningCandidate) =>
        candidate.latestRun?.summary?.invocations ?? 0,
    },
    {
      id: 'accuracy',
      header: 'Accuracy',
      cell: (candidate: TuningCandidate) =>
        percent(candidate.latestRun?.summary?.accuracy),
    },
    {
      id: 'falsePass',
      header: 'False passes',
      cell: (candidate: TuningCandidate) => {
        const count = candidate.latestRun?.summary?.falsePass ?? 0;
        return count > 0 ? (
          <Box color="text-status-error" fontWeight="bold">
            {count}
          </Box>
        ) : (
          <span>{count}</span>
        );
      },
    },
    {
      id: 'falseFail',
      header: 'False fails',
      cell: (candidate: TuningCandidate) =>
        candidate.latestRun?.summary?.falseFail ?? 0,
    },
    {
      id: 'parseFailure',
      header: 'Parse failures',
      cell: (candidate: TuningCandidate) =>
        candidate.latestRun?.summary?.parseFailure ?? 0,
    },
    {
      id: 'invocationError',
      header: 'Errors',
      cell: (candidate: TuningCandidate) =>
        candidate.latestRun?.summary?.invocationError ?? 0,
    },
    {
      id: 'unstable',
      header: 'Unstable',
      cell: (candidate: TuningCandidate) =>
        candidate.latestRun?.summary?.unstable ?? 0,
    },
    {
      id: 'tokens',
      header: 'Mean tokens',
      cell: (candidate: TuningCandidate) => {
        const tokens = candidate.latestRun?.summary?.meanOutputTokens;
        return tokens === null || tokens === undefined
          ? '—'
          : `${Math.round(tokens)}`;
      },
    },
    {
      id: 'actions',
      header: '',
      cell: (candidate: TuningCandidate) => (
        <SpaceBetween direction="horizontal" size="xs">
          <Button
            variant="inline-link"
            disabled={!candidate.latestRun}
            onClick={() => setDrillRunId(candidate.latestRun?.runId ?? null)}
            data-testid={`drill-${candidate.candidateId}`}
          >
            Outcomes
          </Button>
          <Button
            variant="inline-link"
            loading={selecting}
            disabled={candidate.candidateId === selectedCandidateId}
            onClick={() => select(candidate.candidateId)}
            data-testid={`select-${candidate.candidateId}`}
          >
            Select
          </Button>
        </SpaceBetween>
      ),
    },
  ];

  const drilledRun: ScoreRunView | null = outcomes?.run ?? null;

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Requirement 7.6: stated at selection time, before any reload. */}
      {!!falsePasses && falsePasses > 0 && (
        <Alert type="warning" data-testid="selection-false-passes">
          {falsePassWarning(
            falsePasses,
            selectedCandidate?.name ?? 'The selected candidate'
          )}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h2"
            description="One row per candidate's most recent score run"
            actions={
              selectedCandidateId && (
                <Button
                  loading={selecting}
                  onClick={() => select(null)}
                  data-testid="clear-selection"
                >
                  Clear selection
                </Button>
              )
            }
          >
            Comparison
          </Header>
        }
      >
        <Table
          variant="embedded"
          items={candidates}
          trackBy="candidateId"
          data-testid="comparison-table"
          empty={
            <Box variant="p" data-testid="no-scored-runs">
              No candidate has been scored yet. Start a score run to compare.
            </Box>
          }
          columnDefinitions={summaryColumns}
        />
      </Container>

      {/* Requirement 7.3: the two-run diff. */}
      <Container
        header={
          <Header variant="h2" description="Samples on which two runs disagree">
            Diff two runs
          </Header>
        }
      >
        <SpaceBetween size="m">
          <ColumnLayout columns={3}>
            <FormField label="Run A">
              <Select
                selectedOption={diffA}
                options={runOptions}
                onChange={({ detail }) => setDiffA(detail.selectedOption)}
                placeholder="Select a run"
                empty="No run has been scored yet"
                data-testid="diff-run-a"
              />
            </FormField>
            <FormField label="Run B">
              <Select
                selectedOption={diffB}
                options={runOptions}
                onChange={({ detail }) => setDiffB(detail.selectedOption)}
                placeholder="Select a run"
                empty="No run has been scored yet"
                data-testid="diff-run-b"
              />
            </FormField>
            <FormField label=" ">
              <Button
                loading={diffLoading}
                disabled={!diffA?.value || !diffB?.value}
                onClick={runDiff}
                data-testid="run-diff"
              >
                Compare
              </Button>
            </FormField>
          </ColumnLayout>
          {diff && (
            <Table
              variant="embedded"
              items={diff.differing}
              trackBy="sampleId"
              data-testid="diff-table"
              empty={
                <Box variant="p" data-testid="diff-empty">
                  The two runs categorized every sample the same way.
                </Box>
              }
              columnDefinitions={[
                { id: 'sample', header: 'Sample', cell: (row) => row.sampleId },
                { id: 'label', header: 'Label', cell: (row) => row.label ?? '—' },
                {
                  id: 'a',
                  header: `A · ${diff.a.candidateName || diff.a.runId}`,
                  cell: (row) => (row.a?.categories || []).join(', ') || '—',
                },
                {
                  id: 'b',
                  header: `B · ${diff.b.candidateName || diff.b.runId}`,
                  cell: (row) => (row.b?.categories || []).join(', ') || '—',
                },
              ]}
            />
          )}
        </SpaceBetween>
      </Container>

      {/* Requirement 7.2: the drill-down. */}
      {drillRunId && (
        <Container
          header={
            <Header
              variant="h2"
              description={
                drilledRun
                  ? `${drilledRun.candidateName || drilledRun.candidateId} · ${drilledRun.status} · ${outcomes?.matched ?? 0} outcomes`
                  : undefined
              }
              actions={
                <Button
                  onClick={() => setDrillRunId(null)}
                  data-testid="close-drilldown"
                >
                  Close
                </Button>
              }
            >
              {`Outcomes of run ${drillRunId}`}
            </Header>
          }
        >
          <SpaceBetween size="m">
            <ColumnLayout columns={2}>
              <FormField label="Category">
                <Select
                  selectedOption={category}
                  options={CATEGORY_OPTIONS}
                  onChange={({ detail }) => setCategory(detail.selectedOption)}
                  data-testid="outcome-category"
                />
              </FormField>
              <FormField label="Sort">
                <Select
                  selectedOption={sort}
                  options={SORT_OPTIONS}
                  onChange={({ detail }) => setSort(detail.selectedOption)}
                  data-testid="outcome-sort"
                />
              </FormField>
            </ColumnLayout>
            {outcomes?.summary && (
              <ColumnLayout columns={5} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Correct</Box>
                  <Box variant="p">{outcomes.summary.correct}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">False passes</Box>
                  <Box variant="p" data-testid="drill-false-pass">
                    {outcomes.summary.falsePass}
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">False fails</Box>
                  <Box variant="p">{outcomes.summary.falseFail}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Parse failures</Box>
                  <Box variant="p">{outcomes.summary.parseFailure}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Accuracy</Box>
                  <Box variant="p">{percent(outcomes.summary.accuracy)}</Box>
                </div>
              </ColumnLayout>
            )}
            {outcomesLoading && <Spinner />}
            {!outcomesLoading && !outcomes?.outcomes?.length && (
              <Box variant="p" data-testid="no-outcomes">
                No outcome matches this category.
              </Box>
            )}
            {(outcomes?.outcomes || []).map(outcomeRow)}
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
}
