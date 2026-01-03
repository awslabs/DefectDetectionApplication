#!/usr/bin/env python3

"""
Registry Refresh Manager for Greengrass Component Browser
"""

import json
import boto3
from datetime import datetime, timedelta
from typing import Dict, List, Any
import logging

logger = logging.getLogger(__name__)

class RegistryRefreshManager:
    """Manages automatic refresh of component registry"""
    
    def __init__(self, registry, discovery_service):
        self.registry = registry
        self.discovery_service = discovery_service
        self.cloudwatch = boto3.client('cloudwatch')
        self.events = boto3.client('events')
        
    def create_refresh_schedule(self, schedule_expression: str = "rate(6 hours)", 
                              lambda_function_arn: str = None) -> bool:
        """Create CloudWatch Events rule for scheduled registry refresh"""
        try:
            rule_name = "greengrass-component-registry-refresh"
            
            # Create the rule
            self.events.put_rule(
                Name=rule_name,
                ScheduleExpression=schedule_expression,
                Description="Scheduled refresh of Greengrass component registry",
                State='ENABLED'
            )
            
            # Add Lambda target if provided
            if lambda_function_arn:
                self.events.put_targets(
                    Rule=rule_name,
                    Targets=[
                        {
                            'Id': '1',
                            'Arn': lambda_function_arn,
                            'Input': json.dumps({
                                'action': 'refresh_registry',
                                'source': 'scheduled'
                            })
                        }
                    ]
                )
            
            logger.info(f"Created refresh schedule: {rule_name} with expression: {schedule_expression}")
            return True
            
        except Exception as e:
            logger.error(f"Error creating refresh schedule: {str(e)}")
            return False
    
    def perform_incremental_refresh(self) -> Dict[str, Any]:
        """Perform incremental refresh with change detection"""
        try:
            refresh_stats = {
                'start_time': datetime.utcnow().isoformat(),
                'discovered': 0,
                'new': 0,
                'updated': 0,
                'unchanged': 0,
                'deprecated': 0,
                'errors': 0,
                'changes_detected': []
            }
            
            logger.info("Starting incremental registry refresh...")
            
            # Get current registry state
            current_components = self._get_registry_snapshot()
            
            # Discover latest components
            discovered_components = self.discovery_service.discover_components()
            refresh_stats['discovered'] = len(discovered_components)
            
            # Create lookup for discovered components
            discovered_lookup = {
                f"{comp.component_name}#{comp.version}": comp 
                for comp in discovered_components
            }
            
            # Process discovered components
            for component in discovered_components:
                try:
                    component_id = f"{component.component_name}#{component.version}"
                    
                    if component_id in current_components:
                        # Check for changes
                        current_comp = current_components[component_id]
                        if self._has_component_changed(current_comp, component):
                            if self.registry.register_component(component):
                                refresh_stats['updated'] += 1
                                refresh_stats['changes_detected'].append({
                                    'component_id': component_id,
                                    'change_type': 'updated',
                                    'changes': self._detect_changes(current_comp, component)
                                })
                        else:
                            refresh_stats['unchanged'] += 1
                    else:
                        # New component
                        if self.registry.register_component(component):
                            refresh_stats['new'] += 1
                            refresh_stats['changes_detected'].append({
                                'component_id': component_id,
                                'change_type': 'new'
                            })
                            
                except Exception as e:
                    logger.error(f"Error processing component {component.component_name}: {str(e)}")
                    refresh_stats['errors'] += 1
            
            # Check for deprecated components (exist in registry but not discovered)
            for component_id in current_components:
                if component_id not in discovered_lookup:
                    # Component no longer exists, mark as deprecated
                    if self.registry.update_component_status(component_id, 'deprecated'):
                        refresh_stats['deprecated'] += 1
                        refresh_stats['changes_detected'].append({
                            'component_id': component_id,
                            'change_type': 'deprecated'
                        })
            
            refresh_stats['end_time'] = datetime.utcnow().isoformat()
            
            # Send metrics to CloudWatch
            self._send_refresh_metrics(refresh_stats)
            
            logger.info(f"Incremental refresh completed: {refresh_stats['new']} new, {refresh_stats['updated']} updated, {refresh_stats['deprecated']} deprecated")
            return refresh_stats
            
        except Exception as e:
            logger.error(f"Error during incremental refresh: {str(e)}")
            raise Exception(f"Incremental refresh failed: {str(e)}")
    
    def _get_registry_snapshot(self) -> Dict[str, Any]:
        """Get current state of registry for change detection"""
        try:
            # Get all components from registry
            all_components = self.registry.search_components(limit=1000)  # Adjust limit as needed
            
            snapshot = {}
            for component in all_components:
                component_id = f"{component.component_name}#{component.version}"
                snapshot[component_id] = component
            
            return snapshot
            
        except Exception as e:
            logger.error(f"Error creating registry snapshot: {str(e)}")
            return {}
    
    def _has_component_changed(self, current, new) -> bool:
        """Check if component has changed"""
        # Compare key fields that might change
        fields_to_compare = [
            'description', 'publisher', 'platform_compatibility',
            'resource_requirements', 'dependencies', 'tags', 'status'
        ]
        
        for field in fields_to_compare:
            current_value = getattr(current, field, None)
            new_value = getattr(new, field, None)
            
            if current_value != new_value:
                return True
        
        return False
    
    def _detect_changes(self, current, new) -> List[str]:
        """Detect specific changes between components"""
        changes = []
        
        if current.description != new.description:
            changes.append("description")
        
        if current.publisher != new.publisher:
            changes.append("publisher")
        
        if current.platform_compatibility != new.platform_compatibility:
            changes.append("platform_compatibility")
        
        if current.resource_requirements != new.resource_requirements:
            changes.append("resource_requirements")
        
        if current.dependencies != new.dependencies:
            changes.append("dependencies")
        
        if current.tags != new.tags:
            changes.append("tags")
        
        if current.status != new.status:
            changes.append("status")
        
        return changes
    
    def _send_refresh_metrics(self, stats: Dict[str, Any]) -> None:
        """Send refresh metrics to CloudWatch"""
        try:
            namespace = "EdgeCV/ComponentRegistry"
            
            metrics = [
                {
                    'MetricName': 'ComponentsDiscovered',
                    'Value': stats['discovered'],
                    'Unit': 'Count'
                },
                {
                    'MetricName': 'ComponentsNew',
                    'Value': stats['new'],
                    'Unit': 'Count'
                },
                {
                    'MetricName': 'ComponentsUpdated',
                    'Value': stats['updated'],
                    'Unit': 'Count'
                },
                {
                    'MetricName': 'ComponentsDeprecated',
                    'Value': stats['deprecated'],
                    'Unit': 'Count'
                },
                {
                    'MetricName': 'RefreshErrors',
                    'Value': stats['errors'],
                    'Unit': 'Count'
                }
            ]
            
            # Calculate refresh duration
            if 'start_time' in stats and 'end_time' in stats:
                start = datetime.fromisoformat(stats['start_time'])
                end = datetime.fromisoformat(stats['end_time'])
                duration = (end - start).total_seconds()
                
                metrics.append({
                    'MetricName': 'RefreshDuration',
                    'Value': duration,
                    'Unit': 'Seconds'
                })
            
            self.cloudwatch.put_metric_data(
                Namespace=namespace,
                MetricData=metrics
            )
            
        except Exception as e:
            logger.warning(f"Error sending refresh metrics: {str(e)}")


def create_registry_refresh_manager(registry, discovery_service):
    """Factory function to create RegistryRefreshManager instance"""
    return RegistryRefreshManager(registry, discovery_service)