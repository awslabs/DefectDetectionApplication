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
  StatusIndicator,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { apiService, ApiError } from '../services/api';
import { UseCase } from '../types';
import { useUsecase } from '../contexts/UsecaseContext';
import { getErrorMessage, scrollToTop } from '../utils/errorHandling';
import { LifecycleBadge } from './node-designer/badges';
import {
  PluginGateRejection,
  isPluginComponent,
  parsePluginGateRejection,
} from './deployments/pluginComponents';
import { ArchitectureChips, PluginGateRejectionAlert } from './deployments/PluginComponentUi';
import {
  VllmComponentManifest,
  VllmGateRejection,
  describeVllmArchEntry,
  evaluateVllmArchGate,
  isVllmModelComponent,
  parseVllmGateRejection,
} from './deployments/vllmArchGate';
import {
  classifyGatedComponent,
  componentSupportedArchs,
  describeArchIncompatibility,
  inferComponentTargetArchs,
  isCompatibleWithAllDevices,
} from './deployments/archCompatibility';
import {
  BindingCell,
  BindingSelections,
  CameraBindingContext,
  CameraBindingIssue,
  CameraBindingWarning,
  buildCameraBindings,
  expectedBindingWarnings,
  initialBindingSelections,
  parseCameraBindingRejection,
  parseWorkflowComponent,
  unboundCells,
  withBindingCell,
} from './deployments/cameraBindings';
import { CameraBindingMatrix } from './deployments/CameraBindingMatrix';
import { isWorkflowComponent, workflowComponentName } from './workflows/workflowComponentName';

// {workflowId: name} for resolving friendly names of packaged workflow
// components (dda.workflow.{id}). Populated from listWorkflows when the
// deployment picker loads so getComponentDisplayName — which is called in
// many places (cards, dropdown, selected list, preload) — can show the
// workflow name instead of the raw UUID. Module-level so the pure display
// helper can consult it without threading the map through every call site.
let workflowNameMap: Record<string, string> = {};

interface ComponentSelection {
  component_name: string;
  component_version: string;
  arn: string;
  scope: 'PRIVATE' | 'PUBLIC';
  displayName?: string;
  category?: string;
  model_name?: string;
  // Node Designer Plugin_Component fields (custom-node-designer, 16.2)
  is_plugin_component?: boolean;
  lifecycle_state?: string | null;
  supported_architectures?: string[];
}

interface DeviceInfo {
  device_id: string;
  platform: string;
  architecture: string;
  // Portal-recorded DDA Target_Architecture (Devices table) — the value
  // the deployment architecture gates match by exact name; null when the
  // device has none recorded (fails closed, vllm-triton-inference 3.9).
  target_architecture?: string | null;
  status: string;
  installed_components?: Array<{ component_name: string; version: string }>;
}

interface ComponentInfo {
  arn: string;
  component_name: string;
  latest_version: { componentVersion: string };
  description?: string;
  model_name?: string;
  // Backing training-jobs record id (registry tag) — joins a
  // model-vllm-* component with its vLLM_Model_Record so the client-side
  // architecture gate twin can read the supported set (15.3, Req 3.9).
  training_job_id?: string;
  platforms?: Array<{ name?: string; attributes?: Record<string, string> }>;
  scope: 'PRIVATE' | 'PUBLIC';
  // Node Designer Plugin_Component fields (custom-node-designer, 16.2):
  // backing Plugin_Record Lifecycle_State and the Target_Architectures
  // derived from the recipe's platform manifests (components.py).
  is_plugin_component?: boolean;
  lifecycle_state?: string | null;
  supported_architectures?: string[];
}

// Helper to parse component name into friendly display name
const getComponentDisplayName = (componentName: string, modelName?: string): string => {
  // Packaged workflow components are named dda.workflow.{uuid}; show the
  // friendly workflow name resolved from listWorkflows when available,
  // falling back to the raw component name (never the model-name branch).
  if (isWorkflowComponent(componentName)) {
    return workflowComponentName(componentName, workflowNameMap) ?? componentName;
  }

  // If it's a model component with a model name, use that
  if (modelName) {
    return modelName;
  }
  
  // Node Designer Plugin_Components: dda.plugin.{pluginId} (16.2)
  if (isPluginComponent(componentName)) {
    return componentName.replace('dda.plugin.', '');
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
  // Packaged workflow components (dda.workflow.{uuid}) get their own category
  // so they are obviously identifiable rather than lumped into "Other".
  if (isWorkflowComponent(componentName)) {
    return 'Workflows';
  }
  if (isPluginComponent(componentName)) {
    return 'Node Plugins';
  }
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
  // Pre-submit plugin gate rejection (custom-node-designer, 16.3/16.6)
  const [gateRejection, setGateRejection] = useState<PluginGateRejection | null>(null);
  // Backend 409 VLLM_ARCH_UNSUPPORTED rejection (vllm-triton-inference 3.4)
  const [vllmGateRejection, setVllmGateRejection] = useState<VllmGateRejection | null>(null);
  // Supported Target_Architecture sets of selected model-vllm-* components,
  // keyed by component name — read from the backing vLLM_Model_Record's
  // published_component.supported_architectures via the model detail API
  // (joined through the component listing's training_job_id tag). An
  // unresolvable record maps to [] so the client-side gate twin fails
  // closed, mirroring the backend (15.3, Requirement 3.9).
  const [vllmComponentArchs, setVllmComponentArchs] = useState<Record<string, string[]>>({});
  const [loading, setLoading] = useState(true);
  const [showRemovalWarning, setShowRemovalWarning] = useState(false);

  // Deploy-time Camera_Binding matrix state, keyed by workflow id
  // (camera-registry-sync 12.1 — Requirements 8.1, 8.4, 8.5, 8.7-8.9,
  // 9.2, 9.3). Contexts come from GET /deployments?view=binding-context
  // for each selected dda.workflow.* component once targets are chosen.
  const [bindingContexts, setBindingContexts] = useState<Record<string, CameraBindingContext>>({});
  const [bindingSelections, setBindingSelections] = useState<Record<string, BindingSelections>>({});
  // Warnings reported by a rejected submission (409) beyond the ones
  // predicted client-side; confirmation checkboxes feed confirmed_warnings.
  const [serverBindingWarnings, setServerBindingWarnings] = useState<Record<string, CameraBindingWarning[]>>({});
  const [confirmedWarningIds, setConfirmedWarningIds] = useState<Set<string>>(new Set());
  // Validation errors of a 409 CAMERA_BINDINGS_INVALID rejection,
  // surfaced next to the matrix identifying node and device.
  const [bindingErrors, setBindingErrors] = useState<Record<string, CameraBindingIssue[]>>({});
  const [bindingContextError, setBindingContextError] = useState('');

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

  // Resolve the supported Target_Architecture set of every selected
  // model-vllm-* component from its backing vLLM_Model_Record
  // (published_component.supported_architectures, written at publish
  // time). Components whose record cannot be resolved get an empty set
  // so the gate twin fails closed, like the backend's GSI lookup (15.3).
  useEffect(() => {
    // Resolve both the selected model-vllm-* components AND the catalog
    // model-vllm-* components (private + public), so the deploy-screen
    // arch filter can evaluate not-yet-selected vLLM components too
    // (device-arch-compatibility 4.1). Keyed by component name and
    // deduped against already-resolved entries; still-resolving entries
    // stay absent (undefined) and are not hidden until they resolve.
    const candidates = [
      ...selectedComponents.map(c => c.component_name),
      ...allPrivateComponents.map(c => c.component_name),
      ...allPublicComponents.map(c => c.component_name),
    ];
    const pending = [...new Set(candidates)]
      .filter(isVllmModelComponent)
      .filter(name => !(name in vllmComponentArchs));
    if (pending.length === 0) return;
    let cancelled = false;
    const resolveArchs = async () => {
      const resolved: Record<string, string[]> = {};
      const catalog = [...allPrivateComponents, ...allPublicComponents];
      for (const name of pending) {
        const trainingJobId = catalog.find(
          c => c.component_name === name
        )?.training_job_id;
        if (!trainingJobId) {
          resolved[name] = []; // unresolvable record: fail closed
          continue;
        }
        try {
          const resp = await apiService.getModel(trainingJobId);
          resolved[name] = (
            resp.model.published_component?.supported_architectures || []
          ).map(String);
        } catch (err) {
          console.error(`Failed to load vLLM model record for ${name}:`, err);
          resolved[name] = []; // fail closed
        }
      }
      if (!cancelled) {
        setVllmComponentArchs(prev => ({ ...prev, ...resolved }));
      }
    };
    resolveArchs();
    return () => { cancelled = true; };
  }, [selectedComponents, allPrivateComponents, allPublicComponents, vllmComponentArchs]);

  // Client-side vLLM architecture gate twin (15.3, Requirement 3.9):
  // each selected device's recorded Target_Architecture is checked
  // against the supported set of every selected model-vllm-* component
  // with the same pure predicate as the backend gate (exact-name match,
  // absent arch fails closed, jp4-specific reason). LLM-bearing workflow
  // components cannot be checked client-side — the version-item
  // has_llm_inference/packaged_architectures discriminators are not
  // exposed by the workflow APIs — so those rely on the authoritative
  // backend gate, whose 409 VLLM_ARCH_UNSUPPORTED is surfaced on submit.
  const vllmArchWarnings = useMemo(() => {
    if (targetType !== 'devices' || targetDevices.length === 0) return [];
    const manifests: Record<string, VllmComponentManifest> = {};
    for (const comp of selectedComponents) {
      if (!isVllmModelComponent(comp.component_name)) continue;
      const architectures = vllmComponentArchs[comp.component_name];
      // Still resolving: contribute nothing yet (the backend gate is
      // authoritative) rather than flash a transient warning.
      if (architectures === undefined) continue;
      const version = comp.component_version
        && !['0.0.0', 'unknown', 'latest'].includes(comp.component_version)
        ? comp.component_version
        : null;
      manifests[comp.component_name] = { version, architectures };
    }
    if (Object.keys(manifests).length === 0) return [];
    const deviceArchs: Record<string, string | null> = {};
    for (const opt of targetDevices) {
      const device = allDevices.find(d => d.device_id === opt.value);
      deviceArchs[opt.value as string] = device?.target_architecture ?? null;
    }
    return evaluateVllmArchGate(manifests, deviceArchs);
  }, [selectedComponents, vllmComponentArchs, targetDevices, allDevices, targetType]);

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

  // Selected packaged Workflow_Components (dda.workflow.*) resolved to
  // their workflow identity (camera-registry-sync 12.1).
  const selectedWorkflows = useMemo(() => {
    const refs: Array<{ workflowId: string; workflowVersion: number | null }> = [];
    for (const comp of selectedComponents) {
      const ref = parseWorkflowComponent(comp.component_name, comp.component_version);
      if (ref && !refs.some(r => r.workflowId === ref.workflowId)) {
        refs.push(ref);
      }
    }
    return refs;
  }, [selectedComponents]);

  // Matrix step shown only for workflow versions with Camera_Input_Nodes
  // (binding_required) — skipped entirely otherwise (Requirement 8.9).
  const bindingMatrices = useMemo(
    () => Object.entries(bindingContexts).filter(([, ctx]) => ctx.binding_required),
    [bindingContexts]
  );

  // The warnings the current matrix state needs confirmed (8.8, 9.3):
  // client-side predictions plus any extra warnings a rejected
  // submission reported.
  const bindingWarningsFor = (workflowId: string): CameraBindingWarning[] => {
    const context = bindingContexts[workflowId];
    if (!context) return [];
    const expected = expectedBindingWarnings(context, bindingSelections[workflowId] || {});
    const seen = new Set(expected.map(w => w.id));
    const extra = (serverBindingWarnings[workflowId] || []).filter(w => !seen.has(w.id));
    return [...expected, ...extra];
  };

  // Load the binding context of every selected workflow component once a
  // use case and deployment targets are chosen (Requirement 8.1).
  useEffect(() => {
    const usecaseId = selectedUseCase?.value as string | undefined;
    const deviceNames = targetType === 'devices'
      ? targetDevices.map(d => d.value as string)
      : [];
    const thingGroup = targetType === 'group' ? targetThingGroup.trim() : '';
    if (!usecaseId || selectedWorkflows.length === 0
        || (deviceNames.length === 0 && !thingGroup)) {
      setBindingContexts({});
      setBindingSelections({});
      setServerBindingWarnings({});
      setBindingErrors({});
      setBindingContextError('');
      return;
    }
    let cancelled = false;
    const loadBindingContexts = async () => {
      try {
        setBindingContextError('');
        const contexts: Record<string, CameraBindingContext> = {};
        for (const workflow of selectedWorkflows) {
          contexts[workflow.workflowId] = await apiService.getCameraBindingContext({
            usecase_id: usecaseId,
            workflow_id: workflow.workflowId,
            workflow_version: workflow.workflowVersion ?? undefined,
            target_devices: deviceNames.length > 0 ? deviceNames : undefined,
            target_thing_group: thingGroup || undefined,
          });
        }
        if (cancelled) return;
        setBindingContexts(contexts);
        // Seed each matrix from the hint pre-selection (8.5), keeping
        // choices the user already made for still-present cells.
        setBindingSelections(prev => {
          const next: Record<string, BindingSelections> = {};
          for (const [workflowId, context] of Object.entries(contexts)) {
            if (context.binding_required) {
              next[workflowId] = initialBindingSelections(context, prev[workflowId]);
            }
          }
          return next;
        });
      } catch (err) {
        if (cancelled) return;
        console.error('Failed to load camera binding context:', err);
        setBindingContexts({});
        setBindingSelections({});
        setBindingContextError(getErrorMessage(
          err,
          'Failed to load the camera binding context for the selected workflow component(s)'
        ));
      }
    };
    loadBindingContexts();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedUseCase, targetType, targetDevices, targetThingGroup, selectedWorkflows]);

  const handleBindingCellChange = (workflowId: string, device: string,
                                   nodeId: string, cell: BindingCell) => {
    setBindingSelections(prev => ({
      ...prev,
      [workflowId]: withBindingCell(prev[workflowId] || {}, device, nodeId, cell),
    }));
  };

  const handleToggleBindingWarning = (warningId: string, confirmed: boolean) => {
    setConfirmedWarningIds(prev => {
      const next = new Set(prev);
      if (confirmed) {
        next.add(warningId);
      } else {
        next.delete(warningId);
      }
      return next;
    });
  };

  // Filter and categorize components based on selected devices
  const {
    recommendedComponents,
    compatiblePrivate,
    compatiblePublic,
    incompatibleComponents,
    pluginComponents,
    incompatibleGatedComponents,
    devicesWithoutRecordedArch,
  } = useMemo(() => {
    const hasDeviceSelection = targetType === 'devices' && targetDevices.length > 0;

    // Selected devices' recorded DDA Target_Architecture map (Req 3.1)
    // and the subset with no recorded architecture (Req 3.5). Only
    // resolvable for explicit device targets — a thing group's member
    // architectures are not available on this screen (Req 3.7), so
    // gated-arch filtering is skipped for groups (targetType !== 'devices').
    const selectedDeviceTargetArchs: Record<string, string | null> = {};
    if (hasDeviceSelection) {
      for (const opt of targetDevices) {
        const device = allDevices.find(d => d.device_id === opt.value);
        selectedDeviceTargetArchs[opt.value as string] = device?.target_architecture ?? null;
      }
    }
    const selectedDeviceArchList = Object.values(selectedDeviceTargetArchs);
    const devicesMissingArch = Object.keys(selectedDeviceTargetArchs)
      .filter(d => selectedDeviceTargetArchs[d] === null)
      .sort();

    // The recorded architectures of the selected devices, ignoring devices
    // with none recorded (name-based JetPack filtering can only judge a
    // device whose Target_Architecture is known).
    const recordedDeviceArchs = selectedDeviceArchList.filter(
      (a): a is string => a !== null
    );

    // Returns a human-readable reason when `comp` is architecture-incompatible
    // with the selected device(s), or null when it is compatible / cannot be
    // judged. Two sources of truth (device-arch-compatibility Req 3):
    //   - Backend-gated components (model-vllm-* / dda.plugin.*): the recorded
    //     supported_architectures, matched by the exact-name predicate the
    //     backend gate applies, failing closed on a null device arch (Req 3.1,
    //     3.2, 3.5, 5.1). A vLLM set still resolving (undefined) is not hidden
    //     yet (design B4).
    //   - Regular model / LocalServer components: the JetPack target inferred
    //     from the component name (`*-jp5`, `arm64JP6`, …). The backend does
    //     not arch-check these, but a jp5 build must not be offered for a jp6
    //     device. Names with no JetPack token are left to the coarse
    //     arm64/amd64 filter (kept). A device with no recorded arch cannot be
    //     judged by name, so it does not hide these (fails open here — the
    //     no-recorded-architecture warning covers that case for gated ones).
    const archIncompatReason = (comp: ComponentInfo): string | null => {
      if (!hasDeviceSelection) return null;
      const kind = classifyGatedComponent(comp.component_name);
      if (kind !== null) {
        if (kind === 'vllm' && vllmComponentArchs[comp.component_name] === undefined) {
          return null; // still resolving — do not hide yet
        }
        const supported = componentSupportedArchs(comp, vllmComponentArchs);
        if (isCompatibleWithAllDevices(supported, selectedDeviceArchList)) return null;
        return describeArchIncompatibility(comp, selectedDeviceTargetArchs, supported);
      }
      const inferred = inferComponentTargetArchs(comp.component_name);
      if (inferred.length === 0) return null; // no JetPack token in the name
      if (recordedDeviceArchs.every(a => inferred.includes(a))) return null;
      return describeArchIncompatibility(comp, selectedDeviceTargetArchs, inferred);
    };
    const isArchIncompatible = (comp: ComponentInfo): boolean =>
      archIncompatReason(comp) !== null;

    // Node Designer Plugin_Components (dda.plugin.*, 16.2) are listed in
    // their own tab with lifecycle badges and Target_Architecture chips,
    // now subject to the exact-name Target_Architecture gate (Req 3.3):
    // arch-incompatible plugins are excluded here and surfaced below.
    const plugins = allPrivateComponents
      .filter(comp => comp.is_plugin_component || isPluginComponent(comp.component_name))
      .filter(comp => !isArchIncompatible(comp));
    const nonPluginPrivate = allPrivateComponents.filter(
      comp => !(comp.is_plugin_component || isPluginComponent(comp.component_name))
    );

    // A component is offered only when it passes BOTH the architecture
    // incompatibility check (gated supported set OR name-inferred JetPack
    // target) and, for non-gated components, the coarse arm64/amd64 filter
    // (Req 3.3, 3.6, 5.3).
    const passesArchFilter = (comp: ComponentInfo): boolean => {
      if (isArchIncompatible(comp)) return false;
      if (classifyGatedComponent(comp.component_name) !== null) return true;
      if (!hasDeviceSelection) return true;
      return selectedDeviceArchitectures.every(arch => isCompatibleWithDevice(comp, arch));
    };

    // Filter private components
    const filteredPrivate = nonPluginPrivate.filter(passesArchFilter);

    // Filter public components
    const filteredPublic = allPublicComponents.filter(passesArchFilter);

    // Coarse arm64/amd64-incompatible NON-gated components (an x86 build on
    // an arm device, etc.) — surfaced only as a hidden-count notice. A
    // JetPack-major mismatch is NOT coarse-incompatible (both are arm64) and
    // is instead collected with an explainable reason below.
    const isCoarseIncompatible = (comp: ComponentInfo): boolean =>
      classifyGatedComponent(comp.component_name) === null &&
      inferComponentTargetArchs(comp.component_name).length === 0 &&
      !selectedDeviceArchitectures.every(arch => isCompatibleWithDevice(comp, arch));
    const incompatible = hasDeviceSelection ? [
      ...nonPluginPrivate.filter(isCoarseIncompatible),
      ...allPublicComponents.filter(isCoarseIncompatible),
    ] : [];

    // Every component excluded for a Target_Architecture mismatch (gated
    // supported set OR name-inferred JetPack target), each with a
    // per-component reason so the exclusion is explainable rather than
    // silent (Req 3.3, 3.4).
    const incompatibleGated = hasDeviceSelection
      ? [...nonPluginPrivate, ...allPublicComponents,
         ...allPrivateComponents.filter(
           comp => comp.is_plugin_component || isPluginComponent(comp.component_name))]
          .map(comp => ({ component: comp, reason: archIncompatReason(comp) }))
          .filter((x): x is { component: ComponentInfo; reason: string } =>
            x.reason !== null)
      : [];

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
      incompatibleComponents: incompatible,
      pluginComponents: plugins,
      incompatibleGatedComponents: incompatibleGated,
      devicesWithoutRecordedArch: devicesMissingArch,
    };
  }, [allPrivateComponents, allPublicComponents, selectedDeviceArchitectures, targetType, targetDevices, allDevices, vllmComponentArchs, devicesWithoutDDA]);

  // Revise-mode surfacing (device-arch-compatibility 4.3, Req 4.1-4.3):
  // a pre-loaded gated component that is now arch-incompatible with the
  // target device's recorded architecture is flagged in the selected-
  // components table (keyed by arn) with the same reason detail as the
  // exclusion grouping — WITHOUT removing it from the pre-loaded set or
  // blocking removal/submission. Incompatible gated components can only
  // reach the selected list via pre-load/clone (the picker excludes them),
  // so this is effectively the revise-mode indicator.
  const selectedComponentArchIssues = useMemo(() => {
    if (targetType !== 'devices' || targetDevices.length === 0) return {};
    const deviceArchs: Record<string, string | null> = {};
    for (const opt of targetDevices) {
      const device = allDevices.find(d => d.device_id === opt.value);
      deviceArchs[opt.value as string] = device?.target_architecture ?? null;
    }
    const archList = Object.values(deviceArchs);
    const recorded = archList.filter((a): a is string => a !== null);
    const issues: Record<string, string> = {};
    for (const comp of selectedComponents) {
      const kind = classifyGatedComponent(comp.component_name);
      if (kind !== null) {
        // Still-resolving vLLM set: don't flag yet (mirrors the picker).
        if (kind === 'vllm' && vllmComponentArchs[comp.component_name] === undefined) continue;
        const supported = componentSupportedArchs(comp, vllmComponentArchs);
        if (!isCompatibleWithAllDevices(supported, archList)) {
          issues[comp.arn] = describeArchIncompatibility(comp, deviceArchs, supported);
        }
        continue;
      }
      // Non-gated: name-inferred JetPack target (e.g. a jp5 build on a jp6
      // device). No token / no recorded device arch → not flagged.
      const inferred = inferComponentTargetArchs(comp.component_name);
      if (inferred.length === 0) continue;
      if (!recorded.every(a => inferred.includes(a))) {
        issues[comp.arn] = describeArchIncompatibility(comp, deviceArchs, inferred);
      }
    }
    return issues;
  }, [selectedComponents, targetDevices, allDevices, targetType, vllmComponentArchs]);

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
      labelTag: isWorkflowComponent(comp.component_name)
        ? 'Workflow'
        : (comp.model_name ? 'Model' : undefined),
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
          is_plugin_component: match.is_plugin_component,
          lifecycle_state: match.lifecycle_state,
          supported_architectures: match.supported_architectures,
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
      
      // Load private components, public components, devices, and workflow
      // metadata in parallel. Workflow names resolve the friendly label for
      // packaged dda.workflow.{id} components (best-effort; a failure just
      // leaves the raw UUID).
      const [privateResponse, publicResponse, devicesResponse, workflowsResponse] = await Promise.all([
        apiService.listComponents({ usecase_id: selectedUseCase.value, scope: 'PRIVATE' }),
        apiService.listComponents({ usecase_id: selectedUseCase.value, scope: 'PUBLIC' }).catch(() => ({ components: [] })),
        apiService.listDevices(selectedUseCase.value),
        apiService.listWorkflows(selectedUseCase.value).catch(() => ({ workflows: [], count: 0 })),
      ]);

      // Populate the module-level name map before mapping components so
      // getComponentDisplayName resolves workflow names on this render pass.
      workflowNameMap = Object.fromEntries(
        (workflowsResponse.workflows || []).map((w) => [w.workflow_id, w.name])
      );

      // Store raw component data for filtering
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      setAllPrivateComponents(privateResponse.components.map((comp: any) => ({
        arn: comp.arn,
        component_name: comp.component_name,
        latest_version: comp.latest_version,
        description: comp.description,
        model_name: comp.model_name,
        training_job_id: comp.training_job_id,
        platforms: comp.platforms || comp.latest_version?.platforms || [],
        scope: 'PRIVATE' as const,
        // Node Designer Plugin_Component listing fields (16.2)
        is_plugin_component: comp.is_plugin_component || isPluginComponent(comp.component_name),
        lifecycle_state: comp.lifecycle_state ?? null,
        supported_architectures: comp.supported_architectures || []
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
        target_architecture: device.target_architecture || null,
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
      model_name: comp.model_name,
      is_plugin_component: comp.is_plugin_component,
      lifecycle_state: comp.lifecycle_state,
      supported_architectures: comp.supported_architectures
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

  // Look up the latest available version for an already-selected component
  // (matched by ARN/component name against the loaded catalog).
  const getLatestVersionFor = (item: ComponentSelection): string | null => {
    const allComponents = [...allPrivateComponents, ...allPublicComponents];
    const match = allComponents.find(
      c => c.arn === item.arn || c.component_name === item.component_name
    );
    const latest = match?.latest_version?.componentVersion;
    if (!latest || latest === '0.0.0' || latest === 'unknown') return null;
    return latest;
  };

  // Bump a selected component to its latest available version IN PLACE so the
  // revise/deploy flow needs only a single deployment (no remove + re-add).
  const handleUpdateToLatest = (arn: string) => {
    setSelectedComponents(prev =>
      prev.map(c => {
        if (c.arn !== arn) return c;
        const latest = getLatestVersionFor(c);
        return latest ? { ...c, component_version: latest } : c;
      })
    );
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
    setGateRejection(null);
    setVllmGateRejection(null);
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

      // Camera binding gates (camera-registry-sync 8.7, 9.3): every
      // Camera_Input_Node needs a binding on every target device, and
      // every warning needs its confirmation checkbox checked, before
      // the deployment is submitted.
      if (selectedWorkflows.length > 0 && bindingContextError) {
        throw new Error(
          'The camera binding context for the selected workflow ' +
          'component(s) could not be loaded, so camera bindings cannot ' +
          'be validated. Resolve the problem and try again.'
        );
      }
      for (const [workflowId, context] of bindingMatrices) {
        const missing = unboundCells(context, bindingSelections[workflowId] || {});
        if (missing.length > 0) {
          throw new Error(
            `Workflow '${workflowId}' still has unbound camera input ` +
            `node(s): ` +
            missing
              .map(m => `node '${m.nodeId}' on device '${m.device}'`)
              .join('; ')
          );
        }
        const unconfirmed = bindingWarningsFor(workflowId)
          .filter(w => !confirmedWarningIds.has(w.id));
        if (unconfirmed.length > 0) {
          throw new Error(
            'Camera binding warnings require confirmation before the ' +
            'deployment can be created. Review the checkboxes in the ' +
            'camera bindings section.'
          );
        }
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

      // Workflow components with Camera_Input_Nodes are submitted through
      // the workflow deployment path (component_type: workflow) so the
      // backend validates and delivers the Camera_Bindings (8.2, 8.6).
      // Everything else keeps the existing generic path unchanged (8.9,
      // 11.5). The generic deployment goes first: the workflow path
      // revises it, merging its component set with the workflow component.
      if (bindingMatrices.length > 0) {
        const bindingWorkflowIds = new Set(bindingMatrices.map(([id]) => id));
        const otherComponents = deploymentData.components.filter(c => {
          const ref = parseWorkflowComponent(c.component_name, c.component_version);
          return !ref || !bindingWorkflowIds.has(ref.workflowId);
        });

        let lastDeploymentId = '';
        if (otherComponents.length > 0) {
          const genericResponse = await apiService.createDeployment({
            ...deploymentData,
            components: otherComponents,
          });
          lastDeploymentId = genericResponse.deployment_id;
        }

        for (const [workflowId, context] of bindingMatrices) {
          try {
            const workflowResponse = await apiService.createWorkflowDeployment({
              usecase_id: selectedUseCase.value as string,
              workflow_id: workflowId,
              workflow_version: context.workflow_version,
              target_devices: deploymentData.target_devices,
              target_thing_group: deploymentData.target_thing_group,
              deployment_name: deploymentData.deployment_name,
              rollout_config: deploymentData.rollout_config,
              camera_bindings: buildCameraBindings(bindingSelections[workflowId] || {}),
              confirmed_warnings: Array.from(confirmedWarningIds),
            });
            lastDeploymentId = workflowResponse.deployment_id;
          } catch (err) {
            // 409 CAMERA_BINDINGS_INVALID / CAMERA_WARNINGS_UNCONFIRMED,
            // 503 REGISTRY_UNAVAILABLE, 502 BINDING_DELIVERY_FAILED:
            // surface errors and warnings next to the matrix, naming the
            // node and device (8.7, 9.2, 9.3).
            const rejection = err instanceof ApiError
              ? parseCameraBindingRejection(err.code, err.message, err.details)
              : null;
            if (!rejection) {
              throw err;
            }
            setBindingErrors(prev => ({ ...prev, [workflowId]: rejection.errors }));
            if (rejection.warnings.length > 0) {
              setServerBindingWarnings(prev => ({ ...prev, [workflowId]: rejection.warnings }));
            }
            setError(rejection.message);
            scrollToTop();
            return;
          }
        }

        setBindingErrors({});
        setTimeout(() => {
          navigate(`/deployments/${lastDeploymentId}?usecase_id=${selectedUseCase.value}`);
        }, 500);
        return;
      }

      const response = await apiService.createDeployment(deploymentData);
      
      // Navigate to deployment detail page after successful creation
      // Use a small delay to ensure the response is fully processed
      setTimeout(() => {
        navigate(`/deployments/${response.deployment_id}?usecase_id=${selectedUseCase.value}`);
      }, 500);
    } catch (err) {
      // Pre-submit plugin gate rejections carry distinct codes so each
      // Plugin_Component and its lifecycle violation (16.3) or
      // unsupported architecture (16.6) can be identified.
      const rejection = err instanceof ApiError
        ? parsePluginGateRejection(err.code, err.message, err.details)
        : null;
      // 409 VLLM_ARCH_UNSUPPORTED (vllm-triton-inference 3.4): the
      // authoritative backend gate rejected the deployment; itemize the
      // offending (component, device) pairs.
      const vllmRejection = err instanceof ApiError
        ? parseVllmGateRejection(err.code, err.message, err.details)
        : null;
      if (rejection) {
        setGateRejection(rejection);
      } else if (vllmRejection) {
        setVllmGateRejection(vllmRejection);
      } else {
        setError(getErrorMessage(err, 'Failed to create deployment'));
      }
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

            {/* Pre-submit plugin gate rejection (16.3, 16.6) */}
            {gateRejection && (
              <PluginGateRejectionAlert
                rejection={gateRejection}
                onDismiss={() => setGateRejection(null)}
              />
            )}

            {/* Backend vLLM architecture gate rejection
                (vllm-triton-inference 3.4/3.9) */}
            {vllmGateRejection && (
              <Alert
                type="error"
                dismissible
                onDismiss={() => setVllmGateRejection(null)}
                header="Deployment rejected: unsupported device architecture for vLLM"
              >
                <SpaceBetween size="xs">
                  <span>{vllmGateRejection.message}</span>
                  <ul style={{ margin: 0, paddingLeft: '20px' }}>
                    {vllmGateRejection.unsupported.map((entry, i) => (
                      <li key={`${entry.component}-${entry.device}-${i}`}>
                        {describeVllmArchEntry(entry)}
                      </li>
                    ))}
                  </ul>
                </SpaceBetween>
              </Alert>
            )}

            {/* Client-side vLLM incompatibility warning before submit
                (vllm-triton-inference 15.3, Requirement 3.9): each
                selected device incompatible with a selected vLLM model
                component, with its recorded architecture (or absence)
                and the component's supported set. The backend gate is
                authoritative and will reject the deployment. */}
            {vllmArchWarnings.length > 0 && (
              <Alert
                type="warning"
                header="Selected devices are incompatible with vLLM model component(s)"
              >
                <SpaceBetween size="xs">
                  <span>
                    The following target devices do not support the selected
                    vLLM model component(s). Submitting will be rejected by
                    deployment validation.
                  </span>
                  <ul style={{ margin: 0, paddingLeft: '20px' }}>
                    {vllmArchWarnings.map((entry, i) => (
                      <li key={`${entry.component}-${entry.device}-${i}`}>
                        {describeVllmArchEntry(entry)}
                      </li>
                    ))}
                  </ul>
                </SpaceBetween>
              </Alert>
            )}

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

                {/* Selected device(s) with no recorded DDA Target_Architecture
                    (device-arch-compatibility Req 3.5): gated components fail
                    closed for these devices, so the architecture must be
                    recorded (via the device's Target Architecture editor, or
                    automatically through Quick Setup) before they can be
                    deployed. */}
                {devicesWithoutRecordedArch.length > 0 && (
                  <Alert
                    type="warning"
                    header="Selected device(s) have no recorded architecture"
                  >
                    <SpaceBetween size="xs">
                      <span>
                        The following selected device(s) have no recorded DDA
                        Target Architecture. Architecture-gated components
                        (vLLM model and Node plugin components) are hidden for
                        these devices and cannot be deployed until their
                        architecture is recorded.
                      </span>
                      <ul style={{ margin: 0, paddingLeft: '20px' }}>
                        {devicesWithoutRecordedArch.map(deviceId => (
                          <li key={deviceId}>{deviceId}</li>
                        ))}
                      </ul>
                    </SpaceBetween>
                  </Alert>
                )}

                {/* Gated components excluded for Target_Architecture
                    incompatibility, each with an explainable reason (device
                    arch(s) vs supported set) so the exclusion is discoverable
                    rather than silent (device-arch-compatibility Req 3.3, 3.4). */}
                {incompatibleGatedComponents.length > 0 && (
                  <ExpandableSection
                    headerText={`Incompatible with the selected device(s) (${incompatibleGatedComponents.length})`}
                  >
                    <SpaceBetween size="xs">
                      <Box color="text-body-secondary" fontSize="body-s">
                        These components are not offered because they do not
                        support the selected device(s)' recorded architecture.
                        The backend deployment gate would reject them.
                      </Box>
                      <ul style={{ margin: 0, paddingLeft: '20px' }}>
                        {incompatibleGatedComponents.map(({ component, reason }) => (
                          <li key={component.arn}>{reason}</li>
                        ))}
                      </ul>
                    </SpaceBetween>
                  </ExpandableSection>
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
                    {
                      // Node Designer Plugin_Components (dda.plugin.*):
                      // name, version, backing Lifecycle_State badge, and
                      // supported Target_Architecture chips (16.2).
                      id: 'plugins',
                      label: `Node Plugins (${pluginComponents.length})`,
                      content: (
                        <SpaceBetween size="s">
                          {pluginComponents.length === 0 ? (
                            <Box color="text-body-secondary" padding="s">
                              No custom node plugin components found. Build a plugin in the
                              Node Designer to make it deployable here.
                            </Box>
                          ) : (
                            <ColumnLayout columns={2} variant="text-grid">
                              {pluginComponents.map(comp => {
                                const isSelected = selectedComponents.some(c => c.arn === comp.arn);
                                const displayName = getComponentDisplayName(comp.component_name);
                                const version = comp.latest_version?.componentVersion || 'latest';

                                return (
                                  <Box key={comp.arn} padding="s" variant="div">
                                    <SpaceBetween size="xxs">
                                      <SpaceBetween direction="horizontal" size="xs">
                                        <Box fontWeight="bold">{displayName}</Box>
                                        <Badge color="blue">Plugin</Badge>
                                        <LifecycleBadge state={comp.lifecycle_state} />
                                      </SpaceBetween>
                                      <Box color="text-body-secondary" fontSize="body-s">
                                        v{version} • {comp.component_name}
                                      </Box>
                                      <ArchitectureChips
                                        architectures={comp.supported_architectures || []}
                                      />
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
                  ]}
                />

                {/* Selected components table */}
                {selectedComponents.length > 0 && (
                  <SpaceBetween size="xs">
                    {existingDeployment && selectedComponents.some(c => {
                      const latest = getLatestVersionFor(c);
                      return latest && latest !== c.component_version
                        && c.component_version !== '0.0.0' && c.component_version !== 'unknown' && c.component_version !== 'latest';
                    }) && (
                      <Box>
                        <Button
                          iconName="upload"
                          onClick={() => {
                            setSelectedComponents(prev => prev.map(c => {
                              const latest = getLatestVersionFor(c);
                              const updatable = latest && latest !== c.component_version
                                && c.component_version !== '0.0.0' && c.component_version !== 'unknown' && c.component_version !== 'latest';
                              return updatable ? { ...c, component_version: latest as string } : c;
                            }));
                          }}
                        >
                          Update all to latest
                        </Button>
                      </Box>
                    )}
                    <Table
                      resizableColumns
                      items={selectedComponents}
                      columnDefinitions={[
                      {
                        id: 'name',
                        header: 'Component',
                        cell: item => (
                          <SpaceBetween size="xxs">
                            <SpaceBetween direction="horizontal" size="xs">
                              <span>{item.displayName || item.component_name}</span>
                              {item.category === 'Model Components' && <Badge color="green">Model</Badge>}
                              {item.is_plugin_component && (
                                <>
                                  <Badge color="blue">Plugin</Badge>
                                  <LifecycleBadge state={item.lifecycle_state} />
                                </>
                              )}
                            </SpaceBetween>
                            {/* Now-incompatible pre-loaded gated component
                                (revise mode) surfaced without being dropped
                                (device-arch-compatibility Req 4.1-4.3). */}
                            {selectedComponentArchIssues[item.arn] && (
                              <StatusIndicator type="warning">
                                {selectedComponentArchIssues[item.arn]}
                              </StatusIndicator>
                            )}
                          </SpaceBetween>
                        ),
                      },
                      {
                        id: 'architectures',
                        header: 'Architectures',
                        cell: item =>
                          item.is_plugin_component ? (
                            <ArchitectureChips architectures={item.supported_architectures || []} />
                          ) : (
                            <Box color="text-body-secondary">—</Box>
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
                          const latest = getLatestVersionFor(item);
                          const updateAvailable = latest && latest !== item.component_version
                            && item.component_version !== '0.0.0' && item.component_version !== 'unknown' && item.component_version !== 'latest';
                          return (
                            <SpaceBetween direction="horizontal" size="xs">
                              <span>{displayVersion}</span>
                              {updateAvailable && (
                                <Badge color="blue">update available: v{latest}</Badge>
                              )}
                            </SpaceBetween>
                          );
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
                        cell: item => {
                          const latest = getLatestVersionFor(item);
                          const updateAvailable = latest && latest !== item.component_version
                            && item.component_version !== '0.0.0' && item.component_version !== 'unknown' && item.component_version !== 'latest';
                          return (
                            <SpaceBetween direction="horizontal" size="xs">
                              {updateAvailable && (
                                <Button
                                  variant="normal"
                                  iconName="upload"
                                  onClick={() => handleUpdateToLatest(item.arn)}
                                >
                                  Update to latest
                                </Button>
                              )}
                              <Button
                                variant="normal"
                                iconName="remove"
                                onClick={() => handleRemoveComponent(item.arn)}
                              >
                                Remove
                              </Button>
                            </SpaceBetween>
                          );
                        },
                      },
                    ]}
                    empty={<Box textAlign="center">No components selected</Box>}
                  />
                  </SpaceBetween>
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

            {/* Camera binding matrix (camera-registry-sync 12.1): shown
                only when a selected workflow component's version has
                Camera_Input_Nodes (8.9). */}
            {bindingContextError && (
              <Alert type="error" header="Camera binding context unavailable">
                {bindingContextError}
              </Alert>
            )}
            {bindingMatrices.map(([workflowId, context]) => (
              <CameraBindingMatrix
                key={workflowId}
                context={context}
                selections={bindingSelections[workflowId] || {}}
                onCellChange={(device, nodeId, cell) =>
                  handleBindingCellChange(workflowId, device, nodeId, cell)}
                warnings={bindingWarningsFor(workflowId)}
                confirmedWarningIds={confirmedWarningIds}
                onToggleWarning={handleToggleBindingWarning}
                errors={bindingErrors[workflowId] || []}
              />
            ))}

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
