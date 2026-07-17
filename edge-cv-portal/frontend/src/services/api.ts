/**
 * API service for making HTTP requests to the backend
 */
import { getConfig } from '../config';
import { UseCase, Device, User, S3Bucket } from '../types';
import type { Capture } from '../components/ResultsViewer';
import type {
  NodeTypeDescriptor,
  TestDataset,
  TestDatasetCompletedFile,
  TestDatasetUploadInitiation,
  WorkflowDefinition,
  WorkflowGenerationResult,
  WorkflowSummary,
  WorkflowTestRun,
  WorkflowTestRunDetail,
  WorkflowValidationRun,
  WorkflowValidationStatus,
} from '../pages/workflows/types';
import type {
  CameraMutationResponse,
  CameraSourceMutationBody,
  DeviceCameraConflictsResponse,
  DeviceCamerasResponse,
} from '../pages/workflows/cameraReference';
import type { CameraBindingContext } from '../pages/deployments/cameraBindings';
import { beginRequest, endRequest } from './loadingBus';

/**
 * Error thrown for API failures. Workflow Manager endpoints use the
 * structured error envelope {error: {code, message, details}}; `code`
 * and `details` are carried through so callers can act on them (e.g.
 * the deployment ids of a rejected workflow delete, Requirement 5.6).
 */
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
    public readonly code?: string,
    public readonly details?: Record<string, unknown>
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

// Code_Assistant API types (custom-node-code-assist).

/** Runtime entry-point contract of the node type being edited. */
export type CodeAssistContract =
  | 'process_frame'
  | 'process_frame_or_handle'
  | 'frame_hook';

/** One `POST /code-assist` request body. */
export interface CodeAssistRequest {
  usecase_id: string;
  surface: 'workflow-builder' | 'node-designer';
  contract: CodeAssistContract;
  /** 1..4,000 chars with at least one non-whitespace character. */
  prompt: string;
  /** Present iff the editor holds a non-whitespace character (2.6, 2.10). */
  current_code?: string;
  context?: {
    nodeType?: string;
    parameters?: { name: string; param_type: string; description?: string }[];
  };
}

/** Successful code-assist result; nothing is persisted server-side. */
export interface CodeAssistResponse {
  code: string;
  notes: string;
  model_id: string;
  contract: CodeAssistContract;
}

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

    // Track this request globally so the app-wide activity bar shows while any
    // API call is in flight.
    beginRequest();
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
        // Structured error envelope: {error: {code, message, details}}
        if (error.error && typeof error.error === 'object') {
          throw new ApiError(
            error.error.message || `HTTP ${response.status}`,
            response.status,
            error.error.code,
            error.error.details
          );
        }
        throw new Error(error.error || `HTTP ${response.status}`);
      }

      return response.json();
    } catch (err: any) {
      // Structured API errors carry code/details; re-throw untouched.
      if (err instanceof ApiError) {
        throw err;
      }
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
    } finally {
      endRequest();
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

  // List S3 buckets in the current (portal) account - used by Onboard New Use Case
  async listS3Buckets(): Promise<{ buckets: S3Bucket[]; count: number }> {
    return this.request<{ buckets: S3Bucket[]; count: number }>('/s3-buckets');
  }

  // Create a new S3 bucket in the current (portal) account with default settings
  async createS3Bucket(data: { name: string; region?: string }): Promise<{ bucket: S3Bucket }> {
    return this.request<{ bucket: S3Bucket }>('/s3-buckets', {
      method: 'POST',
      body: JSON.stringify(data),
    });
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

  // Captures endpoint (inference-results Results_Viewer)
  // Mirrors getImagePreview: presigned-URL pattern against the inference-results
  // bucket, returning parsed capture metadata (detection typing + Detections_Block)
  // and presigned URLs for the source / overlay / mask artifacts.
  async getCaptures(params: {
    usecase_id: string;
    prefix: string;
    device_id?: string;
    limit?: number;
  }): Promise<{
    captures: Capture[];
    bucket: string;
    prefix: string;
    total_found: number;
    expires_in_seconds: number;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      prefix: params.prefix,
      ...(params.device_id && { device_id: params.device_id }),
      ...(params.limit && { limit: params.limit.toString() }),
    });
    return this.request(`/captures?${queryParams}`);
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

  async verifyRole(roleArn: string, externalId?: string): Promise<{
    status: string;
    account_id?: string;
    assumed_role?: string;
    error?: string;
  }> {
    return this.request('/usecases/verify-role', {
      method: 'POST',
      body: JSON.stringify({ role_arn: roleArn, external_id: externalId }),
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

  /**
   * The device's Camera_Registry entries with computed staleness and the
   * device's IoT connectivity status (camera-registry-sync Requirements
   * 1.3, 7.1). Devices that never completed a synchronization return
   * `state: "never-synced"` rather than a bare empty list.
   */
  async getDeviceCameras(deviceId: string, usecaseId: string): Promise<DeviceCamerasResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<DeviceCamerasResponse>(
      `/devices/${deviceId}/cameras?usecase_id=${usecaseId}`
    );
  }

  /**
   * The device's recorded camera-sync conflict events, newest first
   * (camera-registry-sync Requirement 6.3).
   */
  async getDeviceCameraConflicts(
    deviceId: string,
    usecaseId: string
  ): Promise<DeviceCameraConflictsResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<DeviceCameraConflictsResponse>(
      `/devices/${deviceId}/cameras/conflicts?usecase_id=${usecaseId}`
    );
  }

  /** Create a portal-managed Camera_Source (Operator, Requirement 5.1). */
  async createDeviceCamera(
    deviceId: string,
    usecaseId: string,
    body: CameraSourceMutationBody
  ): Promise<CameraMutationResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<CameraMutationResponse>(
      `/devices/${deviceId}/cameras?usecase_id=${usecaseId}`,
      { method: 'POST', body: JSON.stringify(body) }
    );
  }

  /** Update a portal-managed Camera_Source (Operator, Requirement 5.1). */
  async updateDeviceCamera(
    deviceId: string,
    cameraSourceId: string,
    usecaseId: string,
    body: CameraSourceMutationBody
  ): Promise<CameraMutationResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<CameraMutationResponse>(
      `/devices/${deviceId}/cameras/${encodeURIComponent(cameraSourceId)}?usecase_id=${usecaseId}`,
      { method: 'PUT', body: JSON.stringify(body) }
    );
  }

  /** Pending-delete a portal-managed Camera_Source (Operator). */
  async deleteDeviceCamera(
    deviceId: string,
    cameraSourceId: string,
    usecaseId: string
  ): Promise<CameraMutationResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<CameraMutationResponse>(
      `/devices/${deviceId}/cameras/${encodeURIComponent(cameraSourceId)}?usecase_id=${usecaseId}`,
      { method: 'DELETE' }
    );
  }

  /**
   * Re-issue a conflict's overridden portal version as a new pending
   * change (Operator, Requirement 6.4).
   */
  async reapplyCameraConflict(
    deviceId: string,
    conflictId: string,
    usecaseId: string
  ): Promise<CameraMutationResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<CameraMutationResponse>(
      `/devices/${deviceId}/cameras/conflicts/${encodeURIComponent(conflictId)}/reapply?usecase_id=${usecaseId}`,
      { method: 'POST' }
    );
  }

  /**
   * On-demand refresh: pulls the device's registry shadow through the
   * same reducer as the ingest path and returns the refreshed inventory.
   */
  async refreshDeviceCameras(
    deviceId: string,
    usecaseId: string
  ): Promise<DeviceCamerasResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<DeviceCamerasResponse>(
      `/devices/${deviceId}/cameras/refresh?usecase_id=${usecaseId}`,
      { method: 'POST' }
    );
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

  // SSH tunnel (AWS IoT Secure Tunneling) endpoints
  async getSshTunnelStatus(deviceId: string, usecaseId: string): Promise<{
    device_id: string;
    enabled: boolean;
    component_version?: string | null;
  }> {
    return this.request(`/devices/${deviceId}/ssh-tunnel?usecase_id=${usecaseId}`);
  }

  async setSshTunnel(deviceId: string, usecaseId: string, enabled: boolean, osUser?: string): Promise<{
    device_id: string;
    enabled: boolean;
    os_user?: string | null;
    deployment_id?: string;
    message: string;
  }> {
    return this.request(`/devices/${deviceId}/ssh-tunnel?usecase_id=${usecaseId}`, {
      method: 'POST',
      body: JSON.stringify({ enabled, osUser }),
    });
  }

  async openSshTunnel(deviceId: string, usecaseId: string, lifetimeMinutes?: number): Promise<{
    device_id: string;
    tunnel_id: string;
    region: string;
    source_access_token: string;
    lifetime_minutes: number;
    message: string;
  }> {
    const qs = new URLSearchParams({ usecase_id: usecaseId });
    if (lifetimeMinutes) qs.set('lifetime_minutes', String(lifetimeMinutes));
    return this.request(`/devices/${deviceId}/ssh-tunnel/open?${qs}`, { method: 'POST' });
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
    enable_automated_labeling?: boolean;
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
      console_url?: string;
      worker_portal_url?: string;
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
    return this.request('/training/transform-manifest', {
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
  async startPackaging(trainingId: string, targets?: string[], autoTriggered?: boolean): Promise<{
    training_id: string;
    packaged_components: Array<{
      target: string;
      component_package_s3?: string;
      status: string;
      error?: string;
    }>;
    message: string;
    component_creation_triggered?: boolean;
  }> {
    return this.request(`/training/${trainingId}/package`, {
      method: 'POST',
      // auto_triggered chains packaging -> greengrass publish (component creation).
      body: JSON.stringify({ targets, auto_triggered: autoTriggered }),
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

  async getTargetDeployment(params: {
    usecase_id: string;
    target_device?: string;
    target_thing_group?: string;
  }): Promise<{
    existing_deployment: null | {
      deployment_id: string;
      deployment_name: string;
      target_arn: string;
      deployment_status: string;
      revision_id: string;
      creation_timestamp: string;
      components: Array<{
        component_name: string;
        component_version: string;
      }>;
    };
    group_member_conflicts?: Array<{
      device: string;
      deployment_id: string;
      deployment_name: string;
      deployment_status: string;
    }>;
  }> {
    const qs = new URLSearchParams({ usecase_id: params.usecase_id });
    if (params.target_device) qs.set('target_device', params.target_device);
    if (params.target_thing_group) qs.set('target_thing_group', params.target_thing_group);
    return this.request(`/deployments?${qs.toString()}`);
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
    is_revision?: boolean;
    superseded_deployment_id?: string | null;
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

  /**
   * Deploy-time Camera_Binding context for the CreateDeployment binding
   * matrix (camera-registry-sync Requirements 8.1, 8.5, 8.9): for each
   * Camera_Input_Node of the workflow version and each target device,
   * the device's registered Camera_Sources as binding options with
   * hint-matching pre-selection. `binding_required: false` skips the
   * matrix step entirely.
   */
  async getCameraBindingContext(params: {
    usecase_id: string;
    workflow_id: string;
    workflow_version?: number;
    target_devices?: string[];
    target_thing_group?: string;
  }): Promise<CameraBindingContext> {
    const qs = new URLSearchParams({
      view: 'binding-context',
      usecase_id: params.usecase_id,
      workflow_id: params.workflow_id,
    });
    if (params.workflow_version !== undefined) {
      qs.set('workflow_version', String(params.workflow_version));
    }
    if (params.target_devices && params.target_devices.length > 0) {
      qs.set('target_devices', params.target_devices.join(','));
    }
    if (params.target_thing_group) {
      qs.set('target_thing_group', params.target_thing_group);
    }
    return this.request(`/deployments?${qs.toString()}`);
  }

  /**
   * Deploy a packaged Workflow_Component (component_type: workflow) with
   * optional deploy-time Camera_Bindings and confirmed warning ids
   * (camera-registry-sync Requirements 8.2, 8.5, 9.3). Rejections carry
   * structured codes: 409 CAMERA_BINDINGS_INVALID {errors, warnings},
   * 409 CAMERA_WARNINGS_UNCONFIRMED {warnings}, 503 REGISTRY_UNAVAILABLE,
   * and 502 BINDING_DELIVERY_FAILED.
   */
  async createWorkflowDeployment(data: {
    usecase_id: string;
    workflow_id: string;
    workflow_version?: number;
    target_devices?: string[];
    target_thing_group?: string;
    deployment_name?: string;
    rollout_config?: {
      auto_rollback?: boolean;
      timeout_seconds?: number;
    };
    camera_bindings?: Record<
      string,
      Record<string, { cameraSourceId: string } | { override: Record<string, unknown> }>
    >;
    confirmed_warnings?: string[];
  }): Promise<{
    deployment_id: string;
    iot_job_id: string;
    iot_job_arn: string;
    workflow_id: string;
    workflow_version: number;
    component_name: string;
    component_version: string;
    target_arn: string;
    target_devices: string[];
    target_thing_group?: string | null;
    is_revision: boolean;
    superseded_deployment_id?: string | null;
    camera_bindings_delivered: boolean;
    message: string;
  }> {
    return this.request('/deployments', {
      method: 'POST',
      body: JSON.stringify({ component_type: 'workflow', ...data }),
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
      // ONNX auto-detected attributes (from graph input/output shapes).
      detection_arch?: string;
      input_width?: number | null;
      input_height?: number | null;
      num_outputs?: number;
      input_shapes?: (number | null)[][];
      output_shapes?: (number | null)[][];
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
    // 'pytorch' (legacy .pt/DLR) or 'onnx' (pluggable ONNX Runtime engine).
    export_format?: string;
    // Object-detection decode thresholds (only used for object_detection).
    score_threshold?: number;
    iou_threshold?: number;
    // Object-detection decoder family: 'yolo' (single tensor + NMS) or
    // 'rf_detr' (DETR-family, two tensors, NMS-free top-k). Only used when
    // model_type === 'object_detection'.
    detection_arch?: string;
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

  // Bedrock_Configuration endpoints (workflow-manager Requirement 10.6).
  // No dedicated settings route exists in API Gateway, so the configuration
  // rides the PortalAdmin-only /data-accounts/{id} routes with the reserved
  // id 'bedrock-configuration' (handled by the data_accounts Lambda).
  async getBedrockConfiguration(): Promise<{
    bedrock_configuration: {
      model_id: string;
      region: string;
      max_tokens: number;
      // null means "unset": the sampling parameter is omitted at invocation.
      temperature: number | null;
      top_p: number | null;
      timeout_seconds: number;
    };
    defaults: Record<string, string | number | null>;
    max_timeout_seconds: number;
  }> {
    return this.request('/data-accounts/bedrock-configuration');
  }

  // Invokable model options (inference profiles + on-demand foundation
  // models) for the settings-page model dropdown. An empty list with a
  // 'permissions' hint means the backend lacks the bedrock list
  // permissions and the UI should fall back to free-text entry.
  async getBedrockModels(): Promise<{
    models: { id: string; label: string }[];
    region: string;
    permissions?: string;
  }> {
    return this.request('/data-accounts/bedrock-configuration/models');
  }

  async updateBedrockConfiguration(config: {
    model_id?: string;
    region?: string;
    max_tokens?: number;
    // An explicit null unsets the sampling parameter (the backend merges
    // provided keys, so omitting the key keeps the current value).
    temperature?: number | null;
    top_p?: number | null;
    timeout_seconds?: number;
  }): Promise<{
    message: string;
    bedrock_configuration: {
      model_id: string;
      region: string;
      max_tokens: number;
      temperature: number | null;
      top_p: number | null;
      timeout_seconds: number;
    };
  }> {
    return this.request('/data-accounts/bedrock-configuration', {
      method: 'PUT',
      body: JSON.stringify(config),
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

  // Workflow Manager endpoints
  // Node type catalog for the Workflow_Builder Node_Palette (camelCase
  // wire form of workflow_core.catalog, Requirement 2.8).
  async getWorkflowNodeCatalog(
    usecaseId?: string
  ): Promise<{ nodeTypes: NodeTypeDescriptor[] }> {
    // With usecase_id the Use_Case's registered Custom_Node_Types are
    // merged in (test/prod backed only); without it the endpoint serves
    // the built-in catalog unchanged.
    const query = usecaseId
      ? `?usecase_id=${encodeURIComponent(usecaseId)}`
      : '';
    return this.request(`/workflows/node-catalog${query}`);
  }

  // Workflow_Store API (workflows.py): CRUD, versioning, duplication
  // (Requirements 5.1, 5.2, 5.4, 5.5, 5.7).
  async listWorkflows(usecaseId?: string): Promise<{ workflows: WorkflowSummary[]; count: number }> {
    const query = usecaseId ? `?usecase_id=${encodeURIComponent(usecaseId)}` : '';
    return this.request(`/workflows${query}`);
  }

  // Create a workflow as version 1 (Requirement 5.1).
  async createWorkflow(data: {
    usecase_id: string;
    name: string;
    definition: WorkflowDefinition;
    description?: string;
  }): Promise<{ workflow: WorkflowSummary; version: number }> {
    return this.request('/workflows', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Open/load a stored definition, latest version by default (Requirement 5.4).
  async getWorkflow(
    workflowId: string,
    version?: number
  ): Promise<{
    workflow: WorkflowSummary;
    version: number;
    validation_status?: WorkflowValidationStatus;
    definition: WorkflowDefinition;
  }> {
    const query = version !== undefined ? `?version=${version}` : '';
    return this.request(`/workflows/${encodeURIComponent(workflowId)}${query}`);
  }

  // Save changes as a new version; prior versions are retained (Requirement 5.2).
  async updateWorkflow(
    workflowId: string,
    data: { definition: WorkflowDefinition; name?: string; description?: string }
  ): Promise<{ workflow: WorkflowSummary; version: number }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  // Delete a workflow and its versions; rejected with 409 and the
  // referencing deployment ids when active deployments exist (5.5, 5.6).
  async deleteWorkflow(workflowId: string): Promise<{ workflow_id: string; message: string }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}`, {
      method: 'DELETE',
    });
  }

  // Duplicate under a new name (Requirement 5.7).
  async duplicateWorkflow(
    workflowId: string,
    data: { name?: string; description?: string } = {}
  ): Promise<{ workflow: WorkflowSummary; version: number }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/duplicate`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Version history, newest first (Requirement 5.2).
  async listWorkflowVersions(workflowId: string): Promise<{
    workflow_id: string;
    latest_version: number;
    versions: Array<{
      version: number;
      created_at?: number;
      created_by?: string;
      validation_status?: WorkflowValidationStatus;
      component_arn?: string | null;
    }>;
    count: number;
  }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/versions`);
  }

  // Run all backend Workflow_Validator checks on a stored version and
  // return the complete findings list (Requirements 4.8, 4.9).
  async validateWorkflow(workflowId: string, version?: number): Promise<WorkflowValidationRun> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/validate`, {
      method: 'POST',
      body: JSON.stringify(version !== undefined ? { version } : {}),
    });
  }

  // Prompt-based workflow generation via the configured Bedrock model
  // (workflow_generator.py, Requirements 10.2, 10.3, 10.5, 10.7).
  // `session_id` continues an existing chat session; `current_definition`
  // is the canvas snapshot so follow-up prompts modify rather than
  // regenerate. Failures raise ApiError with the structured envelope
  // codes (e.g. GENERATION_TIMEOUT, BEDROCK_*, GENERATED_DEFINITION_INVALID).
  // `temperature` (0..1) overrides the configured model temperature for
  // this invocation only; omitted = use the configured value.
  async generateWorkflow(data: {
    usecase_id: string;
    prompt: string;
    session_id?: string;
    current_definition?: WorkflowDefinition;
    temperature?: number;
  }): Promise<WorkflowGenerationResult> {
    return this.request('/workflows/generate', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Code_Assistant endpoint (custom-node-code-assist): synchronous,
  // stateless code generation for one custom Python node module.
  // Failures surface as ApiError with the envelope's code/details so
  // describeCodeAssistError can categorize them (Requirements 5.1-5.3).
  async codeAssist(request: CodeAssistRequest): Promise<CodeAssistResponse> {
    return this.request('/code-assist', {
      method: 'POST',
      body: JSON.stringify(request),
    });
  }

  // Workflow_Test_Runner endpoints (workflow_testing.py, Requirement 12)
  // Test_Datasets scoped to the Use_Case (Requirement 12.2).
  async listTestDatasets(usecaseId?: string): Promise<{ datasets: TestDataset[]; count: number }> {
    const query = usecaseId ? `?usecase_id=${encodeURIComponent(usecaseId)}` : '';
    return this.request(`/test-datasets${query}`);
  }

  // Initiate a dataset upload: declares the file set and returns presigned
  // multipart upload URLs. No dataset record is written until finalize
  // verifies the uploaded content (Requirements 12.3, 12.11).
  async createTestDataset(data: {
    usecase_id: string;
    name: string;
    description?: string;
    files: Array<{ name: string; size: number; content_type?: string }>;
  }): Promise<TestDatasetUploadInitiation> {
    return this.request('/test-datasets', {
      method: 'POST',
      body: JSON.stringify({ action: 'initiate', ...data }),
    });
  }

  // Finalize a dataset upload: completes the multipart uploads and commits
  // the Test_Dataset record after server-side verification (12.3, 12.11).
  async finalizeTestDataset(data: {
    usecase_id: string;
    dataset_id: string;
    name: string;
    description?: string;
    files: TestDatasetCompletedFile[];
  }): Promise<{ dataset: TestDataset }> {
    return this.request('/test-datasets', {
      method: 'POST',
      body: JSON.stringify({ action: 'finalize', ...data }),
    });
  }

  // Start a test run of a stored workflow version against a Test_Dataset
  // (Requirement 12.4; latest version when omitted). `simulated_inference`
  // configures the outcome injected for simulation-stubbed model
  // inference nodes — the model itself is not executed in the cloud
  // sandbox (Requirement 12.6); the backend defaults it when omitted.
  async startTestRun(
    workflowId: string,
    data: {
      dataset_id: string;
      version?: number;
      simulated_inference?: { is_anomalous: boolean; confidence: number };
    }
  ): Promise<{ test_run: WorkflowTestRun }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/test-runs`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Test runs of one workflow, newest first.
  async listTestRuns(
    workflowId: string
  ): Promise<{ test_runs: WorkflowTestRun[]; count: number }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/test-runs`);
  }

  // Test run status plus the per-node results {nodeId, status, outputs,
  // stubActivity, error} produced so far (Requirements 12.7, 12.10).
  async getTestRun(testRunId: string): Promise<WorkflowTestRunDetail> {
    return this.request(`/test-runs/${encodeURIComponent(testRunId)}`);
  }

  // Manifest Validator endpoints
  async manifestValidator(data: {
    action: 'validate' | 'transform' | 'fix_timestamps' | 'validate_and_transform';
    manifestPath: string;
    usecaseId: string;
    outputPath?: string;
  }): Promise<any> {
    return this.request('/manifest-validator', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }
}

export const apiService = new ApiService();
export default apiService;
