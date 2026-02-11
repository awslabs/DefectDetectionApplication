"""
Device Logs Analyzer - Pattern-based intelligent log analysis
Analyzes Greengrass device logs and provides actionable troubleshooting guidance
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError
from shared_utils import (
    create_response, get_user_from_event, check_user_access, is_super_user, 
    get_usecase, assume_cross_account_role, create_boto3_client
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

logs_client = boto3.client('logs')


class IssueSeverity(Enum):
    """Issue severity levels"""
    CRITICAL = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4


# Comprehensive pattern definitions for common DDA/Greengrass issues
LOG_PATTERNS = {
    'deployment_failed': {
        'pattern': r'deployment.*failed|FAILED_NO_STATE_CHANGE|deployment.*error',
        'severity': IssueSeverity.HIGH,
        'title': 'Deployment Failed',
        'causes': [
            'Greengrass Nucleus version mismatch',
            'Component architecture incompatibility (ARM64 vs x86)',
            'Insufficient device resources (disk/memory)',
            'Component dependency not satisfied',
            'Invalid component configuration'
        ],
        'actions': [
            'Verify Nucleus version matches deployment requirements (2.13.0+)',
            'Check component was compiled for device architecture (ARM64 or x86)',
            'Verify device has sufficient disk space (>1GB) and memory (>512MB)',
            'Check all component dependencies are deployed',
            'Review deployment configuration in portal'
        ],
        'prevention': [
            'Always include Nucleus in deployments',
            'Test components on target architecture before production',
            'Monitor device resource usage regularly',
            'Use deployment policies with auto-rollback enabled'
        ]
    },
    'model_inference_error': {
        'pattern': r'inference.*error|model.*failed|triton.*error|model.*not found|inference.*exception',
        'severity': IssueSeverity.HIGH,
        'title': 'Model Inference Error',
        'causes': [
            'Model not compiled for device architecture',
            'Model file corrupted or incomplete',
            'Insufficient GPU/memory for model',
            'Model input format mismatch',
            'Triton server not running'
        ],
        'actions': [
            'Verify model was compiled for device architecture in portal',
            'Check model file integrity: verify file size and checksums',
            'Monitor device GPU/memory usage during inference',
            'Verify input image format matches model requirements',
            'Check Triton server logs for startup errors'
        ],
        'prevention': [
            'Compile models for target architecture before deployment',
            'Test models locally before production deployment',
            'Monitor inference latency and resource usage',
            'Implement input validation in inference pipeline'
        ]
    },
    'permission_denied': {
        'pattern': r'permission denied|access denied|EACCES|permission error|not permitted',
        'severity': IssueSeverity.MEDIUM,
        'title': 'Permission Denied Error',
        'causes': [
            'Component running with insufficient permissions',
            'File/directory ownership incorrect',
            'IAM role missing required permissions',
            'Device role not properly configured'
        ],
        'actions': [
            'Check component is running as ggc_user with proper permissions',
            'Verify file ownership: sudo chown -R ggc_user:ggc_group /path/to/files',
            'Review IAM role permissions in AWS console',
            'Redeploy component to refresh permissions',
            'Check device role has S3, CloudWatch, and Greengrass permissions'
        ],
        'prevention': [
            'Use proper IAM roles for edge devices',
            'Set correct file permissions during component creation',
            'Test permissions in staging before production',
            'Regularly audit IAM role permissions'
        ]
    },
    'network_error': {
        'pattern': r'connection refused|network unreachable|timeout|connection reset|no route to host',
        'severity': IssueSeverity.MEDIUM,
        'title': 'Network Connectivity Error',
        'causes': [
            'Device offline or network disconnected',
            'Network misconfiguration',
            'Firewall blocking traffic',
            'DNS resolution failure',
            'AWS IoT endpoint unreachable'
        ],
        'actions': [
            'Verify device network connectivity: ping 8.8.8.8',
            'Check security group rules allow required ports (8883 for MQTT)',
            'Verify DNS resolution: nslookup iot.region.amazonaws.com',
            'Check AWS IoT endpoint configuration in device',
            'Review CloudWatch logs for network errors'
        ],
        'prevention': [
            'Configure security groups to allow IoT traffic',
            'Use static IP or DHCP reservation for devices',
            'Implement network monitoring and alerting',
            'Test network connectivity before deployment'
        ]
    },
    'out_of_memory': {
        'pattern': r'out of memory|OOM|memory exhausted|cannot allocate memory',
        'severity': IssueSeverity.CRITICAL,
        'title': 'Out of Memory Error',
        'causes': [
            'Model too large for device memory',
            'Memory leak in component',
            'Too many concurrent inference requests',
            'Insufficient device RAM'
        ],
        'actions': [
            'Check device memory: free -h',
            'Monitor memory usage during inference: top -b',
            'Reduce model size or use quantization (INT8/FP16)',
            'Limit concurrent inference requests',
            'Consider upgrading device with more RAM'
        ],
        'prevention': [
            'Profile model memory usage before deployment',
            'Use model quantization to reduce size',
            'Implement request queuing to limit concurrency',
            'Monitor memory trends over time'
        ]
    },
    'disk_space_error': {
        'pattern': r'no space left on device|disk full|ENOSPC|out of disk',
        'severity': IssueSeverity.HIGH,
        'title': 'Disk Space Error',
        'causes': [
            'Device storage full',
            'Log files consuming too much space',
            'Model files too large',
            'Temporary files not cleaned up'
        ],
        'actions': [
            'Check disk usage: df -h',
            'Clean up old logs: sudo rm -rf /aws_dda/greengrass/v2/logs/*.log.old',
            'Remove unused models or components',
            'Enable log rotation in LogManager configuration',
            'Consider expanding device storage'
        ],
        'prevention': [
            'Configure log rotation and retention policies',
            'Monitor disk usage regularly',
            'Clean up temporary files periodically',
            'Use appropriate device storage size for workload'
        ]
    },
    'component_not_found': {
        'pattern': r'component.*not found|component.*missing|cannot find component',
        'severity': IssueSeverity.HIGH,
        'title': 'Component Not Found',
        'causes': [
            'Component not deployed to device',
            'Component name mismatch',
            'Component version not available',
            'Component repository unreachable'
        ],
        'actions': [
            'Verify component is deployed: aws greengrass list-components',
            'Check component name spelling and version',
            'Verify component is available in Greengrass repository',
            'Check device has internet connectivity to fetch components',
            'Redeploy component if missing'
        ],
        'prevention': [
            'Verify component deployment before using',
            'Use exact component names and versions',
            'Test deployments in staging first',
            'Monitor component deployment status'
        ]
    },
    'configuration_error': {
        'pattern': r'configuration.*error|invalid.*config|config.*failed|malformed.*config',
        'severity': IssueSeverity.MEDIUM,
        'title': 'Configuration Error',
        'causes': [
            'Invalid component configuration JSON',
            'Missing required configuration parameters',
            'Configuration type mismatch',
            'Invalid parameter values'
        ],
        'actions': [
            'Review component configuration in portal',
            'Validate JSON syntax in configuration',
            'Check all required parameters are provided',
            'Verify parameter types match schema',
            'Redeploy with corrected configuration'
        ],
        'prevention': [
            'Use portal UI for configuration (validates automatically)',
            'Test configuration changes in staging',
            'Document required configuration parameters',
            'Implement configuration validation'
        ]
    },
    'triton_error': {
        'pattern': r'triton.*error|triton.*failed|triton.*exception|model.*repository',
        'severity': IssueSeverity.HIGH,
        'title': 'Triton Server Error',
        'causes': [
            'Triton server failed to start',
            'Model repository misconfigured',
            'Model format not supported',
            'Triton version incompatibility'
        ],
        'actions': [
            'Check Triton server status: systemctl status tritonserver',
            'Review Triton logs: tail -f /var/log/triton/tritonserver.log',
            'Verify model repository path is correct',
            'Check model format is supported (ONNX, TensorRT, etc)',
            'Restart Triton: systemctl restart tritonserver'
        ],
        'prevention': [
            'Test Triton configuration before deployment',
            'Use supported model formats',
            'Monitor Triton server health',
            'Keep Triton updated'
        ]
    },
    'dda_component_error': {
        'pattern': r'aws\.edgeml\.dda|LocalServer.*error|InferenceApp.*error|dda.*failed',
        'severity': IssueSeverity.HIGH,
        'title': 'DDA Component Error',
        'causes': [
            'DDA LocalServer not running',
            'InferenceApp configuration error',
            'DDA component dependency missing',
            'DDA component version mismatch'
        ],
        'actions': [
            'Check DDA component status: aws greengrass list-components',
            'Review DDA component logs in CloudWatch',
            'Verify all DDA dependencies are deployed',
            'Check DDA component version compatibility',
            'Redeploy DDA components'
        ],
        'prevention': [
            'Always deploy DDA LocalServer before model components',
            'Use compatible DDA component versions',
            'Test DDA configuration in staging',
            'Monitor DDA component health'
        ]
    },
    'inference_uploader_error': {
        'pattern': r'InferenceUploader.*error|S3.*upload.*failed|inference.*upload',
        'severity': IssueSeverity.MEDIUM,
        'title': 'Inference Uploader Error',
        'causes': [
            'S3 bucket not accessible',
            'IAM permissions insufficient',
            'Network connectivity issue',
            'S3 bucket configuration error'
        ],
        'actions': [
            'Verify S3 bucket exists and is accessible',
            'Check IAM role has S3 permissions',
            'Verify network connectivity to S3',
            'Check S3 bucket policy allows device role',
            'Review InferenceUploader configuration'
        ],
        'prevention': [
            'Test S3 access before deployment',
            'Use proper IAM roles with S3 permissions',
            'Monitor S3 upload success rate',
            'Implement retry logic for failed uploads'
        ]
    }
}


def handler(event, context):
    """
    Handle device log analysis requests
    
    POST /api/v1/devices/{id}/logs/analyze - Analyze device logs
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        path_parameters = event.get('pathParameters') or {}
        query_parameters = event.get('queryStringParameters') or {}
        
        logger.info(f"Device logs analyzer request: {http_method} {path}")
        
        if http_method == 'OPTIONS':
            return create_response(200, {})
        
        user = get_user_from_event(event)
        device_id = path_parameters.get('id')
        
        if http_method == 'POST' and device_id:
            return analyze_device_logs(device_id, user, query_parameters)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in device logs analyzer: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def analyze_device_logs(device_id: str, user: Dict, query_params: Dict) -> Dict:
    """
    Analyze device logs and provide troubleshooting guidance
    """
    try:
        usecase_id = query_params.get('usecase_id')
        hours_back = int(query_params.get('hours', '1'))
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        # Fetch logs from CloudWatch
        logs_text = fetch_device_logs(device_id, usecase_id, hours_back)
        
        if not logs_text:
            return create_response(200, {
                'analysis': {
                    'issues_detected': 0,
                    'issues': [],
                    'message': 'No logs found for the specified time period'
                }
            })
        
        # Analyze logs using pattern matching
        analysis = analyze_logs_pattern_based(logs_text, device_id)
        
        logger.info(f"Analyzed logs for device {device_id}: {analysis['issues_detected']} issues detected")
        
        return create_response(200, {'analysis': analysis})
        
    except ClientError as e:
        logger.error(f"AWS error analyzing logs: {str(e)}")
        return create_response(500, {'error': f'Failed to analyze logs: {str(e)}'})
    except Exception as e:
        logger.error(f"Error analyzing logs: {str(e)}")
        return create_response(500, {'error': 'Failed to analyze logs'})


def fetch_device_logs(device_id: str, usecase_id: str, hours_back: int = 1) -> str:
    """
    Fetch logs from CloudWatch for the device using cross-account role
    """
    try:
        # Get usecase details for cross-account access
        usecase = get_usecase(usecase_id)
        if not usecase:
            logger.error(f"Use case {usecase_id} not found")
            return ""
        
        # Assume cross-account role
        credentials = assume_cross_account_role(
            usecase['cross_account_role_arn'],
            usecase['external_id']
        )
        
        region = os.environ.get('AWS_REGION', 'us-east-1')
        
        # Create CloudWatch Logs client with assumed role
        cross_account_logs_client = create_boto3_client('logs', credentials, region)
        
        # Try multiple log group patterns
        log_group_patterns = [
            f'/aws/greengrass/GreengrassSystemComponent/{region}/{device_id}',
            f'/aws/greengrass/UserComponent/{region}/{device_id}',
            f'/aws/greengrass/{usecase_id}/{device_id}',
            f'/aws/greengrass/dda-{device_id}',
            f'/aws/greengrass/{device_id}',
        ]
        
        logs_text = ""
        start_time = int((datetime.utcnow() - timedelta(hours=hours_back)).timestamp() * 1000)
        
        for log_group_prefix in log_group_patterns:
            try:
                # First, try to describe log groups with this prefix
                response = cross_account_logs_client.describe_log_groups(
                    logGroupNamePrefix=log_group_prefix,
                    limit=10
                )
                
                for log_group in response.get('logGroups', []):
                    log_group_name = log_group['logGroupName']
                    try:
                        # Fetch events from this log group
                        events_response = cross_account_logs_client.filter_log_events(
                            logGroupName=log_group_name,
                            startTime=start_time,
                            limit=1000
                        )
                        
                        events = events_response.get('events', [])
                        if events:
                            logs_text += '\n'.join([event['message'] for event in events])
                            logger.info(f"Fetched {len(events)} log events from {log_group_name}")
                    except ClientError as e:
                        logger.warning(f"Error fetching logs from {log_group_name}: {e}")
                        continue
                
                if logs_text:
                    break
                    
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceNotFoundException':
                    logger.warning(f"Error describing log groups with prefix {log_group_prefix}: {e}")
                continue
        
        if not logs_text:
            logger.warning(f"No logs found for device {device_id} in usecase {usecase_id}")
        
        return logs_text
        
    except Exception as e:
        logger.error(f"Error fetching device logs: {str(e)}", exc_info=True)
        return ""


def analyze_logs_pattern_based(logs_text: str, device_id: str) -> Dict:
    """
    Analyze logs using pattern matching against known issues
    """
    detected_issues = []
    matched_patterns = set()
    
    # Search for each pattern in logs
    for issue_name, pattern_config in LOG_PATTERNS.items():
        if re.search(pattern_config['pattern'], logs_text, re.IGNORECASE):
            # Avoid duplicate issues from overlapping patterns
            if issue_name not in matched_patterns:
                matched_patterns.add(issue_name)
                
                detected_issues.append({
                    'issue_id': issue_name,
                    'title': pattern_config['title'],
                    'severity': pattern_config['severity'].name,
                    'likely_causes': pattern_config['causes'],
                    'recommended_actions': pattern_config['actions'],
                    'prevention_tips': pattern_config['prevention']
                })
    
    # Sort by severity (CRITICAL first)
    severity_order = {
        'CRITICAL': 0,
        'HIGH': 1,
        'MEDIUM': 2,
        'LOW': 3
    }
    detected_issues.sort(key=lambda x: severity_order.get(x['severity'], 4))
    
    # Build summary
    summary = {
        'device_id': device_id,
        'analysis_timestamp': datetime.utcnow().isoformat(),
        'issues_detected': len(detected_issues),
        'critical_count': sum(1 for i in detected_issues if i['severity'] == 'CRITICAL'),
        'high_count': sum(1 for i in detected_issues if i['severity'] == 'HIGH'),
        'medium_count': sum(1 for i in detected_issues if i['severity'] == 'MEDIUM'),
        'low_count': sum(1 for i in detected_issues if i['severity'] == 'LOW'),
        'issues': detected_issues,
        'next_steps': get_next_steps(detected_issues)
    }
    
    return summary


def get_next_steps(issues: List[Dict]) -> List[str]:
    """
    Generate prioritized next steps based on detected issues
    """
    if not issues:
        return ['No issues detected. Device appears to be operating normally.']
    
    next_steps = []
    
    # Add critical issues first
    critical_issues = [i for i in issues if i['severity'] == 'CRITICAL']
    if critical_issues:
        next_steps.append(f"⚠️  CRITICAL: Address {len(critical_issues)} critical issue(s) immediately")
        for issue in critical_issues:
            next_steps.append(f"  • {issue['title']}: {issue['recommended_actions'][0]}")
    
    # Add high priority issues
    high_issues = [i for i in issues if i['severity'] == 'HIGH']
    if high_issues:
        next_steps.append(f"🔴 HIGH: Resolve {len(high_issues)} high-priority issue(s)")
        for issue in high_issues[:2]:  # Show top 2
            next_steps.append(f"  • {issue['title']}: {issue['recommended_actions'][0]}")
    
    # Add general guidance
    if len(issues) > 0:
        next_steps.append("📋 Review detailed analysis below for complete troubleshooting steps")
        next_steps.append("💡 Check prevention tips to avoid similar issues in future")
    
    return next_steps
