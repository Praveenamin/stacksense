/**
 * ExecutiveCharts — renders the Resource Utilization Trend and Capacity
 * Forecast charts from JSON embedded by the server (json_script), and wires the
 * 7/30/90 and 30/60/90 toggles. Plain Chart.js (v4) — no external deps.
 */
(function () {
    function readJSON(id) {
        const el = document.getElementById(id);
        if (!el) return null;
        try { return JSON.parse(el.textContent); } catch (e) { return null; }
    }

    function lineChart(canvasId, series) {
        const ctx = document.getElementById(canvasId);
        if (!ctx || typeof Chart === 'undefined' || !series) return null;
        return new Chart(ctx, {
            type: 'line',
            data: {
                labels: series.labels,
                datasets: [
                    { label: 'CPU %', data: series.cpu, borderColor: '#6366f1',
                      backgroundColor: 'rgba(99,102,241,.10)', fill: true, tension: .35, pointRadius: 0, borderWidth: 2 },
                    { label: 'Memory %', data: series.mem, borderColor: '#14b8a6',
                      backgroundColor: 'rgba(20,184,166,.10)', fill: true, tension: .35, pointRadius: 0, borderWidth: 2 },
                ],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } } },
                scales: {
                    x: { grid: { display: false }, ticks: { maxTicksLimit: 8, font: { size: 10 } } },
                    y: { min: 0, max: 100, ticks: { stepSize: 20, callback: v => v + '%', font: { size: 10 } } },
                },
            },
        });
    }

    function setupToggle(attr, data, chart, defaultKey) {
        const buttons = document.querySelectorAll('[' + attr + ']');
        buttons.forEach(btn => btn.addEventListener('click', () => {
            const key = btn.getAttribute(attr);
            const s = data[key];
            if (!s || !chart) return;
            buttons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            chart.data.labels = s.labels;
            chart.data.datasets[0].data = s.cpu;
            chart.data.datasets[1].data = s.mem;
            chart.update();
        }));
    }

    document.addEventListener('DOMContentLoaded', function () {
        const trend = readJSON('exec-trend-data');
        if (trend) {
            const c = lineChart('exec-trend-chart', trend['7'] || Object.values(trend)[0]);
            setupToggle('data-trend-period', trend, c, '7');
        }
        const forecast = readJSON('exec-forecast-data');
        if (forecast) {
            const c = lineChart('exec-forecast-chart', forecast['30'] || Object.values(forecast)[0]);
            setupToggle('data-forecast-period', forecast, c, '30');
        }
    });
})();
