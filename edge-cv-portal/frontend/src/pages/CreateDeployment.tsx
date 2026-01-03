import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  Form,
  FormField,
  Input,
  Select,
  SelectProps,
  SpaceBetween,
  Button,
  Alert,
  Multiselect,
  MultiselectProps,
  RadioGroup,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { apiService } from '../services/api';

export default function CreateDeployment() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  
  const [selectedComponent, setSelectedComponent] = useState<SelectProps.Option | null>(null);
  const [targetDevices, setTargetDevices] = useState<readonly MultiselectProps.Option[]>([]);
  const [rolloutStrategy, setRolloutStrategy] = useState('all-at-once');
  const [canarySize, setCanarySize] = useState('1');
  const [failureThreshold, setFailureThreshold] = useState('20');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');
  
  // Dynamic data
  const [componentOptions, setComponentOptions] = useState<SelectProps.Option[]>([]);
  const [deviceOptions, setDeviceOptions] = useState<MultiselectProps.Option[]>([]);
  const [loading, setLoading] = useState(true);

  const currentUseCaseId = searchParams.get('usecase_id');
  const preSelectedComponentArn = searchParams.get('component_arn');

  useEffect(() => {
    if (currentUseCaseId) {
      loadData();
    }
  }, [currentUseCaseId]);

  useEffect(() => {
    // Pre-select component if provided in URL
    if (preSelectedComponentArn && componentOptions.length > 0) {
      const preSelectedOption = componentOptions.find(
        option => option.value === preSelectedComponentArn
      );
      if (preSelectedOption) {
        setSelectedComponent(preSelectedOption);
      }
    }
  }, [preSelectedComponentArn, componentOptions]);

  const loadData = async () => {
    try {
      setLoading(true);
      
      // Load components and devices in parallel
      const [componentsResponse, devicesResponse] = await Promise.all([
        apiService.listComponents({
          usecase_id: currentUseCaseId!,
          scope: 'PRIVATE', // Only show private components for deployment
        }),
        apiService.listDevices(currentUseCaseId!)
      ]);

      // Transform components to options
      const componentOpts: SelectProps.Option[] = componentsResponse.components
        .filter(component => component.status === 'DEPLOYABLE')
        .map(component => ({
          label: `${component.component_name} v${component.latest_version?.componentVersion || 'latest'}`,
          value: component.arn,
          description: component.description || 'No description available',
        }));

      // Transform devices to options
      const deviceOpts: MultiselectProps.Option[] = devicesResponse.devices
        .filter(device => device.status === 'online')
        .map(device => ({
          label: device.device_id,
          value: device.device_id,
          description: `${device.thing_name} - ${device.status}`,
        }));

      setComponentOptions(componentOpts);
      setDeviceOptions(deviceOpts);
    } catch (err) {
      console.error('Failed to load deployment data:', err);
      setError('Failed to load components and devices. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreating(true);
    setError('');

    try {
      if (!selectedComponent || !currentUseCaseId) {
        throw new Error('Missing required fields');
      }

      // Extract component version from ARN
      const componentVersion = selectedComponent.label?.match(/v([\d.]+)/)?.[1] || 'latest';

      const deploymentData = {
        usecase_id: currentUseCaseId,
        component_arn: selectedComponent.value as string,
        component_version: componentVersion,
        target_devices: targetDevices.map((d) => d.value as string),
        rollout_strategy: rolloutStrategy as 'all-at-once' | 'canary' | 'percentage',
        rollout_config: {
          canarySize: rolloutStrategy === 'canary' ? parseInt(canarySize) : undefined,
          failureThreshold: parseInt(failureThreshold),
        },
      };

      const response = await apiService.createDeploymentFromComponent(deploymentData);
      
      navigate(`/deployments/${response.deployment_id}?usecase_id=${currentUseCaseId}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create deployment. Please try again.');
      console.error('Failed to create deployment:', err);
    } finally {
      setCreating(false);
    }
  };

  return (
    <form onSubmit={handleSubmit}>
      <Form
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => navigate('/deployments')}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={creating}
              disabled={!selectedComponent || targetDevices.length === 0}
            >
              Create Deployment
            </Button>
          </SpaceBetween>
        }
        errorText={error}
      >
        <Container
          header={
            <Header variant="h1" description="Deploy a component to edge devices">
              Create Deployment
            </Header>
          }
        >
          <SpaceBetween size="l">
            {error && <Alert type="error">{error}</Alert>}

            <FormField
              label="Component"
              description="Select the Greengrass component to deploy"
              constraintText="Required"
            >
              <Select
                selectedOption={selectedComponent}
                onChange={({ detail }) => setSelectedComponent(detail.selectedOption)}
                options={componentOptions}
                placeholder={loading ? "Loading components..." : "Select a component"}
                disabled={loading}
                loadingText="Loading components..."
                statusType={loading ? "loading" : "finished"}
              />
            </FormField>

            <FormField
              label="Target Devices"
              description="Select the devices to deploy to (only online devices are shown)"
              constraintText="Required - Select at least one device"
            >
              <Multiselect
                selectedOptions={targetDevices}
                onChange={({ detail }) => setTargetDevices(detail.selectedOptions)}
                options={deviceOptions}
                placeholder={loading ? "Loading devices..." : "Select devices"}
                filteringType="auto"
                disabled={loading}
                loadingText="Loading devices..."
                statusType={loading ? "loading" : "finished"}
              />
            </FormField>

            <FormField
              label="Rollout Strategy"
              description="Choose how the deployment should be rolled out"
            >
              <RadioGroup
                value={rolloutStrategy}
                onChange={({ detail }) => setRolloutStrategy(detail.value)}
                items={[
                  {
                    value: 'all-at-once',
                    label: 'All at once',
                    description: 'Deploy to all devices simultaneously',
                  },
                  {
                    value: 'canary',
                    label: 'Canary',
                    description: 'Deploy to a subset of devices first, then roll out to the rest',
                  },
                ]}
              />
            </FormField>

            {rolloutStrategy === 'canary' && (
              <FormField
                label="Canary Size"
                description="Number of devices to deploy to in the canary phase"
              >
                <Input
                  type="number"
                  value={canarySize}
                  onChange={({ detail }) => setCanarySize(detail.value)}
                  inputMode="numeric"
                />
              </FormField>
            )}

            <FormField
              label="Failure Threshold"
              description="Maximum percentage of devices that can fail before stopping the deployment"
            >
              <Input
                type="number"
                value={failureThreshold}
                onChange={({ detail }) => setFailureThreshold(detail.value)}
                inputMode="numeric"
              />
            </FormField>
          </SpaceBetween>
        </Container>
      </Form>
    </form>
  );
}
