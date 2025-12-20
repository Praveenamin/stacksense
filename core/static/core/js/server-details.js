import { Chart } from "@/components/ui/chart"
// Server details page JavaScript
let cpuChart, ramChart, networkChart, diskChart, anomalyChart
let detailsUpdateInterval

function generateRandomMetric(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min
}

function formatPercentage(value) {
  return value + "%"
}

function formatBytes(bytes) {
  if (bytes === 0) return "0 Bytes"
  const k = 1024
  const sizes = ["Bytes", "KB", "MB", "GB", "TB"]
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return Number.parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i]
}

function initDetailCharts() {
  const chartConfig = {
    type: "line",
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          display: false,
        },
      },
      scales: {
        y: {
          beginAtZero: true,
          max: 100,
          ticks: {
            color: "oklch(0.65 0 0)",
            font: {
              size: 10,
            },
          },
          grid: {
            color: "oklch(0.2 0 0)",
          },
        },
        x: {
          ticks: {
            color: "oklch(0.65 0 0)",
            font: {
              size: 10,
            },
          },
          grid: {
            display: false,
          },
        },
      },
    },
  }

  // CPU Chart
  cpuChart = new Chart(document.getElementById("cpuDetailChart"), {
    ...chartConfig,
    data: {
      labels: generateShortTimeLabels(20),
      datasets: [
        {
          data: [],
          borderColor: "oklch(0.55 0.22 260)",
          backgroundColor: "oklch(0.55 0.22 260 / 0.1)",
          tension: 0.4,
          fill: true,
        },
      ],
    },
  })

  // RAM Chart
  ramChart = new Chart(document.getElementById("ramDetailChart"), {
    ...chartConfig,
    data: {
      labels: generateShortTimeLabels(20),
      datasets: [
        {
          data: [],
          borderColor: "oklch(0.6 0.15 200)",
          backgroundColor: "oklch(0.6 0.15 200 / 0.1)",
          tension: 0.4,
          fill: true,
        },
      ],
    },
  })

  // Network Chart
  networkChart = new Chart(document.getElementById("networkDetailChart"), {
    ...chartConfig,
    data: {
      labels: generateShortTimeLabels(20),
      datasets: [
        {
          data: [],
          borderColor: "oklch(0.65 0.18 160)",
          backgroundColor: "oklch(0.65 0.18 160 / 0.1)",
          tension: 0.4,
          fill: true,
        },
      ],
    },
  })

  // Disk Chart
  diskChart = new Chart(document.getElementById("diskDetailChart"), {
    ...chartConfig,
    data: {
      labels: generateShortTimeLabels(20),
      datasets: [
        {
          data: [],
          borderColor: "oklch(0.7 0.2 120)",
          backgroundColor: "oklch(0.7 0.2 120 / 0.1)",
          tension: 0.4,
          fill: true,
        },
      ],
    },
  })

  // Anomaly Metrics Chart
  anomalyChart = new Chart(document.getElementById("anomalyMetricsChart"), {
    type: "line",
    data: {
      labels: generateShortTimeLabels(30),
      datasets: [
        {
          label: "CPU",
          data: [],
          borderColor: "oklch(0.55 0.22 260)",
          tension: 0.4,
        },
        {
          label: "Memory",
          data: [],
          borderColor: "oklch(0.6 0.15 200)",
          tension: 0.4,
        },
        {
          label: "Anomaly Threshold",
          data: Array(30).fill(80),
          borderColor: "oklch(0.55 0.25 25)",
          borderDash: [5, 5],
          tension: 0,
        },
      ],
    },
    options: {
      responsive: true,
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
  })

  updateDetailData()
}

function generateShortTimeLabels(count) {
  const labels = []
  const now = new Date()
  for (let i = count; i >= 0; i--) {
    const time = new Date(now - i * 30 * 1000) // 30 second intervals
    labels.push(time.getMinutes() + ":" + String(time.getSeconds()).padStart(2, "0"))
  }
  return labels
}

function updateDetailData() {
  // Simulate real-time metrics (replace with actual API calls)
  const cpuData = Array.from({ length: 21 }, () => generateRandomMetric(20, 85))
  const ramData = Array.from({ length: 21 }, () => generateRandomMetric(30, 75))
  const netData = Array.from({ length: 21 }, () => generateRandomMetric(10, 90))
  const diskData = Array.from({ length: 21 }, () => generateRandomMetric(40, 70))

  cpuChart.data.datasets[0].data = cpuData
  cpuChart.update("none")

  ramChart.data.datasets[0].data = ramData
  ramChart.update("none")

  networkChart.data.datasets[0].data = netData
  networkChart.update("none")

  diskChart.data.datasets[0].data = diskData
  diskChart.update("none")

  // Update metrics
  const currentCpu = cpuData[cpuData.length - 1]
  const currentRam = ramData[ramData.length - 1]
  const currentNet = netData[netData.length - 1]
  const currentDisk = diskData[diskData.length - 1]

  document.getElementById("cpuDetailUsage").textContent = formatPercentage(currentCpu)
  document.getElementById("cpuTemp").textContent = (45 + currentCpu * 0.3).toFixed(1) + "°C"
  document.getElementById("cpuLoad").textContent = (currentCpu / 25).toFixed(2)

  document.getElementById("ramDetailUsage").textContent = formatPercentage(currentRam)
  const totalRam = 32
  const usedRam = ((totalRam * currentRam) / 100).toFixed(1)
  document.getElementById("ramUsed").textContent = usedRam + " GB"
  document.getElementById("ramFree").textContent = (totalRam - usedRam).toFixed(1) + " GB"
  document.getElementById("ramCached").textContent = (usedRam * 0.2).toFixed(1) + " GB"

  document.getElementById("netDownload").textContent = currentNet.toFixed(1) + " Mbps"
  document.getElementById("netUpload").textContent = (currentNet * 0.3).toFixed(1) + " Mbps"
  document.getElementById("netTotalRx").textContent = formatBytes(currentNet * 1024 * 1024 * 3600)
  document.getElementById("netTotalTx").textContent = formatBytes(currentNet * 0.3 * 1024 * 1024 * 3600)
  document.getElementById("netLatency").textContent = (5 + Math.random() * 10).toFixed(1) + " ms"

  document.getElementById("diskDetailUsage").textContent = formatPercentage(currentDisk)
  const totalDisk = 500
  const usedDisk = ((totalDisk * currentDisk) / 100).toFixed(1)
  document.getElementById("diskUsed").textContent = usedDisk + " GB"
  document.getElementById("diskFree").textContent = (totalDisk - usedDisk).toFixed(1) + " GB"
  document.getElementById("diskIO").textContent = (Math.random() * 50).toFixed(1) + " MB/s"

  // Update anomaly chart
  anomalyChart.data.datasets[0].data = cpuData
  anomalyChart.data.datasets[1].data = ramData
  anomalyChart.update("none")

  updateAnomalyDetails()
}

function updateAnomalyDetails() {
  const anomalyDetails = document.getElementById("anomalyDetails")
  const anomalies = [
    { metric: "CPU Spike", value: "85%", severity: "warning", time: "5m ago" },
    { metric: "Memory Leak Detected", value: "12MB/min", severity: "critical", time: "2m ago" },
    { metric: "Network Latency", value: "45ms", severity: "info", time: "1m ago" },
  ]

  anomalyDetails.innerHTML = anomalies
    .map(
      (a) => `
        <div class="p-3 bg-background rounded border border-border">
            <div class="flex items-center justify-between mb-2">
                <span class="text-sm font-medium">${a.metric}</span>
                <span class="px-2 py-1 text-xs rounded ${
                  a.severity === "critical" ? "bg-destructive" : a.severity === "warning" ? "bg-warning" : "bg-primary"
                } text-white">${a.severity}</span>
            </div>
            <div class="text-lg font-bold">${a.value}</div>
            <div class="text-xs text-muted mt-1">${a.time}</div>
        </div>
    `,
    )
    .join("")
}

// Service monitoring toggles
document.addEventListener("DOMContentLoaded", () => {
  initDetailCharts()

  // Update every 3 seconds
  detailsUpdateInterval = setInterval(updateDetailData, 3000)

  // Service toggle handlers
  document.querySelectorAll(".service-toggle").forEach((toggle) => {
    toggle.addEventListener("change", function () {
      const serviceId = this.dataset.serviceId
      const monitored = this.checked
      console.log("[v0] Service monitoring toggled:", serviceId, monitored)

      // Send API request to update service monitoring status
      // fetch(`/api/services/${serviceId}/monitor`, {
      //     method: 'POST',
      //     body: JSON.stringify({ monitored }),
      //     headers: { 'Content-Type': 'application/json' }
      // });
    })
  })
})

window.addEventListener("beforeunload", () => {
  if (detailsUpdateInterval) {
    clearInterval(detailsUpdateInterval)
  }
})
