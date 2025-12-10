/**
 * Anomaly History Chart
 * 
 * Renders Chart.js line chart showing CPU, memory, and disk metrics
 * with anomaly events overlaid as scatter points.
 * 
 * Performance optimized for small deployments (4 CPU / 8 GB RAM).
 */

(function() {
    'use strict';

    let chartInstance = null;

    /**
     * Fetch metric history from API
     * @param {number} serverId - Server ID
     * @param {string} range - Time range (1h, 7d, 1m, 3m)
     * @returns {Promise<Object|null>} - History data or null on error
     */
    async function fetchMetricHistory(serverId, range = '1h') {
        try {
            const response = await fetch(`/api/server/${serverId}/metric-history/?range=${range}`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const data = await response.json();
            return data;
        } catch (error) {
            console.warn(`Failed to fetch metric history for server ${serverId}:`, error);
            return null;
        }
    }

    /**
     * Get severity color
     * @param {string} severity - Severity level
     * @returns {string} - Color hex code
     */
    function getSeverityColor(severity) {
        const colors = {
            'OK': '#94a3b8',
            'LOW': '#fbbf24',
            'MEDIUM': '#fb923c',
            'HIGH': '#ef4444',
            'CRITICAL': '#dc2626'
        };
        return colors[severity] || colors['OK'];
    }

    /**
     * Get severity point radius
     * @param {string} severity - Severity level
     * @returns {number} - Point radius
     */
    function getSeverityRadius(severity) {
        const radii = {
            'LOW': 3,
            'MEDIUM': 4,
            'HIGH': 5,
            'CRITICAL': 6
        };
        return radii[severity] || 3;
    }

    /**
     * Render metrics and anomaly history chart
     * @param {CanvasRenderingContext2D} ctx - Canvas context
     * @param {Object} data - Chart data from API
     */
    function renderMetricsAnomalyChart(ctx, data) {
        // Destroy existing chart if present
        if (chartInstance) {
            chartInstance.destroy();
            chartInstance = null;
        }

        const timestamps = data.timestamps || [];
        const cpuValues = data.cpu || [];
        const memoryValues = data.memory || [];
        const diskValues = data.disk || [];
        const anomalies = data.anomalies || [];

        // Prepare anomaly scatter data
        const anomalyScatterData = [];
        const anomalyLabels = [];

        // Map anomalies to chart coordinates
        anomalies.forEach(anomaly => {
            const timestamp = anomaly.timestamp;
            const timestampIndex = timestamps.indexOf(timestamp);
            
            // Find corresponding metric value
            let yValue = null;
            if (anomaly.metric_type === 'cpu' && timestampIndex >= 0 && timestampIndex < cpuValues.length) {
                yValue = cpuValues[timestampIndex];
            } else if (anomaly.metric_type === 'memory' && timestampIndex >= 0 && timestampIndex < memoryValues.length) {
                yValue = memoryValues[timestampIndex];
            } else if (anomaly.metric_type === 'disk' && timestampIndex >= 0 && timestampIndex < diskValues.length) {
                yValue = diskValues[timestampIndex];
            } else {
                // Fallback to metric_value from anomaly
                yValue = anomaly.metric_value;
            }

            if (yValue !== null && timestampIndex >= 0) {
                anomalyScatterData.push({
                    x: timestampIndex,
                    y: yValue
                });
                
                anomalyLabels.push({
                    timestamp: timestamp,
                    metric_name: anomaly.metric_name,
                    metric_type: anomaly.metric_type,
                    severity: anomaly.severity,
                    metric_value: anomaly.metric_value
                });
            }
        });

        // Create datasets
        const datasets = [
            // CPU line (primary)
            {
                label: 'CPU %',
                data: cpuValues,
                borderColor: '#60a5fa',
                backgroundColor: 'rgba(96, 165, 250, 0.1)',
                borderWidth: 2,
                fill: false,
                tension: 0.1,
                pointRadius: 0,
                pointHoverRadius: 4
            },
            // Memory line (optional)
            {
                label: 'Memory %',
                data: memoryValues,
                borderColor: '#f472b6',
                backgroundColor: 'rgba(244, 114, 182, 0.1)',
                borderWidth: 2,
                fill: false,
                tension: 0.1,
                pointRadius: 0,
                pointHoverRadius: 4
            },
            // Disk line (optional)
            {
                label: 'Disk %',
                data: diskValues,
                borderColor: '#34d399',
                backgroundColor: 'rgba(52, 211, 153, 0.1)',
                borderWidth: 2,
                fill: false,
                tension: 0.1,
                pointRadius: 0,
                pointHoverRadius: 4
            }
        ];

        // Add anomaly scatter dataset if there are anomalies
        if (anomalyScatterData.length > 0) {
            // Group anomalies by severity for better visualization
            const severityGroups = {};
            anomalies.forEach((anomaly, index) => {
                const severity = anomaly.severity || 'MEDIUM';
                if (!severityGroups[severity]) {
                    severityGroups[severity] = [];
                }
                
                const timestamp = anomaly.timestamp;
                const timestampIndex = timestamps.indexOf(timestamp);
                let yValue = null;
                
                if (anomaly.metric_type === 'cpu' && timestampIndex >= 0 && timestampIndex < cpuValues.length) {
                    yValue = cpuValues[timestampIndex];
                } else if (anomaly.metric_type === 'memory' && timestampIndex >= 0 && timestampIndex < memoryValues.length) {
                    yValue = memoryValues[timestampIndex];
                } else if (anomaly.metric_type === 'disk' && timestampIndex >= 0 && timestampIndex < diskValues.length) {
                    yValue = diskValues[timestampIndex];
                } else {
                    yValue = anomaly.metric_value;
                }

                if (yValue !== null && timestampIndex >= 0) {
                    severityGroups[severity].push({
                        x: timestampIndex,
                        y: yValue,
                        anomaly: anomaly
                    });
                }
            });

            // Create scatter dataset for each severity level
            Object.keys(severityGroups).forEach(severity => {
                const groupData = severityGroups[severity];
                if (groupData.length > 0) {
                    datasets.push({
                        label: `Anomaly (${severity})`,
                        data: groupData.map(item => ({ x: item.x, y: item.y })),
                        type: 'scatter',
                        backgroundColor: getSeverityColor(severity),
                        borderColor: getSeverityColor(severity),
                        pointRadius: getSeverityRadius(severity),
                        pointHoverRadius: getSeverityRadius(severity) + 2,
                        pointStyle: 'circle',
                        showLine: false,
                        order: 0, // Render on top
                        parsing: false
                    });
                }
            });
        }

        // Create chart
        chartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: timestamps,
                datasets: datasets
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: {
                    intersect: false,
                    mode: 'index'
                },
                plugins: {
                    legend: {
                        display: true,
                        position: 'top',
                        labels: {
                            color: '#f1f5f9',
                            font: {
                                size: 12
                            },
                            usePointStyle: true,
                            padding: 10
                        }
                    },
                    tooltip: {
                        backgroundColor: 'rgba(15, 23, 42, 0.95)',
                        titleColor: '#f1f5f9',
                        bodyColor: '#cbd5e1',
                        borderColor: 'rgba(255, 255, 255, 0.2)',
                        borderWidth: 1,
                        padding: 12,
                        callbacks: {
                            title: function(context) {
                                const index = context[0].dataIndex;
                                if (index >= 0 && index < timestamps.length) {
                                    return timestamps[index];
                                }
                                return '';
                            },
                            label: function(context) {
                                const label = context.dataset.label || '';
                                const value = context.parsed.y;
                                
                                // Check if this is an anomaly point
                                if (label.includes('Anomaly')) {
                                    const index = context.dataIndex;
                                    const anomaly = anomalies.find((a, i) => {
                                        const tsIndex = timestamps.indexOf(a.timestamp);
                                        return tsIndex === index;
                                    });
                                    
                                    if (anomaly) {
                                        return [
                                            `${label}: ${value.toFixed(1)}%`,
                                            `Type: ${anomaly.metric_type}`,
                                            `Severity: ${anomaly.severity}`,
                                            `Metric: ${anomaly.metric_name}`
                                        ];
                                    }
                                }
                                
                                if (value !== null && value !== undefined) {
                                    return `${label}: ${value.toFixed(1)}%`;
                                }
                                return `${label}: N/A`;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        ticks: {
                            color: '#94a3b8',
                            maxTicksLimit: 10,
                            font: {
                                size: 10
                            }
                        },
                        grid: {
                            color: 'rgba(255, 255, 255, 0.1)'
                        }
                    },
                    y: {
                        beginAtZero: true,
                        max: 100,
                        min: 0,
                        ticks: {
                            color: '#94a3b8',
                            maxTicksLimit: 8,
                            font: {
                                size: 10
                            },
                            callback: function(value) {
                                return value + '%';
                            }
                        },
                        grid: {
                            color: 'rgba(255, 255, 255, 0.1)'
                        },
                        // Ensure we can see values up to 100%
                        suggestedMax: 100
                    }
                },
                animation: {
                    duration: 0 // Disable animations for performance
                }
            }
        });
    }

    /**
     * Load and render chart with specified time range
     * @param {number} serverId - Server ID
     * @param {string} range - Time range (1h, 7d, 1m, 3m)
     */
    async function loadChartWithRange(serverId, range = '1h') {
        const chartCanvas = document.getElementById('metricsAnomalyHistoryChart');
        if (!chartCanvas) {
            return;
        }

        // Show loading state (optional - can add a spinner)
        chartCanvas.style.opacity = '0.5';

        const data = await fetchMetricHistory(serverId, range);
        
        chartCanvas.style.opacity = '1';
        
        if (!data) {
            console.warn('No metric history data available');
            return;
        }

        const ctx = chartCanvas.getContext('2d');
        renderMetricsAnomalyChart(ctx, data);
    }

    /**
     * Initialize chart on page load
     */
    function initAnomalyHistoryChart() {
        const chartCanvas = document.getElementById('metricsAnomalyHistoryChart');
        if (!chartCanvas) {
            return; // Chart container not found
        }

        // Find server ID from anomaly summary or any element with data-server-id
        const container = document.getElementById('anomaly-summary') ||
                          document.querySelector('[data-server-id]');
        
        if (!container) {
            console.warn('Could not find server ID for metric history chart');
            return;
        }

        const serverId = parseInt(container.getAttribute('data-server-id'), 10);
        if (!serverId) {
            console.warn('Server ID not found in data attribute');
            return;
        }

        // Set up time range filter buttons
        const filterButtons = document.querySelectorAll('.time-range-btn');
        filterButtons.forEach(button => {
            button.addEventListener('click', function() {
                // Remove active class from all buttons
                filterButtons.forEach(btn => btn.classList.remove('active'));
                
                // Add active class to clicked button
                this.classList.add('active');
                
                // Get range from data attribute
                const range = this.getAttribute('data-range');
                
                // Load chart with new range
                loadChartWithRange(serverId, range);
            });
        });

        // Initial load with default range (1h)
        loadChartWithRange(serverId, '1h');
    }

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAnomalyHistoryChart);
    } else {
        initAnomalyHistoryChart();
    }

    // Clean up on page unload
    window.addEventListener('beforeunload', () => {
        if (chartInstance) {
            chartInstance.destroy();
            chartInstance = null;
        }
    });
})();

