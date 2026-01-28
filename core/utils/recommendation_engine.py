"""
Recommendation Engine - Rule-based AI recommendations for infrastructure optimization
"""
from django.utils import timezone
from datetime import timedelta
from core.models import Server, SystemMetric, MonitoringConfig
from core.trend_detection import detect_alert_patterns


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
    Only suggests when the server has at least 7 days of monitoring data.
    """
    recommendations = []
    
    seven_days_ago = timezone.now() - timedelta(days=7)
    
    servers = Server.objects.all()
    for server in servers:
        # Require at least 7 days of monitoring data before suggesting downsizing
        oldest = SystemMetric.objects.filter(server=server).order_by('timestamp').first()
        if not oldest or oldest.timestamp > seven_days_ago:
            continue

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
            # Check for CPU alert patterns before recommending downsize
            pattern = detect_alert_patterns(server, alert_type='CPU', lookback_days=30, min_alerts=3)
            
            if pattern['has_pattern'] and pattern['total_alerts'] >= 3:
                # Has recurring spikes - don't recommend pure downsize
                description = (
                    f"{server.name} averages <30% CPU but has recurring spikes "
                    f"({pattern['pattern_description']}). "
                    f"Resolve the spike pattern first, then consider downsizing."
                )
                priority = 'low'  # Lower priority since blocked by spike issue
            else:
                # No patterns - safe to recommend downsize
                description = f'{server.name} has been underutilized (<30% CPU) for 7 days. Consider downsizing.'
                priority = 'medium'
            
            recommendations.append({
                'type': 'underutilization',
                'priority': priority,
                'description': description,
                'impact': None,
                'action_label': None,
                'action_handler': None,
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

