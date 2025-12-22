/**
 * NetworkMetric - Network metric component
 * Extends BaseMetric to handle Network-specific rendering and updates
 */
class NetworkMetric extends BaseMetric {
    constructor(serverId, elementId, options = {}) {
        super(serverId, elementId, options);
        this.metricType = 'network';
    }
    
    getMetricType() {
        return 'network';
    }
    
    _validateData(data) {
        return super._validateData(data);
    }
    
    _getDefaultValue() {
        return '-- KB/s';
    }
    
    _renderSpecific(data) {
        const isCompact = this.options.compactMode || this.element?.classList.contains('compact');
        
        if (isCompact) {
            const mainElement = document.getElementById(`network-main-${this.serverId}`);
            if (mainElement) {
                const netIo = data.net_io_sent !== null && data.net_io_sent !== undefined
                    ? Math.round(data.net_io_sent)
                    : '--';
                mainElement.textContent = `${netIo} KB/s`;
            }
        } else {
            const sentElement = document.getElementById(`network-sent-${this.serverId}`);
            if (sentElement) {
                const sent = data.net_io_sent !== null && data.net_io_sent !== undefined
                    ? Math.round(data.net_io_sent)
                    : '--';
                sentElement.textContent = `${sent} KB/s`;
            }
            
            const recvElement = document.getElementById(`network-recv-${this.serverId}`);
            if (recvElement) {
                const recv = data.net_io_recv !== null && data.net_io_recv !== undefined
                    ? Math.round(data.net_io_recv)
                    : '--';
                recvElement.textContent = `${recv} KB/s`;
            }
        }
    }
}




