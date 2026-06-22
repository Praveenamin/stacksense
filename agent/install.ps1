<#
.SYNOPSIS
  StackSense push-agent installer for Windows (standalone exe; analog of install.sh).

  Run this ON THE MONITORED WINDOWS HOST, elevated (Administrator). Fully automated, no
  prerequisites and NO Python on the box: it downloads the standalone agent .exe and runs
  it via a native Windows Scheduled Task (SYSTEM, at startup, restart-on-failure). The
  agent only dials OUT over HTTPS with its per-server token; it opens no inbound port.

.PARAMETER Url       Monitoring server base URL, e.g. https://mon.example.com  (required)
.PARAMETER Token     Per-server agent token from the Add-Server page           (required)
.PARAMETER Interval  Seconds between metric pushes (default 30)
.PARAMETER Insecure  Skip TLS verification (self-signed servers only)
.PARAMETER Uninstall Remove the task and files, then exit
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
$TaskName     = "StackSenseAgent"
$InstallDir   = Join-Path $env:ProgramFiles "StackSense Agent"
$ExePath      = Join-Path $InstallDir "stacksense-agent.exe"
$CmdLauncher  = Join-Path $InstallDir "run-agent.cmd"
$LogFile      = Join-Path $InstallDir "agent.log"

function Assert-Admin {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "Please run this installer as Administrator."; exit 1
    }
}
Assert-Admin

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if ($Insecure) {
    try {
        Add-Type @"
using System.Net; using System.Security.Cryptography.X509Certificates;
public class StackSenseTrustAll : ICertificatePolicy {
  public bool CheckValidationResult(ServicePoint s, X509Certificate c, WebRequest r, int p) { return true; }
}
"@
        [Net.ServicePointManager]::CertificatePolicy = New-Object StackSenseTrustAll
    } catch {}
}

function Stop-Agent {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    # Kill the running agent -- the exe, and any python.exe from a prior Python-based install.
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq "stacksense-agent.exe" -or
                       ($_.Name -eq "python.exe" -and $_.CommandLine -like "*StackSense Agent*") } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}

if ($Uninstall) {
    Write-Host "[uninstall] Removing $TaskName ..."
    Stop-Agent
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    Write-Host "[uninstall] Done."; exit 0
}

if (-not $Token) { Write-Error "-Token is required."; exit 1 }
$Url = $Url.TrimEnd('/')

Write-Host "[1/3] Stopping any existing agent + preparing $InstallDir ..."
Stop-Agent
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

Write-Host "[2/3] Downloading the agent .exe ..."
try {
    Invoke-WebRequest -Uri "$Url/agent/stacksense-agent.exe" -OutFile $ExePath -UseBasicParsing
} catch {
    Write-Error ("Could not download the agent .exe from $Url/agent/stacksense-agent.exe " +
        "($($_.Exception.Message)). The Windows agent binary may not be published yet -- build it " +
        "(GitHub Actions 'Build Windows agent exe') and place stacksense-agent.exe in the server's " +
        "agent/ folder, then re-run this installer.")
    exit 1
}

Write-Host "      Verifying authentication ..."
try {
    Invoke-WebRequest -Uri "$Url/api/agent/ping/" -Headers @{ Authorization = "Bearer $Token" } -UseBasicParsing | Out-Null
    Write-Host "      auth OK"
} catch {
    Write-Warning "Auth check failed ($($_.Exception.Message)). Continuing; verify token/URL if no data appears."
}

Write-Host "[3/3] Registering + starting the startup task ..."
# Launcher carries the config as env vars (the agent reads STACKSENSE_* first) and logs to a
# file. Locked down so the token isn't readable by standard users.
$verify = if ($Insecure) { "false" } else { "true" }
$cmd = @"
@echo off
set "STACKSENSE_URL=$Url"
set "STACKSENSE_TOKEN=$Token"
set "STACKSENSE_INTERVAL=$Interval"
set "STACKSENSE_VERIFY_TLS=$verify"
"$ExePath" >> "$LogFile" 2>&1
"@
Set-Content -Path $CmdLauncher -Value $cmd -Encoding ASCII
& icacls "$CmdLauncher" /inheritance:r /grant:r "SYSTEM:F" "Administrators:F" | Out-Null

$action    = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$CmdLauncher`""
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description "StackSense monitoring push agent" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "StackSense agent installed (no Python); running via the '$TaskName' scheduled task (SYSTEM, auto-start)."
Write-Host "Logs: $LogFile   |   Status: Get-ScheduledTask $TaskName   |   Uninstall: install.ps1 -Uninstall"
