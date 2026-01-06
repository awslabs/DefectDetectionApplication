/**
 * Type definitions for Edge CV Portal
 */

export interface User {
  user_id: string;
  email: string;
  username: string;
  role: UserRole;
  is_super_user: boolean;
  use_cases?: UseCase[];
}

export type UserRole = 'PortalAdmin' | 'UseCaseAdmin' | 'DataScientist' | 'Operator' | 'Viewer';

export interface UseCase {
  usecase_id: string;
  name: string;
  account_id: string;
  s3_bucket: string;
  s3_prefix?: string;
  cross_account_role_arn: string;
  sagemaker_execution_role_arn: string;
  external_id: string;
  owner: string;
  cost_center?: string;
  default_device_group?: string;
  created_at: number;
  updated_at: number;
  tags?: Record<string, string>;
  // Data Account fields (optional - for separate data storage)
  data_account_id?: string;
  data_account_role_arn?: string;
  data_account_external_id?: string;
  data_s3_bucket?: string;
  data_s3_prefix?: string;
}

export interface Device {
  device_id: string;
  usecase_id: string;
  thing_name: string;
  thing_arn?: string;
  thing_type?: string;
  status: string;
  last_status_update?: string;
  last_heartbeat?: number;
  greengrass_version?: string;
  platform?: string;
  architecture?: string;
  attributes?: Record<string, string>;
  tags?: Record<string, string>;
  installed_components?: InstalledComponent[];
  deployments?: DeviceDeployment[];
  // Legacy fields for backward compatibility
  components?: ComponentInfo[];
  storage_used?: number;
  storage_total?: number;
  camera_status?: string;
  metadata?: Record<string, any>;
  created_at?: number;
  updated_at?: number;
}

export interface InstalledComponent {
  componentName: string;
  componentVersion: string;
  lifecycleState: string;
  lifecycleStateDetails?: string;
  isRoot: boolean;
  lastStatusChangeTimestamp?: string;
  lastInstallationSource?: string;
  lastReportedTimestamp?: string;
}

export interface DeviceDeployment {
  deploymentId: string;
  deploymentName?: string;
  iotJobId?: string;
  iotJobArn?: string;
  targetArn?: string;
  coreDeviceExecutionStatus: string;
  reason?: string;
  creationTimestamp?: string;
  modifiedTimestamp?: string;
}

export interface ComponentInfo {
  name: string;
  version: string;
  status: string;
}

export interface ApiResponse<T> {
  data?: T;
  error?: string;
  message?: string;
}

export interface Model {
  model_id: string;
  usecase_id: string;
  name: string;
  version: string;
  stage: 'candidate' | 'staging' | 'production';
  training_job_id: string;
  dataset_manifest_id: string;
  metrics: Record<string, number>;
  component_arns: Record<string, string>; // target -> arn
  deployed_devices: string[];
  created_by: string;
  created_at: number;
  promoted_at?: number;
  promoted_by?: string;
}

export interface CompilationJob {
  target: string;
  compilation_job_name: string;
  compilation_job_arn: string;
  status: 'InProgress' | 'Completed' | 'Failed' | 'Stopped' | 'INPROGRESS' | 'COMPLETED' | 'FAILED' | 'STOPPED';
  compiled_model_s3?: string;
  failure_reason?: string;
  error?: string;
  created_at?: number;
  completed_at?: number;
}

export interface PackagedComponent {
  target: string;
  component_package_s3?: string;
  status: 'packaged' | 'failed';
  error?: string;
}

export interface GreengrassComponent {
  component_name: string;
  component_version: string;
  component_arn: string;
  target_architecture: string;
  status: 'creating' | 'active' | 'failed';
  created_at: number;
  deployment_count: number;
}

export type CompilationStatus = 'not-started' | 'in-progress' | 'completed' | 'failed' | 'partial';

export interface TrainingJob {
  training_id: string;
  usecase_id: string;
  model_name: string;
  model_version: string;
  dataset_manifest_s3: string;
  algorithm_uri: string;
  hyperparameters: Record<string, any>;
  instance_type: string;
  training_job_arn: string;
  status: 'Pending' | 'InProgress' | 'Completed' | 'Failed' | 'Stopped';
  progress?: number;
  metrics: Record<string, number>;
  artifact_s3: string;
  created_by: string;
  created_at: number;
  completed_at?: number;
  logs_url?: string;
  compilation_jobs?: CompilationJob[];
  compilation_status?: CompilationStatus;
  packaged_components?: PackagedComponent[];
  greengrass_components?: GreengrassComponent[];
}

export interface LabelingJob {
  job_id: string;
  usecase_id: string;
  name: string;
  manifest_s3: string;
  output_s3: string;
  task_type: 'ObjectDetection' | 'Classification' | 'Segmentation';
  images_count: number;
  labeled_count: number;
  status: 'pending' | 'in_progress' | 'completed' | 'failed';
  progress_percent: number;
  ground_truth_job_arn: string;
  workforce_type: string;
  created_by: string;
  created_at: number;
  completed_at?: number;
}

export interface S3Dataset {
  prefix: string;
  image_count: number;
  last_modified: number;
}

export interface Deployment {
  deployment_id: string;
  usecase_id: string;
  component_arn: string;
  component_version: string;
  target_devices: string[];
  target_groups: string[];
  rollout_strategy: 'all-at-once' | 'canary' | 'percentage';
  rollout_config?: RolloutConfig;
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'rolled_back';
  device_statuses: Record<string, string>;
  greengrass_deployment_id: string;
  created_by: string;
  created_at: number;
  completed_at?: number;
}

export interface RolloutConfig {
  canarySize?: number;
  canaryPercentage?: number;
  failureThreshold?: number;
}

export interface Config {
  apiUrl: string;
  userPoolId: string;
  userPoolClientId: string;
  region: string;
}

// Greengrass Component Types
export interface Component {
  arn: string;
  component_name: string;
  latest_version: ComponentVersion;
  description: string;
  publisher: string;
  creation_timestamp: string;
  status: string;
  platforms: ComponentPlatform[];
  tags: Record<string, string>;
  component_type: string;
  deployment_info: ComponentDeploymentInfo;
  model_name?: string;
  training_job_id?: string;
  created_by_portal?: boolean;
}

export interface ComponentVersion {
  arn: string;
  componentName: string;
  componentVersion: string;
  creationTimestamp: string;
  description?: string;
  status: string;
  platforms: ComponentPlatform[];
}

export interface ComponentPlatform {
  name?: string;
  attributes?: Record<string, string>;
}

export interface ComponentDeploymentInfo {
  total_deployments: number;
  active_deployments: number;
  deployed_devices: string[];
  device_count: number;
}

export interface ComponentDetails {
  arn: string;
  component_name: string;
  description: string;
  publisher: string;
  creation_timestamp: string;
  status: string;
  platforms: ComponentPlatform[];
  tags: Record<string, string>;
  component_type: string;
  deployment_info: ComponentDeploymentInfo;
  versions: ComponentVersion[];
  recipe: ComponentRecipe;
  model_name?: string;
  training_job_id?: string;
  created_by_portal?: boolean;
}

export interface ComponentRecipe {
  RecipeFormatVersion: string;
  ComponentName: string;
  ComponentVersion: string;
  ComponentType: string;
  ComponentPublisher?: string;
  ComponentConfiguration?: {
    DefaultConfiguration?: Record<string, any>;
  };
  ComponentDependencies?: Record<string, ComponentDependency>;
  Manifests?: ComponentManifest[];
  Lifecycle?: Record<string, any>;
}

export interface ComponentDependency {
  VersionRequirement: string;
  DependencyType: string;
}

export interface ComponentManifest {
  Platform: ComponentPlatform;
  Lifecycle?: Record<string, ComponentLifecycleStep>;
  Artifacts?: ComponentArtifact[];
}

export interface ComponentLifecycleStep {
  Script?: string;
  Timeout?: number;
  requiresPrivilege?: boolean;
  runWith?: {
    posixUser?: string;
    windowsUser?: string;
  };
}

export interface ComponentArtifact {
  Uri: string;
  Digest?: string;
  Algorithm?: string;
  Unarchive?: string;
  Permission?: {
    Read?: string;
    Execute?: string;
  };
}
