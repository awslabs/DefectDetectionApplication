import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Table,
  Header,
  SpaceBetween,
  Box,
  TextFilter,
  Link,
  Badge,
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
  metrics: Record<string, number>;
  deployed_devices: string[];
  created_by: string;
  created_at: number;
  marketplace_arn?: string;
  requires_subscription?: boolean;
}

// Mock data
const mockModels: Model[] = [
  {
    model_id: 'marketplace-cv-defect-detection',
    usecase_id: 'marketplace',
    name: 'AWS Marketplace CV Defect Detection',
    version: 'Latest',
    stage: 'production',
    training_job_id: 'marketplace-algorithm',
    metrics: { accuracy: 0.0, precision: 0.0, recall: 0.0, f1_score: 0.0 },
    deployed_devices: [],
    created_by: 'AWS Marketplace',
    created_at: Date.now() - 365 * 24 * 60 * 60 * 1000,
    marketplace_arn: 'arn:aws:sagemaker:us-east-1:865070037744:algorithm/computer-vision-defect-detection',
    requires_subscription: true,
  },
  {
    model_id: 'model-001',
    usecase_id: 'usecase-manufacturing-001',
    name: 'Defect Detection v1',
    version: '1.0.0',
    stage: 'production',
    training_job_id: 'training-job-001',
    metrics: { accuracy: 0.95, precision: 0.93, recall: 0.94, f1_score: 0.935 },
    deployed_devices: ['device-001', 'device-002', 'device-003'],
    created_by: 'user@example.com',
    created_at: Date.now() - 7 * 24 * 60 * 60 * 1000,
  },
  {
    model_id: 'model-002',
    usecase_id: 'usecase-manufacturing-001',
    name: 'Defect Detection v2',
    version: '2.0.0',
    stage: 'staging',
    training_job_id: 'training-job-002',
    metrics: { accuracy: 0.97, precision: 0.96, recall: 0.95, f1_score: 0.955 },
    deployed_devices: ['device-004'],
    created_by: 'user@example.com',
    created_at: Date.now() - 2 * 24 * 60 * 60 * 1000,
  },
  {
    model_id: 'model-003',
    usecase_id: 'usecase-manufacturing-002',
    name: 'Scratch Detection',
    version: '1.0.0',
    stage: 'candidate',
    training_job_id: 'training-job-003',
    metrics: { accuracy: 0.92, precision: 0.91, recall: 0.90, f1_score: 0.905 },
    deployed_devices: [],
    created_by: 'scientist@example.com',
    created_at: Date.now() - 1 * 24 * 60 * 60 * 1000,
  },
];

export default function Models() {
  const navigate = useNavigate();
  const [filteringText, setFilteringText] = useState('');
  const [stageFilter, setStageFilter] = useState<SelectProps.Option | null>(null);

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

  const formatTimestamp = (timestamp: number) => {
    const date = new Date(timestamp);
    return date.toLocaleDateString();
  };

  // Filter models
  const filteredModels = mockModels.filter((model) => {
    const matchesText =
      !filteringText ||
      model.name.toLowerCase().includes(filteringText.toLowerCase()) ||
      model.version.toLowerCase().includes(filteringText.toLowerCase()) ||
      model.model_id.toLowerCase().includes(filteringText.toLowerCase());

    const matchesStage = !stageFilter || model.stage === stageFilter.value;

    return matchesText && matchesStage;
  });

  const stageOptions: SelectProps.Option[] = [
    { label: 'All Stages', value: '' },
    { label: 'Production', value: 'production' },
    { label: 'Staging', value: 'staging' },
    { label: 'Candidate', value: 'candidate' },
  ];

  return (
    <SpaceBetween size="l">
      <Table
        header={
          <Header
            variant="h1"
            description="Manage trained models and their deployment stages"
            counter={`(${filteredModels.length})`}
          >
            Model Registry
          </Header>
        }
        items={filteredModels}
        columnDefinitions={[
          {
            id: 'name',
            header: 'Model Name',
            cell: (item) => (
              <SpaceBetween direction="horizontal" size="xs">
                <Link onFollow={() => navigate(`/models/${item.model_id}`)}>{item.name}</Link>
                {item.requires_subscription && (
                  <Badge color="blue">Requires Subscription</Badge>
                )}
              </SpaceBetween>
            ),
            sortingField: 'name',
          },
          {
            id: 'version',
            header: 'Version',
            cell: (item) => item.version,
            sortingField: 'version',
          },
          {
            id: 'stage',
            header: 'Stage',
            cell: (item) => getStageBadge(item.stage),
            sortingField: 'stage',
          },
          {
            id: 'accuracy',
            header: 'Accuracy',
            cell: (item) =>
              item.metrics.accuracy > 0 ? `${(item.metrics.accuracy * 100).toFixed(1)}%` : 'N/A',
          },
          {
            id: 'deployed_devices',
            header: 'Deployed Devices',
            cell: (item) => item.deployed_devices.length,
          },
          {
            id: 'created_by',
            header: 'Created By',
            cell: (item) => item.created_by,
          },
          {
            id: 'created_at',
            header: 'Created',
            cell: (item) => formatTimestamp(item.created_at),
            sortingField: 'created_at',
          },
        ]}
        filter={
          <SpaceBetween direction="horizontal" size="xs">
            <TextFilter
              filteringText={filteringText}
              filteringPlaceholder="Search models"
              filteringAriaLabel="Filter models"
              onChange={({ detail }) => setFilteringText(detail.filteringText)}
            />
            <Select
              selectedOption={stageFilter}
              onChange={({ detail }) => setStageFilter(detail.selectedOption)}
              options={stageOptions}
              placeholder="Filter by stage"
              selectedAriaLabel="Selected"
            />
          </SpaceBetween>
        }
        sortingDisabled={false}
        empty={
          <Box textAlign="center" color="inherit">
            <b>No models</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              No models have been trained yet.
            </Box>
          </Box>
        }
        variant="full-page"
      />
    </SpaceBetween>
  );
}
