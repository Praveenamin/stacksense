/**
 * ResponseTimeChart - Service response time trend chart component (24h)
 * Shows average latency for monitored services over time.
 */
class ResponseTimeChart extends BaseDashboardComponent {
    constructor() {
        super('response-time', '/api/dashboard/response-time-trend/24h/?server_id=all');
        this.chart = null;
        this.servers = [];
        this.currentServerId = 'all';
        this.currentPeriod = '24h';
        this.filterOpen = false;
    }
    
    setPeriod(period) {
        this.currentPeriod = period;
        this.apiEndpoint = `/api/dashboard/response-time-trend/${period}/?server_id=${this.currentServerId}`;
        this.fetchData();
    }
    
    async init() {
        await this.loadServers();
        this.setupEventListeners();
        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            const container = document.getElementById('response-time-filter-container');
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
            console.error('ResponseTimeChart.loadServers error:', error);
        }
    }
    
    populateServerDropdown() {
        const dropdown = document.getElementById('response-time-filter-dropdown');
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
        const savedServerId = localStorage.getItem('response-time-selected-server');
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
        const button = document.getElementById('response-time-filter-button');
        if (button) {
            button.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleDropdown();
            });
        }
    }
    
    toggleDropdown() {
        const dropdown = document.getElementById('response-time-filter-dropdown');
        if (!dropdown) return;
        
        this.filterOpen = !this.filterOpen;
        dropdown.style.display = this.filterOpen ? 'block' : 'none';
    }
    
    closeDropdown() {
        const dropdown = document.getElementById('response-time-filter-dropdown');
        if (dropdown) {
            dropdown.style.display = 'none';
            this.filterOpen = false;
        }
    }
    
    selectServer(serverId, serverName) {
        this.currentServerId = serverId;
        
        // Save selection to localStorage
        localStorage.setItem('response-time-selected-server', String(serverId));
        
        // Update button text
        const textEl = document.getElementById('response-time-filter-text');
        if (textEl) textEl.textContent = serverId === 'all' ? 'All VMs (Average)' : serverName;
        
        // Update selected state in dropdown
        const options = document.querySelectorAll('#response-time-filter-dropdown .filter-option');
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
        this.apiEndpoint = `/api/dashboard/response-time-trend/${this.currentPeriod}/?server_id=${serverId}`;
        this.fetchData();
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    
    render(data) {
        // Update summary values even if no chart data
        const currentEl = document.getElementById('response-time-current');
        const peakEl = document.getElementById('response-time-peak');
        const avgEl = document.getElementById('response-time-average');
        const servicesEl = document.getElementById('response-time-services');
        
        if (data) {
            if (currentEl) currentEl.textContent = data.current !== undefined ? `${data.current}ms` : '—';
            if (peakEl) peakEl.textContent = data.peak !== undefined ? `${data.peak}ms` : '—';
            if (avgEl) avgEl.textContent = data.average !== undefined ? `${data.average}ms` : '—';
            if (servicesEl) servicesEl.textContent = data.monitored_services !== undefined ? data.monitored_services : '—';
        }
        
        // Check if we have data points for the chart
        if (!data || !data.points) { 
            this.showNoData(); 
            return; 
        }
        
        // Prepare chart data
        const labels = data.points.map(point => {
            const date = new Date(point.timestamp);
            return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
        });
        
        const label = this.currentServerId === 'all' ? 'Latency (Average)' : 'Latency';
        const chartData = {
            labels: labels,
            datasets: [{
                label: label,
                data: data.points.map(p => p.value),
                borderColor: '#f59e0b',  // Amber, matching the unified palette
                backgroundColor: 'rgba(245, 158, 11, 0.12)',
                fill: true,
                tension: 0.35
            }]
        };
        
        // Create or update chart
        if (!this.chart) {
            this.chart = ChartWrapper.createLineChart('response-time-chart', chartData, {
                scales: { 
                    y: { 
                        min: 0,
                        ticks: { 
                            callback: function(value) { 
                                return value + 'ms'; 
                            } 
                        } 
                    } 
                },
                plugins: {
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                return `${context.dataset.label}: ${context.raw.toFixed(2)}ms`;
                            }
                        }
                    }
                }
            });
        } else {
            ChartWrapper.updateChart(this.chart, chartData);
        }
    }
    
    showNoData() {
        const container = document.getElementById('dashboard-content-response-time');
        if (container) {
            const chartContainer = container.querySelector('.chart-container');
            if (chartContainer) {
                chartContainer.innerHTML = `
                    <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 200px; color: var(--cds-color-gray-50, #64748b);">
                        <div style="font-size: 32px; margin-bottom: 12px;">⚡</div>
                        <div style="font-weight: 500; margin-bottom: 8px;">No Response Time Data</div>
                        <div style="font-size: 13px; text-align: center; max-width: 300px;">
                            Enable monitoring on services to start collecting latency data.
                            Services are auto-detected from listening ports.
                        </div>
                    </div>
                `;
            }
        }
    }
}
