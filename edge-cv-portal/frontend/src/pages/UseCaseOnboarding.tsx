import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Wizard,
  Container,
  Header,
  SpaceBetween,
  FormField,
  Input,
  Textarea,
  Alert,
  Box,
  Button,
  ColumnLayout,
  ExpandableSection,
  StatusIndicator,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface OnboardingState {
  // Step 1: Basic Info
  useCaseName: string;
  description: string;
  costCenter: string;

  // Step 2: AWS Account Setup
  accountId: string;
  roleArn: string;
  sagemakerExecutionRoleArn: string;
  externalId: string;
  s3Bucket: string;
  s3Prefix: string;

  // Step 3: Role Deployment Status
  roleDeployed: boolean;
  roleVerified: boolean;

  // Step 4: S3 Setup Status
  s3Created: boolean;
  s3Verified: boolean;

  // Optional: Separate Data Account
  useSeparateDataAccount: boolean;
  dataAccountId: string;
  dataAccountRoleArn: string;
  dataAccountExternalId: string;
  dataS3Bucket: string;
  dataS3Prefix: string;
  dataRoleVerified: boolean;

  // Step 5: Next Steps Selection
  nextSteps: {
    labelData: boolean;
    trainModel: boolean;
    setupDevices: boolean;
  };
}

export default function UseCaseOnboarding() {
  const navigate = useNavigate();
  const [activeStepIndex, setActiveStepIndex] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [state, setState] = useState<OnboardingState>({
    useCaseName: '',
    description: '',
    costCenter: '',
    accountId: '',
    roleArn: '',
    sagemakerExecutionRoleArn: '',
    externalId: '',
    s3Bucket: '',
    s3Prefix: 'datasets/',
    roleDeployed: false,
    roleVerified: false,
    s3Created: false,
    s3Verified: false,
    useSeparateDataAccount: false,
    dataAccountId: '',
    dataAccountRoleArn: '',
    dataAccountExternalId: '',
    dataS3Bucket: '',
    dataS3Prefix: 'datasets/',
    dataRoleVerified: false,
    nextSteps: {
      labelData: false,
      trainModel: false,
      setupDevices: false,
    },
  });

  const updateState = (updates: Partial<OnboardingState>) => {
    setState((prev) => ({ ...prev, ...updates }));
  };

  const handleVerifyRole = async () => {
    try {
      setError(null);
      // TODO: Call API to verify role can be assumed
      await new Promise((resolve) => setTimeout(resolve, 1000)); // Simulate API call
      updateState({ roleVerified: true });
    } catch (err) {
      setError('Failed to verify role. Please check the ARN and External ID.');
    }
  };

  const handleVerifyDataRole = async () => {
    try {
      setError(null);
      // TODO: Call API to verify data account role can be assumed
      await new Promise((resolve) => setTimeout(resolve, 1000)); // Simulate API call
      updateState({ dataRoleVerified: true });
    } catch (err) {
      setError('Failed to verify Data Account role. Please check the ARN and External ID.');
    }
  };

  const handleVerifyS3 = async () => {
    try {
      setError(null);
      // TODO: Call API to verify S3 bucket access
      await new Promise((resolve) => setTimeout(resolve, 1000)); // Simulate API call
      updateState({ s3Verified: true });
    } catch (err) {
      setError('Failed to verify S3 bucket access.');
    }
  };

  const handleSubmit = async () => {
    try {
      setSubmitting(true);
      setError(null);

      // Create the use case
      const useCaseData: Record<string, unknown> = {
        name: state.useCaseName,
        account_id: state.accountId,
        s3_bucket: state.s3Bucket,
        s3_prefix: state.s3Prefix,
        cross_account_role_arn: state.roleArn,
        sagemaker_execution_role_arn: state.sagemakerExecutionRoleArn,
        external_id: state.externalId,
        cost_center: state.costCenter,
      };

      // Add separate data account fields if configured
      if (state.useSeparateDataAccount && state.dataAccountRoleArn) {
        useCaseData.data_account_id = state.dataAccountId;
        useCaseData.data_account_role_arn = state.dataAccountRoleArn;
        useCaseData.data_account_external_id = state.dataAccountExternalId;
        useCaseData.data_s3_bucket = state.dataS3Bucket;
        useCaseData.data_s3_prefix = state.dataS3Prefix;
      }

      await apiService.createUseCase(useCaseData);

      // Navigate based on next steps
      if (state.nextSteps.labelData) {
        navigate('/labeling/create');
      } else if (state.nextSteps.trainModel) {
        navigate('/training/create');
      } else if (state.nextSteps.setupDevices) {
        navigate('/devices');
      } else {
        navigate('/usecases');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create use case');
    } finally {
      setSubmitting(false);
    }
  };

  const cdkDeployCommand = `cd edge-cv-portal
./deploy-account-role.sh`;

  const s3SetupCommands = `# Create S3 bucket
aws s3 mb s3://${state.s3Bucket || 'your-bucket-name'}

# Create folder structure
aws s3api put-object --bucket ${state.s3Bucket || 'your-bucket-name'} --key datasets/
aws s3api put-object --bucket ${state.s3Bucket || 'your-bucket-name'} --key models/
aws s3api put-object --bucket ${state.s3Bucket || 'your-bucket-name'} --key manifests/

# Enable versioning (recommended)
aws s3api put-bucket-versioning \\
  --bucket ${state.s3Bucket || 'your-bucket-name'} \\
  --versioning-configuration Status=Enabled`;

  return (
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
        submitButton: 'Create Use Case',
        optional: 'optional',
      }}
      onNavigate={({ detail }) => setActiveStepIndex(detail.requestedStepIndex)}
      onCancel={() => navigate('/usecases')}
      onSubmit={handleSubmit}
      activeStepIndex={activeStepIndex}
      isLoadingNextStep={submitting}
      steps={[
        {
          title: 'Basic Information',
          description: 'Provide basic details about your use case',
          content: (
            <Container header={<Header variant="h2">Use Case Details</Header>}>
              <SpaceBetween size="l">
                {error && (
                  <Alert type="error" dismissible onDismiss={() => setError(null)}>
                    {error}
                  </Alert>
                )}

                <FormField
                  label="Use Case Name"
                  description="A descriptive name for your use case"
                  stretch
                >
                  <Input
                    value={state.useCaseName}
                    onChange={({ detail }) => updateState({ useCaseName: detail.value })}
                    placeholder="e.g., Manufacturing Line 1 Defect Detection"
                  />
                </FormField>

                <FormField
                  label="Description"
                  description="Detailed description of what this use case does"
                  stretch
                >
                  <Textarea
                    value={state.description}
                    onChange={({ detail }) => updateState({ description: detail.value })}
                    placeholder="Describe the purpose and scope of this use case..."
                    rows={4}
                  />
                </FormField>

                <FormField
                  label="Cost Center"
                  description="Cost center or department for billing tracking"
                  stretch
                >
                  <Input
                    value={state.costCenter}
                    onChange={({ detail }) => updateState({ costCenter: detail.value })}
                    placeholder="e.g., DEPT-001"
                  />
                </FormField>
              </SpaceBetween>
            </Container>
          ),
        },
        {
          title: 'Deploy IAM Role',
          description: 'Set up cross-account access role in your AWS account',
          content: (
            <SpaceBetween size="l">
              <Container header={<Header variant="h2">Why is this role needed?</Header>}>
                <SpaceBetween size="s">
                  <Box>
                    The DDA Portal runs in a central AWS account but needs to manage resources in your UseCase account. 
                    This cross-account IAM role enables the portal to:
                  </Box>
                  <Box variant="small">
                    <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>
                      <li><strong>SageMaker</strong> - Start training jobs, create labeling jobs, manage models</li>
                      <li><strong>S3</strong> - Access training datasets and store model artifacts</li>
                      <li><strong>Greengrass</strong> - Deploy models to edge devices, manage components</li>
                      <li><strong>IoT</strong> - Register and monitor edge devices</li>
                      <li><strong>CloudWatch</strong> - View training logs and device metrics</li>
                    </ul>
                  </Box>
                  <Alert type="info">
                    The role uses an External ID for security - only the DDA Portal can assume this role, 
                    and all actions are auditable in CloudTrail.
                  </Alert>
                </SpaceBetween>
              </Container>

              <Container header={<Header variant="h2">Deploy the Role</Header>}>
                <SpaceBetween size="m">
                  <Box>
                    Run the deployment script in your terminal. Make sure your AWS CLI is configured 
                    for the UseCase account where you want to deploy.
                  </Box>
                  <Box
                    variant="code"
                    padding="s"
                    fontSize="body-s"
                  >
                    <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                      {cdkDeployCommand}
                    </pre>
                  </Box>
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      iconName="copy"
                      onClick={() => {
                        navigator.clipboard.writeText(cdkDeployCommand);
                      }}
                    >
                      Copy Command
                    </Button>
                  </SpaceBetween>
                  <Box variant="small">
                    The script will prompt you for the Portal Account ID and output the Role ARN, 
                    SageMaker Execution Role ARN, and External ID needed below.
                  </Box>

                  <FormField label="I have deployed the role" stretch>
                    <Button
                      variant={state.roleDeployed ? 'normal' : 'primary'}
                      onClick={() => updateState({ roleDeployed: true })}
                      disabled={state.roleDeployed}
                    >
                      {state.roleDeployed ? '✓ Role Deployed' : 'Mark as Deployed'}
                    </Button>
                  </FormField>
                </SpaceBetween>
              </Container>

              {state.roleDeployed && (
                <Container header={<Header variant="h2">Enter Role Details</Header>}>
                  <SpaceBetween size="m">
                    <Alert type="info">
                      Upload the <code>usecase-account-config.txt</code> file generated by the deployment script, 
                      or enter the values manually below.
                    </Alert>

                    <FormField
                      label="Upload Configuration File"
                      description="Upload usecase-account-config.txt to auto-fill the fields"
                      stretch
                    >
                      <input
                        type="file"
                        accept=".txt"
                        onChange={(e) => {
                          const file = e.target.files?.[0];
                          if (file) {
                            const reader = new FileReader();
                            reader.onload = (event) => {
                              const content = event.target?.result as string;
                              // Parse the config file
                              const lines = content.split('\n');
                              const config: Record<string, string> = {};
                              lines.forEach(line => {
                                const match = line.match(/^([^:]+):\s*(.+)$/);
                                if (match) {
                                  config[match[1].trim()] = match[2].trim();
                                }
                              });
                              // Update state with parsed values
                              updateState({
                                accountId: config['Account ID'] || '',
                                roleArn: config['Role ARN'] || '',
                                sagemakerExecutionRoleArn: config['SageMaker Execution Role ARN'] || '',
                                externalId: config['External ID'] || '',
                              });
                            };
                            reader.readAsText(file);
                          }
                        }}
                        style={{ 
                          padding: '8px',
                          border: '1px dashed #aab7b8',
                          borderRadius: '4px',
                          width: '100%',
                          cursor: 'pointer'
                        }}
                      />
                    </FormField>

                    <Box variant="h4">Or enter manually:</Box>

                    <FormField
                      label="AWS Account ID"
                      description="The AWS account where the role was deployed"
                      stretch
                    >
                      <Input
                        value={state.accountId}
                        onChange={({ detail }) => updateState({ accountId: detail.value })}
                        placeholder="123456789012"
                      />
                    </FormField>

                    <FormField
                      label="Role ARN"
                      description="The ARN of the deployed role"
                      stretch
                    >
                      <Input
                        value={state.roleArn}
                        onChange={({ detail }) => updateState({ roleArn: detail.value })}
                        placeholder="arn:aws:iam::123456789012:role/DDAPortalAccessRole"
                      />
                    </FormField>

                    <FormField
                      label="SageMaker Execution Role ARN"
                      description="The ARN of the SageMaker execution role (from CloudFormation outputs)"
                      stretch
                    >
                      <Input
                        value={state.sagemakerExecutionRoleArn}
                        onChange={({ detail }) => updateState({ sagemakerExecutionRoleArn: detail.value })}
                        placeholder="arn:aws:iam::123456789012:role/DDASageMakerExecutionRole"
                      />
                    </FormField>

                    <FormField
                      label="External ID"
                      description="The External ID used when creating the role"
                      stretch
                    >
                      <Input
                        value={state.externalId}
                        onChange={({ detail }) => updateState({ externalId: detail.value })}
                        placeholder="a1b2c3d4-e5f6-7890-abcd-ef1234567890"
                        type="password"
                      />
                    </FormField>

                    <FormField label="Verify Role Access" stretch>
                      <SpaceBetween direction="horizontal" size="xs">
                        <Button
                          onClick={handleVerifyRole}
                          disabled={!state.accountId || !state.roleArn || !state.externalId}
                        >
                          Verify Role
                        </Button>
                        {state.roleVerified && (
                          <StatusIndicator type="success">Role verified successfully</StatusIndicator>
                        )}
                      </SpaceBetween>
                    </FormField>
                  </SpaceBetween>
                </Container>
              )}
            </SpaceBetween>
          ),
          isOptional: false,
        },
        {
          title: 'Configure S3 Storage',
          description: 'Set up S3 bucket for datasets and models',
          content: (
            <SpaceBetween size="l">
              <Alert type="info">
                Your training data, models, and artifacts will be stored in an S3 bucket in your
                AWS account.
              </Alert>

              <Container header={<Header variant="h2">Step 1: Create S3 Bucket</Header>}>
                <SpaceBetween size="m">
                  <Box>
                    Create an S3 bucket in your AWS account with the following structure:
                  </Box>

                  <Box
                    variant="code"
                    padding="s"
                    fontSize="body-s"
                  >
                    <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                      {s3SetupCommands}
                    </pre>
                  </Box>
                  <Button
                    iconName="copy"
                    onClick={() => {
                      navigator.clipboard.writeText(s3SetupCommands);
                    }}
                  >
                    Copy Commands
                  </Button>

                  <Box variant="small">
                    <strong>Recommended folder structure:</strong>
                    <ul>
                      <li>
                        <code>datasets/</code> - Raw training images
                      </li>
                      <li>
                        <code>manifests/</code> - Ground Truth manifest files
                      </li>
                      <li>
                        <code>models/</code> - Trained model artifacts
                      </li>
                      <li>
                        <code>compiled/</code> - Compiled models for edge devices
                      </li>
                    </ul>
                  </Box>

                  <FormField label="I have created the S3 bucket" stretch>
                    <Button
                      variant={state.s3Created ? 'normal' : 'primary'}
                      onClick={() => updateState({ s3Created: true })}
                      disabled={state.s3Created}
                    >
                      {state.s3Created ? '✓ Bucket Created' : 'Mark as Created'}
                    </Button>
                  </FormField>
                </SpaceBetween>
              </Container>

              {state.s3Created && (
                <Container header={<Header variant="h2">Step 2: Enter Bucket Details</Header>}>
                  <SpaceBetween size="m">
                    <FormField
                      label="S3 Bucket Name"
                      description="The name of your S3 bucket"
                      stretch
                    >
                      <Input
                        value={state.s3Bucket}
                        onChange={({ detail }) => updateState({ s3Bucket: detail.value })}
                        placeholder="my-edge-cv-data"
                      />
                    </FormField>

                    <FormField
                      label="S3 Prefix (Optional)"
                      description="Path prefix within the bucket"
                      stretch
                    >
                      <Input
                        value={state.s3Prefix}
                        onChange={({ detail }) => updateState({ s3Prefix: detail.value })}
                        placeholder="datasets/"
                      />
                    </FormField>

                    <FormField label="Verify Bucket Access" stretch>
                      <SpaceBetween direction="horizontal" size="xs">
                        <Button onClick={handleVerifyS3} disabled={!state.s3Bucket}>
                          Verify Access
                        </Button>
                        {state.s3Verified && (
                          <StatusIndicator type="success">
                            Bucket access verified successfully
                          </StatusIndicator>
                        )}
                      </SpaceBetween>
                    </FormField>
                  </SpaceBetween>
                </Container>
              )}

              {/* Optional: Separate Data Account */}
              <ExpandableSection 
                headerText="Advanced: Use Separate Data Account (Optional)"
                variant="footer"
              >
                <SpaceBetween size="m">
                  <Alert type="info">
                    By default, training data is stored in the same AWS account as your UseCase.
                    If you need to store data in a different AWS account (e.g., a centralized data lake),
                    configure a separate Data Account below.
                  </Alert>

                  <FormField stretch>
                    <Box>
                      <input
                        type="checkbox"
                        checked={state.useSeparateDataAccount}
                        onChange={(e) =>
                          updateState({ useSeparateDataAccount: e.target.checked })
                        }
                      />{' '}
                      <strong>Use a separate AWS account for data storage</strong>
                    </Box>
                  </FormField>

                  {state.useSeparateDataAccount && (
                    <Container header={<Header variant="h3">Data Account Configuration</Header>}>
                      <SpaceBetween size="m">
                        <Alert type="info">
                          Upload the <code>data-account-config.txt</code> file generated by the deployment script, 
                          or enter the values manually below.
                        </Alert>

                        <FormField
                          label="Upload Data Account Configuration File"
                          description="Upload data-account-config.txt to auto-fill the fields"
                          stretch
                        >
                          <input
                            type="file"
                            accept=".txt"
                            onChange={(e) => {
                              const file = e.target.files?.[0];
                              if (file) {
                                const reader = new FileReader();
                                reader.onload = (event) => {
                                  const content = event.target?.result as string;
                                  // Parse the config file
                                  const lines = content.split('\n');
                                  const config: Record<string, string> = {};
                                  lines.forEach(line => {
                                    const match = line.match(/^([^:]+):\s*(.+)$/);
                                    if (match) {
                                      config[match[1].trim()] = match[2].trim();
                                    }
                                  });
                                  // Update state with parsed values
                                  updateState({
                                    dataAccountId: config['Data Account ID'] || '',
                                    dataAccountRoleArn: config['Portal Access Role ARN'] || '',
                                    dataAccountExternalId: config['External ID'] || '',
                                  });
                                };
                                reader.readAsText(file);
                              }
                            }}
                            style={{ 
                              padding: '8px',
                              border: '1px dashed #aab7b8',
                              borderRadius: '4px',
                              width: '100%',
                              cursor: 'pointer'
                            }}
                          />
                        </FormField>

                        <Box variant="h4">Or enter manually:</Box>

                        <FormField
                          label="Data Account ID"
                          description="AWS Account ID where training data will be stored"
                          stretch
                        >
                          <Input
                            value={state.dataAccountId}
                            onChange={({ detail }) => updateState({ dataAccountId: detail.value })}
                            placeholder="987654321098"
                          />
                        </FormField>

                        <FormField
                          label="Data Account Role ARN"
                          description="IAM role ARN in the Data Account"
                          stretch
                        >
                          <Input
                            value={state.dataAccountRoleArn}
                            onChange={({ detail }) => updateState({ dataAccountRoleArn: detail.value })}
                            placeholder="arn:aws:iam::987654321098:role/DDAPortalDataAccessRole"
                          />
                        </FormField>

                        <FormField
                          label="Data Account External ID"
                          description="External ID for the Data Account role"
                          stretch
                        >
                          <Input
                            value={state.dataAccountExternalId}
                            onChange={({ detail }) => updateState({ dataAccountExternalId: detail.value })}
                            placeholder="a1b2c3d4-e5f6-7890-abcd-ef1234567890"
                            type="password"
                          />
                        </FormField>

                        <FormField
                          label="Data S3 Bucket"
                          description="S3 bucket in the Data Account for training data"
                          stretch
                        >
                          <Input
                            value={state.dataS3Bucket}
                            onChange={({ detail }) => updateState({ dataS3Bucket: detail.value })}
                            placeholder="my-data-lake-bucket"
                          />
                        </FormField>

                        <FormField
                          label="Data S3 Prefix (Optional)"
                          description="Path prefix within the data bucket"
                          stretch
                        >
                          <Input
                            value={state.dataS3Prefix}
                            onChange={({ detail }) => updateState({ dataS3Prefix: detail.value })}
                            placeholder="datasets/"
                          />
                        </FormField>

                        <FormField label="Verify Data Account Role" stretch>
                          <SpaceBetween direction="horizontal" size="xs">
                            <Button
                              onClick={handleVerifyDataRole}
                              disabled={!state.dataAccountId || !state.dataAccountRoleArn || !state.dataAccountExternalId}
                            >
                              Verify Role
                            </Button>
                            {state.dataRoleVerified && (
                              <StatusIndicator type="success">Data Account role verified successfully</StatusIndicator>
                            )}
                          </SpaceBetween>
                        </FormField>
                      </SpaceBetween>
                    </Container>
                  )}
                </SpaceBetween>
              </ExpandableSection>
            </SpaceBetween>
          ),
        },
        {
          title: 'Review & Next Steps',
          description: 'Review configuration and choose next steps',
          content: (
            <SpaceBetween size="l">
              <Container header={<Header variant="h2">Configuration Summary</Header>}>
                <ColumnLayout columns={2} variant="text-grid">
                  <SpaceBetween size="xs">
                    <div>
                      <Box variant="awsui-key-label">Use Case Name</Box>
                      <Box>{state.useCaseName}</Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">AWS Account ID</Box>
                      <Box>{state.accountId}</Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Role ARN</Box>
                      <Box fontSize="body-s">
                        <code>{state.roleArn}</code>
                      </Box>
                    </div>
                  </SpaceBetween>
                  <SpaceBetween size="xs">
                    <div>
                      <Box variant="awsui-key-label">Cost Center</Box>
                      <Box>{state.costCenter || 'Not specified'}</Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">S3 Bucket</Box>
                      <Box>{state.s3Bucket}</Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">S3 Prefix</Box>
                      <Box>{state.s3Prefix || '/'}</Box>
                    </div>
                  </SpaceBetween>
                </ColumnLayout>

                {state.useSeparateDataAccount && state.dataAccountRoleArn && (
                  <Box margin={{ top: 'l' }}>
                    <Box variant="h3" margin={{ bottom: 's' }}>Separate Data Account</Box>
                    <ColumnLayout columns={2} variant="text-grid">
                      <SpaceBetween size="xs">
                        <div>
                          <Box variant="awsui-key-label">Data Account ID</Box>
                          <Box>{state.dataAccountId}</Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Data Account Role ARN</Box>
                          <Box fontSize="body-s">
                            <code>{state.dataAccountRoleArn}</code>
                          </Box>
                        </div>
                      </SpaceBetween>
                      <SpaceBetween size="xs">
                        <div>
                          <Box variant="awsui-key-label">Data S3 Bucket</Box>
                          <Box>{state.dataS3Bucket}</Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Data S3 Prefix</Box>
                          <Box>{state.dataS3Prefix || '/'}</Box>
                        </div>
                      </SpaceBetween>
                    </ColumnLayout>
                  </Box>
                )}
              </Container>

              <Container header={<Header variant="h2">What would you like to do next?</Header>}>
                <SpaceBetween size="m">
                  <Box>Select the workflows you want to start after creating this use case:</Box>

                  <FormField stretch>
                    <SpaceBetween size="s">
                      <Box>
                        <input
                          type="checkbox"
                          checked={state.nextSteps.labelData}
                          onChange={(e) =>
                            updateState({
                              nextSteps: { ...state.nextSteps, labelData: e.target.checked },
                            })
                          }
                        />{' '}
                        <strong>Label Training Data</strong>
                        <Box variant="small" color="text-body-secondary">
                          Create a Ground Truth labeling job to annotate your images
                        </Box>
                      </Box>

                      <Box>
                        <input
                          type="checkbox"
                          checked={state.nextSteps.trainModel}
                          onChange={(e) =>
                            updateState({
                              nextSteps: { ...state.nextSteps, trainModel: e.target.checked },
                            })
                          }
                        />{' '}
                        <strong>Train a Model</strong>
                        <Box variant="small" color="text-body-secondary">
                          Start a SageMaker training job with your labeled data
                        </Box>
                      </Box>

                      <Box>
                        <input
                          type="checkbox"
                          checked={state.nextSteps.setupDevices}
                          onChange={(e) =>
                            updateState({
                              nextSteps: { ...state.nextSteps, setupDevices: e.target.checked },
                            })
                          }
                        />{' '}
                        <strong>Set Up Edge Devices</strong>
                        <Box variant="small" color="text-body-secondary">
                          Register and configure IoT devices for this use case
                        </Box>
                      </Box>
                    </SpaceBetween>
                  </FormField>

                  <Alert type="info">
                    You can always access these workflows later from the use case detail page.
                  </Alert>
                </SpaceBetween>
              </Container>
            </SpaceBetween>
          ),
        },
      ]}
    />
  );
}
