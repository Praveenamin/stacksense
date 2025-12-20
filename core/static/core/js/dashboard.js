import { Chart } from "@/components/ui/chart"
// Dashboard-specific JavaScript
let cpuMemChart, networkDiskChart
let updateInterval

function initCharts() {
  const chartConfig = {
    type: "line",
    options: {
      responsive: true,
      maintainAspectRatio: true,
      plugins: {
        legend: {
          labels: {
            color: "oklch(0.98 0 0)",
          },
        },
      },
      scales: {
        y: {
          beginAtZero: true,
          max: 100,
          ticks: {
            color: "oklch(0.65 0 0)",
          },
          grid: {
            color: "oklch(0.2 0 0)",
          },
        },
        x: {
          ticks: {
            color: "oklch(0.65 0 0)",
          },
          grid: {
            color: "oklch(0.2 0 0)",
          },
        },
      },
    },
  }

  // CPU & Memory Chart
  const cpuMemCtx = document.getElementById("cpuMemChart").getContext("2d")
  cpuMemChart = new Chart(cpuMemCtx, {
    ...chartConfig,
    data: {
      labels: generateTimeLabels(12),
      datasets: [
        {
          label: "CPU %",
          data: [],
          borderColor: "oklch(0.55 0.22 260)",
          backgroundColor: "oklch(0.55 0.22 260 / 0.1)",
          tension: 0.4,
        },
        {
          label: "Memory %",
          data: [],
          borderColor: "oklch(0.6 0.15 200)",
          backgroundColor: "oklch(0.6 0.15 200 / 0.1)",
          tension: 0.4,
        },
      ],
    },
  })

  // Network & Disk Chart
  const networkDiskCtx = document.getElementById("networkDiskChart").getContext("2d")
  networkDiskChart = new Chart(networkDiskCtx, {
    ...chartConfig,
    data: {
      labels: generateTimeLabels(12),
      datasets: [
        {
          label: "Network (Mbps)",
          data: [],
          borderColor: "oklch(0.65 0.18 160)",
          backgroundColor: "oklch(0.65 0.18 160 / 0.1)",
          tension: 0.4,
        },
        {
          label: "Disk I/O (MB/s)",
          data: [],
          borderColor: "oklch(0.7 0.2 120)",
          backgroundColor: "oklch(0.7 0.2 120 / 0.1)",
          tension: 0.4,
        },
      ],
    },
  })

  // Initialize with sample data
  updateChartData()
}

function generateTimeLabels(hours) {
  const labels = []
  const now = new Date()
  for (let i = hours; i >= 0; i--) {
    const time = new Date(now - i * 60 * 60 * 1000)
    labels.push(time.getHours() + ":00")
  }
  return labels
}

function generateRandomMetric(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min
}

function formatPercentage(value) {
  return value.toFixed(1) + "%"
}

function updateChartData() {
  // Simulate real-time data (replace with actual API calls)
  const cpuData = Array.from({ length: 13 }, () => generateRandomMetric(20, 80))
  const memData = Array.from({ length: 13 }, () => generateRandomMetric(30, 70))
  const netData = Array.from({ length: 13 }, () => generateRandomMetric(10, 100))
  const diskData = Array.from({ length: 13 }, () => generateRandomMetric(5, 50))

  cpuMemChart.data.datasets[0].data = cpuData
  cpuMemChart.data.datasets[1].data = memData
  cpuMemChart.update()

  networkDiskChart.data.datasets[0].data = netData
  networkDiskChart.data.datasets[1].data = diskData
  networkDiskChart.update()

  // Update metric cards
  document.getElementById("cpuUsage").textContent = formatPercentage(cpuData[cpuData.length - 1])
  document.getElementById("memUsage").textContent = formatPercentage(memData[memData.length - 1])
  document.getElementById("netUsage").textContent = netData[netData.length - 1].toFixed(1) + " Mbps"
  document.getElementById("diskUsage").textContent = formatPercentage(diskData[diskData.length - 1])

  // Update trends
  document.getElementById("cpuTrend").textContent = "↑ 2.3% from last hour"
  document.getElementById("memTrend").textContent = "↓ 1.5% from last hour"
  document.getElementById("netTrend").textContent = "↑ 5.2 Mbps from last hour"
  document.getElementById("diskTrend").textContent = "↓ 0.8% from last hour"

  loadAnomalies()
}

function loadServerDetails(serverId) {
  console.log("[v0] Loading details for server:", serverId)
  // Fetch server details via API
  // For now, just update the charts
  updateChartData()
}

function loadDashboardData(environment) {
  console.log("[v0] Loading dashboard data for environment:", environment)
  // Fetch data from API based on environment
  updateChartData()
}

function loadAnomalies() {
  const anomalyList = document.getElementById("anomalyList")
  const anomalies = [
    { type: "warning", message: "CPU usage spike detected on server-prod-01", time: "2 minutes ago" },
    { type: "info", message: "Memory usage trending upward on server-prod-03", time: "15 minutes ago" },
    { type: "critical", message: "Disk space critically low on server-staging-02", time: "1 hour ago" },
  ]

  anomalyList.innerHTML = anomalies
    .map(
      (anomaly) => `
        <div class="flex items-start gap-3 p-3 bg-background rounded border border-border">
            <svg class="h-5 w-5 mt-0.5 ${anomaly.type === "critical" ? "text-destructive" : anomaly.type === "warning" ? "text-warning" : "text-primary"}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path>
            </svg>
            <div class="flex-1">
                <p class="text-sm">${anomaly.message}</p>
                <p class="text-xs text-muted mt-1">${anomaly.time}</p>
            </div>
        </div>
    `,
    )
    .join("")
}

// Initialize on page load
document.addEventListener("DOMContentLoaded", () => {
  initCharts()

  // Update data every 5 seconds
  updateInterval = setInterval(updateChartData, 5000)

  // Time range selector
  document.getElementById("timeRange").addEventListener("change", function () {
    console.log("[v0] Time range changed to:", this.value)
    // Regenerate chart labels and data based on time range
    const hours = Number.parseInt(this.value)
    cpuMemChart.data.labels = generateTimeLabels(hours)
    networkDiskChart.data.labels = generateTimeLabels(hours)
    updateChartData()
  })
})

// Cleanup on page unload
window.addEventListener("beforeunload", () => {
  if (updateInterval) {
    clearInterval(updateInterval)
  }
})
