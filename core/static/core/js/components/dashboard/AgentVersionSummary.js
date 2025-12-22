/**
 * AgentVersionSummary - Agent version distribution component
 */
class AgentVersionSummary extends BaseDashboardComponent {
    constructor() {
        super('agent-versions', '/api/dashboard/agent-versions/');
    }
    
    render(data) {
        if (!data || !data.versions || !Array.isArray(data.versions)) {
            this.showError('No data available');
            return;
        }
        
        const listEl = document.getElementById('agent-versions-list');
        if (!listEl) return;
        
        if (data.versions.length === 0) {
            listEl.innerHTML = '<div style="padding: var(--cds-spacing-4, 16px); text-align: center; color: var(--cds-color-gray-70, #64748b);">No agent version data</div>';
            return;
        }
        
        let html = '';
        data.versions.forEach(version => {
            html += `
                <div class="version-distribution-item">
                    <div class="version-label">${this.escapeHtml(version.version)}</div>
                    <div class="version-bar-container">
                        <div class="version-bar" style="width: ${version.percentage}%;"></div>
                    </div>
                    <div class="version-stats">${version.count} VMs (${version.percentage}%)</div>
                </div>
            `;
        });
        
        listEl.innerHTML = html;
        
        // Update summary box
        const summaryEl = document.getElementById('agent-version-summary');
        const summaryTextEl = document.getElementById('agent-version-summary-text');
        if (summaryEl && summaryTextEl && data.latest_version && data.latest_percentage) {
            summaryTextEl.textContent = `${data.latest_percentage}% of agents are on the latest version`;
            summaryEl.style.display = 'flex';
        }
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}
