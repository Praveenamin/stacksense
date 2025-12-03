/**
 * Anomaly Status Polling System
 * 
 * Fetches and updates anomaly status for servers on the dashboard and server details pages.
 * Polls the API every 30 seconds to keep status current.
 */

(function() {
    'use strict';

    const POLL_INTERVAL = 30000; // 30 seconds
    let pollIntervalId = null;

    /**
     * Update anomaly status for a single server
     * @param {number} serverId - Server ID
     * @param {Object} data - Anomaly status data from API
     */
    function updateAnomalyStatus(serverId, data) {
        // Find all elements with this server ID
        const elements = document.querySelectorAll(`[data-server-id="${serverId}"] .anomaly-status`);
        
        if (elements.length === 0) {
            return; // No elements found, skip gracefully
        }

        // Determine severity and active count
        const severity = data.highest_severity || 'unknown';
        const active = data.active || 0;
        
        // Build label text
        let labelText = `Anomaly: ${severity}`;
        if (active > 0) {
            labelText += ` (${active} active)`;
        }

        // Update each element
        elements.forEach(element => {
            const label = element.querySelector('.anomaly-status-label');
            if (label) {
                label.textContent = labelText;
            }

            // Remove all severity classes
            element.classList.remove(
                'anomaly-ok',
                'anomaly-low',
                'anomaly-medium',
                'anomaly-high',
                'anomaly-critical',
                'anomaly-unknown'
            );

            // Add appropriate severity class
            const severityClass = `anomaly-${severity.toLowerCase()}`;
            element.classList.add(severityClass);

            // Add/remove active indicator
            if (active > 0) {
                element.classList.add('has-active');
            } else {
                element.classList.remove('has-active');
            }
        });
    }

    /**
     * Fetch anomaly status for a server
     * @param {number} serverId - Server ID
     * @returns {Promise<Object|null>} - Status data or null on error
     */
    async function fetchAnomalyStatus(serverId) {
        try {
            const response = await fetch(`/api/server/${serverId}/anomaly-status/`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const data = await response.json();
            return data;
        } catch (error) {
            console.warn(`Failed to fetch anomaly status for server ${serverId}:`, error);
            return null;
        }
    }

    /**
     * Update status for a single server (with error handling)
     * @param {number} serverId - Server ID
     */
    async function updateServerStatus(serverId) {
        const data = await fetchAnomalyStatus(serverId);
        
        if (data) {
            updateAnomalyStatus(serverId, data);
        } else {
            // On error, set to unknown
            const elements = document.querySelectorAll(`[data-server-id="${serverId}"] .anomaly-status`);
            elements.forEach(element => {
                const label = element.querySelector('.anomaly-status-label');
                if (label) {
                    label.textContent = 'Anomaly: unknown';
                }
                element.classList.remove(
                    'anomaly-ok',
                    'anomaly-low',
                    'anomaly-medium',
                    'anomaly-high',
                    'anomaly-critical'
                );
                element.classList.add('anomaly-unknown');
                element.classList.remove('has-active');
            });
        }
    }

    /**
     * Update all server statuses on the page
     */
    async function updateAllServerStatuses() {
        // Find all server cards/elements with data-server-id
        const serverElements = document.querySelectorAll('[data-server-id]');
        const serverIds = new Set();
        
        serverElements.forEach(element => {
            const serverId = element.getAttribute('data-server-id');
            if (serverId) {
                serverIds.add(parseInt(serverId, 10));
            }
        });

        // Update each server
        const promises = Array.from(serverIds).map(serverId => updateServerStatus(serverId));
        await Promise.allSettled(promises);
    }

    /**
     * Update anomaly summary on server details page
     * @param {number} serverId - Server ID
     * @param {Object} data - Anomaly status data
     */
    function updateAnomalySummary(serverId, data) {
        const summaryElement = document.querySelector(`#anomaly-summary[data-server-id="${serverId}"]`);
        if (!summaryElement) {
            return; // Not on server details page
        }

        const severity = data.highest_severity || 'unknown';
        const active = data.active || 0;
        const details = data.details || {};

        // Update main severity
        const severityElement = summaryElement.querySelector('.anomaly-summary-severity');
        if (severityElement) {
            severityElement.textContent = `Anomaly: ${severity}`;
            severityElement.classList.remove(
                'anomaly-ok',
                'anomaly-low',
                'anomaly-medium',
                'anomaly-high',
                'anomaly-critical',
                'anomaly-unknown'
            );
            severityElement.classList.add(`anomaly-${severity.toLowerCase()}`);
        }

        // Update active count
        const countElement = summaryElement.querySelector('.anomaly-summary-count');
        if (countElement) {
            if (active > 0) {
                countElement.textContent = `${active} active`;
                countElement.style.display = 'inline';
            } else {
                countElement.textContent = '';
                countElement.style.display = 'none';
            }
        }

        // Update metric chips
        const metrics = ['cpu', 'memory', 'disk', 'network'];
        metrics.forEach(metric => {
            const chip = summaryElement.querySelector(`.metric-chip.metric-${metric}`);
            if (chip) {
                const status = details[metric] || 'normal';
                const statusText = status === 'anomaly' ? 'anomaly' : 'normal';
                chip.textContent = `${metric.charAt(0).toUpperCase() + metric.slice(1)}: ${statusText}`;
                
                chip.classList.remove('metric-ok', 'metric-anomaly');
                chip.classList.add(status === 'anomaly' ? 'metric-anomaly' : 'metric-ok');
            }
        });
    }

    /**
     * Update server details page anomaly summary
     * @param {number} serverId - Server ID
     */
    async function updateServerDetailsSummary(serverId) {
        const data = await fetchAnomalyStatus(serverId);
        if (data) {
            updateAnomalySummary(serverId, data);
        } else {
            // On error, set to unknown
            const summaryElement = document.querySelector(`#anomaly-summary[data-server-id="${serverId}"]`);
            if (summaryElement) {
                const severityElement = summaryElement.querySelector('.anomaly-summary-severity');
                if (severityElement) {
                    severityElement.textContent = 'Anomaly: unknown';
                    severityElement.classList.remove(
                        'anomaly-ok',
                        'anomaly-low',
                        'anomaly-medium',
                        'anomaly-high',
                        'anomaly-critical'
                    );
                    severityElement.classList.add('anomaly-unknown');
                }
            }
        }
    }

    /**
     * Initialize anomaly status polling
     */
    function initAnomalyStatus() {
        // Initial load
        updateAllServerStatuses();

        // Check if we're on server details page
        const summaryElement = document.querySelector('#anomaly-summary[data-server-id]');
        if (summaryElement) {
            const serverId = summaryElement.getAttribute('data-server-id');
            if (serverId) {
                updateServerDetailsSummary(parseInt(serverId, 10));
            }
        }

        // Set up polling
        pollIntervalId = setInterval(() => {
            updateAllServerStatuses();
            
            // Also update server details summary if present
            const summaryElement = document.querySelector('#anomaly-summary[data-server-id]');
            if (summaryElement) {
                const serverId = summaryElement.getAttribute('data-server-id');
                if (serverId) {
                    updateServerDetailsSummary(parseInt(serverId, 10));
                }
            }
        }, POLL_INTERVAL);
    }

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAnomalyStatus);
    } else {
        initAnomalyStatus();
    }

    // Clean up on page unload
    window.addEventListener('beforeunload', () => {
        if (pollIntervalId) {
            clearInterval(pollIntervalId);
        }
    });
})();

