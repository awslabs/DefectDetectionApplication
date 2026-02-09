# Defect Detection Application (DDA) - Complete End-to-End Architecture Documentation

**Version:** 2.0  
**Last Updated:** February 2, 2026  
**Document Type:** Technical Architecture & Implementation Guide

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Quick Start Guide for Non-Technical Users](#quick-start-guide-for-non-technical-users)
3. [System Overview](#system-overview)
4. [Technology Stack](#technology-stack)
5. [Architecture Components](#architecture-components)
6. [Build & Deployment Process](#build--deployment-process)
7. [Edge Application Runtime](#edge-application-runtime)
8. [Cloud Portal Architecture](#cloud-portal-architecture)
9. [ML Workflow Pipeline](#ml-workflow-pipeline)
10. [Data Flow & Integration](#data-flow--integration)
11. [Security & Access Control](#security--access-control)
12. [Monitoring & Operations](#monitoring--operations)
13. [Troubleshooting Guide](#troubleshooting-guide)

---

## Executive Summary

The Defect Detection Application (DDA) is a comprehensive edge-deployed computer vision solution for manufacturing quality assurance. It combines:

- **Edge Runtime**: Containerized Python/React application running on AWS IoT Greengrass
- **ML Inference**: NVIDIA Triton Inference Server for real-time defect detection
- **Cloud Portal**: Multi-tenant web application for ML lifecycle management
- **Training Pipeline**: SageMaker-based model training and compilation
- **Device Management**: Fleet management via IoT Core and Greengrass

**Key Capabilities:**
- Real-time defect detection at the edge (sub-second latency)
- End-to-end ML workflow from data labeling to deployment
- Multi-account architecture for enterprise isolation
- Support for x86-64, ARM64, and NVIDIA Jetson platforms
- Offline operation with optional cloud connectivity

**Portal-Driven Workflow:**

The Edge CV Portal provides a complete, user-friendly interface for the entire ML lifecycle:

1. **Data Management** → Upload training images to S3 via Portal UI
2. **Labeling** → Create Ground Truth labeling jobs via Portal UI
3. **Manifest Transformation** → Auto-transform manifests via Portal UI
4. **Training** → Launch SageMaker training jobs via Portal UI
5. **Compilation** → Compile models for edge hardware via Portal UI (automatic or manual)
6. **Packaging** → Model restructuring for Triton (automatic via Portal backend)
7. **Component Creation** → Greengrass component generation (automatic via Portal backend)
8. **Deployment** → Deploy to edge devices via Portal UI

**No CLI Required:** All operations from data labeling through edge deployment are managed through the Portal UI with automatic validation, status tracking, and audit logging.

**See [ML Workflow Pipeline](#ml-workflow-pipeline) for complete Portal workflow details.**

---

## Quick Start Guide for Non-Technical Users

> **Audience:** Quality managers, production supervisors, operators, and business users who need to use the DDA system without technical expertise.

### What is DDA?

DDA (Defect Detection Application) is a smart camera system that automatically detects defects in products on your production line. Think of it as having an AI-powered quality inspector that never gets tired and can check thousands of products per hour.

**Two Main Parts:**
1. **Edge Device (Factory Floor)** - The camera and computer that inspects products in real-time
2. **Cloud Portal (Web Browser)** - The website where you manage everything

---

### Getting Started: Your First Defect Detection Model

**Timeline:** 2-3 days for your first model, then a few hours for improvements.

#### Step 1: Collect Sample Images (Day 1)

**What you need:**
- 100-500 images of your products
- Mix of good products and products with defects
- Clear, well-lit photos from the same angle as your production line

**How to do it:**
1. Log into the **Edge CV Portal** (your admin will give you the URL)
2. Click **"Data Management"** in the left menu
3. Click **"Upload Images"**
4. Select your images and click **"Upload to S3"**
5. Wait for upload to complete (green checkmark appears)

**Tips:**
- More images = better accuracy (aim for 200+ images)
- Include various defect types
- Ensure good lighting and focus

---

#### Step 2: Label Your Images (Day 1-2)

**What this means:** You'll draw boxes around defects or mark defective areas so the AI can learn what to look for.

**How to do it:**
1. Click **"Labeling"** in the left menu
2. Click **"Create Labeling Job"**
3. Fill in the form:
   - **Job Name:** Something descriptive like "cookies-defects-jan2026"
   - **Dataset:** Click "Browse" and select your uploaded images
   - **Job Type:** Choose "Bounding Box" (for marking defects) or "Segmentation" (for precise areas)
   - **Labels:** Add your defect types (e.g., "crack", "discoloration", "missing piece")
   - **Workforce:** Select your labeling team
4. Click **"Create Job"**

**What happens next:**
- Your labeling team receives tasks to label images
- They draw boxes around defects or mark defective areas
- Progress shows in the Portal (e.g., "45 of 200 images labeled")
- When complete, status changes to "Completed" (usually 1-2 days)

**Tips:**
- Be consistent with labels (always use the same name for the same defect)
- Label carefully - the AI learns from your examples
- You can pause and resume labeling anytime

---

#### Step 3: Transform Labels (5 minutes)

**What this means:** Convert labels to a format the AI can understand (automatic process).

**How to do it:**
1. Go to **"Labeling"** page
2. Find your completed job (green "Completed" badge)
3. Click **"Transform Manifest"** button
4. Click **"Transform"** (Portal auto-fills everything)
5. Wait 10-30 seconds for completion

**What happens:** Portal converts your labels to training format. You'll see a green "✓ Transformed" badge.

---

#### Step 4: Train Your AI Model (Day 2-3)

**What this means:** The AI learns to recognize defects from your labeled images.

**How to do it:**
1. Click **"Training"** in the left menu
2. Click **"Create Training Job"**
3. Fill in the form:
   - **Training Name:** Descriptive name like "cookies-model-v1"
   - **Labeling Job:** Select your transformed job from dropdown (shows ✓ Transformed)
   - **Model Type:** Choose based on your need:
     - **Classification:** Good vs. Bad (simple)
     - **Object Detection:** Find and mark defects (most common)
     - **Segmentation:** Precise defect boundaries (advanced)
   - **Hyperparameters:** Use defaults (or ask your data scientist)
4. Click **"Start Training"**

**What happens next:**
- Training runs in the cloud (30 minutes to 3 hours)
- Portal shows progress: "InProgress" → "Completed"
- You'll see accuracy metrics when done (e.g., "95% accuracy")

**Tips:**
- Higher accuracy = better defect detection
- If accuracy is low (<85%), you may need more labeled images
- Training costs money (AWS charges), but usually $5-20 per training job

---

#### Step 5: Prepare Model for Edge Device (15 minutes, automatic)

**What this means:** Optimize the model to run fast on your factory floor device.

**How to do it:**
1. Go to **"Training"** page
2. Click on your completed training job
3. Click **"Compile Model"** button
4. Select your device type:
   - **ARM64:** Most edge devices, Jetson
   - **x86-64:** Intel/AMD computers
   - **Jetson Xavier:** NVIDIA Jetson devices
5. Click **"Compile"**

**What happens next (all automatic):**
- Compilation: 5-10 minutes (optimizes model)
- Packaging: 2-5 minutes (prepares for device)
- Component Creation: 1-2 minutes (makes it deployable)
- Status changes to "Available" when ready

**You don't need to do anything else - just wait for "Available" status!**

---

#### Step 6: Deploy to Your Factory Device (10 minutes)

**What this means:** Install the model on your edge device so it can start detecting defects.

**How to do it:**
1. Click **"Deployments"** in the left menu
2. Click **"Create Deployment"**
3. Fill in the form:
   - **Deployment Name:** Descriptive like "line-3-cookies-v1"
   - **Target Device:** Select your device from dropdown (e.g., "line-3-station-1")
   - **Model:** Select your compiled model
   - **InferenceUploader:** Check this if you want to save results to cloud (optional)
4. Click **"Deploy"**

**What happens next:**
- Device downloads model (2-10 minutes depending on size)
- Model installs automatically
- Status shows: "Pending" → "In Progress" → "Completed"
- Device is ready to detect defects!

**Tips:**
- Make sure device is online (green status in Devices page)
- First deployment takes longer (10-15 minutes)
- Subsequent deployments are faster (5 minutes)

---

### Using the Edge Device (Factory Floor)

Once deployed, your edge device is ready to inspect products:

#### Accessing the Edge Device UI

1. **Find the device IP address:**
   - Check the device label or ask your IT admin
   - Example: `http://192.168.1.100:3000`

2. **Open in web browser:**
   - Use Chrome, Firefox, or Edge
   - Navigate to `http://<device-ip>:3000`

3. **Log in:**
   - Use credentials provided by your admin
   - Default: username from your organization

#### Running Defect Detection

**Option 1: Live Camera Feed**
1. Click **"Workflows"** in the device UI
2. Select your workflow (e.g., "cookies-inspection")
3. Click **"Start"**
4. Camera feed appears with real-time defect detection
5. Defects highlighted with colored boxes
6. Results saved automatically

**Option 2: Upload Images**
1. Click **"Image Sources"**
2. Select **"File Upload"**
3. Upload product images
4. Click **"Run Inference"**
5. View results with defect markings

#### Understanding Results

**On the device screen you'll see:**
- **Green box:** Good product (no defects)
- **Red box:** Defect detected
- **Label:** Type of defect (e.g., "crack", "discoloration")
- **Confidence:** How sure the AI is (e.g., "95%")

**Results are saved to:**
- Local device storage (for offline operation)
- Cloud S3 (if InferenceUploader is enabled)
- Can be exported as CSV or JSON

---

### Common Tasks

#### Viewing Device Status

1. Go to **"Devices"** in Portal
2. See all your devices with status:
   - 🟢 **Online:** Device is connected and working
   - 🔴 **Offline:** Device is disconnected
   - 🟡 **Warning:** Device has issues
3. Click device name to see details:
   - Current model deployed
   - Inference count (how many products inspected)
   - Last heartbeat (when device last checked in)
   - Logs (for troubleshooting)

#### Checking Model Performance

1. Go to **"Models"** in Portal
2. Click on your model
3. View metrics:
   - **Accuracy:** How often model is correct
   - **Inference Count:** How many products inspected
   - **Average Latency:** How fast (e.g., "50ms per image")
4. Download inference results for analysis

#### Improving Your Model

**When to retrain:**
- Accuracy drops below 90%
- New defect types appear
- Production line changes (lighting, angle, etc.)

**How to retrain:**
1. Collect new images (especially misclassified ones)
2. Upload to **"Data Management"**
3. Create new labeling job (include old + new images)
4. Repeat Steps 2-6 above
5. Deploy new model version

**Tip:** Keep old model deployed until new model is tested!

---

### User Roles & Permissions

**What you can do depends on your role:**

| Role | Can Do | Cannot Do |
|------|--------|-----------|
| **Viewer** | View devices, models, results | Change anything |
| **Operator** | View + Deploy models, monitor devices | Create/train models |
| **Data Scientist** | View + Create labeling jobs, train models | Deploy to production devices |
| **UseCase Admin** | Everything within your use case | Access other use cases |
| **Portal Admin** | Everything across all use cases | - |

**To check your role:**
1. Click your name in top-right corner
2. Select **"Settings"**
3. View your role under "User Information"

---

### Getting Help

#### In the Portal

- **Help Icon (?)**: Click for context-sensitive help
- **Status Indicators**: Hover over badges for explanations
- **Error Messages**: Read carefully - they tell you what to fix

#### Common Issues

**"Manifest not transformed"**
- **Fix:** Go to Labeling → Select job → Click "Transform Manifest"

**"Training failed"**
- **Fix:** Check error message, usually means:
  - Not enough images (need 100+)
  - Images not accessible in S3
  - Invalid manifest format

**"Deployment stuck in 'In Progress'"**
- **Fix:** Check device is online in Devices page
- **Fix:** Check device has internet connection
- **Fix:** Wait 15 minutes (large models take time)

**"Low accuracy (<80%)"**
- **Fix:** Add more labeled images (aim for 300+)
- **Fix:** Ensure consistent labeling
- **Fix:** Check image quality (lighting, focus)

#### Contact Support

- **Portal Admin:** Check "Settings" → "Support" for contact
- **Audit Logs:** Go to "Audit Logs" to see what happened
- **Device Logs:** Go to "Devices" → Select device → "View Logs"

---

### Best Practices

**For Best Results:**

1. **Start Small:** Begin with 100-200 images, test, then expand
2. **Label Consistently:** Use the same names for the same defects
3. **Test Before Production:** Deploy to test device first
4. **Monitor Performance:** Check accuracy weekly
5. **Retrain Regularly:** Add new examples monthly
6. **Keep Old Models:** Don't delete working models
7. **Document Changes:** Use descriptive names (e.g., "v1-baseline", "v2-added-cracks")

**For Smooth Operations:**

1. **Check Device Status Daily:** Ensure all devices are online
2. **Review Inference Results Weekly:** Look for patterns
3. **Update Models Quarterly:** Incorporate new defect types
4. **Backup Important Data:** Export results regularly
5. **Train Your Team:** Ensure multiple people know how to use the system

---

### Quick Reference Card

**Daily Operations:**
```
✓ Check device status (Devices page)
✓ Monitor inference results (Models page)
✓ Review any alerts (Dashboard)
```

**Weekly Tasks:**
```
✓ Review model accuracy (Models page)
✓ Export inference results (Data Management)
✓ Check for failed deployments (Deployments page)
```

**Monthly Tasks:**
```
✓ Collect new training images
✓ Retrain models with new data
✓ Update documentation
✓ Review audit logs (Audit Logs page)
```

**Emergency Contacts:**
```
Portal Admin: [Your admin contact]
IT Support: [Your IT contact]
AWS Support: [Your AWS account team]
```

---

## System Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           DDA ECOSYSTEM                                      │
└─────────────────────────────────────────────────────────────────────────────┘

        ┌──────────────────────────────────────────────────────────┐
        │              PORTAL ACCOUNT (Cloud)                      │
        │  ┌────────────┐  ┌────────────┐  ┌────────────┐        │
        │  │ CloudFront │  │ API Gateway│  │  Cognito   │        │
        │  │  + React   │  │  + Lambda  │  │   (Auth)   │        │
        │  └────────────┘  └────────────┘  └────────────┘        │
        │  ┌────────────┐  ┌────────────┐                        │
        │  │ DynamoDB   │  │ EventBridge│                        │
        │  │ (Metadata) │  │  (Events)  │                        │
        │  └────────────┘  └────────────┘                        │
        └──────────────────────┬───────────────────────────────────┘
                               │ STS AssumeRole
        ┌──────────────────────┴───────────────────────────────────┐
        │                                                           │
        ▼                                                           ▼
┌───────────────────────┐                              ┌───────────────────────┐
│  USECASE ACCOUNT      │                              │  DATA ACCOUNT         │
│  (ML Workloads)       │                              │  (Optional)           │
│                       │                              │                       │
│  ┌─────────────────┐ │                              │  ┌─────────────────┐ │
│  │  SageMaker      │ │                              │  │  S3 Buckets     │ │
│  │  - Training     │ │                              │  │  - Training Data│ │
│  │  - Compilation  │ │                              │  │  - Shared Data  │ │
│  │  - Ground Truth │ │                              │  └─────────────────┘ │
│  └─────────────────┘ │                              │                       │
│  ┌─────────────────┐ │                              └───────────────────────┘
│  │  Greengrass     │ │
│  │  - Components   │ │
│  │  - Deployments  │ │
│  └─────────────────┘ │
│  ┌─────────────────┐ │
│  │  IoT Core       │ │
│  │  - Things       │ │
│  │  - Shadows      │ │
│  └─────────────────┘ │
│  ┌─────────────────┐ │
│  │  S3 Buckets     │ │
│  │  - Models       │ │
│  │  - Datasets     │ │
│  └─────────────────┘ │
│           │           │
│           ▼           │
│  ┌─────────────────┐ │
│  │  EDGE DEVICES   │ │
│  │  (Greengrass)   │ │
│  └─────────────────┘ │
└───────────────────────┘

        ┌──────────────────────────────────────────────────────────┐
        │              EDGE DEVICE (Station)                       │
        │  ┌────────────────────────────────────────────────────┐ │
        │  │  AWS IoT Greengrass V2                             │ │
        │  │  ┌──────────────────┐  ┌──────────────────┐       │ │
        │  │  │ DDA LocalServer  │  │  Model Component │       │ │
        │  │  │  (Docker)        │  │  (Triton Models) │       │ │
        │  │  └──────────────────┘  └──────────────────┘       │ │
        │  │  ┌──────────────────┐  ┌──────────────────┐       │ │
        │  │  │ InferenceUploader│  │  Other Components│       │ │
        │  │  │  (Optional)      │  │                  │       │ │
        │  │  └──────────────────┘  └──────────────────┘       │ │
        │  └────────────────────────────────────────────────────┘ │
        │                                                          │
        │  ┌────────────────────────────────────────────────────┐ │
        │  │  DDA LocalServer Container                         │ │
        │  │  ┌──────────────────┐  ┌──────────────────┐       │ │
        │  │  │  Backend         │  │  Frontend        │       │ │
        │  │  │  (Flask/Python)  │  │  (React/Nginx)   │       │ │
        │  │  │  Port: 5000      │  │  Port: 3000      │       │ │
        │  │  └──────────────────┘  └──────────────────┘       │ │
        │  │  ┌──────────────────┐  ┌──────────────────┐       │ │
        │  │  │ Triton Server    │  │  SQLite DB       │       │ │
        │  │  │ (Inference)      │  │  (Config/State)  │       │ │
        │  │  └──────────────────┘  └──────────────────┘       │ │
        │  │  ┌──────────────────┐  ┌──────────────────┐       │ │
        │  │  │ GStreamer        │  │  EdgeML SDK      │       │ │
        │  │  │ (Video Pipeline) │  │  (Camera/Triton) │       │ │
        │  │  └──────────────────┘  └──────────────────┘       │ │
        │  └────────────────────────────────────────────────────┘ │
        │                                                          │
        │  ┌────────────────────────────────────────────────────┐ │
        │  │  Hardware Interfaces                               │ │
        │  │  - GigE Vision Cameras (GenICam)                   │ │
        │  │  - USB Vision Cameras                              │ │
        │  │  - Digital I/O (Triggers/Outputs)                  │ │
        │  │  - RTSP/ONVIF Cameras                              │ │
        │  └────────────────────────────────────────────────────┘ │
        └──────────────────────────────────────────────────────────┘
```

### Component Relationships

| Component | Purpose | Technology | Location |
|-----------|---------|------------|----------|
| **Edge CV Portal** | ML lifecycle management UI | React + CloudFront | Portal Account (Cloud) |
| **Portal API** | Backend orchestration | Lambda + API Gateway | Portal Account (Cloud) |
| **DDA LocalServer** | Edge application runtime | Docker + Greengrass | Edge Device |
| **Triton Server** | ML inference engine | NVIDIA Triton | Edge Device (in LocalServer) |
| **Model Components** | Packaged ML models | Greengrass Components | Edge Device |
| **SageMaker** | Training & compilation | AWS SageMaker | UseCase Account (Cloud) |
| **IoT Core** | Device connectivity | AWS IoT Core | UseCase Account (Cloud) |

---

## Technology Stack

### Edge Application Stack

**Operating System:**
- Ubuntu 18.04, 20.04, 22.04 (x86-64, ARM64)
- NVIDIA Jetpack 4.x (for Jetson devices)

**Container Runtime:**
- Docker 20.10+
- Docker Compose 3.0

**Backend (Python 3.9):**

- **Web Framework**: FastAPI (async REST API)
- **Inference**: NVIDIA Triton Inference Server (embedded)
- **Video Processing**: GStreamer 1.0+ with custom plugins
- **Camera SDK**: Aravis (GigE Vision/GenICam support)
- **Database**: SQLite with Alembic migrations
- **ML Runtime**: DLR (Deep Learning Runtime), EdgeML SDK
- **IoT**: AWS IoT Device SDK, Greengrass IPC

**Key Python Dependencies:**
```
fastapi==0.109.2          # Async web framework
uvicorn==0.23.2           # ASGI server
sqlalchemy==2.0.21        # ORM
alembic==1.12.1           # Database migrations
opencv-python             # Image processing
numpy==1.24.3             # Numerical computing
Pillow==10.3.0            # Image manipulation
PyGObject==3.42.2         # GStreamer bindings
grpcio==1.56.2            # Triton gRPC client
awsiotsdk==1.11.9         # IoT connectivity
dlr==1.10.0               # Deep Learning Runtime
```

**Frontend (React 18):**
- **Framework**: React 18 with TypeScript
- **Build Tool**: Vite
- **UI Library**: Custom components
- **HTTP Client**: Axios
- **Routing**: React Router
- **State Management**: React Context API

**Inference Engine:**
- **NVIDIA Triton Inference Server** (custom build)
  - Python backend support
  - Model repository management
  - Multi-model serving
  - gRPC/HTTP inference APIs

**Video Pipeline:**
- **GStreamer 1.0+**
  - Custom plugins: `emltriton`, `emlcapture`, `emexifextract`
  - Hardware acceleration support (CUDA, NVENC)
  - RTSP/ONVIF camera support

### Cloud Portal Stack

**Frontend:**
- React 18 + TypeScript
- CloudScape Design System (AWS UI components)
- Vite build system
- CloudFront + S3 hosting

**Backend:**
- AWS Lambda (Python 3.11)
- API Gateway REST API
- Lambda Layers (shared utilities, JWT)

**Authentication:**
- Amazon Cognito User Pool
- JWT-based authorization
- SAML/OIDC federation support (Okta, Azure AD, etc.)

**Storage:**
- DynamoDB (13 tables for metadata)
- S3 (artifacts, models, datasets)

**ML Services:**
- Amazon SageMaker Training
- Amazon SageMaker Compilation
- Amazon SageMaker Ground Truth (labeling)

**Device Management:**
- AWS IoT Core
- AWS IoT Greengrass V2
- EventBridge (event-driven workflows)

**Infrastructure as Code:**
- AWS CDK (TypeScript)
- CloudFormation

### Build & Deployment Tools

**Component Build:**
- Greengrass Development Kit (GDK) CLI
- Docker BuildKit
- Python 3.9 build environment

**CI/CD:**
- Bash scripts for automation
- AWS CLI for deployment
- S3 for artifact storage

---

## Architecture Components

### 1. DDA LocalServer (Edge Application)

**Purpose:** Main edge application providing UI, API, and inference orchestration.

**Architecture:**
```
┌─────────────────────────────────────────────────────────────┐
│  DDA LocalServer Greengrass Component                       │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  Docker Compose Stack                                  │ │
│  │                                                         │ │
│  │  ┌──────────────────────┐  ┌──────────────────────┐  │ │
│  │  │  Backend Container   │  │  Frontend Container  │  │ │
│  │  │  (flask-app)         │  │  (react-webapp)      │  │ │
│  │  │                      │  │                      │  │ │
│  │  │  - FastAPI app       │  │  - Nginx server      │  │ │
│  │  │  - Triton client     │  │  - React SPA         │  │ │
│  │  │  - GStreamer         │  │  - Port 3000 (HTTP)  │  │ │
│  │  │  - SQLite DB         │  │  - Port 3443 (HTTPS) │  │ │
│  │  │  - Port 5000 (HTTP)  │  │                      │  │ │
│  │  │  - Port 5443 (HTTPS) │  │                      │  │ │
│  │  └──────────────────────┘  └──────────────────────┘  │ │
│  │                                                         │ │
│  │  Shared Volumes:                                       │ │
│  │  - /aws_dda (persistent data)                          │ │
│  │  - /tmp (temporary files)                              │ │
│  │  - /dev/shm (shared memory for IPC)                    │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  Greengrass Lifecycle:                                      │
│  - Install: Load Docker images from tar archives           │
│  - Run: Start Docker Compose with environment variables    │
│  - Shutdown: Stop Docker Compose gracefully                │
└─────────────────────────────────────────────────────────────┘
```

**Key Files:**
- `src/backend/app.py` - FastAPI application entry point
- `src/backend/Dockerfile` - Backend container build
- `src/frontend/Dockerfile` - Frontend container build
- `src/docker-compose.yaml` - Container orchestration
- `recipe-arm64.yaml` / `recipe-amd64.yaml` - Greengrass component recipes

**Backend Modules:**
```
src/backend/
├── app.py                          # FastAPI application
├── endpoints/                      # REST API endpoints
│   ├── workflow.py                 # Workflow management
│   ├── camera.py                   # Camera configuration
│   ├── image_source.py             # Image source management
│   ├── inference_result.py         # Inference results API
│   └── system.py                   # System health/status
├── dao/                            # Data Access Objects
│   └── sqlite_db/                  # SQLite database layer
├── dda_triton/                     # Triton integration
│   ├── triton_edge_client.py       # Triton client wrapper
│   ├── triton_setup.py             # Triton initialization
│   └── model_convertor.py          # Model format conversion
├── gstreamer/                      # GStreamer pipeline management
│   ├── gst_pipeline.py             # Pipeline abstraction
│   └── pipeline_builder.py         # Pipeline construction
├── utils/                          # Utility modules
│   ├── camera_manager.py           # Camera lifecycle
│   ├── capture_task_manager.py     # Async capture tasks
│   └── inference_results_utils.py  # Result processing
└── lyra_science_processing_utils/  # ML pre/post-processing
    ├── inference_preprocessor.py   # Input preprocessing
    ├── inference_postprocessor.py  # Output postprocessing
    └── model_graphs/               # Model-specific logic
```

**Database Schema (SQLite):**
- **Workflows**: Inference workflow configurations
- **ImageSources**: Camera/file input configurations
- **InputConfigurations**: Digital I/O trigger settings
- **OutputConfigurations**: Result output settings
- **FeatureConfigurations**: Feature flags
- **WorkflowMetadata**: Runtime statistics

**Triton Integration:**

- Triton server runs embedded in backend container
- Model repository: `/aws_dda/dda_triton/triton_model_repo/`
- Models loaded from Greengrass model components
- gRPC communication on localhost:8001
- HTTP inference API on localhost:8000

### 2. Model Components (Greengrass)

**Purpose:** Packaged ML models deployed as Greengrass components via the Edge CV Portal.

**Structure:**
```
Model Component (e.g., model-cookies-classification-arm64)
├── recipe.yaml                     # Component definition
└── artifacts/
    └── model-cookies-classification-arm64.tar.gz
        ├── model-cookies-classification-arm64/  # Triton model dir
        │   ├── 1/                              # Version 1
        │   │   └── model.py                    # Python model
        │   └── config.pbtxt                    # Triton config
        ├── base_model-cookies-classification-arm64/
        │   ├── 1/
        │   │   └── model.savedmodel/           # TensorFlow model
        │   └── config.pbtxt
        └── marshal_model-cookies-classification-arm64/
            ├── 1/
            │   └── model.py                    # Output marshaling
            └── config.pbtxt
```

**Model Types:**
- **Base Model**: Compiled ML model (TensorFlow SavedModel, ONNX, PyTorch)
- **Marshal Model**: Input preprocessing (Python)
- **Main Model**: Output postprocessing and result formatting (Python)

**Portal-Managed Component Creation Flow:**

The entire model component creation and deployment process is automated through the Portal:

1. **Training** (Portal UI → SageMaker):
   - User creates training job via Portal
   - SageMaker produces model artifact (tar.gz)
   - EventBridge notifies Portal of completion

2. **Compilation** (Portal UI → SageMaker):
   - User selects target architecture (ARM64/x86-64/Jetson)
   - Portal creates compilation job
   - SageMaker optimizes for target hardware
   - EventBridge notifies Portal of completion

3. **Packaging** (Automatic via Portal Backend):
   - CompilationEvents Lambda triggered on completion
   - Packaging Lambda invoked automatically:
     - Downloads compiled model from S3
     - Extracts and restructures for Triton format
     - Creates three-model structure (base, marshal, main)
     - Generates Triton config.pbtxt files
     - Uploads packaged model to UseCase Account S3
   - Invokes GreengrassPublish Lambda

4. **Component Creation** (Automatic via Portal Backend):
   - GreengrassPublish Lambda:
     - Creates Greengrass component recipe
     - Registers component in UseCase Account Greengrass registry
     - Updates Models table in DynamoDB
     - Sets model status to "Available"
   - Component now visible in Portal Components page

5. **Deployment** (Portal UI → Greengrass):
   - User navigates to Deployments → Create Deployment
   - Selects device/group and model component from dropdown
   - Portal creates Greengrass deployment
   - Edge device downloads and installs component
   - Model auto-loaded into Triton server

**Portal Advantages:**
- **Fully Automated**: No manual packaging or component creation
- **Event-Driven**: Compilation completion triggers packaging automatically
- **Validated**: Portal ensures model compatibility before deployment
- **Tracked**: All steps logged in DynamoDB and AuditLog
- **Visible**: Component status shown in Portal UI
- **Deployable**: One-click deployment to devices

**Component Lifecycle in Portal:**

```
Training Job (Portal) 
   ↓
Compilation Job (Portal)
   ↓ EventBridge trigger
Packaging Lambda (Automatic)
   ↓
GreengrassPublish Lambda (Automatic)
   ↓
Component Available (Portal UI)
   ↓
Create Deployment (Portal UI)
   ↓
Model Running on Edge Device
```

### 3. InferenceUploader Component (Optional)

**Purpose:** Automatically upload inference results to S3 for centralized analysis.

**Configuration via Portal:**

The InferenceUploader is configured during deployment creation in the Portal UI:

1. Navigate to **Deployments → Create Deployment**
2. Select device/group and model component
3. **Enable InferenceUploader** (checkbox)
4. Configure settings:
   - S3 bucket (auto-filled or custom)
   - Upload interval (10s to daily)
   - Batch size (default: 100)
   - Retention days (default: 7)
5. Click **Deploy**

**Portal-Generated Configuration:**
```yaml
# Portal automatically generates this configuration
configurationUpdate:
  merge: |
    {
      "s3Bucket": "dda-inference-results-123456789012",
      "s3Prefix": "usecase-id/device-id",
      "uploadIntervalSeconds": 300,
      "batchSize": 100,
      "retentionDays": 7
    }
```

**No Manual Configuration Required:**
- Portal handles all component configuration
- Settings stored in DynamoDB per UseCase
- Deployment Lambda applies configuration automatically
- Changes require redeployment via Portal

**Behavior:**
- Monitors `/aws_dda/inference-results/` directory
- Uploads images + metadata JSON to S3
- Organizes by date: `s3://bucket/prefix/model-id/YYYY/MM/DD/`
- Cleans up local files after retention period
- Configurable upload frequency (10s to daily)

### 4. Edge CV Portal (Cloud Application)

**Purpose:** Multi-tenant web application for ML lifecycle management.

**Architecture:**
```
┌─────────────────────────────────────────────────────────────┐
│  Edge CV Portal (Portal Account)                            │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  Frontend (CloudFront + S3)                            │ │
│  │  - React SPA with TypeScript                           │ │
│  │  - CloudScape Design System                            │ │
│  │  - Cognito authentication                              │ │
│  └────────────────────────────────────────────────────────┘ │
│                          │                                   │
│                          ▼                                   │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  API Gateway + Lambda Functions                        │ │
│  │                                                         │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐ │ │
│  │  │  UseCases    │  │  Labeling    │  │  Training   │ │ │
│  │  │  Devices     │  │  Datasets    │  │  Compilation│ │ │
│  │  │  Deployments │  │  Models      │  │  Packaging  │ │ │
│  │  └──────────────┘  └──────────────┘  └─────────────┘ │ │
│  └────────────────────────────────────────────────────────┘ │
│                          │                                   │
│                          ▼                                   │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  DynamoDB Tables (13 tables)                           │ │
│  │  - UseCases, UserRoles, Devices                        │ │
│  │  - TrainingJobs, LabelingJobs, Models                  │ │
│  │  - Deployments, Components, AuditLog                   │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  EventBridge Rules                                     │ │
│  │  - SageMaker Training State Changes                    │ │
│  │  - SageMaker Compilation State Changes                 │ │
│  │  - Ground Truth Labeling Job Status                    │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                          │
                          │ STS AssumeRole
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  UseCase Account                                            │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  SageMaker                                             │ │
│  │  - Training Jobs                                       │ │
│  │  - Compilation Jobs                                    │ │
│  │  - Ground Truth Labeling                               │ │
│  └────────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  Greengrass                                            │ │
│  │  - Component Registry                                  │ │
│  │  - Deployments                                         │ │
│  └────────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  IoT Core                                              │ │
│  │  - Things (devices)                                    │ │
│  │  - Shadows (device state)                              │ │
│  └────────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  S3 Buckets                                            │ │
│  │  - Training data                                       │ │
│  │  - Model artifacts                                     │ │
│  │  - Inference results                                   │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

**Key Lambda Functions:**

| Function | Purpose | Timeout | Memory |
|----------|---------|---------|--------|
| UseCases | UseCase CRUD, onboarding, shared component provisioning | 120s | 128MB |
| Labeling | Ground Truth job management, manifest transformation | 60s | 128MB |
| Training | SageMaker training job orchestration | 60s | 128MB |
| Compilation | SageMaker compilation | 300s | 1024MB |
| Packaging | Model extraction and Triton restructuring | 900s | 3008MB |
| GreengrassPublish | Component creation and publishing | 300s | 128MB |
| Deployments | Greengrass deployment management | 60s | 128MB |
| Devices | Device inventory and health monitoring | 30s | 128MB |
| Components | Component browser and management | 60s | 128MB |
| DataManagement | S3 bucket/folder operations | 60s | 128MB |

**DynamoDB Schema:**

| Table | Partition Key | Sort Key | GSIs | Purpose |
|-------|---------------|----------|------|---------|
| UseCases | usecase_id | - | owner-index | UseCase configurations |
| UserRoles | user_id | usecase_id | usecase-users-index | RBAC assignments |
| Devices | device_id | - | usecase-devices-index, status-index | Device inventory |
| TrainingJobs | training_id | - | usecase-training-index, model-index | Training metadata |
| LabelingJobs | job_id | - | usecase-jobs-index, status-index | Labeling job tracking |
| Models | model_id | - | usecase-models-index, stage-index | Model registry |
| Deployments | deployment_id | - | usecase-deployments-index, status-index | Deployment tracking |
| Components | component_id | - | component-name-index, component-type-index | Component catalog |
| SharedComponents | usecase_id | component_name | - | Shared component provisioning |
| DataAccounts | data_account_id | - | status-index | Registered Data Accounts |
| AuditLog | event_id | timestamp | user-actions-index, usecase-actions-index | Audit trail |

---

## Build & Deployment Process

**Overview:** This section covers the infrastructure setup for DDA - building the shared LocalServer component and setting up edge devices. For the complete ML workflow (labeling, training, compilation, and model deployment), see [ML Workflow Pipeline](#ml-workflow-pipeline).

**Infrastructure Setup (Covered Here):**
- Phase 1: DDA LocalServer Component Build & Publish (Portal Account)
- Phase 2: Edge Device Setup & Registration (UseCase Account)
- Phase 3: Component Deployment via Portal

**ML Lifecycle (See ML Workflow Pipeline Section):**
- Data collection and labeling via Portal
- Model training and compilation via Portal
- Model component creation (automatic)
- Model deployment to edge devices via Portal

---

### Phase 1: DDA LocalServer Component Build & Publish (Portal Account - One-Time Setup)

**IMPORTANT: This is a one-time setup performed in the Portal Account. The resulting DDA LocalServer component is shared as a common component across all UseCase accounts.**

**Purpose:** Build and publish the DDA LocalServer shared component that will be automatically provisioned to all UseCase accounts.

**Account Context:**
- **Build Location**: Portal Account (where Edge CV Portal is deployed)
- **Component Storage**: Portal Account S3 bucket (`dda-component-{region}-{portal-account}`)
- **Component Registry**: Portal Account Greengrass registry
- **Shared Access**: Automatically provisioned to UseCase accounts during onboarding

**Why Portal Account?**
- **Centralized Management**: Single build server for all use cases
- **Version Control**: Consistent DDA LocalServer version across organization
- **Cost Efficiency**: One build server instead of per-usecase builds
- **Simplified Updates**: Update once, deploy to all use cases via Portal
- **Automatic Provisioning**: Portal handles cross-account component sharing

---

#### Step 1: Build Server Setup

**Requirements:**
- EC2 instance matching target edge device architecture
  - ARM64: m6g.4xlarge (Graviton) for Jetson/ARM64 devices
  - x86-64: c5.4xlarge for x86 devices
- Ubuntu 18.04 (for Jetson JP4.x) or 20.04/22.04
- 100GB+ storage for Docker builds
- IAM role with Greengrass and S3 permissions (`dda-build-role`)

**Automated Launch:**
```bash
# Launch ARM64 build server in Portal Account
./launch-arm64-build-server.sh \
  --key-name your-key-name \
  --security-group-id sg-xxxxxxxx \
  --subnet-id subnet-xxxxxxxx \
  --iam-profile dda-build-role
```

**Manual Setup:**
```bash
# SSH to build server
ssh -i "your-key.pem" ubuntu@<build-server-ip>

# Clone repository
git clone https://github.com/aws-samples/defect-detection-application.git
cd DefectDetectionApplication

# Run setup script
sudo ./setup-build-server.sh
```

**Setup Script Actions:**
1. Install Docker via snap
2. Install Python 3.9 (from source on Ubuntu 18.04, from PPA on 20.04+)
3. Install AWS CLI v2
4. Install Greengrass Development Kit (GDK) CLI
5. Configure Docker permissions
6. Add GDK to PATH

---

#### Step 2: Component Build & Publish

**Unified Script:** `gdk-component-build-and-publish.sh`

This single script handles the complete build-to-publish workflow, automating both the component build process and publishing to AWS Greengrass in the Portal Account.

**Usage:**
```bash
# Run from DefectDetectionApplication directory on build server
./gdk-component-build-and-publish.sh
```

**Automated Workflow:**

1. **Architecture Detection**:
   - Automatically detects build server architecture (x86_64 or aarch64)
   - Selects appropriate recipe file:
     - ARM64: `recipe-arm64.yaml` → `aws.edgeml.dda.LocalServer.arm64`
     - x86-64: `recipe-amd64.yaml` → `aws.edgeml.dda.LocalServer.amd64`

2. **Configuration Generation**:
   - Retrieves Portal Account ID and region
   - Generates `gdk-config.json` with Portal Account settings
   - Configures S3 bucket: `dda-component-{region}-{portal-account}`

3. **Component Build** (via `build-custom.sh`):
   
   a. **EdgeML SDK Build** (`src/edgemlsdk/build.sh`):
      - Builds custom NVIDIA Triton Server with EdgeML plugins
      - Compiles GStreamer plugins (`emltriton`, `emlcapture`, `emexifextract`)
      - Creates .deb packages and Python wheels
      - Packages Triton installation files as tar.gz
      - Architecture-specific build (ARM64 or x86-64)
      - Ubuntu version-specific (18.04 or 20.04)

   b. **Docker Image Build**:
      ```bash
      # Backend container (includes Triton, Python, GStreamer)
      docker build --build-arg OS=18.04 -f src/backend/Dockerfile -t flask-app .
      
      # Frontend container (React + Nginx)
      docker build -f src/frontend/Dockerfile -t react-webapp .
      ```

   c. **Image Export**:
      ```bash
      docker save --output flask-app.tar flask-app
      docker save --output react-webapp.tar react-webapp
      ```

   d. **Artifact Packaging**:
      - Create zip archive with Docker images, docker-compose.yaml, and host scripts
      - Architecture-specific naming: `aws.edgeml.dda.LocalServer.arm64-aarch64.zip`

   e. **GDK Build**:
      ```bash
      gdk component build
      ```
      - Executes `build-custom.sh` as custom build command
      - Creates `greengrass-build/` directory structure
      - Generates component recipe with S3 artifact URIs

4. **Component Publishing**:
   ```bash
   gdk component publish
   ```
   - Uploads component artifacts to Portal Account S3
   - Registers component version in Portal Account Greengrass registry
   - Creates component ARN: `arn:aws:greengrass:{region}:{portal-account}:components:{component-name}:versions:{version}`

5. **Post-Publish Configuration**:
   - Tags component for DDA Portal visibility:
     ```
     dda-portal:managed=true
     dda-portal:component-type=local-server
     dda-portal:architecture=arm64
     ```
   - Displays component details and next steps
   - Provides instructions for portal integration

**Build Time:**
- ARM64: ~45-60 minutes (includes EdgeML SDK compilation)
- x86-64: ~30-45 minutes

**Output:**

**S3 Artifact Structure:**
```
s3://dda-component-{region}-{portal-account}/
└── aws.edgeml.dda.LocalServer.arm64/
    └── 1.0.63/
        └── aws.edgeml.dda.LocalServer.arm64-aarch64.zip
```

**Greengrass Component (Portal Account):**
```
arn:aws:greengrass:{region}:{portal-account}:components:aws.edgeml.dda.LocalServer.arm64:versions:1.0.63
```

---

#### Step 3: Component Sharing to UseCase Accounts

After building and publishing, the component becomes available as a shared component:

**Automatic Provisioning:**
- When a UseCase is onboarded via Portal UI
- Portal's SharedComponents Lambda provisions the component
- Component copied to UseCase Account Greengrass registry
- S3 bucket policy updated for cross-account access
- Tracked in SharedComponents DynamoDB table

**UseCase Account Access:**
```
arn:aws:greengrass:{region}:{usecase-account}:components:aws.edgeml.dda.LocalServer.arm64:versions:1.0.63
```

**Shared Component Benefits:**

| Aspect | Shared Component (Portal) | Per-UseCase Build |
|--------|---------------------------|-------------------|
| **Build Frequency** | Once per version | Once per UseCase |
| **Build Time** | 45-60 minutes (one-time) | 45-60 minutes × N use cases |
| **Storage Cost** | Single S3 bucket | N S3 buckets |
| **Version Consistency** | Guaranteed identical | Potential drift |
| **Update Process** | Build once, update all | Build N times |
| **Maintenance** | Single build server | N build servers |

---

#### Component Update Workflow

When a new DDA LocalServer version is needed:

1. **Build in Portal Account** (Steps 1-2 above):
   ```bash
   # On build server
   ./gdk-component-build-and-publish.sh
   ```

2. **Update Portal Configuration**:
   - Update `DDA_LOCAL_SERVER_VERSION` in `compute-stack.ts`
   - Deploy Portal stack: `cdk deploy EdgeCVPortalComputeStack`

3. **Provision to UseCase Accounts** (via Portal UI):
   - Navigate to Settings → Shared Components
   - Click "Update All UseCases"
   - Portal provisions new version to all UseCase accounts

4. **Deploy to Edge Devices** (via Portal UI):
   - Navigate to Deployments → Create Deployment
   - Select new DDA LocalServer version
   - Deploy to devices/groups

**Key Benefits:**
- Single command for complete build-to-publish workflow
- Automatic architecture detection and configuration
- Built-in error handling and validation
- Consistent versioning (NEXT_PATCH auto-increment)
- Automatic tagging for portal integration
- Clear output with next steps
- Centralized management across all use cases

---

### Phase 2: Edge Device Setup & Registration (UseCase Account)

**IMPORTANT: Edge devices are registered in the UseCase Account where they will operate. Each UseCase Account manages its own fleet of edge devices.**

**Account Context:**
- **Device Location**: UseCase Account (where ML workloads and edge devices reside)
- **IoT Core Registration**: UseCase Account IoT Core
- **Greengrass Connection**: UseCase Account Greengrass service
- **Component Access**: Receives components provisioned from Portal Account

**Why UseCase Account?**
- **Workload Isolation**: Each use case has dedicated edge devices
- **Security Boundaries**: Devices only access their UseCase resources
- **Cost Allocation**: Device costs tracked per UseCase
- **Independent Scaling**: Each use case scales independently
- **Compliance**: Data and devices stay within UseCase boundaries

---

#### Device Setup Script: `station_install/setup_station.sh`

This one-time setup script prepares the edge device for Greengrass and registers it with IoT Core in the UseCase Account:

**Prerequisites:**
- Edge device (Jetson Xavier, x86-64, ARM64 system)
- Ubuntu 18.04, 20.04, or 22.04
- Network connectivity to AWS
- AWS credentials for UseCase Account (temporary or IAM role)

**Actions:**
1. Install AWS IoT Greengrass V2
2. Create Greengrass system user and directories (`/aws_dda`)
3. Configure Greengrass with AWS region and thing name
4. Register device with UseCase Account IoT Core (creates IoT Thing)
5. Generate and attach X.509 certificates
6. Attach IAM policies for Greengrass operations (`dda-greengrass-policy`)
7. Start Greengrass service

**Usage:**
```bash
# On edge device (in UseCase Account context)
cd station_install
sudo -E ./setup_station.sh <aws-region> <thing-name>

# Example
sudo -E ./setup_station.sh us-east-1 factory-line-1-device
```

**What Happens:**
1. **IoT Thing Created** in UseCase Account:
   ```
   arn:aws:iot:{region}:{usecase-account}:thing/factory-line-1-device
   ```

2. **Certificates Generated** and attached to Thing

3. **Greengrass Installed** and configured to connect to UseCase Account

4. **Device Appears in Portal**:
   - Portal queries UseCase Account IoT Core (via assumed role)
   - Device visible in Portal UI → Devices page
   - Status: Online, no components deployed yet

**Post-Setup:**
- Device registered in UseCase Account IoT Core
- Device ready to receive Greengrass deployments
- No components deployed yet (requires Portal deployment)
- Device visible in Portal UI for authorized users

**Multi-Account Flow:**

```
Edge Device (Physical)
   ↓ Greengrass connects to
UseCase Account IoT Core
   ↓ Thing registered in
UseCase Account Greengrass
   ↓ Portal queries via
Portal Account (STS AssumeRole)
   ↓ Visible in
Portal UI → Devices Page
```

---

### Phase 3: Component Deployment via Portal

**All deployments are managed through the Edge CV Portal UI**, not via manual CLI commands. This ensures:
- Proper authorization and audit logging
- Consistent deployment configurations
- Centralized monitoring and rollback capabilities
- Integration with the ML workflow pipeline

**Cross-Account Deployment Flow:**

```
Portal UI (Portal Account)
   ↓ User creates deployment
Deployments Lambda (Portal Account)
   ↓ STS AssumeRole
UseCase Account Greengrass
   ↓ Creates deployment
Edge Device (UseCase Account)
   ↓ Downloads components
   ↓ Installs and runs
DDA LocalServer + Models
```

**Deployment Workflow:**

1. **Portal UI - Create Deployment**:
   - Navigate to Deployments page
   - Click "Create Deployment"
   - Select target device or device group (from UseCase Account)
   - Choose components:
     - DDA LocalServer (required, provisioned from Portal Account)
     - Model components (optional, created in UseCase Account)
     - InferenceUploader (optional)
   - Configure component settings (e.g., InferenceUploader S3 bucket)
   - Click "Deploy"

2. **Portal Backend - Deployment Lambda** (`deployments.py`):
   - Validates user permissions (RBAC)
   - Assumes role in UseCase Account
   - Creates Greengrass deployment via AWS SDK:
     ```python
     greengrass.create_deployment(
         targetArn='arn:aws:iot:region:account:thing/device-id',
         components={
             'aws.greengrass.Nucleus': {'componentVersion': '2.15.0'},
             'aws.edgeml.dda.LocalServer.arm64': {'componentVersion': '1.0.63'},
             'model-cookies-classification-arm64': {'componentVersion': '1.0.0'},
             'aws.edgeml.dda.InferenceUploader': {
                 'componentVersion': '1.0.0',
                 'configurationUpdate': {
                     'merge': json.dumps({
                         's3Bucket': 'dda-inference-results-123456789012',
                         's3Prefix': 'usecase-id/device-id',
                         'uploadIntervalSeconds': 300
                     })
                 }
             }
         }
     )
     ```
   - Records deployment in DynamoDB (Deployments table)
   - Logs action to AuditLog table

3. **Greengrass Deployment Process** (on edge device):
   - Greengrass receives deployment notification from IoT Core
   - Downloads component artifacts from S3
   - Executes component Install lifecycle:
     - DDA LocalServer: Load Docker images from tar archives
     - Model components: Extract models to Triton repository
   - Executes component Run lifecycle:
     - DDA LocalServer: Start Docker Compose
     - InferenceUploader: Start monitoring service
   - Monitors component health
   - Reports deployment status to IoT Core

4. **Portal Monitoring**:
   - Deployment status updates in real-time
   - Shows progress: Pending → In Progress → Completed/Failed
   - Device logs accessible via CloudWatch integration
   - Deployment history tracked in DynamoDB

**Deployment Types:**

| Type | Target | Use Case |
|------|--------|----------|
| **Single Device** | IoT Thing ARN | Deploy to specific device |
| **Device Group** | Thing Group ARN | Deploy to multiple devices simultaneously |
| **Fleet-wide** | Multiple groups | Staged rollout across production lines |

**Component Configuration:**

Components can be configured during deployment via the Portal UI:

- **DDA LocalServer**: Station name, shadow names
- **Model Components**: Auto-loaded into Triton (no config needed)
- **InferenceUploader**: S3 bucket, upload interval, retention policy

**Rollback Capability:**

The Portal supports deployment rollback:
- View deployment history per device
- Select previous successful deployment
- Click "Rollback" to revert to previous component versions
- Greengrass handles the rollback deployment automatically

---

## Edge Application Runtime

### Startup Sequence

1. **Greengrass Component Start**:
   - Environment variables set by Greengrass
   - Host scripts execute: `setup_dda_users.sh`, `setup_paths.sh`
   - Docker Compose starts with appropriate profile (tegra or generic)

2. **Backend Container Initialization** (`src/backend/app.py`):

   ```python
   # Startup order in app.py
   1. Setup logging (structlog)
   2. Setup Triton environment (create_virtual_env, cp_model_conversion_files)
   3. Run Alembic database migrations (configuration_database, metadata_database)
   4. Backfill database with default data
   5. Initialize Triton client (TritonEdgeClient.get_instance())
   6. Setup digital input workflows (GPIO triggers)
   7. Connect to saved cameras (Aravis)
   8. Start FastAPI server (uvicorn)
   9. Start capture task manager (async background tasks)
   ```

3. **Triton Server Initialization**:
   - Triton server starts automatically in backend container
   - Loads models from `/aws_dda/dda_triton/triton_model_repo/`
   - Exposes gRPC on localhost:8001, HTTP on localhost:8000
   - Models must be in READY state before inference

4. **Frontend Container**:
   - Nginx serves React SPA on port 3000 (HTTP) or 3443 (HTTPS)
   - Proxies API requests to backend on port 5000/5443

### Inference Workflow

**Trigger Types:**
1. **Manual Capture**: User clicks "Capture" in UI
2. **Digital Input**: GPIO trigger from PLC/sensor
3. **Scheduled**: Timer-based capture
4. **Continuous**: Video stream processing

**GStreamer Pipeline Example:**
```bash
gst-launch-1.0 \
  filesrc location=/aws_dda/images/test.jpg ! \
  jpegdec ! \
  videoconvert ! \
  capsfilter caps=video/x-raw,format=RGB ! \
  emltriton \
    model-repo=/aws_dda/dda_triton/triton_model_repo \
    model=model-cookies-classification-arm64 \
    metadata='{"capture_id": "test-123"}' ! \
  jpegenc ! \
  emlcapture \
    buffer-message-id=file-target_/aws_dda/inference-results/test.jpg \
    interval=0
```

**Pipeline Components:**
- `filesrc` / `v4l2src` / `rtspsrc`: Image/video source
- `jpegdec` / `h264parse`: Decoder
- `videoconvert`: Format conversion
- `emltriton`: Triton inference plugin (custom)
- `emlcapture`: Result capture plugin (custom)

**Inference Flow:**
```
1. Image Acquisition
   ├─ Camera capture (GigE Vision, USB, RTSP)
   ├─ File input
   └─ Digital I/O trigger

2. Preprocessing (marshal_model)
   ├─ Resize to model input size
   ├─ Normalize pixel values
   ├─ Convert color space (RGB/BGR)
   └─ Add batch dimension

3. Inference (base_model)
   ├─ Load compiled model in Triton
   ├─ Execute inference on CPU/GPU
   └─ Return raw predictions

4. Postprocessing (main model)
   ├─ Apply confidence thresholds
   ├─ Non-max suppression (for detection)
   ├─ Format results (bounding boxes, masks, labels)
   └─ Generate overlay image

5. Result Storage
   ├─ Save annotated image to /aws_dda/inference-results/
   ├─ Save metadata JSON
   ├─ Update SQLite database
   └─ Trigger output actions (digital I/O, webhooks)

6. Optional Upload (InferenceUploader)
   ├─ Monitor inference-results directory
   ├─ Batch upload to S3
   └─ Clean up local files
```

### Camera Management

**Supported Cameras:**
- GigE Vision (GenICam 2.0)
- USB Vision
- RTSP/ONVIF

**Aravis Integration:**
- Camera discovery via `arv-tool-0.8`
- Configuration: exposure, gain, frame rate, ROI
- Trigger modes: software, hardware, continuous
- Pixel formats: Mono8, RGB8, BayerRG8

**Camera Lifecycle:**
```python
# Camera connection
camera_manager.connect_camera(camera_id)

# Configuration
camera.set_exposure(10000)  # microseconds
camera.set_gain(1.5)
camera.set_frame_rate(30)

# Capture
image = camera.capture_image()

# Disconnect
camera_manager.disconnect_camera(camera_id)
```

### Digital I/O

**Input Triggers:**
- GPIO pins on Jetson devices
- PLC voltage signals
- Beam break sensors

**Output Actions:**
- Stack lights (pass/fail indication)
- Diverters (reject parts)
- PLC signals

**Configuration:**
```json
{
  "inputConfigurations": [
    {
      "pin": 7,
      "trigger_type": "rising_edge",
      "debounce_ms": 50,
      "workflow_id": "workflow-123"
    }
  ],
  "outputConfigurations": [
    {
      "pin": 11,
      "action": "set_high",
      "condition": "defect_detected",
      "duration_ms": 1000
    }
  ]
}
```

---

## Cloud Portal Architecture

### Authentication & Authorization

**Cognito User Pool:**
- Email/username sign-in
- Password policy: 12+ chars, mixed case, digits, symbols
- Custom attributes: `role`, `groups`
- Account recovery via email
- Optional SAML/OIDC federation

**JWT Authorizer:**
- Validates Cognito JWT tokens
- Extracts user identity and claims
- Enforces API Gateway authorization

**RBAC Model:**

| Role | Permissions |
|------|-------------|
| **PortalAdmin** | All operations, all use cases, settings, audit logs, user management |
| **UseCaseAdmin** | Full control within assigned use cases, team management |
| **DataScientist** | Labeling, training, models, compilation within assigned use cases |
| **Operator** | Deployments, devices, monitoring within assigned use cases |
| **Viewer** | Read-only access within assigned use cases |

**Authorization Flow:**
```
1. User authenticates with Cognito
2. Receives JWT access token
3. Frontend includes token in API requests (Authorization header)
4. API Gateway JWT authorizer validates token
5. Lambda function checks user role in DynamoDB (UserRoles table)
6. Enforces permissions based on role and usecase_id
7. Logs action to AuditLog table
```

### Multi-Account Architecture

**Account Types:**

1. **Portal Account**:
   - Hosts Edge CV Portal application
   - Orchestrates cross-account operations
   - Stores metadata in DynamoDB
   - No direct access to UseCase resources

2. **UseCase Account**:
   - Runs ML workloads (SageMaker, Greengrass)
   - Hosts edge devices
   - Stores training data and models in S3
   - Grants Portal Account access via IAM role

3. **Data Account** (Optional):
   - Centralized training data storage
   - Shared across multiple UseCase accounts
   - Grants Portal and UseCase accounts access via IAM role

**Cross-Account Access:**
```
Portal Account Lambda
  ↓ STS AssumeRole
UseCase Account IAM Role (DDAPortalUseCaseRole)
  ↓ Permissions
SageMaker, Greengrass, IoT Core, S3
```

**IAM Role Trust Policy:**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::PORTAL_ACCOUNT:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "unique-external-id-per-usecase"
        }
      }
    }
  ]
}
```

### UseCase Onboarding

**Automated Configuration:**

1. **Deploy UseCase Stack** (in UseCase Account):
   ```bash
   cd edge-cv-portal/infrastructure
   ./deploy-account-role.sh  # Select option 1 for UseCase
   ```
   Creates:
   - IAM role: `DDAPortalUseCaseRole`
   - S3 bucket: `dda-usecase-{usecase-id}-{account}`
   - External ID for secure cross-account access

2. **Create UseCase in Portal**:
   - Provide UseCase name, description, AWS account ID
   - Provide IAM role ARN and External ID
   - Optional: Select Data Account for training data

3. **Auto-Configuration** (by Portal):
   - Validate IAM role and permissions
   - Configure S3 bucket policy for SageMaker cross-account access
   - Configure CORS for browser uploads (using CloudFront domain)
   - Tag bucket: `dda-portal:managed=true`
   - Provision shared components (dda-LocalServer)
   - Update GDK component bucket policy for cross-account access
   - Configure EventBridge for cross-account event forwarding
   - Create entry in DynamoDB UseCases table

4. **Shared Component Provisioning**:
   - Portal creates dda-LocalServer component in UseCase account
   - Copies component artifacts from Portal account to UseCase account
   - Registers component in UseCase Greengrass registry
   - Tracks provisioning status in SharedComponents table

---

## ML Workflow Pipeline

### Quick Start: 5-Step Workflow

The Edge CV Portal provides a streamlined, dropdown-driven workflow from data labeling to edge deployment. **All operations are performed through the Portal UI** - no CLI or manual configuration required.

**High-Level Overview:**

```
┌──────────────────────────────────────────────────────────────────┐
│                    STEP 1: DATA LABELING                         │
│  ──────────────────────────────────────────────────────────────  │
│  Portal UI: Labeling → Create Labeling Job                      │
│  • Upload images to S3 (via Portal or CLI)                      │
│  • Select dataset, configure labels, choose workforce           │
│  • Click "Create Job"                                           │
│                                                                  │
│  What Happens: SageMaker Ground Truth job created               │
│  Result: Labeled images with manifest in S3                     │
│  Duration: Hours to days (depends on dataset size)              │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│                 STEP 2: TRANSFORM MANIFEST                       │
│  ──────────────────────────────────────────────────────────────  │
│  Portal UI: Labeling → Transform Manifest                       │
│  • Select completed job, click "Transform"                      │
│                                                                  │
│  What Happens: Converts Ground Truth → DDA format               │
│  Result: Training-ready manifest                                │
│  Duration: Seconds                                              │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│                    STEP 3: MODEL TRAINING                        │
│  ──────────────────────────────────────────────────────────────  │
│  Portal UI: Training → Create Training Job                      │
│  • Select transformed manifest, configure hyperparameters       │
│  • Click "Start Training"                                       │
│                                                                  │
│  What Happens: SageMaker trains model on GPU instances          │
│  Result: Trained model artifact in S3                           │
│  Duration: Minutes to hours (depends on dataset/model)          │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│                   STEP 4: MODEL COMPILATION                      │
│  ──────────────────────────────────────────────────────────────  │
│  Portal UI: Training → View Details → Compile                   │
│  • Select target architecture (ARM64/x86-64/Jetson)             │
│  • Click "Compile Model"                                        │
│                                                                  │
│  What Happens: SageMaker optimizes → Auto-packages for Triton   │
│                → Auto-creates Greengrass component              │
│  Result: Deployable component in Greengrass registry            │
│  Duration: 5-15 minutes                                         │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│                   STEP 5: EDGE DEPLOYMENT                        │
│  ──────────────────────────────────────────────────────────────  │
│  Portal UI: Deployments → Create Deployment                     │
│  • Select device/group, choose model component                  │
│  • Click "Deploy"                                               │
│                                                                  │
│  What Happens: Greengrass downloads and installs on edge device │
│  Result: Model running and ready for inference                  │
│  Duration: 2-10 minutes (depends on model size/network)         │
└──────────────────────────────────────────────────────────────────┘
```

**Time to Production:** Typically 1-3 days for initial model, then hours for iterations.

---

### Portal Features & Automation

**What Makes the Portal Powerful:**

| Feature | Benefit | Example |
|---------|---------|---------|
| **Dropdown-Driven UI** | No manual ARN/URI entry | Select from completed jobs, not S3 paths |
| **Auto-Validation** | Catch errors before submission | Manifest format checked before training |
| **Event-Driven Sync** | Real-time status updates | No manual polling needed |
| **Auto-Packaging** | Compilation → Component (automatic) | No manual Triton restructuring |
| **RBAC** | Role-based access control | DataScientist can't deploy to production |
| **Audit Trail** | Complete action history | Who deployed what model when |
| **Multi-Account** | Seamless cross-account operations | Portal handles STS AssumeRole |

**Continuous Improvement Loop:**
```
Deploy → Collect Inference Results → Review → Re-label → Re-train → Re-deploy
```

---

### Detailed Step-by-Step Guide

> **For High-Level Readers:** The 5-step overview above is sufficient to understand the workflow.  
> **For Deep-Dive Readers:** The sections below provide technical details, API calls, and troubleshooting.

---

#### STEP 1 (Detailed): Data Collection & Labeling

**User Actions (Portal UI):**
1. Navigate to **Data Management** → Upload images to S3
2. Navigate to **Labeling** → Click **"Create Labeling Job"**
3. Configure:
   - Dataset S3 URI (browse or paste)
   - Job type (bounding box or segmentation)
   - Label categories (e.g., "defect", "good")
   - Workforce (private team or public)
4. Click **"Create Job"**

**What Happens Behind the Scenes:**
**What Happens Behind the Scenes:**

1. **Labeling Lambda** (`labeling.py`) receives request
2. **Assumes role** in UseCase Account via STS
3. **Creates SageMaker Ground Truth job**:
   ```python
   sagemaker.create_labeling_job(
       LabelingJobName='cookies-labeling-001',
       InputConfig={
           'DataSource': {
               'S3DataSource': {
                   'ManifestS3Uri': 's3://bucket/datasets/raw/manifest.json'
               }
           }
       },
       OutputConfig={
           'S3OutputPath': 's3://bucket/datasets/labeled/'
       },
       LabelCategoryConfigS3Uri='s3://bucket/label-categories.json',
       HumanTaskConfig={
           'WorkteamArn': 'arn:aws:sagemaker:region:account:workteam/private',
           'TaskTitle': 'Label defects in cookies',
           'TaskDescription': 'Draw bounding boxes around defects',
           'NumberOfHumanWorkersPerDataObject': 1,
           'TaskTimeLimitInSeconds': 600,
           'PreHumanTaskLambdaArn': 'arn:aws:lambda:...',
           'AnnotationConsolidationLambdaArn': 'arn:aws:lambda:...'
       }
   )
   ```
4. **Records job** in DynamoDB LabelingJobs table
5. **Logs action** to AuditLog table

**Labeling Process:**
- Workers access Ground Truth labeling UI
- Draw bounding boxes or segment images
- Submit labels for review
- Ground Truth consolidates annotations

**Status Monitoring:**
- **EventBridge** captures Ground Truth status changes
- **LabelingMonitor Lambda** updates DynamoDB automatically
- **Portal UI** shows real-time status (InProgress → Completed)

**Output Manifest Example:**
```json
{
  "source-ref": "s3://bucket/images/img001.jpg",
  "bounding-box": {
    "annotations": [
      {
        "class_id": 0,
        "left": 100,
        "top": 150,
        "width": 200,
        "height": 180
      }
    ],
    "image_size": [{"width": 1920, "height": 1080, "depth": 3}]
  },
  "bounding-box-metadata": {
    "job-name": "cookies-labeling-001",
    "class-map": {"0": "defect"},
    "human-annotated": "yes",
    "creation-date": "2026-01-15T10:30:00.000Z"
  }
}
```

**S3 Output Location:**
```
s3://bucket/datasets/labeled/cookies-labeling-001/
├── manifests/
│   └── output/
│       └── output.manifest  # Labeled manifest
└── annotations/
    └── consolidated-annotation/
        └── consolidation-response/
            └── {iteration}/
                └── {worker-response}.json
```

---

#### STEP 2 (Detailed): Manifest Transformation

**User Actions (Portal UI):**
1. Navigate to **Labeling** → Select completed job
2. Click **"Transform Manifest"** button
3. Portal auto-fills S3 URIs (input/output)
4. Click **"Transform"**

**What Happens Behind the Scenes:**

1. **Labeling Lambda** validates job status (must be Completed)
2. **Downloads Ground Truth manifest** from S3
3. **Transforms format**:
   ```python
   # Ground Truth format
   {
     "source-ref": "s3://bucket/images/img001.jpg",
     "bounding-box": {...}
   }
   
   # DDA format (adds metadata, validates structure)
   {
     "source-ref": "s3://bucket/images/img001.jpg",
     "bounding-box": {...},
     "bounding-box-metadata": {...}
   }
   ```
4. **Validates transformed manifest**:
   - Required fields present
   - S3 URIs accessible
   - Image dimensions valid
   - Class IDs consistent
5. **Uploads to S3**: `s3://bucket/datasets/manifests/dda-{job-name}.manifest`
6. **Updates DynamoDB**: Sets `manifest_transformed` = true
7. **Returns success** with manifest S3 URI

**Visual Indicator in Portal:**
- ✓ **Transformed** (green badge) - Ready for training
- ⚠️ **Not transformed** (yellow badge) - Needs transformation

---

#### STEP 3 (Detailed): Model Training

**User Actions (Portal UI):**
1. Navigate to **Training** → Click **"Create Training Job"**
2. Select **Ground Truth job** from dropdown
   - Portal shows ✓ Transformed or ⚠️ Not transformed
   - If not transformed, button to transform appears
3. Configure **training parameters**:
   - Model type (classification, detection, segmentation)
   - Hyperparameters (learning rate, epochs, batch size)
   - Instance type (ml.p3.2xlarge recommended)
   - Instance count (1 for most cases)
4. Click **"Start Training"**

**What Happens Behind the Scenes:**

1. **Training Lambda** (`training.py`) receives request
2. **Validates manifest**:
   - Checks format compatibility
   - Verifies S3 access permissions
   - Validates image paths exist
   - Shows helpful error messages if invalid
3. **Assumes role** in UseCase Account
4. **Creates SageMaker training job**:
   ```python
   sagemaker.create_training_job(
       TrainingJobName='cookies-training-001',
       AlgorithmSpecification={
           'TrainingImage': 'marketplace-algorithm-image',
           'TrainingInputMode': 'File'
       },
       InputDataConfig=[
           {
               'ChannelName': 'training',
               'DataSource': {
                   'S3DataSource': {
                       'S3Uri': 's3://bucket/datasets/manifests/dda-cookies.manifest',
                       'S3DataType': 'AugmentedManifestFile',
                       'AttributeNames': ['source-ref', 'bounding-box']
                   }
               },
               'ContentType': 'application/x-image'
           }
       ],
       OutputDataConfig={
           'S3OutputPath': 's3://bucket/models/training/'
       },
       ResourceConfig={
           'InstanceType': 'ml.p3.2xlarge',
           'InstanceCount': 1,
           'VolumeSizeInGB': 50
       },
       StoppingCondition={
           'MaxRuntimeInSeconds': 86400  # 24 hours
       },
       HyperParameters={
           'epochs': '50',
           'learning_rate': '0.001',
           'batch_size': '16',
           'num_classes': '2'
       }
   )
   ```
5. **Records job** in DynamoDB TrainingJobs table
6. **Logs action** to AuditLog table

**Training Process:**
- SageMaker provisions GPU instances (ml.p3.2xlarge)
- Downloads training data from S3
- Runs training algorithm (typically TensorFlow or PyTorch)
- Saves checkpoints periodically
- Uploads final model artifact to S3

**Status Monitoring:**
- **EventBridge** captures training state changes
- **TrainingEvents Lambda** updates DynamoDB automatically
- **Portal UI** shows real-time progress:
  - InProgress (with CloudWatch metrics)
  - Completed (with training metrics)
  - Failed (with error details)

**Training Output:**
```
s3://bucket/models/training/cookies-training-001/
└── output/
    └── model.tar.gz  # Contains TensorFlow SavedModel or PyTorch model
```

**Training Metrics (visible in Portal):**
- Training loss/accuracy
- Validation loss/accuracy
- Training duration
- Billable seconds
- Instance utilization

---

#### STEP 4 (Detailed): Model Compilation & Packaging

**User Actions (Portal UI):**
1. Navigate to **Training** → Select completed job
2. Click **"Compile Model"** button
3. Select **target architecture**:
   - ARM64 (for Graviton, Jetson)
   - x86-64 (for Intel/AMD)
   - Jetson Xavier (NVIDIA-specific optimizations)
4. Click **"Compile"**

**What Happens Behind the Scenes:**

**Phase 1: Compilation (5-10 minutes)**

1. **Compilation Lambda** (`compilation.py`) receives request
2. **Assumes role** in UseCase Account
3. **Creates SageMaker compilation job**:
   ```python
   sagemaker.create_compilation_job(
       CompilationJobName='cookies-compilation-arm64-001',
       InputConfig={
           'S3Uri': 's3://bucket/models/training/cookies-training-001/output/model.tar.gz',
           'DataInputConfig': '{"input": [1, 224, 224, 3]}',
           'Framework': 'TENSORFLOW'
       },
       OutputConfig={
           'S3OutputLocation': 's3://bucket/models/compiled/',
           'TargetPlatform': {
               'Os': 'LINUX',
               'Arch': 'ARM64'
           }
       },
       StoppingCondition={
           'MaxRuntimeInSeconds': 900  # 15 minutes
       }
   )
   ```
4. **Records job** in DynamoDB Models table
5. **SageMaker optimizes model** for target hardware
6. **Compilation output** saved to S3

**Phase 2: Packaging (Automatic, 2-5 minutes)**

When compilation completes, **EventBridge** triggers **CompilationEvents Lambda**, which invokes **Packaging Lambda** (`packaging.py`):

1. **Downloads compiled model** from S3
2. **Extracts and validates** model format
3. **Creates Triton model structure**:
   ```
   model-cookies-classification-arm64/
   ├── 1/                              # Version 1
   │   └── model.py                    # Main model (postprocessing)
   ├── config.pbtxt                    # Triton configuration
   base_model-cookies-classification-arm64/
   ├── 1/
   │   └── model.savedmodel/           # Compiled TensorFlow model
   │       ├── saved_model.pb
   │       └── variables/
   ├── config.pbtxt
   marshal_model-cookies-classification-arm64/
   ├── 1/
   │   └── model.py                    # Preprocessing
   └── config.pbtxt
   ```
4. **Generates Triton config files**:
   ```protobuf
   # config.pbtxt for base_model
   name: "base_model-cookies-classification-arm64"
   platform: "tensorflow_savedmodel"
   max_batch_size: 1
   input [
     {
       name: "input"
       data_type: TYPE_FP32
       dims: [224, 224, 3]
     }
   ]
   output [
     {
       name: "output"
       data_type: TYPE_FP32
       dims: [2]
     }
   ]
   ```
5. **Packages as tar.gz** and uploads to S3
6. **Invokes GreengrassPublish Lambda**

**Phase 3: Component Creation (Automatic, 1-2 minutes)**

**GreengrassPublish Lambda** (`greengrass_publish.py`):

1. **Downloads packaged model** from S3
2. **Uploads to UseCase Account S3**:
   ```
   s3://dda-usecase-{usecase-id}/components/
   └── model-cookies-classification-arm64/
       └── 1.0.0/
           └── model-cookies-classification-arm64.tar.gz
   ```
3. **Creates Greengrass component recipe**:
   ```yaml
   RecipeFormatVersion: "2020-01-25"
   ComponentName: "model-cookies-classification-arm64"
   ComponentVersion: "1.0.0"
   ComponentDescription: "Cookies defect detection model - ARM64"
   ComponentPublisher: "DDA Portal"
   Manifests:
     - Platform:
         os: linux
         architecture: aarch64
       Lifecycle:
         Install:
           Script: |
             mkdir -p /aws_dda/dda_triton/triton_model_repo
             tar -xzf {artifacts:path}/model-cookies-classification-arm64.tar.gz \
               -C /aws_dda/dda_triton/triton_model_repo/
       Artifacts:
         - URI: s3://dda-usecase-{usecase-id}/components/model-cookies-classification-arm64/1.0.0/model.tar.gz
           Unarchive: NONE
   ```
4. **Registers component** in Greengrass:
   ```python
   greengrass.create_component_version(
       inlineRecipe=recipe_yaml
   )
   ```
5. **Updates DynamoDB**:
   - Models table: status = "Available"
   - Components table: adds component entry
6. **Logs action** to AuditLog

**End Result:**
- Model component visible in Portal **Components** page
- Ready for deployment to edge devices
- No manual intervention required

---

#### STEP 5 (Detailed): Edge Deployment

**User Actions (Portal UI):**
1. Navigate to **Deployments** → Click **"Create Deployment"**
2. Select **target**:
   - Single device (dropdown of registered devices)
   - Device group (for fleet deployments)
3. Select **components**:
   - DDA LocalServer (required, auto-selected)
   - Model component (dropdown of available models)
   - InferenceUploader (optional checkbox)
4. **Configure InferenceUploader** (if enabled):
   - S3 bucket (auto-filled or custom)
   - Upload interval (10s to daily)
   - Batch size (default: 100)
   - Retention days (default: 7)
5. Click **"Deploy"**

**What Happens Behind the Scenes:**

**Phase 1: Deployment Creation (Portal Backend)**

1. **Deployments Lambda** (`deployments.py`) receives request
2. **Validates deployment**:
   - Device exists and is online
   - Components are compatible with device architecture
   - User has permission to deploy to device
3. **Assumes role** in UseCase Account
4. **Creates Greengrass deployment**:
```python
# Portal assumes role in UseCase Account
credentials = sts.assume_role(
    RoleArn=usecase_role_arn,
    ExternalId=usecase_external_id
)

# Create Greengrass deployment
greengrass = boto3.client('greengrassv2', **credentials)
deployment = greengrass.create_deployment(
    targetArn=f'arn:aws:iot:{region}:{account}:thing/{device_id}',
    deploymentName=f'dda-deployment-{timestamp}',
    components={
        'aws.greengrass.Nucleus': {
            'componentVersion': '2.15.0'
        },
        'aws.edgeml.dda.LocalServer.arm64': {
            'componentVersion': '1.0.63'
        },
        'model-cookies-classification-arm64': {
            'componentVersion': '1.0.0'
        },
        'aws.edgeml.dda.InferenceUploader': {
            'componentVersion': '1.0.0',
            'configurationUpdate': {
                'merge': json.dumps({
                    's3Bucket': f'dda-inference-results-{account}',
                    's3Prefix': f'{usecase_id}/{device_id}',
                    'uploadIntervalSeconds': 300,
                    'batchSize': 100,
                    'retentionDays': 7
                })
            }
        }
    }
)

# Record in DynamoDB
deployments_table.put_item(Item={
    'deployment_id': deployment['deploymentId'],
    'usecase_id': usecase_id,
    'device_id': device_id,
    'model_id': model_id,
    'status': 'PENDING',
    'created_at': timestamp,
    'created_by': user_id
})

# Log to audit trail
audit_log_table.put_item(Item={
    'event_id': event_id,
    'user_id': user_id,
    'action': 'CREATE_DEPLOYMENT',
    'timestamp': timestamp,
    'details': {
        'device_id': device_id,
        'model_id': model_id,
        'components': list(components.keys())
    }
})
```

**Phase 2: Edge Device Installation (2-10 minutes)**

1. **Notification**: Device receives deployment via IoT Core MQTT
2. **Download**: Greengrass downloads component artifacts from S3
3. **Install Lifecycle**:
   - **DDA LocalServer**: Load Docker images from tar archives
   - **Model component**: Extract model.tar.gz to `/aws_dda/dda_triton/triton_model_repo/`
   - **InferenceUploader**: Install Python dependencies
4. **Run Lifecycle**:
   - **DDA LocalServer**: Execute `docker-compose up` with environment variables
   - **InferenceUploader**: Start monitoring `/aws_dda/inference-results/`
5. **Health Monitoring**: Greengrass monitors component status
6. **Status Reporting**: Device reports deployment status to IoT Core

**Phase 3: Portal Monitoring (Real-Time)**

Portal UI shows:
- **Deployment status**: Pending → In Progress → Completed/Failed
- **Component status**: Each component's installation progress
- **Timeline view**: Shows when each phase completed
- **Device logs**: Accessible via CloudWatch Logs integration
- **Rollback button**: One-click rollback to previous deployment

**Post-Deployment Verification:**

Portal automatically verifies:
- ✓ All components in RUNNING state
- ✓ DDA LocalServer UI accessible (port 3000)
- ✓ Triton server loaded models successfully
- ✓ InferenceUploader monitoring active (if configured)
- ✓ Device heartbeat received within 5 minutes

**Deployment Targets:**

| Target Type | ARN Format | Use Case |
|-------------|------------|----------|
| Single Device | `arn:aws:iot:region:account:thing/device-id` | Deploy to specific device |
| Device Group | `arn:aws:iot:region:account:thinggroup/group-name` | Deploy to multiple devices |
| Fleet-wide | Multiple group deployments | Staged rollout across factory |

---

### Portal vs CLI Comparison

**Why Use the Portal?**

| Aspect | Portal (Recommended) | Manual CLI | Winner |
|--------|---------------------|------------|--------|
| **Ease of Use** | Dropdown selection, auto-fill, visual wizards | Manual ARN/URI entry, complex commands | 🏆 Portal |
| **Validation** | Pre-flight checks, helpful error messages | Trial and error, cryptic AWS errors | 🏆 Portal |
| **Status Tracking** | Real-time UI updates, push notifications | Manual polling with AWS CLI | 🏆 Portal |
| **Audit Logging** | Automatic, queryable, compliance-ready | Manual logging required | 🏆 Portal |
| **RBAC** | Role-based permissions (DataScientist, Operator, etc.) | IAM policies only | 🏆 Portal |
| **Multi-Account** | Seamless cross-account (Portal handles STS) | Complex assume-role commands | 🏆 Portal |
| **Rollback** | One-click rollback to previous deployment | Manual redeployment | 🏆 Portal |
| **Team Collaboration** | Shared visibility, comments, notifications | Individual access, no collaboration | 🏆 Portal |
| **Automation** | Compilation → Packaging → Component (automatic) | Manual steps for each phase | 🏆 Portal |
| **Learning Curve** | Intuitive UI, guided workflows | Requires AWS expertise | 🏆 Portal |

**When to Use CLI:**
- Automation/CI/CD pipelines
- Bulk operations (100+ devices)
- Custom integrations
- Advanced troubleshooting

**Recommendation:** Use Portal for 95% of operations, CLI for automation only.

---

## Data Flow & Integration

### Inference Data Flow

```
┌─────────────────────────────────────────────────────────────┐
│  1. Image Acquisition                                       │
│     Camera → GStreamer → Image Buffer                       │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  2. Preprocessing (marshal_model)                           │
│     - Resize to 224x224                                     │
│     - Normalize [0-255] → [0-1]                             │
│     - Convert BGR → RGB                                     │
│     - Add batch dimension [1, 224, 224, 3]                  │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  3. Inference (base_model)                                  │
│     Triton Server → Compiled Model → Raw Predictions        │
│     Output: [batch, num_classes] e.g., [1, 2]              │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  4. Postprocessing (main model)                             │
│     - Apply softmax                                         │
│     - Get class with max probability                        │
│     - Apply confidence threshold                            │
│     - Format result JSON                                    │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  5. Result Storage                                          │
│     - Save annotated image                                  │
│     - Save metadata JSON                                    │
│     - Update SQLite database                                │
│     - Trigger output actions                                │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  6. Optional Upload (InferenceUploader)                     │
│     - Batch upload to S3                                    │
│     - Organize by date                                      │
│     - Clean up local files                                  │
└─────────────────────────────────────────────────────────────┘
```

### Training Data Flow

```
┌─────────────────────────────────────────────────────────────┐
│  1. Data Collection                                         │
│     Edge Device → S3 (raw images)                           │
│     OR Browser Upload → S3                                  │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  2. Labeling (Ground Truth)                                 │
│     S3 Images → Ground Truth UI → Labeled Manifest          │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  3. Manifest Transformation                                 │
│     Ground Truth Format → DDA Format                        │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  4. Training (SageMaker)                                    │
│     DDA Manifest → Training Job → Model Artifact            │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  5. Compilation (SageMaker)                                 │
│     Model Artifact → Compilation Job → Compiled Model       │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  6. Packaging (Lambda)                                      │
│     Compiled Model → Triton Structure → Packaged Model      │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  7. Component Creation (Greengrass)                         │
│     Packaged Model → Component Recipe → Greengrass Registry │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  8. Deployment (Greengrass)                                 │
│     Component → Edge Device → Triton Model Repo             │
└─────────────────────────────────────────────────────────────┘
```

### Event-Driven Workflows

**EventBridge Integration:**

1. **Training Completion**:
   ```
   SageMaker Training Job → EventBridge → TrainingEvents Lambda
   → Update DynamoDB → Trigger Compilation (optional)
   ```

2. **Compilation Completion**:
   ```
   SageMaker Compilation Job → EventBridge → CompilationEvents Lambda
   → Invoke Packaging Lambda → Invoke GreengrassPublish Lambda
   ```

3. **Labeling Status**:
   ```
   Ground Truth Job → EventBridge → LabelingMonitor Lambda
   → Update DynamoDB → Portal UI refresh
   ```

---

## Security & Access Control

### Edge Device Security

**Greengrass Security:**
- TLS 1.2+ for IoT Core communication
- X.509 certificates for device authentication
- Rotating credentials via IAM roles
- Secure component downloads from S3

**Container Security:**
- Privileged mode required for hardware access
- Host network mode for camera connectivity
- Volume mounts restricted to `/aws_dda`
- No external network access required (offline capable)

**Data Security:**
- Local SQLite database (no external access)
- Inference results stored locally
- Optional encrypted upload to S3
- HTTPS for UI access (optional)

### Cloud Portal Security

**API Security:**
- JWT-based authentication
- API Gateway throttling (10,000 req/sec)
- Lambda execution role least-privilege
- VPC endpoints for private connectivity (optional)

**Data Security:**
- DynamoDB encryption at rest
- S3 encryption (SSE-S3)
- CloudFront HTTPS only
- Cognito password policies

**Cross-Account Security:**
- STS AssumeRole with ExternalID
- Time-limited session tokens (1 hour)
- Least-privilege IAM policies
- Audit logging to DynamoDB

**RBAC Enforcement:**
```python
# Example authorization check
def check_permission(user_id, usecase_id, action):
    # Get user role for usecase
    role = user_roles_table.get_item(
        Key={'user_id': user_id, 'usecase_id': usecase_id}
    )['Item']['role']
    
    # Check permission
    if action == 'create_training_job':
        return role in ['PortalAdmin', 'UseCaseAdmin', 'DataScientist']
    elif action == 'create_deployment':
        return role in ['PortalAdmin', 'UseCaseAdmin', 'Operator']
    elif action == 'view_devices':
        return role in ['PortalAdmin', 'UseCaseAdmin', 'Operator', 'Viewer']
    else:
        return False
```

---

## Monitoring & Operations

### Edge Device Monitoring

**Greengrass Logs:**
```bash
# Component logs
/aws_dda/greengrass/v2/logs/aws.edgeml.dda.LocalServer.*.log
/aws_dda/greengrass/v2/logs/model-*.log

# Greengrass system logs
/aws_dda/greengrass/v2/logs/greengrass.log
```

**Docker Logs:**
```bash
# Backend container
docker logs awsedgemlddalocalserverarm64-backend_generic-1

# Frontend container
docker logs awsedgemlddalocalserverarm64-frontend-1
```

**CloudWatch Logs** (optional):
- Greengrass forwards logs to CloudWatch
- Log group: `/aws/greengrass/{component-name}/{device-id}`
- Retention: 7 days (configurable)

**Device Health Metrics:**
- CPU usage
- Memory usage
- Disk usage
- Inference latency
- Inference throughput
- Model accuracy

### Portal Monitoring

**CloudWatch Metrics:**
- Lambda invocations, errors, duration
- API Gateway requests, latency, errors
- DynamoDB read/write capacity
- S3 storage, requests

**CloudWatch Logs:**
- Lambda function logs: `/aws/lambda/EdgeCVPortal*`
- API Gateway access logs
- EventBridge rule executions

**Audit Logging:**
- All user actions logged to AuditLog table
- Includes: user_id, usecase_id, action, timestamp, details
- Retention: 90 days (TTL)
- Queryable by user, usecase, or time range

---

## Troubleshooting Guide

### Common Edge Issues

**1. Triton Server Not Initialized**
```
Error: AttributeError: 'NoneType' object has no attribute 'get_model_status'
```
**Cause:** Model component deployed after LocalServer started.
**Solution:**
```bash
# Restart backend container
sudo docker restart awsedgemlddalocalserverarm64-backend_generic-1
```

**2. Model Loading Errors**
```
Error: Model 'model-cookies-classification-arm64' failed to load
```
**Cause:** Model syntax errors or missing files.
**Solution:**
```bash
# Test Triton directly
sudo docker exec -it awsedgemlddalocalserverarm64-backend_generic-1 bash
cd /opt/tritonserver/bin
./tritonserver --model-repository /aws_dda/dda_triton/triton_model_repo/
# Check for errors in output
```

**3. GStreamer Pipeline Crashes**
```
Error: Segmentation fault (core dumped)
```
**Cause:** Model loading issues or corrupted model files.
**Solution:**
1. Verify model loads in Triton (see above)
2. Test simplified pipeline without inference
3. Check GStreamer debug logs: `export GST_DEBUG=4`

**4. Camera Connection Failures**
```
Error: Unable to connect to camera
```
**Solution:**
```bash
# List available cameras
arv-tool-0.8 -l

# Test camera connectivity
arv-camera-test-0.8 -n "Camera-Name"

# Check network settings (for GigE cameras)
sudo ifconfig
```

### Common Portal Issues

**1. Cross-Account Access Denied**
```
Error: AccessDenied when calling AssumeRole
```
**Solution:**
- Verify IAM role trust policy includes Portal account
- Check ExternalID matches UseCase configuration
- Ensure role has required permissions

**2. Training Job Fails**
```
Error: ClientError: Manifest file is invalid
```
**Solution:**
- Validate manifest format using Portal UI
- Check S3 URIs are accessible
- Verify manifest transformation completed

**3. Component Deployment Stuck**
```
Status: In Progress (>10 minutes)
```
**Solution:**
```bash
# Check Greengrass logs on device
sudo tail -f /aws_dda/greengrass/v2/logs/greengrass.log

# Check component logs
sudo tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.LocalServer.*.log

# Restart Greengrass
sudo systemctl restart greengrass
```

---

## Appendix

### File Locations

**Edge Device:**
```
/aws_dda/
├── greengrass/v2/              # Greengrass installation
├── dda_data/                   # SQLite database
├── dda_triton/
│   └── triton_model_repo/      # Triton models
├── image-capture/              # Captured images
├── inference-results/          # Inference outputs
└── {model-name}/               # Model-specific data
```

**Build Server:**
```
DefectDetectionApplication/
├── src/
│   ├── backend/                # Backend source
│   ├── frontend/               # Frontend source
│   └── edgemlsdk/              # EdgeML SDK source
├── greengrass-build/           # GDK build output
├── custom-build/               # Docker images
└── recipe.yaml                 # Component recipe
```

### Key Commands

**Build & Publish:**
```bash
# Build DDA LocalServer component
./gdk-component-build-and-publish.sh

# Build InferenceUploader component
./build-inference-uploader.sh
```

**Deployment:**
```bash
# Deploy to single device
aws greengrassv2 create-deployment \
  --target-arn "arn:aws:iot:region:account:thing/device-id" \
  --components '{...}'

# Deploy to device group
aws greengrassv2 create-deployment \
  --target-arn "arn:aws:iot:region:account:thinggroup/group-name" \
  --components '{...}'
```

**Monitoring:**
```bash
# View Greengrass logs
sudo tail -f /aws_dda/greengrass/v2/logs/greengrass.log

# View Docker logs
docker logs -f awsedgemlddalocalserverarm64-backend_generic-1

# Check Triton status
curl http://localhost:8000/v2/health/ready
```

---

**End of Document**
