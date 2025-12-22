/**
 * DashboardManager - Coordinates all dashboard components and handles refresh intervals
 */
class DashboardManager {
    constructor() {
        this.components = {};
        this.refreshInterval = null;
        this.refreshIntervalMs = 30000; // 30 seconds
    }
    
    /**
     * Initialize all dashboard components
     */
    init() {
        // Initialize all components
        this.components.summaryMetrics = new SummaryMetricsCard();
        this.components.alertsBanner = new AlertsBanner();
        this.components.cpuTrend = new CPUTrendChart();
        this.components.memoryTrend = new MemoryTrendChart();
        this.components.networkTraffic = new NetworkTrafficChart();
        this.components.diskIOSummary = new DiskIOSummary();
        
        // Initialize components with server selectors
        if (this.components.cpuTrend.init) this.components.cpuTrend.init();
        if (this.components.memoryTrend.init) this.components.memoryTrend.init();
        if (this.components.networkTraffic.init) this.components.networkTraffic.init();
        if (this.components.diskIOSummary.init) this.components.diskIOSummary.init();
        this.components.topCPUConsumers = new TopCPUConsumers();
        this.components.topMemoryConsumers = new TopMemoryConsumers();
        this.components.healthStatus = new HealthStatusChart();
        this.components.alertTimeline = new AlertTimeline();
        this.components.aiRecommendations = new AIRecommendations();
        this.components.diskForecast = new DiskForecast();
        this.components.agentVersions = new AgentVersionSummary();
        this.components.loginActivity = new LoginActivitySummary();
        
        // Initialize DiskForecast with server selector
        if (this.components.diskForecast.init) {
            this.components.diskForecast.init();
        }
        
        // Start auto-refresh for all components
        this.startAutoRefresh();
    }
    
    /**
     * Start auto-refresh for all components
     */
    startAutoRefresh() {
        this.stopAutoRefresh(); // Clear any existing interval
        
        // Initial fetch for all components
        Object.values(this.components).forEach(component => {
            if (component.startAutoRefresh) {
                component.startAutoRefresh();
            } else {
                component.fetchData();
            }
        });
        
        // Set up global refresh interval (components may have their own intervals)
        this.refreshInterval = setInterval(() => {
            Object.values(this.components).forEach(component => {
                if (component.fetchData && !component.refreshInterval) {
                    component.fetchData();
                }
            });
        }, this.refreshIntervalMs);
    }
    
    /**
     * Stop auto-refresh
     */
    stopAutoRefresh() {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
            this.refreshInterval = null;
        }
        
        Object.values(this.components).forEach(component => {
            if (component.stopAutoRefresh) {
                component.stopAutoRefresh();
            }
        });
    }
    
    /**
     * Get a specific component
     */
    getComponent(name) {
        return this.components[name] || null;
    }
    
    /**
     * Destroy all components and clean up
     */
    destroy() {
        this.stopAutoRefresh();
        Object.values(this.components).forEach(component => {
            if (component.destroy) {
                component.destroy();
            }
        });
        this.components = {};
    }
}

