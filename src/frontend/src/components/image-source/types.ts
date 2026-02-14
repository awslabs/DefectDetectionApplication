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
}

export interface CameraStatusModel {
  status: CameraStatus;
  error?: string;
  lastUpdatedTime?: number;
}

export interface ImageSourceConfiguration {
  imageSourceConfigurationId?: string;
  gain: number;
  exposure: number;
  processingPipeline: string;
  imageCrop?: RegionOfInterest;
  creationTime?: number;
}

export type RegionOfInterest = {
  top: number;
  bottom: number;
  left: number;
  right: number;
};
