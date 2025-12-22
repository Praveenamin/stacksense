/**
 * UptimeMetric - Uptime metric component
 * Extends BaseMetric to handle Uptime-specific rendering and updates
 */
class UptimeMetric extends BaseMetric {
    constructor(serverId, elementId, options = {}) {
        super(serverId, elementId, options);
        this.metricType = 'uptime';
    }
    
    getMetricType() {
        return 'uptime';
    }
    
    _validateData(data) {
        return super._validateData(data);
    }
    
    _getDefaultValue() {
        return '--';
    }
    
    _renderSpecific(data) {
        const mainElement = document.getElementById(`uptime-main-${this.serverId}`);
        if (!mainElement) return;
        
        let uptimeText = '--';
        
        if (data.uptime_formatted) {
            uptimeText = data.uptime_formatted;
        } else if (data.system_uptime_seconds !== null && data.system_uptime_seconds !== undefined) {
            uptimeText = this.formatTime(data.system_uptime_seconds);
        }
        
        mainElement.textContent = uptimeText;
    }
}




