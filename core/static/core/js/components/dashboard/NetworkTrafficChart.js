/**
 * NetworkTrafficChart - Network traffic chart component (24h) - Stacked Area
 */
class NetworkTrafficChart extends BaseDashboardComponent {
    constructor() {
        super('network-traffic', '/api/dashboard/network-trend/24h/?server_id=all');
        this.chart = null;
        this.servers = [];
        this.currentServerId = 'all';
        this.filterOpen = false;
    }
    async init() {
        await this.loadServers();
        this.setupEventListeners();
        document.addEventListener('click', (e) => {
            const container = document.getElementById('network-traffic-filter-container');
            if (container && !container.contains(e.target)) {
                this.closeDropdown();
            }
        });
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
            console.error('NetworkTrafficChart.loadServers error:', error);
        }
    }
    populateServerDropdown() {
        const dropdown = document.getElementById('network-traffic-filter-dropdown');
        if (!dropdown) return;
        const allOption = dropdown.querySelector('[data-value="all"]');
        dropdown.innerHTML = '';
        if (allOption) dropdown.appendChild(allOption);
        this.servers.forEach(server => {
            const option = document.createElement('div');
            option.className = 'filter-option';
            option.setAttribute('data-value', server.id);
            option.innerHTML = `<span>${this.escapeHtml(server.name)}</span><span class="filter-check" style="display: none;">✓</span>`;
            option.addEventListener('click', () => this.selectServer(server.id, server.name));
            dropdown.appendChild(option);
        });
        this.selectServer('all', 'All VMs (Average)');
    }
    setupEventListeners() {
        const button = document.getElementById('network-traffic-filter-button');
        if (button) {
            button.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleDropdown();
            });
        }
    }
    toggleDropdown() {
        const dropdown = document.getElementById('network-traffic-filter-dropdown');
        if (dropdown) {
            this.filterOpen = !this.filterOpen;
            dropdown.style.display = this.filterOpen ? 'block' : 'none';
        }
    }
    closeDropdown() {
        const dropdown = document.getElementById('network-traffic-filter-dropdown');
        if (dropdown) {
            dropdown.style.display = 'none';
            this.filterOpen = false;
        }
    }
    selectServer(serverId, serverName) {
        this.currentServerId = serverId;
        const textEl = document.getElementById('network-traffic-filter-text');
        if (textEl) textEl.textContent = serverId === 'all' ? 'All VMs (Average)' : serverName;
        const options = document.querySelectorAll('#network-traffic-filter-dropdown .filter-option');
        options.forEach(opt => {
            if (opt.getAttribute('data-value') === String(serverId)) {
                opt.classList.add('selected');
            } else {
                opt.classList.remove('selected');
            }
        });
        this.closeDropdown();
        this.apiEndpoint = `/api/dashboard/network-trend/24h/?server_id=${serverId}`;
        this.fetchData();
    }
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    render(data) {
        if (!data || !data.points || data.points.length === 0) { this.showError('No data available'); return; }
        const lastPoint = data.points[data.points.length - 1];
        if (lastPoint) {
            const inboundEl = document.getElementById('network-inbound');
            if (inboundEl) inboundEl.textContent = `${(lastPoint.inbound || 0).toFixed(1)} MB/s`;
            const outboundEl = document.getElementById('network-outbound');
            if (outboundEl) outboundEl.textContent = `${(lastPoint.outbound || 0).toFixed(1)} MB/s`;
        }
        const labels = data.points.map(point => {
            const date = new Date(point.timestamp);
            return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
        });
        const labelPrefix = this.currentServerId === 'all' ? ' (Average)' : '';
        const chartData = {
            labels: labels,
            datasets: [
                {
                    label: `Inbound${labelPrefix}`,
                    data: data.points.map(p => p.inbound || 0),
                    borderColor: '#a855f7',
                    backgroundColor: 'rgba(168, 85, 247, 0.6)',
                    fill: true
                },
                {
                    label: `Outbound${labelPrefix}`,
                    data: data.points.map(p => p.outbound || 0),
                    borderColor: '#06b6d4',
                    backgroundColor: 'rgba(6, 182, 212, 0.6)',
                    fill: true
                }
            ]
        };
        if (!this.chart) {
            this.chart = ChartWrapper.createStackedAreaChart('network-traffic-chart', chartData, {
                scales: { y: { ticks: { callback: function(value) { return value.toFixed(1) + ' MB/s'; } } } }
            });
        } else {
            ChartWrapper.updateChart(this.chart, chartData);
        }
    }
}
