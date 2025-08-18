# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# time descending img0(newest)-img5(oldest)
EXAMPLE_IMAGE_LIST = {
    "5": "A_img0_03-14T16:11:30_1-1.jpg",
    "4": "A_img1_03-14T16:11:32_2-1.jpg",
    "3": "B_img2_03-14T16:11:56_18-1.jpg",
    "2": "B_img3_03-14T16:11:57_19-1.jpg",
    "1": "C_img4_03-14T16:12:27_39-1.jpg",
    "0": "D_img5_03-14T16:12:27_39-1.jpg"
}

FAKE_TIME_STAMP = 1686260178341

EMPTY_EM_AGENT_CONFIG = {
    "sagemaker_edge_core_capture_data_disk_path": "",
    "sagemaker_edge_core_device_fleet_name": "",
    "sagemaker_edge_core_capture_data_buffer_size": "",
    "sagemaker_edge_core_device_name": "",
    "sagemaker_edge_provider_provider": "",
    "sagemaker_edge_core_capture_data_batch_size": "",
    "sagemaker_edge_local_data_root_path": "",
    "sagemaker_edge_core_folder_prefix": "",
    "sagemaker_edge_core_region": "",
    "sagemaker_edge_core_capture_data_destination": "",
    "sagemaker_edge_provider_provider_path": "",
    "sagemaker_edge_provider_s3_bucket_name": "",
    "sagemaker_edge_log_verbose": "",
    "sagemaker_edge_core_root_certs_path": ""
}