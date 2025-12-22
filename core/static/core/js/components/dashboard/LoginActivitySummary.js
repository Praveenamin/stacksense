/**
 * LoginActivitySummary - Login activity summary component
 */
class LoginActivitySummary extends BaseDashboardComponent {
    constructor() {
        super('login-activity', '/api/dashboard/login-activity/');
    }
    
    render(data) {
        if (!data || !Array.isArray(data)) {
            this.showError('No data available');
            return;
        }
        
        const listEl = document.getElementById('login-activity-list');
        if (!listEl) return;
        
        if (data.length === 0) {
            listEl.innerHTML = '<div style="padding: var(--cds-spacing-4, 16px); text-align: center; color: var(--cds-color-gray-70, #64748b);">No login activity</div>';
            return;
        }
        
        let html = '';
        data.forEach(activity => {
            const statusClass = activity.status || 'success';
            html += `
                <div class="login-activity-item">
                    <span class="login-indicator ${statusClass}"></span>
                    <div class="login-content">
                        <div class="login-email">${this.escapeHtml(activity.email)}</div>
                        <div class="login-location">${this.escapeHtml(activity.location)}</div>
                        <div class="login-time">${activity.time_ago}</div>
                    </div>
                    <span class="login-status-tag">
                        <span class="status-tag ${statusClass}">${statusClass}</span>
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
