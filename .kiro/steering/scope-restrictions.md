# Scope Restrictions for DDA Portal Development

## Primary Scope: edge-cv-portal/

All changes should be made within the `edge-cv-portal/` directory unless there's a specific reason to modify files outside this scope.

### Allowed Directories
- `edge-cv-portal/backend/` - Lambda functions and backend logic
- `edge-cv-portal/frontend/` - React frontend application
- `edge-cv-portal/infrastructure/` - CDK infrastructure code
- `edge-cv-portal/scripts/` - Portal-specific scripts

### Root-Level Files (Limited Scope)
The following root-level files are part of the portal build pipeline and can be modified:
- `gdk-component-build-and-publish.sh` - Builds and publishes LocalServer component
- `build-inference-uploader.sh` - Builds and publishes InferenceUploader component
- `README.md` - Main project documentation

## Out-of-Scope (Requires Justification)

The following directories should NOT be modified without explicit justification:
- `src/` - Edge device application (separate from portal)
- `src/edgemlsdk/` - EdgeML SDK (external dependency)
- `src/backend/` - Edge device backend
- `src/frontend/` - Edge device frontend
- `test/` - Test suites
- `station_install/` - Edge device installation scripts
- `com.dda.InferenceUploader/` - Greengrass component (use build scripts instead)
- `inference-uploader/` - Greengrass component (use build scripts instead)

## When Modifying Out-of-Scope Files

If you need to modify files outside `edge-cv-portal/`, you MUST:

1. **Explain why** - Provide a clear reason why the change cannot be made within edge-cv-portal/
2. **Minimize scope** - Make only the minimal necessary changes
3. **Document impact** - Explain how this affects the portal or other systems
4. **Get confirmation** - Ask for explicit approval before proceeding

### Example Justification
```
Reason: Need to fix Docker networking issue in edgemlsdk Dockerfile
Why not in edge-cv-portal: The edgemlsdk is a build dependency used by build-custom.sh
Impact: Fixes component build failures on build servers with restricted network access
Minimal change: Only modify the apt-key commands that fail in Docker containers
```

## Portal-Specific Guidelines

### Backend Changes
- All Lambda functions go in `edge-cv-portal/backend/functions/`
- Shared utilities go in `edge-cv-portal/backend/layers/shared/`
- Infrastructure code goes in `edge-cv-portal/infrastructure/lib/`

### Frontend Changes
- React components go in `edge-cv-portal/frontend/src/components/`
- Pages go in `edge-cv-portal/frontend/src/pages/`
- Services/API calls go in `edge-cv-portal/frontend/src/services/`

### Documentation
- All docs go in `README.md`

## Build Scripts

The following scripts are part of the portal build pipeline:
- `gdk-component-build-and-publish.sh` - Orchestrates component building
- `build-inference-uploader.sh` - Builds InferenceUploader component
- `build-custom.sh` - Custom build logic (calls into src/edgemlsdk)

These scripts can be modified to improve the build process, but should not make major changes to external dependencies.
