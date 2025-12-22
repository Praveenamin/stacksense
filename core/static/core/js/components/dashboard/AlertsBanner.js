/**
 * AlertsBanner - Alerts banner component
 */
class AlertsBanner extends BaseDashboardComponent {
    constructor() {
        super('alerts-banner', '/api/dashboard/summary-stats/');
    }
    render(data) {
        if (!data) return;
        const bannerEl = document.getElementById('dashboard-alerts-banner');
        const alertsCountEl = document.getElementById('alerts-count');
        const criticalCountEl = document.getElementById('critical-count');
        const activeAlerts = data.active_alerts || 0;
        const criticalVMs = data.critical_vms || 0;
        if (activeAlerts > 0 && bannerEl) {
            if (alertsCountEl) alertsCountEl.textContent = activeAlerts;
            if (criticalCountEl) criticalCountEl.textContent = criticalVMs;
            bannerEl.style.display = 'flex';
        } else if (bannerEl) {
            bannerEl.style.display = 'none';
        }
    }
}
