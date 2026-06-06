import { useState, useEffect } from 'react';
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
  Select,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { validateBucketName } from '../utils/s3Validation';
import S3BucketPicker from '../components/S3BucketPicker';

interface OnboardingState {
  // Step 1: Setup Type Selection
  setupType: 'single-account' | 'multi-account';

  // Step 2: Basic Info
  useCaseName: string;
  description: string;
  region: string;  // AWS region where devices are located

  // Step 3: AWS Account Setup
  accountId: string;
  roleArn: string;
  sagemakerExecutionRoleArn: string;
  externalId: string;
  s3Bucket: string;
  s3Prefix: string;

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
}

export default function UseCaseOnboarding() {
  const navigate = useNavigate();
  const [activeStepIndex, setActiveStepIndex] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [registeredDataAccounts, setRegisteredDataAccounts] = useState<Array<{
    data_account_id: string;
    name: string;
    role_arn: string;
    external_id: string;
    region: string;
  }>>([]);
  const [selectedDataAccount, setSelectedDataAccount] = useState<string | null>(null);

  const [state, setState] = useState<OnboardingState>({
    setupType: 'multi-account',
    useCaseName: '',
    description: '',
    region: 'us-east-1',  // Default region
    accountId: '',
    roleArn: '',
    sagemakerExecutionRoleArn: '',
    externalId: '',
    s3Bucket: '',
    s3Prefix: '',
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
  });

  const updateState = (updates: Partial<OnboardingState>) => {
    setState((prev) => ({ ...prev, ...updates }));
  };

  // Load registered data accounts from Settings
  useEffect(() => {
    const loadDataAccounts = async () => {
      try {
        const result = await apiService.listDataAccounts();
        setRegisteredDataAccounts(result.data_accounts || []);
      } catch (err) {
        console.error('Failed to load data accounts:', err);
      }
    };
    loadDataAccounts();
  }, []);

  const handleVerifyRole = async () => {
    try {
      setError(null);
      const result = await apiService.verifyRole(state.roleArn, state.externalId);
      if (result.status === 'success') {
        updateState({ roleVerified: true });
      } else {
        setError(`Role verification failed: ${result.error}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to verify role. Please check the ARN and External ID.');
    }
  };

  const handleSubmit = async () => {
    try {
      setSubmitting(true);
      setError(null);

      // Create the use case
      const useCaseData: Record<string, unknown> = {
        name: state.useCaseName,
        description: state.description,  // Persist the description entered in Basic Information
        region: state.region,  // Include region
      };

      // For single-account setup: only name and s3_bucket required
      // Backend will auto-detect account_id and roles
      if (state.setupType === 'single-account') {
        useCaseData.s3_bucket = state.s3Bucket;
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
          if (state.s3Prefix) useCaseData.s3_prefix = state.s3Prefix;
        } else {
          useCaseData.data_account_id = state.dataAccountId;
          useCaseData.data_account_role_arn = state.dataAccountRoleArn;
          useCaseData.data_account_external_id = state.dataAccountExternalId;
          useCaseData.data_s3_bucket = state.dataS3Bucket;
          useCaseData.s3_bucket = state.s3Bucket;
          if (state.s3Prefix) useCaseData.s3_prefix = state.s3Prefix;
        }
      }

      const result = await apiService.createUseCase(useCaseData) as any;

      // Check for provisioning warnings
      const warnings: string[] = [];
      if (result.shared_components?.status === 'failed') {
        warnings.push(`Shared components provisioning failed: ${result.shared_components.error || 'Unknown error'}. You can retry from the UseCases page.`);
      }
      if (result.data_bucket_policy?.status === 'failed') {
        warnings.push(`Data bucket policy update failed: ${result.data_bucket_policy.error}`);
      }

      if (warnings.length > 0) {
        setError(`Use case created with warnings:\n${warnings.join('\n')}`);
        // Still navigate after a delay so user can read the warning
        setTimeout(() => navigate('/usecases'), 5000);
      } else {
        // Navigate to use cases list after creating
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

                {state.setupType === 'multi-account' && (
                <FormField
                  label="UseCase Account Region"
                  description="Region where the UseCase Account's SageMaker, IoT, and Greengrass resources are deployed"
                  stretch
                >
                  <Select
                    selectedOption={
                      state.region
                        ? { label: state.region, value: state.region }
                        : null
                    }
                    onChange={({ detail }) =>
                      updateState({ region: detail.selectedOption.value || '' })
                    }
                    options={[
                      { label: 'us-east-1 — N. Virginia', value: 'us-east-1' },
                      { label: 'us-east-2 — Ohio', value: 'us-east-2' },
                      { label: 'us-west-1 — N. California', value: 'us-west-1' },
                      { label: 'us-west-2 — Oregon', value: 'us-west-2' },
                    ]}
                    placeholder="Select a region"
                  />
                </FormField>
                )}

                {state.setupType === 'single-account' && (
                  <>
                    <Box variant="h3">S3 Storage Configuration</Box>
                    <FormField
                      label="S3 Bucket"
                      description="Browse and select a bucket in this account, or type a bucket name below. Used for storing training datasets, models, and labeling results."
                      errorText={validateBucketName(state.s3Bucket)}
                      stretch
                    >
                      <SpaceBetween size="s">
                        <S3BucketPicker
                          selectedBucket={state.s3Bucket}
                          onSelect={(bucketName) => updateState({ s3Bucket: bucketName })}
                          region={state.region}
                        />
                        <Input
                          value={state.s3Bucket}
                          onChange={({ detail }) => updateState({ s3Bucket: detail.value })}
                          placeholder="e.g., my-training-data-bucket"
                        />
                      </SpaceBetween>
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
                          onChange={() => updateState({ dataAccountSameAsUseCase: false })}
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

              {/* Data Account fields - show when Separate Data Account is selected */}
              {!state.dataAccountSameAsUseCase && (
                <Container header={<Header variant="h2">Data Account Details</Header>}>
                  <SpaceBetween size="m">
                    {registeredDataAccounts.length > 0 ? (
                      <>
                        <FormField
                          label="Select Data Account"
                          description="Choose a data account registered in Settings, or enter details manually"
                          stretch
                        >
                          <Select
                            selectedOption={
                              selectedDataAccount
                                ? {
                                    label: `${registeredDataAccounts.find(a => a.data_account_id === selectedDataAccount)?.name || ''} (${selectedDataAccount})`,
                                    value: selectedDataAccount,
                                  }
                                : null
                            }
                            onChange={({ detail }) => {
                              const accountId = detail.selectedOption.value || '';
                              setSelectedDataAccount(accountId);
                              if (accountId === '__manual__') {
                                updateState({
                                  dataAccountId: '',
                                  dataAccountRoleArn: '',
                                  dataAccountExternalId: '',
                                });
                              } else {
                                const account = registeredDataAccounts.find(a => a.data_account_id === accountId);
                                if (account) {
                                  updateState({
                                    dataAccountId: account.data_account_id,
                                    dataAccountRoleArn: account.role_arn,
                                    dataAccountExternalId: account.external_id,
                                  });
                                }
                              }
                            }}
                            options={[
                              ...registeredDataAccounts.map(account => ({
                                label: `${account.name} (${account.data_account_id})`,
                                value: account.data_account_id,
                                description: account.role_arn,
                              })),
                              { label: 'Enter manually...', value: '__manual__' },
                            ]}
                            placeholder="Select a registered data account"
                          />
                        </FormField>

                        {selectedDataAccount && selectedDataAccount !== '__manual__' && (
                          <Alert type="success">
                            Account ID, Role ARN, and External ID auto-filled from registered data account.
                          </Alert>
                        )}
                      </>
                    ) : (
                      <Alert type="info">
                        No data accounts registered yet. Enter details manually below, or register data accounts in <strong>Settings → Data Accounts</strong> first.
                      </Alert>
                    )}

                    {(registeredDataAccounts.length === 0 || selectedDataAccount === '__manual__') && (
                      <>
                        <FormField
                          label="Data Account ID"
                          description="AWS Account ID where your training data is stored"
                          stretch
                        >
                          <Input
                            value={state.dataAccountId}
                            onChange={({ detail }) => updateState({ dataAccountId: detail.value })}
                            placeholder="123456789012"
                          />
                        </FormField>

                        <FormField
                          label="Data Account Role ARN"
                          description="Role ARN from the Data Account deployment (DDAPortalDataAccessRole)"
                          stretch
                        >
                          <Input
                            value={state.dataAccountRoleArn}
                            onChange={({ detail }) => updateState({ dataAccountRoleArn: detail.value })}
                            placeholder="arn:aws:iam::123456789012:role/DDAPortalDataAccessRole"
                          />
                        </FormField>

                        <FormField
                          label="Data Account External ID"
                          description="External ID configured in the Data Account role trust policy"
                          stretch
                        >
                          <Input
                            value={state.dataAccountExternalId}
                            onChange={({ detail }) => updateState({ dataAccountExternalId: detail.value })}
                            placeholder="a1b2c3d4-e5f6-7890-abcd-ef1234567890"
                            type="password"
                          />
                        </FormField>
                      </>
                    )}

                    <FormField
                      label="Data S3 Bucket"
                      description="S3 bucket in the Data Account containing your training data"
                      errorText={validateBucketName(state.dataS3Bucket)}
                      stretch
                    >
                      <Input
                        value={state.dataS3Bucket}
                        onChange={({ detail }) => updateState({ dataS3Bucket: detail.value })}
                        placeholder="e.g., my-training-data-bucket"
                      />
                    </FormField>

                    <FormField
                      label="UseCase S3 Bucket"
                      description="S3 bucket in the UseCase Account for SageMaker outputs (models, manifests, checkpoints)"
                      errorText={validateBucketName(state.s3Bucket)}
                      stretch
                    >
                      <Input
                        value={state.s3Bucket}
                        onChange={({ detail }) => updateState({ s3Bucket: detail.value })}
                        placeholder="e.g., my-usecase-output-bucket"
                      />
                    </FormField>
                  </SpaceBetween>
                </Container>
              )}

              {/* S3 bucket for same-account data */}
              {state.dataAccountSameAsUseCase && (
                <Container header={<Header variant="h2">S3 Storage</Header>}>
                  <FormField
                    label="S3 Bucket"
                    description="S3 bucket in the UseCase Account for all data and models"
                    errorText={validateBucketName(state.s3Bucket)}
                    stretch
                  >
                    <Input
                      value={state.s3Bucket}
                      onChange={({ detail }) => updateState({ s3Bucket: detail.value })}
                      placeholder="e.g., my-training-data-bucket"
                    />
                  </FormField>
                </Container>
              )}

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
                              const lines = content.split('\n');
                              const config: Record<string, string> = {};
                              lines.forEach(line => {
                                if (line.startsWith('#') || !line.trim()) return;
                                const match = line.match(/^([A-Z_]+)=(.+)$/);
                                if (match) {
                                  config[match[1].trim()] = match[2].trim();
                                }
                              });
                              updateState({
                                accountId: config['USECASE_ACCOUNT_ID'] || '',
                                roleArn: config['ROLE_ARN'] || '',
                                sagemakerExecutionRoleArn: config['SAGEMAKER_ROLE_ARN'] || '',
                                externalId: config['EXTERNAL_ID'] || '',
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
                          <code>s3://{state.s3Bucket}</code>
                          <Box variant="small" color="text-body-secondary">UseCase Account ({state.accountId})</Box>
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
