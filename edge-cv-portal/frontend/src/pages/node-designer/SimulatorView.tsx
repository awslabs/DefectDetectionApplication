/**
 * Plugin_Simulator view (custom-node-designer, task 12.4).
 *
 * Simulate one Plugin_Record version against a Use_Case-scoped
 * Test_Dataset or uploaded sample frames (Requirement 7.1); render the
 * side-by-side input/output frame strips with the per-frame emitted
 * metadata (7.3); edit parameter values and re-run (7.4); show the
 * missing-x86_64 refusal (7.5) and failure/timeout alerts with the
 * partial results produced before termination (7.6, 7.7).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import {
  Alert,
  AttributeEditor,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  Select,
  SelectProps,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Tiles,
} from '@cloudscape-design/components';
import { useNavigate, useParams } from 'react-router-dom';
import { nodeDesignerApi } from './api';
import {
  PluginVersionDetail,
  SampleFrameUpload,
  SimulationFrameRecord,
  SimulationResultsDocument,
  SimulationRunSummary,
  StartSimulationRequest,
  TestDatasetSummary,
} from './types';
import { LifecycleBadge } from './badges';
import {
  ParameterRow,
  MISSING_X86_64_MESSAGE,
  dataUrlToBase64,
  describeRunFailure,
  describeStartError,
  frameLabel,
  hasSuccessfulX86Build,
  isRenderableUrl,
  isSupportedFrameName,
  isTerminalStatus,
  orderedFrames,
  parametersFromRows,
} from './simulation';

/** Poll the run every 5 s while it executes (results flush incrementally). */
const RUN_POLL_MS = 5_000;

const frameTileStyle: CSSProperties = {
  border: '1px solid #d5dbdb',
  borderRadius: '4px',
  padding: '8px',
  minHeight: '96px',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  background: '#fafafa',
  overflow: 'hidden',
};

/** One frame tile: the image when the ref is a URL, else its file name. */
function FrameTile({ refKey, alt }: { refKey: string | null; alt: string }) {
  if (!refKey) {
    return (
      <div style={frameTileStyle}>
        <Box color="text-status-inactive">no output frame</Box>
      </div>
    );
  }
  if (isRenderableUrl(refKey)) {
    return (
      <div style={frameTileStyle}>
        <img src={refKey} alt={alt} style={{ maxWidth: '100%', maxHeight: '160px' }} />
      </div>
    );
  }
  return (
    <div style={frameTileStyle}>
      <Box fontSize="body-s" color="text-body-secondary">
        {frameLabel(refKey)}
      </Box>
    </div>
  );
}

/** One row of the side-by-side strip: input, output, emitted metadata (7.3). */
function FrameRow({ record }: { record: SimulationFrameRecord }) {
  const metadata = record.metadata || {};
  return (
    <div data-testid={`frame-row-${record.frameIndex}`}>
      <Box variant="awsui-key-label">Frame {record.frameIndex}</Box>
      <ColumnLayout columns={3} variant="text-grid">
        <div>
          <Box fontSize="body-s" color="text-body-secondary">
            Input
          </Box>
          <FrameTile refKey={record.inputRef} alt={`Input frame ${record.frameIndex}`} />
        </div>
        <div>
          <Box fontSize="body-s" color="text-body-secondary">
            Output
          </Box>
          <FrameTile refKey={record.outputRef} alt={`Output frame ${record.frameIndex}`} />
        </div>
        <div>
          <Box fontSize="body-s" color="text-body-secondary">
            Emitted metadata
          </Box>
          <pre
            style={{
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              margin: 0,
              fontSize: '12px',
              background: '#f2f3f3',
              padding: '8px',
              borderRadius: '4px',
              maxHeight: '160px',
              overflow: 'auto',
            }}
          >
            {Object.keys(metadata).length > 0 ? JSON.stringify(metadata, null, 2) : '—'}
          </pre>
        </div>
      </ColumnLayout>
    </div>
  );
}

export default function SimulatorView() {
  const navigate = useNavigate();
  const { pluginId, version } = useParams<{ pluginId: string; version: string }>();
  const versionNumber = Number(version);

  const [plugin, setPlugin] = useState<PluginVersionDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Input source (7.1): a Use_Case Test_Dataset or uploaded sample frames.
  const [inputKind, setInputKind] = useState<'dataset' | 'upload'>('dataset');
  const [datasets, setDatasets] = useState<TestDatasetSummary[]>([]);
  const [datasetsError, setDatasetsError] = useState<string | null>(null);
  const [selectedDataset, setSelectedDataset] = useState<SelectProps.Option | null>(null);
  const [sampleFrames, setSampleFrames] = useState<SampleFrameUpload[]>([]);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Parameter editor (7.4).
  const [parameterRows, setParameterRows] = useState<ParameterRow[]>([]);
  const [elementFactory, setElementFactory] = useState('');

  // Run state.
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [run, setRun] = useState<SimulationRunSummary | null>(null);
  const [results, setResults] = useState<SimulationResultsDocument | null>(null);

  // ------------------------------------------------------------- loading

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!pluginId || !Number.isInteger(versionNumber)) {
        setLoadError('Invalid plugin version');
        setLoading(false);
        return;
      }
      try {
        const response = await nodeDesignerApi.getVersion(pluginId, versionNumber);
        if (cancelled) return;
        setPlugin(response.plugin);
        try {
          const list = await nodeDesignerApi.listTestDatasets(response.plugin.usecase_id);
          if (!cancelled) setDatasets(list.datasets || []);
        } catch (err: any) {
          if (!cancelled) {
            setDatasetsError(err.message || 'Failed to load Test_Datasets');
          }
        }
      } catch (err: any) {
        if (!cancelled) setLoadError(err.message || 'Failed to load the plugin record');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pluginId, versionNumber]);

  // Poll the running simulation until it settles; the results document is
  // flushed incrementally, so partial frames render while it runs and
  // survive failures/timeouts (7.6, 7.7).
  useEffect(() => {
    if (!run || isTerminalStatus(run.status)) {
      return;
    }
    const timer = setInterval(async () => {
      try {
        const response = await nodeDesignerApi.getSimulation(run.run_id);
        setRun(response.simulation_run);
        setResults(response.results);
      } catch {
        // transient poll failure: keep the last known state
      }
    }, RUN_POLL_MS);
    return () => clearInterval(timer);
  }, [run]);

  // -------------------------------------------------------------- actions

  const onFilesSelected = useCallback(async (fileList: FileList | null) => {
    setUploadError(null);
    const files = Array.from(fileList || []);
    if (files.length === 0) return;
    const unsupported = files.find((f) => !isSupportedFrameName(f.name));
    if (unsupported) {
      setUploadError(
        `'${unsupported.name}' is not a supported sample frame: only JPEG and PNG images are accepted.`
      );
      return;
    }
    const encoded = await Promise.all(
      files.map(
        (file) =>
          new Promise<SampleFrameUpload>((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () =>
              resolve({
                name: file.name,
                content_base64: dataUrlToBase64(String(reader.result)),
              });
            reader.onerror = () => reject(new Error(`Could not read '${file.name}'`));
            reader.readAsDataURL(file);
          })
      )
    ).catch((err: Error) => {
      setUploadError(err.message);
      return null;
    });
    if (encoded) {
      setSampleFrames(encoded);
    }
  }, []);

  const startRun = async () => {
    if (!pluginId || !plugin) return;
    setStartError(null);

    const body: StartSimulationRequest = {
      parameters: parametersFromRows(parameterRows),
    };
    if (elementFactory.trim()) {
      body.element_factory = elementFactory.trim();
    }
    if (inputKind === 'dataset') {
      if (!selectedDataset?.value) {
        setStartError('Select a Test_Dataset to simulate against.');
        return;
      }
      body.dataset_id = selectedDataset.value;
    } else {
      if (sampleFrames.length === 0) {
        setStartError('Upload at least one sample frame (JPEG or PNG).');
        return;
      }
      body.sample_frames = sampleFrames;
    }

    setStarting(true);
    setResults(null);
    try {
      const response = await nodeDesignerApi.startSimulation(
        pluginId,
        versionNumber,
        body
      );
      setRun(response.simulation_run);
    } catch (err) {
      setStartError(describeStartError(err));
    } finally {
      setStarting(false);
    }
  };

  // ------------------------------------------------------------ rendering

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (loadError || !plugin) {
    return (
      <SpaceBetween size="l">
        <Alert type="error">{loadError || 'Plugin record not found'}</Alert>
        <Button onClick={() => navigate('/node-designer')}>Back to Node Designer</Button>
      </SpaceBetween>
    );
  }

  const guardOk = hasSuccessfulX86Build(plugin.artifacts);
  const frames = orderedFrames(results);
  const failure = run ? describeRunFailure(run, results) : null;
  const running = run !== null && !isTerminalStatus(run.status);

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Run the plugin against sample frames and inspect its output and emitted metadata."
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <LifecycleBadge state={plugin.lifecycle_state} />
            <Button onClick={() => navigate(`/node-designer/plugins/${plugin.plugin_id}`)}>
              Back to plugin
            </Button>
          </SpaceBetween>
        }
      >
        Simulate {plugin.name} v{plugin.version}
      </Header>

      {/* Missing-x86_64 refusal (7.5): shown up front; the run button stays
          disabled because the backend refuses the start with a 409 anyway. */}
      {!guardOk && (
        <Alert type="error" header="Simulation unavailable">
          {MISSING_X86_64_MESSAGE}
        </Alert>
      )}

      <Container header={<Header variant="h2">Input</Header>}>
        <SpaceBetween size="m">
          <Tiles
            value={inputKind}
            onChange={({ detail }) => setInputKind(detail.value as 'dataset' | 'upload')}
            items={[
              {
                value: 'dataset',
                label: 'Test_Dataset',
                description: 'An existing dataset of this Use_Case',
              },
              {
                value: 'upload',
                label: 'Upload sample frames',
                description: 'JPEG/PNG frames uploaded for this run',
              },
            ]}
          />
          {inputKind === 'dataset' ? (
            <FormField
              label="Test_Dataset"
              description="Datasets are scoped to the plugin's Use_Case."
              errorText={datasetsError || undefined}
            >
              <Select
                placeholder="Select a Test_Dataset"
                selectedOption={selectedDataset}
                onChange={({ detail }) => setSelectedDataset(detail.selectedOption)}
                options={datasets.map((d) => ({
                  label: d.name,
                  value: d.dataset_id,
                  description: `${d.file_count ?? '?'} file(s)`,
                }))}
                empty="No Test_Datasets in this Use_Case"
              />
            </FormField>
          ) : (
            <FormField
              label="Sample frames"
              description="JPEG or PNG images; larger inputs should use a Test_Dataset."
              errorText={uploadError || undefined}
            >
              <SpaceBetween size="xs">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".jpg,.jpeg,.png"
                  multiple
                  aria-label="Upload sample frames"
                  onChange={(e) => onFilesSelected(e.target.files)}
                />
                {sampleFrames.length > 0 && (
                  <Box fontSize="body-s" color="text-body-secondary">
                    {sampleFrames.length} frame(s) selected
                  </Box>
                )}
              </SpaceBetween>
            </FormField>
          )}
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            description="Declared parameter values applied to the element for this run. Change values and run again to compare."
          >
            Parameters
          </Header>
        }
      >
        <SpaceBetween size="m">
          <AttributeEditor
            addButtonText="Add parameter"
            removeButtonText="Remove"
            empty="No parameter values set for this run."
            items={parameterRows}
            onAddButtonClick={() =>
              setParameterRows([...parameterRows, { name: '', value: '' }])
            }
            onRemoveButtonClick={({ detail }) =>
              setParameterRows(parameterRows.filter((_, i) => i !== detail.itemIndex))
            }
            definition={[
              {
                label: 'Name',
                control: (item: ParameterRow, index: number) => (
                  <Input
                    value={item.name}
                    placeholder="property-name"
                    ariaLabel={`Parameter ${index + 1} name`}
                    onChange={({ detail }) =>
                      setParameterRows(
                        parameterRows.map((row, i) =>
                          i === index ? { ...row, name: detail.value } : row
                        )
                      )
                    }
                  />
                ),
              },
              {
                label: 'Value',
                control: (item: ParameterRow, index: number) => (
                  <Input
                    value={item.value}
                    placeholder="value"
                    ariaLabel={`Parameter ${index + 1} value`}
                    onChange={({ detail }) =>
                      setParameterRows(
                        parameterRows.map((row, i) =>
                          i === index ? { ...row, value: detail.value } : row
                        )
                      )
                    }
                  />
                ),
              },
            ]}
          />
          <FormField
            label="Element factory (optional)"
            description="GStreamer element factory name; derived from the plugin name when omitted."
          >
            <Input
              value={elementFactory}
              placeholder={plugin.name}
              onChange={({ detail }) => setElementFactory(detail.value)}
            />
          </FormField>
          {startError && <Alert type="error">{startError}</Alert>}
          <Button
            variant="primary"
            onClick={startRun}
            disabled={!guardOk || starting || running}
            loading={starting}
          >
            {run ? 'Re-run simulation' : 'Run simulation'}
          </Button>
        </SpaceBetween>
      </Container>

      {run && (
        <Container
          header={
            <Header
              variant="h2"
              actions={
                running ? (
                  <StatusIndicator type="in-progress">Running</StatusIndicator>
                ) : run.status === 'completed' ? (
                  <StatusIndicator type="success">Completed</StatusIndicator>
                ) : (
                  <StatusIndicator type="error">
                    {failure?.timeout ? 'Timed out' : 'Failed'}
                  </StatusIndicator>
                )
              }
            >
              Results
            </Header>
          }
        >
          <SpaceBetween size="m">
            {/* Failure/timeout display (7.6, 7.7): the partial results the
                harness flushed before termination stay rendered below. */}
            {failure && (
              <Alert type="error" header={failure.header}>
                <SpaceBetween size="xs">
                  <div>{failure.message}</div>
                  {failure.errorOutput && (
                    <pre
                      style={{
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word',
                        margin: 0,
                        fontSize: '12px',
                      }}
                    >
                      {failure.errorOutput}
                    </pre>
                  )}
                  {frames.length > 0 && (
                    <Box fontSize="body-s">
                      Partial results: {frames.length} frame(s) were produced before
                      the run ended.
                    </Box>
                  )}
                </SpaceBetween>
              </Alert>
            )}

            {frames.length === 0 ? (
              <Box color="text-status-inactive">
                {running
                  ? 'Waiting for the first frames…'
                  : 'No frame results were produced.'}
              </Box>
            ) : (
              <SpaceBetween size="m">
                {frames.map((record) => (
                  <FrameRow key={record.frameIndex} record={record} />
                ))}
              </SpaceBetween>
            )}
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
}
