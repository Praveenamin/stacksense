/**
 * HealthStatusChart - Health status distribution donut chart component
 */
class HealthStatusChart extends BaseDashboardComponent {
    constructor() {
        super('health-status', '/api/dashboard/health-status/');
        this.chart = null;
    }
    render(data) {
        if (!data) { this.showError('No data available'); return; }
        const healthyEl = document.getElementById('health-count-healthy');
        if (healthyEl) healthyEl.textContent = data.healthy || 0;
        const warningEl = document.getElementById('health-count-warning');
        if (warningEl) warningEl.textContent = data.warning || 0;
        const criticalEl = document.getElementById('health-count-critical');
        if (criticalEl) criticalEl.textContent = data.critical || 0;
        const offlineEl = document.getElementById('health-count-offline');
        if (offlineEl) offlineEl.textContent = data.offline || 0;
        const chartData = {
            labels: ['Healthy', 'Warning', 'Critical', 'Offline'],
            datasets: [{
                data: [data.healthy || 0, data.warning || 0, data.critical || 0, data.offline || 0],
                backgroundColor: ['#22c55e', '#f59e0b', '#ef4444', '#64748b'],
                borderWidth: 0
            }]
        };
        if (!this.chart) {
            this.chart = ChartWrapper.createDoughnutChart('health-status-donut-chart', chartData);
        } else {
            ChartWrapper.updateChart(this.chart, chartData);
        }
    }
}
