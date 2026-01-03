# RBAC (Role-Based Access Control) System

This directory contains the shared utilities for implementing role-based access control in the DDA Portal Lambda functions.

## Overview

The RBAC system provides:
- **Authentication**: JWT token validation and user context extraction
- **Authorization**: Role-based permission checking
- **Use Case Access Control**: Restricting access to specific use cases
- **Audit Logging**: Tracking user actions for compliance

## Components

### 1. `rbac_utils.py`
Core RBAC logic including:
- `Role` enum: Defines user roles (PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer)
- `Permission` enum: Defines system permissions
- `UserContext` class: Contains user identity and permissions
- `RBACManager` class: Handles permission checking and use case access

### 2. `auth_middleware.py`
Decorators for Lambda functions:
- `@auth_required`: Requires valid JWT authentication
- `@require_permission(permission, usecase_param)`: Requires specific permission
- `@require_usecase_access(usecase_param)`: Requires use case access
- `@super_user_required`: Requires PortalAdmin role
- Convenience decorators: `@data_scientist_required`, `@operator_required`, `@admin_required`

### 3. `user_roles_dao.py`
Data access layer for UserRoles DynamoDB table:
- User-to-use-case assignments
- Role management operations
- Bulk operations

## User Roles

| Role | Description | Permissions |
|------|-------------|-------------|
| **PortalAdmin** | Super user with access to everything | All permissions across all use cases |
| **UseCaseAdmin** | Admin for specific use cases | All permissions within assigned use cases |
| **DataScientist** | ML workflow management | Labeling, training, model registry (read/write) |
| **Operator** | Deployment and device management | Deployments, device control, monitoring |
| **Viewer** | Read-only access | View-only permissions |

## Usage Examples

### Basic Authentication

```python
from auth_middleware import auth_required, create_response

@auth_required
def my_handler(event, context):
    user_context = event['user_context']
    return create_response(200, {
        'user_id': user_context.user_id,
        'roles': [role.value for role in user_context.roles]
    })
```

### Permission-Based Access

```python
from auth_middleware import require_permission
from rbac_utils import Permission

@auth_required
@require_permission(Permission.CREATE_LABELING_JOB, 'usecase_id')
def create_labeling_job(event, context):
    # Only users with CREATE_LABELING_JOB permission can access
    # Must also have access to the specified use case
    usecase_id = event['pathParameters']['usecase_id']
    # ... business logic
```

### Role-Based Access

```python
from auth_middleware import data_scientist_required

@data_scientist_required('usecase_id')
def start_training(event, context):
    # Only DataScientist, UseCaseAdmin, or PortalAdmin can access
    # Must have access to the specified use case
    # ... business logic
```

### Super User Only

```python
from auth_middleware import super_user_required

@super_user_required
def manage_system_settings(event, context):
    # Only PortalAdmin can access
    # ... business logic
```

### Use Case Access Control

```python
from auth_middleware import auth_required, require_usecase_access

@auth_required
@require_usecase_access('usecase_id')
def get_usecase_data(event, context):
    # User must be assigned to the use case
    # ... business logic
```

## Environment Variables

Required environment variables for Lambda functions:

```bash
USER_ROLES_TABLE=dda-portal-user-roles  # DynamoDB table name
```

## API Gateway Integration

The middleware expects API Gateway events with JWT authorizer context:

```json
{
  "httpMethod": "POST",
  "path": "/api/v1/usecases/usecase123/labeling",
  "pathParameters": {
    "usecase_id": "usecase123"
  },
  "requestContext": {
    "authorizer": {
      "sub": "user123",
      "email": "user@example.com",
      "custom:role": "DataScientist",
      "custom:groups": "cv-data-scientists"
    }
  },
  "body": "{\"name\": \"My Labeling Job\"}"
}
```

## JWT Token Claims

Expected JWT claims from Cognito or IdP:

| Claim | Description | Example |
|-------|-------------|---------|
| `sub` | User ID | `user123` |
| `email` | User email | `user@example.com` |
| `custom:role` | Single role | `DataScientist` |
| `custom:groups` | Comma-separated groups | `cv-data-scientists,cv-operators` |

## Group to Role Mapping

SSO groups are mapped to Portal roles:

| SSO Group | Portal Role |
|-----------|-------------|
| `portal-admins` | `PortalAdmin` |
| `cv-usecase-admins` | `UseCaseAdmin` |
| `cv-data-scientists` | `DataScientist` |
| `cv-operators` | `Operator` |
| `cv-viewers` | `Viewer` |

## Error Responses

The middleware returns standardized error responses:

### 401 Unauthorized
```json
{
  "error": "Authentication required",
  "message": "Valid JWT token required"
}
```

### 403 Forbidden
```json
{
  "error": "Insufficient permissions",
  "required_permission": "create_labeling_job",
  "usecase_id": "usecase123",
  "message": "This action requires create_labeling_job permission"
}
```

### 403 Use Case Access Denied
```json
{
  "error": "Use case access denied",
  "usecase_id": "usecase123",
  "message": "You do not have access to this use case"
}
```

## Testing

Unit tests are provided in the `tests/` directory:
- `test_rbac_utils.py`: Tests core RBAC logic
- `test_auth_middleware.py`: Tests middleware decorators

Run tests with:
```bash
python -m pytest tests/test_rbac_utils.py -v
python -m pytest tests/test_auth_middleware.py -v
```

## Best Practices

1. **Always use `@auth_required` first** in the decorator chain
2. **Use specific permissions** rather than role checks when possible
3. **Include use case parameters** for use case-specific operations
4. **Log security events** for audit purposes
5. **Handle errors gracefully** with user-friendly messages
6. **Test permission logic** thoroughly with unit tests

## Deployment

The shared utilities are deployed as a Lambda layer:

1. Package the Python files into a layer
2. Set the layer path to `/opt/python`
3. Add the layer to your Lambda functions
4. Set required environment variables

## Security Considerations

- **JWT validation** is handled by API Gateway authorizer
- **Cross-account access** uses STS AssumeRole with ExternalID
- **Audit logging** captures all user actions
- **Least privilege** principle enforced through role permissions
- **Use case isolation** prevents cross-contamination