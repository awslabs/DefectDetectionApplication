# DDA User Guide

This guide walks through every section of the Defect Detection Application (DDA), one page per section, with screenshots and an explanation of each field and value a user fills in. It covers the three surfaces a user works with:

- **The Portal** – the central web application where use cases, data, labeling, training, models, workflows, builds, deployments and devices are managed.
- **The device web UI** – the LocalServer's own web interface on each edge device, used at the station to run inference and review results.
- **The Quality Station HMI** – the full-screen operator display for a station monitor.

Deployment of the Portal itself (CDK stacks, accounts, the first admin user) is covered by the [README](../../README.md#deployment) and the [Administrator Guide](../../edge-cv-portal/ADMIN_GUIDE.md); this guide starts where those end, with a running Portal and a signed-in user.

The worked example throughout is the **cookies** use case and its **blue plates** dataset: images of blue inspection plates that are labeled for plates and surface defects, trained into a detector, built into a workflow and deployed to a Jetson station. Screenshots were taken from a live Portal; account IDs, email addresses, bucket names and device names that identify that installation are blurred.

## Sections

| # | Section | Portal page | Video module |
|---|---------|-------------|--------------|
| 1 | [Getting started: signing in and finding your way around](01-getting-started.md) | Sign in, Dashboard, navigation | [Overview](https://youtu.be/lTLSIMHLy7Y) |
| 2 | [Use Cases](02-use-cases.md) | Use Cases | [Module 0: Portal Setup](https://youtu.be/HtH_4zT23Fo) |
| 3 | [Data Management](03-data-management.md) | Data Management | – |
| 4 | [Labeling](04-labeling.md) | Labeling, My Labeling Tasks | [Module 2: Labeling](https://youtu.be/pxSoAIOLp18) |
| 5 | [Synthetic Data](05-synthetic-data.md) | Synthetic Data | [Module 5: Synthetic Data](https://youtu.be/n-egAmfZiJw) |
| 6 | [Training](06-training.md) | Training | [Module 9: Training Models](https://youtu.be/azKIaPPlhZM) |
| 7 | [Models](07-models.md) | Models | [Module 9: Training Models](https://youtu.be/azKIaPPlhZM) |
| 8 | [Workflows](08-workflows.md) | Workflows (Workflow Builder) | [Module 3: Workflows](https://youtu.be/vpL62wIT218) |
| 9 | [Workflow Tuning](09-workflow-tuning.md) | Workflow Tuning → VLM/LLM Anomaly Tuning | [Module 10: Prompt Tuning](https://youtu.be/ZnxGwMxCB1M) |
| 10 | [Node Designer](10-node-designer.md) | Node Designer, Git connections | [Module 4: Custom Node Designer](https://youtu.be/si5gOzzPhn0) |
| 11 | [Components](11-components.md) | Components | – |
| 12 | [Builds](12-builds.md) | Builds | [Module 6: Build Servers](https://youtu.be/ZNJBk94Ucjo) |
| 13 | [Deployments](13-deployments.md) | Deployments | [Module 7: Deployments](https://youtu.be/FTYq8aql-CU) |
| 14 | [Workflow dashboards](14-workflow-dashboards.md) | Workflow dashboards, Telemetry settings | – |
| 15 | [Devices](15-devices.md) | Devices | [Module 1: Device Setup](https://youtu.be/PFdFycCQXA0), [Module 8: Devices](https://youtu.be/r8nz33LQPoc) |
| 16 | [Debug log](16-debug-log.md) | Debug log | – |
| 17 | [Plugin Review](17-plugin-review.md) | Plugin Review | [Module 4: Custom Node Designer](https://youtu.be/si5gOzzPhn0) |
| 18 | [Build Fleet](18-build-fleet.md) | Build Fleet | [Module 6: Build Servers](https://youtu.be/ZNJBk94Ucjo) |
| 19 | [Settings](19-settings.md) | Settings | [Module 0: Portal Setup](https://youtu.be/HtH_4zT23Fo) |
| 20 | [User Manager](20-user-manager.md) | User menu → User Manager | [Module 11: User Access](https://youtu.be/_HDKtKvft3c) |
| 21 | [Audit Logs](21-audit-logs.md) | Audit Logs | – |
| 22 | [Device web UI](22-device-web-ui.md) | On the edge device, port 3000 | [Module 12: Edge Software](https://youtu.be/pInKqoADn2c) |
| 23 | [Quality Station HMI](23-station-hmi.md) | On the edge device, `/hmi/` | – |

## The end-to-end path

A new installation is usually taken through the sections in this order. Each step names the section that explains it.

1. A PortalAdmin creates the **use case** and its S3 bucket ([2](02-use-cases.md)) and adds the team ([20](20-user-manager.md), [2](02-use-cases.md)).
2. Images are uploaded or browsed in **Data Management** ([3](03-data-management.md)).
3. A **labeling job** labels them, with a private labeling team and optional model-assisted pre-labeling ([4](04-labeling.md)). Synthetic defect images can widen a small dataset ([5](05-synthetic-data.md)).
4. A **training job** trains a classifier, segmentation model or detector from the labeled manifest ([6](06-training.md)); the result lands in **Models**, where it is compiled or packaged into a Greengrass component ([7](07-models.md)). Existing ONNX, PyTorch or LLM models are imported there instead.
5. A **workflow** connects a camera, the model and the station's outputs, is validated and packaged ([8](08-workflows.md)); custom processing nodes come from the **Node Designer** ([10](10-node-designer.md), [17](17-plugin-review.md)).
6. The **LocalServer** component for the device's architecture is built once per release in **Builds** ([12](12-builds.md), [18](18-build-fleet.md)).
7. The device is registered with **Add Device** ([15](15-devices.md)) and receives a **deployment** of LocalServer, the model and the workflow ([13](13-deployments.md)).
8. Operators run and review inspections on the device ([22](22-device-web-ui.md), [23](23-station-hmi.md)), while the Portal shows telemetry ([14](14-workflow-dashboards.md)), debug logs ([16](16-debug-log.md)) and prompt tuning ([9](09-workflow-tuning.md)).

## Roles

What a user can see and do depends on the role held in the Portal's user registry. The full permission matrix is in the [Administrator Guide](../../edge-cv-portal/ADMIN_GUIDE.md#user-roles-and-permissions); in short:

| Role | Typical user | Reach |
|------|--------------|-------|
| **PortalAdmin** | Portal owner | Everything, across all use cases, plus Use Cases creation, Build Fleet, Settings, User Manager and Plugin Review. |
| **UseCaseAdmin** | Team lead | Everything inside the assigned use cases, including team membership, allowed buckets and audit logs. |
| **DataScientist** | ML engineer | Labeling, synthetic data, training, models, workflows, workflow tuning, node designer and builds. |
| **Operator** | Line / station owner | Deployments and devices (register, deploy, restart, logs). |
| **Viewer** | Stakeholder | Read-only view of jobs, models, deployments and devices. |
| **DataLabeler** | Labeling workforce | Only **My Labeling Tasks**, the labeling workspace. |

Each section below lists the roles that can use it.

## PDF version

`docs/user-guide/build-pdf.sh` renders this guide, with its screenshots, into a single `DDA-User-Guide.pdf` (about 180 pages). It needs only Docker: Markdown conversion and the Chromium print run happen inside the Playwright container.

## Conventions used in this guide

- Portal page names are written in **bold** exactly as they appear in the navigation.
- Field names are written in **bold**; values you type are written in `monospace`.
- Steps are numbered. Where a form has many fields, a table lists each field, what to enter, and the default.
- "The use case selector" means the **Use Case** dropdown at the top of most pages; its choice is remembered across pages.
