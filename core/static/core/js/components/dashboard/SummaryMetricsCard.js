/**
 * SummaryMetricsCard - Summary metrics card component
 */
class SummaryMetricsCard extends BaseDashboardComponent {
    constructor() {
        super('summary-metrics', '/api/dashboard/summary-stats/');
    }
    render(data) {
        if (!data) { this.showError('No data available'); return; }
        const totalVMsEl = document.getElementById('total-vms-value');
        if (totalVMsEl) totalVMsEl.textContent = data.total_vms || 0;
        const totalVMsTrendEl = document.getElementById('total-vms-trend');
        if (totalVMsTrendEl && data.server_trend !== undefined) {
            const trend = data.server_trend || 0;
            if (trend > 0) {
                totalVMsTrendEl.innerHTML = `<span class="positive">+${trend} this month</span>`;
                totalVMsTrendEl.className = 'summary-metric-trend positive';
            } else if (trend < 0) {
                totalVMsTrendEl.innerHTML = `<span class="negative">${trend} this month</span>`;
                totalVMsTrendEl.className = 'summary-metric-trend negative';
            } else {
                totalVMsTrendEl.innerHTML = '<span>Same as yesterday</span>';
                totalVMsTrendEl.className = 'summary-metric-trend';
            }
        }
        const activeAlertsEl = document.getElementById('active-alerts-value');
        if (activeAlertsEl) activeAlertsEl.textContent = data.active_alerts || 0;
        const activeAlertsTrendEl = document.getElementById('active-alerts-trend');
        if (activeAlertsTrendEl && data.alert_trend !== undefined) {
            const trend = data.alert_trend || 0;
            if (trend > 0) {
                activeAlertsTrendEl.innerHTML = `<span class="negative">+${trend} from yesterday</span>`;
                activeAlertsTrendEl.className = 'summary-metric-trend negative';
            } else if (trend < 0) {
                activeAlertsTrendEl.innerHTML = `<span class="positive">${trend} from yesterday</span>`;
                activeAlertsTrendEl.className = 'summary-metric-trend positive';
            } else {
                activeAlertsTrendEl.innerHTML = '<span>Same as yesterday</span>';
                activeAlertsTrendEl.className = 'summary-metric-trend';
            }
        }
        const criticalVMsEl = document.getElementById('critical-vms-value');
        if (criticalVMsEl) criticalVMsEl.textContent = data.critical_vms || 0;
        const slaComplianceEl = document.getElementById('sla-compliance-value');
        if (slaComplianceEl) slaComplianceEl.textContent = `${data.sla_compliance || 0}%`;
        const slaComplianceTrendEl = document.getElementById('sla-compliance-trend');
        if (slaComplianceTrendEl) {
            slaComplianceTrendEl.innerHTML = '<span>+0.2% this week</span>';
            slaComplianceTrendEl.className = 'summary-metric-trend positive';
        }
    }
}
