/**
 * AlertTimeline - Recent alert timeline component
 */
class AlertTimeline extends BaseDashboardComponent {
    constructor() {
        super('alert-timeline', '/api/dashboard/recent-alerts/');
    }
    
    render(data) {
        if (!data || !Array.isArray(data)) {
            this.showError('No data available');
            return;
        }
        
        const listEl = document.getElementById('alert-timeline-list');
        if (!listEl) return;
        
        if (data.length === 0) {
            listEl.innerHTML = '<div style="padding: var(--cds-spacing-4, 16px); text-align: center; color: var(--cds-color-gray-70, #64748b);">No recent alerts</div>';
            return;
        }
        
        let html = '';
        data.forEach(alert => {
            const severityClass = alert.severity || 'warning';
            html += `
                <div class="alert-timeline-item">
                    <span class="alert-indicator ${severityClass}"></span>
                    <div class="alert-content">
                        <div class="alert-title">${this.escapeHtml(alert.title)}</div>
                        <div class="alert-host">${this.escapeHtml(alert.host)}</div>
                        <div class="alert-description">${this.escapeHtml(alert.description)}</div>
                        <div class="alert-time">${alert.time_ago}</div>
                    </div>
                    <span class="alert-severity-tag">
                        <span class="status-tag ${severityClass}">${severityClass}</span>
                    </span>
                </div>
            `;
        });
        
        listEl.innerHTML = html;
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}
