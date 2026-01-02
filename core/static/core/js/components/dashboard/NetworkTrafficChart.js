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
            option.innerHTML = `<span>${this.escapeHtml(server.name)}</span><span class="filter-check" style="display: none;">✓</span>`;
            option.addEventListener('click', () => this.selectServer(server.id, server.name));
            dropdown.appendChild(option);
        });
        
        // Restore saved selection or default to "All VMs"
        const savedServerId = localStorage.getItem('network-traffic-selected-server');
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
        
        // Save selection to localStorage
        localStorage.setItem('network-traffic-selected-server', String(serverId));
        
        const textEl = document.getElementById('network-traffic-filter-text');
        if (textEl) textEl.textContent = serverId === 'all' ? 'All VMs (Average)' : serverName;
        const options = document.querySelectorAll('#network-traffic-filter-dropdown .filter-option');
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
            const inboundVal = parseFloat(lastPoint.inbound) || 0;
            const outboundVal = parseFloat(lastPoint.outbound) || 0;
            
            // Use appropriate precision based on value
            let inboundPrecision = 1;
            let outboundPrecision = 1;
            
            if (inboundVal > 0) {
                if (inboundVal < 0.01) {
                    inboundPrecision = 3;
                } else if (inboundVal < 0.1) {
                    inboundPrecision = 2;
                }
            }
            
            if (outboundVal > 0) {
                if (outboundVal < 0.01) {
                    outboundPrecision = 3;
                } else if (outboundVal < 0.1) {
                    outboundPrecision = 2;
                }
            }
            
            const inboundEl = document.getElementById('network-inbound');
            if (inboundEl) {
                inboundEl.textContent = inboundVal.toFixed(inboundPrecision) + ' MB/s';
            }
            const outboundEl = document.getElementById('network-outbound');
            if (outboundEl) {
                outboundEl.textContent = outboundVal.toFixed(outboundPrecision) + ' MB/s';
            }
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
        // Calculate min and max values for proper Y-axis scaling
        // Include all values (including zeros) for max calculation, but filter invalid values
        const allValues = [
            ...data.points.map(p => p.inbound || 0),
            ...data.points.map(p => p.outbound || 0)
        ].filter(v => !isNaN(v) && isFinite(v));
        
        // Get max from all values (including zeros) - this helps ensure scale is set even if data is sparse
        const actualMax = allValues.length > 0 ? Math.max(...allValues) : 0;
        
        // Also get max from non-zero values to better handle cases with mostly zero data
        const nonZeroValues = allValues.filter(v => v > 0);
        const actualMaxNonZero = nonZeroValues.length > 0 ? Math.max(...nonZeroValues) : 0;
        
        // Use the larger of the two to ensure scale is visible
        const maxToUse = Math.max(actualMax, actualMaxNonZero);
        
        // Calculate a nice max value for Y-axis with better precision handling
        let niceMax;
        let stepSize;
        let precision;
        
        if (maxToUse === 0) {
            // If all values are 0, show a small default range
            niceMax = 1.0;
            stepSize = 0.2;
            precision = 1;
        } else if (maxToUse < 0.001) {
            // For extremely tiny values
            niceMax = 0.005;
            stepSize = 0.001;
            precision = 3;
        } else if (maxToUse < 0.01) {
            // For very tiny values (< 0.01 MB/s), use 3 decimal places
            niceMax = Math.ceil(maxToUse * 1000) / 1000;
            niceMax = Math.max(niceMax, 0.01);
            stepSize = niceMax / 5;
            precision = 3;
        } else if (maxToUse < 0.1) {
            // For small values (< 0.1 MB/s), use 2 decimal places
            niceMax = Math.ceil(maxToUse * 100) / 100;
            niceMax = Math.max(niceMax, 0.1);
            stepSize = niceMax / 5;
            precision = 2;
        } else if (maxToUse < 1) {
            // For values less than 1, round up to next 0.1
            niceMax = Math.ceil(maxToUse * 10) / 10;
            stepSize = niceMax / 5;
            precision = 1;
        } else {
            // For larger values, round up to next nice number
            const magnitude = Math.pow(10, Math.floor(Math.log10(maxToUse)));
            niceMax = Math.ceil(maxToUse / magnitude) * magnitude;
            stepSize = niceMax / 5;
            precision = 1;
        }
        
        // Debug logging
        console.log('Network Traffic Chart - actualMax:', actualMax, 'maxToUse:', maxToUse, 'niceMax:', niceMax, 'stepSize:', stepSize, 'precision:', precision);
        console.log('Sample values:', allValues.slice(0, 10));
        console.log('Non-zero values:', nonZeroValues.slice(0, 10));
        
        // Store precision in closure for callback
        const formatPrecision = precision;
        
        const chartOptions = {
            scales: {
                x: {
                    stacked: true  // Keep X stacked for area chart
                },
                y: {
                    stacked: false,  // Disable Y stacking to show actual values, not stacked sum
                    beginAtZero: true,
                    min: 0,
                    max: niceMax,
                    ticks: {
                        stepSize: stepSize,
                        maxTicksLimit: 10,
                        callback: function(value) {
                            return value.toFixed(formatPrecision) + ' MB/s';
                        }
                    }
                }
            }
        };
        
        if (!this.chart) {
            this.chart = ChartWrapper.createStackedAreaChart('network-traffic-chart', chartData, chartOptions);
        } else {
            // Destroy and recreate chart to ensure scale updates properly
            ChartWrapper.destroyChart(this.chart);
            this.chart = ChartWrapper.createStackedAreaChart('network-traffic-chart', chartData, chartOptions);
        }
    }
}
