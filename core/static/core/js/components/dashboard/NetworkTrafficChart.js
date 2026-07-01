/**
 * NetworkTrafficChart - Network traffic chart component (24h).
 * Styled to match the CPU/Memory usage-trend redesign: smooth semi-transparent areas
 * (teal inbound, orange outbound), a dark index tooltip and clean axes, plus a pop-out.
 * Dual-series, so it keeps the Inbound/Outbound summary rather than Avg/Min/Max.
 */
class NetworkTrafficChart extends BaseDashboardComponent {
    constructor() {
        super('network-traffic', '/api/dashboard/network-trend/24h/?server_id=all');
        this.chart = null;
        this.servers = [];
        this.currentServerId = 'all';
        this.currentPeriod = '24h';
        this.filterOpen = false;
        this._lastPoints = null;
        this._pop = null;
    }

    setPeriod(period) {
        this.currentPeriod = period;
        this.apiEndpoint = `/api/dashboard/network-trend/${period}/?server_id=${this.currentServerId}`;
        this.fetchData();
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
        const popout = document.getElementById('network-traffic-popout');
        if (popout) {
            popout.addEventListener('click', () => this.openPopout());
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
        this.apiEndpoint = `/api/dashboard/network-trend/${this.currentPeriod}/?server_id=${serverId}`;
        this.fetchData();
    }
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Value-aware precision: tiny rates need more decimals.
    _precision(v) {
        if (v > 0 && v < 0.01) return 3;
        if (v > 0 && v < 0.1) return 2;
        return 1;
    }

    // Build fresh chartData + chartOptions from the points (fresh objects each call so the
    // card chart and the pop-out chart never share mutable Chart.js state).
    _compose(points) {
        const labels = points.map(point => {
            const date = new Date(point.timestamp);
            return date.toLocaleString('en-US', (this.currentPeriod === '7d' || this.currentPeriod === '30d')
                ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
                : { hour: '2-digit', minute: '2-digit' });
        });
        const labelPrefix = this.currentServerId === 'all' ? ' (Average)' : '';

        const chartData = {
            labels: labels,
            datasets: [
                {
                    label: `Inbound${labelPrefix}`,
                    data: points.map(p => p.inbound || 0),
                    borderColor: '#2cbfc7',
                    backgroundColor: 'rgba(45,191,199,0.28)',
                    fill: true, tension: 0.3, borderWidth: 1.5,
                    pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: '#2cbfc7'
                },
                {
                    label: `Outbound${labelPrefix}`,
                    data: points.map(p => p.outbound || 0),
                    borderColor: '#f97316',
                    backgroundColor: 'rgba(249,115,22,0.18)',
                    fill: true, tension: 0.3, borderWidth: 1.5,
                    pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: '#f97316'
                }
            ]
        };

        // Nice Y-axis max / step / precision from the data (keep zeros so sparse data still scales).
        const allValues = [
            ...points.map(p => p.inbound || 0),
            ...points.map(p => p.outbound || 0)
        ].filter(v => !isNaN(v) && isFinite(v));
        const maxToUse = allValues.length ? Math.max(...allValues) : 0;
        let niceMax, stepSize, precision;
        if (maxToUse === 0) { niceMax = 1.0; stepSize = 0.2; precision = 1; }
        else if (maxToUse < 0.01) { niceMax = Math.max(Math.ceil(maxToUse * 1000) / 1000, 0.01); stepSize = niceMax / 5; precision = 3; }
        else if (maxToUse < 0.1) { niceMax = Math.max(Math.ceil(maxToUse * 100) / 100, 0.1); stepSize = niceMax / 5; precision = 2; }
        else if (maxToUse < 1) { niceMax = Math.ceil(maxToUse * 10) / 10; stepSize = niceMax / 5; precision = 1; }
        else {
            const magnitude = Math.pow(10, Math.floor(Math.log10(maxToUse)));
            niceMax = Math.ceil(maxToUse / magnitude) * magnitude; stepSize = niceMax / 5; precision = 1;
        }

        const chartOptions = {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: {
                    display: true, position: 'top', align: 'end',
                    labels: { usePointStyle: true, boxWidth: 8, boxHeight: 8, font: { size: 11 }, color: '#64748b' }
                },
                tooltip: {
                    mode: 'index', intersect: false,
                    backgroundColor: 'rgba(71,85,105,0.95)', padding: 10,
                    titleColor: '#fff', bodyColor: '#fff',
                    titleFont: { size: 12, weight: '600' }, bodyFont: { size: 12 },
                    callbacks: {
                        label: function (item) {
                            return item.dataset.label + ' : ' + (item.parsed.y || 0).toFixed(precision) + ' MB/s';
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxTicksLimit: 8, autoSkip: true, maxRotation: 0, font: { size: 10 }, color: '#94a3b8' }
                },
                y: {
                    beginAtZero: true, min: 0, max: niceMax,
                    grid: { color: 'rgba(15,23,42,0.05)' },
                    ticks: {
                        stepSize: stepSize, maxTicksLimit: 10, font: { size: 10 }, color: '#94a3b8',
                        callback: function (value) { return value.toFixed(precision) + ' MB/s'; }
                    }
                }
            }
        };

        return { chartData, chartOptions };
    }

    _buildChart(canvasId, chartData, chartOptions) {
        const el = document.getElementById(canvasId);
        if (!el || typeof Chart === 'undefined') return null;
        return new Chart(el.getContext('2d'), { type: 'line', data: chartData, options: chartOptions });
    }

    render(data) {
        if (!data || !data.points || data.points.length === 0) { this.showError('No data available'); return; }

        // Summary: latest inbound / outbound.
        const lastPoint = data.points[data.points.length - 1];
        if (lastPoint) {
            const inboundVal = parseFloat(lastPoint.inbound) || 0;
            const outboundVal = parseFloat(lastPoint.outbound) || 0;
            const inboundEl = document.getElementById('network-inbound');
            if (inboundEl) inboundEl.textContent = inboundVal.toFixed(this._precision(inboundVal)) + ' MB/s';
            const outboundEl = document.getElementById('network-outbound');
            if (outboundEl) outboundEl.textContent = outboundVal.toFixed(this._precision(outboundVal)) + ' MB/s';
        }

        this._lastPoints = data.points;
        const { chartData, chartOptions } = this._compose(data.points);
        if (this.chart) { this.chart.destroy(); this.chart = null; }
        this.chart = this._buildChart('network-traffic-chart', chartData, chartOptions);
    }

    // --- Pop-out: re-render the same two series larger in a centered overlay. ---
    _ensurePopout() {
        if (this._pop) return this._pop;
        const ov = document.createElement('div');
        ov.style.cssText = 'position:fixed;inset:0;z-index:9999;display:none;' +
            'background:rgba(15,23,42,0.55);align-items:center;justify-content:center;';
        ov.innerHTML =
            '<div style="background:#fff;border-radius:12px;width:min(1100px,94vw);max-height:90vh;' +
                'padding:18px 20px;box-shadow:0 20px 60px rgba(0,0,0,.3);">' +
              '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">' +
                '<h3 style="margin:0;font-size:15px;color:#0f172a;">Network Traffic</h3>' +
                '<button type="button" class="ntc-pop-close" style="border:none;background:#f1f5f9;border-radius:6px;' +
                  'width:28px;height:28px;cursor:pointer;font-size:16px;color:#475569;">&times;</button>' +
              '</div>' +
              '<div style="height:62vh;"><canvas class="ntc-pop-canvas"></canvas></div>' +
            '</div>';
        document.body.appendChild(ov);
        const close = () => {
            ov.style.display = 'none';
            if (this._pop && this._pop.chart) { this._pop.chart.destroy(); this._pop.chart = null; }
        };
        ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
        ov.querySelector('.ntc-pop-close').addEventListener('click', close);
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && ov.style.display !== 'none') close(); });
        this._pop = { overlay: ov, chart: null, canvas: ov.querySelector('.ntc-pop-canvas') };
        return this._pop;
    }

    openPopout() {
        if (!this._lastPoints || typeof Chart === 'undefined') return;
        const p = this._ensurePopout();
        p.overlay.style.display = 'flex';
        if (p.chart) { p.chart.destroy(); p.chart = null; }
        const { chartData, chartOptions } = this._compose(this._lastPoints);
        p.chart = new Chart(p.canvas.getContext('2d'), { type: 'line', data: chartData, options: chartOptions });
    }
}
