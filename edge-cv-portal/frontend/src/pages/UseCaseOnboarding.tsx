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
  StatusIndicator,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface OnboardingState {
  // Step 1: Setup Type Selection
  setupType: 'single-account' | 'multi-account';

  // Step 2: Basic Info
  useCaseName: string;
  description: string;
  costCenter: string;

  // Step 3: AWS Account Setup
  accountId: string;
  roleArn: string;
  sagemakerExecutionRoleArn: string;
  externalId: string;
  s3Bucket: string;

  // Step 4: Role Deployment Status
  roleDeployed: boolean;
  roleVerified: boolean;

  // Step 5: S3 Setup Status
  s3Created: boolean;
  s3Verified: boolean;

  // Data Account Configuration (always required)
  // Can be same as UseCase Account or separate
  dataAccountSameAsUseCase: boolean;
  dataAccountId: string;
  dataAccountRoleArn: string;
  dataAccountExternalId: string;
  dataS3Bucket: string;
  dataRoleVerified: boolean;

  // SageMaker Asset Storage Option
  // Can store in UseCase Account bucket or Data Account bucket
  sagemakerAssetsInDataAccount: boolean;
}

export default function UseCaseOnboarding() {
  const navigate = useNavigate();
  const [activeStepIndex, setActiveStepIndex] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [state, setState] = useState<OnboardingState>({
    setupType: 'multi-account',
    useCaseName: '',
    description: '',
    costCenter: '',
    accountId: '',
    roleArn: '',
    sagemakerExecutionRoleArn: '',
    externalId: '',
    s3Bucket: '',
    roleDeployed: false,
    roleVerified: false,
    s3Created: false,
    s3Verified: false,
    // Data Account defaults to separate (most common enterprise setup)
    dataAccountSameAsUseCase: false,
    dataAccountId: '',
    dataAccountRoleArn: '',
    dataAccountExternalId: '',
    dataS3Bucket: '',
    dataRoleVerified: false,
    // SageMaker assets default to Data Account bucket (simpler setup)
    sagemakerAssetsInDataAccount: true,
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

  const handleSubmit = async () => {
    try {
      setSubmitting(true);
      setError(null);

      // Create the use case
      const useCaseData: Record<string, unknown> = {
        name: state.useCaseName,
        cost_center: state.costCenter,
      };

      // For single-account setup: only name and s3_bucket required
      // Backend will auto-detect account_id and roles
      if (state.setupType === 'single-account') {
        useCaseData.s3_bucket = state.s3Bucket;
        // Don't set s3_prefix - backend will use empty string by default
        // Don't set account_id, cross_account_role_arn, sagemaker_execution_role_arn
        // Backend will auto-detect these
      } else {
        // Multi-account setup: all fields required
        useCaseData.account_id = state.accountId;
        useCaseData.cross_account_role_arn = state.roleArn;
        useCaseData.sagemaker_execution_role_arn = state.sagemakerExecutionRoleArn;
        useCaseData.external_id = state.externalId;

        // Data Account configuration
        if (state.dataAccountSameAsUseCase) {
          useCaseData.data_account_id = state.accountId;
          useCaseData.data_s3_bucket = state.s3Bucket;
          useCaseData.s3_bucket = state.s3Bucket;
        } else {
          useCaseData.data_account_id = state.dataAccountId;
          useCaseData.data_account_role_arn = state.dataAccountRoleArn;
          useCaseData.data_account_external_id = state.dataAccountExternalId;
          useCaseData.data_s3_bucket = state.dataS3Bucket;
          
          if (state.sagemakerAssetsInDataAccount) {
            useCaseData.s3_bucket = state.dataS3Bucket;
          } else {
            useCaseData.s3_bucket = state.s3Bucket;
          }
        }
      }

      await apiService.createUseCase(useCaseData);

      // Always navigate to labeling workflow after creating use case
      navigate('/labeling/create');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create use case');
    } finally {
      setSubmitting(false);
    }
  };

  const cdkDeployCommand = `cd edge-cv-portal
./deploy-account-role.sh`;

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
          title: 'Setup Type',
          description: 'Choose your deployment architecture',
          content: (
            <Container header={<Header variant="h2">Select Setup Type</Header>}>
              <SpaceBetween size="l">
                <Box>
                  Choose how you want to set up the DDA Portal. You can change this later if needed.
                </Box>

                <FormField stretch>
                  <SpaceBetween size="m">
                    <Box>
                      <input
                        type="radio"
                        name="setupType"
                        checked={state.setupType === 'single-account'}
                        onChange={() => updateState({ setupType: 'single-account' })}
                      />{' '}
                      <strong>Single Account Setup</strong>
                      <Box variant="small" color="text-body-secondary" margin={{ top: 'xs' }}>
                        Everything runs in one AWS account. Simpler setup, good for small teams or proof-of-concept.
                      </Box>
                    </Box>

                    <Box>
                      <input
                        type="radio"
                        name="setupType"
                        checked={state.setupType === 'multi-account'}
                        onChange={() => updateState({ setupType: 'multi-account' })}
                      />{' '}
                      <strong>Multi-Account Setup</strong>
                      <Box variant="small" color="text-body-secondary" margin={{ top: 'xs' }}>
                        Portal runs in one account, data and training in separate accounts. Recommended for enterprise.
                      </Box>
                    </Box>
                  </SpaceBetween>
                </FormField>

                <Alert type="info">
                  {state.setupType === 'single-account'
                    ? 'Single-account setup requires only your S3 bucket name. The portal will auto-detect your AWS account and use default roles.'
                    : 'Multi-account setup requires deploying a cross-account IAM role and configuring data account access.'}
                </Alert>
              </SpaceBetween>
            </Container>
          ),
        },
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

                {state.setupType === 'single-account' && (
                  <>
                    <Box variant="h3">S3 Storage Configuration</Box>
                    <FormField
                      label="S3 Bucket"
                      description="S3 bucket for storing training datasets, models, and labeling results"
                      stretch
                    >
                      <Input
                        value={state.s3Bucket}
                        onChange={({ detail }) => updateState({ s3Bucket: detail.value })}
                        placeholder="e.g., dda-cookie-dataset"
                      />
                    </FormField>
                  </>
                )}
              </SpaceBetween>
            </Container>
          ),
        },
        ...(state.setupType === 'multi-account' ? [{
          title: 'Configure S3 Storage',
          description: 'Set up S3 storage for data and models',
          content: (
            <SpaceBetween size="l">
              {/* Step 1: Ask where training data is */}
              <Container header={<Header variant="h2">Where is your training data?</Header>}>
                <SpaceBetween size="m">
                  <FormField stretch>
                    <SpaceBetween size="s">
                      <Box>
                        <input
                          type="radio"
                          name="dataAccountChoice"
                          checked={!state.dataAccountSameAsUseCase}
                          onChange={() => updateState({ dataAccountSameAsUseCase: false, sagemakerAssetsInDataAccount: true })}
                        />{' '}
                        <strong>Separate Data Account</strong> (recommended for enterprise)
                        <Box variant="small" color="text-body-secondary">
                          Training data is in a centralized data lake or different AWS account
                        </Box>
                      </Box>

                      <Box>
                        <input
                          type="radio"
                          name="dataAccountChoice"
                          checked={state.dataAccountSameAsUseCase}
                          onChange={() => updateState({ dataAccountSameAsUseCase: true })}
                        />{' '}
                        <strong>Same as UseCase Account</strong>
                        <Box variant="small" color="text-body-secondary">
                          Everything in one AWS account
                        </Box>
                      </Box>
                    </SpaceBetween>
                  </FormField>
                </SpaceBetween>
              </Container>

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
        }] : []),
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
                      <Box variant="awsui-key-label">UseCase Account ID</Box>
                      <Box>
                        {state.setupType === 'single-account' 
                          ? '(Same as Portal Account)' 
                          : state.accountId}
                      </Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Setup Type</Box>
                      <Box>{state.setupType === 'single-account' ? 'Single Account' : 'Multi-Account'}</Box>
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
                  </SpaceBetween>
                </ColumnLayout>

                {state.setupType === 'multi-account' && (
                  <Box margin={{ top: 'l' }}>
                    <Box variant="h3" margin={{ bottom: 's' }}>Cross-Account Access</Box>
                    <div>
                      <Box variant="awsui-key-label">Role ARN</Box>
                      <Box fontSize="body-s">
                        <code>{state.roleArn}</code>
                      </Box>
                    </div>
                  </Box>
                )}

                {/* Data Account Configuration - only show for multi-account setup */}
                {state.setupType === 'multi-account' && (
                  <Box margin={{ top: 'l' }}>
                    <Box variant="h3" margin={{ bottom: 's' }}>Data Account Configuration</Box>
                    {state.dataAccountSameAsUseCase ? (
                      <Box>
                        <Box variant="awsui-key-label">Data Account ID</Box>
                        <Box>{state.accountId} (same as UseCase Account)</Box>
                      </Box>
                    ) : (
                      <Box>
                        <Box variant="awsui-key-label">Data Account ID</Box>
                        <Box>{state.dataAccountId}</Box>
                      </Box>
                    )}
                  </Box>
                )}

                {/* Storage Configuration Summary */}
                <Box margin={{ top: 'l' }}>
                  <Box variant="h3" margin={{ bottom: 's' }}>Storage Configuration</Box>
                  {state.dataAccountSameAsUseCase ? (
                    <Box>
                      <Box variant="awsui-key-label">All data in one bucket</Box>
                      <Box><code>s3://{state.s3Bucket}</code></Box>
                    </Box>
                  ) : (
                    <SpaceBetween size="s">
                      <Box>
                        <Box variant="awsui-key-label">Training Data (Data Account)</Box>
                        <Box><code>s3://{state.dataS3Bucket}</code></Box>
                      </Box>
                      <Box>
                        <Box variant="awsui-key-label">SageMaker Outputs</Box>
                        <Box>
                          {state.sagemakerAssetsInDataAccount ? (
                            <code>s3://{state.dataS3Bucket}</code>
                          ) : (
                            state.s3Bucket ? (
                              <code>s3://{state.s3Bucket}</code>
                            ) : (
                              <StatusIndicator type="error">Not configured</StatusIndicator>
                            )
                          )}
                        </Box>
                      </Box>
                    </SpaceBetween>
                  )}
                </Box>
              </Container>

              <Container header={<Header variant="h2">Next Steps</Header>}>
                <SpaceBetween size="m">
                  <Box>
                    The next step is to create a labeling job to annotate your images. Click "Create Use Case" below to proceed, 
                    and you'll be directed to the labeling workflow where you can set up your Ground Truth labeling job.
                  </Box>
                </SpaceBetween>
              </Container>
            </SpaceBetween>
          ),
        },
      ]}
    />
  );
}
