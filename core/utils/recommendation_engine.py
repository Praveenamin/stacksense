"""
Recommendation Engine - Rule-based AI recommendations for infrastructure optimization
"""
from django.utils import timezone
from datetime import timedelta
from core.models import Server, SystemMetric, MonitoringConfig


def generate_recommendations():
    """
    Generate rule-based recommendations for infrastructure optimization.
    Returns a list of recommendation dictionaries.
    """
    recommendations = []
    
    # Recommendation 1: Detect underutilized servers
    recommendations.extend(_detect_underutilized_servers())
    
    # Recommendation 2: Predict capacity issues
    recommendations.extend(_predict_capacity_issues())
    
    # Recommendation 3: Cost optimization suggestions
    recommendations.extend(_suggest_cost_optimizations())
    
    # Sort by priority (high > medium > low)
    priority_order = {'high': 0, 'medium': 1, 'low': 2}
    recommendations.sort(key=lambda x: priority_order.get(x.get('priority', 'medium'), 1))
    
    return recommendations


def _detect_underutilized_servers():
    """
    Detect servers that have been underutilized (< 30% CPU) for 7+ days.
    Suggest downsizing to save costs.
    """
    recommendations = []
    
    seven_days_ago = timezone.now() - timedelta(days=7)
    
    servers = Server.objects.all()
    for server in servers:
        # Get metrics from the last 7 days
        recent_metrics = SystemMetric.objects.filter(
            server=server,
            timestamp__gte=seven_days_ago
        ).order_by('-timestamp')
        
        if recent_metrics.count() < 10:  # Need enough data points
            continue
        
        # Calculate average CPU usage
        avg_cpu = sum(m.cpu_percent or 0 for m in recent_metrics) / recent_metrics.count()
        
        if avg_cpu < 30:  # Underutilized threshold
            # Estimate potential savings (rough calculation)
            # Assuming $60/month base cost, saving 30% = $18/month
            estimated_savings = 180  # Placeholder calculation
            
            recommendations.append({
                'type': 'underutilization',
                'priority': 'medium',
                'description': f'{server.name} has been underutilized (<30% CPU) for 7 days. Consider downsizing to save costs.',
                'impact': f'Potential savings: ${estimated_savings}/month',
                'action_label': 'Scale Down',
                'action_handler': f'() => scaleDownServer({server.id})',
                'server_id': server.id
            })
    
    return recommendations


def _predict_capacity_issues():
    """
    Predict capacity issues based on trending metrics.
    """
    recommendations = []
    
    servers = Server.objects.all()
    for server in servers:
        # Get metrics from the last 3 days
        three_days_ago = timezone.now() - timedelta(days=3)
        recent_metrics = SystemMetric.objects.filter(
            server=server,
            timestamp__gte=three_days_ago
        ).order_by('timestamp')
        
        if recent_metrics.count() < 20:  # Need enough data points
            continue
        
        # Check memory trend
        memory_values = [m.memory_percent or 0 for m in recent_metrics]
        if len(memory_values) >= 10:
            recent_avg = sum(memory_values[-10:]) / 10
            older_avg = sum(memory_values[:10]) / 10
            
            if recent_avg > older_avg + 5:  # Memory trending upward
                # Predict when capacity will be reached (linear projection)
                growth_rate = (recent_avg - older_avg) / 3  # per day
                if growth_rate > 0 and recent_avg > 70:
                    days_to_full = (100 - recent_avg) / growth_rate if growth_rate > 0 else 999
                    
                    if days_to_full < 5:  # Critical prediction
                        recommendations.append({
                            'type': 'capacity_prediction',
                            'priority': 'high',
                            'description': f'{server.name} memory usage trending upward. May reach capacity in {int(days_to_full)} days based on current growth rate.',
                            'impact': None,
                            'action_label': 'Scale memory',
                            'action_handler': f'() => scaleMemory({server.id})',
                            'server_id': server.id
                        })
    
    return recommendations


def _suggest_cost_optimizations():
    """
    Suggest cost optimization opportunities.
    """
    recommendations = []
    
    # This is a placeholder for more sophisticated cost analysis
    # In a real implementation, this would analyze:
    # - Resource allocation vs usage
    # - Instance type recommendations
    # - Reserved instance opportunities
    # - Idle resource detection
    
    return recommendations

