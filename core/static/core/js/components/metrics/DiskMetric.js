/**
 * DiskMetric - Disk metric component
 * Extends BaseMetric to handle Disk-specific rendering and updates
 */
class DiskMetric extends BaseMetric {
    constructor(serverId, elementId, options = {}) {
        super(serverId, elementId, options);
        this.metricType = 'disk';
    }
    
    getMetricType() {
        return 'disk';
    }
    
    _validateData(data) {
        return super._validateData(data);
    }
    
    _getDefaultValue() {
        return '--';
    }
    
    _renderSpecific(data) {
        const isCompact = this.options.compactMode || this.element?.classList.contains('compact');
        
        if (isCompact) {
            const mainElement = document.getElementById(`disk-main-${this.serverId}`);
            if (mainElement) {
                const diskIo = data.disk_io_read !== null && data.disk_io_read !== undefined
                    ? Math.round(data.disk_io_read)
                    : '--';
                mainElement.textContent = `${diskIo} KB/s`;
            }
        } else {
            this._renderDiskList(data.disks || []);
        }
    }
    
    _renderDiskList(disks) {
        const diskListContainer = document.getElementById(`disk-list-${this.serverId}`);
        if (!diskListContainer) return;
        
        if (!disks || disks.length === 0) {
            diskListContainer.innerHTML = '<div class="metric-process-item">No disk data available</div>';
            return;
        }
        
        let html = '';
        disks.forEach(disk => {
            const mountPoint = disk.mount_point || 'Unknown';
            const percent = disk.percent !== null && disk.percent !== undefined ? parseFloat(disk.percent).toFixed(1) : '0';
            const used = disk.used ? this.formatFileSize(disk.used) : '0 B';
            const total = disk.total ? this.formatFileSize(disk.total) : '0 B';
            const isRoot = mountPoint === '/';
            
            html += `
                <div style="padding: var(--cds-spacing-3, 12px); background-color: var(--cds-color-gray-10, #f8fafc); border-radius: var(--cds-border-radius, 6px); margin-bottom: var(--cds-spacing-2, 8px);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--cds-spacing-2, 8px);">
                        <div style="font-weight: var(--cds-font-weight-medium, 500); color: var(--cds-color-gray-100, #0f172a);">
                            ${mountPoint}${isRoot ? ' (Root)' : ''}
                        </div>
                        <div style="font-size: var(--cds-font-size-sm, 14px); color: var(--cds-color-gray-70, #64748b);">
                            ${percent}%
                        </div>
                    </div>
                    <div style="height: 8px; background-color: var(--cds-color-gray-20, #e2e8f0); border-radius: 4px; overflow: hidden;">
                        <div style="height: 100%; background-color: var(--cds-color-blue-60, #0ea5e9); width: ${percent}%; transition: width 0.3s ease;"></div>
                    </div>
                    <div style="font-size: var(--cds-font-size-xs, 12px); color: var(--cds-color-gray-70, #64748b); margin-top: var(--cds-spacing-1, 4px);">
                        ${used} / ${total}
                    </div>
                </div>
            `;
        });
        
        diskListContainer.innerHTML = html;
    }
}




