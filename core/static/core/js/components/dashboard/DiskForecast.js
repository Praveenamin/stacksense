/**
 * DiskForecast - Disk space forecast component (30-day prediction)
 */
class DiskForecast extends BaseDashboardComponent {
    constructor() {
        super('disk-forecast', null);
        this.chart = null;
        this.currentServerId = null;
        this.currentMountPoint = null;
        this.servers = [];
    }
    async init() {
        await this.loadServers();
        this.setupEventListeners();
    }
    async loadServers() {
        try {
            const response = await fetch('/api/dashboard/servers-list/');
            if (!response.ok) throw new Error('Failed to load servers');
            const data = await response.json();
            if (data.success && data.servers) {
                this.servers = data.servers;
                this.populateServerDropdown();
            }
        } catch (error) {
            console.error('DiskForecast.loadServers error:', error);
        }
    }
    populateServerDropdown() {
        const selectEl = document.getElementById('disk-forecast-server-select');
        if (!selectEl) return;
        selectEl.innerHTML = '<option value="">Select a server...</option>';
        this.servers.forEach(server => {
            const option = document.createElement('option');
            option.value = server.id;
            option.textContent = server.name;
            selectEl.appendChild(option);
        });
    }
    setupEventListeners() {
        const selectEl = document.getElementById('disk-forecast-server-select');
        if (selectEl) {
            selectEl.addEventListener('change', (e) => {
                const serverId = e.target.value;
                if (serverId) {
                    this.loadDiskMountPoints(serverId);
                } else {
                    document.getElementById('disk-selector-container').style.display = 'none';
                    document.getElementById('disk-forecast-info').style.display = 'none';
                }
            });
        }
    }
    async loadDiskMountPoints(serverId) {
        try {
            const response = await fetch(`/api/dashboard/disk-mount-points/${serverId}/`);
            if (!response.ok) throw new Error('Failed to load disk mount points');
            const data = await response.json();
            if (data.success && data.mount_points) {
                this.populateDiskSelector(data.mount_points, serverId);
            }
        } catch (error) {
            console.error('DiskForecast.loadDiskMountPoints error:', error);
            this.showError('Failed to load disk mount points');
        }
    }
    populateDiskSelector(mountPoints, serverId) {
        const container = document.getElementById('disk-selector-container');
        const selector = document.getElementById('disk-selector');
        if (!container || !selector) return;
        container.style.display = 'block';
        selector.innerHTML = '';
        mountPoints.forEach(mountPoint => {
            const button = document.createElement('button');
            button.className = 'disk-selector-button';
            button.textContent = mountPoint;
            button.onclick = () => this.fetchForecast(serverId, mountPoint);
            selector.appendChild(button);
        });
    }
    async fetchForecast(serverId, mountPoint) {
        if (!serverId || !mountPoint) return;
        this.currentServerId = serverId;
        this.currentMountPoint = mountPoint;
        this.apiEndpoint = `/api/dashboard/disk-forecast/${serverId}/${encodeURIComponent(mountPoint)}/`;
        await this.fetchData();
    }
    render(data) {
        if (!data || !data.current_usage) { this.showError('No forecast data available'); return; }
        const mountPointEl = document.getElementById('disk-mount-point');
        if (mountPointEl) mountPointEl.textContent = this.currentMountPoint || '—';
        const sizeEl = document.getElementById('disk-size');
        if (sizeEl && data.disk_size) sizeEl.textContent = data.disk_size;
        const usageEl = document.getElementById('disk-current-usage');
        if (usageEl) usageEl.textContent = `${data.current_usage.toFixed(1)}%`;
        const forecastInfo = document.getElementById('disk-forecast-info');
        if (forecastInfo) forecastInfo.style.display = 'block';
        const warningEl = document.getElementById('disk-forecast-warning');
        const warningMsgEl = document.getElementById('disk-forecast-warning-message');
        if (warningEl && warningMsgEl) {
            if (data.warning) {
                warningMsgEl.textContent = data.warning;
                warningEl.style.display = 'block';
            } else {
                warningEl.style.display = 'none';
            }
        }
        if (data.forecast && data.forecast.length > 0) {
            const labels = data.forecast.map(f => {
                const date = new Date(f.date);
                return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
            });
            const currentData = data.forecast.map(f => f.current_usage || data.current_usage);
            const forecastData = data.forecast.map(f => f.predicted_usage || data.current_usage);
            const chartData = {
                labels: labels,
                datasets: [
                    {
                        label: 'Current Usage',
                        data: currentData,
                        borderColor: '#3b82f6',
                        backgroundColor: 'transparent',
                        borderDash: [5, 5],
                        fill: false
                    },
                    {
                        label: 'Forecast',
                        data: forecastData,
                        borderColor: '#a855f7',
                        backgroundColor: 'transparent',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            };
            if (!this.chart) {
                this.chart = ChartWrapper.createLineChart('disk-forecast-chart', chartData, {
                    scales: { y: { max: 100, ticks: { callback: function(value) { return value + '%'; } } } }
                });
            } else {
                ChartWrapper.updateChart(this.chart, chartData);
            }
        }
    }
}
