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
  - NVIDIA Jetson devices (Xavier, Orin series)

### Supported Cameras and Sensors

**Cameras**:
- GigE Vision and USB Vision (GenICam 2) Industrial Cameras
- Advantech ICAM-520/ICAM-540
- JAI/Zebra GO-X GigE Cameras
- Basler/Cognex Ace GigE Cameras
- RTSP/ONVIF Cameras (via folder input)

**Input Sensors**:
- NVIDIA Jetson sysfs compatible beam/presence sensors
- PLC triggers (voltage device dependent)

**Output Sensors**:
- Digital output (stack lights, PLC, diverters)
- Webhooks (coming soon)
- MQTT (coming soon)

### Software Requirements

- Ubuntu 20.04 LTS or 24.04 LTS
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
   - Build server role: `dda-build-role` (attach `dda-build-policy`)
   - Edge device role: `dda-greengrass-role` (attach `dda-greengrass-policy`)

#### Step 1: Set up Build Environment

1. **Launch EC2 build instance**:
   ```bash
   # Launch Ubuntu 24.04, t2.medium or larger
   # Storage: 64GB, Security: SSH (port 22)
   # Attach IAM role: dda-build-role
   ```

2. **Connect and setup**:
   ```bash
   ssh -i "your-key.pem" ubuntu@<build-server-ip>
   
   # Clone repository
   git clone https://github.com/aws-samples/defect-detection-application.git
   cd DefectDetectionApplication
   
   # Run setup script
   ./setup-build-server.sh
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
  ./gdk-component-build-and-publish.sh
   ```

#### Step 3: Set up Edge Device

1. **For testing only. For real-world deployments: Continue from step 2 when using a Jetson or similar edge device.**:
   ```bash
   # Ubuntu 24.04, t2.medium or larger
   # Storage: 20GB, Security: SSH (22), DDA UI (3000), API (5000)
   # Attach IAM role: dda-greengrass-role
   ```

2. **Install Greengrass Core**:
   ```bash
   # Copy installation files
   scp -i "your-key.pem" -r station_install ubuntu@<edge-device-ip>:~/
   
   # Connect and install
   ssh -i "your-key.pem" ubuntu@<edge-device-ip>
   sudo -E ./installGreengrassCore.sh <aws-region> <thing-name>
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

5. **Access DDA application**:
   ```bash
   # Set up SSH tunnel
   ssh -i "your-key.pem" -L 3000:localhost:3000 -L 5000:localhost:5000 ubuntu@<edge-device-ip>
   
   # Open browser to http://localhost:3000
   ```

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

**Model loading issues**:
```bash
# Test Triton server directly to verify model loading
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
```bash
# TODO: Add GStreamer troubleshooting section
# - How to install gst-debug
# - How to look at gst-debug logs
# - How to run GStreamer pipeline commands
# - Camera connection and streaming troubleshooting
```

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

**Note**: This project was originally developed by the AWS EdgeML service team and is now maintained as an open-source project by the AWS Manufacturing TFC and Auto/Manufacturing IBU.
