/**
 * CPUTrendChart - CPU usage trend chart component (24h)
 */
class CPUTrendChart extends BaseDashboardComponent {
    constructor() {
        super('cpu-trend', '/api/dashboard/cpu-trend/24h/?server_id=all');
        this.chart = null;
        this.servers = [];
        this.currentServerId = 'all';
        this.currentPeriod = '24h';
        this.filterOpen = false;
    }
    
    setPeriod(period) {
        if (!period) {
            console.error('[CPUTrendChart] setPeriod called with invalid period:', period);
            return;
        }
        this.currentPeriod = period;
        const serverId = this.currentServerId || 'all';
        this.apiEndpoint = `/api/dashboard/cpu-trend/${period}/?server_id=${serverId}`;
        console.log(`[CPUTrendChart] Setting period to ${period}, serverId: ${serverId}, API endpoint: ${this.apiEndpoint}`);
        this.fetchData();
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
        const popout = document.getElementById('cpu-trend-popout');
        if (popout && window.CpuUtil) {
            popout.addEventListener('click', () => {
                CpuUtil.popout('CPU Usage Trend', this.points || []);
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
        this.apiEndpoint = `/api/dashboard/cpu-trend/${this.currentPeriod}/?server_id=${serverId}`;
        this.fetchData();
    }
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    render(data) {
        if (!data || !data.points) { this.showError('No data available'); return; }

        // {x, y, top} points; `top` (top processes at the busiest sample of the hour) is
        // only present in single-server mode -- the "All VMs" average can't attribute it.
        this.points = data.points.map(p => ({
            x: new Date(p.timestamp).getTime(),
            y: p.value,
            top: p.top || []
        }));

        // CpuUtil computes Avg/Min/Max from the series (consistent with the orange
        // average line) and writes them into the summary cells.
        const statEls = {
            avg: document.getElementById('cpu-average'),
            min: document.getElementById('cpu-min'),
            max: document.getElementById('cpu-max'),
        };

        if (!this.chart) {
            this.chart = CpuUtil.render('cpu-trend-chart', this.points, { statEls });
        } else {
            CpuUtil.update(this.chart, this.points, { statEls });
        }
    }
}
