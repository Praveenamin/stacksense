<#
.SYNOPSIS
  StackSense push-agent installer for Windows (the analog of install.sh).

  Run this ON THE MONITORED WINDOWS HOST, elevated (Administrator). It downloads the
  standalone agent .exe + the NSSM service wrapper, registers a Windows service that
  runs the agent as LocalSystem, and starts it. The agent only dials OUT over HTTPS to
  the monitoring server with its per-server token; it opens no inbound port.

.PARAMETER Url       Monitoring server base URL, e.g. https://mon.example.com:1443  (required)
.PARAMETER Token     Per-server agent token from the Add-Server page                (required)
.PARAMETER Interval  Seconds between metric pushes (default 30)
.PARAMETER Insecure  Skip TLS verification (self-signed only)
.PARAMETER Uninstall Remove the service and files, then exit

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -Url https://mon:1443 -Token abc123
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $false)][string]$Token,
    [int]$Interval = 30,
    [switch]$Insecure,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$ServiceName = "StackSenseAgent"
$InstallDir  = Join-Path $env:ProgramFiles "StackSense Agent"
$ExePath     = Join-Path $InstallDir "stacksense-agent.exe"
$NssmPath    = Join-Path $InstallDir "nssm.exe"

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "Please run this installer as Administrator."
        exit 1
    }
}

Assert-Admin

# --- Uninstall -------------------------------------------------------------
if ($Uninstall) {
    Write-Host "[uninstall] Removing $ServiceName ..."
    if (Test-Path $NssmPath) {
        & $NssmPath stop $ServiceName 2>$null
        & $NssmPath remove $ServiceName confirm 2>$null
    } else {
        sc.exe stop $ServiceName 2>$null | Out-Null
        sc.exe delete $ServiceName 2>$null | Out-Null
    }
    Start-Sleep -Seconds 2
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    Write-Host "[uninstall] Done."
    exit 0
}

if (-not $Token) { Write-Error "-Token is required."; exit 1 }
$Url = $Url.TrimEnd('/')

# TLS 1.2 for the downloads (older PS defaults can fail against modern servers).
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

Write-Host "[1/5] Preparing $InstallDir ..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

function Get-File($uri, $outFile) {
    Write-Host "       downloading $uri"
    if ($Insecure -and $PSVersionTable.PSVersion.Major -ge 6) {
        Invoke-WebRequest -Uri $uri -OutFile $outFile -UseBasicParsing -SkipCertificateCheck
    } else {
        Invoke-WebRequest -Uri $uri -OutFile $outFile -UseBasicParsing
    }
}

Write-Host "[2/5] Downloading agent + service wrapper ..."
Get-File "$Url/agent/stacksense-agent.exe" $ExePath
Get-File "$Url/agent/nssm.exe" $NssmPath

Write-Host "[3/5] (Re)creating the Windows service ..."
& $NssmPath stop $ServiceName 2>$null
& $NssmPath remove $ServiceName confirm 2>$null
& $NssmPath install $ServiceName $ExePath
$verify = if ($Insecure) { "false" } else { "true" }
# Config via service environment (the agent reads STACKSENSE_* env first), so the token
# is not written to a shared file and LocalSystem's home dir is irrelevant.
& $NssmPath set $ServiceName AppEnvironmentExtra `
    "STACKSENSE_URL=$Url" "STACKSENSE_TOKEN=$Token" `
    "STACKSENSE_INTERVAL=$Interval" "STACKSENSE_VERIFY_TLS=$verify"
& $NssmPath set $ServiceName Start SERVICE_AUTO_START
& $NssmPath set $ServiceName AppExit Default Restart
& $NssmPath set $ServiceName AppRestartDelay 10000
& $NssmPath set $ServiceName AppStdout (Join-Path $InstallDir "agent.log")
& $NssmPath set $ServiceName AppStderr (Join-Path $InstallDir "agent.log")
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName Description "StackSense monitoring push agent"

Write-Host "[4/5] Verifying authentication ..."
try {
    $headers = @{ Authorization = "Bearer $Token" }
    if ($Insecure -and $PSVersionTable.PSVersion.Major -ge 6) {
        Invoke-WebRequest -Uri "$Url/api/agent/ping/" -Headers $headers -UseBasicParsing -SkipCertificateCheck | Out-Null
    } else {
        Invoke-WebRequest -Uri "$Url/api/agent/ping/" -Headers $headers -UseBasicParsing | Out-Null
    }
    Write-Host "       auth OK"
} catch {
    Write-Warning "Auth check failed ($($_.Exception.Message)). Starting anyway; verify the token/URL if no data appears."
}

Write-Host "[5/5] Starting service ..."
& $NssmPath start $ServiceName

Write-Host ""
Write-Host "StackSense agent installed and started as the '$ServiceName' service."
Write-Host "Logs: $(Join-Path $InstallDir 'agent.log')   |   Uninstall: install.ps1 -Uninstall"
