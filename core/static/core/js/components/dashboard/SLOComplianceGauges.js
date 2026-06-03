/**
 * Reliability Metrics Chart Component
 * Displays CPU, Memory, Disk, and Error Rate as a combined line chart
 */

class SLOComplianceGauges {
    constructor(containerId) {
        this.containerId = containerId;
        this.container = document.getElementById(containerId);
        this.chart = null;
        this.updateInterval = null;
        this.updateIntervalMs = 60000; // Update every 60 seconds
        this.currentView = 'overall'; // 'overall' or 'server'
        this.selectedServerId = null;
        this.selectedPeriod = '24h'; // '24h', '7d', '30d'
        this.servers = [];
        
        // Chart colors
        // Calmer, cohesive palette with translucent area fills
        this.colors = {
            cpu: {
                line: '#6366f1',      // Indigo
                fill: 'rgba(99, 102, 241, 0.12)'
            },
            memory: {
                line: '#14b8a6',      // Teal
                fill: 'rgba(20, 184, 166, 0.12)'
            },
            disk: {
                line: '#f59e0b',      // Amber
                fill: 'rgba(245, 158, 11, 0.12)'
            },
            error_rate: {
                line: '#f43f5e',      // Rose
                fill: 'rgba(244, 63, 94, 0.14)'
            }
        };
    }

    init() {
        if (!this.container) {
            console.error(`SLOComplianceGauges: Container with id "${this.containerId}" not found`);
            return;
        }

        this.setupContainer();
        this.setupEventListeners();
        this.loadServers();
        this.showLoading();
        this.fetchAndRender();
        this.startAutoUpdate();
    }

    setupContainer() {
        // Create the chart container structure
        this.container.innerHTML = `
            <div class="reliability-chart-wrapper">
                <canvas id="reliability-metrics-chart" height="280"></canvas>
            </div>
        `;
    }

    setupEventListeners() {
        // Toggle buttons (Overall/Server)
        const overallBtn = document.getElementById('slo-view-overall');
        const serverBtn = document.getElementById('slo-view-server');
        const serverSelect = document.getElementById('slo-server-select');

        if (overallBtn) {
            overallBtn.addEventListener('click', () => {
                this.switchView('overall');
            });
        }

        if (serverBtn) {
            serverBtn.addEventListener('click', () => {
                this.switchView('server');
            });
        }

        if (serverSelect) {
            serverSelect.addEventListener('change', (e) => {
                this.selectedServerId = e.target.value;
                if (this.selectedServerId) {
                    this.fetchAndRender();
                }
            });
        }

        // Time period buttons
        const periodButtons = document.querySelectorAll('.reliability-period-btn');
        periodButtons.forEach(btn => {
            btn.addEventListener('click', (e) => {
                const period = e.target.dataset.period;
                if (period) {
                    this.selectPeriod(period);
                }
            });
        });
    }

    selectPeriod(period) {
        this.selectedPeriod = period;
        
        // Update button styles
        const periodButtons = document.querySelectorAll('.reliability-period-btn');
        periodButtons.forEach(btn => {
            if (btn.dataset.period === period) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });

        this.fetchAndRender();
    }

    switchView(view) {
        this.currentView = view;
        
        const overallBtn = document.getElementById('slo-view-overall');
        const serverBtn = document.getElementById('slo-view-server');
        const serverSelect = document.getElementById('slo-server-select');

        if (view === 'overall') {
            if (overallBtn) overallBtn.classList.add('active');
            if (serverBtn) serverBtn.classList.remove('active');
            if (serverSelect) serverSelect.style.display = 'none';
            this.selectedServerId = null;
        } else {
            if (overallBtn) overallBtn.classList.remove('active');
            if (serverBtn) serverBtn.classList.add('active');
            if (serverSelect) serverSelect.style.display = 'block';
        }

        // Update button styles
        if (overallBtn) {
            overallBtn.style.color = view === 'overall' 
                ? 'var(--cds-color-gray-100, #0f172a)' 
                : 'var(--cds-color-gray-70, #334155)';
            overallBtn.style.backgroundColor = view === 'overall' 
                ? 'white' 
                : 'transparent';
        }
        if (serverBtn) {
            serverBtn.style.color = view === 'server' 
                ? 'var(--cds-color-gray-100, #0f172a)' 
                : 'var(--cds-color-gray-70, #334155)';
            serverBtn.style.backgroundColor = view === 'server' 
                ? 'white' 
                : 'transparent';
        }

        this.fetchAndRender();
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
            console.error('SLOComplianceGauges: Error loading servers:', error);
        }
    }

    populateServerDropdown() {
        const serverSelect = document.getElementById('slo-server-select');
        if (!serverSelect) return;

        // Clear existing options except the first one
        while (serverSelect.options.length > 1) {
            serverSelect.remove(1);
        }

        // Add servers
        this.servers.forEach(server => {
            const option = document.createElement('option');
            option.value = server.id;
            option.textContent = server.name;
            serverSelect.appendChild(option);
        });

        // Select first server if in server view
        if (this.currentView === 'server' && this.servers.length > 0 && !this.selectedServerId) {
            this.selectedServerId = this.servers[0].id;
            serverSelect.value = this.selectedServerId;
        }
    }

    startAutoUpdate() {
        if (this.updateInterval) {
            clearInterval(this.updateInterval);
        }
        this.updateInterval = setInterval(() => {
            this.fetchAndRender();
        }, this.updateIntervalMs);
    }

    stopAutoUpdate() {
        if (this.updateInterval) {
            clearInterval(this.updateInterval);
            this.updateInterval = null;
        }
    }

    showLoading() {
        const chartWrapper = this.container.querySelector('.reliability-chart-wrapper');
        if (chartWrapper) {
            chartWrapper.innerHTML = `
                <div style="text-align: center; padding: 60px; color: #64748b;">
                    <div class="loading-spinner" style="width: 32px; height: 32px; border: 3px solid #e2e8f0; border-top-color: #3b82f6; border-radius: 50%; animation: spin 1s linear infinite; margin: 0 auto 16px;"></div>
                    <span>Loading metrics...</span>
                </div>
            `;
        }
    }

    async fetchAndRender() {
        // Show loading state
        this.showLoading();
        
        try {
            const serverId = this.currentView === 'server' && this.selectedServerId 
                ? this.selectedServerId 
                : 'all';
            
            const url = `/api/dashboard/reliability-metrics/?period=${this.selectedPeriod}&server_id=${serverId}`;

            const response = await fetch(url);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();
            
            if (data.success) {
                this.render(data.data);
            } else {
                console.error('SLOComplianceGauges: API error:', data.error);
                this.renderError(data.error || 'Failed to load metrics data');
            }
        } catch (error) {
            console.error('SLOComplianceGauges: Fetch error:', error);
            this.renderError('Failed to load metrics data');
        }
    }

    render(data) {
        if (!data) {
            this.renderError('No metrics data available');
            return;
        }

        // Ensure chart container exists
        let chartWrapper = this.container.querySelector('.reliability-chart-wrapper');
        if (!chartWrapper) {
            this.setupContainer();
            chartWrapper = this.container.querySelector('.reliability-chart-wrapper');
        }

        // Ensure canvas exists
        let canvas = document.getElementById('reliability-metrics-chart');
        if (!canvas) {
            chartWrapper.innerHTML = '<canvas id="reliability-metrics-chart" height="280"></canvas>';
            canvas = document.getElementById('reliability-metrics-chart');
        }

        // Prepare labels and datasets
        const cpuData = data.cpu || [];
        const memoryData = data.memory || [];
        const diskData = data.disk || [];
        const errorRateData = data.error_rate || [];

        // Get all unique timestamps for labels
        const allTimestamps = new Set();
        [cpuData, memoryData, diskData, errorRateData].forEach(dataset => {
            dataset.forEach(point => allTimestamps.add(point.timestamp));
        });
        const sortedTimestamps = Array.from(allTimestamps).sort();

        // Create value maps for easy lookup (including peak values)
        const cpuMap = new Map(cpuData.map(p => [p.timestamp, { value: p.value, peak: p.peak }]));
        const memoryMap = new Map(memoryData.map(p => [p.timestamp, { value: p.value, peak: p.peak }]));
        const diskMap = new Map(diskData.map(p => [p.timestamp, p.value]));
        const errorRateMap = new Map(errorRateData.map(p => [p.timestamp, p.value]));

        // Format labels based on period
        const labels = sortedTimestamps.map(ts => this.formatTimestamp(ts, data.interval));

        // Check if we have any significant spikes (CPU > 80%, Memory > 85%)
        const cpuHasSpikes = cpuData.some(p => p.peak && p.peak > 80);
        const memoryHasSpikes = memoryData.some(p => p.peak && p.peak > 85);

        // Build datasets
        const datasets = [
            {
                label: 'CPU',
                data: sortedTimestamps.map(ts => {
                    const d = cpuMap.get(ts);
                    return d ? d.value : null;
                }),
                borderColor: this.colors.cpu.line,
                backgroundColor: this.colors.cpu.fill,
                borderWidth: 2,
                fill: true,
                tension: 0.35,
                pointRadius: sortedTimestamps.length > 48 ? 0 : 2,
                pointHoverRadius: 5
            },
            {
                label: 'CPU (peak)',
                data: sortedTimestamps.map(ts => {
                    const d = cpuMap.get(ts);
                    // Only show peak if above 80% threshold
                    if (d && d.peak && d.peak > 80) {
                        return d.peak;
                    }
                    return null;
                }),
                borderColor: '#4338ca',  // Darker indigo for peaks
                backgroundColor: '#4338ca',
                borderWidth: 0,
                fill: false,
                pointRadius: 8,
                pointHoverRadius: 10,
                pointStyle: 'triangle',
                showLine: false,
                hidden: !cpuHasSpikes,
                clip: false  // Allow triangle to draw above chart area when at 100%
            },
            {
                label: 'Memory',
                data: sortedTimestamps.map(ts => {
                    const d = memoryMap.get(ts);
                    return d ? d.value : null;
                }),
                borderColor: this.colors.memory.line,
                backgroundColor: this.colors.memory.fill,
                borderWidth: 2,
                fill: true,
                tension: 0.35,
                pointRadius: sortedTimestamps.length > 48 ? 0 : 2,
                pointHoverRadius: 5
            },
            {
                label: 'Memory (peak)',
                data: sortedTimestamps.map(ts => {
                    const d = memoryMap.get(ts);
                    // Only show peak if above 85% threshold
                    if (d && d.peak && d.peak > 85) {
                        return d.peak;
                    }
                    return null;
                }),
                borderColor: '#0f766e',  // Darker teal for peaks
                backgroundColor: '#0f766e',
                borderWidth: 0,
                fill: false,
                pointRadius: 8,
                pointHoverRadius: 10,
                pointStyle: 'triangle',
                showLine: false,
                hidden: !memoryHasSpikes,
                clip: false  // Allow triangle to draw above chart area when at 100%
            },
            {
                label: 'Disk',
                data: sortedTimestamps.map(ts => diskMap.get(ts) ?? null),
                borderColor: this.colors.disk.line,
                backgroundColor: this.colors.disk.fill,
                borderWidth: 2,
                fill: true,
                tension: 0.35,
                pointRadius: sortedTimestamps.length > 48 ? 0 : 2,
                pointHoverRadius: 5
            },
            {
                label: 'Error Rate',
                data: sortedTimestamps.map(ts => errorRateMap.get(ts) ?? null),
                borderColor: this.colors.error_rate.line,
                backgroundColor: this.colors.error_rate.fill,
                borderWidth: 2,
                borderDash: [5, 5],  // Dashed line for error rate
                fill: false,
                tension: 0.3,
                pointRadius: sortedTimestamps.length > 48 ? 0 : 3,
                pointHoverRadius: 5
            }
        ];

        // Destroy existing chart if any
        if (this.chart) {
            this.chart.destroy();
        }

        // Create new chart
        const ctx = canvas.getContext('2d');
        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: datasets
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                layout: {
                    padding: {
                        top: 18  // Room for spike triangles at 100% (pointRadius 8, clip: false)
                    }
                },
                interaction: {
                    mode: 'index',
                    intersect: false
                },
                plugins: {
                    legend: {
                        display: true,
                        position: 'bottom',
                        labels: {
                            usePointStyle: true,
                            padding: 20,
                            font: {
                                size: 12
                            },
                            // Keep the legend clean: peak series are shown as markers
                            // on the line itself, not as (struck-through) legend entries.
                            filter: function(item) {
                                return !/\(peak\)/i.test(item.text);
                            }
                        }
                    },
                    tooltip: {
                        backgroundColor: 'rgba(15, 23, 42, 0.9)',
                        titleFont: { size: 13 },
                        bodyFont: { size: 12 },
                        padding: 12,
                        callbacks: {
                            label: function(context) {
                                const value = context.parsed.y;
                                if (value === null || value === undefined) return null;
                                return `${context.dataset.label}: ${value.toFixed(1)}%`;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: {
                            display: false
                        },
                        ticks: {
                            maxRotation: 0,
                            autoSkip: true,
                            maxTicksLimit: this.selectedPeriod === '30d' ? 10 : 12,
                            font: {
                                size: 11
                            },
                            color: '#64748b'
                        }
                    },
                    y: {
                        min: 0,
                        max: 100,
                        grid: {
                            color: 'rgba(226, 232, 240, 0.5)'
                        },
                        ticks: {
                            stepSize: 20,
                            callback: function(value) {
                                return value + '%';
                            },
                            font: {
                                size: 11
                            },
                            color: '#64748b'
                        }
                    }
                }
            }
        });

        // Update stats row
        this.renderStats(data.stats);
    }

    renderStats(stats) {
        const statsRow = document.getElementById('reliability-stats-row');
        if (!statsRow || !stats) return;

        const metrics = [
            { key: 'cpu', label: 'CPU', color: this.colors.cpu.line },
            { key: 'memory', label: 'Memory', color: this.colors.memory.line },
            { key: 'disk', label: 'Disk', color: this.colors.disk.line },
            { key: 'error_rate', label: 'Error Rate', color: this.colors.error_rate.line }
        ];

        statsRow.innerHTML = metrics.map(metric => {
            const stat = stats[metric.key] || { current: 0, average: 0, peak: 0 };
            const isErrorRate = metric.key === 'error_rate';
            
            return `
                <div class="reliability-stat-card" style="border-left: 3px solid ${metric.color};">
                    <div class="stat-label">${metric.label}</div>
                    <div class="stat-values">
                        <div class="stat-item">
                            <span class="stat-value ${isErrorRate && stat.current > 0 ? 'error-value' : ''}">${stat.current.toFixed(1)}%</span>
                            <span class="stat-sublabel">Current</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-value">${stat.average.toFixed(1)}%</span>
                            <span class="stat-sublabel">Avg</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-value ${isErrorRate && stat.peak > 0 ? 'error-value' : ''}">${stat.peak.toFixed(1)}%</span>
                            <span class="stat-sublabel">Peak</span>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
    }

    formatTimestamp(isoString, interval) {
        const date = new Date(isoString);
        
        if (interval === 'day') {
            // For 30-day view, show date
            return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        } else {
            // For hourly view, show time
            return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
        }
    }

    renderError(errorMessage) {
        const chartWrapper = this.container.querySelector('.reliability-chart-wrapper');
        if (chartWrapper) {
            chartWrapper.innerHTML = `
                <div style="padding: 40px; background-color: #fee2e2; border: 1px solid #fca5a5; border-radius: 8px; color: #991b1b; text-align: center;">
                    <span style="font-size: 24px; display: block; margin-bottom: 8px;">⚠️</span>
                    ${this.escapeHtml(errorMessage)}
                </div>
            `;
        }
    }

    escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    destroy() {
        this.stopAutoUpdate();
        if (this.chart) {
            this.chart.destroy();
            this.chart = null;
        }
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    const sloGauges = new SLOComplianceGauges('slo-compliance-gauges-container');
    sloGauges.init();
});
