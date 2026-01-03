import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  Table,
  Button,
  SpaceBetween,
  Box,
  StatusIndicator,
  ProgressBar,
  Link,
  Select,
  SelectProps,
} from '@cloudscape-design/components';
import { useNavigate } from 'react-router-dom';
import { LabelingJob, UseCase } from '../types';
import { apiService } from '../services/api';

export default function Labeling() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<LabelingJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedItems, setSelectedItems] = useState<LabelingJob[]>([]);
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);

  useEffect(() => {
    loadUseCases();
  }, []);

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

  useEffect(() => {
    if (selectedUseCase) {
      loadLabelingJobs();
    } else {
      setJobs([]);
      setLoading(false);
    }
  }, [selectedUseCase]);

  const loadLabelingJobs = async () => {
    if (!selectedUseCase?.value) return;
    
    const useCaseId = selectedUseCase.value;
    
    try {
      setLoading(true);
      const response = await apiService.listLabelingJobs({
        usecase_id: useCaseId,
      });
      
      // Transform API response to match LabelingJob type
      const transformedJobs: LabelingJob[] = response.jobs.map(job => ({
        job_id: job.job_id,
        usecase_id: useCaseId,
        name: job.job_name,
        manifest_s3: '', // Not provided by list endpoint
        output_s3: '', // Not provided by list endpoint
        task_type: job.task_type as LabelingJob['task_type'],
        images_count: job.image_count,
        labeled_count: job.labeled_objects || 0,
        status: job.status as LabelingJob['status'],
        progress_percent: job.progress_percent || 0,
        ground_truth_job_arn: '', // Not provided by list endpoint
        workforce_type: 'private', // Default value
        created_by: '', // Not provided by list endpoint
        created_at: job.created_at,
      }));
      
      setJobs(transformedJobs);
    } catch (error) {
      console.error('Failed to load labeling jobs:', error);
      setJobs([]);
      // Show error message to user if it's not just an empty result
      if (error instanceof Error && error.message !== 'Failed to load labeling jobs') {
        console.error('Detailed error:', error.message);
      }
    } finally {
      setLoading(false);
    }
  };

  const getStatusIndicator = (status: LabelingJob['status']) => {
    // Normalize status to handle different formats from API
    const normalizedStatus = status.toLowerCase().replace(/([a-z])([A-Z])/g, '$1_$2').toLowerCase();
    
    const statusMap: Record<string, { type: 'pending' | 'in-progress' | 'success' | 'error' | 'info', label: string }> = {
      pending: { type: 'pending', label: 'Pending' },
      in_progress: { type: 'in-progress', label: 'In Progress' },
      inprogress: { type: 'in-progress', label: 'In Progress' },
      completed: { type: 'success', label: 'Completed' },
      failed: { type: 'error', label: 'Failed' },
      stopped: { type: 'info', label: 'Stopped' },
    };
    const config = statusMap[normalizedStatus] || { type: 'info' as const, label: status };
    return <StatusIndicator type={config.type}>{config.label}</StatusIndicator>;
  };

  return (
    <Container
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
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button 
                onClick={() => navigate(`/labeling/datasets?usecase_id=${selectedUseCase?.value || ''}`)}
                disabled={!selectedUseCase}
              >
                Browse Datasets
              </Button>
              <Button 
                onClick={() => navigate(`/labeling/pre-labeled?usecase_id=${selectedUseCase?.value || ''}`)}
                disabled={!selectedUseCase}
              >
                Use Pre-Labeled Dataset
              </Button>
              <Button 
                variant="primary" 
                onClick={() => navigate(`/labeling/create?usecase_id=${selectedUseCase?.value || ''}`)}
                disabled={!selectedUseCase}
              >
                Create Labeling Job
              </Button>
            </SpaceBetween>
          }
        >
          Labeling Jobs
        </Header>
      }
    >
      <Table
        columnDefinitions={[
          {
            id: 'name',
            header: 'Job Name',
            cell: (item) => (
              <Link onFollow={() => navigate(`/labeling/${item.job_id}`)}>
                {item.name}
              </Link>
            ),
            sortingField: 'name',
          },
          {
            id: 'task_type',
            header: 'Task Type',
            cell: (item) => item.task_type,
          },
          {
            id: 'progress',
            header: 'Progress',
            cell: (item) => (
              <SpaceBetween direction="vertical" size="xxs">
                <ProgressBar
                  value={item.progress_percent}
                  label={`${item.labeled_count} / ${item.images_count} images`}
                />
                <Box fontSize="body-s" color="text-body-secondary">
                  {item.progress_percent}% complete
                </Box>
              </SpaceBetween>
            ),
          },
          {
            id: 'status',
            header: 'Status',
            cell: (item) => getStatusIndicator(item.status),
          },
          {
            id: 'created_at',
            header: 'Created',
            cell: (item) => new Date(item.created_at).toLocaleString(),
            sortingField: 'created_at',
          },
          {
            id: 'created_by',
            header: 'Created By',
            cell: (item) => item.created_by,
          },
        ]}
        items={jobs}
        loading={loading}
        loadingText="Loading labeling jobs"
        selectionType="single"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) =>
          setSelectedItems(detail.selectedItems)
        }
        empty={
          <Box textAlign="center" color="inherit">
            <b>No labeling jobs</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {selectedUseCase 
                ? 'No labeling jobs found for this use case.'
                : 'Select a use case to view labeling jobs.'}
            </Box>
            {selectedUseCase && (
              <Button onClick={() => navigate(`/labeling/create?usecase_id=${selectedUseCase.value}`)}>
                Create Labeling Job
              </Button>
            )}
          </Box>
        }
        sortingDisabled={false}
      />
    </Container>
  );
}
