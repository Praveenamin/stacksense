/**
 * SLI/SLO Compliance Cards Component
 * Displays Service Level Indicator (SLI) and Service Level Objective (SLO) compliance status
 */

class SLIComplianceCards {
    constructor(containerId) {
        this.containerId = containerId;
        this.container = document.getElementById(containerId);
        this.updateInterval = null;
        this.updateIntervalMs = 60000; // Update every 60 seconds
    }

    init() {
        if (!this.container) {
            console.error(`SLIComplianceCards: Container with id "${this.containerId}" not found`);
            return;
        }

        // Show loading state
        this.showLoading();
        this.fetchAndRender();
        this.startAutoUpdate();
    }

    showLoading() {
        this.container.innerHTML = '<div style="text-align: center; padding: 32px; color: #64748b;">Loading compliance data...</div>';
    }

    startAutoUpdate() {
        // Clear existing interval if any
        if (this.updateInterval) {
            clearInterval(this.updateInterval);
        }

        // Set up periodic updates
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
            const response = await fetch('/api/dashboard/sli-compliance/');
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();
            
            if (data.success) {
                this.render(data.data);
            } else {
                console.error('SLIComplianceCards: API error:', data.error);
                this.renderError(data.error || 'Failed to load compliance data');
            }
        } catch (error) {
            console.error('SLIComplianceCards: Fetch error:', error);
            this.renderError('Failed to load compliance data');
        }
    }

    getComplianceColorClass(percentage) {
        if (percentage >= 95) {
            return 'success';
        } else if (percentage >= 80) {
            return 'warning';
        } else {
            return 'danger';
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

    render(data) {
        if (!data || !data.by_metric) {
            this.renderError('No compliance data available');
            return;
        }

        const metrics = Object.keys(data.by_metric);
        if (metrics.length === 0) {
            this.container.innerHTML = '<p style="color: #64748b; padding: 16px;">No SLI metrics configured</p>';
            return;
        }

        // Render overall compliance card first, then per-metric cards
        let html = '<div class="dashboard-grid" style="grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));">';
        
        // Overall compliance card
        const overallClass = this.getComplianceColorClass(data.compliance_percentage);
        html += `
            <div class="summary-metric-card ${overallClass}">
                <div class="summary-metric-value">${data.compliance_percentage.toFixed(1)}%</div>
                <div class="summary-metric-label">Overall SLO Compliance</div>
                <div class="summary-metric-trend">
                    <span>${data.compliant_servers || 0} of ${data.total_servers} servers</span>
                </div>
            </div>
        `;

        // Render per-metric cards (limit to top 7 to avoid overcrowding)
        metrics.slice(0, 7).forEach(metricType => {
            const metricData = data.by_metric[metricType];
            const compliancePercentage = metricData.compliance_percentage || 0;
            const cardClass = this.getComplianceColorClass(compliancePercentage);
            
            html += `
                <div class="summary-metric-card ${cardClass}">
                    <div class="summary-metric-value">${compliancePercentage.toFixed(1)}%</div>
                    <div class="summary-metric-label">${this.getMetricTypeLabel(metricType)}</div>
                    <div class="summary-metric-trend">
                        <span>${metricData.compliant_servers || 0} of ${metricData.total_servers || 0} servers</span>
                    </div>
                </div>
            `;
        });

        html += '</div>';
        this.container.innerHTML = html;
    }

    renderError(errorMessage) {
        this.container.innerHTML = `
            <div style="padding: 16px; background-color: #fee2e2; border: 1px solid #fca5a5; border-radius: 4px; color: #991b1b;">
                ${errorMessage}
            </div>
        `;
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    const sliComplianceCards = new SLIComplianceCards('sli-compliance-cards-container');
    sliComplianceCards.init();
});
