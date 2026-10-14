/**
 * VLM/LLM Anomaly Tuning overview (quality-prompt-tuning, task 8.2).
 *
 * For the selected Use_Case, every workflow whose latest version has at
 * least one Tunable_Node, with each node's id, type, model and the number of
 * Tuning_Samples the Sample_Store holds for it across devices, and a per-node
 * "Open session" action that create-or-gets the node's Tuning_Session and
 * opens its workspace (Requirement 1.2).
 *
 * When the Use_Case has Sample_Export disabled the page explains that devices
 * export no samples until the Use_Case enables tuning sample export and is
 * redeployed, and links to the Use_Case settings (Requirement 1.6).
 *
 * `?workflowId=` preselects one workflow — the designer toolbar's entry point
 * (Requirement 1.3) navigates here with it; the listing is then narrowed to
 * that workflow by the API's `workflow_id` parameter and a "Show all
 * workflows" action clears it.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  ContentLayout,
  Header,
  Link,
  Select,
  SelectProps,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import { useUsecase } from '../../contexts/UsecaseContext';
import { getErrorMessage } from '../../utils/errorHandling';
import type {
  TuningOverviewNode,
  TuningOverviewResponse,
  TuningOverviewWorkflow,
} from './types';

/**
 * Requirement 1.6's explanation, shown whenever the Use_Case has
 * Sample_Export disabled.
 */
export const EXPORT_DISABLED_MESSAGE =
  'Devices export no tuning samples until this use case enables tuning '
  + 'sample export and the devices are redeployed. Existing samples, if any, '
  + 'were exported earlier and are still listed below.';

/** Where Requirement 1.6's link points: the Use_Case settings page. */
export const USECASE_SETTINGS_HREF = '/usecases';

/** Empty state when no workflow of the Use_Case has a Tunable_Node. */
export const NO_TUNABLE_WORKFLOWS_MESSAGE =
  'No workflow in this use case has an anomaly-mode Bedrock or VLM '
  + 'inspection node in its latest version.';

/** Human label for a Tunable_Node's type (Requirement 1.5's two types). */
export function nodeTypeLabel(nodeType: string): string {
  if (nodeType === 'bedrock_inference') return 'Bedrock (cloud VLM)';
  if (nodeType === 'llm_inference') return 'VLM on device';
  return nodeType;
}

export default function AnomalyTuningOverview() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();

  const preselectedWorkflowId = searchParams.get('workflowId');

  // Use case selection (same pattern as the Synthetic Data workspace).
  const [useCaseOptions, setUseCaseOptions] = useState<SelectProps.Option[]>([]);
  const [useCase, setUseCase] = useState<SelectProps.Option | null>(null);

  const [overview, setOverview] = useState<TuningOverviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Node whose session is being create-or-got, as `workflowId/nodeId`. */
  const [opening, setOpening] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiService
      .listUseCases()
      .then(({ usecases }) => {
        if (cancelled) return;
        const options = usecases.map((uc) => ({
          label: uc.name,
          value: uc.usecase_id,
        }));
        setUseCaseOptions(options);
        const saved = options.find((o) => o.value === selectedUsecaseId);
        const chosen = saved ?? options[0] ?? null;
        setUseCase(chosen);
        if (chosen?.value) setSelectedUsecaseId(chosen.value);
      })
      .catch((err) => setError(getErrorMessage(err, 'Failed to load use cases')));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const usecaseId = useCase?.value as string | undefined;

  const loadOverview = useCallback(() => {
    if (!usecaseId) return undefined;
    let cancelled = false;
    setLoading(true);
    setOverview(null);
    apiService
      .listTuningWorkflows(usecaseId, preselectedWorkflowId ?? undefined)
      .then((response) => {
        if (cancelled) return;
        setOverview(response);
        setError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(getErrorMessage(err, 'Failed to load tunable workflows'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [usecaseId, preselectedWorkflowId]);

  useEffect(() => loadOverview(), [loadOverview]);

  const workflows: TuningOverviewWorkflow[] = useMemo(
    () => overview?.workflows ?? [],
    [overview]
  );

  /**
   * Open the node's Tuning_Session: the create-or-get route returns the
   * existing session for the `(workflowId, nodeId)` pair when one exists
   * (Requirement 10.2), so the same call serves both cases.
   */
  const openSession = async (
    workflow: TuningOverviewWorkflow,
    node: TuningOverviewNode
  ) => {
    const key = `${workflow.workflowId}/${node.nodeId}`;
    setOpening(key);
    setError(null);
    try {
      const { session } = await apiService.createTuningSession({
        workflow_id: workflow.workflowId,
        node_id: node.nodeId,
      });
      navigate(`/workflow-tuning/anomaly/sessions/${session.sessionId}`);
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to open the tuning session'));
    } finally {
      setOpening(null);
    }
  };

  const clearPreselection = () => {
    const next = new URLSearchParams(searchParams);
    next.delete('workflowId');
    setSearchParams(next, { replace: true });
  };

  const nodeTable = (workflow: TuningOverviewWorkflow) => (
    <Table
      variant="embedded"
      items={workflow.nodes}
      trackBy="nodeId"
      data-testid={`tuning-nodes-${workflow.workflowId}`}
      empty={<Box variant="p">This workflow has no tunable node.</Box>}
      columnDefinitions={[
        {
          id: 'node',
          header: 'Node',
          cell: (node) => node.nodeId,
        },
        {
          id: 'type',
          header: 'Type',
          cell: (node) => nodeTypeLabel(String(node.nodeType)),
        },
        {
          id: 'model',
          header: 'Model',
          cell: (node) => node.model || '—',
        },
        {
          id: 'samples',
          header: 'Samples',
          cell: (node) => (
            <SpaceBetween direction="horizontal" size="xs">
              <span>{node.sampleCount}</span>
              {node.sessionId && <Badge color="blue">Session</Badge>}
            </SpaceBetween>
          ),
        },
        {
          id: 'open',
          header: '',
          cell: (node) => (
            <Button
              variant="normal"
              loading={opening === `${workflow.workflowId}/${node.nodeId}`}
              disabled={opening !== null}
              onClick={() => openSession(workflow, node)}
            >
              Open session
            </Button>
          ),
        },
      ]}
    />
  );

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Tune the prompts of anomaly-mode Bedrock and VLM inspection nodes against the images your devices really sent"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Select
                selectedOption={useCase}
                onChange={({ detail }) => {
                  setUseCase(detail.selectedOption);
                  if (detail.selectedOption.value) {
                    setSelectedUsecaseId(detail.selectedOption.value);
                  }
                }}
                options={useCaseOptions}
                placeholder="Select a use case"
                selectedAriaLabel="Selected"
              />
              <Button
                iconName="refresh"
                ariaLabel="Refresh"
                onClick={() => loadOverview()}
                disabled={!usecaseId}
              >
                Refresh
              </Button>
            </SpaceBetween>
          }
        >
          VLM/LLM Anomaly Tuning
        </Header>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        {/* Requirement 1.6: Sample_Export disabled on the Use_Case. */}
        {overview && !overview.sampleExportEnabled && (
          <Alert
            type="warning"
            header="Tuning sample export is disabled for this use case"
            data-testid="export-disabled-explanation"
          >
            <SpaceBetween size="xs">
              <Box variant="p">{EXPORT_DISABLED_MESSAGE}</Box>
              <Link
                href={USECASE_SETTINGS_HREF}
                onFollow={(event) => {
                  event.preventDefault();
                  navigate(USECASE_SETTINGS_HREF);
                }}
              >
                Use case settings
              </Link>
            </SpaceBetween>
          </Alert>
        )}

        {overview?.sampleStoreError && (
          <Alert type="warning" header="Sample counts unavailable">
            {`The sample store could not be listed, so the counts below read 0: ${overview.sampleStoreError}`}
          </Alert>
        )}

        {preselectedWorkflowId && (
          <Alert
            type="info"
            data-testid="workflow-preselection"
            action={
              <Button onClick={clearPreselection}>Show all workflows</Button>
            }
          >
            {`Showing one preselected workflow: ${preselectedWorkflowId}`}
          </Alert>
        )}

        {loading && <Box variant="p">Loading workflows with tunable nodes…</Box>}

        {!loading && overview && workflows.length === 0 && (
          <Box variant="p" data-testid="no-tunable-workflows">
            {preselectedWorkflowId
              ? `Workflow ${preselectedWorkflowId} has no anomaly-mode Bedrock or VLM inspection node in its latest version.`
              : NO_TUNABLE_WORKFLOWS_MESSAGE}
          </Box>
        )}

        {workflows.map((workflow) => (
          <Container
            key={workflow.workflowId}
            data-testid={`tuning-workflow-${workflow.workflowId}`}
            header={
              <Header
                variant="h2"
                description={`Version ${workflow.latestVersion} · ${workflow.nodes.length} tunable node${workflow.nodes.length === 1 ? '' : 's'}`}
              >
                {workflow.name || workflow.workflowId}
              </Header>
            }
          >
            {nodeTable(workflow)}
          </Container>
        ))}
      </SpaceBetween>
    </ContentLayout>
  );
}
