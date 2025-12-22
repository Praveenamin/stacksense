/**
 * TopMemoryConsumers - Top 5 memory consumers component
 */
class TopMemoryConsumers extends BaseDashboardComponent {
    constructor() {
        super('top-memory-consumers', '/api/dashboard/top-memory-consumers/');
    }
    render(data) {
        if (!data || !Array.isArray(data)) { this.showError('No data available'); return; }
        const listEl = document.getElementById('top-memory-consumers-list');
        if (!listEl) return;
        if (data.length === 0) {
            listEl.innerHTML = '<li style="padding: var(--cds-spacing-4, 16px); text-align: center; color: var(--cds-color-gray-70, #64748b);">No data available</li>';
            return;
        }
        let html = '';
        data.forEach((item, index) => {
            const rank = index + 1;
            const statusClass = item.status_tag || 'normal';
            html += `<li class="top-consumer-item">
                <div class="top-consumer-rank">#${rank}</div>
                <div class="top-consumer-info">
                    <div class="top-consumer-name">${this.escapeHtml(item.server_name)}</div>
                    <span class="status-tag ${statusClass}">${statusClass}</span>
                </div>
                <div class="top-consumer-value">${item.memory_percent}%</div>
                <div class="progress-bar-container" style="width: 100px;">
                    <div class="progress-bar ${statusClass}" style="width: ${item.memory_percent}%;"></div>
                </div>
            </li>`;
        });
        listEl.innerHTML = html;
    }
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}
