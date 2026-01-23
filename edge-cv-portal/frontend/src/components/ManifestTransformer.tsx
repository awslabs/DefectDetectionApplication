import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  FormField,
  Input,
  Select,
  SelectProps,
  Button,
  Alert,
  Box,
  ExpandableSection,
  Tabs,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { LabelingJob } from '../types';

interface ManifestTransformerProps {
  usecaseId: string;
  preSelectedJobId?: string;
}

export default function ManifestTransformer({ usecaseId, preSelectedJobId }: ManifestTransformerProps) {
  const [activeTabId, setActiveTabId] = useState('job-selector');
  const [labelingJobs, setLabelingJobs] = useState<LabelingJob[]>([]);
  const [selectedJob, setSelectedJob] = useState<SelectProps.Option | null>(null);
  const [loadingJobs, setLoadingJobs] = useState(false);
  const [sourceManifestUri, setSourceManifestUri] = useState('');
  const [outputManifestUri, setOutputManifestUri] = useState('');
  const [taskType, setTaskType] = useState<SelectProps.Option>({
    label: 'Classification',
    value: 'classification',
  });
  const [transforming, setTransforming] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [result, setResult] = useState<any>(null);

  useEffect(() => {
    loadLabelingJobs();
  }, [usecaseId]);

  useEffect(() => {
    // Auto-select job if preSelectedJobId is provided
    if (preSelectedJobId && labelingJobs.length > 0) {
      const job = labelingJobs.find(j => j.job_id === preSelectedJobId);
      if (job) {
        const option = {
          label: `${job.name} (${job.labeled_count} images)`,
          value: job.job_id,
        };
        handleJobSelection(option);
      }
    }
  }, [preSelectedJobId, labelingJobs]);

  const loadLabelingJobs = async () => {
    setLoadingJobs(true);
    try {
      const response = await apiService.listLabelingJobs({ usecase_id: usecaseId });
      const completedJobs = response.jobs.filter((job: any) => job.status === 'Completed');
      
      const transformedJobs: LabelingJob[] = completedJobs.map((job: any) => ({
        job_id: job.job_id,
        usecase_id: usecaseId,
        name: job.job_name,
        manifest_s3: '',
        output_s3: job.output_s3_uri || '',
        task_type: job.task_type as LabelingJob['task_type'],
        images_count: job.image_count,
        labeled_count: job.labeled_objects || 0,
        status: 'completed' as const,
        progress_percent: 100,
        ground_truth_job_arn: '',
        workforce_type: 'private',
        created_by: '',
        created_at: job.created_at,
      }));
      
      setLabelingJobs(transformedJobs);
    } catch (err) {
      console.error('Failed to load labeling jobs:', err);
    } finally {
      setLoadingJobs(false);
    }
  };

  const handleJobSelection = (option: SelectProps.Option) => {
    setSelectedJob(option);
    const job = labelingJobs.find(j => j.job_id === option.value);
    if (job && job.output_s3) {
      // Construct source manifest URI from output S3 path
      const sourceManifestUri = `${job.output_s3}manifests/output/output.manifest`;
      setSourceManifestUri(sourceManifestUri);
      
      // Auto-fill transformed manifest URI with -dda suffix
      const transformedManifestUri = `${job.output_s3}manifests/output/output-dda.manifest`;
      setOutputManifestUri(transformedManifestUri);
      
      // Auto-detect task type from job
      if (job.task_type === 'Segmentation') {
        setTaskType({ label: 'Segmentation', value: 'segmentation' });
      } else {
        setTaskType({ label: 'Classification', value: 'classification' });
      }
    }
  };

  const handleTransform = async () => {
    setError('');
    setSuccess('');
    setResult(null);
    setTransforming(true);

    try {
      const response = await apiService.transformManifest({
        usecase_id: usecaseId,
        source_manifest_uri: sourceManifestUri,
        output_manifest_uri: outputManifestUri || undefined,
        task_type: taskType.value as 'classification' | 'segmentation',
      });

      setSuccess('Manifest transformed successfully!');
      setResult(response);
    } catch (err: any) {
      setError(err.message || 'Failed to transform manifest');
    } finally {
      setTransforming(false);
    }
  };

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Transform Ground Truth manifests to DDA-compatible format"
        >
          Manifest Transformer
        </Header>
      }
    >
      <SpaceBetween size="l">
        <Alert type="info">
          <Box variant="p">
            Ground Truth creates manifests with job-specific attribute names (e.g., <code>my-job</code>, <code>my-job-metadata</code>).
            The DDA model requires standardized names: <code>anomaly-label</code> and <code>anomaly-label-metadata</code>.
          </Box>
          <Box variant="p">
            This tool automatically transforms your Ground Truth output manifest to be compatible with DDA training.
          </Box>
        </Alert>

        {error && (
          <Alert type="error" dismissible onDismiss={() => setError('')}>
            {error}
          </Alert>
        )}

        {success && (
          <Alert type="success" dismissible onDismiss={() => setSuccess('')}>
            {success}
          </Alert>
        )}

        <Tabs
          activeTabId={activeTabId}
          onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
          tabs={[
            {
              id: 'job-selector',
              label: 'Select Labeling Job',
              content: (
                <SpaceBetween size="m">
                  <FormField
                    label="Completed Labeling Job"
                    description="Select a completed labeling job to automatically fill in the manifest URI"
                    stretch
                  >
                    <Select
                      selectedOption={selectedJob}
                      onChange={({ detail }) => handleJobSelection(detail.selectedOption)}
                      options={labelingJobs.map(job => ({
                        label: `${job.name} (${job.labeled_count} images)`,
                        value: job.job_id,
                        description: `Created: ${new Date(job.created_at).toLocaleDateString()}`,
                      }))}
                      placeholder="Select a labeling job"
                      loadingText="Loading jobs..."
                      statusType={loadingJobs ? 'loading' : 'finished'}
                      empty="No completed labeling jobs found"
                    />
                  </FormField>

                  {selectedJob && sourceManifestUri && (
                    <Alert type="success">
                      <SpaceBetween size="xxs">
                        <Box variant="p">
                          <strong>Source manifest detected:</strong>
                        </Box>
                        <Box variant="code" fontSize="body-s">{sourceManifestUri}</Box>
                        <Box variant="p" color="text-body-secondary" fontSize="body-s">
                          Transformed manifest will be saved as: <code>output-dda.manifest</code>
                        </Box>
                      </SpaceBetween>
                    </Alert>
                  )}
                </SpaceBetween>
              ),
            },
            {
              id: 'manual-entry',
              label: 'Manual Entry',
              content: (
                <FormField
                  label="Source Manifest URI"
                  description="S3 URI of the Ground Truth output manifest (e.g., s3://bucket/path/output.manifest)"
                  stretch
                >
                  <Input
                    value={sourceManifestUri}
                    onChange={({ detail }) => setSourceManifestUri(detail.value)}
                    placeholder="s3://my-bucket/labeling/output/output.manifest"
                  />
                </FormField>
              ),
            },
          ]}
        />

        <FormField
          label="Output Manifest URI"
          description="Transformed manifest will be saved with -dda suffix in the same location"
          stretch
        >
          <Input
            value={outputManifestUri}
            onChange={({ detail }) => setOutputManifestUri(detail.value)}
            placeholder="Auto-generated from source manifest"
            disabled
          />
        </FormField>

        <FormField
          label="Task Type"
          description="Type of labeling task (auto-detected from selected job)"
          stretch
        >
          <Select
            selectedOption={taskType}
            onChange={({ detail }) => setTaskType(detail.selectedOption)}
            options={[
              { label: 'Classification', value: 'classification' },
              { label: 'Segmentation', value: 'segmentation' },
            ]}
          />
        </FormField>

        <Button
          variant="primary"
          onClick={handleTransform}
          loading={transforming}
          disabled={!sourceManifestUri}
        >
          Transform Manifest
        </Button>

        {result && (
          <SpaceBetween size="m">
            <Alert type="success">
              <Box variant="h4">Transformation Complete</Box>
              <SpaceBetween size="xs">
                <Box>
                  <strong>Transformed Manifest:</strong> <code>{result.transformed_manifest_uri}</code>
                </Box>
                <Box>
                  <strong>Total Entries:</strong> {result.stats.total_entries}
                </Box>
                <Box>
                  <strong>Transformed:</strong> {result.stats.transformed}
                </Box>
                {result.stats.skipped > 0 && (
                  <Box color="text-status-warning">
                    <strong>Skipped:</strong> {result.stats.skipped}
                  </Box>
                )}
              </SpaceBetween>
            </Alert>

            <ExpandableSection headerText="Detected Attributes">
              <SpaceBetween size="xs">
                <Box>
                  <strong>Original Label Attribute:</strong> <code>{result.detected_attributes.label_attr}</code>
                </Box>
                <Box>
                  <strong>Original Metadata Attribute:</strong> <code>{result.detected_attributes.metadata_attr}</code>
                </Box>
                <Box>
                  <strong>DDA Label Attribute:</strong> <code>{result.dda_attributes.label}</code>
                </Box>
                <Box>
                  <strong>DDA Metadata Attribute:</strong> <code>{result.dda_attributes.metadata}</code>
                </Box>
              </SpaceBetween>
            </ExpandableSection>

            {result.sample_entry && (
              <ExpandableSection headerText="Sample Transformed Entry">
                <Box>
                  <pre style={{ 
                    background: '#f4f4f4', 
                    padding: '12px', 
                    borderRadius: '4px',
                    overflow: 'auto',
                    fontSize: '12px'
                  }}>
                    {JSON.stringify(result.sample_entry, null, 2)}
                  </pre>
                </Box>
              </ExpandableSection>
            )}

            {result.stats.errors && result.stats.errors.length > 0 && (
              <ExpandableSection headerText={`Errors (${result.stats.errors.length})`}>
                <SpaceBetween size="xxs">
                  {result.stats.errors.map((err: string, idx: number) => (
                    <Box key={idx} color="text-status-error" fontSize="body-s">
                      {err}
                    </Box>
                  ))}
                </SpaceBetween>
              </ExpandableSection>
            )}

            <Alert type="info">
              <Box variant="p">
                <strong>Next Step:</strong> Use the transformed manifest URI when creating a training job.
              </Box>
              <Box variant="p">
                The transformed manifest is now compatible with the DDA model and can be used directly in SageMaker training.
              </Box>
            </Alert>
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
}
