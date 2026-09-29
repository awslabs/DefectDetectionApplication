/*
 *
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

export enum ImageSourceType {
  Camera = "Camera",
  Folder = "Folder",
  ICam = "ICam",
  NvidiaCSI = "NvidiaCSI",
  // Network stream cameras (rtsp-rtmp-stream-cameras Requirement 4.1).
  RTSP = "RTSP",
  RTMP = "RTMP",
}

/** The Stream_Settings of an RTSP/RTMP camera (Requirement 4.1). */
export interface StreamSettings {
  /** RTSP only. */
  transport?: "tcp" | "udp" | "auto";
  /** RTSP only, 0 to 5000. */
  latencyMs?: number;
  decoder?: "auto" | "hardware" | "software";
  /** 320 to 4096. */
  maxFrameDimension?: number;
  /** 2 to 60. */
  stallTimeoutS?: number;
}

/** The write-only Stream_Credentials; never returned by the API. */
export interface StreamCredentials {
  username?: string;
  password?: string;
  urlSecret?: string;
}

export type StreamState =
  | "connecting"
  | "streaming"
  | "reconnecting"
  | "failed"
  | "stopped";

/** A stream camera's Stream_Health (GET /image-sources/{id}/stream-health). */
export interface StreamHealth {
  cameraKey?: string;
  state: StreamState;
  codec?: string | null;
  width?: number | null;
  height?: number | null;
  frameWidth?: number | null;
  frameHeight?: number | null;
  sourceFps?: number | null;
  decoder?: string | null;
  decoderFallback?: boolean;
  reconnects?: number;
  lastFrameAtMs?: number | null;
  lastError?: { category: string; message: string; atMs?: number } | null;
  nextAttemptInS?: number;
  leases?: number;
  credentialsConfigured?: boolean;
}

export interface Camera {
  id: string;
  model: string;
  address: string;
  physicalId: string;
  protocol: string;
  serial: string;
  vendor: string;
}

export enum CameraStatus {
  Disconnected = "Disconnected",
  Connected = "Connected",
}

export enum PredictionType {
  Normal = "Normal",
  Anomaly = "Anomaly",
  // Object-detection result type (task=object_detection). Emitted by the
  // marshal for detection captures; distinct from the anomaly-classification
  // values so the UI can surface the bounding-box overlay.
  Detection = "Detection",
}

export enum WorkflowTriggerType {
  RESTAPI = "Line operator or API call",
  DigitalInput = "Digital input",
}

export interface MockCamera {
  name: string;
}

export interface ImageSource {
  imageSourceId: string;
  name: string;
  imageCapturePath?: string;
  description?: string;
  location?: string;
  cameraId?: string;
  cameraStatus?: CameraStatusModel;
  type: ImageSourceType;
  imageSourceConfiguration: ImageSourceConfiguration;
  creationTime: number;
  lastUpdateTime: number;
  /** RTSP/RTMP only: the camera's session health (null with no session). */
  streamHealth?: StreamHealth | null;
  /** RTSP/RTMP only: whether the device holds credentials for the camera. */
  credentialsConfigured?: boolean;
}

export interface CameraStatusModel {
  status: CameraStatus;
  error?: string;
  lastUpdatedTime?: number;
}

export interface AdvancedCameraSettings {
  reverseX?: boolean;
  reverseY?: boolean;
  balanceWhiteAuto?: string;
}

export interface ImageSourceConfiguration {
  imageSourceConfigurationId?: string;
  gain: number;
  exposure: number;
  processingPipeline: string;
  imageCrop?: RegionOfInterest;
  creationTime?: number;
  // Persisted safe advanced GenICam controls (flip, white balance).
  advancedSettings?: AdvancedCameraSettings;
  // RTSP/RTMP only.
  streamSettings?: StreamSettings | null;
}

export type RegionOfInterest = {
  top: number;
  bottom: number;
  left: number;
  right: number;
};
