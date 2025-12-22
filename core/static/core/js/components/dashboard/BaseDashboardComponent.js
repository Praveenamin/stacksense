/**
 * BaseDashboardComponent - Base class for all dashboard components
 */
class BaseDashboardComponent {
    constructor(componentId, apiEndpoint, options = {}) {
        this.componentId = componentId;
        this.apiEndpoint = apiEndpoint;
        this.options = { autoRefresh: true, refreshInterval: 30000, ...options };
        this.element = document.getElementById(`dashboard-${componentId}`);
        this.contentElement = document.getElementById(`dashboard-content-${componentId}`);
        this.loadingElement = document.getElementById(`dashboard-loading-${componentId}`);
        this.errorElement = document.getElementById(`dashboard-error-${componentId}`);
        this.errorMessageElement = document.getElementById(`dashboard-error-message-${componentId}`);
        this.refreshInterval = null;
        this.isLoading = false;
    }
    showLoading() {
        if (this.loadingElement) this.loadingElement.style.display = 'block';
        if (this.contentElement) this.contentElement.style.opacity = '0.5';
        if (this.errorElement) this.errorElement.style.display = 'none';
        this.isLoading = true;
    }
    hideLoading() {
        if (this.loadingElement) this.loadingElement.style.display = 'none';
        if (this.contentElement) this.contentElement.style.opacity = '1';
        this.isLoading = false;
    }
    showError(message = 'An error occurred') {
        if (this.errorElement) this.errorElement.style.display = 'block';
        if (this.errorMessageElement) this.errorMessageElement.textContent = message;
        if (this.loadingElement) this.loadingElement.style.display = 'none';
        if (this.contentElement) this.contentElement.style.opacity = '0.5';
    }
    hideError() {
        if (this.errorElement) this.errorElement.style.display = 'none';
        if (this.contentElement) this.contentElement.style.opacity = '1';
    }
    async fetchData() {
        if (this.isLoading || !this.apiEndpoint) return;
        this.showLoading();
        this.hideError();
        try {
            const response = await fetch(this.apiEndpoint);
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
            const data = await response.json();
            if (!data.success) throw new Error(data.error || 'API request failed');
            this.hideLoading();
            this.render(data.data);
            return data.data;
        } catch (error) {
            console.error(`BaseDashboardComponent.fetchData error for ${this.componentId}:`, error);
            this.hideLoading();
            this.showError(error.message || 'Failed to load data');
            return null;
        }
    }
    render(data) {
        throw new Error('render method must be implemented by child class');
    }
    startAutoRefresh() {
        if (!this.options.autoRefresh) return;
        this.stopAutoRefresh();
        this.fetchData();
        this.refreshInterval = setInterval(() => { this.fetchData(); }, this.options.refreshInterval);
    }
    stopAutoRefresh() {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
            this.refreshInterval = null;
        }
    }
    destroy() {
        this.stopAutoRefresh();
    }
}
