/**
 * The expandable card of a `run` record (workflow-debug-mode design §4.2,
 * §8): status, the timing row `queued / setup (readiness wait) / pipeline`,
 * the trigger (its shape-derived `type`, an `unknown` trigger showing its
 * serialized context), the nodes table, the detections table, the bindings,
 * the error and traceback, the model readiness, the frame source, the
 * artifacts, the run's `logs` list with its `src` badge (`runlog` = the
 * run.log lines, `tap` = the lines run.log never captures), the image through
 * `getDebugImageUrl` when the record carries one (a notice on 409 while
 * registry enforcement is off) and the `truncated` markers.
 */
import { useState, type ReactNode } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  ExpandableSection,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { apiService, isDebugApiError } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import type { TailEvent } from '../../hooks/useDebugTail';
import type { DebugImageUrlResponse, DebugRunRecord, DebugRunTrigger } from '../../pages/debug/types';
import { formatBytes, formatClock, formatMs, formatTime } from './format';

export type ImageUrlFetcher = (
  sessionId: string,
  executionId: string,
  deviceId?: string
) => Promise<DebugImageUrlResponse>;

/** The one-line description of a trigger (design §4.2's five shapes). */
export function describeTrigger(trigger: DebugRunTrigger | null | undefined): string {
  if (!trigger) return 'no trigger context';
  switch (trigger.type) {
    case 'mqtt':
      return `mqtt ${trigger.topic ?? ''}`.trim();
    case 'opcua':
      return `opcua ${trigger.topic ?? ''}`.trim();
    case 'continuous':
      return 'continuous';
    case 'manual':
      return 'manual';
    case 'unknown':
      return `unknown trigger${trigger.source ? ` (source ${trigger.source})` : ''}`;
    default:
      return String(trigger.type);
  }
}

/** The header summary: time, status, execution id, trigger and pipeline time. */
export function runSummary(record: DebugRunRecord): string {
  const id = record.executionId ? record.executionId.slice(0, 8) : '—';
  return `${formatClock(record.ts)} run ${id} ${record.status} · ${describeTrigger(record.trigger)} · pipeline ${formatMs(record.durationMs)}`;
}

export interface RunRecordCardProps {
  event: TailEvent;
  /** The session the image belongs to; without one the image cannot be loaded. */
  sessionId?: string | null;
  deviceId?: string;
  defaultExpanded?: boolean;
  /** Injected for tests; defaults to `apiService.getDebugImageUrl`. */
  fetchImageUrl?: ImageUrlFetcher;
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <Box variant="awsui-key-label">{label}</Box>
      <div>{value}</div>
    </div>
  );
}

export default function RunRecordCard({ event, sessionId, deviceId, defaultExpanded = false, fetchImageUrl }: RunRecordCardProps) {
  const record = event.record as DebugRunRecord;
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [imageNotice, setImageNotice] = useState<{ type: 'warning' | 'error' | 'info'; text: string } | null>(null);
  const [imageLoading, setImageLoading] = useState(false);

  const loadImage = async () => {
    if (!sessionId || !record.executionId) return;
    const fetcher: ImageUrlFetcher = fetchImageUrl ?? ((s, e, d) => apiService.getDebugImageUrl(s, e, d));
    try {
      setImageLoading(true);
      setImageNotice(null);
      const answer = await fetcher(sessionId, record.executionId, deviceId);
      setImageUrl(answer.url);
    } catch (err) {
      if (isDebugApiError(err) && err.status === 409) {
        setImageNotice({
          type: 'warning',
          text: `${err.message}${err.remedy ? ` ${err.remedy}` : ''}`,
        });
      } else if (isDebugApiError(err) && err.status === 404) {
        setImageNotice({ type: 'info', text: 'No image was uploaded for this run.' });
      } else {
        setImageNotice({ type: 'error', text: getErrorMessage(err, 'The image could not be loaded') });
      }
    } finally {
      setImageLoading(false);
    }
  };

  const failed = record.status === 'failed';
  const trigger = record.trigger ?? null;
  const detections = record.outputs?.detections ?? [];
  const llm = record.outputs?.llm ?? {};
  const bedrock = record.outputs?.bedrock ?? {};
  const truncated = record.truncated ?? [];

  return (
    <ExpandableSection
      variant="container"
      defaultExpanded={defaultExpanded}
      data-testid={`debug-run-${record.executionId ?? event.id ?? 'run'}`}
      headerText={runSummary(record)}
      headerActions={
        <SpaceBetween direction="horizontal" size="xs">
          <StatusIndicator type={failed ? 'error' : 'success'}>{record.status}</StatusIndicator>
          {event.late && <Badge color="grey">late</Badge>}
          {truncated.length > 0 && <Badge color="blue">truncated ×{truncated.length}</Badge>}
        </SpaceBetween>
      }
    >
      <SpaceBetween size="m">
        {/* The timing row: queued / setup (readiness wait) / pipeline (design §4.2). */}
        <ColumnLayout columns={4} variant="text-grid">
          <Row label="Queued" value={<span data-testid="run-timing-queued">{formatMs(record.queuedMs)}</span>} />
          <Row
            label="Setup (readiness wait)"
            value={
              <span data-testid="run-timing-setup">
                {formatMs(record.setupMs)} ({formatMs(record.readinessWaitMs)})
              </span>
            }
          />
          <Row label="Pipeline" value={<span data-testid="run-timing-pipeline">{formatMs(record.durationMs)}</span>} />
          <Row label="Finished" value={formatTime(record.finishedMs ?? record.ts)} />
        </ColumnLayout>

        <ColumnLayout columns={2} variant="text-grid">
          <Row label="Execution id" value={<code>{record.executionId}</code>} />
          <Row
            label="Trigger"
            value={
              <SpaceBetween size="xxs">
                <span data-testid="run-trigger">{describeTrigger(trigger)}</span>
                {trigger?.payload && (
                  <Box variant="code" data-testid="run-trigger-payload">
                    {trigger.payload}
                  </Box>
                )}
                {(trigger?.timestamp || trigger?.receivedMs) && (
                  <Box variant="small" color="text-body-secondary">
                    {trigger?.timestamp ? `at ${formatTime(trigger.timestamp)}` : ''}
                    {trigger?.receivedMs ? ` received ${formatClock(trigger.receivedMs)}` : ''}
                  </Box>
                )}
              </SpaceBetween>
            }
          />
        </ColumnLayout>

        {(record.error || record.traceback) && (
          <Alert type="error" header={record.failingNodeId ? `Failed at ${record.failingNodeId}` : 'Run failed'} data-testid="run-error">
            <SpaceBetween size="xxs">
              {record.error && <Box>{record.error}</Box>}
              {record.traceback && (
                <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: '12px' }} data-testid="run-traceback">
                  {record.traceback}
                </pre>
              )}
            </SpaceBetween>
          </Alert>
        )}

        {(record.nodes?.length ?? 0) > 0 && (
          <Table
            variant="embedded"
            header={<Box variant="h4">Nodes</Box>}
            items={record.nodes ?? []}
            trackBy="nodeId"
            data-testid="run-nodes"
            columnDefinitions={[
              { id: 'node', header: 'Node', cell: (n) => n.nodeId },
              { id: 'status', header: 'Status', cell: (n) => n.status },
              { id: 'duration', header: 'Duration', cell: (n) => formatMs(n.durationMs) },
              { id: 'detail', header: 'Detail', cell: (n) => n.detail ?? '—' },
            ]}
          />
        )}

        {detections.length > 0 && (
          <Table
            variant="embedded"
            header={<Box variant="h4">Detections</Box>}
            items={detections.map((d, index) => ({ ...d, _key: d.id ?? `${d.label ?? 'det'}-${index}` }))}
            trackBy="_key"
            data-testid="run-detections"
            columnDefinitions={[
              { id: 'label', header: 'Label', cell: (d) => d.label ?? '—' },
              {
                id: 'confidence',
                header: 'Confidence',
                cell: (d) => (typeof d.confidence === 'number' ? d.confidence.toFixed(2) : '—'),
              },
              { id: 'box', header: 'Box', cell: (d) => (Array.isArray(d.box) ? d.box.map((v) => Math.round(Number(v))).join(', ') : '—') },
            ]}
          />
        )}

        {(Object.keys(llm).length > 0 || Object.keys(bedrock).length > 0 || record.outputs?.isAnomalous != null) && (
          <SpaceBetween size="xxs">
            <Box variant="h4">Model outputs</Box>
            {record.outputs?.isAnomalous != null && (
              <Box>
                Anomalous: {String(record.outputs.isAnomalous)}
                {typeof record.outputs.confidence === 'number' ? ` (confidence ${record.outputs.confidence.toFixed(2)})` : ''}
              </Box>
            )}
            {Object.entries(llm).map(([nodeId, out]) => (
              <Box key={`llm-${nodeId}`}>
                <Box variant="strong">llm {nodeId}: </Box>
                {out.error ? `error ${out.error}` : out.text ?? ''}
              </Box>
            ))}
            {Object.entries(bedrock).map(([nodeId, out]) => (
              <Box key={`bedrock-${nodeId}`}>
                <Box variant="strong">bedrock {nodeId}: </Box>
                {out.error ? `error ${out.error}` : out.text ?? ''}
              </Box>
            ))}
          </SpaceBetween>
        )}

        {(record.bindings?.length ?? 0) > 0 && (
          <Table
            variant="embedded"
            header={<Box variant="h4">Bindings</Box>}
            items={(record.bindings ?? []).map((b, index) => ({ ...b, _key: `${b.nodeId}-${b.kind}-${index}` }))}
            trackBy="_key"
            data-testid="run-bindings"
            columnDefinitions={[
              { id: 'node', header: 'Node', cell: (b) => b.nodeId },
              { id: 'kind', header: 'Kind', cell: (b) => b.kind },
              { id: 'result', header: 'Result', cell: (b) => b.result },
              { id: 'detail', header: 'Detail', cell: (b) => b.detail ?? '—' },
            ]}
          />
        )}

        {(record.model?.readiness?.length || record.model?.wait || record.frameSource || record.artifacts) && (
          <ColumnLayout columns={3} variant="text-grid">
            {(record.model?.readiness?.length || record.model?.wait) && (
              <Row
                label="Model"
                value={
                  <SpaceBetween size="xxs">
                    {(record.model?.readiness ?? []).map((r) => (
                      <Box key={r.model}>
                        {r.model}: {r.state}
                        {typeof r.waitedMs === 'number' ? ` after ${formatMs(r.waitedMs)}` : ''}
                      </Box>
                    ))}
                    {record.model?.wait && (
                      <Box>
                        waiting on {record.model.wait.model ?? record.model.wait.tritonModel}: {record.model.wait.state}
                        {record.model.wait.reason ? ` (${record.model.wait.reason})` : ''}
                        {record.model.wait.stalled ? ' stalled' : ''}
                      </Box>
                    )}
                  </SpaceBetween>
                }
              />
            )}
            {record.frameSource && (
              <Row
                label="Frame source"
                value={`${record.frameSource.kind ?? '—'}${record.frameSource.cameraId ? ` ${record.frameSource.cameraId}` : ''}${
                  record.frameSource.width && record.frameSource.height
                    ? ` ${record.frameSource.width}×${record.frameSource.height}`
                    : ''
                }${typeof record.frameSource.frameSeq === 'number' ? ` #${record.frameSource.frameSeq}` : ''}`}
              />
            )}
            {record.artifacts && (
              <Row
                label="Artifacts"
                value={`${record.artifacts.outputDir ?? '—'}${record.artifacts.captureId ? ` capture ${record.artifacts.captureId}` : ''}${
                  record.artifacts.hasOverlay ? ' (overlay)' : ''
                }`}
              />
            )}
          </ColumnLayout>
        )}

        {(record.logs?.length ?? 0) > 0 && (
          <SpaceBetween size="xxs">
            <Box variant="h4">Logs ({record.logs!.length})</Box>
            <div
              data-testid="run-logs"
              style={{
                fontFamily: 'Monaco, Consolas, "Courier New", monospace',
                fontSize: '12px',
                maxHeight: '320px',
                overflow: 'auto',
              }}
            >
              {record.logs!.map((line, index) => (
                <div key={`${line.ts}-${index}`} style={{ padding: '1px 0', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                  <span style={{ color: '#5f6b7a', marginRight: '6px' }}>{formatClock(line.ts)}</span>
                  <Badge color={line.src === 'tap' ? 'blue' : 'grey'}>{line.src}</Badge>{' '}
                  <span>
                    {line.level} {line.logger} — {line.message}
                  </span>
                </div>
              ))}
            </div>
          </SpaceBetween>
        )}

        {record.image && (
          <SpaceBetween size="xs">
            <Box variant="h4">Image</Box>
            <Box variant="small" color="text-body-secondary">
              {record.image.key}
              {record.image.width && record.image.height ? ` · ${record.image.width}×${record.image.height}` : ''}
              {typeof record.image.bytes === 'number' ? ` · ${formatBytes(record.image.bytes)}` : ''}
            </Box>
            {imageUrl ? (
              <img src={imageUrl} alt={`Run ${record.executionId}`} style={{ maxWidth: '100%', maxHeight: '540px' }} data-testid="run-image" />
            ) : (
              <Button onClick={loadImage} loading={imageLoading} disabled={!sessionId} data-testid="run-image-load">
                Load image
              </Button>
            )}
            {imageNotice && (
              <Alert type={imageNotice.type} data-testid="run-image-notice">
                {imageNotice.text}
              </Alert>
            )}
          </SpaceBetween>
        )}

        {truncated.length > 0 && (
          <SpaceBetween direction="horizontal" size="xs">
            {truncated.map((marker) => (
              <Badge key={marker.field} color="blue">
                truncated {marker.field} ({formatBytes(marker.originalBytes)})
              </Badge>
            ))}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </ExpandableSection>
  );
}
