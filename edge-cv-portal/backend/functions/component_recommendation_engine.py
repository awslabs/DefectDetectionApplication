#!/usr/bin/env python3

"""
Component Recommendation Engine for Greengrass Component Browser
"""

import json
import boto3
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
import logging
import math
from collections import defaultdict

logger = logging.getLogger(__name__)

class ComponentRecommendationEngine:
    """Provides intelligent component recommendations based on various factors"""
    
    def __init__(self, registry, compatibility_engine):
        self.registry = registry
        self.compatibility_engine = compatibility_engine
        self.cloudwatch = boto3.client('cloudwatch')
        
        # Recommendation weights
        self.weights = {
            'compatibility_score': 0.3,
            'popularity_score': 0.2,
            'usage_frequency': 0.2,
            'recency_score': 0.1,
            'dependency_score': 0.1,
            'publisher_reputation': 0.1
        }
    
    def recommend_for_model(self, model_component, target_platform: str, 
                           max_recommendations: int = 10) -> List[Dict[str, Any]]:
        """Recommend infrastructure components for a model component"""
        try:
            logger.info(f"Generating recommendations for model {model_component.component_name} on {target_platform}")
            
            # Get all potential infrastructure components
            infrastructure_components = self._get_infrastructure_components(target_platform)
            
            # Score each component
            scored_recommendations = []
            
            for component in infrastructure_components:
                try:
                    score_breakdown = self._calculate_recommendation_score(
                        model_component, component, target_platform
                    )
                    
                    if score_breakdown['total_score'] > 0.3:  # Minimum threshold
                        recommendation = {
                            'component': component,
                            'score': score_breakdown['total_score'],
                            'score_breakdown': score_breakdown,
                            'recommendation_reasons': self._generate_recommendation_reasons(
                                model_component, component, score_breakdown
                            ),
                            'compatibility_details': self._get_compatibility_details(
                                model_component, component, target_platform
                            )
                        }
                        scored_recommendations.append(recommendation)
                        
                except Exception as e:
                    logger.warning(f"Error scoring component {component.component_name}: {str(e)}")
            
            # Sort by score and return top recommendations
            scored_recommendations.sort(key=lambda x: x['score'], reverse=True)
            
            # Add ranking information
            for i, rec in enumerate(scored_recommendations[:max_recommendations]):
                rec['rank'] = i + 1
                rec['confidence'] = self._calculate_confidence(rec['score'])
            
            logger.info(f"Generated {len(scored_recommendations[:max_recommendations])} recommendations")
            return scored_recommendations[:max_recommendations]
            
        except Exception as e:
            logger.error(f"Error generating recommendations: {str(e)}")
            return []
    
    def recommend_complementary_components(self, selected_components: List, 
                                         target_platform: str,
                                         max_recommendations: int = 5) -> List[Dict[str, Any]]:
        """Recommend components that complement already selected components"""
        try:
            # Analyze selected components to understand the deployment pattern
            deployment_pattern = self._analyze_deployment_pattern(selected_components)
            
            # Find components commonly used with this pattern
            complementary_components = self._find_complementary_components(
                deployment_pattern, target_platform
            )
            
            # Score and rank recommendations
            scored_recommendations = []
            
            for component in complementary_components:
                try:
                    # Check if not already selected
                    if not any(comp.component_name == component.component_name 
                             for comp in selected_components):
                        
                        score = self._calculate_complementary_score(
                            selected_components, component, deployment_pattern
                        )
                        
                        if score > 0.4:  # Higher threshold for complementary
                            recommendation = {
                                'component': component,
                                'score': score,
                                'recommendation_type': 'complementary',
                                'reasons': self._generate_complementary_reasons(
                                    selected_components, component, deployment_pattern
                                )
                            }
                            scored_recommendations.append(recommendation)
                            
                except Exception as e:
                    logger.warning(f"Error scoring complementary component {component.component_name}: {str(e)}")
            
            # Sort and return top recommendations
            scored_recommendations.sort(key=lambda x: x['score'], reverse=True)
            return scored_recommendations[:max_recommendations]
            
        except Exception as e:
            logger.error(f"Error generating complementary recommendations: {str(e)}")
            return []
    
    def recommend_by_use_case(self, use_case: str, target_platform: str,
                             max_recommendations: int = 8) -> List[Dict[str, Any]]:
        """Recommend components based on specific use cases"""
        try:
            use_case_patterns = {
                'computer_vision': {
                    'required_types': ['runtime', 'utility'],
                    'preferred_tags': ['opencv', 'tensorflow', 'pytorch', 'inference'],
                    'resource_profile': 'high_compute'
                },
                'iot_data_processing': {
                    'required_types': ['connector', 'utility'],
                    'preferred_tags': ['mqtt', 'data', 'processing', 'stream'],
                    'resource_profile': 'balanced'
                },
                'edge_analytics': {
                    'required_types': ['runtime', 'utility', 'connector'],
                    'preferred_tags': ['analytics', 'data', 'stream', 'processing'],
                    'resource_profile': 'high_memory'
                },
                'device_management': {
                    'required_types': ['utility', 'connector'],
                    'preferred_tags': ['device', 'management', 'monitoring', 'health'],
                    'resource_profile': 'low_resource'
                }
            }
            
            pattern = use_case_patterns.get(use_case.lower())
            if not pattern:
                logger.warning(f"Unknown use case: {use_case}")
                return []
            
            # Find components matching the use case pattern
            matching_components = self._find_components_by_pattern(pattern, target_platform)
            
            # Score based on use case fit
            scored_recommendations = []
            
            for component in matching_components:
                try:
                    score = self._calculate_use_case_score(component, pattern)
                    
                    if score > 0.5:
                        recommendation = {
                            'component': component,
                            'score': score,
                            'recommendation_type': 'use_case',
                            'use_case': use_case,
                            'fit_reasons': self._generate_use_case_reasons(component, pattern)
                        }
                        scored_recommendations.append(recommendation)
                        
                except Exception as e:
                    logger.warning(f"Error scoring use case component {component.component_name}: {str(e)}")
            
            # Sort and return recommendations
            scored_recommendations.sort(key=lambda x: x['score'], reverse=True)
            return scored_recommendations[:max_recommendations]
            
        except Exception as e:
            logger.error(f"Error generating use case recommendations: {str(e)}")
            return []
    
    def _get_infrastructure_components(self, target_platform: str) -> List:
        """Get all infrastructure components compatible with target platform"""
        try:
            # Get runtime and utility components
            from shared_utils import ComponentType, ComponentFilter, ComponentStatus
            
            runtime_filter = ComponentFilter(
                component_type=ComponentType.RUNTIME,
                platform=target_platform,
                status=ComponentStatus.ACTIVE
            )
            
            utility_filter = ComponentFilter(
                component_type=ComponentType.UTILITY,
                platform=target_platform,
                status=ComponentStatus.ACTIVE
            )
            
            connector_filter = ComponentFilter(
                component_type=ComponentType.CONNECTOR,
                platform=target_platform,
                status=ComponentStatus.ACTIVE
            )
            
            runtime_components = self.registry.search_components(filters=runtime_filter, limit=100)
            utility_components = self.registry.search_components(filters=utility_filter, limit=100)
            connector_components = self.registry.search_components(filters=connector_filter, limit=100)
            
            return runtime_components + utility_components + connector_components
            
        except Exception as e:
            logger.error(f"Error getting infrastructure components: {str(e)}")
            return []
    
    def _calculate_recommendation_score(self, model_component, infrastructure_component, 
                                      target_platform: str) -> Dict[str, float]:
        """Calculate comprehensive recommendation score"""
        try:
            scores = {}
            
            # Compatibility score
            compatibility_result = self.compatibility_engine.validate_compatibility(
                [model_component, infrastructure_component], target_platform
            )
            scores['compatibility_score'] = compatibility_result.score
            
            # Popularity score (based on usage patterns)
            scores['popularity_score'] = self._calculate_popularity_score(infrastructure_component)
            
            # Usage frequency score
            scores['usage_frequency'] = self._calculate_usage_frequency_score(infrastructure_component)
            
            # Recency score (newer components get slight boost)
            scores['recency_score'] = self._calculate_recency_score(infrastructure_component)
            
            # Dependency score (fewer dependencies is better)
            scores['dependency_score'] = self._calculate_dependency_score(infrastructure_component)
            
            # Publisher reputation score
            scores['publisher_reputation'] = self._calculate_publisher_reputation_score(infrastructure_component)
            
            # Calculate weighted total
            total_score = sum(
                scores[key] * self.weights[key] 
                for key in scores if key in self.weights
            )
            
            scores['total_score'] = min(total_score, 1.0)  # Cap at 1.0
            
            return scores
            
        except Exception as e:
            logger.error(f"Error calculating recommendation score: {str(e)}")
            return {'total_score': 0.0}
    
    def _calculate_popularity_score(self, component) -> float:
        """Calculate popularity score based on usage metrics"""
        try:
            # This would typically query usage metrics from CloudWatch or a usage database
            # For now, use a simple heuristic based on component age and type
            
            base_score = 0.5
            
            # Runtime components are generally more popular
            if hasattr(component, 'component_type') and component.component_type.value == 'runtime':
                base_score += 0.2
            
            # Components with common tags get boost
            common_tags = ['python', 'nodejs', 'docker', 'logging', 'monitoring']
            if hasattr(component, 'tags'):
                for tag_value in component.tags.values():
                    if any(common_tag in tag_value.lower() for common_tag in common_tags):
                        base_score += 0.1
                        break
            
            return min(base_score, 1.0)
            
        except Exception as e:
            logger.warning(f"Error calculating popularity score: {str(e)}")
            return 0.5
    
    def _calculate_usage_frequency_score(self, component) -> float:
        """Calculate usage frequency score"""
        try:
            # This would query actual usage metrics
            # For now, return a default score
            return 0.6
            
        except Exception as e:
            return 0.5
    
    def _calculate_recency_score(self, component) -> float:
        """Calculate recency score (newer components get slight boost)"""
        try:
            if not hasattr(component, 'creation_date'):
                return 0.5
            
            # Calculate days since creation
            days_old = (datetime.utcnow() - component.creation_date).days
            
            # Newer components (< 30 days) get full score
            if days_old < 30:
                return 1.0
            # Components < 90 days get good score
            elif days_old < 90:
                return 0.8
            # Components < 365 days get decent score
            elif days_old < 365:
                return 0.6
            # Older components get lower score
            else:
                return 0.4
                
        except Exception as e:
            return 0.5
    
    def _calculate_dependency_score(self, component) -> float:
        """Calculate dependency score (fewer dependencies is better)"""
        try:
            if not hasattr(component, 'dependencies'):
                return 1.0
            
            dep_count = len(component.dependencies)
            
            # No dependencies = perfect score
            if dep_count == 0:
                return 1.0
            # 1-2 dependencies = good score
            elif dep_count <= 2:
                return 0.8
            # 3-5 dependencies = decent score
            elif dep_count <= 5:
                return 0.6
            # Many dependencies = lower score
            else:
                return 0.4
                
        except Exception as e:
            return 0.7
    
    def _calculate_publisher_reputation_score(self, component) -> float:
        """Calculate publisher reputation score"""
        try:
            if not hasattr(component, 'publisher'):
                return 0.5
            
            # AWS published components get high score
            if 'aws' in component.publisher.lower():
                return 0.9
            
            # Known publishers get good score
            known_publishers = ['amazon', 'greengrass', 'iot']
            if any(pub in component.publisher.lower() for pub in known_publishers):
                return 0.8
            
            # Default score for other publishers
            return 0.6
            
        except Exception as e:
            return 0.5
    
    def _generate_recommendation_reasons(self, model_component, infrastructure_component, 
                                       score_breakdown: Dict[str, float]) -> List[str]:
        """Generate human-readable reasons for the recommendation"""
        reasons = []
        
        try:
            # High compatibility
            if score_breakdown.get('compatibility_score', 0) > 0.8:
                reasons.append("Highly compatible with your model component")
            
            # Popular component
            if score_breakdown.get('popularity_score', 0) > 0.7:
                reasons.append("Popular choice among developers")
            
            # Recent component
            if score_breakdown.get('recency_score', 0) > 0.8:
                reasons.append("Recently updated with latest features")
            
            # Few dependencies
            if score_breakdown.get('dependency_score', 0) > 0.8:
                reasons.append("Minimal dependencies for easier deployment")
            
            # Trusted publisher
            if score_breakdown.get('publisher_reputation', 0) > 0.8:
                reasons.append("From a trusted publisher")
            
            # Component type specific reasons
            if hasattr(infrastructure_component, 'component_type'):
                comp_type = infrastructure_component.component_type.value
                if comp_type == 'runtime':
                    reasons.append("Provides essential runtime environment")
                elif comp_type == 'utility':
                    reasons.append("Offers useful utility functions")
                elif comp_type == 'connector':
                    reasons.append("Enables connectivity to external services")
            
            # If no specific reasons, add a general one
            if not reasons:
                reasons.append("Good overall match for your deployment")
            
            return reasons[:3]  # Limit to top 3 reasons
            
        except Exception as e:
            logger.warning(f"Error generating recommendation reasons: {str(e)}")
            return ["Recommended based on compatibility analysis"]
    
    def _get_compatibility_details(self, model_component, infrastructure_component, 
                                 target_platform: str) -> Dict[str, Any]:
        """Get detailed compatibility information"""
        try:
            compatibility_result = self.compatibility_engine.validate_compatibility(
                [model_component, infrastructure_component], target_platform
            )
            
            return {
                'compatible': compatibility_result.compatible,
                'score': compatibility_result.score,
                'issues': compatibility_result.issues,
                'recommendations': compatibility_result.recommendations
            }
            
        except Exception as e:
            logger.warning(f"Error getting compatibility details: {str(e)}")
            return {'compatible': False, 'score': 0.0, 'issues': [], 'recommendations': []}
    
    def _calculate_confidence(self, score: float) -> str:
        """Calculate confidence level based on score"""
        if score >= 0.8:
            return "high"
        elif score >= 0.6:
            return "medium"
        elif score >= 0.4:
            return "low"
        else:
            return "very_low"
    
    def _analyze_deployment_pattern(self, selected_components: List) -> Dict[str, Any]:
        """Analyze the deployment pattern from selected components"""
        pattern = {
            'component_types': defaultdict(int),
            'common_tags': defaultdict(int),
            'resource_profile': 'balanced',
            'platform_requirements': set()
        }
        
        try:
            total_memory = 0
            total_cpu = 0
            
            for component in selected_components:
                # Count component types
                if hasattr(component, 'component_type'):
                    pattern['component_types'][component.component_type.value] += 1
                
                # Collect common tags
                if hasattr(component, 'tags'):
                    for tag_value in component.tags.values():
                        pattern['common_tags'][tag_value.lower()] += 1
                
                # Analyze resource requirements
                if hasattr(component, 'resource_requirements'):
                    reqs = component.resource_requirements
                    total_memory += reqs.get('memory', 0)
                    total_cpu += reqs.get('cpu', 0)
                
                # Collect platform requirements
                if hasattr(component, 'platform_compatibility'):
                    pattern['platform_requirements'].update(component.platform_compatibility)
            
            # Determine resource profile
            if total_memory > 2048 or total_cpu > 2.0:
                pattern['resource_profile'] = 'high_resource'
            elif total_memory < 512 and total_cpu < 0.5:
                pattern['resource_profile'] = 'low_resource'
            
            return pattern
            
        except Exception as e:
            logger.warning(f"Error analyzing deployment pattern: {str(e)}")
            return pattern
    
    def _find_complementary_components(self, deployment_pattern: Dict[str, Any], 
                                     target_platform: str) -> List:
        """Find components that complement the deployment pattern"""
        try:
            complementary_components = []
            
            # Get all infrastructure components
            all_components = self._get_infrastructure_components(target_platform)
            
            # Filter based on deployment pattern
            for component in all_components:
                if self._matches_deployment_pattern(component, deployment_pattern):
                    complementary_components.append(component)
            
            return complementary_components
            
        except Exception as e:
            logger.error(f"Error finding complementary components: {str(e)}")
            return []
    
    def _matches_deployment_pattern(self, component, pattern: Dict[str, Any]) -> bool:
        """Check if component matches the deployment pattern"""
        try:
            # Check if component type is needed
            if hasattr(component, 'component_type'):
                comp_type = component.component_type.value
                
                # If pattern has no utilities, suggest some
                if comp_type == 'utility' and pattern['component_types']['utility'] == 0:
                    return True
                
                # If pattern has no connectors but has models, suggest connectors
                if comp_type == 'connector' and pattern['component_types']['connector'] == 0:
                    if pattern['component_types']['model'] > 0:
                        return True
            
            # Check tag overlap
            if hasattr(component, 'tags'):
                for tag_value in component.tags.values():
                    if tag_value.lower() in pattern['common_tags']:
                        return True
            
            return False
            
        except Exception as e:
            return False
    
    def _calculate_complementary_score(self, selected_components: List, 
                                     component, deployment_pattern: Dict[str, Any]) -> float:
        """Calculate score for complementary component"""
        try:
            score = 0.5  # Base score
            
            # Boost for filling gaps in deployment pattern
            if hasattr(component, 'component_type'):
                comp_type = component.component_type.value
                if pattern['component_types'][comp_type] == 0:
                    score += 0.3
            
            # Boost for tag alignment
            if hasattr(component, 'tags'):
                tag_matches = 0
                for tag_value in component.tags.values():
                    if tag_value.lower() in deployment_pattern['common_tags']:
                        tag_matches += 1
                
                if tag_matches > 0:
                    score += min(tag_matches * 0.1, 0.2)
            
            return min(score, 1.0)
            
        except Exception as e:
            return 0.5
    
    def _generate_complementary_reasons(self, selected_components: List, 
                                      component, deployment_pattern: Dict[str, Any]) -> List[str]:
        """Generate reasons for complementary recommendation"""
        reasons = []
        
        try:
            if hasattr(component, 'component_type'):
                comp_type = component.component_type.value
                if deployment_pattern['component_types'][comp_type] == 0:
                    reasons.append(f"Adds missing {comp_type} functionality to your deployment")
            
            # Check for common patterns
            if hasattr(component, 'tags'):
                for tag_value in component.tags.values():
                    if 'logging' in tag_value.lower():
                        reasons.append("Provides logging capabilities for better observability")
                        break
                    elif 'monitoring' in tag_value.lower():
                        reasons.append("Enables monitoring and health checks")
                        break
            
            if not reasons:
                reasons.append("Commonly used with similar deployments")
            
            return reasons[:2]
            
        except Exception as e:
            return ["Complements your current component selection"]
    
    def _find_components_by_pattern(self, pattern: Dict[str, Any], target_platform: str) -> List:
        """Find components matching a use case pattern"""
        try:
            matching_components = []
            all_components = self._get_infrastructure_components(target_platform)
            
            for component in all_components:
                if self._component_matches_pattern(component, pattern):
                    matching_components.append(component)
            
            return matching_components
            
        except Exception as e:
            logger.error(f"Error finding components by pattern: {str(e)}")
            return []
    
    def _component_matches_pattern(self, component, pattern: Dict[str, Any]) -> bool:
        """Check if component matches use case pattern"""
        try:
            # Check component type
            if hasattr(component, 'component_type'):
                comp_type = component.component_type.value
                if comp_type not in pattern['required_types']:
                    return False
            
            # Check for preferred tags
            if hasattr(component, 'tags') and pattern['preferred_tags']:
                has_preferred_tag = False
                for tag_value in component.tags.values():
                    if any(pref_tag in tag_value.lower() for pref_tag in pattern['preferred_tags']):
                        has_preferred_tag = True
                        break
                
                if not has_preferred_tag:
                    return False
            
            return True
            
        except Exception as e:
            return False
    
    def _calculate_use_case_score(self, component, pattern: Dict[str, Any]) -> float:
        """Calculate use case fit score"""
        try:
            score = 0.5  # Base score
            
            # Boost for matching component type
            if hasattr(component, 'component_type'):
                comp_type = component.component_type.value
                if comp_type in pattern['required_types']:
                    score += 0.2
            
            # Boost for matching tags
            if hasattr(component, 'tags') and pattern['preferred_tags']:
                tag_matches = 0
                for tag_value in component.tags.values():
                    for pref_tag in pattern['preferred_tags']:
                        if pref_tag in tag_value.lower():
                            tag_matches += 1
                            break
                
                if tag_matches > 0:
                    score += min(tag_matches * 0.15, 0.3)
            
            return min(score, 1.0)
            
        except Exception as e:
            return 0.5
    
    def _generate_use_case_reasons(self, component, pattern: Dict[str, Any]) -> List[str]:
        """Generate reasons for use case recommendation"""
        reasons = []
        
        try:
            # Component type reason
            if hasattr(component, 'component_type'):
                comp_type = component.component_type.value
                if comp_type in pattern['required_types']:
                    reasons.append(f"Essential {comp_type} component for this use case")
            
            # Tag-based reasons
            if hasattr(component, 'tags'):
                for tag_value in component.tags.values():
                    for pref_tag in pattern['preferred_tags']:
                        if pref_tag in tag_value.lower():
                            reasons.append(f"Optimized for {pref_tag} workloads")
                            break
            
            if not reasons:
                reasons.append("Well-suited for this use case")
            
            return reasons[:2]
            
        except Exception as e:
            return ["Recommended for this use case"]


def create_component_recommendation_engine(registry, compatibility_engine):
    """Factory function to create ComponentRecommendationEngine instance"""
    return ComponentRecommendationEngine(registry, compatibility_engine)