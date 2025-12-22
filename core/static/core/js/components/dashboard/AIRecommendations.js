/**
 * AIRecommendations - AI recommendations component
 */
class AIRecommendations extends BaseDashboardComponent {
    constructor() {
        super('ai-recommendations', '/api/dashboard/ai-recommendations/');
    }
    render(data) {
        if (!data || !Array.isArray(data)) { this.showError('No recommendations available'); return; }
        const listEl = document.getElementById('recommendations-list');
        if (!listEl) return;
        if (data.length === 0) {
            listEl.innerHTML = '<div style="padding: var(--cds-spacing-4, 16px); text-align: center; color: var(--cds-color-gray-70, #64748b);">No recommendations at this time</div>';
            return;
        }
        let html = '';
        data.forEach(recommendation => {
            const priority = recommendation.priority || 'medium';
            html += `<div class="recommendation-item ${priority}">
                <div class="recommendation-header">
                    <div></div>
                    <span class="status-tag ${priority}">${priority}</span>
                </div>
                <div class="recommendation-description">${this.escapeHtml(recommendation.description)}</div>
                ${recommendation.impact ? `<div class="recommendation-impact"><span>💰</span><span>${this.escapeHtml(recommendation.impact)}</span></div>` : ''}
                ${recommendation.action_label ? `<button class="recommendation-action" onclick="${recommendation.action_handler || '() => {}'}">${this.escapeHtml(recommendation.action_label)}</button>` : ''}
            </div>`;
        });
        listEl.innerHTML = html;
    }
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}
