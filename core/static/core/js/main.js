// Global utilities
function formatBytes(bytes) {
  if (bytes === 0) return "0 B"
  const k = 1024
  const sizes = ["B", "KB", "MB", "GB", "TB"]
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return Number.parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i]
}

function formatPercentage(value) {
  return value.toFixed(1) + "%"
}

// Environment switcher
document.addEventListener("DOMContentLoaded", () => {
  const envButtons = document.querySelectorAll("[data-env]")
  envButtons.forEach((button) => {
    button.addEventListener("click", function () {
      envButtons.forEach((b) => {
        b.classList.remove("bg-secondary")
        b.classList.add("hover:bg-secondary")
      })
      this.classList.add("bg-secondary")
      this.classList.remove("hover:bg-secondary")

      const env = this.dataset.env
      console.log("[v0] Environment switched to:", env)
      // Trigger data reload for new environment
      const loadDashboardData =
        window.loadDashboardData ||
        (() => {}) // Declare the variable // Declare the variable
      loadDashboardData(env)
    })
  })
})

// Server search functionality
function searchServers() {
  const searchTerm = document.getElementById("serverSearch").value.toLowerCase()
  const serverSelect = document.getElementById("selectedServer")
  const options = serverSelect.options

  for (let i = 0; i < options.length; i++) {
    const optionText = options[i].text.toLowerCase()
    if (optionText.includes(searchTerm)) {
      serverSelect.selectedIndex = i
      break
    }
  }

  const loadServerDetails =
    window.loadServerDetails ||
    (() => {}) // Declare the variable // Declare the variable
  if (typeof loadServerDetails === "function") {
    loadServerDetails(serverSelect.value)
  }
}

// Real-time data simulation (replace with actual API calls)
function generateRandomMetric(min, max) {
  return Math.random() * (max - min) + min
}
