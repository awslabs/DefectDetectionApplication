/**
 * Workflow test panel for the Workflow_Builder (Requirements 12.1, 12.2, 12.8).
 *
 * Rendered inside the builder's right-hand side drawer (the "Test" tab,
 * sibling of the generation chat panel), providing the user-invocable
 * test action (12.1). Invoking a test prompts the user to
 * select an existing Test_Dataset scoped to the selected Use_Case or to
 * upload new sample inputs (JPEG/PNG) scoped to that Use_Case (12.2).
 * Uploads follow the backend's presigned-multipart flow: initiate (declare
 * the file set), PUT every part to its presigned URL, then finalize —
 * server-side verification commits the dataset record (12.3, handled
 * backend-side).
 *
 * A started run is polled via GET /test-runs/{id} until it completes or
 * fails; while it runs, the backend's step-level `progress` entries
 * (most recent last) and the count of per-node results flushed so far
 * are rendered as live progress under the "Test in progress" indicator;
 * the same entries appear as the "Run log" in the final report. The active run's identity is
 * persisted in localStorage so navigating away does not lose track of it:
 * on the next mount the panel resumes polling (or shows the final report
 * when the run finished in the meantime), clearing the marker once the
 * result has been displayed. On completion the per-node results are
 * displayed as the test report. Every node executed with
 * a stub (non-empty `stubActivity`) is marked with a "Simulated" badge and
 * the report describes the limitation that stubbed nodes were simulated
 * rather than actuated (12.8). The notes the harness recorded with the
 * stub activity are displayed as each stubbed node's limitation text —
 * for a stubbed Custom_Node_Type the note describes that the node was
 * simulated because no x86_64 build exists (custom-node-designer 12.3).
 *
 * Role gating: the test action requires workflow:test (DataScientist,
 * UseCaseAdmin, PortalAdmin per the design RBAC matrix); the action is also
 * unavailable until the workflow is saved, since test runs execute a stored
 * workflow version.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Checkbox from '@cloudscape-design/components/checkbox';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select, { type SelectProps } from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { apiService } from '../../services/api';
import type { UserRole } from '../../types';
import type { WorkflowMeta } from './WorkflowToolbar';
import type {
  TestDataset,
  TestDatasetCompletedFile,
  TestRunNodeResult,
  TestRunProgressEntry,
  WorkflowDefinition,
  WorkflowTestRun,
  WorkflowTestRunDetail,
} from './types';

// --------------------------------------------------------------------------
// Role gating (design RBAC matrix: workflow:test)
// --------------------------------------------------------------------------

/** Roles permitted to run workflow tests (design RBAC matrix). */
export const WORKFLOW_TEST_ROLES: readonly UserRole[] = [
  'DataScientist',
  'UseCaseAdmin',
  'PortalAdmin',
];

/** True when the role may invoke the test action. */
export function canTestWorkflows(role: UserRole | undefined | null): boolean {
  return role !== undefined && role !== null && WORKFLOW_TEST_ROLES.includes(role);
}

// --------------------------------------------------------------------------
// Constants and helpers
// --------------------------------------------------------------------------

/**
 * The limitation shown with every test report containing stubbed nodes
 * (Requirement 12.8): stubbed nodes were simulated rather than actuated.
 */
export const SIMULATED_LIMITATION_TEXT =
  'Nodes marked "Simulated" were executed with stubs: no camera, digital ' +
  'I/O, MQTT broker, or OPC UA endpoint was actuated. The recorded stub ' +
  'activity shows the data each node would have consumed or emitted on a ' +
  'real device.';

/**
 * Help text of the simulated inference outcome section (Requirement
 * 12.6): the model is never executed in cloud test runs; the configured
 * outcome is injected as the inference metadata.
 */
export const SIMULATED_INFERENCE_HELP_TEXT =
  'The model is not executed in cloud tests. The outcome configured here ' +
  'is injected as the inference result and drives downstream nodes ' +
  '(filters, conditionals, outputs).';

/** Default simulated confidence (mirrors the backend default). */
const DEFAULT_SIMULATED_CONFIDENCE = '0.9';

/**
 * Node types whose inference is stubbed in cloud test runs, so the
 * panel offers the simulated inference outcome controls (12.6):
 * model_inference (device-compiled models never run in the sandbox)
 * and bedrock_inference (the sandbox VPC has no internet, so the
 * Bedrock model is never invoked).
 */
const SIMULATED_INFERENCE_NODE_TYPES = ['model_inference', 'bedrock_inference'];

/**
 * Parse the simulated-confidence input: a number between 0 and 1, or
 * null when the text is not a valid confidence.
 */
export function parseSimulatedConfidence(text: string): number | null {
  if (text.trim() === '') {
    return null;
  }
  const value = Number(text);
  return Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
}

/** Accepted upload formats (backend verifies JPEG/PNG server-side, 12.3). */
const UPLOAD_ACCEPT = 'image/jpeg,image/png,.jpg,.jpeg,.png';

/** Default interval between test-run status polls. */
const DEFAULT_POLL_INTERVAL_MS = 3000;

/**
 * localStorage key persisting the active test run so it survives
 * navigation: the run keeps executing server-side, and the panel resumes
 * polling it when the user returns. Cleared once the final report has
 * been shown.
 */
export const ACTIVE_TEST_RUN_STORAGE_KEY = 'edgeCvPortal.activeTestRun';

/** The persisted identity of an in-flight test run. */
interface PersistedTestRun {
  testRunId: string;
  workflowId: string;
  usecaseId: string;
}

/** Read the persisted active test run, or null (missing or malformed). */
export function readPersistedTestRun(): PersistedTestRun | null {
  try {
    const raw = window.localStorage.getItem(ACTIVE_TEST_RUN_STORAGE_KEY);
    if (raw === null) {
      return null;
    }
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === 'object' &&
      parsed !== null &&
      typeof (parsed as PersistedTestRun).testRunId === 'string' &&
      typeof (parsed as PersistedTestRun).workflowId === 'string' &&
      typeof (parsed as PersistedTestRun).usecaseId === 'string'
    ) {
      return parsed as PersistedTestRun;
    }
  } catch {
    // Storage unavailable or malformed content: treat as no active run.
  }
  return null;
}

function persistTestRun(run: PersistedTestRun): void {
  try {
    window.localStorage.setItem(ACTIVE_TEST_RUN_STORAGE_KEY, JSON.stringify(run));
  } catch {
    // Storage unavailable: the run still polls for this mount.
  }
}

function clearPersistedTestRun(): void {
  try {
    window.localStorage.removeItem(ACTIVE_TEST_RUN_STORAGE_KEY);
  } catch {
    // Storage unavailable: nothing to clear.
  }
}

/** True when the node was executed with a stub (12.6, 12.8). */
export function isStubbedResult(result: TestRunNodeResult): boolean {
  return Array.isArray(result.stubActivity) && result.stubActivity.length > 0;
}

/**
 * The distinct limitation notes recorded with a stubbed node's stub
 * activity, in recorded order. The harness attaches a `note` describing
 * each substitution — for a stubbed Custom_Node_Type the note describes
 * the limitation that the node was simulated because no x86_64 build
 * exists (custom-node-designer Requirement 12.3). Entries without a
 * note (or non-object entries) contribute nothing.
 */
export function stubActivityNotes(result: TestRunNodeResult): string[] {
  if (!Array.isArray(result.stubActivity)) {
    return [];
  }
  const notes: string[] = [];
  for (const entry of result.stubActivity) {
    if (typeof entry === 'object' && entry !== null && !Array.isArray(entry)) {
      const note = (entry as { [key: string]: unknown }).note;
      if (typeof note === 'string' && note !== '' && !notes.includes(note)) {
        notes.push(note);
      }
    }
  }
  return notes;
}

/**
 * The run's well-formed progress entries in recorded order (most recent
 * last). Malformed entries are dropped rather than crashing the panel.
 */
export function progressEntries(run: WorkflowTestRun): TestRunProgressEntry[] {
  if (!Array.isArray(run.progress)) {
    return [];
  }
  return run.progress.filter(
    (entry): entry is TestRunProgressEntry =>
      typeof entry === 'object' &&
      entry !== null &&
      typeof entry.message === 'string' &&
      entry.message !== ''
  );
}

/**
 * The live-view progress entries: the run's recorded step entries, and
 * once per-node results start flushing during the run, an
 * "Executing pipeline — N node(s) reported" entry as the latest line.
 */
export function liveProgressEntries(
  detail: WorkflowTestRunDetail
): TestRunProgressEntry[] {
  const entries = progressEntries(detail.test_run);
  if (detail.node_results.length > 0) {
    const latestAt = entries.length > 0 ? entries[entries.length - 1].at : 0;
    entries.push({
      at: latestAt,
      message: `Executing pipeline — ${detail.node_results.length} node(s) reported`,
    });
  }
  return entries;
}

/**
 * The step-progress list rendered while polling and in the report. With
 * `emphasizeLatest` (the live view), the most recent entry - what the run
 * is doing right now - is emphasized while prior entries stay as small
 * secondary text.
 */
function ProgressLog({
  entries,
  emphasizeLatest = false,
}: {
  entries: TestRunProgressEntry[];
  emphasizeLatest?: boolean;
}) {
  return (
    <ul
      aria-label="Test run progress log"
      style={{ listStyle: 'none', margin: 0, padding: 0 }}
    >
      {entries.map((entry, index) => (
        <li key={`${entry.at}-${index}`}>
          {emphasizeLatest && index === entries.length - 1 ? (
            <Box variant="strong">{entry.message}</Box>
          ) : (
            <Box color="text-body-secondary" fontSize="body-s">
              {entry.message}
            </Box>
          )}
        </li>
      ))}
    </ul>
  );
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function datasetOption(dataset: TestDataset): SelectProps.Option {
  const files = dataset.file_count !== undefined ? `${dataset.file_count} file(s)` : undefined;
  return {
    label: dataset.name,
    value: dataset.dataset_id,
    description: files,
  };
}

function nodeStatusType(result: TestRunNodeResult): 'success' | 'error' | 'pending' {
  if (result.error !== null || result.status === 'failed' || result.status === 'error') {
    return 'error';
  }
  return result.status === 'completed' ? 'success' : 'pending';
}

// --------------------------------------------------------------------------
// Props
// --------------------------------------------------------------------------

export interface TestPanelProps {
  /** The acting user's role; testing requires workflow:test. */
  role: UserRole | undefined;
  /** The selected Use_Case scoping datasets and test runs (12.2). */
  usecaseId: string | null;
  /** The saved workflow on the canvas; tests run stored versions. */
  workflow: WorkflowMeta | null;
  /**
   * The current canvas as a Workflow_Definition document (provided by
   * the builder). When it contains a model_inference node, the panel
   * offers the simulated inference outcome controls: the model is not
   * executed in cloud test runs, so the user chooses the injected
   * outcome per run (Requirement 12.6).
   */
  getDefinition?: () => WorkflowDefinition;
  /** Poll interval override for test-run status (tests use a small value). */
  pollIntervalMs?: number;
  /**
   * Whether the panel is currently shown to the user (drawer open on the
   * Test tab). Dataset loading is deferred until the panel first becomes
   * active for the selected Use_Case; the panel stays mounted while
   * inactive so selections, uploads, and run results are preserved.
   */
  active?: boolean;
}

// --------------------------------------------------------------------------
// Panel
// --------------------------------------------------------------------------

export default function TestPanel({
  role,
  usecaseId,
  workflow,
  getDefinition,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  active = true,
}: TestPanelProps) {
  const canTest = canTestWorkflows(role);

  // Simulated inference outcome (12.6): offered when the workflow on the
  // canvas contains a node whose inference is stubbed in the cloud
  // sandbox — model_inference (device-compiled models, no emltriton in
  // the sandbox) and bedrock_inference (no internet in the sandbox VPC).
  // The user configures the injected outcome per run.
  const hasModelInference =
    getDefinition !== undefined &&
    getDefinition().nodes.some((node) => SIMULATED_INFERENCE_NODE_TYPES.includes(node.type));
  const [simAnomalous, setSimAnomalous] = useState(false);
  const [simConfidenceText, setSimConfidenceText] = useState(DEFAULT_SIMULATED_CONFIDENCE);
  const simConfidence = parseSimulatedConfidence(simConfidenceText);

  // Dataset picker (12.2): datasets of the selected Use_Case, loaded when
  // the panel first becomes active for that Use_Case.
  const [datasets, setDatasets] = useState<TestDataset[] | null>(null);
  const [datasetsError, setDatasetsError] = useState<string | null>(null);
  const [selectedDataset, setSelectedDataset] = useState<SelectProps.Option | null>(null);

  // Upload of new sample inputs (12.2, 12.3)
  const [uploadName, setUploadName] = useState('');
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadSuccess, setUploadSuccess] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Test run state
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<WorkflowTestRunDetail | null>(null);

  // Cancel in-flight polling when the component unmounts.
  const unmountedRef = useRef(false);
  useEffect(() => {
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
    };
  }, []);

  // ------------------------------------------------------------------
  // Dataset list scoped to the Use_Case (12.2)
  // ------------------------------------------------------------------

  const refreshDatasets = useCallback(async () => {
    if (!usecaseId) {
      return;
    }
    setDatasetsError(null);
    try {
      const response = await apiService.listTestDatasets(usecaseId);
      if (!unmountedRef.current) {
        setDatasets(response.datasets);
      }
    } catch (err) {
      if (!unmountedRef.current) {
        setDatasetsError(err instanceof Error ? err.message : String(err));
      }
    }
  }, [usecaseId]);

  // Load the dataset list the first time the panel is active for the
  // selected Use_Case; re-activating (tab switches, drawer toggles) never
  // reloads, so the dataset selection and run results are preserved.
  const loadedUsecaseRef = useRef<string | null>(null);
  useEffect(() => {
    if (active && usecaseId && loadedUsecaseRef.current !== usecaseId) {
      loadedUsecaseRef.current = usecaseId;
      setDatasets(null);
      setSelectedDataset(null);
      void refreshDatasets();
    }
  }, [active, usecaseId, refreshDatasets]);

  // ------------------------------------------------------------------
  // Upload new sample inputs (12.2; verification is backend-side, 12.3)
  // ------------------------------------------------------------------

  const handleUpload = useCallback(async () => {
    if (!usecaseId || uploadFiles.length === 0 || uploadName.trim() === '') {
      return;
    }
    setUploading(true);
    setUploadError(null);
    setUploadSuccess(null);
    try {
      // 1. Initiate: declare the file set, receive presigned multipart URLs.
      const initiation = await apiService.createTestDataset({
        usecase_id: usecaseId,
        name: uploadName.trim(),
        files: uploadFiles.map((file) => ({
          name: file.name,
          size: file.size,
          ...(file.type ? { content_type: file.type } : {}),
        })),
      });

      // 2. PUT every part of every file to its presigned URL.
      const completed: TestDatasetCompletedFile[] = [];
      for (const uploadFile of initiation.upload.files) {
        const source = uploadFiles.find((file) => file.name === uploadFile.name);
        if (source === undefined) {
          throw new Error(`No selected file matches '${uploadFile.name}'`);
        }
        const parts: TestDatasetCompletedFile['parts'] = [];
        for (const part of uploadFile.parts) {
          const start = (part.part_number - 1) * uploadFile.part_size;
          const chunk = source.slice(start, start + uploadFile.part_size);
          const response = await fetch(part.url, { method: 'PUT', body: chunk });
          if (!response.ok) {
            throw new Error(`Uploading '${uploadFile.name}' failed (HTTP ${response.status})`);
          }
          const etag = response.headers.get('ETag');
          if (!etag) {
            throw new Error(`Upload of '${uploadFile.name}' returned no ETag`);
          }
          parts.push({ part_number: part.part_number, etag });
        }
        completed.push({ key: uploadFile.key, upload_id: uploadFile.upload_id, parts });
      }

      // 3. Finalize: server-side verification commits the dataset (12.3).
      const finalized = await apiService.finalizeTestDataset({
        usecase_id: usecaseId,
        dataset_id: initiation.dataset_id,
        name: uploadName.trim(),
        files: completed,
      });

      if (!unmountedRef.current) {
        setUploadSuccess(`Dataset '${finalized.dataset.name}' uploaded`);
        setUploadName('');
        setUploadFiles([]);
        if (fileInputRef.current) {
          fileInputRef.current.value = '';
        }
        // The new dataset becomes selectable and selected for the run.
        setDatasets((existing) => [finalized.dataset, ...(existing ?? [])]);
        setSelectedDataset(datasetOption(finalized.dataset));
      }
    } catch (err) {
      // Rejections (unsupported format, size over 500 MB) surface the
      // backend's reason; nothing was persisted (12.11, backend-side).
      if (!unmountedRef.current) {
        setUploadError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      if (!unmountedRef.current) {
        setUploading(false);
      }
    }
  }, [usecaseId, uploadFiles, uploadName]);

  // ------------------------------------------------------------------
  // Run the test and poll until completed/failed
  // ------------------------------------------------------------------

  /**
   * Poll a test run until it completes or fails, rendering the live
   * progress along the way. The persisted run marker is cleared only when
   * the final report is shown; unmounting mid-run keeps the marker so the
   * run is resumed when the panel returns.
   */
  const pollRun = useCallback(
    async (testRunId: string) => {
      setRunning(true);
      setRunError(null);
      try {
        let detail = await apiService.getTestRun(testRunId);
        while (
          !unmountedRef.current &&
          detail.test_run.status !== 'completed' &&
          detail.test_run.status !== 'failed'
        ) {
          setRunDetail(detail);
          await delay(pollIntervalMs);
          detail = await apiService.getTestRun(testRunId);
        }
        if (!unmountedRef.current) {
          setRunDetail(detail);
          // The user sees the final report now: the run no longer needs
          // to be resumed after navigation.
          clearPersistedTestRun();
        }
      } catch (err) {
        if (!unmountedRef.current) {
          setRunError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!unmountedRef.current) {
          setRunning(false);
        }
      }
    },
    [pollIntervalMs]
  );

  const handleRunTest = useCallback(async () => {
    const datasetId = selectedDataset?.value;
    if (!workflow || !datasetId || !canTest || !usecaseId) {
      return;
    }
    if (hasModelInference && simConfidence === null) {
      return;
    }
    setRunning(true);
    setRunError(null);
    setRunDetail(null);
    let testRunId: string;
    try {
      const started = await apiService.startTestRun(workflow.workflowId, {
        dataset_id: datasetId,
        version: workflow.version,
        // The configured simulated inference outcome for stubbed model
        // inference nodes (12.6); omitted for workflows without one.
        ...(hasModelInference && simConfidence !== null
          ? {
              simulated_inference: {
                is_anomalous: simAnomalous,
                confidence: simConfidence,
              },
            }
          : {}),
      });
      testRunId = started.test_run.test_run_id;
    } catch (err) {
      if (!unmountedRef.current) {
        setRunError(err instanceof Error ? err.message : String(err));
        setRunning(false);
      }
      return;
    }
    // The run now executes server-side regardless of navigation: persist
    // its identity so the panel resumes polling after a remount.
    persistTestRun({ testRunId, workflowId: workflow.workflowId, usecaseId });
    await pollRun(testRunId);
  }, [
    workflow,
    selectedDataset,
    canTest,
    usecaseId,
    hasModelInference,
    simAnomalous,
    simConfidence,
    pollRun,
  ]);

  // Resume a persisted run for the selected Use_Case when the panel
  // becomes active: the run kept executing server-side while the user was
  // elsewhere. When it already finished, the first poll returns the
  // terminal status and the final report is shown immediately.
  const resumedUsecaseRef = useRef<string | null>(null);
  useEffect(() => {
    if (!active || !usecaseId || resumedUsecaseRef.current === usecaseId) {
      return;
    }
    resumedUsecaseRef.current = usecaseId;
    const persisted = readPersistedTestRun();
    if (persisted !== null && persisted.usecaseId === usecaseId) {
      setRunDetail(null);
      void pollRun(persisted.testRunId);
    }
  }, [active, usecaseId, pollRun]);

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------

  const testDisabledReason = !canTest
    ? 'Your role does not permit workflow testing'
    : !usecaseId
      ? 'Select a use case first'
      : workflow === null
        ? 'Save the workflow first'
        : selectedDataset === null
          ? 'Select or upload a test dataset'
          : hasModelInference && simConfidence === null
            ? 'Enter a simulated confidence between 0 and 1'
            : undefined;

  const anyStubbed =
    runDetail !== null && runDetail.node_results.some((result) => isStubbedResult(result));

  return (
    <section aria-label="Test workflow panel">
      <SpaceBetween size="m">
        {/* Compact drawer-tab title: small bold text, not a heading. */}
        <div>
          <Box fontSize="body-m" fontWeight="bold">
            Test workflow
          </Box>
          <Box fontSize="body-s" color="text-body-secondary">
            Run the saved workflow against a test dataset in a simulated cloud environment - no
            edge device involved.
          </Box>
        </div>
        {/* Dataset selection scoped to the Use_Case (12.2) */}
        <FormField
          label="Test dataset"
          description="Select an existing dataset of this use case."
          errorText={datasetsError ?? undefined}
        >
          <Select
            selectedOption={selectedDataset}
            onChange={({ detail }) => setSelectedDataset(detail.selectedOption)}
            options={(datasets ?? []).map(datasetOption)}
            statusType={datasetsError ? 'error' : datasets === null ? 'loading' : 'finished'}
            loadingText="Loading test datasets"
            placeholder="Choose a test dataset"
            empty="No test datasets for this use case - upload one below"
            ariaLabel="Test dataset"
            disabled={!usecaseId}
          />
        </FormField>

        {/* Upload of new sample inputs (12.2) */}
        <ExpandableSection headerText="Upload a new dataset" variant="footer">
          <SpaceBetween size="s">
            {uploadError !== null && (
              <Alert
                type="error"
                header="Upload rejected"
                dismissible
                onDismiss={() => setUploadError(null)}
              >
                {uploadError} No dataset was saved.
              </Alert>
            )}
            {uploadSuccess !== null && (
              <Alert type="success" dismissible onDismiss={() => setUploadSuccess(null)}>
                {uploadSuccess}
              </Alert>
            )}
            <FormField label="Dataset name">
              <Input
                value={uploadName}
                onChange={({ detail }) => setUploadName(detail.value)}
                ariaLabel="Test dataset name"
                disabled={uploading}
              />
            </FormField>
            <FormField
              label="Sample images"
              description="JPEG or PNG images, at most 500 MB in total."
            >
              <input
                ref={fileInputRef}
                type="file"
                accept={UPLOAD_ACCEPT}
                multiple
                aria-label="Test dataset files"
                disabled={uploading}
                onChange={(event) => setUploadFiles(Array.from(event.target.files ?? []))}
              />
            </FormField>
            <SpaceBetween direction="horizontal" size="xs" alignItems="center">
              <Button
                onClick={handleUpload}
                disabled={
                  uploading ||
                  !canTest ||
                  !usecaseId ||
                  uploadName.trim() === '' ||
                  uploadFiles.length === 0
                }
                disabledReason={
                  !canTest ? 'Your role does not permit workflow testing' : undefined
                }
              >
                Upload dataset
              </Button>
              {uploading && (
                <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                  <Spinner />
                  <Box color="text-body-secondary">Uploading...</Box>
                </SpaceBetween>
              )}
            </SpaceBetween>
          </SpaceBetween>
        </ExpandableSection>

        {/* Simulated inference outcome (12.6): shown only when the
            workflow contains a model_inference node. The model is not
            executed in cloud tests; the configured outcome is injected
            and drives downstream nodes. */}
        {hasModelInference && (
          <section aria-label="Simulated inference outcome">
            <SpaceBetween size="xs">
              <div>
                <Box fontSize="body-m" fontWeight="bold">
                  Simulated inference outcome
                </Box>
                <Box fontSize="body-s" color="text-body-secondary">
                  {SIMULATED_INFERENCE_HELP_TEXT}
                </Box>
              </div>
              <Checkbox
                checked={simAnomalous}
                onChange={({ detail }) => setSimAnomalous(detail.checked)}
                disabled={running}
              >
                Anomaly detected
              </Checkbox>
              <FormField
                label="Confidence"
                description="Simulated confidence score between 0 and 1."
                errorText={
                  simConfidence === null ? 'Enter a number between 0 and 1' : undefined
                }
              >
                <Input
                  type="number"
                  step={0.05}
                  value={simConfidenceText}
                  onChange={({ detail }) => setSimConfidenceText(detail.value)}
                  ariaLabel="Simulated confidence"
                  disabled={running}
                />
              </FormField>
            </SpaceBetween>
          </section>
        )}

        {/* Run action (12.1) */}
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <Button
            variant="primary"
            onClick={handleRunTest}
            disabled={running || testDisabledReason !== undefined}
            disabledReason={testDisabledReason}
          >
            Run test
          </Button>
          {running && (
            <SpaceBetween direction="horizontal" size="xs" alignItems="center">
              <Spinner />
              <Box color="text-body-secondary">
                {runDetail === null ? 'Starting test...' : 'Test in progress'}
              </Box>
            </SpaceBetween>
          )}
        </SpaceBetween>

        {/* Live progress while the run executes: the step progress
            entries (most recent last); once per-node results start
            flushing, the pipeline-execution count is the latest line. */}
        {running && runDetail !== null && (
          <div aria-label="Test run progress">
            <ProgressLog entries={liveProgressEntries(runDetail)} emphasizeLatest />
          </div>
        )}

        {runError !== null && (
          <Alert
            type="error"
            header="Test run error"
            dismissible
            onDismiss={() => setRunError(null)}
          >
            {runError}
          </Alert>
        )}

        {/* Test report (12.7 display, 12.8) */}
        {runDetail !== null && !running && (
          <SpaceBetween size="s">
            <Alert
              type={runDetail.test_run.status === 'completed' ? 'success' : 'error'}
              header={
                runDetail.test_run.status === 'completed'
                  ? 'Test run completed'
                  : runDetail.test_run.failure?.timeout
                    ? 'Test run failed: timeout'
                    : 'Test run failed'
              }
            >
              {runDetail.test_run.failure !== null && (
                <div>
                  {runDetail.test_run.failure.message}
                  {runDetail.test_run.failure.nodeId !== null &&
                    ` (node: ${runDetail.test_run.failure.nodeId})`}
                </div>
              )}
            </Alert>

            {/* The recorded step progress also appears in the report. */}
            {progressEntries(runDetail.test_run).length > 0 && (
              <ExpandableSection headerText="Run log" variant="footer" defaultExpanded>
                <ProgressLog entries={progressEntries(runDetail.test_run)} />
              </ExpandableSection>
            )}

            {/* Limitation description whenever stubbed nodes appear (12.8) */}
            {anyStubbed && (
              <Alert type="info" header="Some nodes were simulated">
                {SIMULATED_LIMITATION_TEXT}
              </Alert>
            )}

            {runDetail.node_results.length === 0 ? (
              <Box color="text-body-secondary">No per-node results were produced.</Box>
            ) : (
              <ul
                aria-label="Per-node test results"
                style={{ listStyle: 'none', margin: 0, padding: 0 }}
              >
                {runDetail.node_results.map((result) => (
                  <li key={result.nodeId}>
                    <Box padding={{ vertical: 'xxs' }}>
                      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                        <Box variant="strong">{result.nodeId}</Box>
                        <StatusIndicator type={nodeStatusType(result)}>
                          {result.status}
                        </StatusIndicator>
                        {isStubbedResult(result) && <Badge color="blue">Simulated</Badge>}
                      </SpaceBetween>
                      {/* Per-node limitation description: the notes the
                          harness recorded with the stub activity (for a
                          stubbed Custom_Node_Type: simulated because no
                          x86_64 build exists — custom-node-designer 12.3);
                          entries without notes fall back to the generic
                          simulated-not-actuated text (12.8). */}
                      {isStubbedResult(result) &&
                        (stubActivityNotes(result).length > 0 ? (
                          stubActivityNotes(result).map((note) => (
                            <Box key={note} color="text-body-secondary" fontSize="body-s">
                              {note}.
                            </Box>
                          ))
                        ) : (
                          <Box color="text-body-secondary" fontSize="body-s">
                            Simulated: this node's hardware was not actuated; its stub
                            recorded {result.stubActivity.length} activity{' '}
                            {result.stubActivity.length === 1 ? 'entry' : 'entries'}.
                          </Box>
                        ))}
                      {result.outputs.length > 0 && (
                        <Box color="text-body-secondary" fontSize="body-s">
                          {result.outputs.length} output{result.outputs.length === 1 ? '' : 's'}{' '}
                          produced
                        </Box>
                      )}
                      {result.error !== null && (
                        <Box color="text-status-error" fontSize="body-s">
                          {result.error.code !== undefined && `${result.error.code}: `}
                          {result.error.message ?? 'Node failed'}
                        </Box>
                      )}
                    </Box>
                  </li>
                ))}
              </ul>
            )}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </section>
  );
}
