/**
 * CPUTrendChart - CPU usage trend chart component (24h)
 */
class CPUTrendChart extends BaseDashboardComponent {
    constructor() {
        super('cpu-trend', '/api/dashboard/cpu-trend/24h/?server_id=all');
        this.chart = null;
        this.servers = [];
        this.currentServerId = 'all';
        this.filterOpen = false;
    }
    async init() {
        await this.loadServers();
        this.setupEventListeners();
        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            const container = document.getElementById('cpu-trend-filter-container');
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
            console.error('CPUTrendChart.loadServers error:', error);
        }
    }
    populateServerDropdown() {
        const dropdown = document.getElementById('cpu-trend-filter-dropdown');
        if (!dropdown) return;
        
        // Clear existing options
        dropdown.innerHTML = '';
        
        // Create "All VMs" option with click handler
        const allOption = document.createElement('div');
        allOption.className = 'filter-option';
        allOption.setAttribute('data-value', 'all');
        allOption.innerHTML = `
            <span>All VMs (Average)</span>
            <span class="filter-check" style="display: none;">✓</span>
        `;
        allOption.addEventListener('click', () => this.selectServer('all', 'All VMs (Average)'));
        dropdown.appendChild(allOption);
        
        // Add individual servers
        this.servers.forEach(server => {
            const option = document.createElement('div');
            option.className = 'filter-option';
            option.setAttribute('data-value', server.id);
            option.innerHTML = `
                <span>${this.escapeHtml(server.name)}</span>
                <span class="filter-check" style="display: none;">✓</span>
            `;
            option.addEventListener('click', () => this.selectServer(server.id, server.name));
            dropdown.appendChild(option);
        });
        
        // Restore saved selection or default to "All VMs"
        const savedServerId = localStorage.getItem('cpu-trend-selected-server');
        if (savedServerId) {
            const savedServer = this.servers.find(s => String(s.id) === savedServerId);
            if (savedServer) {
                this.selectServer(savedServerId, savedServer.name);
            } else if (savedServerId === 'all') {
                this.selectServer('all', 'All VMs (Average)');
            } else {
                this.selectServer('all', 'All VMs (Average)');
            }
        } else {
            this.selectServer('all', 'All VMs (Average)');
        }
    }
    setupEventListeners() {
        const button = document.getElementById('cpu-trend-filter-button');
        if (button) {
            button.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleDropdown();
            });
        }
    }
    toggleDropdown() {
        const dropdown = document.getElementById('cpu-trend-filter-dropdown');
        if (!dropdown) return;
        
        this.filterOpen = !this.filterOpen;
        dropdown.style.display = this.filterOpen ? 'block' : 'none';
    }
    closeDropdown() {
        const dropdown = document.getElementById('cpu-trend-filter-dropdown');
        if (dropdown) {
            dropdown.style.display = 'none';
            this.filterOpen = false;
        }
    }
    selectServer(serverId, serverName) {
        this.currentServerId = serverId;
        
        // Save selection to localStorage
        localStorage.setItem('cpu-trend-selected-server', String(serverId));
        
        // Update button text
        const textEl = document.getElementById('cpu-trend-filter-text');
        if (textEl) textEl.textContent = serverId === 'all' ? 'All VMs (Average)' : serverName;
        
        // Update selected state in dropdown
        const options = document.querySelectorAll('#cpu-trend-filter-dropdown .filter-option');
        options.forEach(opt => {
            const checkMark = opt.querySelector('.filter-check');
            if (opt.getAttribute('data-value') === String(serverId)) {
                opt.classList.add('selected');
                if (checkMark) checkMark.style.display = 'inline';
            } else {
                opt.classList.remove('selected');
                if (checkMark) checkMark.style.display = 'none';
            }
        });
        
        // Close dropdown
        this.closeDropdown();
        
        // Update API endpoint and fetch data
        this.apiEndpoint = `/api/dashboard/cpu-trend/24h/?server_id=${serverId}`;
        this.fetchData();
    }
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    render(data) {
        if (!data || !data.points) { this.showError('No data available'); return; }
        if (data.current !== undefined) {
            const currentEl = document.getElementById('cpu-current');
            if (currentEl) currentEl.textContent = `${data.current}%`;
        }
        if (data.peak !== undefined) {
            const peakEl = document.getElementById('cpu-peak');
            if (peakEl) peakEl.textContent = `${data.peak}%`;
        }
        if (data.average !== undefined) {
            const avgEl = document.getElementById('cpu-average');
            if (avgEl) avgEl.textContent = `${data.average}%`;
        }
        const labels = data.points.map(point => {
            const date = new Date(point.timestamp);
            return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
        });
        const label = this.currentServerId === 'all' ? 'CPU % (Average)' : 'CPU %';
        const chartData = {
            labels: labels,
            datasets: [{
                label: label,
                data: data.points.map(p => p.value),
                borderColor: '#0ea5e9',
                backgroundColor: 'rgba(14, 165, 233, 0.1)',
                fill: true
            }]
        };
        if (!this.chart) {
            this.chart = ChartWrapper.createLineChart('cpu-trend-chart', chartData, {
                scales: { y: { max: 100, ticks: { callback: function(value) { return value + '%'; } } } }
            });
        } else {
            ChartWrapper.updateChart(this.chart, chartData);
        }
    }
}
