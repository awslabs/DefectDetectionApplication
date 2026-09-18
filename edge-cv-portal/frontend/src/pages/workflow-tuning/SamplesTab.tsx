/**
 * Samples tab of the Tuning_Session workspace
 * (quality-prompt-tuning, task 8.3 — Requirements 4.1-4.8).
 *
 * One pair card per Tuning_Sample: the Input_Image beside its
 * Reference_Image (or a single-image indication), the verdict and confidence
 * the deployed node recorded, its raw answer on demand, the device,
 * `executionId`, version, detection slot, source and Label, with
 * duplicate / synthetic / different-prompt badges (Req 4.1). Labels are set
 * per card and for a multi-selection and persisted immediately (Req 4.2);
 * unlabelled samples are shown as such and count as excluded from scoring
 * (Req 4.3). Filters cover Label, recorded verdict, device, version, source,
 * duplicates, the different-prompt flag and verdict/Label disagreement
 * (Req 4.4). The Synthetic_Negatives toggle creates and removes them
 * (Req 4.5, 4.6), and the counts panel warns while either the OK or the NOK
 * count is zero (Req 4.7). Images are the presigned URLs the route returns
 * (Req 4.8).
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  ColumnLayout,
  Container,
  ExpandableSection,
  FormField,
  Header,
  Input,
  Select,
  SelectProps,
  SpaceBetween,
  Spinner,
  Toggle,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import type {
  LabelCounts,
  ListTuningSamplesParams,
  TuningLabel,
  TuningSampleView,
} from './types';

/** Requirement 4.7's warning: a run needs both classes to be meaningful. */
export const ZERO_CLASS_WARNING =
  'A score run needs samples of both classes: label at least one sample OK '
  + 'and at least one NOK before starting a run. Unlabelled and EXCLUDE '
  + 'samples are not scored.';

/** Requirement 4.3's wording for a sample with no Label. */
export const UNLABELLED_TEXT = 'Unlabelled — excluded from scoring';

const PAGE_SIZE = 24;

const LABEL_OPTIONS: SelectProps.Option[] = [
  { label: 'Any label', value: '' },
  { label: 'OK', value: 'OK' },
  { label: 'NOK', value: 'NOK' },
  { label: 'EXCLUDE', value: 'EXCLUDE' },
  { label: 'Unlabelled', value: 'UNLABELLED' },
];

const VERDICT_OPTIONS: SelectProps.Option[] = [
  { label: 'Any recorded verdict', value: '' },
  { label: 'Recorded anomalous', value: 'anomalous' },
  { label: 'Recorded normal', value: 'normal' },
];

const SOURCE_OPTIONS: SelectProps.Option[] = [
  { label: 'Any source', value: '' },
  { label: 'Live', value: 'live' },
  { label: 'Backfill', value: 'backfill' },
];

const FLAG_OPTIONS: SelectProps.Option[] = [
  { label: 'Any', value: '' },
  { label: 'Only', value: 'true' },
  { label: 'Exclude', value: 'false' },
];

interface SamplesTabProps {
  sessionId: string;
  /** `syntheticNegativesEnabled` of the session (Requirement 4.5). */
  syntheticEnabled: boolean;
  labelCounts: LabelCounts | null;
  /** Every mutation answers fresh counts; the workspace header shows them. */
  onCountsChange: (counts: LabelCounts) => void;
  /** Devices that exported the samples — the VLM run's device picker. */
  onDevicesSeen: (devices: string[]) => void;
  onSyntheticChange: (enabled: boolean) => void;
}

export default function SamplesTab({
  sessionId,
  syntheticEnabled,
  labelCounts,
  onCountsChange,
  onDevicesSeen,
  onSyntheticChange,
}: SamplesTabProps) {
  const [samples, setSamples] = useState<TuningSampleView[]>([]);
  const [matched, setMatched] = useState(0);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<string[]>([]);

  // Filters (Requirement 4.4).
  const [label, setLabel] = useState<SelectProps.Option>(LABEL_OPTIONS[0]);
  const [verdict, setVerdict] = useState<SelectProps.Option>(VERDICT_OPTIONS[0]);
  const [source, setSource] = useState<SelectProps.Option>(SOURCE_OPTIONS[0]);
  const [duplicates, setDuplicates] = useState<SelectProps.Option>(FLAG_OPTIONS[0]);
  const [differentPrompt, setDifferentPrompt] =
    useState<SelectProps.Option>(FLAG_OPTIONS[0]);
  const [disagree, setDisagree] = useState<SelectProps.Option>(FLAG_OPTIONS[0]);
  const [device, setDevice] = useState('');
  const [version, setVersion] = useState('');

  const params = useMemo<ListTuningSamplesParams>(() => {
    const query: ListTuningSamplesParams = { limit: PAGE_SIZE };
    if (label.value) query.label = label.value as ListTuningSamplesParams['label'];
    if (verdict.value) query.verdict = verdict.value as 'anomalous' | 'normal';
    if (source.value) query.source = source.value;
    if (duplicates.value) query.duplicates = duplicates.value === 'true';
    if (differentPrompt.value) query.differentPrompt = differentPrompt.value === 'true';
    if (disagree.value) query.disagree = disagree.value === 'true';
    if (device.trim()) query.device = device.trim();
    if (version.trim()) query.version = version.trim();
    return query;
  }, [label, verdict, source, duplicates, differentPrompt, disagree, device, version]);

  const load = useCallback(
    async (cursor?: string) => {
      setLoading(true);
      try {
        const response = await apiService.listTuningSamples(sessionId, {
          ...params,
          ...(cursor ? { cursor } : {}),
        });
        setSamples((current) =>
          cursor ? [...current, ...response.samples] : response.samples
        );
        setMatched(response.matched);
        setNextCursor(response.nextCursor);
        onCountsChange(response.labelCounts);
        setError(null);
      } catch (err) {
        setError(getErrorMessage(err, 'Failed to load samples'));
      } finally {
        setLoading(false);
      }
    },
    [sessionId, params, onCountsChange]
  );

  // Reload from the first page whenever a filter changes.
  useEffect(() => {
    setSelected([]);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, params]);

  // The devices that exported these samples feed the VLM device picker.
  useEffect(() => {
    const devices = Array.from(
      new Set(samples.map((s) => s.thingName).filter(Boolean) as string[])
    ).sort();
    if (devices.length) onDevicesSeen(devices);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [samples]);

  /** Requirements 4.2, 4.3: persist a Label (or clear it) immediately. */
  const setLabels = async (value: TuningLabel | null, sampleIds: string[]) => {
    if (!sampleIds.length) return;
    setBusy(true);
    try {
      const response = await apiService.setTuningSampleLabels(sessionId, {
        sampleIds,
        label: value,
      });
      const updated = new Set(response.updated);
      setSamples((current) =>
        current.map((sample) =>
          updated.has(sample.sampleId) ? { ...sample, label: value } : sample
        )
      );
      onCountsChange(response.labelCounts);
      setError(null);
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to save the label'));
    } finally {
      setBusy(false);
    }
  };

  /** Requirements 4.5, 4.6: create / remove the Synthetic_Negatives. */
  const toggleSynthetic = async (enabled: boolean) => {
    setBusy(true);
    try {
      const response = await apiService.setTuningSyntheticNegatives(
        sessionId,
        enabled
      );
      onSyntheticChange(response.enabled);
      onCountsChange(response.labelCounts);
      setError(null);
      await load();
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to update synthetic negatives'));
    } finally {
      setBusy(false);
    }
  };

  const toggleSelected = (sampleId: string, checked: boolean) =>
    setSelected((current) =>
      checked
        ? [...current.filter((id) => id !== sampleId), sampleId]
        : current.filter((id) => id !== sampleId)
    );

  const counts = labelCounts;
  const zeroClass = !!counts && (counts.OK === 0 || counts.NOK === 0);

  const image = (
    url: string | null | undefined,
    alt: string,
    caption: string
  ) => (
    <SpaceBetween size="xxs">
      <Box variant="awsui-key-label">{caption}</Box>
      {url ? (
        <img
          src={url}
          alt={alt}
          style={{ maxWidth: '100%', maxHeight: 220, objectFit: 'contain' }}
        />
      ) : (
        <Box variant="p" color="text-status-inactive">
          Image unavailable
        </Box>
      )}
    </SpaceBetween>
  );

  const card = (sample: TuningSampleView) => {
    const isSelected = selected.includes(sample.sampleId);
    const recorded = sample.recorded || {};
    const verdictText =
      recorded.isAnomalous === null || recorded.isAnomalous === undefined
        ? recorded.parseError
          ? `No verdict (parser: ${recorded.parseError})`
          : 'No verdict recorded'
        : recorded.isAnomalous
          ? 'Anomalous'
          : 'Normal';
    const confidenceText =
      recorded.confidence === null || recorded.confidence === undefined
        ? '—'
        : `${recorded.confidence}`;
    return (
      <Container
        key={sample.sampleId}
        data-testid={`sample-card-${sample.sampleId}`}
        header={
          <Header
            variant="h3"
            description={
              sample.label
                ? `Label: ${sample.label}`
                : UNLABELLED_TEXT
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant={sample.label === 'OK' ? 'primary' : 'normal'}
                  disabled={busy}
                  onClick={() => setLabels('OK', [sample.sampleId])}
                >
                  OK
                </Button>
                <Button
                  variant={sample.label === 'NOK' ? 'primary' : 'normal'}
                  disabled={busy}
                  onClick={() => setLabels('NOK', [sample.sampleId])}
                >
                  NOK
                </Button>
                <Button
                  variant={sample.label === 'EXCLUDE' ? 'primary' : 'normal'}
                  disabled={busy}
                  onClick={() => setLabels('EXCLUDE', [sample.sampleId])}
                >
                  EXCLUDE
                </Button>
                <Button
                  disabled={busy || !sample.label}
                  onClick={() => setLabels(null, [sample.sampleId])}
                >
                  Clear
                </Button>
              </SpaceBetween>
            }
          >
            <SpaceBetween direction="horizontal" size="xs">
              <Checkbox
                checked={isSelected}
                onChange={({ detail }) =>
                  toggleSelected(sample.sampleId, detail.checked)
                }
                ariaLabel={`Select sample ${sample.sampleId}`}
              >
                {sample.executionId || sample.sampleId}
              </Checkbox>
              {sample.duplicateOf && <Badge color="grey">Duplicate</Badge>}
              {sample.synthetic && <Badge color="blue">Synthetic</Badge>}
              {sample.differentPrompt && (
                <Badge color="red">Different prompt</Badge>
              )}
            </SpaceBetween>
          </Header>
        }
      >
        <SpaceBetween size="s">
          {sample.unavailable && (
            <Alert type="warning">
              The exported images are no longer in the sample store (the use
              case retention expired them); the recorded answer below is kept.
            </Alert>
          )}
          <ColumnLayout columns={2}>
            {image(sample.input?.url, `Input image of ${sample.executionId}`, 'Input')}
            {sample.singleImage ? (
              <SpaceBetween size="xxs">
                <Box variant="awsui-key-label">Reference</Box>
                <Box variant="p" data-testid={`single-image-${sample.sampleId}`}>
                  Single-image inspection — this node sent no reference image.
                </Box>
              </SpaceBetween>
            ) : (
              image(
                sample.reference?.url,
                `Reference image of ${sample.executionId}`,
                'Reference'
              )
            )}
          </ColumnLayout>
          <ColumnLayout columns={3} variant="text-grid">
            <SpaceBetween size="xxs">
              <Box variant="awsui-key-label">Recorded verdict</Box>
              <Box variant="p">{verdictText}</Box>
              <Box variant="awsui-key-label">Confidence</Box>
              <Box variant="p">{confidenceText}</Box>
            </SpaceBetween>
            <SpaceBetween size="xxs">
              <Box variant="awsui-key-label">Device</Box>
              <Box variant="p">{sample.thingName || '—'}</Box>
              <Box variant="awsui-key-label">Execution</Box>
              <Box variant="p">{sample.executionId || '—'}</Box>
            </SpaceBetween>
            <SpaceBetween size="xxs">
              <Box variant="awsui-key-label">Version</Box>
              <Box variant="p">
                {sample.version === null || sample.version === undefined
                  ? '—'
                  : `${sample.version}`}
              </Box>
              <Box variant="awsui-key-label">Source</Box>
              <Box variant="p">{sample.source || '—'}</Box>
              {sample.detectionSlot !== null
                && sample.detectionSlot !== undefined && (
                <>
                  <Box variant="awsui-key-label">Detection slot</Box>
                  <Box variant="p">{`${sample.detectionSlot}`}</Box>
                </>
              )}
            </SpaceBetween>
          </ColumnLayout>
          {/* Requirement 4.1: the raw answer on demand. */}
          <ExpandableSection
            headerText="Raw answer"
            expanded={expanded.includes(sample.sampleId)}
            onChange={({ detail }) =>
              setExpanded((current) =>
                detail.expanded
                  ? [...current, sample.sampleId]
                  : current.filter((id) => id !== sample.sampleId)
              )
            }
          >
            <Box
              variant="code"
              data-testid={`raw-answer-${sample.sampleId}`}
            >
              <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                {recorded.answer ?? 'No answer was recorded for this sample.'}
              </pre>
            </Box>
          </ExpandableSection>
        </SpaceBetween>
      </Container>
    );
  };

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Requirement 4.7: the counts and the zero-OK / zero-NOK warning. */}
      <Container header={<Header variant="h2">Labels</Header>}>
        <SpaceBetween size="s">
          <ColumnLayout columns={6} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">OK</Box>
              <Box variant="p" data-testid="count-ok">{counts?.OK ?? 0}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">NOK</Box>
              <Box variant="p" data-testid="count-nok">{counts?.NOK ?? 0}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">EXCLUDE</Box>
              <Box variant="p" data-testid="count-exclude">
                {counts?.EXCLUDE ?? 0}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Unlabelled</Box>
              <Box variant="p" data-testid="count-unlabelled">
                {counts?.unlabelled ?? 0}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Synthetic</Box>
              <Box variant="p" data-testid="count-synthetic">
                {counts?.synthetic ?? 0}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Total</Box>
              <Box variant="p" data-testid="count-total">{counts?.total ?? 0}</Box>
            </div>
          </ColumnLayout>
          {zeroClass && (
            <Alert type="warning" data-testid="zero-class-warning">
              {ZERO_CLASS_WARNING}
            </Alert>
          )}
          {/* Requirements 4.5, 4.6. */}
          <Toggle
            checked={syntheticEnabled}
            disabled={busy}
            onChange={({ detail }) => toggleSynthetic(detail.checked)}
            data-testid="synthetic-toggle"
          >
            Synthetic negatives — pair each OK sample with a sibling node&apos;s
            reference image as a known-bad NOK sample
          </Toggle>
        </SpaceBetween>
      </Container>

      {/* Requirement 4.4: the filters. */}
      <Container header={<Header variant="h2">Filters</Header>}>
        <ColumnLayout columns={4}>
          <FormField label="Label">
            <Select
              selectedOption={label}
              options={LABEL_OPTIONS}
              onChange={({ detail }) => setLabel(detail.selectedOption)}
              data-testid="filter-label"
            />
          </FormField>
          <FormField label="Recorded verdict">
            <Select
              selectedOption={verdict}
              options={VERDICT_OPTIONS}
              onChange={({ detail }) => setVerdict(detail.selectedOption)}
              data-testid="filter-verdict"
            />
          </FormField>
          <FormField label="Device">
            <Input
              value={device}
              placeholder="Any device"
              onChange={({ detail }) => setDevice(detail.value)}
              data-testid="filter-device"
            />
          </FormField>
          <FormField label="Version">
            <Input
              value={version}
              placeholder="Any version"
              onChange={({ detail }) => setVersion(detail.value)}
              data-testid="filter-version"
            />
          </FormField>
          <FormField label="Source">
            <Select
              selectedOption={source}
              options={SOURCE_OPTIONS}
              onChange={({ detail }) => setSource(detail.selectedOption)}
              data-testid="filter-source"
            />
          </FormField>
          <FormField label="Duplicates">
            <Select
              selectedOption={duplicates}
              options={FLAG_OPTIONS}
              onChange={({ detail }) => setDuplicates(detail.selectedOption)}
              data-testid="filter-duplicates"
            />
          </FormField>
          <FormField label="Different prompt">
            <Select
              selectedOption={differentPrompt}
              options={FLAG_OPTIONS}
              onChange={({ detail }) => setDifferentPrompt(detail.selectedOption)}
              data-testid="filter-different-prompt"
            />
          </FormField>
          <FormField
            label="Verdict disagrees with label"
            description="Samples the deployed prompt got wrong by your labels"
          >
            <Select
              selectedOption={disagree}
              options={FLAG_OPTIONS}
              onChange={({ detail }) => setDisagree(detail.selectedOption)}
              data-testid="filter-disagree"
            />
          </FormField>
        </ColumnLayout>
      </Container>

      {/* Requirement 4.2: the multi-selection. */}
      <Container
        header={
          <Header
            variant="h2"
            counter={`(${samples.length} of ${matched})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  disabled={busy || !selected.length}
                  onClick={() => setLabels('OK', selected)}
                >
                  {`Label ${selected.length} OK`}
                </Button>
                <Button
                  disabled={busy || !selected.length}
                  onClick={() => setLabels('NOK', selected)}
                >
                  {`Label ${selected.length} NOK`}
                </Button>
                <Button
                  disabled={busy || !selected.length}
                  onClick={() => setLabels('EXCLUDE', selected)}
                >
                  {`Label ${selected.length} EXCLUDE`}
                </Button>
                <Button
                  disabled={busy || !samples.length}
                  onClick={() => setSelected(samples.map((s) => s.sampleId))}
                >
                  Select all shown
                </Button>
                <Button
                  disabled={!selected.length}
                  onClick={() => setSelected([])}
                >
                  Clear selection
                </Button>
              </SpaceBetween>
            }
          >
            Samples
          </Header>
        }
      >
        <SpaceBetween size="l">
          {loading && !samples.length && <Spinner />}
          {!loading && !samples.length && (
            <Box variant="p" data-testid="no-samples">
              No sample matches these filters. Devices export samples after
              every anomaly-mode invocation; use &quot;Refresh samples&quot; to
              index newly exported ones.
            </Box>
          )}
          {samples.map(card)}
          {nextCursor && (
            <Button
              loading={loading}
              onClick={() => load(nextCursor)}
              data-testid="load-more-samples"
            >
              Load more
            </Button>
          )}
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}
