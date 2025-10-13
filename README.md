# Defect Detection Application (DDA)

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=flat&logo=docker&logoColor=white)](https://www.docker.com/)

The Defect Detection Application (DDA) is an edge-deployed computer vision solution for quality assurance in discrete manufacturing environments. Originally developed by the AWS EdgeML service team, DDA is now available as an open-source project under the stewardship of the AWS Manufacturing TFC and Auto/Manufacturing IBU.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Usage](#usage)
- [Development](#development)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## Overview

DDA provides real-time defect detection capabilities for manufacturing quality control using computer vision and machine learning. The system runs at the edge using AWS IoT Greengrass, enabling low-latency inference and reducing dependency on cloud connectivity.

### Key Benefits

- **Real-time Processing**: Sub-second inference times for immediate quality feedback
- **Edge Deployment**: Operates independently of cloud connectivity
- **Scalable Architecture**: Supports multiple camera inputs and production lines
- **ML Model Flexibility**: Compatible with various computer vision models
- **Manufacturing Integration**: RESTful APIs for integration with existing systems

## High-Level Workflow

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           DDA End-to-End Workflow                                   │
└─────────────────────────────────────────────────────────────────────────────────────┘

1. Setup Edge Device & Hardware
   ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
   │   Edge Device   │    │    Camera       │    │   Output        │
   │   (Station)     │◄──►│   Hardware      │    │   Devices       │
   └─────────────────┘    └─────────────────┘    └─────────────────┘
                                    │
                                    ▼
2. Buid and Deploy Deploy DDA Application
   ┌─────────────────────────────────────────────────────────────────┐
   │                    AWS IoT Greengrass                           │
   │  ┌─────────────────┐    ┌─────────────────┐                   │
   │  │  DDA Frontend   │    │  DDA Backend    │                   │
   │  │   (React UI)    │◄──►│  (Flask + ML)   │                   │
   │  └─────────────────┘    └─────────────────┘                   │
   └─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
3. Capture & Upload Images
   ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
   │  Image Capture  │───▶│  Local Storage  │───▶│   Amazon S3     │
   │  (GStreamer)    │    │            │    │   (Training)    │
   └─────────────────┘    └─────────────────┘    └─────────────────┘
                                    │
                                    ▼
4. Label Training Data
   ┌─────────────────────────────────────────────────────────────────┐
   │                    Amazon SageMaker   GroundTruth             │
   │  ┌─────────────────┐    ┌─────────────────┐                   │
   │  │  Ground Truth   │───▶│   Labeling      │                   │
   │  │   (Setup)       │    │   Workforce     │                   │
   │  └─────────────────┘    └─────────────────┘                   │
   └─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
5. Train CV Model
   ┌─────────────────────────────────────────────────────────────────┐
   │                    Amazon SageMaker                             │
   │  ┌─────────────────┐    ┌─────────────────┐                   │
   │  │   Training      │───▶│   Model         │                   │
   │  │   Pipeline      │    │   Compilation   │                   │
   │  └─────────────────┘    └─────────────────┘                   │
   └─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
6. Deploy Model to Edge
   ┌─────────────────────────────────────────────────────────────────┐
   │                    AWS IoT Greengrass                           │
   │  ┌─────────────────┐    ┌─────────────────┐                   │
   │  │  Model Component│───▶│  Triton Server  │                   │
   │  │   (Greengrass)  │    │   (Inference)   │                   │
   │  └─────────────────┘    └─────────────────┘                   │
   └─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
7. Run Edge Inference
   ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
   │  Live Images    │───▶│   Defect        │───▶│   Actions       │
   │  (Production)   │    │   Detection     │    │  (Alerts/Sort)  │
   └─────────────────┘    └─────────────────┘    └─────────────────┘
                                    │
                                    ▼
8. Continuous Improvement Loop
   ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
   │  New Images     │───▶│   Re-labeling   │───▶│   Re-training   │
   │  (Edge Data)    │    │   (Ground Truth)│    │   (SageMaker)   │
   └─────────────────┘    └─────────────────┘    └─────────────────┘
                                    │                        │
                                    └────────────────────────┘
                                           (Back to Step 6)
```

### Detailed Steps

1. **Setup Edge Device & Hardware**: Install DDA on edge device, connect cameras and sensors
2. **Deploy DDA Application**: Build and deploy using AWS IoT Greengrass
3. **Capture & Upload Images**: Use DDA to capture images and upload to S3
4. **Label Training Data**: Use SageMaker Ground Truth for image labeling
5. **Train CV Model**: Train and Compile computer vision models using SageMaker
6. **Deploy Model to Edge**: Package and deploy trained model via Greengrass
7. **Run Edge Inference**: Process live images for real-time defect detection
8. **Continuous Improvement**: Collect new data, re-label, re-train, and re-deploy (loop back to step 6)

## Architecture

DDA consists of several key components:

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Camera/       │    │   Edge Device    │    │   AWS Cloud     │
│   Image Source  │───▶│   (Greengrass)   │───▶│   (Optional)    │
│   (GStreamer)   │    └──────────────────┘    └─────────────────┘
└─────────────────┘              │
                                 ▼
                       ┌──────────────────┐
                       │  DDA Components  │
                       │  ┌─────────────┐ │
                       │  │  Frontend   │ │
                       │  │  (React)    │ │
                       │  └─────────────┘ │
                       │  ┌─────────────┐ │
                       │  │  Backend    │ │
                       │  │  (Python +  │ │
                       │  │   Triton)   │ │
                       │  └─────────────┘ │
                       └──────────────────┘
```

### Components

- **Frontend**: React-based web interface for system monitoring and configuration
- **Backend**: Python Flask application handling API requests and business logic (includes packaged NVIDIA Triton inference server)
- **GStreamer**: Video streaming pipeline for camera input processing
- **Database**: SQLite for local data storage
- **File Storage**: Local filesystem for images and results

## Features

-  **Real-time Defect Detection**: Automated quality inspection using computer vision
-  **Web Dashboard**: Intuitive interface for monitoring and configuration
-  **RESTful API**: Integration endpoints for manufacturing systems
-  **Multi-Camera Support**: Handle multiple image sources simultaneously
-  **Secure Edge Deployment**: Can run at edge without cloud connectivity. 

## Prerequisites

### Hardware Requirements

- **Minimum**: 4GB RAM, 20GB storage, x86_64 or ARM64 processor
- **Recommended**: 8GB RAM, 64GB storage, GPU acceleration (optional)
- **Supported Platforms**:
  - x86_64 CPU systems
  - ARM64 CPU systems
  - NVIDIA Jetson devices (Xavier Only with Jetpack 4.X, JP5+ coming soon)
- **Supported Operating Systems**:
  - X86 Ubuntu 20.04, 22.04, (24.04 coming soon)
  - Jetson devices currently Jetpack 4.X
  - ARM64 - Ubuntu 18.04-22.04

### Supported Cameras and Sensors

**Cameras**:
- GigE Vision and USB Vision (GenICam 2) Industrial Cameras
- Advantech ICAM-520/ICAM-540
- JAI/Zebra GO-X GigE Cameras
- Basler/Cognex Ace GigE Cameras
- RTSP/ONVIF Cameras (via folder input)

**Input Sensors**:
- NVIDIA Jetson sysfs compatible beam/presence sensors, etc
- PLC triggers (voltage device dependent)

**Output Sensors**:
- Digital output (stack lights, PLC, diverters)
- Webhooks (coming soon)
- MQTT (coming soon)

### Software Requirements

- Ubuntu 20.04 LTS or 22.04 LTS
- Docker and Docker Compose
- AWS CLI (for cloud deployment)
- AWS IoT Greengrass v2 (for edge deployment)

### Included Components

- Python 3.9+ runtime
- GStreamer 1.0+ (for video streaming)
- NVIDIA Triton Inference Server
- React frontend framework
- SQLite database

### AWS Services (Optional)

- AWS IoT Core
- AWS IoT Greengrass
- Amazon S3 (for component storage)
- AWS IAM (for permissions)
- Amazon SageMaker for Model Training and Compiling

## Quick Start


#### Step 0: Set up IAM Permissions and Roles

1. **Create build server policy**:
   - Go to AWS Console → IAM → Policies → Create policy
   - Use policy name: `dda-build-policy`
   - Policy JSON (replace `[AWS account id]` with your account ID):
   ```json
   {
       "Version": "2012-10-17",
       "Statement": [
           {
               "Effect": "Allow",
               "Action": [
                   "greengrass:*",
                   "iot:*",
                   "s3:CreateBucket",
                   "s3:GetBucketLocation",
                   "s3:PutBucketVersioning",
                   "s3:GetObject",
                   "s3:PutObject",
                   "s3:ListBucket"
               ],
               "Resource": "*"
           }
       ]
   }
   ```

2. **Create edge device policy**:
   - Policy name: `dda-greengrass-policy`
   - Attach S3 permissions for component downloads

3. **Create IAM roles**:
   - Build server role: `dda-build-role` (attach `dda-build-policy` + `AmazonSSMManagedInstanceCore`)
   - Edge device role: `dda-greengrass-role` (attach `dda-greengrass-policy` + `AmazonSSMManagedInstanceCore`)

#### Step 1: Set up Build Environment

1. **Launch EC2 build instance**:
   ```bash
   # Launch Ubuntu 24.04, t2.medium or larger
   # Storage: 64GB, Security: SSH (port 22)
   # Attach IAM role: dda-build-role
   ```

2. **Connect and setup**:
   
   **Option A: SSH (Traditional)**
   ```bash
   ssh -i "your-key.pem" ubuntu@<build-server-ip>
   ```
   
   **Option B: Systems Manager Session Manager (Recommended)**
   ```bash
   # Install Session Manager plugin first
   # https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
   
   # Connect to instance (no SSH keys required)
   aws ssm start-session --target <ec2-instance-id>
   ```
   
   **Session Manager Benefits:**
   - No SSH keys or bastion hosts required
   - No inbound ports needed in security groups
   - Sessions secured with AWS KMS encryption
   - Optional logging to S3 or CloudWatch Logs
   - Configure preferences in AWS Console → Systems Manager → Session Manager
   
   **Setup DDA:**
   ```bash
   # Clone repository
   git clone https://github.com/aws-samples/defect-detection-application.git
   cd DefectDetectionApplication
   
   # Run setup script
   sudo ./setup-build-server.sh
   ```

#### Step 2: Build and Publish DDA Component

1. **Configure deployment settings**:
   ```bash
   # Edit gdk-config.json to set your region and S3 bucket
   {
     "component": {
       "aws.edgeml.dda.LocalServer": {
         "publish": {
           "bucket": "dda-component-[your-account-id]",
           "region": "us-east-1"
         }
       }
     }
   }
   ```

2. **Build and publish**:
   ```bash
  ./gdk-component-build-and-publish.sh >logfile.log 2>&1 &
   ```

#### Step 3: Set up Edge Device

1. **For testing only. For real-world deployments: Continue from step 2 when using a Jetson or similar edge device.**:
   ```bash
   # Ubuntu 24.04, t2.medium or larger
   # Storage: 20GB, Security: SSH (22), DDA UI (3000), API (5000)
   # Attach IAM role: dda-greengrass-role
   ```

2. **Install Greengrass Core**:
   
   **Copy installation files:**
   ```bash
   # Option A: SCP
   scp -i "your-key.pem" -r station_install ubuntu@<edge-device-ip>:~/
   
   # Option B: S3 transfer
   aws s3 cp station_install/ s3://your-bucket/station_install/ --recursive
   ```
   
   **Connect and install:**
   ```bash
   # Option A: SSH
   ssh -i "your-key.pem" ubuntu@<edge-device-ip>
   
   # Option B: Systems Manager
   aws ssm start-session --target i-edge-instance-id
   
   # If using S3 transfer, download files first:
   # aws s3 cp s3://your-bucket/station_install/ ~/station_install/ --recursive
   
   cd station_install
   sudo -E ./setup_station.sh <aws-region> <thing-name>
   ```

3. **Deploy DDA component**:
   ```bash
   # From build server, create deployment
   aws greengrassv2 create-deployment \
     --target-arn "arn:aws:iot:us-east-1:$(aws sts get-caller-identity --query Account --output text):thing/<thing-name>" \
     --components '{
       "aws.greengrass.Nucleus": {"componentVersion": "2.15.0"},
       "aws.edgeml.dda.LocalServer": {"componentVersion": "1.0.0"} # Make sure to upgrade the version as appropriate
     }' \
     --deployment-name "DDA-Deployment" \
     --region us-east-1
   ```

4. **Monitor deployment**:
   ```bash
   # On edge device
   sudo tail -f /aws_dda/greengrass/v2/logs/greengrass.log
   ```

   **Note**: Before proceeding, we recommend running `docker ps` to make sure both backend and frontend containers are running.

5. **Access DDA application**:
   
   **Option A: SSH Tunnel**
   ```bash
   ssh -i "your-key.pem" -L 3000:localhost:3000 -L 5000:localhost:5000 ubuntu@<edge-device-ip>
   ```
   
   **Option B: Systems Manager Port Forwarding**
   ```bash
   # Forward DDA UI port
   aws ssm start-session --target i-edge-instance-id \
     --document-name AWS-StartPortForwardingSession \
     --parameters '{"portNumber":["3000"],"localPortNumber":["3000"]}'
   
   # In another terminal, forward API port
   aws ssm start-session --target i-edge-instance-id \
     --document-name AWS-StartPortForwardingSession \
     --parameters '{"portNumber":["5000"],"localPortNumber":["5000"]}'
   ```
   
   **Open browser to http://localhost:3000**

#### Step 4: Deploy ML Model (Optional)

1. **Train and Compile model using Amazon SageMaker** (see [SageMaker blog guide](https://aws.amazon.com/blogs/machine-learning/))

2. **Create model component**:
   - Use `DDA_Greengrass_Component_Creator.ipynb` notebook
   - Package trained model for edge deployment

3. **Deploy model with DDA**:
   ```bash
   aws greengrassv2 create-deployment \
     --target-arn "arn:aws:iot:us-east-1:$(aws sts get-caller-identity --query Account --output text):thing/<thing-name>" \
     --components '{
       "aws.greengrass.Nucleus": {"componentVersion": "2.15.0"},
       "aws.edgeml.dda.LocalServer": {"componentVersion": "1.0.0"},
       "<your-model-name>": {"componentVersion": "1.0.0"} # Make sure to upgrade the version as appropriate
     }' \
     --deployment-name "DDA-Model-Deployment" \
     --region us-east-1

   ```

 **Note**: Before proceeding, we recommend following the steps in the [Model Loading](#model-loading) troubleshooting section to make sure the model can load without issues into Triton server.

## Usage

### Web Interface

1. **Access the dashboard**: Navigate to `http://your-device-ip:3000`
2. **Upload test images**: Use the Images section to process sample images
3. **Configure models**: Set detection thresholds and parameters
4. **Monitor results**: View inference results and system metrics


### Configuration

Key configuration files:

- `src/backend/l4v.ini`: Backend service configuration
- `src/docker-compose.yaml`: Container orchestration
- `gdk-config.json`: Greengrass component configuration

## Development

### Project Structure

```
defect-detection-application/
├── src/
│   ├── backend/           # Python Flask backend
│   ├── frontend/          # React web interface
│   ├── edgemlsdk/         # ML inference SDK
│   └── docker-compose.yaml
├── station_install/       # Edge device installation scripts
├── test/                  # Test suites
├── build-tools/           # Build utilities
└── docs/                  # Documentation
```


## Deployment

### Production Considerations

- **Security**: Configure proper IAM roles and security groups
- **Monitoring**: Set up CloudWatch logging and metrics
- **Backup**: Implement data backup strategies for critical results
- **Updates**: Plan for component version management and updates

### Scaling

- **Multi-device**: Deploy to multiple edge devices using Greengrass device groups
- **Load balancing**: Use multiple inference servers for high-throughput scenarios
- **Cloud integration**: Optional integration with AWS services for centralized management

## Troubleshooting

### Common Issues

**Docker permission errors**:
```bash
sudo chmod 666 /var/run/docker.sock
```

**Component deployment fails**:
```bash
# Check Greengrass logs
sudo tail -f /greengrass/v2/logs/greengrass.log

# Check component logs
sudo tail -f /greengrass/v2/logs/aws.edgeml.dda.LocalServer.*.log

# Check component logs
sudo tail -f /greengrass/v2/logs/<mode-name>.log
```


**S3 access denied**:
- Verify IAM permissions for Greengrass service role
- Check S3 bucket policies and access permissions

**Frontend not accessible**:
```bash
# Check port forwarding for remote access
ssh -i "key.pem" -L 3000:localhost:3000 -L 5000:localhost:5000 user@device-ip
```

**Model loading**:
```bash
# Test Triton server directly to verify model loading. 

cd /opt/tritonserver/bin
./tritonserver --model-repository /aws_dda/dda_triton/triton_model_repo/

# Expected output should show models in READY status:
# +-------------------------------------------+---------+--------+
# | Model                                     | Version | Status |
# +-------------------------------------------+---------+--------+
# | base_model-bd-dda-classification-arm64    | 1       | READY  |
# | marshal_model-bd-dda-classification-arm64 | 1       | READY  |
# | model-bd-dda-classification-arm64         | 1       | READY  |
# +-------------------------------------------+---------+--------+
```

**Database errors**:
DDA uses SQLite database for local data storage, managed with Alembic for schema migrations.

```bash
# Check database file location
ls -la /aws_dda/dda_data/dda.db

# Inspect database tables using SQLite CLI
sqlite3 /aws_dda/dda_data/dda.db
.tables
.schema
.quit

# Check Alembic migration status
cd /aws_dda/src/backend
python -m alembic current
python -m alembic history

# Apply pending migrations if needed
python -m alembic upgrade head
```

**GStreamer issues**:
DDA uses GStreamer pipelines for video processing and ML inference. Here's how to troubleshoot pipeline issues:

```bash
# Install GStreamer tools if not available
sudo apt update
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good

# Set GStreamer plugin path for DDA custom plugins
export GST_PLUGIN_PATH=/usr/lib/panoramagst/

# Enable GStreamer debug logging
export GST_DEBUG=3  # or GST_DEBUG=4 for more verbose output

# Test basic GStreamer installation
gst-inspect-1.0 --version

# List available GStreamer plugins
gst-inspect-1.0 | grep -E "emltriton|emlcapture"

# Test DDA inference pipeline with sample image
gst-launch-1.0 filesrc blocksize=-1 location="/aws_dda/bd-classification/test-anomaly-1.jpg" ! \
  jpegdec idct-method=2 ! \
  videoconvert ! \
  videoflip method=automatic ! \
  capsfilter caps=video/x-raw,format=RGB ! \
  emltriton model-repo=/aws_dda/dda_triton/triton_model_repo \
    server-path=/opt/tritonserver \
    model=model-bd-dda-classification-arm64 \
    metadata='{"sagemaker_edge_core_capture_data_disk_path": "/aws_dda/inference-results/test", "capture_id": "test-pipeline"}' \
    correlation-id=test-pipeline ! \
  jpegenc idct-method=2 quality=100 ! \
  emlcapture buffer-message-id=file-target_/aws_dda/inference-results/test-jpg \
    interval=0 \
    meta=triton_inference_output_overlay:file-target_/aws_dda/inference-results/test-overlay.jpg

# Check GStreamer logs for errors
journalctl -u greengrass | grep -i gstreamer

# Verify custom plugins are loaded
gst-inspect-1.0 emltriton
gst-inspect-1.0 emlcapture

# Test camera connectivity (if using USB camera)
gst-launch-1.0 v4l2src device=/dev/video0 ! videoconvert ! autovideosink

# Test RTSP camera connectivity
gst-launch-1.0 rtspsrc location=rtsp://camera-ip:554/stream ! decodebin ! autovideosink
```

**Pipeline Crashes and Debugging**:
If the pipeline crashes with segmentation fault (SIGSEGV), debug systematically:

```bash
# First, verify the model loads correctly in Triton server
cd /opt/tritonserver/bin
./tritonserver --model-repository /aws_dda/dda_triton/triton_model_repo/

# Check if your specific model shows as READY
# If model shows UNAVAILABLE, fix the model syntax error first

# Test simplified pipeline without inference
export GST_PLUGIN_PATH=/usr/lib/panoramagst/
gst-launch-1.0 filesrc location="/aws_dda/cookies/test-anomaly-3.jpg" ! \
  jpegdec ! videoconvert ! jpegenc ! filesink location="/tmp/test-output.jpg"

# Test inside Docker container (recommended approach)
docker ps | grep backend
docker exec -it <backend-container-name> bash

# Inside container, test the full pipeline
export GST_PLUGIN_PATH=/usr/lib/panoramagst/
gst-launch-1.0 filesrc blocksize=-1 location="/aws_dda/cookies/test-anomaly-3.jpg" ! \
  emexifextract ! jpegdec idct-method=2 ! videoconvert ! videoflip method=automatic ! \
  capsfilter caps=video/x-raw,format=RGB ! \
  emltriton model-repo=/aws_dda/dda_triton/triton_model_repo \
    server-path=/opt/tritonserver model=model-rajat-segmentation \
    metadata='{"capture_id": "test-pipeline"}' correlation-id=test-pipeline ! \
  jpegenc idct-method=2 quality=100 ! \
  emlcapture buffer-message-id=file-target_/aws_dda/inference-results/test-jpg interval=0
```

**Common GStreamer Issues**:
- **Segmentation fault**: Usually indicates model loading issues or corrupted model files
- **Plugin not found**: Ensure `GST_PLUGIN_PATH` includes `/usr/lib/panoramagst/`
- **Model loading errors**: Verify Triton server is running and model paths are correct
- **Permission errors**: Check file permissions for input images and output directories
- **Memory issues**: Monitor system resources during pipeline execution
- **Model syntax errors**: Fix Python syntax errors in model.py files (see Model Loading section)

### Logs and Monitoring

- **Application logs**: `/aws_dda/greengrass/v2/logs/`
- **Docker logs**: `docker-compose logs -f`
- **Work logs**: TODO - Add description of work logs location and usage

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

### Development Workflow

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. test for new functionality
5. Submit a pull request

### Code of Conduct

This project adheres to the [Amazon Open Source Code of Conduct](CODE_OF_CONDUCT.md).

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.

## Support

- **Documentation**: [AWS Lookout for Vision DDA User Guide](https://docs.aws.amazon.com/lookout-for-vision/latest/dda-user-guide/what-is.html)
- **Issues**: Report bugs and feature requests via [GitHub Issues](https://github.com/aws-samples/defect-detection-application/issues)
- **Discussions**: Join the community discussions for questions and support

---
