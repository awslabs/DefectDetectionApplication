import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  ColumnLayout,
  Box,
  StatusIndicator,
  ProgressBar,
  Button,
  ButtonDropdown,
  Tabs,
  KeyValuePairs,
  Alert,
  Link,
  Modal,
} from '@cloudscape-design/components';
import { useParams, useNavigate } from 'react-router-dom';
import { LabelingJob } from '../types';
import { apiService } from '../services/api';
import ManifestTransformer from '../components/ManifestTransformer';

export default function LabelingDetail() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  const [job, setJob] = useState<LabelingJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTabId, setActiveTabId] = useState('overview');
  const [showTransformModal, setShowTransformModal] = useState(false);

  useEffect(() => {
    loadJob();
  }, [jobId]);

  const loadJob = async () => {
    if (!jobId) return;
    
    setLoading(true);
    try {
      const response = await apiService.getLabelingJob(jobId);
      const apiJob = response.job;
      
      // Map API response to LabelingJob type
      // Convert status from backend format (InProgress, Completed, Failed) to frontend format
      const statusMap: Record<string, LabelingJob['status']> = {
        'InProgress': 'in_progress',
        'Completed': 'completed',
        'Failed': 'failed',
        'Stopped': 'failed',
      };
      
      const mappedJob: LabelingJob = {
        job_id: apiJob.job_id,
        usecase_id: apiJob.usecase_id,
        name: apiJob.job_name,
        manifest_s3: apiJob.manifest_s3_uri,
        output_s3: apiJob.output_s3_uri,
        task_type: apiJob.task_type as LabelingJob['task_type'],
        images_count: apiJob.image_count,
        labeled_count: apiJob.human_labeled || apiJob.labeled_objects || 0,
        status: statusMap[apiJob.status] || 'pending',
        progress_percent: apiJob.progress_percent || 0,
        ground_truth_job_arn: apiJob.sagemaker_job_name,
        workforce_type: 'private',
        created_by: apiJob.created_by,
        created_at: apiJob.created_at,
        completed_at: apiJob.completed_at,
      };
      
      setJob(mappedJob);
    } catch (error) {
      console.error('Failed to load labeling job:', error);
    } finally {
      setLoading(false);
    }
  };

  const getStatusIndicator = (status: LabelingJob['status']) => {
    const statusMap = {
      pending: { type: 'pending' as const, label: 'Pending' },
      in_progress: { type: 'in-progress' as const, label: 'In Progress' },
      completed: { type: 'success' as const, label: 'Completed' },
      failed: { type: 'error' as const, label: 'Failed' },
    };
    const config = statusMap[status];
    return <StatusIndicator type={config.type}>{config.label}</StatusIndicator>;
  };

  const handleDownloadManifest = () => {
    if (job) {
      console.log('Downloading manifest from:', job.manifest_s3);
      // TODO: Implement actual download
      alert('Manifest download will be implemented with API integration');
    }
  };

  const handleDownloadOutput = () => {
    if (job) {
      console.log('Downloading output from:', job.output_s3);
      // TODO: Implement actual download
      alert('Output download will be implemented with API integration');
    }
  };

  if (loading) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          Loading labeling job details...
        </Box>
      </Container>
    );
  }

  if (!job) {
    return (
      <Container>
        <Alert type="error">Labeling job not found</Alert>
      </Container>
    );
  }

  return (
    <>
      <SpaceBetween size="l">
        <Container
          header={
            <Header
              variant="h1"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={() => navigate('/labeling')}>
                    Back to List
                  </Button>
                  {job.status === 'completed' && (
                    <>
                      <ButtonDropdown
                        items={[
                          {
                            id: 'transform',
                            text: 'Transform Manifest',
                            description: 'Convert to DDA-compatible format',
                          },
                          {
                            id: 'download-manifest',
                            text: 'Download Manifest',
                          },
                          {
                            id: 'view-s3',
                            text: 'View in S3',
                            external: true,
                          },
                        ]}
                        onItemClick={({ detail }) => {
                          if (detail.id === 'transform') {
                            setShowTransformModal(true);
                          } else if (detail.id === 'download-manifest') {
                            handleDownloadManifest();
                          } else if (detail.id === 'view-s3') {
                            window.open(
                              `https://s3.console.aws.amazon.com/s3/buckets/${job.output_s3.replace('s3://', '').split('/')[0]}`,
                              '_blank'
                            );
                          }
                        }}
                      >
                        Actions
                      </ButtonDropdown>
                      <Button variant="primary" onClick={handleDownloadOutput}>
                        Download Labeled Data
                      </Button>
                    </>
                  )}
                </SpaceBetween>
              }
            >
              {job.name}
            </Header>
          }
      >
        <ColumnLayout columns={4} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Status</Box>
            <div>{getStatusIndicator(job.status)}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Task Type</Box>
            <div>{job.task_type}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Workforce</Box>
            <div>{job.workforce_type}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Created By</Box>
            <div>{job.created_by}</div>
          </div>
        </ColumnLayout>
      </Container>

      <Container header={<Header variant="h2">Progress</Header>}>
        <SpaceBetween size="l">
          <ProgressBar
            value={job.progress_percent}
            label="Labeling Progress"
            description={`${job.labeled_count} of ${job.images_count} images labeled`}
            additionalInfo={`${job.progress_percent}% complete`}
          />

          <ColumnLayout columns={3} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Total Images</Box>
              <Box fontSize="heading-xl" fontWeight="bold">
                {job.images_count.toLocaleString()}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Labeled Images</Box>
              <Box fontSize="heading-xl" fontWeight="bold" color="text-status-success">
                {job.labeled_count.toLocaleString()}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Remaining</Box>
              <Box fontSize="heading-xl" fontWeight="bold" color="text-status-info">
                {(job.images_count - job.labeled_count).toLocaleString()}
              </Box>
            </div>
          </ColumnLayout>
        </SpaceBetween>
      </Container>

      <Container>
        <Tabs
          activeTabId={activeTabId}
          onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
          tabs={[
            {
              id: 'overview',
              label: 'Overview',
              content: (
                <SpaceBetween size="l">
                  <KeyValuePairs
                    columns={2}
                    items={[
                      {
                        label: 'Job ID',
                        value: job.job_id,
                      },
                      {
                        label: 'Ground Truth Job ARN',
                        value: (
                          <Box fontSize="body-s">
                            {job.ground_truth_job_arn}
                          </Box>
                        ),
                      },
                      {
                        label: 'Created',
                        value: new Date(job.created_at).toLocaleString(),
                      },
                      {
                        label: 'Completed',
                        value: job.completed_at
                          ? new Date(job.completed_at).toLocaleString()
                          : '-',
                      },
                      {
                        label: 'Duration',
                        value: job.completed_at
                          ? `${Math.round((job.completed_at - job.created_at) / 3600000)} hours`
                          : `${Math.round((Date.now() - job.created_at) / 3600000)} hours (ongoing)`,
                      },
                    ]}
                  />
                </SpaceBetween>
              ),
            },
            {
              id: 'data',
              label: 'Data Locations',
              content: (
                <SpaceBetween size="l">
                  <KeyValuePairs
                    columns={1}
                    items={[
                      {
                        label: 'Input Manifest',
                        value: (
                          <SpaceBetween direction="horizontal" size="xs">
                            <Box fontSize="body-s">
                              {job.manifest_s3}
                            </Box>
                            <Link onFollow={handleDownloadManifest}>Download</Link>
                          </SpaceBetween>
                        ),
                      },
                      {
                        label: 'Output Location',
                        value: (
                          <SpaceBetween direction="horizontal" size="xs">
                            <Box fontSize="body-s">
                              {job.output_s3}
                            </Box>
                            {job.status === 'completed' && (
                              <Link onFollow={handleDownloadOutput}>Download</Link>
                            )}
                          </SpaceBetween>
                        ),
                      },
                    ]}
                  />

                  {job.status === 'completed' && (
                    <Alert type="success">
                      Labeling job completed successfully. Labeled data is available for download
                      and can be used for training.
                    </Alert>
                  )}
                </SpaceBetween>
              ),
            },
            {
              id: 'workers',
              label: 'Worker Metrics',
              content: (
                <SpaceBetween size="l">
                  <Alert type="info">
                    Worker metrics and quality statistics will be available here once the API
                    integration is complete.
                  </Alert>

                  <Box>
                    <Box variant="h3">Placeholder Metrics</Box>
                    <ColumnLayout columns={3} variant="text-grid">
                      <div>
                        <Box variant="awsui-key-label">Active Workers</Box>
                        <Box fontSize="heading-l">12</Box>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Avg. Time per Image</Box>
                        <Box fontSize="heading-l">45s</Box>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Quality Score</Box>
                        <Box fontSize="heading-l">94%</Box>
                      </div>
                    </ColumnLayout>
                  </Box>
                </SpaceBetween>
              ),
            },
          ]}
        />
      </Container>
    </SpaceBetween>

    <Modal
      visible={showTransformModal}
      onDismiss={() => setShowTransformModal(false)}
      header="Transform Manifest"
      size="large"
      footer={
        <Box float="right">
          <Button variant="link" onClick={() => setShowTransformModal(false)}>
            Close
          </Button>
        </Box>
      }
    >
      <ManifestTransformer usecaseId={job.usecase_id} preSelectedJobId={job.job_id} />
    </Modal>
  </>
  );
}
