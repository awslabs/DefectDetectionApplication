import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Table,
  Header,
  SpaceBetween,
  Box,
  TextFilter,
  Button,
  Link,
  StatusIndicator,
  Select,
  SelectProps,
  ProgressBar,
  Alert,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface TrainingJob {
  training_id: string;
  usecase_id: string;
  model_name: string;
  model_version: string;
  algorithm_uri: string;
  instance_type: string;
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'stopped';
  progress?: number;
  created_by: string;
  created_at: number;
  completed_at?: number;
}

export default function Training() {
  const navigate = useNavigate();
  const [filteringText, setFilteringText] = useState('');
  const [statusFilter, setStatusFilter] = useState<SelectProps.Option | null>(null);
  const [trainingJobs, setTrainingJobs] = useState<TrainingJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [useCases, setUseCases] = useState<any[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);

  // Load use cases on mount
  useEffect(() => {
    const loadUseCases = async () => {
      try {
        const response = await apiService.listUseCases();
        const useCaseList = response.usecases || [];
        setUseCases(useCaseList);
        // Auto-select first use case if available
        if (useCaseList.length > 0) {
          setSelectedUseCase({
            label: useCaseList[0].name,
            value: useCaseList[0].usecase_id,
          });
        }
      } catch (error) {
        console.error('Failed to load use cases:', error);
      }
    };
    loadUseCases();
  }, []);

  // Fetch training jobs when use case changes
  useEffect(() => {
    if (!selectedUseCase?.value) {
      setTrainingJobs([]);
      setLoading(false);
      return;
    }

    const fetchTrainingJobs = async () => {
      try {
        setLoading(true);
        setError(null);
        const response = await apiService.listTrainingJobs(selectedUseCase.value);
        setTrainingJobs(response.jobs || []);
      } catch (err) {
        console.error('Failed to fetch training jobs:', err);
        setError(err instanceof Error ? err.message : 'Failed to load training jobs');
      } finally {
        setLoading(false);
      }
    };

    fetchTrainingJobs();
    
    // Poll for updates every 30 seconds if there are in-progress jobs
    const interval = setInterval(() => {
      if (trainingJobs.some(job => {
        const status = normalizeStatus(job.status);
        return status === 'in_progress' || status === 'pending';
      })) {
        fetchTrainingJobs();
      }
    }, 30000);

    return () => clearInterval(interval);
  }, [selectedUseCase]);

  const normalizeStatus = (status: string): string => {
    // Convert backend status (PascalCase) to frontend format (lowercase)
    const statusMap: Record<string, string> = {
      'InProgress': 'in_progress',
      'Completed': 'completed',
      'Failed': 'failed',
      'Stopped': 'stopped',
      'Pending': 'pending',
    };
    return statusMap[status] || status.toLowerCase();
  };

  const getStatusIndicator = (status: string, progress?: number) => {
    const normalizedStatus = normalizeStatus(status);
    switch (normalizedStatus) {
      case 'completed':
        return <StatusIndicator type="success">Completed</StatusIndicator>;
      case 'in_progress':
        return (
          <SpaceBetween size="xs" direction="horizontal">
            <StatusIndicator type="in-progress">In Progress</StatusIndicator>
            {progress !== undefined && <ProgressBar value={progress} variant="standalone" />}
          </SpaceBetween>
        );
      case 'failed':
        return <StatusIndicator type="error">Failed</StatusIndicator>;
      case 'stopped':
        return <StatusIndicator type="stopped">Stopped</StatusIndicator>;
      case 'pending':
        return <StatusIndicator type="pending">Pending</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{status}</StatusIndicator>;
    }
  };

  const formatTimestamp = (timestamp: number) => {
    return new Date(timestamp).toLocaleString();
  };

  const formatDuration = (start: number, end?: number) => {
    if (!end) return 'In progress';
    const duration = end - start;
    const hours = Math.floor(duration / (1000 * 60 * 60));
    const minutes = Math.floor((duration % (1000 * 60 * 60)) / (1000 * 60));
    return `${hours}h ${minutes}m`;
  };

  // Filter jobs
  const filteredJobs = trainingJobs.filter((job) => {
    const matchesText =
      !filteringText ||
      job.model_name.toLowerCase().includes(filteringText.toLowerCase()) ||
      job.training_id.toLowerCase().includes(filteringText.toLowerCase());

    const matchesStatus = !statusFilter || !statusFilter.value || normalizeStatus(job.status) === statusFilter.value;

    return matchesText && matchesStatus;
  });

  const statusOptions: SelectProps.Option[] = [
    { label: 'All Statuses', value: '' },
    { label: 'In Progress', value: 'in_progress' },
    { label: 'Completed', value: 'completed' },
    { label: 'Failed', value: 'failed' },
    { label: 'Stopped', value: 'stopped' },
    { label: 'Pending', value: 'pending' },
  ];

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}
      <Table
        loading={loading}
        header={
          <Header
            variant="h1"
            description={
              <SpaceBetween direction="horizontal" size="s" alignItems="center">
                <Box variant="span">Use Case:</Box>
                <Select
                  selectedOption={selectedUseCase}
                  onChange={({ detail }) => setSelectedUseCase(detail.selectedOption)}
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
            counter={`(${filteredJobs.length})`}
            actions={
              <Button 
                variant="primary" 
                onClick={() => navigate('/training/create')}
                disabled={!selectedUseCase}
              >
                Start Training Job
              </Button>
            }
          >
            Training Jobs
          </Header>
        }
        items={filteredJobs}
        columnDefinitions={[
          {
            id: 'model_name',
            header: 'Model Name',
            cell: (item) => (
              <Link onFollow={() => navigate(`/training/${item.training_id}`)}>{item.model_name}</Link>
            ),
            sortingField: 'model_name',
          },
          {
            id: 'version',
            header: 'Version',
            cell: (item) => item.model_version,
          },
          {
            id: 'status',
            header: 'Status',
            cell: (item) => getStatusIndicator(item.status, item.progress),
            sortingField: 'status',
          },
          {
            id: 'instance_type',
            header: 'Instance Type',
            cell: (item) => item.instance_type,
          },
          {
            id: 'duration',
            header: 'Duration',
            cell: (item) => formatDuration(item.created_at, item.completed_at),
          },
          {
            id: 'created_by',
            header: 'Created By',
            cell: (item) => item.created_by,
          },
          {
            id: 'created_at',
            header: 'Started',
            cell: (item) => formatTimestamp(item.created_at),
            sortingField: 'created_at',
          },
        ]}
        filter={
          <SpaceBetween direction="horizontal" size="xs">
            <TextFilter
              filteringText={filteringText}
              filteringPlaceholder="Search training jobs"
              filteringAriaLabel="Filter training jobs"
              onChange={({ detail }) => setFilteringText(detail.filteringText)}
            />
            <Select
              selectedOption={statusFilter}
              onChange={({ detail }) => setStatusFilter(detail.selectedOption)}
              options={statusOptions}
              placeholder="Filter by status"
              selectedAriaLabel="Selected"
            />
          </SpaceBetween>
        }
        sortingDisabled={false}
        empty={
          <Box textAlign="center" color="inherit">
            <b>No training jobs</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {selectedUseCase 
                ? 'No training jobs have been started yet for this use case.'
                : 'Select a use case to view training jobs.'}
            </Box>
            {selectedUseCase && (
              <Button onClick={() => navigate('/training/create')}>Start Training Job</Button>
            )}
          </Box>
        }
        variant="full-page"
      />
    </SpaceBetween>
  );
}
