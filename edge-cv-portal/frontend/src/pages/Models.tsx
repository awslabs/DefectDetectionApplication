import { useState, useEffect, useCallback } from 'react';
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
  Button,
  Spinner,
  Alert,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { useUsecase } from '../contexts/UsecaseContext';

interface Model {
  model_id: string;
  usecase_id: string;
  name: string;
  version: string;
  stage: 'candidate' | 'staging' | 'production';
  source: string;
  training_job_id: string;
  model_type: string;
  metrics: Record<string, number>;
  artifact_s3?: string;
  component_arns: Record<string, string>;
  deployed_devices: string[];
  created_by: string;
  created_at: number;
  description?: string;
  compilation_status?: string;
}

export default function Models() {
  const navigate = useNavigate();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  const [filteringText, setFilteringText] = useState('');
  const [stageFilter, setStageFilter] = useState<SelectProps.Option | null>(null);
  const [sourceFilter, setSourceFilter] = useState<SelectProps.Option | null>(null);
  const [useCases, setUseCases] = useState<any[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);
  const [models, setModels] = useState<Model[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load use cases on mount
  useEffect(() => {
    const loadUseCases = async () => {
      try {
        const response = await apiService.listUseCases();
        const useCaseList = response.usecases || [];
        setUseCases(useCaseList);
        
        // Use saved selection from context, or auto-select first
        if (selectedUsecaseId) {
          const saved = useCaseList.find(uc => uc.usecase_id === selectedUsecaseId);
          if (saved) {
            setSelectedUseCase({
              label: saved.name,
              value: saved.usecase_id,
            });
            return;
          }
        }
        
        // Auto-select first use case if available
        if (useCaseList.length > 0) {
          setSelectedUseCase({
            label: useCaseList[0].name,
            value: useCaseList[0].usecase_id,
          });
          setSelectedUsecaseId(useCaseList[0].usecase_id);
        }
      } catch (err) {
        console.error('Failed to load use cases:', err);
        setError('Failed to load use cases');
      }
    };
    loadUseCases();
  }, [selectedUsecaseId, setSelectedUsecaseId]);

  // Load models when use case changes
  const loadModels = useCallback(async () => {
    if (!selectedUseCase?.value) return;
    
    setLoading(true);
    setError(null);
    
    try {
      const response = await apiService.listModels({
        usecase_id: selectedUseCase.value,
        ...(stageFilter?.value && { stage: stageFilter.value as 'candidate' | 'staging' | 'production' }),
        ...(sourceFilter?.value && { source: sourceFilter.value as 'trained' | 'imported' | 'marketplace' }),
      });
      setModels(response.models || []);
    } catch (err: any) {
      console.error('Failed to load models:', err);
      setError(err.message || 'Failed to load models');
      setModels([]);
    } finally {
      setLoading(false);
    }
  }, [selectedUseCase, stageFilter, sourceFilter]);

  useEffect(() => {
    loadModels();
  }, [loadModels]);

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

  const getSourceBadge = (source: string) => {
    switch (source) {
      case 'trained':
        return <Badge color="green">Trained</Badge>;
      case 'imported':
        return <Badge color="blue">Imported</Badge>;
      case 'marketplace':
        return <Badge color="grey">Marketplace</Badge>;
      default:
        return <Badge>{source}</Badge>;
    }
  };

  const formatTimestamp = (timestamp: number) => {
    if (!timestamp) return 'N/A';
    const date = new Date(timestamp);
    return date.toLocaleDateString();
  };

  // Filter models by text
  const filteredModels = models.filter((model) => {
    const matchesText =
      !filteringText ||
      model.name.toLowerCase().includes(filteringText.toLowerCase()) ||
      model.version.toLowerCase().includes(filteringText.toLowerCase()) ||
      model.model_id.toLowerCase().includes(filteringText.toLowerCase());

    return matchesText;
  });

  const stageOptions: SelectProps.Option[] = [
    { label: 'All Stages', value: '' },
    { label: 'Production', value: 'production' },
    { label: 'Staging', value: 'staging' },
    { label: 'Candidate', value: 'candidate' },
  ];

  const sourceOptions: SelectProps.Option[] = [
    { label: 'All Sources', value: '' },
    { label: 'Trained', value: 'trained' },
    { label: 'Imported', value: 'imported' },
  ];

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}
      
      <Table
        header={
          <Header
            variant="h1"
            description={
              <SpaceBetween direction="horizontal" size="s" alignItems="center">
                <Box variant="span">Use Case:</Box>
                <Select
                  selectedOption={selectedUseCase}
                  onChange={({ detail }) => {
                    setSelectedUseCase(detail.selectedOption);
                    setSelectedUsecaseId(detail.selectedOption?.value || null);
                  }}
                  options={useCases.map((uc) => ({
                    label: uc.name,
                    value: uc.usecase_id,
                  }))}
                  placeholder="Select a use case"
                  disabled={useCases.length === 0}
                  expandToViewport
                />
              </SpaceBetween>
            }
            counter={`(${filteredModels.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={loadModels} disabled={loading}>
                  Refresh
                </Button>
                <Button 
                  onClick={() => navigate('/models/smart-import')}
                  disabled={!selectedUseCase}
                >
                  Smart Import
                </Button>
                <Button 
                  onClick={() => navigate('/models/import')}
                  disabled={!selectedUseCase}
                >
                  Manual Import
                </Button>
              </SpaceBetween>
            }
          >
            Model Registry
          </Header>
        }
        loading={loading}
        loadingText="Loading models..."
        items={filteredModels}
        columnDefinitions={[
          {
            id: 'name',
            header: 'Model Name',
            cell: (item) => (
              <Link onFollow={() => navigate(`/models/${item.model_id}`)}>{item.name}</Link>
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
            id: 'source',
            header: 'Source',
            cell: (item) => getSourceBadge(item.source),
            sortingField: 'source',
          },
          {
            id: 'model_type',
            header: 'Type',
            cell: (item) => item.model_type || 'N/A',
          },
          {
            id: 'compilation',
            header: 'Compilation',
            cell: (item) => {
              const status = item.compilation_status;
              if (!status) return <Badge color="grey">Not Started</Badge>;
              if (status === 'Completed') return <Badge color="green">Compiled</Badge>;
              if (status === 'InProgress') return <Badge color="blue">In Progress</Badge>;
              if (status === 'Failed') return <Badge color="red">Failed</Badge>;
              return <Badge>{status}</Badge>;
            },
          },
          {
            id: 'deployed_devices',
            header: 'Deployed',
            cell: (item) => item.deployed_devices?.length || 0,
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
            <Select
              selectedOption={sourceFilter}
              onChange={({ detail }) => setSourceFilter(detail.selectedOption)}
              options={sourceOptions}
              placeholder="Filter by source"
              selectedAriaLabel="Selected"
            />
          </SpaceBetween>
        }
        sortingDisabled={false}
        empty={
          <Box textAlign="center" color="inherit">
            {loading ? (
              <Spinner />
            ) : (
              <>
                <b>No models</b>
                <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                  {selectedUseCase 
                    ? 'No models have been trained or imported yet. Start by training a model or importing one.'
                    : 'Select a use case to view models.'}
                </Box>
              </>
            )}
          </Box>
        }
        variant="full-page"
      />
    </SpaceBetween>
  );
}
