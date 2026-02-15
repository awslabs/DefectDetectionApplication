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

  // Shared Components endpoints
  async provisionSharedComponents(usecaseId: string, componentVersion?: string): Promise<{
    usecase_id: string;
    components: Array<{
      component_name: string;
      component_version?: string;
      component_arn?: string;
      platform: string;
      status: string;
      error?: string;
    }>;
    policy_updated: boolean;
    message: string;
  }> {
    return this.request('/shared-components/provision', {
      method: 'POST',
      body: JSON.stringify({
        usecase_id: usecaseId,
        ...(componentVersion && { component_version: componentVersion }),
      }),
    });
  }

  async listAvailableSharedComponents(): Promise<{
    components: Array<{
      component_name: string;
      description: string;
      platform: string;
      platforms: string[];
      source: string;
      latest_version: string;
    }>;
    count: number;
  }> {
    return this.request('/shared-components/available');
  }

  async listSharedComponents(usecaseId: string): Promise<{
    usecase_id: string;
    components: Array<{
      component_name: string;
      component_version: string;
      component_arn: string;
      platform: string;
      status: string;
      update_available?: boolean;
      latest_version?: string;
    }>;
    count: number;
    latest_version: string;
  }> {
    return this.request(`/shared-components?usecase_id=${usecaseId}`);
  }

  async getSharedComponentsStatus(): Promise<{
    usecases: Array<{
      usecase_id: string;
      usecase_name: string;
      account_id: string;
      needs_update: boolean;
      shared_components_provisioned: boolean;
      components: Array<{
        component_name: string;
        current_version: string;
        latest_version: string;
        update_available: boolean;
        status: string;
      }>;
    }>;
    total_usecases: number;
    usecases_needing_update: number;
    latest_version: string;
  }> {
    return this.request('/shared-components/status');
  }

  async updateAllSharedComponents(params?: {
    version?: string;
    usecase_ids?: string[];
  }): Promise<{
    message: string;
    target_version: string;
    results: Array<{
      usecase_id: string;
      usecase_name: string;
      status: string;
      error?: string;
      components?: Array<{
        component_name: string;
        status: string;
        error?: string;
      }>;
    }>;
    success_count: number;
    failed_count: number;
  }> {
    return this.request('/shared-components/update-all', {
      method: 'POST',
      body: JSON.stringify(params || {}),
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

  // Device Logs endpoints
  async getDeviceLogGroups(deviceId: string, usecaseId: string): Promise<{
    device_id: string;
    log_groups: Array<{
      log_group_name: string;
      component_type: 'system' | 'user';
      component_name: string;
      creation_time?: number;
      stored_bytes: number;
      retention_days?: number;
    }>;
    count: number;
  }> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request(`/devices/${deviceId}/logs?usecase_id=${usecaseId}`);
  }

  async getDeviceLogs(
    deviceId: string,
    componentName: string,
    usecaseId: string,
    params?: {
      start_time?: number;
      end_time?: number;
      limit?: number;
      next_token?: string;
      filter_pattern?: string;
    }
  ): Promise<{
    device_id: string;
    component_name: string;
    log_group_name: string;
    logs: Array<{
      timestamp: number;
      message: string;
      log_stream_name: string;
      ingestion_time?: number;
    }>;
    count: number;
    start_time: number;
    end_time: number;
    next_token?: string;
  }> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    if (params?.start_time) queryParams.set('start_time', params.start_time.toString());
    if (params?.end_time) queryParams.set('end_time', params.end_time.toString());
    if (params?.limit) queryParams.set('limit', params.limit.toString());
    if (params?.next_token) queryParams.set('next_token', params.next_token);
    if (params?.filter_pattern) queryParams.set('filter_pattern', params.filter_pattern);
    
    return this.request(`/devices/${deviceId}/logs/${encodeURIComponent(componentName)}?${queryParams}`);
  }

  async analyzeLogs(
    deviceId: string,
    usecaseId: string,
    params?: {
      hours?: number;
    }
  ): Promise<{
    analysis: {
      device_id: string;
      analysis_timestamp: string;
      issues_detected: number;
      critical_count: number;
      high_count: number;
      medium_count: number;
      low_count: number;
      issues: Array<{
        issue_id: string;
        title: string;
        severity: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW';
        likely_causes: string[];
        recommended_actions: string[];
        prevention_tips: string[];
      }>;
      next_steps: string[];
    };
  }> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    if (params?.hours) queryParams.set('hours', params.hours.toString());
    
    return this.request(
      `/devices/${deviceId}/logs/analyze?${queryParams}`,
      { method: 'POST' }
    );
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
    mask_prefix?: string;
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

  async transformManifest(data: {
    usecase_id: string;
    source_manifest_uri: string;
    output_manifest_uri?: string;
    task_type?: 'classification' | 'segmentation';
  }): Promise<{
    message: string;
    transformed_manifest_uri: string;
    stats: {
      total_entries: number;
      transformed: number;
      skipped: number;
      errors: string[];
    };
    detected_attributes: {
      label_attr: string;
      metadata_attr: string;
    };
    dda_attributes: {
      label: string;
      metadata: string;
    };
    sample_entry: any;
  }> {
    return this.request('/labeling/transform-manifest', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async createTrainingJob(data: {
    usecase_id: string;
    model_source?: string;
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

  async browseS3Bucket(usecaseId: string, prefix: string = ''): Promise<{
    bucket: string;
    current_prefix: string;
    breadcrumbs: Array<{ name: string; prefix: string }>;
    folders: Array<{
      name: string;
      prefix: string;
      type: 'folder';
    }>;
    files: Array<{
      name: string;
      key: string;
      size: number;
      size_mb: number;
      last_modified: string;
      type: 'file' | 'manifest' | 'image';
      s3_uri: string;
    }>;
    folder_count: number;
    file_count: number;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: usecaseId,
      prefix: prefix,
    });
    return this.request(`/datasets/pre-labeled/browse?${queryParams}`);
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
    components?: Array<{
      component_name: string;
      component_version: string;
    }>;
    auto_included?: Array<{
      component_name: string;
      component_version: string;
      reason: string;
    }>;
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

  // User Roles / Team Management endpoints
  async listUsecaseUsers(usecaseId: string): Promise<{
    users: Array<{
      user_id: string;
      roles: Array<{
        usecase_id: string;
        role: string;
        assigned_at?: number;
        assigned_by?: string;
      }>;
    }>;
    total_count: number;
  }> {
    return this.request(`/users?usecase_id=${usecaseId}`);
  }

  async assignUserRole(data: {
    user_id: string;
    usecase_id: string;
    role: string;
  }): Promise<{
    message: string;
    user_id: string;
    usecase_id: string;
    role: string;
  }> {
    return this.request('/users/assign-role', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async removeUserRole(userId: string, usecaseId: string): Promise<{
    message: string;
    user_id: string;
    usecase_id: string;
  }> {
    return this.request(`/users/${userId}/roles/${usecaseId}`, {
      method: 'DELETE',
    });
  }

  // Model Import (BYOM) endpoints
  async getModelFormatSpec(): Promise<{
    description: string;
    format: string;
    framework: string;
    required_structure: Record<string, {
      description: string;
      required_fields?: Record<string, string>;
      example?: string;
      notes?: string;
    }>;
    validation_rules: string[];
    supported_compilation_targets: string[];
  }> {
    return this.request('/models/format-spec');
  }

  // Model Registry endpoints
  async listModels(params: {
    usecase_id: string;
    stage?: 'candidate' | 'staging' | 'production';
    source?: 'trained' | 'imported' | 'marketplace';
  }): Promise<{
    models: Array<{
      model_id: string;
      usecase_id: string;
      name: string;
      version: string;
      stage: 'candidate' | 'staging' | 'production';
      source: string;
      training_job_id: string;
      model_type: string;
      metrics: Record<string, number>;
      artifact_s3?: string;
      component_arns: Record<string, string>;
      deployed_devices: string[];
      created_by: string;
      created_at: number;
      updated_at: number;
      description?: string;
      compilation_status?: string;
      packaging_status?: string;
    }>;
    count: number;
    usecase_id: string;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      ...(params.stage && { stage: params.stage }),
      ...(params.source && { source: params.source }),
    });
    return this.request(`/models?${queryParams}`);
  }

  async getModel(modelId: string): Promise<{
    model: {
      model_id: string;
      usecase_id: string;
      name: string;
      version: string;
      stage: 'candidate' | 'staging' | 'production';
      source: string;
      training_job_id: string;
      training_job_name?: string;
      model_type: string;
      description?: string;
      metrics: Record<string, number>;
      artifact_s3?: string;
      component_arns: Record<string, string>;
      deployed_devices: string[];
      created_by: string;
      created_at: number;
      updated_at: number;
      completed_at?: number;
      promoted_at?: number;
      promoted_by?: string;
      compilation_status?: string;
      compilation_jobs?: Array<{
        target: string;
        status: string;
        compiled_model_s3?: string;
      }>;
      packaging_status?: string;
      packaged_components?: Array<{
        target: string;
        status: string;
        component_package_s3?: string;
      }>;
      validation_result?: Record<string, unknown>;
      hyperparameters?: Record<string, unknown>;
      instance_type?: string;
      dataset_manifest_s3?: string;
    };
  }> {
    return this.request(`/models/${modelId}`);
  }

  async updateModelStage(modelId: string, stage: 'candidate' | 'staging' | 'production'): Promise<{
    model_id: string;
    previous_stage: string;
    stage: string;
    message: string;
  }> {
    return this.request(`/models/${modelId}/stage`, {
      method: 'PUT',
      body: JSON.stringify({ stage }),
    });
  }

  async deleteModel(modelId: string): Promise<{
    model_id: string;
    message: string;
  }> {
    return this.request(`/models/${modelId}`, {
      method: 'DELETE',
    });
  }

  async validateModel(data: {
    usecase_id: string;
    model_s3_uri: string;
  }): Promise<{
    valid: boolean;
    model_s3_uri?: string;
    metadata?: {
      image_width: number;
      image_height: number;
      input_shape: number[];
      model_type: string;
      pt_file: string;
      framework: string;
      framework_version: string;
    };
    files_found?: string[];
    warnings?: string[];
    error?: string;
    details?: string[];
  }> {
    return this.request('/models/validate', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async importModel(data: {
    usecase_id: string;
    model_name: string;
    model_version: string;
    model_s3_uri: string;
    description?: string;
    auto_compile?: boolean;
    compilation_targets?: string[];
  }): Promise<{
    training_id: string;
    model_name: string;
    model_version: string;
    status: string;
    source: string;
    validation_result: {
      valid: boolean;
      metadata: {
        image_width: number;
        image_height: number;
        input_shape: number[];
        model_type: string;
        pt_file: string;
        framework: string;
        framework_version: string;
      };
      files_found: string[];
      warnings: string[];
    };
    message: string;
    auto_compile_triggered: boolean;
  }> {
    return this.request('/models/import', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Model Converter endpoints (Smart Import)
  async getSupportedModelTypes(): Promise<{
    model_types: Record<string, {
      description: string;
      output_format: string;
    }>;
    common_dimensions: Record<string, number[]>;
    supported_frameworks: string[];
    framework_versions: string[];
  }> {
    return this.request('/models/types');
  }

  async inspectModel(data: {
    usecase_id: string;
    model_s3_uri: string;
  }): Promise<{
    model_s3_uri: string;
    inspection_result: {
      type: string;
      is_state_dict?: boolean;
      is_jit?: boolean;
      is_full_model?: boolean;
      layers?: string[];
      total_layers?: number;
      input_channels?: number;
      num_classes?: number;
      architecture_hints: string[];
      suggested_type?: string;
      error?: string;
    };
    supported_model_types: Record<string, {
      description: string;
      output_format: string;
    }>;
  }> {
    return this.request('/models/inspect', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async convertModel(data: {
    usecase_id: string;
    model_s3_uri: string;
    model_name: string;
    model_type: string;
    image_width: number;
    image_height: number;
    num_classes?: number;
    class_names?: string[];
    auto_import?: boolean;
  }): Promise<{
    converted_model_s3_uri: string;
    model_name: string;
    model_type: string;
    input_shape: number[];
    model_info: {
      type: string;
      architecture_hints: string[];
      suggested_type?: string;
    };
    message: string;
    import_result?: {
      training_id: string;
      message: string;
    };
    training_id?: string;
    import_error?: string;
  }> {
    return this.request('/models/convert', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Audit Logs endpoints
  async getAuditLogs(params?: {
    usecase_id?: string;
    action?: string;
    user_id?: string;
    start_time?: number;
    end_time?: number;
    limit?: number;
    next_token?: string;
  }): Promise<{
    logs: Array<{
      event_id: string;
      timestamp: number;
      user_id: string;
      usecase_id?: string;
      action: string;
      resource_type: string;
      resource_id: string;
      result: string;
      details?: Record<string, any>;
    }>;
    count: number;
    scanned_count: number;
    next_token?: string;
    available_actions: string[];
    is_admin?: boolean;
  }> {
    const queryParams = new URLSearchParams();
    if (params?.usecase_id) queryParams.set('usecase_id', params.usecase_id);
    if (params?.action) queryParams.set('action', params.action);
    if (params?.user_id) queryParams.set('user_id', params.user_id);
    if (params?.start_time) queryParams.set('start_time', params.start_time.toString());
    if (params?.end_time) queryParams.set('end_time', params.end_time.toString());
    if (params?.limit) queryParams.set('limit', params.limit.toString());
    if (params?.next_token) queryParams.set('next_token', params.next_token);
    
    const query = queryParams.toString() ? `?${queryParams}` : '';
    return this.request(`/audit-logs${query}`);
  }

  // Data Accounts endpoints
  async listDataAccounts(): Promise<{
    data_accounts: Array<{
      data_account_id: string;
      name: string;
      description?: string;
      role_arn: string;
      external_id: string;
      region: string;
      status: string;
      created_at: number;
      created_by: string;
      updated_at: number;
      connection_test?: {
        status: string;
        message: string;
      };
      last_tested_at?: number;
    }>;
    count: number;
  }> {
    return this.request('/data-accounts');
  }

  async getDataAccount(accountId: string): Promise<{
    data_account: {
      data_account_id: string;
      name: string;
      description?: string;
      role_arn: string;
      external_id: string;
      region: string;
      status: string;
      created_at: number;
      created_by: string;
      updated_at: number;
      connection_test?: {
        status: string;
        message: string;
      };
      last_tested_at?: number;
    };
  }> {
    return this.request(`/data-accounts/${accountId}`);
  }

  async createDataAccount(data: {
    data_account_id: string;
    name: string;
    description?: string;
    role_arn: string;
    external_id: string;
    region: string;
  }): Promise<{
    data_account_id: string;
    message: string;
  }> {
    return this.request('/data-accounts', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async updateDataAccount(accountId: string, data: {
    name?: string;
    description?: string;
    role_arn?: string;
    external_id?: string;
    region?: string;
  }): Promise<{
    data_account_id: string;
    message: string;
  }> {
    return this.request(`/data-accounts/${accountId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  async deleteDataAccount(accountId: string): Promise<{
    message: string;
  }> {
    return this.request(`/data-accounts/${accountId}`, {
      method: 'DELETE',
    });
  }

  async testDataAccountConnection(accountId: string): Promise<{
    result: {
      status: string;
      message: string;
      error?: string;
    };
  }> {
    return this.request(`/data-accounts/${accountId}/test`, {
      method: 'POST',
    });
  }

  // Component Configuration endpoints
  async getComponentConfigurationSchema(componentName: string): Promise<{
    component_name: string;
    displayName: string;
    description: string;
    parameters: Record<string, {
      name: string;
      type: 'string' | 'number' | 'boolean' | 'select';
      default: any;
      description: string;
      required: boolean;
      validation?: { min?: number; max?: number };
      options?: Array<{ label: string; value: any }>;
      envVar?: string;
    }>;
  }> {
    const queryParams = new URLSearchParams({ component_name: componentName });
    return this.request(`/components/schema?${queryParams}`);
  }

  async configureComponent(data: {
    component_name: string;
    usecase_id: string;
    configuration: Record<string, any>;
    target_devices: string[];
    deployment_name?: string;
  }): Promise<{
    status: string;
    deployment_id: string;
    component_name: string;
    configuration: Record<string, any>;
    environment_variables: Record<string, string>;
    target_devices: string[];
    message: string;
  }> {
    return this.request('/components/configure', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }
}

export const apiService = new ApiService();
export default apiService;
