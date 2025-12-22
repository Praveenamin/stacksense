/**
 * CPUMetric - CPU metric component
 * Extends BaseMetric to handle CPU-specific rendering and updates
 */
class CPUMetric extends BaseMetric {
    constructor(serverId, elementId, options = {}) {
        super(serverId, elementId, options);
        this.metricType = 'cpu';
    }
    
    getMetricType() {
        return 'cpu';
    }
    
    _validateData(data) {
        return super._validateData(data) && (data.cpu_percent !== undefined || data.cpu_count !== undefined);
    }
    
    _getDefaultValue() {
        return '--%';
    }
    
    _renderSpecific(data) {
        const isCompact = this.options.compactMode || this.element?.classList.contains('compact');
        
        if (isCompact) {
            // Compact mode - just show main CPU percentage
            const mainElement = document.getElementById(`cpu-main-${this.serverId}`);
            if (mainElement) {
                const cpuPercent = data.cpu_percent !== null && data.cpu_percent !== undefined 
                    ? Math.round(data.cpu_percent) 
                    : '--';
                mainElement.textContent = `${cpuPercent}%`;
            }
        } else {
            // Full mode - show all CPU metrics
            const percentElement = document.getElementById(`cpu-percent-${this.serverId}`);
            if (percentElement) {
                const cpuPercent = data.cpu_percent !== null && data.cpu_percent !== undefined
                    ? parseFloat(data.cpu_percent).toFixed(1)
                    : '--';
                percentElement.textContent = `${cpuPercent}%`;
            }
            
            const coresElement = document.getElementById(`cpu-cores-${this.serverId}`);
            if (coresElement) {
                coresElement.textContent = data.cpu_count !== null && data.cpu_count !== undefined
                    ? data.cpu_count.toString()
                    : '--';
            }
            
            const loadAvgElement = document.getElementById(`cpu-load-avg-${this.serverId}`);
            if (loadAvgElement) {
                loadAvgElement.textContent = data.cpu_load_avg_1m !== null && data.cpu_load_avg_1m !== undefined
                    ? parseFloat(data.cpu_load_avg_1m).toFixed(2)
                    : '--';
            }
            
            // Update top processes
            this._renderTopProcesses(data.top_processes);
        }
    }
    
    _renderTopProcesses(processes) {
        const processesContainer = document.getElementById(`top-cpu-processes-${this.serverId}`);
        if (!processesContainer) return;
        
        if (!processes || processes.length === 0) {
            processesContainer.innerHTML = '<div class="metric-process-item">No process data available</div>';
            return;
        }
        
        // Show top 3 processes
        const topProcesses = processes.slice(0, 3);
        let html = '';
        
        topProcesses.forEach(process => {
            const name = (process.name || 'Unknown').substring(0, 40);
            const cpu = process.cpu !== null && process.cpu !== undefined
                ? parseFloat(process.cpu).toFixed(1)
                : '0.0';
            html += `
                <div class="metric-process-item">
                    <span class="metric-process-name">${name}</span>
                    <span class="metric-process-value">${cpu}%</span>
                </div>
            `;
        });
        
        processesContainer.innerHTML = html;
    }
}
