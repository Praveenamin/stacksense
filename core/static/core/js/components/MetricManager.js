/**
 * MetricManager - Coordinates multiple metric components and handles API calls
 * Manages all metric components for servers and handles refresh intervals
 */
class MetricManager {
    constructor(serverId, metricConfigs = {}) {
        this.serverId = serverId;
        this.metricConfigs = metricConfigs;
        this.metrics = {};
        this.refreshInterval = null;
        this.refreshIntervalMs = 10000; // Default 10 seconds
        this.isRefreshing = false;
        
        // Initialize metric components
        this._initializeMetrics();
    }
    
    /**
     * Initialize metric component instances based on config
     */
    _initializeMetrics() {
        // CPU Metric
        if (this.metricConfigs.cpu && this.metricConfigs.cpu.elementId) {
            const element = document.getElementById(this.metricConfigs.cpu.elementId);
            if (element) {
                this.metrics.cpu = new CPUMetric(
                    this.serverId,
                    this.metricConfigs.cpu.elementId,
                    this.metricConfigs.cpu.options || {}
                );
            }
        }
        
        // Memory Metric
        if (this.metricConfigs.memory && this.metricConfigs.memory.elementId) {
            const element = document.getElementById(this.metricConfigs.memory.elementId);
            if (element) {
                this.metrics.memory = new MemoryMetric(
                    this.serverId,
                    this.metricConfigs.memory.elementId,
                    this.metricConfigs.memory.options || {}
                );
            }
        }
        
        // Disk Metric
        if (this.metricConfigs.disk && this.metricConfigs.disk.elementId) {
            const element = document.getElementById(this.metricConfigs.disk.elementId);
            if (element) {
                this.metrics.disk = new DiskMetric(
                    this.serverId,
                    this.metricConfigs.disk.elementId,
                    this.metricConfigs.disk.options || {}
                );
            }
        }
        
        // Network Metric
        if (this.metricConfigs.network && this.metricConfigs.network.elementId) {
            const element = document.getElementById(this.metricConfigs.network.elementId);
            if (element) {
                this.metrics.network = new NetworkMetric(
                    this.serverId,
                    this.metricConfigs.network.elementId,
                    this.metricConfigs.network.options || {}
                );
            }
        }
        
        // Uptime Metric
        if (this.metricConfigs.uptime && this.metricConfigs.uptime.elementId) {
            const element = document.getElementById(this.metricConfigs.uptime.elementId);
            if (element) {
                this.metrics.uptime = new UptimeMetric(
                    this.serverId,
                    this.metricConfigs.uptime.elementId,
                    this.metricConfigs.uptime.options || {}
                );
            }
        }
    }
    
    /**
     * Fetch metrics from API and update all components
     */
    async fetchAndUpdate() {
        if (this.isRefreshing) {
            return; // Prevent concurrent fetches
        }
        
        this.isRefreshing = true;
        
        try {
            const response = await fetch('/api/live-metrics/');
            if (!response.ok) {
                console.error(`MetricManager: Failed to fetch metrics for server ${this.serverId}:`, response.status);
                this._showErrorsOnAll('Failed to fetch metrics');
                return;
            }
            
            const data = await response.json();
            const metrics = data.metrics || [];
            
            // Find metrics for this server
            const serverMetric = metrics.find(m => m.server_id === this.serverId);
            if (!serverMetric) {
                console.warn(`MetricManager: No metrics found for server ${this.serverId}`);
                return;
            }
            
            // Update each metric component
            this._updateMetrics(serverMetric);
            
        } catch (error) {
            console.error(`MetricManager: Error fetching metrics for server ${this.serverId}:`, error);
            this._showErrorsOnAll('Network error');
        } finally {
            this.isRefreshing = false;
        }
    }
    
    /**
     * Update all metric components with data
     */
    _updateMetrics(metricData) {
        // Update CPU
        if (this.metrics.cpu) {
            this.metrics.cpu.update({
                cpu_percent: metricData.cpu_percent,
                cpu_count: metricData.cpu_count,
                cpu_load_avg_1m: metricData.cpu_load_avg_1m,
                top_processes: metricData.top_processes
            });
        }
        
        // Update Memory
        if (this.metrics.memory) {
            this.metrics.memory.update({
                memory_percent: metricData.memory_percent,
                memory_total: metricData.memory_total,
                memory_available: metricData.memory_available,
                memory_cached: metricData.memory_cached,
                swap_total: metricData.swap_total,
                swap_used: metricData.swap_used,
                top_processes: metricData.top_processes
            });
        }
        
        // Update Disk
        if (this.metrics.disk) {
            this.metrics.disk.update({
                disk_io_read: metricData.disk_io_read,
                disk_io_write: metricData.disk_io_write,
                disks: metricData.disk_usage || []
            });
        }
        
        // Update Network
        if (this.metrics.network) {
            this.metrics.network.update({
                net_io_sent: metricData.net_io_sent,
                net_io_recv: metricData.net_io_recv,
                network_utilization: metricData.network_utilization
            });
        }
        
        // Update Uptime
        if (this.metrics.uptime) {
            this.metrics.uptime.update({
                uptime_formatted: metricData.uptime_formatted,
                system_uptime_seconds: metricData.system_uptime_seconds
            });
        }
    }
    
    /**
     * Show error on all metric components
     */
    _showErrorsOnAll(message) {
        Object.values(this.metrics).forEach(metric => {
            if (metric && typeof metric.showError === 'function') {
                metric.showError(message);
            }
        });
    }
    
    /**
     * Start auto-refresh interval
     */
    startAutoRefresh(intervalMs = 10000) {
        this.refreshIntervalMs = intervalMs;
        this.stopAutoRefresh(); // Clear any existing interval
        
        // Initial fetch
        this.fetchAndUpdate();
        
        // Set up interval
        this.refreshInterval = setInterval(() => {
            this.fetchAndUpdate();
        }, this.refreshIntervalMs);
    }
    
    /**
     * Stop auto-refresh interval
     */
    stopAutoRefresh() {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
            this.refreshInterval = null;
        }
    }
    
    /**
     * Manually update a specific metric
     */
    updateMetric(metricType, data) {
        if (this.metrics[metricType]) {
            this.metrics[metricType].update(data);
        }
    }
    
    /**
     * Get a specific metric component
     */
    getMetric(metricType) {
        return this.metrics[metricType] || null;
    }
    
    /**
     * Destroy manager and clean up
     */
    destroy() {
        this.stopAutoRefresh();
        this.metrics = {};
    }
}




