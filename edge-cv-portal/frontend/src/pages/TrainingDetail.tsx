import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  ColumnLayout,
  StatusIndicator,
  Button,
  KeyValuePairs,
  Tabs,
  Textarea,
  ProgressBar,
  Alert,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import CompilationTab from '../components/CompilationTab';

export default function TrainingDetail() {
  const { trainingId } = useParams<{ trainingId: string }>();
  const navigate = useNavigate();
  const [activeTabId, setActiveTabId] = useState('overview');
  const [job, setJob] = useState<any>(null);
  const [logs, setLogs] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [logsLoading, setLogsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Fetch training job details
  useEffect(() => {
    const fetchJob = async () => {
      if (!trainingId) return;
      
      try {
        setLoading(true);
        setError(null);
        const response = await apiService.getTrainingJob(trainingId);
        setJob(response);
      } catch (err) {
        console.error('Failed to fetch training job:', err);
        setError(err instanceof Error ? err.message : 'Failed to load training job');
      } finally {
        setLoading(false);
      }
    };

    fetchJob();

    // Poll for updates every 30 seconds if job is in progress
    const interval = setInterval(() => {
      if (job?.status === 'InProgress' || job?.status === 'Pending') {
        fetchJob();
      }
    }, 30000);

    return () => clearInterval(interval);
  }, [trainingId]);

  // Fetch logs when logs tab is active
  useEffect(() => {
    const fetchLogs = async () => {
      if (!trainingId || activeTabId !== 'logs') return;

      try {
        setLogsLoading(true);
        const response = await apiService.getTrainingLogs(trainingId);
        
        // Format log events into a string
        if (response.message) {
          setLogs(response.message);
        } else if (response.logs && response.logs.length > 0) {
          const formattedLogs = response.logs
            .map(event => {
              const timestamp = new Date(event.timestamp).toLocaleTimeString();
              return `[${timestamp}] ${event.message}`;
            })
            .join('\n');
          setLogs(formattedLogs);
        } else {
          setLogs('No logs available yet. Training may not have started.');
        }
      } catch (err) {
        console.error('Failed to fetch logs:', err);
        setLogs('Failed to load logs. Please try again.');
      } finally {
        setLogsLoading(false);
      }
    };

    fetchLogs();

    // Auto-refresh logs every 10 seconds when logs tab is active and job is in progress
    const interval = setInterval(() => {
      if (activeTabId === 'logs' && (job?.status === 'InProgress' || job?.status === 'Pending')) {
        fetchLogs();
      }
    }, 10000);

    return () => clearInterval(interval);
  }, [trainingId, activeTabId, job?.status]);

  const handleStopTraining = async () => {
    if (!trainingId) return;

    try {
      await apiService.stopTrainingJob(trainingId);
      // Refresh job details
      const response = await apiService.getTrainingJob(trainingId);
      setJob(response);
    } catch (err) {
      console.error('Failed to stop training job:', err);
      setError(err instanceof Error ? err.message : 'Failed to stop training job');
    }
  };

  if (loading) {
    return <Box textAlign="center" padding="xxl">Loading training job details...</Box>;
  }

  if (error || !job) {
    return (
      <Alert type="error" header="Error loading training job">
        {error || 'Training job not found'}
      </Alert>
    );
  }

  const getStatusIndicator = (status: string) => {
    switch (status) {
      case 'Completed':
        return <StatusIndicator type="success">Completed</StatusIndicator>;
      case 'InProgress':
        return <StatusIndicator type="in-progress">In Progress</StatusIndicator>;
      case 'Failed':
        return <StatusIndicator type="error">Failed</StatusIndicator>;
      case 'Stopped':
        return <StatusIndicator type="stopped">Stopped</StatusIndicator>;
      case 'Pending':
        return <StatusIndicator type="pending">Pending</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{status}</StatusIndicator>;
    }
  };

  const formatTimestamp = (timestamp: number) => {
    return new Date(timestamp).toLocaleString();
  };

  const refreshLogs = async () => {
    if (!trainingId) return;
    
    try {
      setLogsLoading(true);
      const response = await apiService.getTrainingLogs(trainingId);
      
      // Format log events into a string
      if (response.message) {
        setLogs(response.message);
      } else if (response.logs && response.logs.length > 0) {
        const formattedLogs = response.logs
          .map(event => {
            const timestamp = new Date(event.timestamp).toLocaleTimeString();
            return `[${timestamp}] ${event.message}`;
          })
          .join('\n');
        setLogs(formattedLogs);
      } else {
        setLogs('No logs available yet. Training may not have started.');
      }
    } catch (err) {
      console.error('Failed to refresh logs:', err);
      setLogs('Failed to load logs. Please try again.');
    } finally {
      setLogsLoading(false);
    }
  };

  return (
    <SpaceBetween size="l">
      {/* Header */}
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => navigate('/training')}>Back to Training Jobs</Button>
            {job.source !== 'imported' && (
              <Button 
                onClick={() => navigate('/training/create', { 
                  state: { 
                    cloneFrom: {
                      model_name: job.model_name,
                      model_type: job.model_type,
                      dataset_manifest_s3: job.dataset_manifest_s3,
                      instance_type: job.instance_type,
                      hyperparameters: job.hyperparameters,
                      usecase_id: job.usecase_id
                    }
                  }
                })}
              >
                Clone Job
              </Button>
            )}
            <Button 
              disabled={job.status !== 'InProgress' || job.source === 'imported'} 
              onClick={handleStopTraining}
            >
              Stop Training
            </Button>
          </SpaceBetween>
        }
      >
        <SpaceBetween direction="horizontal" size="xs">
          {job.model_name}
          {job.source === 'imported' && (
            <StatusIndicator type="info">Imported</StatusIndicator>
          )}
        </SpaceBetween>
      </Header>

      {/* Show failure reason if job failed */}
      {job.status === 'Failed' && job.failure_reason && (
        <Alert type="error" header="Training Job Failed">
          {job.failure_reason}
        </Alert>
      )}

      {/* Status Cards */}
      <ColumnLayout columns={4} variant="text-grid">
        <Container>
          <Box variant="awsui-key-label">Status</Box>
          <Box variant="h2">{getStatusIndicator(job.status)}</Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Progress</Box>
          <Box variant="h3">
            <ProgressBar value={job.progress || 0} />
          </Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Instance Type</Box>
          <Box variant="h3">{job.instance_type}</Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Validation Accuracy</Box>
          <Box variant="h3">
            {job.metrics && job.metrics['validation:accuracy']
              ? `${(job.metrics['validation:accuracy'] * 100).toFixed(1)}%`
              : 'N/A'}
          </Box>
        </Container>
      </ColumnLayout>

      {/* Tabs */}
      <Tabs
        activeTabId={activeTabId}
        onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
        tabs={[
          {
            id: 'overview',
            label: 'Overview',
            content: (
              <SpaceBetween size="l">
                <Container header={<Header variant="h2">Job Information</Header>}>
                  <ColumnLayout columns={2} variant="text-grid">
                    <KeyValuePairs
                      columns={1}
                      items={[
                        { label: 'Training Job ID', value: job.training_id },
                        { label: 'Model Name', value: job.model_name },
                        { label: 'Version', value: job.model_version },
                        { label: 'Use Case', value: job.usecase_id },
                        { label: 'Source', value: job.source === 'imported' ? 'Imported Model (BYOM)' : 'SageMaker Training' },
                      ]}
                    />
                    <KeyValuePairs
                      columns={1}
                      items={[
                        { label: 'Status', value: job.status },
                        { label: 'Instance Type', value: job.source === 'imported' ? 'N/A (Imported)' : job.instance_type },
                        { label: 'Created By', value: job.created_by },
                        { label: 'Started', value: formatTimestamp(job.created_at) },
                      ]}
                    />
                  </ColumnLayout>
                </Container>

                {/* Show import metadata for imported models */}
                {job.source === 'imported' && job.metadata && (
                  <Container header={<Header variant="h2">Import Metadata</Header>}>
                    <ColumnLayout columns={2} variant="text-grid">
                      <KeyValuePairs
                        columns={1}
                        items={[
                          { label: 'Model Type', value: job.metadata.model_type },
                          { label: 'Framework', value: `${job.metadata.framework} ${job.metadata.framework_version}` },
                          { label: 'Model File', value: job.metadata.pt_file },
                        ]}
                      />
                      <KeyValuePairs
                        columns={1}
                        items={[
                          { label: 'Image Dimensions', value: `${job.metadata.image_width} x ${job.metadata.image_height}` },
                          { label: 'Input Shape', value: `[${job.metadata.input_shape?.join(', ')}]` },
                          { label: 'Model Artifact', value: job.artifact_s3 },
                        ]}
                      />
                    </ColumnLayout>
                  </Container>
                )}

                {/* Show algorithm info for trained models */}
                {job.source !== 'imported' && (
                  <Container header={<Header variant="h2">Algorithm</Header>}>
                    <KeyValuePairs
                      columns={1}
                      items={[
                        {
                          label: 'Algorithm ARN',
                          value: (
                            <Box fontSize="body-s">
                              <span style={{ fontFamily: 'monospace' }}>{job.algorithm_uri}</span>
                            </Box>
                          ),
                        },
                        { label: 'Dataset Manifest', value: job.dataset_manifest_s3 },
                      ]}
                    />
                  </Container>
                )}

                {job.hyperparameters && Object.keys(job.hyperparameters).length > 0 && (
                  <Container header={<Header variant="h2">Hyperparameters</Header>}>
                    <ColumnLayout columns={4} variant="text-grid">
                      {Object.entries(job.hyperparameters).map(([key, value]) => (
                        <div key={key}>
                          <Box variant="awsui-key-label">{key}</Box>
                          <Box>{String(value)}</Box>
                        </div>
                      ))}
                    </ColumnLayout>
                  </Container>
                )}

                {job.metrics && Object.keys(job.metrics).length > 0 && (
                  <Container header={<Header variant="h2">Current Metrics</Header>}>
                    <ColumnLayout columns={2} variant="text-grid">
                      {Object.entries(job.metrics).map(([key, value]) => (
                        <div key={key}>
                          <Box variant="awsui-key-label">{key.toUpperCase()}</Box>
                          <Box variant="h3">{typeof value === 'number' ? value.toFixed(4) : String(value)}</Box>
                        </div>
                      ))}
                    </ColumnLayout>
                  </Container>
                )}
              </SpaceBetween>
            ),
          },
          {
            id: 'logs',
            label: 'Logs',
            content: (
              <Container
                header={
                  <Header
                    variant="h2"
                    actions={
                      <SpaceBetween direction="horizontal" size="xs">
                        <Button 
                          iconName="download" 
                          onClick={async () => {
                            if (!trainingId) return;
                            try {
                              const response = await apiService.downloadTrainingLogs(trainingId);
                              // Create a blob and download it
                              const blob = new Blob([response], { type: 'text/plain' });
                              const url = window.URL.createObjectURL(blob);
                              const a = document.createElement('a');
                              a.href = url;
                              a.download = `training-logs-${job.training_job_name}.txt`;
                              document.body.appendChild(a);
                              a.click();
                              window.URL.revokeObjectURL(url);
                              document.body.removeChild(a);
                            } catch (err) {
                              console.error('Failed to download logs:', err);
                            }
                          }}
                        >
                          Download All Logs
                        </Button>
                        <Button 
                          iconName="refresh" 
                          onClick={refreshLogs}
                          loading={logsLoading}
                          disabled={logsLoading}
                        >
                          Refresh
                        </Button>
                      </SpaceBetween>
                    }
                  >
                    Training Logs
                  </Header>
                }
              >
                <SpaceBetween size="m">
                  {(job?.status === 'InProgress' || job?.status === 'Pending') && (
                    <Alert type="info">
                      Logs are auto-refreshing every 10 seconds. It may take a few minutes for logs to appear after the job starts.
                    </Alert>
                  )}
                  
                  {job?.status === 'Completed' && (
                    <Alert type="success">
                      Training completed. Showing final logs.
                    </Alert>
                  )}
                  
                  {job?.status === 'Failed' && (
                    <Alert type="error">
                      Training failed. Check logs for error details.
                    </Alert>
                  )}

                  <Textarea value={logs} rows={25} readOnly />

                  <Box variant="small" color="text-status-inactive">
                    Showing last 100 lines. Full logs available in CloudWatch.
                  </Box>
                </SpaceBetween>
              </Container>
            ),
          },
          {
            id: 'compilation',
            label: 'Compilation',
            content: (
              <CompilationTab
                trainingId={trainingId!}
                trainingJob={job}
                onRefresh={() => {
                  // Refresh the training job data when compilation status changes
                  const fetchJob = async () => {
                    if (!trainingId) return;
                    try {
                      const response = await apiService.getTrainingJob(trainingId);
                      setJob(response);
                    } catch (err) {
                      console.error('Failed to refresh training job:', err);
                    }
                  };
                  fetchJob();
                }}
              />
            ),
          },
        ]}
      />
    </SpaceBetween>
  );
}
