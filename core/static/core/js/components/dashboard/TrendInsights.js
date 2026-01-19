/**
 * Trend Insights Component
 * Displays detected recurring alert patterns on the dashboard
 */

class TrendInsights {
    constructor() {
        this.containerId = 'trend-insights-content';
        this.container = null;
        this.countBadge = null;
        this.refreshButton = null;
        this.updateInterval = null;
        this.updateIntervalMs = 300000; // Update every 5 minutes
        this.isLoading = false;
    }

    init() {
        this.container = document.getElementById(this.containerId);
        this.countBadge = document.getElementById('trend-insights-count');
        this.refreshButton = document.getElementById('trend-insights-refresh');
        
        if (!this.container) {
            console.error('TrendInsights: Container not found');
            return;
        }
        
        this.setupEventListeners();
        this.fetchAndRender();
        this.startAutoUpdate();
    }

    setupEventListeners() {
        if (this.refreshButton) {
            this.refreshButton.addEventListener('click', () => {
                this.fetchAndRender();
            });
        }
    }

    startAutoUpdate() {
        if (this.updateInterval) {
            clearInterval(this.updateInterval);
        }
        this.updateInterval = setInterval(() => {
            this.fetchAndRender();
        }, this.updateIntervalMs);
    }

    stopAutoUpdate() {
        if (this.updateInterval) {
            clearInterval(this.updateInterval);
            this.updateInterval = null;
        }
    }

    showLoading() {
        if (this.isLoading) return;
        this.isLoading = true;
        
        this.container.innerHTML = `
            <div class="trend-insights-loading" style="text-align: center; padding: 24px; color: var(--cds-color-gray-60, #64748b);">
                <div class="loading-spinner" style="width: 24px; height: 24px; border: 2px solid #e2e8f0; border-top-color: #3b82f6; border-radius: 50%; animation: spin 1s linear infinite; margin: 0 auto 12px;"></div>
                <span>Analyzing patterns...</span>
            </div>
        `;
    }

    async fetchAndRender() {
        this.showLoading();
        
        try {
            const response = await fetch('/api/dashboard/trend-insights/?lookback_days=30');
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            
            const data = await response.json();
            
            if (data.success) {
                this.render(data.data);
            } else {
                this.showError(data.error || 'Failed to load trend insights');
            }
        } catch (error) {
            console.error('TrendInsights: Fetch error:', error);
            this.showError('Failed to load trend insights');
        } finally {
            this.isLoading = false;
        }
    }

    render(data) {
        const { insights, total_patterns, servers_with_patterns, analysis_period_days } = data;
        
        // Update count badge
        if (this.countBadge) {
            this.countBadge.textContent = total_patterns > 0 
                ? `${total_patterns} pattern${total_patterns > 1 ? 's' : ''}`
                : 'No patterns';
            
            // Change badge color based on findings
            if (total_patterns > 0) {
                this.countBadge.style.background = '#fef3c7';
                this.countBadge.style.color = '#92400e';
            } else {
                this.countBadge.style.background = '#dcfce7';
                this.countBadge.style.color = '#166534';
            }
        }
        
        if (!insights || insights.length === 0) {
            this.showEmpty();
            return;
        }
        
        let html = '';
        
        // Summary bar if multiple patterns
        if (insights.length > 1) {
            html += `
                <div class="trend-insights-summary">
                    <div class="summary-stat">
                        <div class="summary-stat-value">${total_patterns}</div>
                        <div class="summary-stat-label">Patterns</div>
                    </div>
                    <div class="summary-stat">
                        <div class="summary-stat-value">${servers_with_patterns}</div>
                        <div class="summary-stat-label">Servers</div>
                    </div>
                    <div class="summary-stat">
                        <div class="summary-stat-value">${analysis_period_days}d</div>
                        <div class="summary-stat-label">Analysis</div>
                    </div>
                </div>
            `;
        }
        
        // Render each insight
        insights.forEach(insight => {
            html += this.renderInsightCard(insight);
        });
        
        this.container.innerHTML = html;
    }

    renderInsightCard(insight) {
        const {
            server_name,
            alert_type,
            pattern_type,
            pattern_description,
            confidence,
            peak_hour,
            peak_day,
            total_alerts,
            recommendation
        } = insight;
        
        // Determine confidence level for styling
        const isHighConfidence = confidence >= 60;
        const confidenceClass = isHighConfidence ? 'high-confidence' : 'medium-confidence';
        const confidenceFillClass = isHighConfidence ? 'high' : 'medium';
        
        // Get icon and badge class for alert type
        const typeConfig = this.getAlertTypeConfig(alert_type);
        
        // Format pattern time
        let patternTime = '';
        if (pattern_type === 'hourly' && peak_hour !== null) {
            patternTime = this.formatHour(peak_hour);
        } else if (pattern_type === 'weekly' && peak_day) {
            patternTime = peak_day + 's';
        }
        
        return `
            <div class="trend-insight-card ${confidenceClass}">
                <div class="trend-insight-header">
                    <div class="trend-insight-title">
                        <span class="trend-insight-icon">${typeConfig.icon}</span>
                        <span>${this.escapeHtml(server_name)}</span>
                    </div>
                    <span class="trend-insight-badge ${typeConfig.badgeClass}">${alert_type}</span>
                </div>
                
                <div class="trend-insight-pattern">
                    ${this.escapeHtml(pattern_description)}
                </div>
                
                <div class="trend-insight-confidence">
                    <div class="confidence-bar">
                        <div class="confidence-fill ${confidenceFillClass}" style="width: ${Math.min(confidence, 100)}%"></div>
                    </div>
                    <span class="confidence-text">${confidence.toFixed(0)}%</span>
                </div>
                
                <div class="trend-insight-recommendation">
                    <span class="recommendation-icon">💡</span>
                    <span>${this.escapeHtml(recommendation)}</span>
                </div>
            </div>
        `;
    }

    getAlertTypeConfig(alertType) {
        const configs = {
            'CPU': { icon: '🔥', badgeClass: 'badge-cpu' },
            'MEMORY': { icon: '💾', badgeClass: 'badge-memory' },
            'DISK': { icon: '💿', badgeClass: 'badge-disk' },
            'CONNECTION': { icon: '🔌', badgeClass: 'badge-cpu' },
            'SERVICE': { icon: '⚙️', badgeClass: 'badge-memory' }
        };
        return configs[alertType] || { icon: '⚠️', badgeClass: 'badge-cpu' };
    }

    formatHour(hour) {
        if (hour === 0) return '12 AM';
        if (hour === 12) return '12 PM';
        if (hour < 12) return `${hour} AM`;
        return `${hour - 12} PM`;
    }

    showEmpty() {
        this.container.innerHTML = `
            <div class="trend-insights-empty">
                <div class="trend-insights-empty-icon">✅</div>
                <div class="trend-insights-empty-text">
                    <strong>No recurring patterns detected</strong><br>
                    Your servers are showing healthy, non-repetitive alert behavior over the last 30 days.
                </div>
            </div>
        `;
    }

    showError(message) {
        this.container.innerHTML = `
            <div style="padding: 16px; background-color: #fee2e2; border: 1px solid #fca5a5; border-radius: 8px; color: #991b1b; text-align: center;">
                <span style="font-size: 18px; margin-right: 8px;">⚠️</span>
                ${this.escapeHtml(message)}
            </div>
        `;
    }

    escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    // Only initialize if on the monitoring dashboard
    if (document.getElementById('trend-insights-content')) {
        window.trendInsights = new TrendInsights();
        window.trendInsights.init();
    }
});
