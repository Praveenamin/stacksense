/**
 * SLO Compliance Cards Component
 * Displays Service Level Objective (SLO) compliance with card-based layout
 */

class SLOComplianceGauges {
    constructor(containerId) {
        this.containerId = containerId;
        this.container = document.getElementById(containerId);
        this.updateInterval = null;
        this.updateIntervalMs = 60000; // Update every 60 seconds
        this.currentView = 'overall'; // 'overall' or 'server'
        this.selectedServerId = null;
        this.servers = [];
    }

    init() {
        if (!this.container) {
            console.error(`SLOComplianceGauges: Container with id "${this.containerId}" not found`);
            return;
        }

        this.setupEventListeners();
        this.loadServers();
        this.showLoading();
        this.fetchAndRender();
        this.startAutoUpdate();
    }

    setupEventListeners() {
        // Toggle buttons
        const overallBtn = document.getElementById('slo-view-overall');
        const serverBtn = document.getElementById('slo-view-server');
        const serverSelect = document.getElementById('slo-server-select');

        if (overallBtn) {
            overallBtn.addEventListener('click', () => {
                this.switchView('overall');
            });
        }

        if (serverBtn) {
            serverBtn.addEventListener('click', () => {
                this.switchView('server');
            });
        }

        if (serverSelect) {
            serverSelect.addEventListener('change', (e) => {
                this.selectedServerId = e.target.value;
                if (this.selectedServerId) {
                    this.fetchAndRender();
                }
            });
        }
    }

    switchView(view) {
        this.currentView = view;
        
        const overallBtn = document.getElementById('slo-view-overall');
        const serverBtn = document.getElementById('slo-view-server');
        const serverSelect = document.getElementById('slo-server-select');

        if (view === 'overall') {
            if (overallBtn) overallBtn.classList.add('active');
            if (serverBtn) serverBtn.classList.remove('active');
            if (serverSelect) serverSelect.style.display = 'none';
            this.selectedServerId = null;
        } else {
            if (overallBtn) overallBtn.classList.remove('active');
            if (serverBtn) serverBtn.classList.add('active');
            if (serverSelect) serverSelect.style.display = 'block';
        }

        // Update button styles
        if (overallBtn) {
            overallBtn.style.color = view === 'overall' 
                ? 'var(--cds-color-gray-100, #0f172a)' 
                : 'var(--cds-color-gray-70, #334155)';
            overallBtn.style.backgroundColor = view === 'overall' 
                ? 'white' 
                : 'transparent';
        }
        if (serverBtn) {
            serverBtn.style.color = view === 'server' 
                ? 'var(--cds-color-gray-100, #0f172a)' 
                : 'var(--cds-color-gray-70, #334155)';
            serverBtn.style.backgroundColor = view === 'server' 
                ? 'white' 
                : 'transparent';
        }

        this.fetchAndRender();
    }

    async loadServers() {
        try {
            const response = await fetch('/api/dashboard/servers-list/');
            if (!response.ok) throw new Error('Failed to load servers');
            const data = await response.json();
            if (data.success && data.data && data.data.servers) {
                this.servers = data.data.servers;
                this.populateServerDropdown();
            }
        } catch (error) {
            console.error('SLOComplianceGauges: Error loading servers:', error);
        }
    }

    populateServerDropdown() {
        const serverSelect = document.getElementById('slo-server-select');
        if (!serverSelect) return;

        // Clear existing options except the first one
        while (serverSelect.options.length > 1) {
            serverSelect.remove(1);
        }

        // Add servers
        this.servers.forEach(server => {
            const option = document.createElement('option');
            option.value = server.id;
            option.textContent = server.name;
            serverSelect.appendChild(option);
        });

        // Select first server if in server view
        if (this.currentView === 'server' && this.servers.length > 0 && !this.selectedServerId) {
            this.selectedServerId = this.servers[0].id;
            serverSelect.value = this.selectedServerId;
        }
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

    showLoading() {
        this.container.innerHTML = '<div style="text-align: center; padding: 32px; color: #64748b;">Loading compliance data...</div>';
    }

    async fetchAndRender() {
        try {
            let url = '/api/dashboard/sli-compliance/';
            if (this.currentView === 'server' && this.selectedServerId) {
                url = `/api/server/${this.selectedServerId}/sli-compliance/`;
            }

            const response = await fetch(url);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();
            
            if (data.success) {
                this.render(data.data);
            } else {
                console.error('SLOComplianceGauges: API error:', data.error);
                this.renderError(data.error || 'Failed to load compliance data');
            }
        } catch (error) {
            console.error('SLOComplianceGauges: Fetch error:', error);
            this.renderError('Failed to load compliance data');
        }
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
        if (!data) {
            this.renderError('No compliance data available');
            return;
        }

        let html = '';
        let metricsToDisplay = [];

        if (this.currentView === 'overall') {
            // Overall view - show aggregate metrics
            html += `
                <div style="margin-bottom: 24px;">
                    <h3 style="font-size: 18px; font-weight: 600; color: #0f172a; margin: 0 0 16px 0;">Current SLI Values & Compliance</h3>
                    <div style="background-color: #f8fafc; border-radius: 8px; padding: 16px;">
                        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px;">
            `;

            if (data.by_metric) {
                metricsToDisplay = Object.keys(data.by_metric).map(metricType => {
                    const metricData = data.by_metric[metricType];
                    // Calculate if compliant (assuming >= 80% is compliant for overall view)
                    const isCompliant = (metricData.compliance_percentage || 0) >= 80;
                    return {
                        type: metricType,
                        label: this.getMetricTypeLabel(metricType),
                        value: metricData.compliance_percentage || 0,
                        target: 80, // Default target for display
                        is_compliant: isCompliant,
                        compliance_percentage: metricData.compliance_percentage || 0
                    };
                });
            }

            // Add overall compliance card if available
            if (data.compliance_percentage !== undefined) {
                metricsToDisplay.unshift({
                    type: 'OVERALL',
                    label: 'Overall Compliance',
                    value: data.compliance_percentage,
                    target: 80,
                    is_compliant: data.compliance_percentage >= 80,
                    compliance_percentage: data.compliance_percentage
                });
            }
        } else {
            // Server view - show individual server metrics
            html += `
                <div style="margin-bottom: 24px;">
                    <h3 style="font-size: 18px; font-weight: 600; color: #0f172a; margin: 0 0 16px 0;">Current SLI Values & Compliance</h3>
                    <div style="background-color: #f8fafc; border-radius: 8px; padding: 16px;">
                        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px;">
            `;

            if (data.metrics) {
                metricsToDisplay = data.metrics.map(metric => ({
                    type: metric.metric_type,
                    label: this.getMetricTypeLabel(metric.metric_type),
                    value: metric.sli_value,
                    target: metric.slo_target,
                    is_compliant: metric.is_compliant,
                    compliance_percentage: metric.compliance_percentage
                }));
            }
        }

        if (metricsToDisplay.length === 0) {
            html += '<p style="color: #64748b; padding: 16px; text-align: center;">No compliance data available</p>';
        } else {
            metricsToDisplay.forEach(metric => {
                const complianceIcon = metric.is_compliant === null 
                    ? '⚪' 
                    : metric.is_compliant 
                        ? '✅' 
                        : '❌';
                
                html += `
                    <div style="background-color: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px;">
                        <div style="font-size: 14px; font-weight: 500; color: #334155; margin-bottom: 4px;">
                            ${metric.label} ${complianceIcon}
                        </div>
                        <div style="font-size: 18px; font-weight: 600; color: #0f172a;">
                            ${this.formatValue(metric.value, metric.type)}
                        </div>
                        <div style="font-size: 12px; color: #64748b; margin-top: 4px;">
                            Target: ${this.formatValue(metric.target, metric.type)}
                            ${metric.compliance_percentage !== null && metric.compliance_percentage !== undefined ? ` (${metric.compliance_percentage.toFixed(1)}% compliant)` : ''}
                        </div>
                    </div>
                `;
            });
        }

        html += `
                        </div>
                    </div>
                </div>
        `;

        this.container.innerHTML = html;
    }

    renderError(errorMessage) {
        this.container.innerHTML = `
            <div style="padding: 16px; background-color: #fee2e2; border: 1px solid #fca5a5; border-radius: 4px; color: #991b1b; text-align: center;">
                ${errorMessage}
            </div>
        `;
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    const sloGauges = new SLOComplianceGauges('slo-compliance-gauges-container');
    sloGauges.init();
});
