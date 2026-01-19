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
  ExpandableSection,
  Toggle,
  Box,
  Table,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { apiService } from '../services/api';
import { UseCase } from '../types';

interface ComponentSelection {
  component_name: string;
  component_version: string;
  arn: string;
  scope: 'PRIVATE' | 'PUBLIC';
}

export default function CreateDeployment() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  
  // Use case selection
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);
  
  // Component selection
  const [selectedComponents, setSelectedComponents] = useState<ComponentSelection[]>([]);
  const [privateComponents, setPrivateComponents] = useState<SelectProps.Option[]>([]);
  const [publicComponents, setPublicComponents] = useState<SelectProps.Option[]>([]);
  const [componentToAdd, setComponentToAdd] = useState<SelectProps.Option | null>(null);
  const [componentScope, setComponentScope] = useState<'PRIVATE' | 'PUBLIC'>('PRIVATE');
  
  // Target selection
  const [targetType, setTargetType] = useState<'devices' | 'group'>('devices');
  const [targetDevices, setTargetDevices] = useState<readonly MultiselectProps.Option[]>([]);
  const [targetThingGroup, setTargetThingGroup] = useState('');
  const [deviceOptions, setDeviceOptions] = useState<MultiselectProps.Option[]>([]);
  
  // Deployment config
  const [deploymentName, setDeploymentName] = useState('');
  const [autoRollback, setAutoRollback] = useState(true);
  const [timeoutSeconds, setTimeoutSeconds] = useState('60');
  
  // State
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');
  const [successInfo, setSuccessInfo] = useState<{
    deployment_id: string;
    auto_included: Array<{component_name: string; component_version: string; reason: string}>;
  } | null>(null);
  const [loading, setLoading] = useState(true);

  const preSelectedComponentArn = searchParams.get('component_arn');
  const urlUseCaseId = searchParams.get('usecase_id');

  // Load use cases on mount
  useEffect(() => {
    loadUseCases();
  }, []);

  // Load data when use case changes
  useEffect(() => {
    if (selectedUseCase?.value) {
      loadComponentsAndDevices();
    }
  }, [selectedUseCase]);

  const loadUseCases = async () => {
    try {
      const response = await apiService.listUseCases();
      const useCaseList = response.usecases || [];
      setUseCases(useCaseList);
      
      // Pre-select from URL or first use case
      if (urlUseCaseId) {
        const preSelected = useCaseList.find((uc: UseCase) => uc.usecase_id === urlUseCaseId);
        if (preSelected) {
          setSelectedUseCase({ label: preSelected.name, value: preSelected.usecase_id });
        }
      } else if (useCaseList.length > 0) {
        setSelectedUseCase({ label: useCaseList[0].name, value: useCaseList[0].usecase_id });
      }
    } catch (err) {
      console.error('Failed to load use cases:', err);
      setError('Failed to load use cases');
    }
  };

  const loadComponentsAndDevices = async () => {
    if (!selectedUseCase?.value) return;
    
    try {
      setLoading(true);
      
      // Load private components, public components, and devices in parallel
      const [privateResponse, publicResponse, devicesResponse] = await Promise.all([
        apiService.listComponents({ usecase_id: selectedUseCase.value, scope: 'PRIVATE' }),
        apiService.listComponents({ usecase_id: selectedUseCase.value, scope: 'PUBLIC' }).catch(() => ({ components: [] })),
        apiService.listDevices(selectedUseCase.value)
      ]);

      // Transform private components
      const privateOpts: SelectProps.Option[] = privateResponse.components.map(comp => ({
        label: `${comp.component_name} v${comp.latest_version?.componentVersion || 'latest'}`,
        value: comp.arn,
        description: comp.description || 'Portal-managed component',
        tags: ['Private'],
      }));

      // Transform public components (AWS provided)
      const publicOpts: SelectProps.Option[] = (publicResponse.components || []).map(comp => ({
        label: `${comp.component_name} v${comp.latest_version?.componentVersion || 'latest'}`,
        value: comp.arn,
        description: 'AWS public component',
        tags: ['Public'],
      }));

      // Transform devices - show all managed devices
      const deviceOpts: MultiselectProps.Option[] = devicesResponse.devices.map(device => ({
        label: device.device_id,
        value: device.device_id,
        description: `${device.status} - ${device.platform || 'Unknown platform'}`,
      }));

      setPrivateComponents(privateOpts);
      setPublicComponents(publicOpts);
      setDeviceOptions(deviceOpts);

      // Pre-select component if provided in URL
      if (preSelectedComponentArn && privateOpts.length > 0) {
        const preSelected = privateOpts.find(opt => opt.value === preSelectedComponentArn);
        if (preSelected) {
          const parts = (preSelected.value as string).split(':');
          const compName = parts[parts.length - 1]?.split('/')[0] || '';
          const version = preSelected.label?.match(/v([\d.]+)/)?.[1] || 'latest';
          setSelectedComponents([{
            component_name: compName,
            component_version: version,
            arn: preSelected.value as string,
            scope: 'PRIVATE'
          }]);
        }
      }
    } catch (err) {
      console.error('Failed to load data:', err);
      setError('Failed to load components and devices');
    } finally {
      setLoading(false);
    }
  };

  const handleAddComponent = () => {
    if (!componentToAdd) return;
    
    // Extract component name and version from the option
    const arn = componentToAdd.value as string;
    const label = componentToAdd.label || '';
    const compName = label.split(' v')[0];
    const version = label.match(/v([\d.]+)/)?.[1] || 'latest';
    
    // Check if already added
    if (selectedComponents.some(c => c.arn === arn)) {
      return;
    }
    
    setSelectedComponents([...selectedComponents, {
      component_name: compName,
      component_version: version,
      arn,
      scope: componentScope
    }]);
    setComponentToAdd(null);
  };

  const handleRemoveComponent = (arn: string) => {
    setSelectedComponents(selectedComponents.filter(c => c.arn !== arn));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreating(true);
    setError('');

    try {
      if (!selectedUseCase?.value) {
        throw new Error('Please select a use case');
      }
      
      if (selectedComponents.length === 0) {
        throw new Error('Please add at least one component');
      }
      
      if (targetType === 'devices' && targetDevices.length === 0) {
        throw new Error('Please select at least one target device');
      }
      
      if (targetType === 'group' && !targetThingGroup.trim()) {
        throw new Error('Please enter a thing group name');
      }

      const deploymentData = {
        usecase_id: selectedUseCase.value,
        deployment_name: deploymentName || undefined,
        components: selectedComponents.map(c => ({
          component_name: c.component_name,
          component_version: c.component_version
        })),
        target_devices: targetType === 'devices' ? targetDevices.map(d => d.value as string) : undefined,
        target_thing_group: targetType === 'group' ? targetThingGroup.trim() : undefined,
        rollout_config: {
          auto_rollback: autoRollback,
          timeout_seconds: parseInt(timeoutSeconds) || 60
        }
      };

      const response = await apiService.createDeployment(deploymentData);
      
      // If auto-included components, show info before navigating
      if (response.auto_included && response.auto_included.length > 0) {
        setSuccessInfo({
          deployment_id: response.deployment_id,
          auto_included: response.auto_included
        });
      } else {
        navigate(`/deployments/${response.deployment_id}?usecase_id=${selectedUseCase.value}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create deployment');
      console.error('Failed to create deployment:', err);
    } finally {
      setCreating(false);
    }
  };

  const currentComponentOptions = componentScope === 'PRIVATE' ? privateComponents : publicComponents;

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
              disabled={selectedComponents.length === 0 || (targetType === 'devices' && targetDevices.length === 0)}
            >
              Create Deployment
            </Button>
          </SpaceBetween>
        }
        errorText={error}
      >
        <Container
          header={
            <Header variant="h1" description="Deploy components to edge devices">
              Create Deployment
            </Header>
          }
        >
          <SpaceBetween size="l">
            {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
            
            {successInfo && (
              <Alert
                type="success"
                header="Deployment created successfully"
                action={
                  <Button onClick={() => navigate(`/deployments/${successInfo.deployment_id}?usecase_id=${selectedUseCase?.value}`)}>
                    View Deployment
                  </Button>
                }
              >
                <SpaceBetween size="xs">
                  <Box>Deployment ID: {successInfo.deployment_id}</Box>
                  {successInfo.auto_included.length > 0 && (
                    <Box>
                      <Box fontWeight="bold">Auto-included components:</Box>
                      {successInfo.auto_included.map((comp, idx) => (
                        <Box key={idx} color="text-body-secondary">
                          • {comp.component_name} v{comp.component_version} - {comp.reason}
                        </Box>
                      ))}
                    </Box>
                  )}
                </SpaceBetween>
              </Alert>
            )}

            {/* Use Case Selection */}
            <FormField label="Use Case" description="Select the use case for this deployment">
              <Select
                selectedOption={selectedUseCase}
                onChange={({ detail }) => setSelectedUseCase(detail.selectedOption)}
                options={useCases.map(uc => ({ label: uc.name, value: uc.usecase_id }))}
                placeholder="Select use case"
              />
            </FormField>

            {/* Deployment Name */}
            <FormField 
              label="Deployment Name" 
              description="Optional name for this deployment"
            >
              <Input
                value={deploymentName}
                onChange={({ detail }) => setDeploymentName(detail.value)}
                placeholder="e.g., Production rollout v1.2"
              />
            </FormField>

            {/* Component Selection */}
            <FormField
              label="Components"
              description="Add components to deploy. You can include both private (portal-managed) and public (AWS) components."
            >
              <SpaceBetween size="s">
                <SpaceBetween direction="horizontal" size="xs">
                  <RadioGroup
                    value={componentScope}
                    onChange={({ detail }) => {
                      setComponentScope(detail.value as 'PRIVATE' | 'PUBLIC');
                      setComponentToAdd(null);
                    }}
                    items={[
                      { value: 'PRIVATE', label: 'Private (Portal-managed)' },
                      { value: 'PUBLIC', label: 'Public (AWS)' },
                    ]}
                  />
                </SpaceBetween>
                
                <SpaceBetween direction="horizontal" size="xs">
                  <Box>
                    <Select
                      selectedOption={componentToAdd}
                      onChange={({ detail }) => setComponentToAdd(detail.selectedOption)}
                      options={currentComponentOptions}
                      placeholder={loading ? "Loading..." : `Select ${componentScope.toLowerCase()} component`}
                      disabled={loading}
                      filteringType="auto"
                    />
                  </Box>
                  <Button onClick={handleAddComponent} disabled={!componentToAdd}>
                    Add Component
                  </Button>
                </SpaceBetween>

                {selectedComponents.length > 0 && (
                  <Table
                    items={selectedComponents}
                    columnDefinitions={[
                      {
                        id: 'name',
                        header: 'Component',
                        cell: item => item.component_name,
                      },
                      {
                        id: 'version',
                        header: 'Version',
                        cell: item => item.component_version,
                      },
                      {
                        id: 'scope',
                        header: 'Scope',
                        cell: item => item.scope,
                      },
                      {
                        id: 'actions',
                        header: 'Actions',
                        cell: item => (
                          <Button
                            variant="icon"
                            iconName="remove"
                            onClick={() => handleRemoveComponent(item.arn)}
                          />
                        ),
                      },
                    ]}
                    empty={<Box textAlign="center">No components selected</Box>}
                  />
                )}
              </SpaceBetween>
            </FormField>

            {/* Target Selection */}
            <FormField
              label="Deployment Target"
              description="Choose whether to deploy to specific devices or a thing group"
            >
              <RadioGroup
                value={targetType}
                onChange={({ detail }) => setTargetType(detail.value as 'devices' | 'group')}
                items={[
                  { value: 'devices', label: 'Specific Devices', description: 'Deploy to selected portal-managed devices' },
                  { value: 'group', label: 'Thing Group', description: 'Deploy to all devices in an IoT thing group' },
                ]}
              />
            </FormField>

            {targetType === 'devices' ? (
              <FormField
                label="Target Devices"
                description="Select the devices to deploy to (only portal-managed devices are shown)"
                constraintText="Required - Select at least one device"
              >
                <Multiselect
                  selectedOptions={targetDevices}
                  onChange={({ detail }) => setTargetDevices(detail.selectedOptions)}
                  options={deviceOptions}
                  placeholder={loading ? "Loading devices..." : "Select devices"}
                  filteringType="auto"
                  disabled={loading}
                />
              </FormField>
            ) : (
              <FormField
                label="Thing Group Name"
                description="Enter the name of the IoT thing group to deploy to"
                constraintText="Required"
              >
                <Input
                  value={targetThingGroup}
                  onChange={({ detail }) => setTargetThingGroup(detail.value)}
                  placeholder="e.g., production-devices"
                />
              </FormField>
            )}

            {/* Advanced Options */}
            <ExpandableSection headerText="Advanced Options" variant="footer">
              <SpaceBetween size="m">
                <FormField label="Auto Rollback">
                  <Toggle
                    checked={autoRollback}
                    onChange={({ detail }) => setAutoRollback(detail.checked)}
                  >
                    Automatically rollback on failure
                  </Toggle>
                </FormField>

                <FormField
                  label="Component Update Timeout"
                  description="Time in seconds to wait for components to update"
                >
                  <Input
                    type="number"
                    value={timeoutSeconds}
                    onChange={({ detail }) => setTimeoutSeconds(detail.value)}
                    inputMode="numeric"
                  />
                </FormField>
              </SpaceBetween>
            </ExpandableSection>
          </SpaceBetween>
        </Container>
      </Form>
    </form>
  );
}
