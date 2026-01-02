/**
 * DiskIOSummary - Disk I/O summary component
 */
class DiskIOSummary extends BaseDashboardComponent {
    constructor() {
        super('disk-io-summary', '/api/dashboard/disk-io-summary/?server_id=all');
        this.chart = null;
        this.servers = [];
        this.currentServerId = 'all';
        this.filterOpen = false;
    }
    async init() {
        await this.loadServers();
        this.setupEventListeners();
        document.addEventListener('click', (e) => {
            const container = document.getElementById('disk-io-filter-container');
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
            console.error('DiskIOSummary.loadServers error:', error);
        }
    }
    populateServerDropdown() {
        const dropdown = document.getElementById('disk-io-filter-dropdown');
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
        const savedServerId = localStorage.getItem('disk-io-selected-server');
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
        const button = document.getElementById('disk-io-filter-button');
        if (button) {
            button.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleDropdown();
            });
        }
    }
    toggleDropdown() {
        const dropdown = document.getElementById('disk-io-filter-dropdown');
        if (dropdown) {
            this.filterOpen = !this.filterOpen;
            dropdown.style.display = this.filterOpen ? 'block' : 'none';
        }
    }
    closeDropdown() {
        const dropdown = document.getElementById('disk-io-filter-dropdown');
        if (dropdown) {
            dropdown.style.display = 'none';
            this.filterOpen = false;
        }
    }
    selectServer(serverId, serverName) {
        this.currentServerId = serverId;
        
        // Save selection to localStorage
        localStorage.setItem('disk-io-selected-server', String(serverId));
        
        const textEl = document.getElementById('disk-io-filter-text');
        if (textEl) textEl.textContent = serverId === 'all' ? 'All VMs (Average)' : serverName;
        const options = document.querySelectorAll('#disk-io-filter-dropdown .filter-option');
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
        this.apiEndpoint = `/api/dashboard/disk-io-summary/?server_id=${serverId}`;
        this.fetchData();
    }
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    render(data) {
        if (!data) { this.showError('No data available'); return; }
        const readEl = document.getElementById('disk-read-iops');
        if (readEl) readEl.textContent = Math.round(data.read_iops || 0);
        const writeEl = document.getElementById('disk-write-iops');
        if (writeEl) writeEl.textContent = Math.round(data.write_iops || 0);
        const totalEl = document.getElementById('disk-total-iops');
        if (totalEl) totalEl.textContent = Math.round(data.total_iops || 0);
        const ratioEl = document.getElementById('disk-rw-ratio');
        if (ratioEl) ratioEl.textContent = data.read_write_ratio || '—';
        const percentEl = document.getElementById('disk-read-percent');
        if (percentEl) percentEl.textContent = `${Math.round(data.read_percentage || 0)}%`;
        const statusEl = document.getElementById('disk-io-status');
        const statusMsgEl = document.getElementById('disk-io-status-message');
        if (statusEl && statusMsgEl && data.status_message) {
            statusMsgEl.textContent = data.status_message;
            statusEl.className = `forecast-warning ${data.status_class || 'info'}`;
            statusEl.style.display = 'block';
        }
        const chartData = {
            labels: ['Read', 'Write'],
            datasets: [{
                label: 'IOPS',
                data: [Math.round(data.read_iops || 0), Math.round(data.write_iops || 0)],
                backgroundColor: ['#3b82f6', '#ef4444']
            }]
        };
        if (!this.chart) {
            this.chart = ChartWrapper.createBarChart('disk-io-bar-chart', chartData, {
                scales: { y: { ticks: { callback: function(value) { return value.toFixed(0) + ' IOPS'; } } } }
            });
        } else {
            ChartWrapper.updateChart(this.chart, chartData);
        }
    }
}
