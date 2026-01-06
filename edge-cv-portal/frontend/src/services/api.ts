/**
 * API service for making HTTP requests to the backend
 */
import { getConfig } from '../config';
import { UseCase, Device, User } from '../types';

class ApiService {
  private get baseUrl(): string {
    return getConfig().apiUrl;
  }

  private async request<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
    const token = localStorage.getItem('idToken');
    
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(options.headers as Record<string, string>),
    };

    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    try {
      const response = await fetch(`${this.baseUrl}${endpoint}`, {
        ...options,
        headers,
      });

      if (!response.ok) {
        // If 401, token might be expired - redirect to login
        if (response.status === 401) {
          console.error('Authentication failed - token may be expired');
          localStorage.removeItem('idToken');
          // Redirect to login page
          if (window.location.pathname !== '/login') {
            window.location.href = '/login';
          }
        }
        
        const error = await response.json().catch(() => ({ error: 'Request failed' }));
        throw new Error(error.error || `HTTP ${response.status}`);
      }

      return response.json();
    } catch (err: any) {
      // Handle Amplify/AWS errors that have a complex structure
      if (err && typeof err === 'object' && 'message' in err) {
        if (typeof err.message === 'string') {
          throw new Error(err.message);
        }
      }
      // Handle errors with nested structure
      if (err && typeof err === 'object' && 'errors' in err && Array.isArray(err.errors)) {
        const messages = err.errors.map((e: any) => e?.message || String(e)).join(', ');
        throw new Error(messages || 'Request failed');
      }
      // Re-throw if it's already an Error
      if (err instanceof Error) {
        throw err;
      }
      // Fallback
      throw new Error('Request failed');
    }
  }

  // Auth endpoints
  async getCurrentUser(): Promise<{ user: User }> {
    return this.request<{ user: User }>('/auth/me');
  }

  // UseCase endpoints
  async listUseCases(): Promise<{ usecases: UseCase[]; count: number }> {
    return this.request<{ usecases: UseCase[]; count: number }>('/usecases');
  }

  // Dataset endpoints
  async listDatasets(params: {
    usecase_id: string;
    prefix?: string;
    max_depth?: number;
  }): Promise<{
    datasets: Array<{
      prefix: string;
      image_count: number;
      last_modified: string | null;
      has_subdirectories: boolean;
    }>;
    bucket: string;
    base_prefix: string;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      ...(params.prefix && { prefix: params.prefix }),
      ...(params.max_depth && { max_depth: params.max_depth.toString() }),
    });
    return this.request(`/datasets?${queryParams}`);
  }

  async countImages(params: {
    usecase_id: string;
    prefix: string;
  }): Promise<{
    prefix: string;
    image_count: number;
    sample_images: Array<{
      key: string;
      size: number;
      last_modified: string;
    }>;
    bucket: string;
  }> {
    return this.request('/datasets/count', {
      method: 'POST',
      body: JSON.stringify(params),
    });
  }

  async getImagePreview(params: {
    usecase_id: string;
    prefix: string;
    limit?: number;
  }): Promise<{
    prefix: string;
    bucket: string;
    total_found: number;
    images: Array<{
      key: string;
      filename: string;
      size: number;
      last_modified: string;
      presigned_url: string;
    }>;
    expires_in_seconds: number;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      prefix: params.prefix,
      ...(params.limit && { limit: params.limit.toString() }),
    });
    return this.request(`/datasets/preview?${queryParams}`);
  }

  async getUseCase(id: string): Promise<{ usecase: UseCase }> {
    return this.request<{ usecase: UseCase }>(`/usecases/${id}`);
  }

  async createUseCase(data: Partial<UseCase>): Promise<{ usecase: UseCase; message: string }> {
    return this.request<{ usecase: UseCase; message: string }>('/usecases', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async updateUseCase(id: string, data: Partial<UseCase>): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/usecases/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  async deleteUseCase(id: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/usecases/${id}`, {
      method: 'DELETE',
    });
  }

  // Device endpoints
  async listDevices(usecaseId: string): Promise<{ devices: Device[]; count: number }> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<{ devices: Device[]; count: number }>(`/devices?usecase_id=${usecaseId}`);
  }

  async getDevice(id: string, usecaseId: string): Promise<{ device: Device }> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<{ device: Device }>(`/devices/${id}?usecase_id=${usecaseId}`);
  }

  // Training endpoints
  async listTrainingJobs(usecaseId?: string): Promise<{ jobs: any[]; count: number }> {
    const query = usecaseId ? `?usecase_id=${usecaseId}` : '';
    return this.request<{ jobs: any[]; count: number }>(`/training${query}`);
  }

  async getTrainingJob(id: string): Promise<any> {
    return this.request<any>(`/training/${id}`);
  }

  // Workteams endpoints
  async listWorkteams(usecaseId: string): Promise<{
    workteams: Array<{
      name: string;
      arn: string;
      description: string;
      member_count: number;
    }>;
    count: number;
  }> {
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    return this.request(`/workteams?${queryParams}`);
  }

  // Labeling endpoints
  async listLabelingJobs(params: {
    usecase_id: string;
    status?: string;
  }): Promise<{
    jobs: Array<{
      job_id: string;
      job_name: string;
      status: string;
      task_type: string;
      image_count: number;
      labeled_objects?: number;
      progress_percent?: number;
      created_at: number;
      updated_at: number;
    }>;
    count: number;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      ...(params.status && { status: params.status }),
    });
    return this.request(`/labeling?${queryParams}`);
  }

  async createLabelingJob(data: {
    usecase_id: string;
    job_name: string;
    dataset_prefix: string;
    task_type: string;
    label_categories: string[];
    workforce_arn: string;
    instructions?: string;
    num_workers_per_object?: number;
    task_time_limit?: number;
  }): Promise<{
    job_id: string;
    sagemaker_job_name: string;
    status: string;
    message: string;
  }> {
    return this.request('/labeling', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async getLabelingJob(jobId: string): Promise<{
    job: {
      job_id: string;
      usecase_id: string;
      job_name: string;
      sagemaker_job_name: string;
      status: string;
      task_type: string;
      dataset_prefix: string;
      image_count: number;
      label_categories: string[];
      labeled_objects?: number;
      human_labeled?: number;
      machine_labeled?: number;
      failed_objects?: number;
      progress_percent?: number;
      manifest_s3_uri: string;
      output_s3_uri: string;
      output_manifest_s3_uri?: string;
      workforce_arn: string;
      created_at: number;
      created_by: string;
      updated_at: number;
      completed_at?: number;
      failure_reason?: string;
    };
  }> {
    return this.request(`/labeling/${jobId}`);
  }

  async getLabelingJobManifest(jobId: string): Promise<{
    manifest_uri: string;
    job_id: string;
  }> {
    return this.request(`/labeling/${jobId}/manifest`);
  }

  async createTrainingJob(data: {
    usecase_id: string;
    model_name: string;
    model_version: string;
    model_type: string;
    dataset_manifest_s3: string;
    instance_type: string;
    max_runtime_seconds?: number;
    hyperparameters?: Record<string, any>;
    auto_compile?: boolean;
    compilation_targets?: string[];
  }): Promise<{ training_job_id: string; message: string }> {
    return this.request<{ training_job_id: string; message: string }>('/training', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async getTrainingLogs(id: string, nextToken?: string): Promise<{ 
    training_id: string;
    training_job_name: string;
    logs: Array<{
      timestamp: number;
      message: string;
      ingestionTime?: number;
    }>;
    nextForwardToken?: string;
    nextBackwardToken?: string;
    message?: string;
  }> {
    const query = nextToken ? `?nextToken=${nextToken}` : '';
    return this.request(`/training/${id}/logs${query}`);
  }

  async downloadTrainingLogs(id: string): Promise<string> {
    const token = localStorage.getItem('idToken');
    const headers: Record<string, string> = {};
    
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    const response = await fetch(`${this.baseUrl}/training/${id}/logs/download`, {
      headers,
    });

    if (!response.ok) {
      throw new Error(`Failed to download logs: ${response.statusText}`);
    }

    return response.text();
  }

  async stopTrainingJob(id: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/training/${id}/stop`, {
      method: 'POST',
    });
  }

  // Compilation endpoints
  async startCompilation(trainingId: string, targets: string[]): Promise<{
    training_id: string;
    compilation_jobs: Array<{
      target: string;
      compilation_job_name: string;
      compilation_job_arn: string;
      status: string;
    }>;
    message: string;
  }> {
    return this.request(`/training/${trainingId}/compile`, {
      method: 'POST',
      body: JSON.stringify({ targets }),
    });
  }

  async getCompilationStatus(trainingId: string): Promise<{
    training_id: string;
    compilation_jobs: Array<{
      target: string;
      compilation_job_name: string;
      compilation_job_arn: string;
      status: string;
      compiled_model_s3?: string;
      failure_reason?: string;
      error?: string;
    }>;
  }> {
    return this.request(`/training/${trainingId}/compile`);
  }

  // Packaging endpoints
  async startPackaging(trainingId: string, targets?: string[]): Promise<{
    training_id: string;
    packaged_components: Array<{
      target: string;
      component_package_s3?: string;
      status: string;
      error?: string;
    }>;
    message: string;
  }> {
    return this.request(`/training/${trainingId}/package`, {
      method: 'POST',
      body: JSON.stringify({ targets }),
    });
  }

  // Greengrass publish endpoints
  async publishGreengrassComponent(
    trainingId: string,
    componentName: string,
    componentVersion: string,
    friendlyName?: string,
    targets?: string[]
  ): Promise<{
    training_id: string;
    component_name: string;
    component_version: string;
    published_components: Array<{
      target: string;
      platform: string;
      component_name: string;
      component_version: string;
      component_arn?: string;
      status: string;
      error?: string;
    }>;
    message: string;
  }> {
    return this.request(`/training/${trainingId}/publish`, {
      method: 'POST',
      body: JSON.stringify({
        component_name: componentName,
        component_version: componentVersion,
        friendly_name: friendlyName,
        targets,
      }),
    });
  }

  // Pre-labeled datasets endpoints
  async listPreLabeledDatasets(usecaseId: string): Promise<{
    datasets: Array<{
      dataset_id: string;
      usecase_id: string;
      name: string;
      description?: string;
      manifest_s3_uri: string;
      image_count: number;
      label_attribute: string;
      label_stats: Record<string, number>;
      task_type: string;
      created_at: number;
      created_by: string;
      updated_at: number;
    }>;
    count: number;
  }> {
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    return this.request(`/datasets/pre-labeled?${queryParams}`);
  }

  async getPreLabeledDataset(datasetId: string): Promise<{
    dataset: {
      dataset_id: string;
      usecase_id: string;
      name: string;
      description?: string;
      manifest_s3_uri: string;
      image_count: number;
      label_attribute: string;
      label_stats: Record<string, number>;
      task_type: string;
      created_at: number;
      created_by: string;
      updated_at: number;
    };
  }> {
    return this.request(`/datasets/pre-labeled/${datasetId}`);
  }

  async createPreLabeledDataset(data: {
    usecase_id: string;
    name: string;
    description?: string;
    manifest_s3_uri: string;
    task_type: string;
    label_attribute: string;
    image_count: number;
    label_stats: Record<string, number>;
    created_by: string;
  }): Promise<{
    dataset_id: string;
    message: string;
  }> {
    return this.request('/datasets/pre-labeled', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async deletePreLabeledDataset(datasetId: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/datasets/pre-labeled/${datasetId}`, {
      method: 'DELETE',
    });
  }

  async validateManifest(data: {
    usecase_id: string;
    manifest_s3_uri: string;
  }): Promise<{
    valid: boolean;
    errors: string[];
    warnings: string[];
    stats: {
      total_images: number;
      task_type: string;
      label_distribution: Record<string, number>;
      sample_entries: any[];
    };
  }> {
    return this.request('/datasets/validate-manifest', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Component endpoints
  async listComponents(params: {
    usecase_id: string;
    scope?: 'PRIVATE' | 'PUBLIC';
    search?: string;
    sort_by?: 'component_name' | 'creation_timestamp';
    sort_order?: 'asc' | 'desc';
  }): Promise<{
    components: Array<{
      arn: string;
      component_name: string;
      latest_version: {
        arn: string;
        componentName: string;
        componentVersion: string;
        creationTimestamp: string;
        description?: string;
        status: string;
        platforms: Array<{
          name?: string;
          attributes?: Record<string, string>;
        }>;
      };
      description: string;
      publisher: string;
      creation_timestamp: string;
      status: string;
      platforms: Array<{
        name?: string;
        attributes?: Record<string, string>;
      }>;
      tags: Record<string, string>;
      component_type: string;
      deployment_info: {
        total_deployments: number;
        active_deployments: number;
        deployed_devices: string[];
        device_count: number;
      };
    }>;
    total_count: number;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      ...(params.scope && { scope: params.scope }),
      ...(params.search && { search: params.search }),
      ...(params.sort_by && { sort_by: params.sort_by }),
      ...(params.sort_order && { sort_order: params.sort_order }),
    });
    return this.request(`/components?${queryParams}`);
  }

  async getComponent(arn: string, usecaseId: string): Promise<{
    arn: string;
    component_name: string;
    description: string;
    publisher: string;
    creation_timestamp: string;
    status: string;
    platforms: Array<{
      name?: string;
      attributes?: Record<string, string>;
    }>;
    tags: Record<string, string>;
    component_type: string;
    versions: Array<{
      arn: string;
      componentName: string;
      componentVersion: string;
      creationTimestamp: string;
      description?: string;
      status: string;
      platforms: Array<{
        name?: string;
        attributes?: Record<string, string>;
      }>;
    }>;
    deployment_info: {
      total_deployments: number;
      active_deployments: number;
      deployed_devices: string[];
      device_count: number;
    };
    recipe: {
      RecipeFormatVersion: string;
      ComponentName: string;
      ComponentVersion: string;
      ComponentType: string;
      ComponentPublisher?: string;
      ComponentConfiguration?: {
        DefaultConfiguration?: Record<string, any>;
      };
      ComponentDependencies?: Record<string, {
        VersionRequirement: string;
        DependencyType: string;
      }>;
      Manifests?: Array<{
        Platform: {
          name?: string;
          attributes?: Record<string, string>;
        };
        Lifecycle?: Record<string, {
          Script?: string;
          Timeout?: number;
          requiresPrivilege?: boolean;
          runWith?: {
            posixUser?: string;
            windowsUser?: string;
          };
        }>;
        Artifacts?: Array<{
          Uri: string;
          Digest?: string;
          Algorithm?: string;
          Unarchive?: string;
          Permission?: {
            Read?: string;
            Execute?: string;
          };
        }>;
      }>;
      Lifecycle?: Record<string, any>;
    };
  }> {
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    return this.request(`/components/${encodeURIComponent(arn)}?${queryParams}`);
  }

  async deleteComponent(arn: string, usecaseId: string): Promise<{ message: string }> {
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    return this.request<{ message: string }>(`/components/${encodeURIComponent(arn)}?${queryParams}`, {
      method: 'DELETE',
    });
  }

  // Deployment endpoints
  async listDeployments(usecaseId: string): Promise<{
    deployments: Array<{
      deployment_id: string;
      deployment_name: string;
      target_arn: string;
      revision_id: string;
      deployment_status: string;
      is_latest_for_target: boolean;
      creation_timestamp: string;
      usecase_id: string;
    }>;
    count: number;
  }> {
    return this.request(`/deployments?usecase_id=${usecaseId}`);
  }

  async getDeployment(deploymentId: string, usecaseId: string): Promise<{
    deployment: {
      deployment_id: string;
      deployment_name: string;
      target_arn: string;
      revision_id: string;
      deployment_status: string;
      iot_job_id: string;
      iot_job_arn: string;
      is_latest_for_target: boolean;
      creation_timestamp: string;
      components: Array<{
        component_name: string;
        component_version: string;
        configuration_update: Record<string, unknown>;
      }>;
      deployment_policies: Record<string, unknown>;
      tags: Record<string, string>;
      usecase_id: string;
    };
  }> {
    return this.request(`/deployments/${deploymentId}?usecase_id=${usecaseId}`);
  }

  async createDeployment(data: {
    usecase_id: string;
    deployment_name?: string;
    components: Array<{
      component_name: string;
      component_version: string;
    }>;
    target_devices?: string[];
    target_thing_group?: string;
    rollout_config?: {
      auto_rollback?: boolean;
      timeout_seconds?: number;
    };
  }): Promise<{
    deployment_id: string;
    iot_job_id: string;
    iot_job_arn: string;
    message: string;
  }> {
    return this.request('/deployments', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async cancelDeployment(deploymentId: string, usecaseId: string): Promise<{
    message: string;
    deployment_id: string;
  }> {
    return this.request(`/deployments/${deploymentId}?usecase_id=${usecaseId}`, {
      method: 'DELETE',
    });
  }

  async createDeploymentFromComponent(data: {
    usecase_id: string;
    component_arn: string;
    component_version: string;
    target_devices?: string[];
    target_groups?: string[];
    rollout_strategy: 'all-at-once' | 'canary' | 'percentage';
    rollout_config?: {
      canarySize?: number;
      canaryPercentage?: number;
      failureThreshold?: number;
    };
  }): Promise<{
    deployment_id: string;
    message: string;
  }> {
    return this.request('/deployments', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Data Management endpoints
  async listDataBuckets(usecaseId: string): Promise<{
    buckets: Array<{
      name: string;
      creation_date?: string;
      region: string;
      tags?: Record<string, string>;
      is_configured?: boolean;
    }>;
    current_data_bucket: string | null;
    target_account?: string;
    has_data_account_role?: boolean;
    message?: string;
  }> {
    return this.request(`/usecases/${usecaseId}/data/buckets`);
  }

  async createDataBucket(usecaseId: string, data: {
    bucket_name: string;
    region?: string;
    enable_versioning?: boolean;
    encryption?: string;
  }): Promise<{
    bucket_name: string;
    region: string;
    arn: string;
    created: boolean;
    versioning_enabled: boolean;
    encryption: string;
  }> {
    return this.request(`/usecases/${usecaseId}/data/buckets`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async listDataFolders(usecaseId: string, params: {
    bucket?: string;
    prefix?: string;
  }): Promise<{
    bucket: string;
    prefix: string;
    folders: Array<{ name: string; path: string }>;
    files: Array<{
      name: string;
      key: string;
      size: number;
      last_modified: string;
    }>;
    is_truncated: boolean;
  }> {
    const queryParams = new URLSearchParams();
    if (params.bucket) queryParams.set('bucket', params.bucket);
    if (params.prefix) queryParams.set('prefix', params.prefix);
    const query = queryParams.toString() ? `?${queryParams}` : '';
    return this.request(`/usecases/${usecaseId}/data/folders${query}`);
  }

  async createDataFolder(usecaseId: string, data: {
    bucket?: string;
    folder_path: string;
  }): Promise<{
    bucket: string;
    folder_path: string;
    created: boolean;
  }> {
    return this.request(`/usecases/${usecaseId}/data/folders`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async getUploadUrl(usecaseId: string, data: {
    bucket?: string;
    key: string;
    content_type?: string;
    expires_in?: number;
  }): Promise<{
    upload_url: string;
    bucket: string;
    key: string;
    expires_in: number;
  }> {
    return this.request(`/usecases/${usecaseId}/data/upload-url`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async getBatchUploadUrls(usecaseId: string, data: {
    bucket?: string;
    prefix?: string;
    files: Array<{ filename: string; content_type?: string }>;
    expires_in?: number;
  }): Promise<{
    bucket: string;
    prefix: string;
    uploads: Array<{
      filename: string;
      key: string;
      upload_url: string;
      content_type: string;
      error?: string;
    }>;
    expires_in: number;
  }> {
    return this.request(`/usecases/${usecaseId}/data/batch-upload-urls`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async configureDataAccount(usecaseId: string, data: {
    data_account_id?: string;
    data_account_role_arn?: string;
    data_account_external_id?: string;
    data_s3_bucket?: string;
    data_s3_prefix?: string;
  }): Promise<{
    message: string;
    usecase_id: string;
  }> {
    return this.request(`/usecases/${usecaseId}/data/configure`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }
}

export const apiService = new ApiService();
export default apiService;
