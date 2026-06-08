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
  Modal,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { apiService } from '../services/api';
import { UseCase } from '../types';
import { useUsecase } from '../contexts/UsecaseContext';
import { getErrorMessage, scrollToTop } from '../utils/errorHandling';

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
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  
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
  const [loading, setLoading] = useState(true);
  const [showRemovalWarning, setShowRemovalWarning] = useState(false);

  // Existing deployment for the selected target (revise mode). Greengrass
  // deployments are immutable and one-per-target; deploying again revises the
  // existing deployment rather than creating a parallel one.
  interface ExistingDeployment {
    deployment_id: string;
    deployment_name: string;
    deployment_status: string;
    components: Array<{ component_name: string; component_version: string }>;
  }
  const [existingDeployment, setExistingDeployment] = useState<ExistingDeployment | null>(null);
  const [checkingExisting, setCheckingExisting] = useState(false);

  // Member devices of a target thing group that already have their own individual
  // (thing-level) deployments. These conflict with a group deployment.
  interface GroupMemberConflict {
    device: string;
    deployment_id: string;
    deployment_name: string;
    deployment_status: string;
  }
  const [groupMemberConflicts, setGroupMemberConflicts] = useState<GroupMemberConflict[]>([]);
  const [showGroupConflictWarning, setShowGroupConflictWarning] = useState(false);

  const preSelectedComponentArn = searchParams.get('component_arn');
  const preSelectedComponentArns = searchParams.get('component_arns');
  const cloneComponentNames = searchParams.get('clone_components');
  const urlUseCaseId = searchParams.get('usecase_id');
  const reviseTargetDevice = searchParams.get('target_device');
  const reviseTargetThingGroup = searchParams.get('target_thing_group');

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

  // Compute components that will be removed from each target device
  const componentsToBeRemoved = useMemo(() => {
    if (targetType !== 'devices' || targetDevices.length === 0 || selectedComponents.length === 0) return {};
    
    const newComponentNames = new Set(selectedComponents.map(c => c.component_name));
    // Auto-included components won't be in selectedComponents yet, but will be added by backend
    // Add known auto-includes so they don't show as "removed"
    const autoIncludePatterns = ['aws.greengrass.Nucleus', 'aws.greengrass.LogManager'];
    autoIncludePatterns.forEach(p => newComponentNames.add(p));
    
    const removals: Record<string, Array<{ component_name: string; version: string }>> = {};
    
    for (const opt of targetDevices) {
      const device = allDevices.find(d => d.device_id === opt.value);
      if (device?.installed_components && device.installed_components.length > 0) {
        const removed = device.installed_components.filter(existing => 
          existing.component_name && 
          !newComponentNames.has(existing.component_name) &&
          // Ignore Greengrass internal/dependency components (non-root)
          !existing.component_name.startsWith('aws.greengrass.clientdevices') &&
          !existing.component_name.startsWith('aws.greengrass.ShadowManager') &&
          !existing.component_name.startsWith('aws.greengrass.SecureTunneling')
        );
        if (removed.length > 0) {
          removals[opt.value as string] = removed;
        }
      }
    }
    return removals;
  }, [targetDevices, allDevices, selectedComponents, targetType]);

  const hasComponentRemovals = Object.keys(componentsToBeRemoved).length > 0;

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

  // When a single target is selected, check whether it already has a deployment.
  // If so, we switch to "revise" mode: reuse the existing name and (when the user
  // hasn't already chosen components) pre-load the existing component set.
  useEffect(() => {
    const checkExisting = async () => {
      if (!selectedUseCase?.value) {
        setExistingDeployment(null);
        return;
      }

      let targetDevice: string | undefined;
      let targetGroup: string | undefined;
      if (targetType === 'devices') {
        // Revision identity only applies to a single device target
        setGroupMemberConflicts([]);
        if (targetDevices.length === 1) {
          targetDevice = targetDevices[0].value as string;
        } else {
          setExistingDeployment(null);
          return;
        }
      } else {
        if (targetThingGroup.trim()) {
          targetGroup = targetThingGroup.trim();
        } else {
          setExistingDeployment(null);
          setGroupMemberConflicts([]);
          return;
        }
      }

      try {
        setCheckingExisting(true);
        const resp = await apiService.getTargetDeployment({
          usecase_id: selectedUseCase.value as string,
          target_device: targetDevice,
          target_thing_group: targetGroup,
        });
        setExistingDeployment(resp.existing_deployment);
        setGroupMemberConflicts(resp.group_member_conflicts || []);

        // Pre-populate the deployment name so the revision keeps the same identity
        if (resp.existing_deployment) {
          if (!deploymentName) {
            setDeploymentName(resp.existing_deployment.deployment_name || '');
          }
          // If the user hasn't selected components yet, pre-load the existing set
          // so they revise rather than wipe the device.
          if (selectedComponents.length === 0 && resp.existing_deployment.components.length > 0) {
            preloadExistingComponents(resp.existing_deployment.components);
          }
        }
      } catch (err) {
        console.error('Failed to check existing deployment:', err);
        setExistingDeployment(null);
        setGroupMemberConflicts([]);
      } finally {
        setCheckingExisting(false);
      }
    };
    checkExisting();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedUseCase, targetType, targetDevices, targetThingGroup, allPrivateComponents, allPublicComponents]);

  // Map existing deployment components (by name) to selectable component entries.
  const preloadExistingComponents = (
    existingComps: Array<{ component_name: string; component_version: string }>
  ) => {
    const allComponents = [...allPrivateComponents, ...allPublicComponents];
    // Skip auto-included infrastructure that the backend re-adds automatically.
    const autoManaged = new Set(['aws.greengrass.Nucleus', 'aws.greengrass.LogManager']);

    const preloaded: ComponentSelection[] = [];
    for (const ec of existingComps) {
      if (autoManaged.has(ec.component_name)) continue;
      const match = allComponents.find(c => c.component_name === ec.component_name);
      if (match) {
        preloaded.push({
          component_name: match.component_name,
          component_version: ec.component_version || match.latest_version?.componentVersion || 'latest',
          arn: match.arn,
          scope: match.scope,
          displayName: getComponentDisplayName(match.component_name, match.model_name),
          category: getComponentCategory(match.component_name, match.model_name, match.scope),
          model_name: match.model_name,
        });
      } else {
        // Component not found in catalog (e.g. removed) - still represent it so the
        // user is aware it's currently deployed.
        preloaded.push({
          component_name: ec.component_name,
          component_version: ec.component_version || 'latest',
          arn: ec.component_name,
          scope: 'PRIVATE',
          displayName: getComponentDisplayName(ec.component_name),
          category: getComponentCategory(ec.component_name),
        });
      }
    }
    if (preloaded.length > 0) {
      setSelectedComponents(preloaded);
    }
  };

  const loadUseCases = async () => {
    try {
      const response = await apiService.listUseCases();
      const useCaseList = response.usecases || [];
      setUseCases(useCaseList);
      
      // Use saved selection from context, or check URL, or auto-select first
      if (selectedUsecaseId) {
        const saved = useCaseList.find((uc: UseCase) => uc.usecase_id === selectedUsecaseId);
        if (saved) {
          setSelectedUseCase({ label: saved.name, value: saved.usecase_id });
          return;
        }
      }
      
      // Pre-select from URL or first use case
      if (urlUseCaseId) {
        const preSelected = useCaseList.find((uc: UseCase) => uc.usecase_id === urlUseCaseId);
        if (preSelected) {
          setSelectedUseCase({ label: preSelected.name, value: preSelected.usecase_id });
          setSelectedUsecaseId(preSelected.usecase_id);
          return;
        }
      }
      
      if (useCaseList.length > 0) {
        setSelectedUseCase({ label: useCaseList[0].name, value: useCaseList[0].usecase_id });
        setSelectedUsecaseId(useCaseList[0].usecase_id);
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
      const mappedDevices = devicesResponse.devices.map((device: any) => ({
        device_id: device.device_id,
        platform: device.platform || '',
        architecture: device.architecture || '',
        status: device.status || 'UNKNOWN',
        installed_components: (device.installed_components || []).map((c: any) => ({
          component_name: c.componentName || c.component_name,
          version: c.componentVersion || c.version || ''
        }))
      }));
      setAllDevices(mappedDevices);

      // Revise mode: pre-select the target from URL so the existing-deployment
      // detection kicks in and pre-loads the current component set.
      if (reviseTargetThingGroup) {
        setTargetType('group');
        setTargetThingGroup(reviseTargetThingGroup);
      } else if (reviseTargetDevice) {
        setTargetType('devices');
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const match = mappedDevices.find((d: any) => d.device_id === reviseTargetDevice);
        setTargetDevices([{
          label: reviseTargetDevice,
          value: reviseTargetDevice,
          ...(match?.architecture ? { tags: [match.architecture.toUpperCase()] } : {}),
        }]);
      }


      // Pre-select component(s) if provided in URL
      if (preSelectedComponentArns) {
        // Multiple components from bulk deploy
        const arns = preSelectedComponentArns.split(',');
        const allComponents = [...privateResponse.components, ...(publicResponse.components || [])];
        const preSelectedComps = allComponents.filter((c: any) => arns.includes(c.arn));
        
        const selectedComps = preSelectedComps.map((comp: any) => {
          const displayName = getComponentDisplayName(comp.component_name, comp.model_name);
          const version = comp.latest_version?.componentVersion || 'latest';
          return {
            component_name: comp.component_name,
            component_version: version,
            arn: comp.arn,
            scope: comp.scope || 'PRIVATE',
            displayName,
            category: getComponentCategory(comp.component_name, comp.model_name, comp.scope),
            model_name: comp.model_name
          };
        });
        
        if (selectedComps.length > 0) {
          setSelectedComponents(selectedComps);
        }
      } else if (preSelectedComponentArn && privateResponse.components.length > 0) {
        // Single component from individual deploy
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
      } else if (cloneComponentNames) {
        // Clone deployment - match by component name
        const names = cloneComponentNames.split(',');
        const allComponents = [...privateResponse.components, ...(publicResponse.components || [])];
        const matchedComps = names
          .map((name: string) => allComponents.find((c: any) => c.component_name === name))
          .filter(Boolean);
        
        const selectedComps = matchedComps.map((comp: any) => {
          const displayName = getComponentDisplayName(comp.component_name, comp.model_name);
          const version = comp.latest_version?.componentVersion || 'latest';
          return {
            component_name: comp.component_name,
            component_version: version,
            arn: comp.arn,
            scope: comp.scope || 'PRIVATE',
            displayName,
            category: getComponentCategory(comp.component_name, comp.model_name, comp.scope),
            model_name: comp.model_name
          };
        });
        
        if (selectedComps.length > 0) {
          setSelectedComponents(selectedComps);
        }
      }
    } catch (err) {
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

    // If deploying to a group whose members have individual deployments, require
    // explicit confirmation before proceeding.
    if (targetType === 'group' && groupMemberConflicts.length > 0 && !showGroupConflictWarning) {
      setShowGroupConflictWarning(true);
      return;
    }

    // If there are component removals and user hasn't confirmed yet, show warning
    if (hasComponentRemovals && !showRemovalWarning) {
      setShowRemovalWarning(true);
      return;
    }
    
    await executeDeployment();
  };

  const executeDeployment = async () => {
    setCreating(true);
    setError('');
    setShowRemovalWarning(false);
    setShowGroupConflictWarning(false);

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
        components: selectedComponents.map(c => {
          const version = (c.component_version && !['0.0.0', 'unknown', 'latest'].includes(c.component_version)) 
            ? c.component_version 
            : '';
          return {
            component_name: c.component_name,
            component_version: version
          };
        }),
        target_devices: targetType === 'devices' ? targetDevices.map(d => d.value as string) : undefined,
        target_thing_group: targetType === 'group' ? targetThingGroup.trim() : undefined,
        rollout_config: {
          auto_rollback: autoRollback,
          timeout_seconds: parseInt(timeoutSeconds) || 60
        }
      };

      const response = await apiService.createDeployment(deploymentData);
      
      // Navigate to deployment detail page after successful creation
      // Use a small delay to ensure the response is fully processed
      setTimeout(() => {
        navigate(`/deployments/${response.deployment_id}?usecase_id=${selectedUseCase.value}`);
      }, 500);
    } catch (err) {
      setError(getErrorMessage(err, 'Failed to create deployment'));
      console.error('Failed to create deployment:', err);
      scrollToTop();
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
              {existingDeployment ? 'Update Deployment' : 'Create Deployment'}
            </Button>
          </SpaceBetween>
        }
        errorText={error}
      >
        <Container
          header={
            <Header
              variant="h1"
              description={existingDeployment
                ? 'This target already has a deployment. Saving will revise the existing deployment.'
                : 'Deploy components to edge devices'}
            >
              {existingDeployment ? 'Update Deployment' : 'Create Deployment'}
            </Header>
          }
        >
          <SpaceBetween size="l">
            {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}

            {/* Revise-mode banner */}
            {existingDeployment && (
              <Alert type="info" header="Revising existing deployment">
                This target is already running deployment{' '}
                <strong>{existingDeployment.deployment_name || existingDeployment.deployment_id}</strong>{' '}
                (status: {existingDeployment.deployment_status}). A device can only have one active
                deployment, so saving will <strong>update and supersede</strong> the existing deployment
                rather than create a new one. The current components have been pre-loaded below — adjust
                them as needed.
              </Alert>
            )}
            
            {/* CloudWatch Logging Info */}
            <Alert type="info" header="CloudWatch Logging">
              To view component logs in the portal, ensure the <strong>aws.greengrass.LogManager</strong> component is included in your deployment. 
              After deployment, log groups will be created automatically when components generate output. 
              See <a href="https://docs.aws.amazon.com/greengrass/latest/developerguide/log-manager-component.html" target="_blank" rel="noopener noreferrer">LogManager documentation</a> for configuration details.
            </Alert>

            {/* Use Case Selection */}
            <FormField label="Use Case" description="Select the use case for this deployment">
              <Select
                selectedOption={selectedUseCase}
                onChange={({ detail }) => {
                  setSelectedUseCase(detail.selectedOption);
                  setSelectedUsecaseId(detail.selectedOption?.value || null);
                }}
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
                                const displayVersion = (version === '0.0.0' || version === 'unknown') ? 'Latest' : `v${version}`;
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
                                        {displayVersion} • {comp.component_name}
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
                    resizableColumns
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
                        cell: item => {
                          const displayVersion = (item.component_version === '0.0.0' || item.component_version === 'unknown') 
                            ? 'Latest' 
                            : item.component_version;
                          return displayVersion;
                        },
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
                            variant="normal"
                            iconName="remove"
                            onClick={() => handleRemoveComponent(item.arn)}
                          >
                            Remove
                          </Button>
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
                <SpaceBetween size="xs">
                  <Multiselect
                    selectedOptions={targetDevices}
                    onChange={({ detail }) => setTargetDevices(detail.selectedOptions)}
                    options={deviceOptions}
                    placeholder={loading ? "Loading devices..." : "Select devices"}
                    filteringType="auto"
                    disabled={loading}
                    tokenLimit={3}
                  />
                  {checkingExisting && (
                    <Box color="text-body-secondary" fontSize="body-s">Checking for existing deployment…</Box>
                  )}
                  {targetDevices.length > 1 && (
                    <Alert type="warning">
                      You've selected multiple devices. Existing-deployment detection and revision only
                      apply when a single device is targeted. Deploying to multiple devices at once may
                      create separate deployments per device. To revise a specific device's deployment,
                      select just that one device.
                    </Alert>
                  )}
                </SpaceBetween>
              </FormField>
            ) : (
              <FormField
                label="Thing Group Name"
                description="Enter the name of the IoT thing group to deploy to"
                constraintText="Required"
              >
                <SpaceBetween size="xs">
                  <Input
                    value={targetThingGroup}
                    onChange={({ detail }) => setTargetThingGroup(detail.value)}
                    placeholder="e.g., production-devices"
                  />
                  {checkingExisting && (
                    <Box color="text-body-secondary" fontSize="body-s">Checking group members for existing deployments…</Box>
                  )}
                  {groupMemberConflicts.length > 0 && (
                    <Alert type="warning" header="Devices in this group have individual deployments">
                      <SpaceBetween size="xs">
                        <Box>
                          The following device(s) in this thing group already have their own
                          individual (device-level) deployment. A device that has both an individual
                          deployment and a group deployment will run two deployments at once, which
                          can lead to conflicting or unpredictable component state.
                        </Box>
                        <ul style={{ margin: 0, paddingLeft: '20px' }}>
                          {groupMemberConflicts.map(c => (
                            <li key={c.device}>
                              <strong>{c.device}</strong> — {c.deployment_name || c.deployment_id}{' '}
                              ({c.deployment_status})
                            </li>
                          ))}
                        </ul>
                        <Box variant="p" color="text-body-secondary">
                          <strong>Recommended:</strong> Cancel the individual deployment(s) on these
                          devices before deploying to the group, so the group deployment becomes the
                          single source of truth. You can cancel a deployment from its detail page.
                        </Box>
                      </SpaceBetween>
                    </Alert>
                  )}
                </SpaceBetween>
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

      {/* Group Member Conflict Warning Modal */}
      <Modal
        visible={showGroupConflictWarning}
        onDismiss={() => setShowGroupConflictWarning(false)}
        header="Devices already have individual deployments"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowGroupConflictWarning(false)}>Cancel</Button>
              <Button
                variant="primary"
                loading={creating}
                onClick={() => {
                  // Proceed past the group-conflict gate; component-removal gate (if any) still applies.
                  setShowGroupConflictWarning(false);
                  if (hasComponentRemovals) {
                    setShowRemovalWarning(true);
                  } else {
                    executeDeployment();
                  }
                }}
              >
                Deploy anyway
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Alert type="warning">
            One or more devices in this thing group already have their own individual
            (device-level) deployment. Deploying to the group will leave those devices running
            two deployments at once, which can cause conflicting component state.
          </Alert>
          <ul style={{ margin: 0, paddingLeft: '20px' }}>
            {groupMemberConflicts.map(c => (
              <li key={c.device}>
                <strong>{c.device}</strong> — {c.deployment_name || c.deployment_id} ({c.deployment_status})
              </li>
            ))}
          </ul>
          <Box color="text-body-secondary">
            We recommend cancelling these individual deployments first so the group deployment
            is the single source of truth. Click Cancel to go back, or Deploy anyway to proceed.
          </Box>
        </SpaceBetween>
      </Modal>

      {/* Component Removal Warning Modal */}
      <Modal
        visible={showRemovalWarning}
        onDismiss={() => setShowRemovalWarning(false)}
        header="Components will be removed"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowRemovalWarning(false)}>Cancel</Button>
              <Button variant="primary" loading={creating} onClick={executeDeployment}>
                Continue Deployment
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Alert type="warning">
            The following components are currently installed on the target device(s) but are not included in this deployment. 
            Greengrass will remove them from the device.
          </Alert>
          {Object.entries(componentsToBeRemoved).map(([deviceId, removedComps]) => (
            <SpaceBetween key={deviceId} size="xs">
              <Box variant="h4">{deviceId}</Box>
              <ul style={{ margin: 0, paddingLeft: '20px' }}>
                {removedComps.map(comp => (
                  <li key={comp.component_name}>
                    {comp.component_name} {comp.version ? `(v${comp.version})` : ''}
                  </li>
                ))}
              </ul>
            </SpaceBetween>
          ))}
          <Box color="text-body-secondary">
            If this is not intended, click Cancel and add the missing components to your deployment.
          </Box>
        </SpaceBetween>
      </Modal>
    </form>
  );
}
