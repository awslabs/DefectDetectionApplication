import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  Wizard,
  FormField,
  Input,
  Select,
  SelectProps,
  SpaceBetween,
  Box,
  Alert,
  Textarea,
} from '@cloudscape-design/components';
import { useNavigate, useLocation, useSearchParams } from 'react-router-dom';
import { S3Dataset } from '../types';
import { apiService } from '../services/api';

interface LocationState {
  dataset?: S3Dataset;
}

export default function CreateLabelingJob() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const useCaseIdFromUrl = searchParams.get('usecase_id');
  const preselectedDataset = (location.state as LocationState)?.dataset;

  const [activeStepIndex, setActiveStepIndex] = useState(0);
  const [useCases, setUseCases] = useState<any[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<any>(null);
  const [jobName, setJobName] = useState('');
  const [description, setDescription] = useState('');
  const [datasetPrefix, setDatasetPrefix] = useState(preselectedDataset?.prefix || '');
  const [taskType, setTaskType] = useState<SelectProps.Option | null>(null);
  const [workforceType, setWorkforceType] = useState<SelectProps.Option | null>({
    label: 'Private',
    value: 'private',
  });
  const [labelCategories, setLabelCategories] = useState('');
  const [instructions, setInstructions] = useState('');
  const [workteams, setWorkteams] = useState<any[]>([]);
  const [selectedWorkteam, setSelectedWorkteam] = useState<SelectProps.Option | null>(null);
  const [loadingWorkteams, setLoadingWorkteams] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');

  // Load use cases on mount
  useEffect(() => {
    const loadUseCases = async () => {
      try {
        console.log('Loading use cases...');
        const data = await apiService.listUseCases();
        console.log('Use cases loaded:', data);
        setUseCases(data.usecases || []);
        
        // If use case ID is in URL, select that one
        if (useCaseIdFromUrl && data.usecases) {
          const useCaseFromUrl = data.usecases.find((uc: any) => uc.usecase_id === useCaseIdFromUrl);
          if (useCaseFromUrl) {
            console.log('Auto-selecting use case from URL:', useCaseFromUrl);
            setSelectedUseCase(useCaseFromUrl);
            return;
          }
        }
        
        // Otherwise auto-select first use case
        if (data.usecases && data.usecases.length > 0) {
          console.log('Auto-selecting first use case:', data.usecases[0]);
          setSelectedUseCase(data.usecases[0]);
        }
      } catch (err) {
        console.error('Failed to load use cases:', err);
        setError('Failed to load use cases');
      }
    };
    loadUseCases();
  }, [useCaseIdFromUrl]);

  // Load workteams when use case changes
  useEffect(() => {
    const loadWorkteams = async () => {
      if (!selectedUseCase) return;
      
      setLoadingWorkteams(true);
      try {
        console.log('Loading workteams for use case:', selectedUseCase.usecase_id);
        const data = await apiService.listWorkteams(selectedUseCase.usecase_id);
        console.log('Workteams loaded:', data);
        setWorkteams(data.workteams || []);
        // Auto-select first workteam
        if (data.workteams && data.workteams.length > 0) {
          setSelectedWorkteam({
            label: data.workteams[0].name,
            value: data.workteams[0].name,
            description: data.workteams[0].description,
          });
        }
      } catch (err) {
        console.error('Failed to load workteams:', err);
        // Don't set error here, just log it - workteams might not be set up yet
      } finally {
        setLoadingWorkteams(false);
      }
    };
    loadWorkteams();
  }, [selectedUseCase]);

  const taskTypeOptions = [
    { label: 'Object Detection', value: 'ObjectDetection' },
    { label: 'Image Classification', value: 'Classification' },
    { label: 'Semantic Segmentation', value: 'Segmentation' },
  ];

  const workforceOptions = [
    { label: 'Private', value: 'private' },
    { label: 'Public (Mechanical Turk)', value: 'public' },
    { label: 'Vendor', value: 'vendor' },
  ];

  const handleSubmit = async () => {
    setCreating(true);
    setError('');
    try {
      // Parse label categories
      const categories = labelCategories.split(',').map(c => c.trim()).filter(c => c);
      
      if (categories.length === 0) {
        setError('Please provide at least one label category');
        setCreating(false);
        return;
      }
      
      if (!selectedUseCase) {
        setError('Please select a use case');
        setCreating(false);
        return;
      }

      // Build workforce ARN from selected workteam
      if (!selectedWorkteam) {
        setError('Please select a workteam');
        setCreating(false);
        return;
      }
      
      const workforceArn = `arn:aws:sagemaker:us-east-1:${selectedUseCase.account_id}:workteam/private-crowd/${selectedWorkteam.value}`;
      
      await apiService.createLabelingJob({
        usecase_id: selectedUseCase.usecase_id,
        job_name: jobName,
        dataset_prefix: datasetPrefix,
        task_type: taskType?.value as string,
        label_categories: categories,
        workforce_arn: workforceArn,
        instructions: instructions || undefined,
        num_workers_per_object: 1,
        task_time_limit: 600,
      });

      navigate('/labeling');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create labeling job. Please try again.');
      console.error('Failed to create labeling job:', err);
    } finally {
      setCreating(false);
    }
  };

  return (
    <Container
      header={
        <Header variant="h1" description="Create a new Ground Truth labeling job">
          Create Labeling Job
        </Header>
      }
    >
      <Wizard
        i18nStrings={{
          stepNumberLabel: (stepNumber) => `Step ${stepNumber}`,
          collapsedStepsLabel: (stepNumber, stepsCount) =>
            `Step ${stepNumber} of ${stepsCount}`,
          skipToButtonLabel: (step) => `Skip to ${step.title}`,
          navigationAriaLabel: 'Steps',
          cancelButton: 'Cancel',
          previousButton: 'Previous',
          nextButton: 'Next',
          submitButton: 'Create Job',
          optional: 'optional',
        }}
        onNavigate={({ detail }) => setActiveStepIndex(detail.requestedStepIndex)}
        onCancel={() => navigate('/labeling')}
        onSubmit={handleSubmit}
        activeStepIndex={activeStepIndex}
        isLoadingNextStep={creating}
        steps={[
          {
            title: 'Job Configuration',
            description: 'Basic job information',
            content: (
              <SpaceBetween size="l">
                {error && <Alert type="error">{error}</Alert>}
                
                <FormField
                  label="Job Name"
                  description="A unique name for this labeling job"
                  constraintText="Required"
                >
                  <Input
                    value={jobName}
                    onChange={({ detail }) => setJobName(detail.value)}
                    placeholder="e.g., Defect Detection - Batch 1"
                  />
                </FormField>

                <FormField
                  label="Description"
                  description="Optional description of this labeling job"
                >
                  <Textarea
                    value={description}
                    onChange={({ detail }) => setDescription(detail.value)}
                    placeholder="Describe the purpose of this labeling job..."
                    rows={3}
                  />
                </FormField>

                {useCaseIdFromUrl ? (
                  <FormField
                    label="Use Case"
                    description="The use case this job belongs to"
                  >
                    <Input
                      value={selectedUseCase?.name || ''}
                      disabled
                      readOnly
                    />
                  </FormField>
                ) : (
                  <FormField
                    label="Use Case"
                    description="The use case this job belongs to"
                    constraintText="Required"
                  >
                    <Select
                      selectedOption={
                        selectedUseCase
                          ? {
                              label: selectedUseCase.name,
                              value: selectedUseCase.usecase_id,
                            }
                          : null
                      }
                      onChange={({ detail }) => {
                        const useCase = useCases.find(
                          (uc) => uc.usecase_id === detail.selectedOption.value
                        );
                        setSelectedUseCase(useCase);
                      }}
                      options={useCases.map((uc) => ({
                        label: uc.name,
                        value: uc.usecase_id,
                      }))}
                      placeholder="Select a use case"
                      selectedAriaLabel="Selected"
                    />
                  </FormField>
                )}
              </SpaceBetween>
            ),
          },
          {
            title: 'Dataset Selection',
            description: 'Choose the dataset to label',
            content: (
              <SpaceBetween size="l">
                <FormField
                  label="S3 Dataset Prefix"
                  description="The S3 prefix containing images to label"
                  constraintText="Required"
                >
                  <Input
                    value={datasetPrefix}
                    onChange={({ detail }) => setDatasetPrefix(detail.value)}
                    placeholder="e.g., raw-images/production-line-1/"
                  />
                </FormField>

                {preselectedDataset && (
                  <Alert type="info">
                    Dataset preselected: {preselectedDataset.image_count.toLocaleString()} images
                  </Alert>
                )}

                <FormField
                  label="S3 Bucket"
                  description="The S3 bucket containing the dataset"
                >
                  <Input value={selectedUseCase?.data_s3_bucket || selectedUseCase?.s3_bucket || ''} disabled />
                </FormField>
              </SpaceBetween>
            ),
          },
          {
            title: 'Task Configuration',
            description: 'Configure the labeling task',
            content: (
              <SpaceBetween size="l">
                <FormField
                  label="Task Type"
                  description="The type of labeling task"
                  constraintText="Required"
                >
                  <Select
                    selectedOption={taskType}
                    onChange={({ detail }) => setTaskType(detail.selectedOption)}
                    options={taskTypeOptions}
                    placeholder="Select task type"
                  />
                </FormField>

                <FormField
                  label="Label Categories"
                  description="Comma-separated list of label categories"
                  constraintText="Required"
                  info={
                    <Box>
                      For anomaly detection, the first category should be "normal" (non-defect) 
                      and subsequent categories should be defect types. This ensures correct 
                      label encoding where 0=normal, 1+=anomaly.
                    </Box>
                  }
                >
                  <Input
                    value={labelCategories}
                    onChange={({ detail }) => setLabelCategories(detail.value)}
                    placeholder="e.g., normal, defect"
                  />
                </FormField>
                
                {labelCategories && (
                  <Alert type="info">
                    <Box>
                      <strong>Label Order Preview:</strong>
                      <ul style={{ marginTop: '8px', marginBottom: 0 }}>
                        {labelCategories.split(',').map((cat, idx) => (
                          <li key={idx}>
                            <strong>{idx}</strong> = {cat.trim()}
                            {idx === 0 && ' (should be normal/non-defect)'}
                            {idx > 0 && ' (anomaly/defect)'}
                          </li>
                        ))}
                      </ul>
                    </Box>
                  </Alert>
                )}

                <FormField
                  label="Labeling Instructions"
                  description="Instructions for workers performing the labeling"
                >
                  <Textarea
                    value={instructions}
                    onChange={({ detail }) => setInstructions(detail.value)}
                    placeholder="Provide clear instructions for labeling workers..."
                    rows={5}
                  />
                </FormField>
              </SpaceBetween>
            ),
          },
          {
            title: 'Workforce Configuration',
            description: 'Configure the labeling workforce',
            content: (
              <SpaceBetween size="l">
                <FormField
                  label="Workforce Type"
                  description="The type of workforce to use for labeling"
                  constraintText="Required"
                >
                  <Select
                    selectedOption={workforceType}
                    onChange={({ detail }) => setWorkforceType(detail.selectedOption)}
                    options={workforceOptions}
                  />
                </FormField>

                {workforceType?.value === 'private' && (
                  <>
                    <Alert type="info">
                      Private workforce requires a pre-configured work team in SageMaker Ground Truth.
                    </Alert>
                    
                    <FormField
                      label="Workteam"
                      description="Select the workteam to use for labeling"
                      constraintText="Required"
                    >
                      <Select
                        selectedOption={selectedWorkteam}
                        onChange={({ detail }) => setSelectedWorkteam(detail.selectedOption)}
                        options={workteams.map((wt) => ({
                          label: wt.name,
                          value: wt.name,
                          description: wt.description || `${wt.member_count} members`,
                        }))}
                        placeholder={loadingWorkteams ? 'Loading workteams...' : 'Select a workteam'}
                        disabled={loadingWorkteams || workteams.length === 0}
                        empty={workteams.length === 0 ? 'No workteams found. Please create a workteam in SageMaker Ground Truth.' : undefined}
                        selectedAriaLabel="Selected"
                      />
                    </FormField>
                  </>
                )}

                {workforceType?.value === 'public' && (
                  <Alert type="warning">
                    Public workforce (Mechanical Turk) may incur additional costs and requires
                    careful review of labeled data.
                  </Alert>
                )}
              </SpaceBetween>
            ),
          },
          {
            title: 'Review and Create',
            description: 'Review your configuration',
            content: (
              <SpaceBetween size="l">
                <Box variant="h3">Job Configuration</Box>
                <Box>
                  <Box variant="awsui-key-label">Job Name</Box>
                  <Box>{jobName || '-'}</Box>
                </Box>
                <Box>
                  <Box variant="awsui-key-label">Description</Box>
                  <Box>{description || '-'}</Box>
                </Box>

                <Box variant="h3">Dataset</Box>
                <Box>
                  <Box variant="awsui-key-label">Source Images</Box>
                  <Box>s3://{selectedUseCase?.data_s3_bucket || selectedUseCase?.s3_bucket || 'bucket'}/{datasetPrefix}</Box>
                </Box>
                <Box>
                  <Box variant="awsui-key-label">Labeling Output</Box>
                  <Box>
                    {selectedUseCase?.s3_bucket 
                      ? `s3://${selectedUseCase.s3_bucket}/labeled/` 
                      : <Alert type="error">Output bucket not configured. Please update the UseCase settings to add an S3 bucket for labeling outputs.</Alert>
                    }
                  </Box>
                </Box>

                <Box variant="h3">Task Configuration</Box>
                <Box>
                  <Box variant="awsui-key-label">Task Type</Box>
                  <Box>{taskType?.label || '-'}</Box>
                </Box>
                <Box>
                  <Box variant="awsui-key-label">Label Categories</Box>
                  <Box>{labelCategories || '-'}</Box>
                </Box>

                <Box variant="h3">Workforce</Box>
                <Box>
                  <Box variant="awsui-key-label">Workforce Type</Box>
                  <Box>{workforceType?.label || '-'}</Box>
                </Box>
                {workforceType?.value === 'private' && (
                  <Box>
                    <Box variant="awsui-key-label">Workteam</Box>
                    <Box>{selectedWorkteam?.label || '-'}</Box>
                  </Box>
                )}

                {(!jobName || !datasetPrefix || !taskType || !labelCategories) && (
                  <Alert type="warning">
                    Please complete all required fields before creating the job.
                  </Alert>
                )}
              </SpaceBetween>
            ),
            isOptional: false,
          },
        ]}
      />
    </Container>
  );
}
