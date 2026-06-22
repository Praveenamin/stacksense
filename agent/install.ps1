<#
.SYNOPSIS
  StackSense push-agent installer for Windows (self-contained; analog of install.sh).

  Run this ON THE MONITORED WINDOWS HOST, elevated (Administrator). It is fully automated
  and needs NO prerequisites on the box: it lays down a PRIVATE Python (the official
  embeddable build -- nothing system-wide, no PATH changes), installs psutil, downloads
  the agent, and registers a Windows service (via NSSM) that runs it. The agent only
  dials OUT over HTTPS with its per-server token; it opens no inbound port.

.PARAMETER Url       Monitoring server base URL, e.g. https://mon.example.com  (required)
.PARAMETER Token     Per-server agent token from the Add-Server page           (required)
.PARAMETER Interval  Seconds between metric pushes (default 30)
.PARAMETER Insecure  Skip TLS verification (self-signed servers only)
.PARAMETER Uninstall Remove the service and files, then exit

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -Url https://mon -Token abc123
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
$ServiceName  = "StackSenseAgent"
$InstallDir   = Join-Path $env:ProgramFiles "StackSense Agent"
$PyDir        = Join-Path $InstallDir "python"
$PyExe        = Join-Path $PyDir "python.exe"
$NssmPath     = Join-Path $InstallDir "nssm.exe"
$AgentScript  = Join-Path $InstallDir "stacksense_agent.py"
$LogFile      = Join-Path $InstallDir "agent.log"

$PyVersion    = "3.11.9"
$PyZipUrl     = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
$GetPipUrl    = "https://bootstrap.pypa.io/get-pip.py"
$NssmZipUrl   = "https://nssm.cc/release/nssm-2.24.zip"

function Assert-Admin {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "Please run this installer as Administrator."; exit 1
    }
}
Assert-Admin

# TLS 1.2 for all downloads; optionally trust self-signed (Windows PowerShell 5.1 path).
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

function Remove-Agent {
    if (Test-Path $NssmPath) {
        & $NssmPath stop $ServiceName 2>$null
        & $NssmPath remove $ServiceName confirm 2>$null
    } else {
        sc.exe stop $ServiceName 2>$null | Out-Null
        sc.exe delete $ServiceName 2>$null | Out-Null
    }
    Start-Sleep -Seconds 2
}

if ($Uninstall) {
    Write-Host "[uninstall] Removing $ServiceName ..."
    Remove-Agent
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    Write-Host "[uninstall] Done."; exit 0
}

if (-not $Token) { Write-Error "-Token is required."; exit 1 }
$Url = $Url.TrimEnd('/')

function Get-File($uri, $outFile) {
    Write-Host "       $uri"
    Invoke-WebRequest -Uri $uri -OutFile $outFile -UseBasicParsing
}

Write-Host "[1/6] Stopping any existing service + preparing $InstallDir ..."
Remove-Agent
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

Write-Host "[2/6] Installing a private Python ($PyVersion embeddable -- nothing system-wide) ..."
$pyZip = Join-Path $env:TEMP "ss-python.zip"
Get-File $PyZipUrl $pyZip
if (Test-Path $PyDir) { Remove-Item -Recurse -Force $PyDir }
Expand-Archive -Path $pyZip -DestinationPath $PyDir -Force
Remove-Item $pyZip -Force
# Enable site-packages in the embeddable build so pip-installed modules import.
$pth = Join-Path $PyDir "python311._pth"
if (Test-Path $pth) {
    (Get-Content $pth) -replace '^\s*#\s*import\s+site', 'import site' | Set-Content $pth
    if (-not (Select-String -Path $pth -Pattern 'Lib\\site-packages' -Quiet)) {
        Add-Content $pth "Lib\site-packages"
    }
}

Write-Host "[3/6] Bootstrapping pip + installing psutil ..."
$getpip = Join-Path $InstallDir "get-pip.py"
Get-File $GetPipUrl $getpip
& $PyExe $getpip --no-warn-script-location --no-cache-dir
& $PyExe -m pip install --no-warn-script-location --no-cache-dir psutil certifi
Remove-Item $getpip -Force
if (-not (Test-Path (Join-Path $PyDir "Lib\site-packages\psutil"))) {
    Write-Error "psutil did not install into the embedded Python. Aborting."; exit 1
}

Write-Host "[4/6] Downloading the agent + NSSM service wrapper ..."
Get-File "$Url/agent/stacksense_agent.py" $AgentScript
$nssmZip = Join-Path $env:TEMP "ss-nssm.zip"
$nssmTmp = Join-Path $env:TEMP "ss-nssm"
Get-File $NssmZipUrl $nssmZip
if (Test-Path $nssmTmp) { Remove-Item -Recurse -Force $nssmTmp }
Expand-Archive -Path $nssmZip -DestinationPath $nssmTmp -Force
Copy-Item (Join-Path $nssmTmp "nssm-2.24\win64\nssm.exe") $NssmPath -Force
Remove-Item $nssmZip -Force; Remove-Item -Recurse -Force $nssmTmp

Write-Host "[5/6] Verifying authentication ..."
try {
    Invoke-WebRequest -Uri "$Url/api/agent/ping/" -Headers @{ Authorization = "Bearer $Token" } -UseBasicParsing | Out-Null
    Write-Host "       auth OK"
} catch {
    Write-Warning "Auth check failed ($($_.Exception.Message)). Continuing; verify token/URL if no data appears."
}

Write-Host "[6/6] Registering + starting the Windows service ..."
$verify = if ($Insecure) { "false" } else { "true" }
& $NssmPath install $ServiceName $PyExe
& $NssmPath set $ServiceName AppParameters "`"$AgentScript`""
& $NssmPath set $ServiceName AppDirectory $InstallDir
# Config via service env vars (the agent reads STACKSENSE_* first) -- token stays out of
# a shared file and LocalSystem's home dir is irrelevant.
& $NssmPath set $ServiceName AppEnvironmentExtra `
    "STACKSENSE_URL=$Url" "STACKSENSE_TOKEN=$Token" `
    "STACKSENSE_INTERVAL=$Interval" "STACKSENSE_VERIFY_TLS=$verify"
& $NssmPath set $ServiceName Start SERVICE_AUTO_START
& $NssmPath set $ServiceName AppExit Default Restart
& $NssmPath set $ServiceName AppRestartDelay 10000
& $NssmPath set $ServiceName AppStdout $LogFile
& $NssmPath set $ServiceName AppStderr $LogFile
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName Description "StackSense monitoring push agent"
& $NssmPath start $ServiceName

Write-Host ""
Write-Host "StackSense agent installed and started as the '$ServiceName' service."
Write-Host "Logs: $LogFile   |   Uninstall: install.ps1 -Uninstall"
