/**
 * Score runs tab of the Tuning_Session workspace
 * (quality-prompt-tuning, task 8.3 — Requirements 6.6-6.13).
 *
 * Starting a run is a confirmation dialog stating the number of invocations
 * it will issue (labelled non-excluded samples × repeats), the repeats
 * (1..3, default 1) and — for an `llm_inference` node, which a device
 * executes — the device that will run it, picked from the devices that
 * exported the samples (Req 6.6, 6.7, 6.9). The Portal admits at most one
 * run per session and bounds a run to 600 invocations, so both are stated
 * before the attempt (Req 6.10, 6.13). While a run is in progress the tab
 * polls it and shows its progress and running Score_Summary, with a Cancel
 * that keeps the outcomes already produced (Req 6.8, 6.11).
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
  Modal,
  ProgressBar,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { ApiError, apiService } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import type {
  DeviceEligibility,
  LabelCounts,
  ScoreRunView,
  TuningCandidate,
  TuningNodeView,
} from './types';

/** The Portal's bound on one Score_Run (Requirement 6.13). */
export const MAX_PLANNED_INVOCATIONS = 600;

/** Repeats the user may configure (Requirement 6.7). */
export const REPEAT_OPTIONS: SelectProps.Option[] = [
  { label: '1 (no repeats)', value: '1' },
  { label: '2', value: '2' },
  { label: '3', value: '3' },
];

/** Poll cadence of a running Score_Run (Requirement 6.8). */
export const RUN_POLL_INTERVAL_MS = 5000;

/** Requirement 6.13's message shown before the attempt. */
export function runTooLargeMessage(planned: number): string {
  return (
    `This run would issue ${planned} invocations; a score run is bounded to `
    + `${MAX_PLANNED_INVOCATIONS} (labelled samples × repeats). Exclude `
    + 'samples or lower the repeats.'
  );
}

/** Requirement 6.11's note on the cancel action. */
export const CANCEL_NOTE =
  'Cancelling issues no further invocations and keeps every outcome the run '
  + 'already produced; the run is marked cancelled with its partial summary.';

interface ScoreRunsTabProps {
  sessionId: string;
  node: TuningNodeView | null;
  candidates: TuningCandidate[];
  labelCounts: LabelCounts | null;
  /** Devices that exported this node's samples (the picker's choices). */
  devicesSeen: string[];
  onChanged: () => void | Promise<void>;
}

export default function ScoreRunsTab({
  sessionId,
  node,
  candidates,
  labelCounts,
  devicesSeen,
  onChanged,
}: ScoreRunsTabProps) {
  const deviceMode = node?.nodeType === 'llm_inference';

  const [dialogOpen, setDialogOpen] = useState(false);
  const [candidateOption, setCandidateOption] =
    useState<SelectProps.Option | null>(null);
  const [repeats, setRepeats] = useState<SelectProps.Option>(REPEAT_OPTIONS[0]);
  const [deviceOption, setDeviceOption] = useState<SelectProps.Option | null>(
    null
  );
  const [eligibility, setEligibility] = useState<DeviceEligibility | null>(null);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [liveRun, setLiveRun] = useState<ScoreRunView | null>(null);

  const runs = useMemo(
    () =>
      candidates
        .map((candidate) => candidate.latestRun)
        .filter((run): run is ScoreRunView => !!run),
    [candidates]
  );

  const runningFromSession = useMemo(
    () => runs.find((run) => run.status === 'running') || null,
    [runs]
  );

  const activeRun = liveRun ?? runningFromSession;
  const running = activeRun?.status === 'running';

  // Poll the in-progress run for its progress and running summary (Req 6.8).
  const pollRun = useCallback(async (runId: string) => {
    try {
      const { run } = await apiService.getTuningScoreRun(runId);
      setLiveRun(run);
      return run;
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to read the score run'));
      return null;
    }
  }, []);

  useEffect(() => {
    if (!running || !activeRun) return undefined;
    const runId = activeRun.runId;
    const timer = setInterval(() => {
      pollRun(runId).then((run) => {
        // A finished run refreshes the session so Compare sees it.
        if (run && run.status !== 'running') onChanged();
      });
    }, RUN_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, activeRun?.runId, pollRun]);

  const candidateOptions: SelectProps.Option[] = candidates.map((candidate) => ({
    label: candidate.isBaseline
      ? `${candidate.name} (baseline)`
      : candidate.name,
    value: candidate.candidateId,
  }));

  const deviceOptions: SelectProps.Option[] = (
    eligibility?.eligible?.length ? eligibility.eligible : devicesSeen
  ).map((thingName) => ({ label: thingName, value: thingName }));

  const repeatCount = Number(repeats.value || '1');
  const scorable = (labelCounts?.OK ?? 0) + (labelCounts?.NOK ?? 0);
  const planned = scorable * repeatCount;
  const tooLarge = planned > MAX_PLANNED_INVOCATIONS;
  const missingClass =
    !!labelCounts && (labelCounts.OK === 0 || labelCounts.NOK === 0);

  const openDialog = () => {
    setCandidateOption(candidateOptions[0] ?? null);
    setDialogError(null);
    setDialogOpen(true);
  };

  const start = async () => {
    if (!candidateOption?.value) {
      setDialogError('Select the candidate to score');
      return;
    }
    setStarting(true);
    setDialogError(null);
    try {
      const response = await apiService.startTuningScoreRun(sessionId, {
        candidateId: String(candidateOption.value),
        repeats: repeatCount,
        ...(deviceMode && deviceOption?.value
          ? { deviceThingName: String(deviceOption.value) }
          : {}),
      });
      if (response.deviceEligibility) setEligibility(response.deviceEligibility);
      setLiveRun(response.run);
      setDialogOpen(false);
      setError(
        response.dispatchFailed
          ? response.run.error
            || 'The score run could not be started; it is recorded as failed.'
          : null
      );
      await onChanged();
    } catch (err) {
      // The device-eligibility errors carry the eligible / ineligible
      // devices, which narrows the picker to the devices that can really
      // execute the run (Requirement 6.9).
      if (err instanceof ApiError && err.details) {
        const details = err.details as Partial<DeviceEligibility>;
        if (Array.isArray(details.eligible) || Array.isArray(details.exported)) {
          setEligibility({
            exported: details.exported ?? [],
            registered: details.registered ?? [],
            eligible: details.eligible ?? [],
            ineligible: details.ineligible ?? [],
          });
        }
      }
      setDialogError(getErrorMessage(err, 'Failed to start the score run'));
    } finally {
      setStarting(false);
    }
  };

  const cancel = async () => {
    if (!activeRun) return;
    setCancelling(true);
    try {
      const { run } = await apiService.cancelTuningScoreRun(activeRun.runId);
      setLiveRun(run);
      setError(null);
      await onChanged();
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to cancel the score run'));
    } finally {
      setCancelling(false);
    }
  };

  const summaryPanel = (run: ScoreRunView) => {
    const summary = run.summary;
    return (
      <ColumnLayout columns={4} variant="text-grid">
        <div>
          <Box variant="awsui-key-label">Correct</Box>
          <Box variant="p" data-testid="live-correct">{summary?.correct ?? 0}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">False passes</Box>
          <Box variant="p" data-testid="live-false-pass">
            {summary?.falsePass ?? 0}
          </Box>
        </div>
        <div>
          <Box variant="awsui-key-label">False fails</Box>
          <Box variant="p">{summary?.falseFail ?? 0}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Parse failures</Box>
          <Box variant="p">{summary?.parseFailure ?? 0}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Invocation errors</Box>
          <Box variant="p">{summary?.invocationError ?? 0}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Accuracy</Box>
          <Box variant="p">
            {summary?.accuracy === null || summary?.accuracy === undefined
              ? '—'
              : `${Math.round(summary.accuracy * 1000) / 10}%`}
          </Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Unstable samples</Box>
          <Box variant="p">{summary?.unstable ?? 0}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Mean latency</Box>
          <Box variant="p">
            {summary?.meanLatencyMs === null
              || summary?.meanLatencyMs === undefined
              ? '—'
              : `${Math.round(summary.meanLatencyMs)} ms`}
          </Box>
        </div>
      </ColumnLayout>
    );
  };

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h2"
            description={
              deviceMode
                ? 'This node runs on the device: a score run is executed by the device that exported the samples.'
                : 'This node runs on Bedrock: the portal replays the candidate through the same invocation builder the executor uses.'
            }
            actions={
              <Button
                variant="primary"
                disabled={!candidates.length || running}
                onClick={openDialog}
                data-testid="start-score-run"
              >
                Start score run
              </Button>
            }
          >
            Score runs
          </Header>
        }
      >
        <SpaceBetween size="m">
          {running && activeRun && (
            <Alert type="info" data-testid="run-in-progress">
              {`Score run ${activeRun.runId} is in progress for candidate `
                + `${activeRun.candidateName || activeRun.candidateId}. `
                + 'At most one run per session may be in progress.'}
            </Alert>
          )}
          {missingClass && (
            <Alert type="warning" data-testid="missing-class-warning">
              Label at least one sample OK and one NOK on the Samples tab: only
              OK/NOK samples are scored.
            </Alert>
          )}
          {activeRun ? (
            <SpaceBetween size="s">
              <ProgressBar
                value={
                  activeRun.plannedInvocations
                    ? Math.round(
                        ((activeRun.done ?? 0) / activeRun.plannedInvocations)
                          * 100
                      )
                    : 0
                }
                additionalInfo={`${activeRun.done ?? 0} of ${activeRun.plannedInvocations} invocations`}
                description={
                  activeRun.mode === 'device'
                    ? `Device run on ${activeRun.deviceThingName || 'the selected device'}`
                    : 'Bedrock run'
                }
                label={`Run ${activeRun.runId}`}
              />
              <SpaceBetween direction="horizontal" size="xs">
                <StatusIndicator
                  type={
                    activeRun.status === 'completed'
                      ? 'success'
                      : activeRun.status === 'running'
                        ? 'in-progress'
                        : activeRun.status === 'cancelled'
                          ? 'stopped'
                          : 'error'
                  }
                >
                  {activeRun.status}
                </StatusIndicator>
                <Badge>{`${activeRun.repeats}× repeats`}</Badge>
                {running && (
                  <Button
                    loading={cancelling}
                    onClick={cancel}
                    data-testid="cancel-score-run"
                  >
                    Cancel run
                  </Button>
                )}
              </SpaceBetween>
              {activeRun.error && (
                <Alert type="error" data-testid="run-error">
                  {activeRun.error}
                </Alert>
              )}
              {running && <Box variant="small">{CANCEL_NOTE}</Box>}
              {summaryPanel(activeRun)}
            </SpaceBetween>
          ) : (
            <Box variant="p" data-testid="no-runs">
              No score run has been started for this session yet.
            </Box>
          )}
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">Latest run per candidate</Header>}>
        <Table
          variant="embedded"
          items={candidates}
          trackBy="candidateId"
          empty={<Box variant="p">This session has no candidate yet.</Box>}
          columnDefinitions={[
            {
              id: 'candidate',
              header: 'Candidate',
              cell: (candidate) =>
                candidate.isBaseline
                  ? `${candidate.name} (baseline)`
                  : candidate.name,
            },
            {
              id: 'status',
              header: 'Latest run',
              cell: (candidate) => candidate.latestRun?.status || 'never scored',
            },
            {
              id: 'mode',
              header: 'Executed by',
              cell: (candidate) =>
                candidate.latestRun
                  ? candidate.latestRun.mode === 'device'
                    ? `device ${candidate.latestRun.deviceThingName || ''}`.trim()
                    : 'bedrock'
                  : '—',
            },
            {
              id: 'invocations',
              header: 'Invocations',
              cell: (candidate) =>
                candidate.latestRun
                  ? `${candidate.latestRun.done ?? 0} / ${candidate.latestRun.plannedInvocations}`
                  : '—',
            },
            {
              id: 'accuracy',
              header: 'Accuracy',
              cell: (candidate) => {
                const accuracy = candidate.latestRun?.summary?.accuracy;
                return accuracy === null || accuracy === undefined
                  ? '—'
                  : `${Math.round(accuracy * 1000) / 10}%`;
              },
            },
          ]}
        />
      </Container>

      {/* Requirement 6.6: the confirmation naming the invocation count. */}
      <Modal
        visible={dialogOpen}
        onDismiss={() => setDialogOpen(false)}
        header="Start score run"
        data-testid="start-run-dialog"
        footer={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => setDialogOpen(false)}>Cancel</Button>
            <Button
              variant="primary"
              loading={starting}
              disabled={tooLarge || scorable === 0}
              onClick={start}
              data-testid="confirm-start-run"
            >
              {`Start ${planned} invocations`}
            </Button>
          </SpaceBetween>
        }
      >
        <SpaceBetween size="m">
          {dialogError && (
            <Alert type="error" data-testid="start-run-error">
              {dialogError}
            </Alert>
          )}
          <FormField label="Candidate">
            <Select
              selectedOption={candidateOption}
              options={candidateOptions}
              onChange={({ detail }) => setCandidateOption(detail.selectedOption)}
              placeholder="Select a candidate"
              data-testid="run-candidate"
            />
          </FormField>
          <FormField
            label="Repeats per sample"
            description="Repeats reveal verdict instability; 1 to 3."
          >
            <Select
              selectedOption={repeats}
              options={REPEAT_OPTIONS}
              onChange={({ detail }) => setRepeats(detail.selectedOption)}
              data-testid="run-repeats"
            />
          </FormField>
          {deviceMode && (
            <FormField
              label="Device"
              description="A VLM node is scored on a device that exported samples for it and reports the workflow."
            >
              <Select
                selectedOption={deviceOption}
                options={deviceOptions}
                onChange={({ detail }) => setDeviceOption(detail.selectedOption)}
                placeholder={
                  deviceOptions.length
                    ? 'Select the device that will execute the run'
                    : 'No device exported samples for this node'
                }
                empty="No device exported samples for this node"
                data-testid="run-device"
              />
            </FormField>
          )}
          {eligibility && !!eligibility.ineligible?.length && (
            <Alert type="warning" data-testid="ineligible-devices">
              {`These devices exported samples but do not report the workflow, `
                + `so they cannot execute the run: ${eligibility.ineligible.join(', ')}.`}
            </Alert>
          )}
          <Box variant="p" data-testid="planned-invocations">
            {`This run will issue ${planned} invocations: ${scorable} labelled `
              + `samples × ${repeatCount} repeat${repeatCount === 1 ? '' : 's'}.`}
          </Box>
          {deviceMode && (
            <Box variant="p" data-testid="run-device-note">
              {deviceOption?.value
                ? `The run will be executed on device ${deviceOption.value}.`
                : 'Pick the device that will execute the run.'}
            </Box>
          )}
          {scorable === 0 && (
            <Alert type="warning">
              No sample is labelled OK or NOK, so there is nothing to score.
            </Alert>
          )}
          {tooLarge && (
            <Alert type="error" data-testid="run-too-large">
              {runTooLargeMessage(planned)}
            </Alert>
          )}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
