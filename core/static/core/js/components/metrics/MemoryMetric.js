/**
 * MemoryMetric - Memory metric component
 * Extends BaseMetric to handle Memory-specific rendering and updates
 */
class MemoryMetric extends BaseMetric {
    constructor(serverId, elementId, options = {}) {
        super(serverId, elementId, options);
        this.metricType = 'memory';
    }
    
    getMetricType() {
        return 'memory';
    }
    
    _validateData(data) {
        return super._validateData(data) && (data.memory_percent !== undefined || data.memory_total !== undefined);
    }
    
    _getDefaultValue() {
        return '--%';
    }
    
    _renderSpecific(data) {
        const isCompact = this.options.compactMode || this.element?.classList.contains('compact');
        
        if (isCompact) {
            const mainElement = document.getElementById(`memory-main-${this.serverId}`);
            if (mainElement) {
                const memoryPercent = data.memory_percent !== null && data.memory_percent !== undefined 
                    ? Math.round(data.memory_percent) 
                    : '--';
                mainElement.textContent = `${memoryPercent}%`;
            }
        } else {
            const percentElement = document.getElementById(`memory-percent-${this.serverId}`);
            if (percentElement && data.memory_percent !== undefined) {
                percentElement.textContent = this.formatValue(data.memory_percent, 'percent');
            }
            
            const totalElement = document.getElementById(`ram-total-${this.serverId}`);
            if (totalElement) {
                totalElement.textContent = data.memory_total !== null && data.memory_total !== undefined
                    ? this.formatFileSize(data.memory_total)
                    : '--';
            }
            
            const freeElement = document.getElementById(`ram-free-${this.serverId}`);
            if (freeElement) {
                freeElement.textContent = data.memory_available !== null && data.memory_available !== undefined
                    ? this.formatFileSize(data.memory_available)
                    : '--';
            }
            
            const cachedElement = document.getElementById(`ram-cached-${this.serverId}`);
            if (cachedElement) {
                cachedElement.textContent = data.memory_cached !== null && data.memory_cached !== undefined
                    ? this.formatFileSize(data.memory_cached)
                    : '--';
            }
            
            const swapElement = document.getElementById(`swap-info-${this.serverId}`);
            if (swapElement && data.swap_total) {
                const swapTotal = this.formatFileSize(data.swap_total);
                const swapUsed = data.swap_used ? this.formatFileSize(data.swap_used) : '0 B';
                swapElement.textContent = `${swapTotal} / ${swapUsed}`;
            }
            
            this._renderTopProcesses(data.top_processes);
        }
    }
    
    _renderTopProcesses(processes) {
        const processesContainer = document.getElementById(`top-ram-processes-${this.serverId}`);
        if (!processesContainer) return;
        
        if (!processes || processes.length === 0) {
            processesContainer.innerHTML = '<div class="metric-process-item">No process data available</div>';
            return;
        }
        
        const topProcesses = processes.slice(0, 3);
        let html = '';
        
        topProcesses.forEach(process => {
            const name = (process.name || 'Unknown').substring(0, 40);
            const memory = process.memory !== null && process.memory !== undefined
                ? this.formatFileSize(process.memory)
                : '0 B';
            html += `
                <div class="metric-process-item">
                    <span class="metric-process-name">${name}</span>
                    <span class="metric-process-value">${memory}</span>
                </div>
            `;
        });
        
        processesContainer.innerHTML = html;
    }
}
