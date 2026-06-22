<#
.SYNOPSIS
  StackSense push-agent installer for Windows (self-contained; analog of install.sh).

  Run this ON THE MONITORED WINDOWS HOST, elevated (Administrator). Fully automated, no
  prerequisites: it lays down a PRIVATE Python (the official embeddable build -- nothing
  system-wide, no PATH change), installs psutil, downloads the agent, and runs it via a
  native Windows Scheduled Task (SYSTEM, at startup, restart-on-failure) -- no third-party
  service wrapper needed. The agent only dials OUT over HTTPS with its per-server token.

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
$PyDir        = Join-Path $InstallDir "python"
$PyExe        = Join-Path $PyDir "python.exe"
$AgentScript  = Join-Path $InstallDir "stacksense_agent.py"
$CmdLauncher  = Join-Path $InstallDir "run-agent.cmd"
$LogFile      = Join-Path $InstallDir "agent.log"

$PyVersion    = "3.11.9"
$PyZipUrl     = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
$GetPipUrl    = "https://bootstrap.pypa.io/get-pip.py"

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
    # Kill any running agent launched from our folder.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*StackSense Agent*" } |
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

function Get-File($uri, $outFile) {
    Write-Host "       $uri"
    Invoke-WebRequest -Uri $uri -OutFile $outFile -UseBasicParsing
}

Write-Host "[1/5] Stopping any existing agent + preparing $InstallDir ..."
Stop-Agent
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

Write-Host "[2/5] Installing a private Python ($PyVersion embeddable -- nothing system-wide) ..."
$pyZip = Join-Path $env:TEMP "ss-python.zip"
Get-File $PyZipUrl $pyZip
if (Test-Path $PyDir) { Remove-Item -Recurse -Force $PyDir }
Expand-Archive -Path $pyZip -DestinationPath $PyDir -Force
Remove-Item $pyZip -Force
$pth = Join-Path $PyDir "python311._pth"     # enable site-packages so pip modules import
if (Test-Path $pth) {
    (Get-Content $pth) -replace '^\s*#\s*import\s+site', 'import site' | Set-Content $pth
    if (-not (Select-String -Path $pth -Pattern 'Lib\\site-packages' -Quiet)) {
        Add-Content $pth "Lib\site-packages"
    }
}

Write-Host "[3/5] Bootstrapping pip + installing psutil ..."
$getpip = Join-Path $InstallDir "get-pip.py"
Get-File $GetPipUrl $getpip
& $PyExe $getpip --no-warn-script-location --no-cache-dir
& $PyExe -m pip install --no-warn-script-location --no-cache-dir psutil certifi
Remove-Item $getpip -Force
if (-not (Test-Path (Join-Path $PyDir "Lib\site-packages\psutil"))) {
    Write-Error "psutil did not install into the embedded Python. Aborting."; exit 1
}

Write-Host "[4/5] Downloading the agent ..."
Get-File "$Url/agent/stacksense_agent.py" $AgentScript

Write-Host "[5/5] Verifying auth, then registering the startup task ..."
try {
    Invoke-WebRequest -Uri "$Url/api/agent/ping/" -Headers @{ Authorization = "Bearer $Token" } -UseBasicParsing | Out-Null
    Write-Host "       auth OK"
} catch {
    Write-Warning "Auth check failed ($($_.Exception.Message)). Continuing; verify token/URL if no data appears."
}

# Launcher carries the config as env vars (the agent reads STACKSENSE_* first) and logs to
# a file. Lock it down so the token isn't readable by standard users.
$verify = if ($Insecure) { "false" } else { "true" }
$cmd = @"
@echo off
set "STACKSENSE_URL=$Url"
set "STACKSENSE_TOKEN=$Token"
set "STACKSENSE_INTERVAL=$Interval"
set "STACKSENSE_VERIFY_TLS=$verify"
"$PyExe" "$AgentScript" >> "$LogFile" 2>&1
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
Write-Host "StackSense agent installed; running via the '$TaskName' scheduled task (SYSTEM, auto-start)."
Write-Host "Logs: $LogFile   |   Status: Get-ScheduledTask $TaskName   |   Uninstall: install.ps1 -Uninstall"
