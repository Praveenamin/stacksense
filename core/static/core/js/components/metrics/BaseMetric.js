/**
 * BaseMetric - Base class for all metric components
 * Provides common functionality for rendering, updating, and error handling
 */
class BaseMetric {
    constructor(serverId, elementId, options = {}) {
        this.serverId = serverId;
        this.elementId = elementId;
        this.options = {
            compactMode: false,
            showHeader: true,
            autoFormat: true,
            ...options
        };
        
        this.element = document.getElementById(elementId);
        if (!this.element) {
            console.error(`BaseMetric: Element with ID "${elementId}" not found`);
            return;
        }
        
        this.loadingElement = document.getElementById(`metric-loading-${this.getMetricType()}-${serverId}`);
        this.errorElement = document.getElementById(`metric-error-${this.getMetricType()}-${serverId}`);
        this.errorMessageElement = document.getElementById(`metric-error-message-${this.getMetricType()}-${serverId}`);
    }
    
    /**
     * Get the metric type identifier
     * Should be overridden by child classes
     */
    getMetricType() {
        return this.element?.dataset?.metricType || 'unknown';
    }
    
    /**
     * Get the DOM element
     */
    getElement() {
        return this.element;
    }
    
    /**
     * Show loading state
     */
    showLoading() {
        if (this.loadingElement) {
            this.loadingElement.style.display = 'block';
        }
        if (this.errorElement) {
            this.errorElement.style.display = 'none';
        }
        if (this.element) {
            const content = this.element.querySelector('.metric-content');
            if (content) {
                content.style.opacity = '0.5';
            }
        }
    }
    
    /**
     * Hide loading state
     */
    hideLoading() {
        if (this.loadingElement) {
            this.loadingElement.style.display = 'none';
        }
        if (this.element) {
            const content = this.element.querySelector('.metric-content');
            if (content) {
                content.style.opacity = '1';
            }
        }
    }
    
    /**
     * Show error state
     */
    showError(message = 'An error occurred') {
        if (this.errorElement) {
            this.errorElement.style.display = 'block';
        }
        if (this.errorMessageElement) {
            this.errorMessageElement.textContent = message;
        }
        if (this.loadingElement) {
            this.loadingElement.style.display = 'none';
        }
        if (this.element) {
            const content = this.element.querySelector('.metric-content');
            if (content) {
                content.style.opacity = '0.5';
            }
        }
    }
    
    /**
     * Hide error state
     */
    hideError() {
        if (this.errorElement) {
            this.errorElement.style.display = 'none';
        }
    }
    
    /**
     * Format metric values based on type
     */
    formatValue(value, type = 'number') {
        if (value === null || value === undefined || value === '') {
            return '--';
        }
        
        switch (type) {
            case 'percent':
                return `${Math.round(value)}%`;
            case 'decimal':
                return parseFloat(value).toFixed(2);
            case 'integer':
                return Math.round(value).toString();
            case 'filesize':
                return this.formatFileSize(value);
            case 'bytes':
                return this.formatBytes(value);
            case 'time':
                return this.formatTime(value);
            default:
                return value.toString();
        }
    }
    
    /**
     * Format file size
     */
    formatFileSize(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
    }
    
    /**
     * Format bytes (for network/disk I/O)
     */
    formatBytes(bytes) {
        if (bytes === 0) return '0 B/s';
        const k = 1024;
        const sizes = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
    }
    
    /**
     * Format time (for uptime)
     */
    formatTime(seconds) {
        if (!seconds) return '--';
        
        const days = Math.floor(seconds / 86400);
        const hours = Math.floor((seconds % 86400) / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        
        if (days > 0) {
            return `${days}d ${hours}h ${minutes}m`;
        } else if (hours > 0) {
            return `${hours}h ${minutes}m`;
        } else {
            return `${minutes}m`;
        }
    }
    
    /**
     * Validate data structure
     * Should be overridden by child classes
     */
    _validateData(data) {
        return data !== null && data !== undefined;
    }
    
    /**
     * Get default value when data is missing
     * Should be overridden by child classes
     */
    _getDefaultValue() {
        return '--';
    }
    
    /**
     * Render component-specific content
     * Must be implemented by child classes
     */
    _renderSpecific(data) {
        throw new Error('_renderSpecific must be implemented by child class');
    }
    
    /**
     * Public method to update metric
     */
    update(data) {
        if (!this.element) {
            return;
        }
        
        this.hideError();
        this.hideLoading();
        
        if (!this._validateData(data)) {
            this.showError('Invalid data');
            return;
        }
        
        try {
            this._renderSpecific(data);
        } catch (error) {
            console.error(`BaseMetric.update error for ${this.getMetricType()}:`, error);
            this.showError('Failed to render metric');
        }
    }
    
    /**
     * Render with initial data
     */
    render(data) {
        this.update(data);
    }
}
