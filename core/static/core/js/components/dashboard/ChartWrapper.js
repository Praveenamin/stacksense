/**
 * ChartWrapper - Utility class for creating Chart.js instances
 */
class ChartWrapper {
    static createLineChart(canvasId, data, options = {}) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) { console.error(`ChartWrapper: Canvas "${canvasId}" not found`); return null; }
        const ctx = canvas.getContext('2d');
        const defaultOptions = {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: true, position: 'top' }, tooltip: { mode: 'index', intersect: false } },
            scales: { x: { display: true, grid: { display: false } }, y: { beginAtZero: true, grid: { color: 'rgba(0, 0, 0, 0.05)' } } },
            elements: { point: { radius: 3, hoverRadius: 5 }, line: { tension: 0.4 } }
        };
        const mergedOptions = this.mergeOptions(defaultOptions, options);
        return new Chart(ctx, { type: 'line', data: data, options: mergedOptions });
    }
    static createBarChart(canvasId, data, options = {}) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) { console.error(`ChartWrapper: Canvas "${canvasId}" not found`); return null; }
        const ctx = canvas.getContext('2d');
        const defaultOptions = {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
            scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid: { color: 'rgba(0, 0, 0, 0.05)' } } }
        };
        const mergedOptions = this.mergeOptions(defaultOptions, options);
        return new Chart(ctx, { type: 'bar', data: data, options: mergedOptions });
    }
    static createDoughnutChart(canvasId, data, options = {}) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) { console.error(`ChartWrapper: Canvas "${canvasId}" not found`); return null; }
        const ctx = canvas.getContext('2d');
        const defaultOptions = {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false }, tooltip: { callbacks: { label: function(context) { const label = context.label || ''; const value = context.parsed || 0; const total = context.dataset.data.reduce((a, b) => a + b, 0); const percentage = total > 0 ? ((value / total) * 100).toFixed(1) : 0; return `${label}: ${value} (${percentage}%)`; } } } },
            cutout: '60%'
        };
        const mergedOptions = this.mergeOptions(defaultOptions, options);
        return new Chart(ctx, { type: 'doughnut', data: data, options: mergedOptions });
    }
    static createStackedAreaChart(canvasId, data, options = {}) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) { console.error(`ChartWrapper: Canvas "${canvasId}" not found`); return null; }
        const ctx = canvas.getContext('2d');
        if (data.datasets) {
            data.datasets = data.datasets.map(dataset => ({ ...dataset, fill: true, tension: 0.4 }));
        }
        const defaultOptions = {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: { legend: { display: true, position: 'top' }, tooltip: { mode: 'index', intersect: false } },
            scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, grid: { color: 'rgba(0, 0, 0, 0.05)' } } },
            elements: { point: { radius: 0, hoverRadius: 4 } }
        };
        const mergedOptions = this.mergeOptions(defaultOptions, options);
        return new Chart(ctx, { type: 'line', data: data, options: mergedOptions });
    }
    static updateChart(chart, newData) {
        if (!chart) return;
        if (newData.labels) chart.data.labels = newData.labels;
        if (newData.datasets) chart.data.datasets = newData.datasets;
        chart.update();
    }
    static destroyChart(chart) {
        if (chart) chart.destroy();
    }
    static mergeOptions(defaultOptions, customOptions) {
        const merged = JSON.parse(JSON.stringify(defaultOptions));
        for (const key in customOptions) {
            if (customOptions[key] && typeof customOptions[key] === 'object' && !Array.isArray(customOptions[key])) {
                merged[key] = this.mergeOptions(merged[key] || {}, customOptions[key]);
            } else {
                merged[key] = customOptions[key];
            }
        }
        return merged;
    }
}
