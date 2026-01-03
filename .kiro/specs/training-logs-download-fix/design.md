# Training Logs Download Fix - Design Document

## Overview

The training logs download functionality currently fails with 500 Internal Server Errors due to several issues including session name length validation, error handling, and API Gateway routing. This design addresses these issues by implementing robust session name generation, comprehensive error handling, and improved CloudWatch Logs integration.

## Architecture

The logs download system follows this flow:
1. User requests log download via API Gateway
2. Lambda function validates user permissions
3. System assumes cross-account role with shortened session name
4. CloudWatch Logs client retrieves all log events via pagination
5. Logs are formatted and returned as downloadable text file

## Components and Interfaces

### Session Name Generator
- **Purpose**: Generate AWS-compliant session names (≤64 characters)
- **Input**: User ID, timestamp, operation type
- **Output**: Valid session name string
- **Key Methods**:
  - `generate_session_name(user_id, operation, timestamp)` → string
  - `validate_session_name(name)` → boolean

### CloudWatch Logs Client
- **Purpose**: Retrieve training job logs from CloudWatch
- **Input**: Training job name, credentials
- **Output**: Formatted log events
- **Key Methods**:
  - `get_all_log_events(log_group, log_stream, credentials)` → list
  - `format_logs_for_download(events, metadata)` → string

### Error Handler
- **Purpose**: Provide consistent error responses
- **Input**: Exception type, context
- **Output**: HTTP response with appropriate status code
- **Key Methods**:
  - `handle_cloudwatch_error(exception)` → response
  - `handle_permission_error(exception)` → response

## Data Models

### Log Download Request
```python
{
    "training_id": "string",
    "user_id": "string",
    "usecase_id": "string"
}
```

### Log Download Response
```python
{
    "statusCode": 200,
    "headers": {
        "Content-Type": "text/plain",
        "Content-Disposition": "attachment; filename=...",
        "Access-Control-Allow-Origin": "*"
    },
    "body": "formatted_log_content"
}
```

### Session Name Format
```python
{
    "pattern": "{operation}-{short_user_id}-{timestamp}",
    "max_length": 64,
    "operation_codes": {
        "logs_download": "logs",
        "training_status": "train",
        "compilation": "comp"
    }
}
```

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system-essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

Property 1: Session name length validation
*For any* user ID and timestamp combination, the generated session name should be 64 characters or less
**Validates: Requirements 1.2, 3.1**

Property 2: Log download completeness
*For any* training job with available logs, the download should return all log events from CloudWatch
**Validates: Requirements 1.1, 4.1**

Property 3: Error handling consistency
*For any* CloudWatch API error, the system should return an appropriate HTTP status code with a user-friendly error message
**Validates: Requirements 1.5, 2.1, 2.2**

Property 4: Session name uniqueness preservation
*For any* set of concurrent requests with different user IDs, the generated session names should remain unique even when shortened
**Validates: Requirements 3.2**

Property 5: Timestamp format compactness
*For any* timestamp, the formatted version used in session names should be as compact as possible while maintaining uniqueness
**Validates: Requirements 3.3**

Property 6: Session name validation before API calls
*For any* generated session name, the system should validate its length before attempting AWS API calls
**Validates: Requirements 3.4**

Property 7: Log stream aggregation
*For any* training job with multiple log streams, the download should include events from all relevant streams
**Validates: Requirements 4.3**

Property 8: Log format consistency
*For any* downloaded log file, it should include timestamps, metadata, and properly formatted log events
**Validates: Requirements 4.4**

Property 9: Filename format validation
*For any* log download, the Content-Disposition header should contain a descriptive filename with training job information
**Validates: Requirements 4.5**

## Error Handling

### Session Name Length Errors
- **Detection**: Validate session name length before AWS API calls
- **Response**: Automatically shorten user ID and retry
- **Fallback**: Use hash-based shortened identifiers

### CloudWatch Access Errors
- **ResourceNotFoundException**: Return 404 with "No logs available yet"
- **AccessDeniedException**: Return 403 with permission guidance
- **ThrottlingException**: Implement exponential backoff retry

### Cross-Account Role Errors
- **InvalidParameterException**: Return 400 with session name guidance
- **AccessDeniedException**: Return 403 with role configuration help
- **TokenRefreshRequired**: Automatically retry with new credentials

### API Gateway Routing
- **Path Parameter Validation**: Ensure training_id is present and valid
- **Method Routing**: Verify correct HTTP method and path matching
- **CORS Headers**: Include proper headers for browser compatibility

## Testing Strategy

### Unit Testing
- Test session name generation with various user ID lengths
- Test error handling for different CloudWatch exceptions
- Test log formatting with sample CloudWatch events
- Test filename generation with different training job names

### Property-Based Testing
- Generate random user IDs and verify session names are always valid
- Test log pagination with varying numbers of log events
- Verify error responses for different exception types
- Test concurrent session name generation for uniqueness

### Integration Testing
- Test end-to-end log download with real training jobs
- Test cross-account role assumption with valid and invalid credentials
- Test CloudWatch API integration with various log scenarios
- Test API Gateway routing with different request formats

The testing approach uses Python's `hypothesis` library for property-based testing, with each property test configured to run a minimum of 100 iterations to ensure comprehensive coverage of edge cases.