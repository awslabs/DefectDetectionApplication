import { useState } from 'react';
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
} from '@cloudscape-design/components';

interface Model {
  model_id: string;
  usecase_id: string;
  name: string;
  version: string;
  stage: 'candidate' | 'staging' | 'production';
  training_job_id: string;
  dataset_manifest_id: string;
  metrics: Record<string, number>;
  component_arns: Record<string, string>;
  deployed_devices: string[];
  created_by: string;
  created_at: number;
  promoted_at?: number;
  promoted_by?: string;
}

// Mock data
const mockModel: Model = {
  model_id: 'model-001',
  usecase_id: 'usecase-manufacturing-001',
  name: 'Defect Detection v1',
  version: '1.0.0',
  stage: 'production',
  training_job_id: 'training-job-001',
  dataset_manifest_id: 'manifest-001',
  metrics: {
    accuracy: 0.95,
    precision: 0.93,
    recall: 0.94,
    f1_score: 0.935,
    loss: 0.12,
  },
  component_arns: {
    'x86_64': 'arn:aws:greengrass:us-east-1:123456789012:components:DefectDetection:versions:1.0.0',
    'aarch64': 'arn:aws:greengrass:us-east-1:123456789012:components:DefectDetection-ARM:versions:1.0.0',
  },
  deployed_devices: ['device-001', 'device-002', 'device-003'],
  created_by: 'user@example.com',
  created_at: Date.now() - 7 * 24 * 60 * 60 * 1000,
  promoted_at: Date.now() - 5 * 24 * 60 * 60 * 1000,
  promoted_by: 'admin@example.com',
};

export default function ModelDetail() {
  const { modelId } = useParams<{ modelId: string }>();
  const navigate = useNavigate();
  
  // In real app, use modelId to fetch the model
  console.log('Model ID:', modelId);
  const [showPromoteModal, setShowPromoteModal] = useState(false);
  const [targetStage, setTargetStage] = useState<SelectProps.Option | null>(null);

  const model = mockModel; // In real app, fetch by modelId

  const getStageBadge = (stage: string) => {
    switch (stage) {
      case 'production':
        return <Badge color="green">Production</Badge>;
      case 'staging':
        return <Badge color="blue">Staging</Badge>;
      case 'candidate':
        return <Badge color="grey">Candidate</Badge>;
      default:
        return <Badge>{stage}</Badge>;
    }
  };

  const formatTimestamp = (timestamp?: number) => {
    if (!timestamp) return 'N/A';
    return new Date(timestamp).toLocaleString();
  };

  const getPromotionOptions = (): SelectProps.Option[] => {
    const options: SelectProps.Option[] = [];
    if (model.stage === 'candidate') {
      options.push({ label: 'Staging', value: 'staging' });
      options.push({ label: 'Production', value: 'production' });
    } else if (model.stage === 'staging') {
      options.push({ label: 'Production', value: 'production' });
    }
    return options;
  };

  const canPromote = model.stage !== 'production';
  const canDelete = model.deployed_devices.length === 0;

  return (
    <SpaceBetween size="l">
      {/* Header */}
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => navigate('/models')}>Back to Models</Button>
            <Button disabled={!canPromote} onClick={() => setShowPromoteModal(true)}>
              Promote Stage
            </Button>
            <Button disabled={!canDelete}>Delete Model</Button>
          </SpaceBetween>
        }
      >
        {model.name}
      </Header>

      {/* Status Cards */}
      <ColumnLayout columns={4} variant="text-grid">
        <Container>
          <Box variant="awsui-key-label">Stage</Box>
          <Box variant="h2">{getStageBadge(model.stage)}</Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Version</Box>
          <Box variant="h3">{model.version}</Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Accuracy</Box>
          <Box variant="h3">{(model.metrics.accuracy * 100).toFixed(1)}%</Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Deployed Devices</Box>
          <Box variant="h3">{model.deployed_devices.length}</Box>
        </Container>
      </ColumnLayout>

      {/* Model Information */}
      <Container header={<Header variant="h2">Model Information</Header>}>
        <ColumnLayout columns={2} variant="text-grid">
          <KeyValuePairs
            columns={1}
            items={[
              { label: 'Model ID', value: model.model_id },
              { label: 'Name', value: model.name },
              { label: 'Version', value: model.version },
              { label: 'Stage', value: model.stage },
            ]}
          />
          <KeyValuePairs
            columns={1}
            items={[
              { label: 'Use Case', value: model.usecase_id },
              { label: 'Training Job', value: model.training_job_id },
              { label: 'Dataset Manifest', value: model.dataset_manifest_id },
              { label: 'Created By', value: model.created_by },
            ]}
          />
        </ColumnLayout>
      </Container>

      {/* Metrics */}
      <Container header={<Header variant="h2">Model Metrics</Header>}>
        <ColumnLayout columns={5} variant="text-grid">
          {Object.entries(model.metrics).map(([key, value]) => (
            <div key={key}>
              <Box variant="awsui-key-label">{key.replace('_', ' ').toUpperCase()}</Box>
              <Box variant="h3">
                {key === 'loss' ? value.toFixed(4) : `${(value * 100).toFixed(2)}%`}
              </Box>
            </div>
          ))}
        </ColumnLayout>
      </Container>

      {/* Component ARNs */}
      <Container header={<Header variant="h2">Greengrass Components</Header>}>
        <Table
          items={Object.entries(model.component_arns).map(([platform, arn]) => ({
            platform,
            arn,
          }))}
          columnDefinitions={[
            {
              id: 'platform',
              header: 'Platform',
              cell: (item) => <Badge>{item.platform}</Badge>,
            },
            {
              id: 'arn',
              header: 'Component ARN',
              cell: (item) => (
                <Box fontSize="body-s">
                  <span style={{ fontFamily: 'monospace' }}>{item.arn}</span>
                </Box>
              ),
            },
          ]}
          empty={
            <Box textAlign="center" color="inherit">
              No components available
            </Box>
          }
        />
      </Container>

      {/* Deployed Devices */}
      <Container
        header={
          <Header variant="h2" counter={`(${model.deployed_devices.length})`}>
            Deployed Devices
          </Header>
        }
      >
        {model.deployed_devices.length > 0 ? (
          <Table
            items={model.deployed_devices.map((deviceId) => ({ device_id: deviceId }))}
            columnDefinitions={[
              {
                id: 'device_id',
                header: 'Device ID',
                cell: (item) => (
                  <Link onFollow={() => navigate(`/devices/${item.device_id}`)}>
                    {item.device_id}
                  </Link>
                ),
              },
            ]}
          />
        ) : (
          <Box textAlign="center" color="inherit" padding="l">
            This model is not deployed to any devices yet.
          </Box>
        )}
      </Container>

      {/* Promotion History */}
      <Container header={<Header variant="h2">Promotion History</Header>}>
        <KeyValuePairs
          columns={3}
          items={[
            { label: 'Created', value: formatTimestamp(model.created_at) },
            { label: 'Promoted At', value: formatTimestamp(model.promoted_at) },
            { label: 'Promoted By', value: model.promoted_by || 'N/A' },
          ]}
        />
      </Container>

      {/* Promote Modal */}
      <Modal
        visible={showPromoteModal}
        onDismiss={() => {
          setShowPromoteModal(false);
          setTargetStage(null);
        }}
        header="Promote Model Stage"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowPromoteModal(false);
                  setTargetStage(null);
                }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                disabled={!targetStage}
                onClick={() => {
                  // TODO: Implement promotion
                  setShowPromoteModal(false);
                  setTargetStage(null);
                }}
              >
                Promote
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Alert type="info">
            Promoting a model to a higher stage makes it available for deployment to production
            devices. Ensure the model has been thoroughly tested before promotion.
          </Alert>

          <Box>
            <Box variant="strong">Current Stage:</Box> {getStageBadge(model.stage)}
          </Box>

          <Box>
            <Box variant="strong" padding={{ bottom: 's' }}>
              Promote to:
            </Box>
            <Select
              selectedOption={targetStage}
              onChange={({ detail }) => setTargetStage(detail.selectedOption)}
              options={getPromotionOptions()}
              placeholder="Select target stage"
              selectedAriaLabel="Selected"
            />
          </Box>

          {model.deployed_devices.length > 0 && (
            <Alert type="warning">
              This model is currently deployed to {model.deployed_devices.length} device(s).
              Promotion will not affect existing deployments.
            </Alert>
          )}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
