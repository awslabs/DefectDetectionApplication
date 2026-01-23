import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  ColumnLayout,
  Badge,
  Button,
  Modal,
  Alert,
  KeyValuePairs,
  Table,
  Link,
  Select,
  SelectProps,
  Spinner,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface Model {
  model_id: string;
  usecase_id: string;
  name: string;
  version: string;
  stage: 'candidate' | 'staging' | 'production';
  source: string;
  training_job_id: string;
  training_job_name?: string;
  model_type: string;
  description?: string;
  metrics: Record<string, number>;
  artifact_s3?: string;
  component_arns: Record<string, string>;
  deployed_devices: string[];
  created_by: string;
  created_at: number;
  updated_at: number;
  completed_at?: number;
  promoted_at?: number;
  promoted_by?: string;
  compilation_status?: string;
  compilation_jobs?: Array<{
    target: string;
    status: string;
    compiled_model_s3?: string;
  }>;
  packaging_status?: string;
  packaged_components?: Array<{
    target: string;
    status: string;
    component_package_s3?: string;
  }>;
  hyperparameters?: Record<string, unknown>;
  instance_type?: string;
  dataset_manifest_s3?: string;
}

export default function ModelDetail() {
  const { modelId } = useParams<{ modelId: string }>();
  const navigate = useNavigate();
  
  const [model, setModel] = useState<Model | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showPromoteModal, setShowPromoteModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [targetStage, setTargetStage] = useState<SelectProps.Option | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  useEffect(() => {
    const loadModel = async () => {
      if (!modelId) return;
      setLoading(true);
      setError(null);
      try {
        const response = await apiService.getModel(modelId);
        setModel(response.model);
      } catch (err: any) {
        console.error('Failed to load model:', err);
        setError(err.message || 'Failed to load model');
      } finally {
        setLoading(false);
      }
    };
    loadModel();
  }, [modelId]);

  const handlePromote = async () => {
    if (!model || !targetStage?.value) return;
    setActionLoading(true);
    try {
      await apiService.updateModelStage(
        model.model_id, 
        targetStage.value as 'candidate' | 'staging' | 'production'
      );
      const response = await apiService.getModel(model.model_id);
      setModel(response.model);
      setShowPromoteModal(false);
      setTargetStage(null);
    } catch (err: any) {
      setError(err.message || 'Failed to update model stage');
    } finally {
      setActionLoading(false);
    }
  };

  const handleDelete = async () => {
    if (!model) return;
    setActionLoading(true);
    try {
      await apiService.deleteModel(model.model_id);
      navigate('/models');
    } catch (err: any) {
      setError(err.message || 'Failed to delete model');
      setShowDeleteModal(false);
    } finally {
      setActionLoading(false);
    }
  };

  const getStageBadge = (stage: string) => {
    switch (stage) {
      case 'production': return <Badge color="green">Production</Badge>;
      case 'staging': return <Badge color="blue">Staging</Badge>;
      case 'candidate': return <Badge color="grey">Candidate</Badge>;
      default: return <Badge>{stage}</Badge>;
    }
  };

  const getSourceBadge = (source: string) => {
    switch (source) {
      case 'trained': return <Badge color="green">Trained</Badge>;
      case 'imported': return <Badge color="blue">Imported</Badge>;
      case 'marketplace': return <Badge color="grey">Marketplace</Badge>;
      default: return <Badge>{source}</Badge>;
    }
  };

  const formatTimestamp = (timestamp?: number) => {
    if (!timestamp) return 'N/A';
    return new Date(timestamp).toLocaleString();
  };

  const getPromotionOptions = (): SelectProps.Option[] => {
    if (!model) return [];
    const options: SelectProps.Option[] = [];
    if (model.stage === 'candidate') {
      options.push({ label: 'Staging', value: 'staging' });
      options.push({ label: 'Production', value: 'production' });
    } else if (model.stage === 'staging') {
      options.push({ label: 'Production', value: 'production' });
      options.push({ label: 'Candidate (Demote)', value: 'candidate' });
    } else if (model.stage === 'production') {
      options.push({ label: 'Staging (Demote)', value: 'staging' });
      options.push({ label: 'Candidate (Demote)', value: 'candidate' });
    }
    return options;
  };

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box variant="p" padding={{ top: 's' }}>Loading model...</Box>
      </Box>
    );
  }

  if (error || !model) {
    return (
      <Alert type="error" header="Error loading model">
        {error || 'Model not found'}
        <Box padding={{ top: 's' }}>
          <Button onClick={() => navigate('/models')}>Back to Models</Button>
        </Box>
      </Alert>
    );
  }

  const canDelete = model.deployed_devices.length === 0;

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => navigate('/models')}>Back to Models</Button>
            <Button onClick={() => setShowPromoteModal(true)}>Change Stage</Button>
            <Button disabled={!canDelete} onClick={() => setShowDeleteModal(true)}>Delete Model</Button>
          </SpaceBetween>
        }
      >
        {model.name}
      </Header>

      <ColumnLayout columns={4} variant="text-grid">
        <Container>
          <Box variant="awsui-key-label">Stage</Box>
          <Box variant="h2">{getStageBadge(model.stage)}</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Source</Box>
          <Box variant="h2">{getSourceBadge(model.source)}</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Version</Box>
          <Box variant="h3">{model.version}</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Deployed Devices</Box>
          <Box variant="h3">{model.deployed_devices.length}</Box>
        </Container>
      </ColumnLayout>

      <Container header={<Header variant="h2">Model Information</Header>}>
        <ColumnLayout columns={2} variant="text-grid">
          <KeyValuePairs columns={1} items={[
            { label: 'Model ID', value: model.model_id },
            { label: 'Name', value: model.name },
            { label: 'Version', value: model.version },
            { label: 'Type', value: model.model_type || 'N/A' },
            { label: 'Stage', value: model.stage },
          ]} />
          <KeyValuePairs columns={1} items={[
            { label: 'Use Case', value: model.usecase_id },
            { label: 'Training Job', value: model.training_job_name || model.training_job_id },
            { label: 'Created By', value: model.created_by },
            { label: 'Instance Type', value: model.instance_type || 'N/A' },
            { label: 'Description', value: model.description || 'N/A' },
          ]} />
        </ColumnLayout>
      </Container>

      {model.metrics && Object.keys(model.metrics).length > 0 && (
        <Container header={<Header variant="h2">Model Metrics</Header>}>
          <ColumnLayout columns={5} variant="text-grid">
            {Object.entries(model.metrics).map(([key, value]) => (
              <div key={key}>
                <Box variant="awsui-key-label">{key.replace(/_/g, ' ').toUpperCase()}</Box>
                <Box variant="h3">
                  {typeof value === 'number' 
                    ? (key === 'loss' ? value.toFixed(4) : `${(value * 100).toFixed(2)}%`)
                    : String(value)}
                </Box>
              </div>
            ))}
          </ColumnLayout>
        </Container>
      )}

      {model.compilation_jobs && model.compilation_jobs.length > 0 && (
        <Container header={<Header variant="h2">Compilation Jobs</Header>}>
          <Table
            items={model.compilation_jobs}
            columnDefinitions={[
              { id: 'target', header: 'Target', cell: (item) => <Badge>{item.target}</Badge> },
              { id: 'status', header: 'Status', cell: (item) => {
                if (item.status === 'COMPLETED') return <Badge color="green">Completed</Badge>;
                if (item.status === 'INPROGRESS') return <Badge color="blue">In Progress</Badge>;
                if (item.status === 'FAILED') return <Badge color="red">Failed</Badge>;
                return <Badge>{item.status}</Badge>;
              }},
              { id: 'output', header: 'Output', cell: (item) => item.compiled_model_s3 ? 
                <Box fontSize="body-s" color="text-status-info">Available</Box> : 
                <Box fontSize="body-s" color="text-status-inactive">N/A</Box> },
            ]}
            empty={<Box textAlign="center">No compilation jobs</Box>}
          />
        </Container>
      )}

      {model.component_arns && Object.keys(model.component_arns).length > 0 && (
        <Container header={<Header variant="h2">Greengrass Components</Header>}>
          <Table
            items={Object.entries(model.component_arns).map(([platform, arn]) => ({ platform, arn }))}
            columnDefinitions={[
              { id: 'platform', header: 'Platform', cell: (item) => <Badge>{item.platform}</Badge> },
              { id: 'arn', header: 'Component ARN', cell: (item) => (
                <Box fontSize="body-s"><span style={{ fontFamily: 'monospace' }}>{item.arn}</span></Box>
              )},
            ]}
            empty={<Box textAlign="center">No components available</Box>}
          />
        </Container>
      )}

      <Container header={<Header variant="h2" counter={`(${model.deployed_devices.length})`}>Deployed Devices</Header>}>
        {model.deployed_devices.length > 0 ? (
          <Table
            items={model.deployed_devices.map((deviceId) => ({ device_id: deviceId }))}
            columnDefinitions={[
              { id: 'device_id', header: 'Device ID', cell: (item) => (
                <Link onFollow={() => navigate(`/devices/${item.device_id}`)}>{item.device_id}</Link>
              )},
            ]}
          />
        ) : (
          <Box textAlign="center" color="inherit" padding="l">This model is not deployed to any devices yet.</Box>
        )}
      </Container>

      <Container header={<Header variant="h2">History</Header>}>
        <KeyValuePairs columns={4} items={[
          { label: 'Created', value: formatTimestamp(model.created_at) },
          { label: 'Completed', value: formatTimestamp(model.completed_at) },
          { label: 'Promoted At', value: formatTimestamp(model.promoted_at) },
          { label: 'Promoted By', value: model.promoted_by || 'N/A' },
        ]} />
      </Container>

      {model.artifact_s3 && (
        <Container header={<Header variant="h2">Artifact Location</Header>}>
          <Box fontSize="body-s"><span style={{ fontFamily: 'monospace' }}>{model.artifact_s3}</span></Box>
        </Container>
      )}

      <Modal visible={showPromoteModal} onDismiss={() => { setShowPromoteModal(false); setTargetStage(null); }}
        header="Change Model Stage"
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => { setShowPromoteModal(false); setTargetStage(null); }}>Cancel</Button>
          <Button variant="primary" disabled={!targetStage || actionLoading} loading={actionLoading} onClick={handlePromote}>Update Stage</Button>
        </SpaceBetween></Box>}>
        <SpaceBetween size="m">
          <Alert type="info">Promoting a model to a higher stage makes it available for deployment. Demoting moves it back.</Alert>
          <Box><Box variant="strong">Current Stage:</Box> {getStageBadge(model.stage)}</Box>
          <Box>
            <Box variant="strong" padding={{ bottom: 's' }}>Change to:</Box>
            <Select selectedOption={targetStage} onChange={({ detail }) => setTargetStage(detail.selectedOption)}
              options={getPromotionOptions()} placeholder="Select target stage" />
          </Box>
          {model.deployed_devices.length > 0 && (
            <Alert type="warning">This model is deployed to {model.deployed_devices.length} device(s). Stage changes won't affect existing deployments.</Alert>
          )}
        </SpaceBetween>
      </Modal>

      <Modal visible={showDeleteModal} onDismiss={() => setShowDeleteModal(false)} header="Delete Model"
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setShowDeleteModal(false)}>Cancel</Button>
          <Button variant="primary" disabled={actionLoading || !canDelete} loading={actionLoading} onClick={handleDelete}>Delete</Button>
        </SpaceBetween></Box>}>
        <SpaceBetween size="m">
          {!canDelete ? (
            <Alert type="error">Cannot delete - model is deployed to {model.deployed_devices.length} device(s). Undeploy first.</Alert>
          ) : (
            <Alert type="warning">Delete model "{model.name}" (v{model.version})? This cannot be undone.</Alert>
          )}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
