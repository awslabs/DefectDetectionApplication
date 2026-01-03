# Design Document

## Overview

This design implements comprehensive compilation job visibility in the DDA Portal frontend. The solution adds a new "Compilation" tab to the training job detail page, enhances the training job list with compilation status indicators, and provides manual compilation triggering capabilities. The design leverages existing backend APIs and adds minimal new frontend components to display compilation job status, errors, and resulting Greengrass components.

## Architecture

### Frontend Architecture
```
TrainingDetail.tsx
├── Overview Tab (existing)
├── Logs Tab (existing)
└── Compilation Tab (new)
    ├── CompilationJobsTable
    ├── ManualCompilationForm
    └── GreengrassComponentsSection

Training.tsx (enhanced)
├── CompilationStatusIndicator (new column)
└── Enhanced filtering by compilation status
```

### Data Flow
1. **Automatic Compilation**: Training completion → EventBridge → training_events.py → compilation.py → DynamoDB update
2. **Manual Compilation**: User action → API call → compilation.py → DynamoDB update
3. **Status Updates**: Frontend polling → API → compilation.py → SageMaker describe calls → updated status
4. **Component Tracking**: Compilation success → packaging.py → Greengrass component creation → DynamoDB update

## Components and Interfaces

### New Frontend Components

#### CompilationTab Component
```typescript
interface CompilationTabProps {
  trainingId: string;
  trainingJob: TrainingJob;
}

interface CompilationJob {
  target: string;
  compilation_job_name: string;
  compilation_job_arn: string;
  status: 'InProgress' | 'Completed' | 'Failed' | 'Stopped';
  compiled_model_s3?: string;
  failure_reason?: string;
  created_at?: number;
  completed_at?: number;
}
```

#### CompilationStatusIndicator Component
```typescript
interface CompilationStatusProps {
  compilationJobs: CompilationJob[];
  showDetails?: boolean;
}

// Status types: 'not-started' | 'in-progress' | 'completed' | 'failed' | 'partial'
```

#### ManualCompilationForm Component
```typescript
interface ManualCompilationFormProps {
  trainingId: string;
  onCompilationStarted: (jobs: CompilationJob[]) => void;
  disabled: boolean;
}

interface CompilationTarget {
  id: string;
  name: string;
  description: string;
  architecture: string;
}
```

### Enhanced API Service Methods

The API service already has the compilation methods added:
- `startCompilation(trainingId: string, targets: string[])`
- `getCompilationStatus(trainingId: string)`

### Backend Integration Points

#### Existing APIs (no changes needed)
- `POST /training/{id}/compile` - Start compilation
- `GET /training/{id}/compile` - Get compilation status
- `GET /training/{id}` - Get training job (includes compilation_jobs field)

#### Data Model Extensions
The training job record in DynamoDB already includes:
```json
{
  "compilation_jobs": [
    {
      "target": "jetson-xavier",
      "compilation_job_name": "model-name-jetson-xavier-20241210-143022",
      "compilation_job_arn": "arn:aws:sagemaker:...",
      "status": "InProgress",
      "compiled_model_s3": "s3://bucket/path/compiled-model.tar.gz"
    }
  ]
}
```

## Data Models

### TypeScript Interfaces

#### CompilationJob Interface
```typescript
export interface CompilationJob {
  target: string;
  compilation_job_name: string;
  compilation_job_arn: string;
  status: 'InProgress' | 'Completed' | 'Failed' | 'Stopped';
  compiled_model_s3?: string;
  failure_reason?: string;
  error?: string;
  created_at?: number;
  completed_at?: number;
}
```

#### Enhanced TrainingJob Interface
```typescript
export interface TrainingJob {
  // ... existing fields
  compilation_jobs?: CompilationJob[];
  compilation_status?: 'not-started' | 'in-progress' | 'completed' | 'failed' | 'partial';
}
```

#### GreengrassComponent Interface
```typescript
export interface GreengrassComponent {
  component_name: string;
  component_version: string;
  component_arn: string;
  target_architecture: string;
  status: 'creating' | 'active' | 'failed';
  created_at: number;
  deployment_count: number;
}
```

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system-essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

Property 1: Compilation status display completeness
*For any* training job with compilation data, the UI should display compilation status for all target architectures present in the data
**Validates: Requirements 1.1**

Property 2: Real-time status updates
*For any* compilation job in progress, the UI should show appropriate progress indicators and update status every 30 seconds
**Validates: Requirements 1.2, 1.5**

Property 3: Successful compilation artifact display
*For any* completed compilation job, the UI should display the S3 location of compiled model artifacts
**Validates: Requirements 1.3**

Property 4: Error message display
*For any* failed compilation job, the UI should display detailed error messages and failure reasons
**Validates: Requirements 1.4, 5.1**

Property 5: Manual compilation availability
*For any* completed training job, the UI should provide a button to start compilation for target architectures
**Validates: Requirements 2.1**

Property 6: Target architecture selection
*For any* manual compilation form, multiple target architectures should be selectable from available options
**Validates: Requirements 2.2**

Property 7: Duplicate compilation prevention
*For any* training job and target combination, the system should prevent creation of duplicate compilation jobs
**Validates: Requirements 2.3**

Property 8: UI state updates after actions
*For any* manual compilation trigger, the UI should immediately update to show new compilation jobs
**Validates: Requirements 2.4**

Property 9: Training job status validation
*For any* training job not in completed status, compilation should not be allowed
**Validates: Requirements 2.5**

Property 10: Compilation tab presence
*For any* training job detail view, a "Compilation" tab should be present alongside Overview and Logs tabs
**Validates: Requirements 3.1**

Property 11: Compilation table completeness
*For any* compilation job data, the table should display target, status, duration, and artifact location columns
**Validates: Requirements 3.2**

Property 12: List view status indicators
*For any* training job with compilation data, the main list should show compilation status indicators
**Validates: Requirements 3.3, 6.1**

Property 13: Target architecture organization
*For any* set of compilation jobs, they should be organized by target architecture for comparison
**Validates: Requirements 3.4**

Property 14: Artifact download links
*For any* compilation job with artifacts, direct download links should be provided
**Validates: Requirements 3.5**

Property 15: Greengrass component information display
*For any* completed packaging process, Greengrass component information should include name, version, and ARN
**Validates: Requirements 4.1**

Property 16: Model-component relationship display
*For any* compiled model with corresponding Greengrass components, the relationship should be clearly shown
**Validates: Requirements 4.2**

Property 17: AWS console link generation
*For any* created Greengrass component, links to AWS console should be generated correctly
**Validates: Requirements 4.3**

Property 18: Component creation status display
*For any* component creation process, status and packaging errors should be displayed
**Validates: Requirements 4.4**

Property 19: Device deployment relationship
*For any* Greengrass component, devices that have the component deployed should be shown
**Validates: Requirements 4.5**

Property 20: Compilation guidance display
*For any* compilation failure, guidance on common causes and resolution steps should be provided
**Validates: Requirements 5.2**

Property 21: Compilation log download
*For any* compilation job with available logs, download functionality should be provided
**Validates: Requirements 5.3**

Property 22: Duration and metrics display
*For any* compilation job, duration and resource usage information should be displayed
**Validates: Requirements 5.4**

Property 23: Compilation history preservation
*For any* training job, all compilation attempts including failed ones should be maintained and displayed
**Validates: Requirements 5.5**

Property 24: Visual status indicators
*For any* compilation state (not compiled, compiling, completed, failed), distinct visual indicators should be used
**Validates: Requirements 6.2**

Property 25: Compilation statistics accuracy
*For any* set of compilation jobs, the count of successful vs total target architectures should be calculated correctly
**Validates: Requirements 6.3**

Property 26: Compilation status filtering
*For any* compilation status filter applied, only training jobs matching that status should be displayed
**Validates: Requirements 6.4**

Property 27: Tooltip information display
*For any* compilation indicator hover action, detailed compilation information should appear in tooltips
**Validates: Requirements 6.5**

<function_calls>
<invoke name="prework">
<parameter name="featureName">compilation-job-visibility
## Error
 Handling

### Frontend Error Handling
- **API Failures**: Display user-friendly error messages when compilation API calls fail
- **Network Issues**: Show retry options and offline indicators
- **Invalid States**: Prevent actions on training jobs in invalid states
- **Missing Data**: Gracefully handle missing compilation job data

### Backend Error Handling
- **SageMaker API Errors**: Capture and store detailed error messages from SageMaker compilation failures
- **Cross-Account Access**: Handle role assumption failures gracefully
- **Resource Limits**: Provide clear messages when compilation limits are exceeded
- **Timeout Handling**: Handle long-running compilation jobs appropriately

### User Experience
- **Loading States**: Show spinners and progress indicators during API calls
- **Error Recovery**: Provide clear actions users can take to resolve issues
- **Validation Messages**: Show real-time validation feedback in forms
- **Confirmation Dialogs**: Confirm destructive or expensive actions

## Testing Strategy

### Unit Testing
- **Component Rendering**: Test that all compilation UI components render correctly with various data states
- **API Integration**: Test API service methods for compilation endpoints
- **State Management**: Test React state updates and polling mechanisms
- **Form Validation**: Test manual compilation form validation logic
- **Error Boundaries**: Test error handling in compilation components

### Property-Based Testing
- **Status Display**: Test that compilation status is displayed correctly across all possible job states
- **Data Consistency**: Test that UI data matches backend data across different compilation scenarios
- **Polling Behavior**: Test that status updates occur at correct intervals
- **Filter Logic**: Test that compilation status filtering works correctly
- **Link Generation**: Test that download and console links are generated correctly

### Integration Testing
- **End-to-End Workflows**: Test complete compilation workflow from training completion to component creation
- **Cross-Component Communication**: Test data flow between training and compilation components
- **Real-time Updates**: Test that compilation status updates propagate correctly through the UI
- **Error Scenarios**: Test UI behavior during various compilation failure scenarios

### Testing Framework
- **Frontend**: Jest + React Testing Library for unit tests, Cypress for integration tests
- **Property Testing**: Use fast-check for JavaScript property-based testing
- **API Testing**: Mock Service Worker (MSW) for API mocking during tests
- **Visual Testing**: Storybook for component visual testing

### Test Configuration
- Property-based tests should run a minimum of 100 iterations
- Each property-based test must include a comment referencing the design document property
- Test format: `**Feature: compilation-job-visibility, Property {number}: {property_text}**`

## Implementation Phases

### Phase 1: Core Compilation Display
1. Add CompilationJob TypeScript interfaces
2. Enhance API service with compilation methods
3. Create CompilationTab component
4. Add basic compilation status display

### Phase 2: Manual Compilation
1. Create ManualCompilationForm component
2. Add compilation triggering functionality
3. Implement target architecture selection
4. Add duplicate prevention logic

### Phase 3: List View Enhancements
1. Create CompilationStatusIndicator component
2. Add compilation status column to training list
3. Implement compilation status filtering
4. Add tooltip functionality

### Phase 4: Advanced Features
1. Add Greengrass component display
2. Implement error guidance system
3. Add compilation log download
4. Enhance real-time polling

### Phase 5: Polish and Testing
1. Add comprehensive error handling
2. Implement loading states and animations
3. Add comprehensive test coverage
4. Performance optimization and accessibility

## Dependencies

### External Dependencies
- **AWS SDK**: For generating AWS console links
- **React Query**: For efficient data fetching and caching (optional enhancement)
- **Date-fns**: For duration calculations and time formatting

### Internal Dependencies
- **Existing API Infrastructure**: Compilation endpoints already exist
- **Shared Components**: Leverage existing CloudScape components
- **Authentication**: Use existing Cognito integration
- **Routing**: Integrate with existing React Router setup

### Backend Dependencies
- **DynamoDB Schema**: Training jobs table already supports compilation_jobs field
- **SageMaker Integration**: Existing compilation.py handles SageMaker API calls
- **EventBridge**: Existing training_events.py triggers automatic compilation

## Security Considerations

### Data Access
- **Cross-Account Roles**: Compilation data access uses existing cross-account role assumption
- **API Authorization**: All compilation endpoints require Cognito authentication
- **Data Filtering**: Users only see compilation jobs for use cases they have access to

### Sensitive Information
- **Error Messages**: Sanitize SageMaker error messages to avoid exposing sensitive information
- **S3 URLs**: Use presigned URLs for artifact downloads with appropriate expiration
- **ARN Display**: Mask account IDs in displayed ARNs where appropriate

### Input Validation
- **Target Selection**: Validate target architectures against allowed list
- **Training Job Status**: Verify training job completion before allowing compilation
- **Rate Limiting**: Prevent excessive compilation job creation

## Performance Considerations

### Frontend Performance
- **Lazy Loading**: Load compilation tab content only when accessed
- **Polling Optimization**: Only poll for updates when compilation jobs are in progress
- **Data Caching**: Cache compilation status to reduce API calls
- **Virtual Scrolling**: Use virtual scrolling for large compilation job lists

### Backend Performance
- **Batch Operations**: Batch SageMaker describe calls when checking multiple compilation jobs
- **Caching**: Cache compilation status in DynamoDB to reduce SageMaker API calls
- **Async Processing**: Use async Lambda invocations for compilation triggering

### Scalability
- **Pagination**: Implement pagination for large numbers of compilation jobs
- **Filtering**: Server-side filtering to reduce data transfer
- **Indexing**: Use DynamoDB GSI for efficient compilation job queries