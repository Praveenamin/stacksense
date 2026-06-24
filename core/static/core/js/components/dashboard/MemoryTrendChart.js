/**
 * MemoryTrendChart - Memory usage trend chart component (24h)
 */
class MemoryTrendChart extends BaseDashboardComponent {
    constructor() {
        super('memory-trend', '/api/dashboard/memory-trend/24h/?server_id=all');
        this.chart = null;
        this.servers = [];
        this.currentServerId = 'all';
        this.currentPeriod = '24h';
        this.filterOpen = false;
    }
    
    setPeriod(period) {
        if (!period) {
            console.error('[MemoryTrendChart] setPeriod called with invalid period:', period);
            return;
        }
        this.currentPeriod = period;
        const serverId = this.currentServerId || 'all';
        this.apiEndpoint = `/api/dashboard/memory-trend/${period}/?server_id=${serverId}`;
        console.log(`[MemoryTrendChart] Setting period to ${period}, serverId: ${serverId}, API endpoint: ${this.apiEndpoint}`);
        this.fetchData();
    }
    async init() {
        await this.loadServers();
        this.setupEventListeners();
        document.addEventListener('click', (e) => {
            const container = document.getElementById('memory-trend-filter-container');
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
            console.error('MemoryTrendChart.loadServers error:', error);
        }
    }
    populateServerDropdown() {
        const dropdown = document.getElementById('memory-trend-filter-dropdown');
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
        const savedServerId = localStorage.getItem('memory-trend-selected-server');
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
        const button = document.getElementById('memory-trend-filter-button');
        if (button) {
            button.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleDropdown();
            });
        }
        const popout = document.getElementById('memory-trend-popout');
        if (popout && window.CpuUtil) {
            popout.addEventListener('click', () => {
                CpuUtil.popout('Memory Usage Trend', this.points || [],
                    { label: 'Memory Usage', color: '#14b8a6', fill: 'rgba(20,184,166,0.25)' });
            });
        }
    }
    toggleDropdown() {
        const dropdown = document.getElementById('memory-trend-filter-dropdown');
        if (dropdown) {
            this.filterOpen = !this.filterOpen;
            dropdown.style.display = this.filterOpen ? 'block' : 'none';
        }
    }
    closeDropdown() {
        const dropdown = document.getElementById('memory-trend-filter-dropdown');
        if (dropdown) {
            dropdown.style.display = 'none';
            this.filterOpen = false;
        }
    }
    selectServer(serverId, serverName) {
        this.currentServerId = serverId;
        
        // Save selection to localStorage
        localStorage.setItem('memory-trend-selected-server', String(serverId));
        
        const textEl = document.getElementById('memory-trend-filter-text');
        if (textEl) textEl.textContent = serverId === 'all' ? 'All VMs (Average)' : serverName;
        const options = document.querySelectorAll('#memory-trend-filter-dropdown .filter-option');
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
        this.apiEndpoint = `/api/dashboard/memory-trend/${this.currentPeriod}/?server_id=${serverId}`;
        this.fetchData();
    }
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    render(data) {
        if (!data || !data.points) { this.showError('No data available'); return; }

        // {x, y, top} points; `top` (top processes by memory at the fullest sample of the
        // hour) is only present in single-server mode.
        this.points = data.points.map(p => ({
            x: new Date(p.timestamp).getTime(),
            y: p.value,
            top: p.top || []
        }));

        const statEls = {
            avg: document.getElementById('memory-average'),
            min: document.getElementById('memory-min'),
            max: document.getElementById('memory-max'),
        };
        const opts = { label: 'Memory Usage', color: '#14b8a6', fill: 'rgba(20,184,166,0.25)', statEls };

        if (!this.chart) {
            this.chart = CpuUtil.render('memory-trend-chart', this.points, opts);
        } else {
            CpuUtil.update(this.chart, this.points, opts);
        }
    }
}
