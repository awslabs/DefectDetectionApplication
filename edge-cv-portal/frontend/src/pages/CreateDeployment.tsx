import { useState, useEffect, useMemo } from 'react';
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
  Badge,
  ColumnLayout,
  Tabs,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { apiService } from '../services/api';
import { UseCase } from '../types';

interface ComponentSelection {
  component_name: string;
  component_version: string;
  arn: string;
  scope: 'PRIVATE' | 'PUBLIC';
  displayName?: string;
  category?: string;
  model_name?: string;
}

interface DeviceInfo {
  device_id: string;
  platform: string;
  architecture: string;
  status: string;
  installed_components?: Array<{ component_name: string; version: string }>;
}

interface ComponentInfo {
  arn: string;
  component_name: string;
  latest_version: { componentVersion: string };
  description?: string;
  model_name?: string;
  platforms?: Array<{ name?: string; attributes?: Record<string, string> }>;
  scope: 'PRIVATE' | 'PUBLIC';
}

// Helper to parse component name into friendly display name
const getComponentDisplayName = (componentName: string, modelName?: string): string => {
  // If it's a model component with a model name, use that
  if (modelName) {
    return modelName;
  }
  
  // Parse common DDA component patterns
  if (componentName.startsWith('com.dda.')) {
    const parts = componentName.replace('com.dda.', '').split('.');
    return parts.map(p => p.charAt(0).toUpperCase() + p.slice(1)).join(' ');
  }
  
  // Parse AWS component patterns
  if (componentName.startsWith('aws.greengrass.')) {
    return componentName.replace('aws.greengrass.', 'AWS ');
  }
  
  // Default: just capitalize
  return componentName;
};

// Helper to categorize components
const getComponentCategory = (componentName: string, modelName?: string, scope?: string): string => {
  if (modelName || componentName.toLowerCase().includes('model')) {
    return 'Model Components';
  }
  if (scope === 'PUBLIC' || componentName.startsWith('aws.greengrass.')) {
    return 'AWS Public Components';
  }
  if (componentName.startsWith('com.dda.')) {
    return 'DDA Infrastructure';
  }
  return 'Other Components';
};

// Helper to extract architecture from component platforms
const getComponentArchitectures = (
  _componentName: string,
  platforms?: Array<{ name?: string; attributes?: Record<string, string>; Platform?: { os?: string; architecture?: string } }>
): string[] => {
  const archs: string[] = [];
  
  // Check platform metadata from Greengrass
  if (platforms && platforms.length > 0) {
    for (const platform of platforms) {
      // Check Platform.architecture (Greengrass recipe format)
      const platformArch = platform.Platform?.architecture?.toLowerCase() || '';
      if (platformArch.includes('arm64') || platformArch.includes('aarch64')) {
        archs.push('arm64');
      } else if (platformArch.includes('amd64') || platformArch.includes('x86_64') || platformArch.includes('x86')) {
        archs.push('amd64');
      }
      
      // Check platform name (e.g., "linux/amd64", "linux/arm64")
      const name = platform.name?.toLowerCase() || '';
      if (name.includes('arm64') || name.includes('aarch64')) {
        archs.push('arm64');
      } else if (name.includes('amd64') || name.includes('x86_64') || name.includes('x86')) {
        archs.push('amd64');
      }
      
      // Check platform attributes
      const attrs = platform.attributes || {};
      const arch = (attrs.architecture || attrs.arch || '').toLowerCase();
      if (arch.includes('arm64') || arch.includes('aarch64')) {
        archs.push('arm64');
      } else if (arch.includes('amd64') || arch.includes('x86_64') || arch.includes('x86')) {
        archs.push('amd64');
      }
    }
  }
  
  // Return unique architectures, or 'all' if none found
  return archs.length > 0 ? [...new Set(archs)] : ['all'];
};

// Check if component is compatible with device architecture
const isCompatibleWithDevice = (component: ComponentInfo, deviceArch: string): boolean => {
  const componentArchs = getComponentArchitectures(component.component_name, component.platforms);
  
  // 'all' means compatible with any architecture
  if (componentArchs.includes('all')) return true;
  
  // Normalize device architecture
  const normalizedDeviceArch = deviceArch.toLowerCase();
  const isArm = normalizedDeviceArch.includes('arm64') || normalizedDeviceArch.includes('aarch64');
  const isX86 = normalizedDeviceArch.includes('amd64') || normalizedDeviceArch.includes('x86');
  
  if (isArm && componentArchs.includes('arm64')) return true;
  if (isX86 && componentArchs.includes('amd64')) return true;
  
  // If we can't determine, assume incompatible (safer default)
  return false;
};

export default function CreateDeployment() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  
  // Use case selection
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);
  
  // Component selection
  const [selectedComponents, setSelectedComponents] = useState<ComponentSelection[]>([]);
  const [allPrivateComponents, setAllPrivateComponents] = useState<ComponentInfo[]>([]);
  const [allPublicComponents, setAllPublicComponents] = useState<ComponentInfo[]>([]);
  const [componentToAdd, setComponentToAdd] = useState<SelectProps.Option | null>(null);
  const [activeComponentTab, setActiveComponentTab] = useState('recommended');
  
  // Target selection
  const [targetType, setTargetType] = useState<'devices' | 'group'>('devices');
  const [targetDevices, setTargetDevices] = useState<readonly MultiselectProps.Option[]>([]);
  const [targetThingGroup, setTargetThingGroup] = useState('');
  const [allDevices, setAllDevices] = useState<DeviceInfo[]>([]);
  
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

  // Compute selected device architectures
  const selectedDeviceArchitectures = useMemo(() => {
    if (targetType !== 'devices' || targetDevices.length === 0) return [];
    
    const archs = new Set<string>();
    for (const opt of targetDevices) {
      const device = allDevices.find(d => d.device_id === opt.value);
      if (device?.architecture) {
        archs.add(device.architecture.toLowerCase());
      }
    }
    return Array.from(archs);
  }, [targetDevices, allDevices, targetType]);

  // Check if selected devices have DDA LocalServer installed
  const devicesWithoutDDA = useMemo(() => {
    if (targetType !== 'devices' || targetDevices.length === 0) return [];
    
    const devicesNeedingDDA: string[] = [];
    for (const opt of targetDevices) {
      const device = allDevices.find(d => d.device_id === opt.value);
      if (device) {
        const hasDDA = device.installed_components?.some(comp => 
          comp.component_name.startsWith('aws.edgeml.dda.LocalServer')
        );
        if (!hasDDA) {
          devicesNeedingDDA.push(device.device_id);
        }
      }
    }
    return devicesNeedingDDA;
  }, [targetDevices, allDevices, targetType]);

  // Check if user is trying to deploy model components without DDA
  const hasModelComponents = useMemo(() => {
    return selectedComponents.some(comp => 
      comp.model_name || comp.component_name.toLowerCase().startsWith('model-')
    );
  }, [selectedComponents]);

  // Filter and categorize components based on selected devices
  const { recommendedComponents, compatiblePrivate, compatiblePublic, incompatibleComponents } = useMemo(() => {
    const hasDeviceSelection = targetType === 'devices' && targetDevices.length > 0;
    
    // Filter private components
    const filteredPrivate = allPrivateComponents.filter(comp => {
      if (!hasDeviceSelection) return true;
      return selectedDeviceArchitectures.every(arch => isCompatibleWithDevice(comp, arch));
    });
    
    // Filter public components
    const filteredPublic = allPublicComponents.filter(comp => {
      if (!hasDeviceSelection) return true;
      return selectedDeviceArchitectures.every(arch => isCompatibleWithDevice(comp, arch));
    });
    
    // Find incompatible components
    const incompatible = hasDeviceSelection ? [
      ...allPrivateComponents.filter(comp => !selectedDeviceArchitectures.every(arch => isCompatibleWithDevice(comp, arch))),
      ...allPublicComponents.filter(comp => !selectedDeviceArchitectures.every(arch => isCompatibleWithDevice(comp, arch)))
    ] : [];
    
    // Build recommended components list
    const recommended: ComponentInfo[] = [];
    
    // ALWAYS recommend DDA LocalServer (required infrastructure)
    // If not included in deployment, Greengrass will remove it from device
    const ddaComponentsPrivate = filteredPrivate.filter(comp => 
      comp.component_name.startsWith('aws.edgeml.dda.LocalServer')
    );
    const ddaComponentsPublic = filteredPublic.filter(comp => 
      comp.component_name.startsWith('aws.edgeml.dda.LocalServer')
    );
    recommended.push(...ddaComponentsPrivate, ...ddaComponentsPublic);
    
    // Add model components
    const modelComponents = filteredPrivate.filter(comp => 
      comp.model_name || comp.component_name.toLowerCase().includes('model')
    );
    recommended.push(...modelComponents);
    
    return {
      recommendedComponents: recommended,
      compatiblePrivate: filteredPrivate,
      compatiblePublic: filteredPublic,
      incompatibleComponents: incompatible
    };
  }, [allPrivateComponents, allPublicComponents, selectedDeviceArchitectures, targetType, targetDevices, devicesWithoutDDA]);

  // Convert components to select options with friendly names
  const componentToOption = (comp: ComponentInfo): SelectProps.Option => {
    const displayName = getComponentDisplayName(comp.component_name, comp.model_name);
    const version = comp.latest_version?.componentVersion || 'latest';
    const category = getComponentCategory(comp.component_name, comp.model_name, comp.scope);
    const archs = getComponentArchitectures(comp.component_name, comp.platforms);
    const archLabel = archs.includes('all') ? '' : ` (${archs.join(', ')})`;
    
    return {
      label: `${displayName} v${version}${archLabel}`,
      value: comp.arn,
      description: comp.description || category,
      tags: [comp.scope === 'PUBLIC' ? 'AWS' : 'Portal', ...archs.filter(a => a !== 'all').map(a => a.toUpperCase())],
      labelTag: comp.model_name ? 'Model' : undefined,
    };
  };

  // Device options with architecture info
  const deviceOptions = useMemo(() => {
    return allDevices.map(device => ({
      label: device.device_id,
      value: device.device_id,
      description: `${device.status} - ${device.platform || 'Unknown'} ${device.architecture || ''}`.trim(),
      tags: device.architecture ? [device.architecture.toUpperCase()] : undefined,
    }));
  }, [allDevices]);

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

      // Store raw component data for filtering
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      setAllPrivateComponents(privateResponse.components.map((comp: any) => ({
        arn: comp.arn,
        component_name: comp.component_name,
        latest_version: comp.latest_version,
        description: comp.description,
        model_name: comp.model_name,
        platforms: comp.platforms || comp.latest_version?.platforms || [],
        scope: 'PRIVATE' as const
      })));
      
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      setAllPublicComponents((publicResponse.components || []).map((comp: any) => ({
        arn: comp.arn,
        component_name: comp.component_name,
        latest_version: comp.latest_version,
        description: comp.description,
        model_name: comp.model_name,
        platforms: comp.platforms || comp.latest_version?.platforms || [],
        scope: 'PUBLIC' as const
      })));

      // Store device data with architecture info
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      setAllDevices(devicesResponse.devices.map((device: any) => ({
        device_id: device.device_id,
        platform: device.platform || '',
        architecture: device.architecture || '',
        status: device.status || 'UNKNOWN'
      })));

      // Pre-select component if provided in URL
      if (preSelectedComponentArn && privateResponse.components.length > 0) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const preSelected: any = privateResponse.components.find((c: any) => c.arn === preSelectedComponentArn);
        if (preSelected) {
          const displayName = getComponentDisplayName(preSelected.component_name, preSelected.model_name);
          const version = preSelected.latest_version?.componentVersion || 'latest';
          setSelectedComponents([{
            component_name: preSelected.component_name,
            component_version: version,
            arn: preSelected.arn,
            scope: 'PRIVATE',
            displayName,
            category: getComponentCategory(preSelected.component_name, preSelected.model_name, 'PRIVATE'),
            model_name: preSelected.model_name
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

  const handleAddComponent = (comp: ComponentInfo) => {
    // Check if already added
    if (selectedComponents.some(c => c.arn === comp.arn)) {
      return;
    }
    
    const displayName = getComponentDisplayName(comp.component_name, comp.model_name);
    const version = comp.latest_version?.componentVersion || 'latest';
    
    setSelectedComponents([...selectedComponents, {
      component_name: comp.component_name,
      component_version: version,
      arn: comp.arn,
      scope: comp.scope,
      displayName,
      category: getComponentCategory(comp.component_name, comp.model_name, comp.scope),
      model_name: comp.model_name
    }]);
  };

  const handleAddFromSelect = () => {
    if (!componentToAdd) return;
    
    // Find the component in our data
    const allComponents = [...allPrivateComponents, ...allPublicComponents];
    const comp = allComponents.find(c => c.arn === componentToAdd.value);
    if (comp) {
      handleAddComponent(comp);
    }
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
              description={
                targetType === 'devices' && targetDevices.length > 0
                  ? `Showing components compatible with selected device architecture${selectedDeviceArchitectures.length > 0 ? ` (${selectedDeviceArchitectures.map(a => a.toUpperCase()).join(', ')})` : ''}`
                  : "Select target devices first to see recommended components, or browse all available components"
              }
            >
              <SpaceBetween size="m">
                {/* Architecture compatibility notice */}
                {targetType === 'devices' && targetDevices.length > 0 && incompatibleComponents.length > 0 && (
                  <Alert type="info" dismissible>
                    {incompatibleComponents.length} component(s) hidden due to architecture incompatibility with selected devices.
                  </Alert>
                )}

                {/* DDA LocalServer requirement warning */}
                {devicesWithoutDDA.length > 0 && hasModelComponents && (
                  <Alert 
                    type="warning"
                    header="DDA LocalServer Required"
                  >
                    <SpaceBetween size="xs">
                      <Box>
                        The following device(s) do not have DDA LocalServer installed, which is required before deploying model components:
                      </Box>
                      <Box>
                        <ul style={{ margin: 0, paddingLeft: '20px' }}>
                          {devicesWithoutDDA.map(deviceId => (
                            <li key={deviceId}>{deviceId}</li>
                          ))}
                        </ul>
                      </Box>
                      <Box variant="p" color="text-body-secondary">
                        <strong>Recommended:</strong> First deploy the DDA LocalServer component (aws.edgeml.dda.LocalServer) to these devices, 
                        then create a second deployment with your model components.
                      </Box>
                    </SpaceBetween>
                  </Alert>
                )}

                {/* Important note about Greengrass deployment behavior */}
                {targetType === 'devices' && targetDevices.length > 0 && (
                  <Alert type="info">
                    <Box variant="strong">Important:</Box> Components not included in this deployment will be removed from the device. 
                    Always include infrastructure components (like DDA LocalServer) in every deployment.
                  </Alert>
                )}

                {/* Info banner when no devices selected yet */}
                {targetType === 'devices' && targetDevices.length === 0 && (
                  <Alert type="info">
                    Select target devices first to see model components compatible with their architecture.
                  </Alert>
                )}

                {/* Component tabs */}
                <Tabs
                  activeTabId={activeComponentTab}
                  onChange={({ detail }) => {
                    setActiveComponentTab(detail.activeTabId);
                    setComponentToAdd(null);
                  }}
                  tabs={[
                    {
                      id: 'recommended',
                      label: (
                        <SpaceBetween direction="horizontal" size="xs">
                          <span>Recommended</span>
                          {recommendedComponents.length > 0 && (
                            <Badge color="blue">{recommendedComponents.length}</Badge>
                          )}
                        </SpaceBetween>
                      ),
                      content: (
                        <SpaceBetween size="s">
                          {recommendedComponents.length === 0 ? (
                            <Box color="text-body-secondary" padding="s">
                              {targetType === 'devices' && targetDevices.length === 0
                                ? "Select target devices to see recommended model components"
                                : "No model components found. Train a model and create a component first."}
                            </Box>
                          ) : (
                            <ColumnLayout columns={2} variant="text-grid">
                              {recommendedComponents.map(comp => {
                                const isSelected = selectedComponents.some(c => c.arn === comp.arn);
                                const displayName = getComponentDisplayName(comp.component_name, comp.model_name);
                                const version = comp.latest_version?.componentVersion || 'latest';
                                const archs = getComponentArchitectures(comp.component_name, comp.platforms);
                                
                                return (
                                  <Box key={comp.arn} padding="s" variant="div">
                                    <SpaceBetween size="xxs">
                                      <SpaceBetween direction="horizontal" size="xs">
                                        <Box fontWeight="bold">{displayName}</Box>
                                        <Badge color="green">Model</Badge>
                                        {archs.filter(a => a !== 'all').map(arch => (
                                          <Badge key={arch} color="grey">{arch.toUpperCase()}</Badge>
                                        ))}
                                      </SpaceBetween>
                                      <Box color="text-body-secondary" fontSize="body-s">
                                        v{version} • {comp.component_name}
                                      </Box>
                                      <Button
                                        variant={isSelected ? "normal" : "primary"}
                                        disabled={isSelected}
                                        onClick={() => handleAddComponent(comp)}
                                        iconName={isSelected ? "status-positive" : "add-plus"}
                                      >
                                        {isSelected ? 'Added' : 'Add'}
                                      </Button>
                                    </SpaceBetween>
                                  </Box>
                                );
                              })}
                            </ColumnLayout>
                          )}
                        </SpaceBetween>
                      ),
                    },
                    {
                      id: 'private',
                      label: `Portal Components (${compatiblePrivate.length})`,
                      content: (
                        <SpaceBetween size="s">
                          <SpaceBetween direction="horizontal" size="xs">
                            <Box>
                              <Select
                                selectedOption={componentToAdd}
                                onChange={({ detail }) => setComponentToAdd(detail.selectedOption)}
                                options={compatiblePrivate.map(componentToOption)}
                                placeholder={loading ? "Loading..." : "Select portal component"}
                                disabled={loading}
                                filteringType="auto"
                              />
                            </Box>
                            <Button onClick={handleAddFromSelect} disabled={!componentToAdd}>
                              Add Component
                            </Button>
                          </SpaceBetween>
                        </SpaceBetween>
                      ),
                    },
                    {
                      id: 'public',
                      label: `AWS Components (${compatiblePublic.length})`,
                      content: (
                        <SpaceBetween size="s">
                          <SpaceBetween direction="horizontal" size="xs">
                            <Box>
                              <Select
                                selectedOption={componentToAdd}
                                onChange={({ detail }) => setComponentToAdd(detail.selectedOption)}
                                options={compatiblePublic.map(componentToOption)}
                                placeholder={loading ? "Loading..." : "Select AWS component"}
                                disabled={loading}
                                filteringType="auto"
                              />
                            </Box>
                            <Button onClick={handleAddFromSelect} disabled={!componentToAdd}>
                              Add Component
                            </Button>
                          </SpaceBetween>
                        </SpaceBetween>
                      ),
                    },
                  ]}
                />

                {/* Selected components table */}
                {selectedComponents.length > 0 && (
                  <Table
                    items={selectedComponents}
                    columnDefinitions={[
                      {
                        id: 'name',
                        header: 'Component',
                        cell: item => (
                          <SpaceBetween direction="horizontal" size="xs">
                            <span>{item.displayName || item.component_name}</span>
                            {item.category === 'Model Components' && <Badge color="green">Model</Badge>}
                          </SpaceBetween>
                        ),
                      },
                      {
                        id: 'technical',
                        header: 'Technical Name',
                        cell: item => <Box color="text-body-secondary" fontSize="body-s">{item.component_name}</Box>,
                      },
                      {
                        id: 'version',
                        header: 'Version',
                        cell: item => item.component_version,
                      },
                      {
                        id: 'scope',
                        header: 'Source',
                        cell: item => (
                          <Badge color={item.scope === 'PUBLIC' ? 'blue' : 'grey'}>
                            {item.scope === 'PUBLIC' ? 'AWS' : 'Portal'}
                          </Badge>
                        ),
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
                description="Select the devices to deploy to. Components will be filtered by device architecture."
                constraintText="Required - Select at least one device"
              >
                <Multiselect
                  selectedOptions={targetDevices}
                  onChange={({ detail }) => setTargetDevices(detail.selectedOptions)}
                  options={deviceOptions}
                  placeholder={loading ? "Loading devices..." : "Select devices"}
                  filteringType="auto"
                  disabled={loading}
                  tokenLimit={3}
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
