/**
 * SLO Configuration Component
 * Manages SLO settings and displays service latency for server details page
 */

class SLOConfiguration {
    constructor(serverId, containerId) {
        this.serverId = serverId;
        this.containerId = containerId;
        this.container = document.getElementById(containerId);
        this.updateInterval = null;
        this.updateIntervalMs = 60000; // Update every 60 seconds
    }

    init() {
        if (!this.container) {
            console.error(`SLOConfiguration: Container with id "${this.containerId}" not found`);
            return;
        }

        this.fetchAndRender();
        this.startAutoUpdate();
    }

    startAutoUpdate() {
        if (this.updateInterval) {
            clearInterval(this.updateInterval);
        }
        this.updateInterval = setInterval(() => {
            this.fetchAndRender();
        }, this.updateIntervalMs);
    }

    stopAutoUpdate() {
        if (this.updateInterval) {
            clearInterval(this.updateInterval);
            this.updateInterval = null;
        }
    }

    async fetchAndRender() {
        try {
            const response = await fetch(`/api/server/${this.serverId}/sli-compliance/`);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();
            
            if (data.success) {
                this.render(data.data);
            } else {
                console.error('SLOConfiguration: API error:', data.error);
                this.renderError(data.error || 'Failed to load compliance data');
            }
        } catch (error) {
            console.error('SLOConfiguration: Fetch error:', error);
            this.renderError('Failed to load compliance data');
        }
    }

    getComplianceColor(isCompliant) {
        if (isCompliant === null) return 'text-gray-500';
        return isCompliant ? 'text-emerald-600' : 'text-red-600';
    }

    getMetricTypeLabel(metricType) {
        const labels = {
            'UPTIME': 'Uptime',
            'CPU': 'CPU',
            'MEMORY': 'Memory',
            'DISK': 'Disk',
            'NETWORK': 'Network',
            'RESPONSE_TIME': 'Response Time',
            'ERROR_RATE': 'Error Rate'
        };
        return labels[metricType] || metricType;
    }

    formatValue(value, metricType) {
        if (value === null || value === undefined) return 'N/A';
        
        if (metricType === 'RESPONSE_TIME') {
            return `${value.toFixed(2)} ms`;
        } else if (['CPU', 'MEMORY', 'DISK', 'NETWORK', 'ERROR_RATE'].includes(metricType)) {
            return `${value.toFixed(2)}%`;
        } else if (metricType === 'UPTIME') {
            return `${value.toFixed(2)}%`;
        }
        return value.toString();
    }

    render(data) {
        if (!data || !data.metrics) {
            this.renderError('No compliance data available');
            return;
        }

        let html = `
            <div style="margin-bottom: var(--cds-spacing-6);">
                <h3 style="font-size: var(--cds-font-size-lg); font-weight: var(--cds-font-weight-semibold); color: var(--cds-color-gray-100); margin: 0 0 var(--cds-spacing-4) 0;">Current SLI Values & Compliance</h3>
                <div style="background-color: var(--cds-color-gray-10); border-radius: var(--cds-border-radius); padding: var(--cds-spacing-4);">
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: var(--cds-spacing-4);">
        `;

        data.metrics.forEach(metric => {
            const complianceIcon = metric.is_compliant === null 
                ? '⚪' 
                : metric.is_compliant 
                    ? '✅' 
                    : '❌';
            
            html += `
                <div style="background-color: var(--cds-color-white); border: 1px solid var(--cds-color-gray-20); border-radius: var(--cds-border-radius); padding: var(--cds-spacing-3);">
                    <div style="font-size: var(--cds-font-size-sm); font-weight: var(--cds-font-weight-medium); color: var(--cds-color-gray-70); margin-bottom: var(--cds-spacing-1);">
                        ${this.getMetricTypeLabel(metric.metric_type)} ${complianceIcon}
                    </div>
                    <div style="font-size: var(--cds-font-size-lg); font-weight: var(--cds-font-weight-semibold); color: var(--cds-color-gray-100);">
                        ${this.formatValue(metric.sli_value, metric.metric_type)}
                    </div>
                    <div style="font-size: var(--cds-font-size-xs); color: var(--cds-color-gray-70); margin-top: var(--cds-spacing-1);">
                        Target: ${this.formatValue(metric.slo_target, metric.metric_type)}
                        ${metric.compliance_percentage !== null ? ` (${metric.compliance_percentage.toFixed(1)}% compliant)` : ''}
                    </div>
                </div>
            `;
        });

        html += `
                    </div>
                </div>
            </div>
        `;

        // Service Latency section (for RESPONSE_TIME metric)
        if (data.service_latencies && data.service_latencies.length > 0) {
            html += `
                <div style="margin-bottom: var(--cds-spacing-6);">
                    <h3 style="font-size: var(--cds-font-size-lg); font-weight: var(--cds-font-weight-semibold); color: var(--cds-color-gray-100); margin: 0 0 var(--cds-spacing-4) 0;">Service Latency (Monitored Services)</h3>
                    <div style="background-color: var(--cds-color-gray-10); border-radius: var(--cds-border-radius); padding: var(--cds-spacing-4);">
                        <table style="width: 100%; border-collapse: collapse;">
                            <thead>
                                <tr style="border-bottom: 1px solid var(--cds-color-gray-20);">
                                    <th style="text-align: left; padding: var(--cds-spacing-2); font-size: var(--cds-font-size-sm); font-weight: var(--cds-font-weight-semibold); color: var(--cds-color-gray-100);">Service</th>
                                    <th style="text-align: left; padding: var(--cds-spacing-2); font-size: var(--cds-font-size-sm); font-weight: var(--cds-font-weight-semibold); color: var(--cds-color-gray-100);">Latency</th>
                                    <th style="text-align: left; padding: var(--cds-spacing-2); font-size: var(--cds-font-size-sm); font-weight: var(--cds-font-weight-semibold); color: var(--cds-color-gray-100);">Last Measured</th>
                                </tr>
                            </thead>
                            <tbody>
            `;

            data.service_latencies.forEach(service => {
                const timestamp = service.timestamp ? new Date(service.timestamp).toLocaleString() : 'N/A';
                html += `
                    <tr style="border-bottom: 1px solid var(--cds-color-gray-20);">
                        <td style="padding: var(--cds-spacing-2); font-size: var(--cds-font-size-sm); color: var(--cds-color-gray-100);">${service.service_name}</td>
                        <td style="padding: var(--cds-spacing-2); font-size: var(--cds-font-size-sm); color: var(--cds-color-gray-100);">${service.latency_ms.toFixed(2)} ms</td>
                        <td style="padding: var(--cds-spacing-2); font-size: var(--cds-font-size-sm); color: var(--cds-color-gray-70);">${timestamp}</td>
                    </tr>
                `;
            });

            html += `
                            </tbody>
                        </table>
                    </div>
                </div>
            `;
        }

        // Overall compliance
        html += `
            <div style="background-color: var(--cds-color-gray-10); border-radius: var(--cds-border-radius); padding: var(--cds-spacing-4); margin-top: var(--cds-spacing-4);">
                <div style="font-size: var(--cds-font-size-sm); font-weight: var(--cds-font-weight-medium); color: var(--cds-color-gray-70); margin-bottom: var(--cds-spacing-2);">
                    Overall Compliance
                </div>
                <div style="font-size: var(--cds-font-size-2xl); font-weight: var(--cds-font-weight-bold); color: var(--cds-color-gray-100);">
                    ${data.overall_compliance.toFixed(1)}%
                </div>
            </div>
        `;

        this.container.innerHTML = html;
    }

    renderError(errorMessage) {
        this.container.innerHTML = `
            <div style="padding: var(--cds-spacing-4); background-color: var(--cds-color-red-10); border: 1px solid var(--cds-color-red-20); border-radius: var(--cds-border-radius); color: var(--cds-color-red-70);">
                ${errorMessage}
            </div>
        `;
    }
}

// Initialize when DOM is ready (will be called from server_details.html)
if (typeof window.initSLOConfiguration === 'undefined') {
    window.initSLOConfiguration = function(serverId) {
        const sloConfig = new SLOConfiguration(serverId, 'slo-configuration-container');
        sloConfig.init();
    };
}

