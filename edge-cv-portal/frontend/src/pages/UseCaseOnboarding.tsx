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
  SelectProps,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface DataAccount {
  data_account_id: string;
  name: string;
  description?: string;
  role_arn: string;
  external_id: string;
  region: string;
  status: string;
  created_at: number;
  created_by: string;
  connection_test?: {
    status: string;
    message: string;
  };
}

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

  // Data Account Configuration (always required)
  // Can be same as UseCase Account or separate
  dataAccountSameAsUseCase: boolean;
  dataAccountId: string;
  dataAccountRoleArn: string;
  dataAccountExternalId: string;
  dataS3Bucket: string;
  dataS3Prefix: string;
  dataRoleVerified: boolean;

  // SageMaker Asset Storage Option
  // Can store in UseCase Account bucket or Data Account bucket
  sagemakerAssetsInDataAccount: boolean;

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
  
  // Data Accounts dropdown
  const [dataAccounts, setDataAccounts] = useState<DataAccount[]>([]);
  const [selectedDataAccount, setSelectedDataAccount] = useState<SelectProps.Option | null>(null);
  const [loadingDataAccounts, setLoadingDataAccounts] = useState(false);

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
    // Data Account defaults to separate (most common enterprise setup)
    dataAccountSameAsUseCase: false,
    dataAccountId: '',
    dataAccountRoleArn: '',
    dataAccountExternalId: '',
    dataS3Bucket: '',
    dataS3Prefix: 'datasets/',
    dataRoleVerified: false,
    // SageMaker assets default to Data Account bucket (simpler setup)
    sagemakerAssetsInDataAccount: true,
    nextSteps: {
      labelData: false,
      trainModel: false,
      setupDevices: false,
    },
  });

  const updateState = (updates: Partial<OnboardingState>) => {
    setState((prev) => ({ ...prev, ...updates }));
  };

  // Load registered Data Accounts
  useEffect(() => {
    const loadDataAccounts = async () => {
      setLoadingDataAccounts(true);
      try {
        const response = await apiService.listDataAccounts();
        setDataAccounts(response.data_accounts || []);
      } catch (err) {
        console.error('Failed to load Data Accounts:', err);
        // Non-critical error - user can still enter manually
      } finally {
        setLoadingDataAccounts(false);
      }
    };
    loadDataAccounts();
  }, []);

  // Handle Data Account selection from dropdown
  const handleDataAccountSelect = (option: SelectProps.Option | null) => {
    setSelectedDataAccount(option);
    if (option && option.value) {
      const account = dataAccounts.find(da => da.data_account_id === option.value);
      if (account) {
        updateState({
          dataAccountId: account.data_account_id,
          dataAccountRoleArn: account.role_arn,
          dataAccountExternalId: account.external_id,
        });
      }
    }
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
        cross_account_role_arn: state.roleArn,
        sagemaker_execution_role_arn: state.sagemakerExecutionRoleArn,
        external_id: state.externalId,
        cost_center: state.costCenter,
      };

      // Always include Data Account configuration
      // If same as UseCase Account, use UseCase Account values
      if (state.dataAccountSameAsUseCase) {
        useCaseData.data_account_id = state.accountId;
        useCaseData.data_account_role_arn = state.roleArn;
        useCaseData.data_account_external_id = state.externalId;
        useCaseData.data_s3_bucket = state.s3Bucket;
        useCaseData.data_s3_prefix = state.s3Prefix;
        // When same account, s3_bucket is always the same
        useCaseData.s3_bucket = state.s3Bucket;
        useCaseData.s3_prefix = state.s3Prefix;
      } else {
        useCaseData.data_account_id = state.dataAccountId;
        useCaseData.data_account_role_arn = state.dataAccountRoleArn;
        useCaseData.data_account_external_id = state.dataAccountExternalId;
        useCaseData.data_s3_bucket = state.dataS3Bucket;
        useCaseData.data_s3_prefix = state.dataS3Prefix;
        
        // SageMaker assets bucket - user can choose where to store
        if (state.sagemakerAssetsInDataAccount) {
          // Store SageMaker outputs in Data Account bucket (simpler)
          useCaseData.s3_bucket = state.dataS3Bucket;
          useCaseData.s3_prefix = 'sagemaker-outputs/';
        } else {
          // Store SageMaker outputs in separate UseCase Account bucket
          useCaseData.s3_bucket = state.s3Bucket;
          useCaseData.s3_prefix = state.s3Prefix;
        }
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

              {/* Separate Data Account flow */}
              {!state.dataAccountSameAsUseCase && (
                <Container header={<Header variant="h2">Data Account Setup</Header>}>
                  <SpaceBetween size="m">
                    <Alert type="info">
                      Deploy the Data Account role first: <code>./deploy-account-role.sh</code> → option 2
                    </Alert>

                    {/* Option 1: Select from registered Data Accounts */}
                    {dataAccounts.length > 0 && (
                      <>
                        <FormField
                          label="Select Registered Data Account"
                          description="Choose from Data Accounts registered in Settings"
                          stretch
                        >
                          <Select
                            selectedOption={selectedDataAccount}
                            onChange={({ detail }) => handleDataAccountSelect(detail.selectedOption)}
                            options={dataAccounts.map(da => ({
                              label: da.name,
                              value: da.data_account_id,
                              description: `${da.data_account_id} (${da.region})`,
                              tags: [da.data_account_id],
                            }))}
                            placeholder="Select a Data Account"
                            empty="No Data Accounts registered"
                            loadingText="Loading Data Accounts..."
                            statusType={loadingDataAccounts ? 'loading' : 'finished'}
                          />
                        </FormField>

                        <Box textAlign="center" color="text-body-secondary">
                          <Box variant="small">or enter manually below</Box>
                        </Box>
                      </>
                    )}

                    {/* Option 2: Manual entry or upload config file */}
                    <FormField
                      label="Upload Configuration File"
                      description="Upload data-account-config.txt to auto-fill"
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
                                const match = line.match(/^([^:]+):\s*(.+)$/);
                                if (match) {
                                  config[match[1].trim()] = match[2].trim();
                                }
                              });
                              updateState({
                                dataAccountId: config['Data Account ID'] || '',
                                dataAccountRoleArn: config['Portal Access Role ARN'] || '',
                                dataAccountExternalId: config['External ID'] || '',
                              });
                              // Clear dropdown selection when uploading file
                              setSelectedDataAccount(null);
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

                    <FormField label="Data Account ID" stretch>
                      <Input
                        value={state.dataAccountId}
                        onChange={({ detail }) => {
                          updateState({ dataAccountId: detail.value });
                          setSelectedDataAccount(null); // Clear dropdown when manually editing
                        }}
                        placeholder="987654321098"
                      />
                    </FormField>

                    <FormField label="Data Account Role ARN" stretch>
                      <Input
                        value={state.dataAccountRoleArn}
                        onChange={({ detail }) => {
                          updateState({ dataAccountRoleArn: detail.value });
                          setSelectedDataAccount(null);
                        }}
                        placeholder="arn:aws:iam::987654321098:role/DDAPortalDataAccessRole"
                      />
                    </FormField>

                    <FormField label="External ID" stretch>
                      <Input
                        value={state.dataAccountExternalId}
                        onChange={({ detail }) => {
                          updateState({ dataAccountExternalId: detail.value });
                          setSelectedDataAccount(null);
                        }}
                        placeholder="a1b2c3d4-e5f6-7890-abcd-ef1234567890"
                        type="password"
                      />
                    </FormField>

                    <FormField label="Data Bucket Name" description="Bucket containing your training data" stretch>
                      <Input
                        value={state.dataS3Bucket}
                        onChange={({ detail }) => {
                          updateState({ dataS3Bucket: detail.value });
                          setSelectedDataAccount(null);
                        }}
                        placeholder="my-data-lake-bucket"
                      />
                    </FormField>

                    <FormField label="S3 Prefix (Optional)" stretch>
                      <Input
                        value={state.dataS3Prefix}
                        onChange={({ detail }) => updateState({ dataS3Prefix: detail.value })}
                        placeholder="datasets/"
                      />
                    </FormField>

                    <FormField label="Verify Access" stretch>
                      <SpaceBetween direction="horizontal" size="xs">
                        <Button
                          onClick={handleVerifyDataRole}
                          disabled={!state.dataAccountId || !state.dataAccountRoleArn || !state.dataAccountExternalId}
                        >
                          Verify Role
                        </Button>
                        {state.dataRoleVerified && (
                          <StatusIndicator type="success">Verified</StatusIndicator>
                        )}
                      </SpaceBetween>
                    </FormField>
                  </SpaceBetween>
                </Container>
              )}

              {/* SageMaker output location - only for separate Data Account */}
              {!state.dataAccountSameAsUseCase && (
                <Container header={<Header variant="h2">SageMaker Output Location</Header>}>
                  <SpaceBetween size="m">
                    <Box>Where should trained models and labeling results be stored?</Box>
                    
                    <FormField stretch>
                      <SpaceBetween size="s">
                        <Box>
                          <input
                            type="radio"
                            name="sagemakerAssetChoice"
                            checked={state.sagemakerAssetsInDataAccount}
                            onChange={() => updateState({ sagemakerAssetsInDataAccount: true })}
                          />{' '}
                          <strong>Same Data Account bucket</strong> (recommended)
                          <Box variant="small" color="text-body-secondary">
                            Keep everything together in <code>{state.dataS3Bucket || 'data-bucket'}</code>
                          </Box>
                        </Box>

                        <Box>
                          <input
                            type="radio"
                            name="sagemakerAssetChoice"
                            checked={!state.sagemakerAssetsInDataAccount}
                            onChange={() => updateState({ sagemakerAssetsInDataAccount: false })}
                          />{' '}
                          <strong>Separate UseCase Account bucket</strong>
                          <Box variant="small" color="text-body-secondary">
                            Store outputs in a different bucket
                          </Box>
                        </Box>
                      </SpaceBetween>
                    </FormField>

                    {!state.sagemakerAssetsInDataAccount && (
                      <FormField label="Output Bucket Name" description="S3 bucket in UseCase Account" stretch>
                        <Input
                          value={state.s3Bucket}
                          onChange={({ detail }) => updateState({ s3Bucket: detail.value })}
                          placeholder="my-sagemaker-outputs"
                        />
                      </FormField>
                    )}
                  </SpaceBetween>
                </Container>
              )}

              {/* Same account flow - simple bucket setup */}
              {state.dataAccountSameAsUseCase && (
                <Container header={<Header variant="h2">S3 Bucket Setup</Header>}>
                  <SpaceBetween size="m">
                    <Box>Create an S3 bucket in your UseCase Account for all data and outputs.</Box>

                    <Box variant="code" padding="s" fontSize="body-s">
                      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                        {s3SetupCommands}
                      </pre>
                    </Box>
                    <Button
                      iconName="copy"
                      onClick={() => navigator.clipboard.writeText(s3SetupCommands)}
                    >
                      Copy Commands
                    </Button>

                    <FormField label="S3 Bucket Name" stretch>
                      <Input
                        value={state.s3Bucket}
                        onChange={({ detail }) => updateState({ s3Bucket: detail.value })}
                        placeholder="my-edge-cv-data"
                      />
                    </FormField>

                    <FormField label="S3 Prefix (Optional)" stretch>
                      <Input
                        value={state.s3Prefix}
                        onChange={({ detail }) => updateState({ s3Prefix: detail.value })}
                        placeholder="datasets/"
                      />
                    </FormField>

                    <FormField label="Verify Access" stretch>
                      <SpaceBetween direction="horizontal" size="xs">
                        <Button onClick={handleVerifyS3} disabled={!state.s3Bucket}>
                          Verify
                        </Button>
                        {state.s3Verified && (
                          <StatusIndicator type="success">Verified</StatusIndicator>
                        )}
                      </SpaceBetween>
                    </FormField>
                  </SpaceBetween>
                </Container>
              )}
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

                {/* Storage Configuration Summary */}
                <Box margin={{ top: 'l' }}>
                  <Box variant="h3" margin={{ bottom: 's' }}>Storage Configuration</Box>
                  {state.dataAccountSameAsUseCase ? (
                    <Box>
                      <Box variant="awsui-key-label">All data in one bucket</Box>
                      <Box><code>s3://{state.s3Bucket}/{state.s3Prefix || ''}</code></Box>
                    </Box>
                  ) : (
                    <SpaceBetween size="s">
                      <Box>
                        <Box variant="awsui-key-label">Training Data (Data Account)</Box>
                        <Box><code>s3://{state.dataS3Bucket}/{state.dataS3Prefix || ''}</code></Box>
                      </Box>
                      <Box>
                        <Box variant="awsui-key-label">SageMaker Outputs</Box>
                        <Box>
                          {state.sagemakerAssetsInDataAccount ? (
                            <code>s3://{state.dataS3Bucket}/sagemaker-outputs/</code>
                          ) : (
                            state.s3Bucket ? (
                              <code>s3://{state.s3Bucket}/</code>
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
