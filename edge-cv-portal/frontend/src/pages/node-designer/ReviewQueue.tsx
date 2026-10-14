/**
 * PortalAdmin security review queue (custom-node-designer, task 12.5,
 * Requirements 10.2, 15.6).
 *
 * Pending Plugin_Record versions (review.decision = pending) with full
 * provenance (repository URL/revision, scaffold origin or generation
 * prompt, importing/creating user, timestamps), the Plugin_Set
 * classification, per-architecture Plugin_Artifact checksums and
 * signatures, and plugin source inspection. Approve/reject actions call
 * POST /plugins/{id}/versions/{v}/review (PortalAdmin only; the
 * decision, acting PortalAdmin, and timestamp are recorded in the audit
 * log server-side, Requirement 10.3).
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  Modal,
  Select,
  SelectProps,
  SpaceBetween,
  Spinner,
  Table,
} from '@cloudscape-design/components';
import { useAuth } from '../../contexts/AuthContext';
import { nodeDesignerApi } from './api';
import {
  ARCHITECTURE_LABELS,
  PluginRecordSummary,
  PluginVersionDetail,
} from './types';
import { BuildStatusIndicator, ClassificationBadge, LifecycleBadge } from './badges';

/** Human-readable provenance labels for the review display (10.2). */
const PROVENANCE_LABELS: Record<string, string> = {
  repoUrl: 'Repository URL',
  revision: 'Revision',
  prompt: 'Generation prompt',
  scaffoldDeclaration: 'Scaffold declaration',
  importedBy: 'Imported by',
  createdBy: 'Created by',
  importedAt: 'Imported at',
  createdAt: 'Created at',
  classification: 'Classification',
  prebuilt: 'Prebuilt binary',
};

function provenanceValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return '—';
  if ((key === 'importedAt' || key === 'createdAt') && typeof value === 'number') {
    return new Date(value).toLocaleString();
  }
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value);
}

export default function ReviewQueue() {
  const { user } = useAuth();
  const isPortalAdmin = user?.role === 'PortalAdmin';

  const [pending, setPending] = useState<PluginRecordSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [selected, setSelected] = useState<PluginVersionDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [sourceFiles, setSourceFiles] = useState<Array<{ file: string; size: number }>>([]);
  const [selectedFile, setSelectedFile] = useState<SelectProps.Option | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);

  const [decision, setDecision] = useState<'approved' | 'rejected' | null>(null);
  const [notes, setNotes] = useState('');
  const [deciding, setDeciding] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);

  const loadQueue = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await nodeDesignerApi.listPendingReviews();
      setPending(response.plugins || []);
    } catch (err: any) {
      setError(err.message || 'Failed to load the review queue');
      setPending([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isPortalAdmin) {
      loadQueue();
    }
  }, [isPortalAdmin, loadQueue]);

  const openRecord = async (item: PluginRecordSummary) => {
    setDetailLoading(true);
    setSelected(null);
    setSourceFiles([]);
    setSelectedFile(null);
    setFileContent(null);
    setDecisionError(null);
    try {
      const response = await nodeDesignerApi.getVersion(item.plugin_id, item.version);
      setSelected(response.plugin);
      const source = await nodeDesignerApi
        .getVersionSource(item.plugin_id, item.version)
        .catch(() => null);
      setSourceFiles(source?.files || []);
    } catch (err: any) {
      setError(err.message || 'Failed to load the plugin record');
    } finally {
      setDetailLoading(false);
    }
  };

  const showFile = async (option: SelectProps.Option) => {
    setSelectedFile(option);
    setFileContent(null);
    if (!selected || !option.value) return;
    try {
      const response = await nodeDesignerApi.getVersionSource(
        selected.plugin_id,
        selected.version,
        option.value
      );
      setFileContent(response.content ?? '');
    } catch (err: any) {
      setFileContent(`Failed to load file: ${err.message}`);
    }
  };

  const decide = async () => {
    if (!selected || !decision) return;
    setDeciding(true);
    setDecisionError(null);
    try {
      await nodeDesignerApi.reviewVersion(
        selected.plugin_id,
        selected.version,
        decision,
        notes.trim() || undefined
      );
      setDecision(null);
      setNotes('');
      setSelected(null);
      await loadQueue();
    } catch (err: any) {
      setDecisionError(err.message || 'Failed to record the review decision');
    } finally {
      setDeciding(false);
    }
  };

  if (!isPortalAdmin) {
    return (
      <SpaceBetween size="l">
        <Header variant="h1">Plugin security review</Header>
        <Alert type="warning" header="PortalAdmin only">
          Approving or rejecting plugin security reviews requires the
          PortalAdmin role.
        </Alert>
      </SpaceBetween>
    );
  }

  const classification = (selected?.provenance?.classification as string) || null;

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Pending plugin security reviews: inspect provenance, classification, artifact checksums and signatures, and source before approving or rejecting."
        actions={
          <Button iconName="refresh" ariaLabel="Refresh review queue" onClick={loadQueue} />
        }
      >
        Plugin security review
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Table<PluginRecordSummary>
        items={pending}
        loading={loading}
        loadingText="Loading pending reviews"
        trackBy={(item) => `${item.plugin_id}:${item.version}`}
        columnDefinitions={[
          {
            id: 'name',
            header: 'Name',
            cell: (item) => (
              <Button variant="inline-link" onClick={() => openRecord(item)}>
                {item.name}
              </Button>
            ),
          },
          { id: 'version', header: 'Version', cell: (item) => `v${item.version}` },
          { id: 'usecase', header: 'Use case', cell: (item) => item.usecase_id },
          { id: 'kind', header: 'Origin', cell: (item) => item.kind },
          {
            id: 'lifecycle',
            header: 'Lifecycle',
            cell: (item) => <LifecycleBadge state={item.lifecycle_state} />,
          },
          {
            id: 'classification',
            header: 'Classification',
            cell: (item) => <ClassificationBadge classification={item.classification} />,
          },
          {
            id: 'updated',
            header: 'Updated',
            cell: (item) =>
              item.updated_at ? new Date(item.updated_at).toLocaleString() : '—',
          },
        ]}
        empty={
          <Box textAlign="center" color="inherit">
            <b>No pending reviews</b>
            <Box variant="p" color="inherit">
              Every plugin record version has a recorded security review decision.
            </Box>
          </Box>
        }
        header={<Header counter={`(${pending.length})`}>Pending plugin records</Header>}
      />

      {detailLoading && (
        <Box textAlign="center" padding="l">
          <Spinner size="large" />
        </Box>
      )}

      {selected && (
        <SpaceBetween size="l">
          <Header
            variant="h2"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setDecision('rejected')}>Reject</Button>
                <Button variant="primary" onClick={() => setDecision('approved')}>
                  Approve
                </Button>
              </SpaceBetween>
            }
          >
            Review: {selected.name} v{selected.version}
          </Header>

          {decisionError && (
            <Alert type="error" dismissible onDismiss={() => setDecisionError(null)}>
              {decisionError}
            </Alert>
          )}

          <Container header={<Header variant="h3">Provenance</Header>}>
            <ColumnLayout columns={2} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Origin</Box>
                <div>{selected.kind}</div>
              </div>
              <div>
                <Box variant="awsui-key-label">Classification</Box>
                <ClassificationBadge classification={classification} />
              </div>
              <div>
                <Box variant="awsui-key-label">Created by</Box>
                <div>{selected.created_by || '—'}</div>
              </div>
              <div>
                <Box variant="awsui-key-label">Created</Box>
                <div>
                  {selected.created_at
                    ? new Date(selected.created_at).toLocaleString()
                    : '—'}
                </div>
              </div>
              {Object.entries(selected.provenance || {})
                .filter(([key]) => key !== 'classification')
                .map(([key, value]) => (
                  <div key={key}>
                    <Box variant="awsui-key-label">{PROVENANCE_LABELS[key] || key}</Box>
                    <div style={{ wordBreak: 'break-word', whiteSpace: 'pre-wrap' }}>
                      {provenanceValue(key, value)}
                    </div>
                  </div>
                ))}
            </ColumnLayout>
          </Container>

          <Container header={<Header variant="h3">Artifacts</Header>}>
            {Object.keys(selected.artifacts || {}).length === 0 ? (
              <Box color="text-status-inactive">No built artifacts.</Box>
            ) : (
              <Table
                items={Object.keys(selected.artifacts || {})
                  .sort()
                  .map((arch) => ({ arch, ...(selected.artifacts?.[arch] || {}) }))}
                trackBy={(item) => item.arch}
                variant="embedded"
                columnDefinitions={[
                  {
                    id: 'arch',
                    header: 'Architecture',
                    cell: (item) =>
                      ARCHITECTURE_LABELS[
                        item.arch as keyof typeof ARCHITECTURE_LABELS
                      ] ?? item.arch,
                  },
                  {
                    id: 'status',
                    header: 'Build',
                    cell: (item) => (
                      <BuildStatusIndicator
                        arch={item.arch}
                        status={item.buildStatus}
                        logTail={item.logTail}
                      />
                    ),
                  },
                  {
                    id: 'checksum',
                    header: 'Checksum (SHA-256)',
                    cell: (item) => (
                      <code style={{ fontSize: '12px', wordBreak: 'break-all' }}>
                        {item.checksum || '—'}
                      </code>
                    ),
                  },
                  {
                    id: 'signature',
                    header: 'Signature',
                    cell: (item) => (
                      <code style={{ fontSize: '12px', wordBreak: 'break-all' }}>
                        {item.signature || '—'}
                      </code>
                    ),
                  },
                ]}
                empty={<Box textAlign="center">No artifacts</Box>}
              />
            )}
          </Container>

          <Container
            header={
              <Header variant="h3" counter={`(${sourceFiles.length})`}>
                Source inspection
              </Header>
            }
          >
            <SpaceBetween size="m">
              <Select
                placeholder="Select a source file to inspect"
                selectedOption={selectedFile}
                options={sourceFiles.map((f) => ({ label: f.file, value: f.file }))}
                onChange={({ detail }) => showFile(detail.selectedOption)}
                empty="No source files"
              />
              {selectedFile && (
                <pre
                  style={{
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    fontSize: '12px',
                    background: '#f2f3f3',
                    padding: '12px',
                    borderRadius: '4px',
                    maxHeight: '400px',
                    overflow: 'auto',
                  }}
                >
                  {fileContent === null ? 'Loading…' : fileContent}
                </pre>
              )}
            </SpaceBetween>
          </Container>
        </SpaceBetween>
      )}

      <Modal
        visible={decision !== null}
        onDismiss={() => setDecision(null)}
        header={decision === 'approved' ? 'Approve security review' : 'Reject security review'}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setDecision(null)}>Cancel</Button>
              <Button variant="primary" loading={deciding} onClick={decide}>
                {decision === 'approved' ? 'Approve' : 'Reject'}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            Record the {decision} decision for {selected?.name} v{selected?.version}?
            The decision, your identity, and a timestamp are written to the audit log.
          </Box>
          <FormField label={<span>Notes <i>- optional</i></span>}>
            <Input value={notes} onChange={({ detail }) => setNotes(detail.value)} />
          </FormField>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
