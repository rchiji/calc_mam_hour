param(
    [switch]$ServerMode,
    [ValidateRange(1, 65535)]
    [int]$Port = 8501,
    [ValidateRange(1, 1000)]
    [int]$MaxPortSearch = 50
)

$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$app = Join-Path $repoRoot "streamlit_app.py"
$appLeaf = Split-Path -Leaf $app

function Show-ErrorDialog {
    param([string]$Message)

    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($Message, "calc_mam_hour", "OK", "Error") | Out-Null
}

function Get-HealthUrl {
    param([int]$Port)

    return "http://127.0.0.1:$Port/_stcore/health"
}

function Get-AppUrl {
    param([int]$Port)

    return "http://127.0.0.1:$Port/"
}

function Start-AppProcess {
    param([int]$Port)

    $logDir = Join-Path $repoRoot ".lh"
    $stdoutLog = Join-Path $logDir "streamlit_stdout.log"
    $stderrLog = Join-Path $logDir "streamlit_stderr.log"

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    Remove-Item -LiteralPath $stdoutLog, $stderrLog -Force -ErrorAction SilentlyContinue

    return Start-Process -FilePath $python -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog `
        -ArgumentList @(
            "-m",
            "streamlit",
            "run",
            $app,
            "--server.address",
            "127.0.0.1",
            "--server.port",
            "$Port",
            "--server.headless",
            "true"
        )
}

function Get-PortSearchEnd {
    param(
        [int]$StartPort,
        [int]$SearchCount
    )

    return [Math]::Min(65535, $StartPort + $SearchCount - 1)
}

function Get-ListeningPortSet {
    $ports = [System.Collections.Generic.HashSet[int]]::new()
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()

    foreach ($listener in $listeners) {
        [void]$ports.Add($listener.Port)
    }

    return $ports
}

function Get-ListeningProcessIds {
    param([int]$Port)

    try {
        return @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        return @()
    }
}

function Test-AppReady {
    param([int]$Port)

    try {
        $response = Invoke-WebRequest -Uri (Get-HealthUrl -Port $Port) -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200 -and $response.Content -match "ok"
    } catch {
        return $false
    }
}

function Test-ManagedAppProcess {
    param([int]$Port)

    foreach ($processId in (Get-ListeningProcessIds -Port $Port)) {
        try {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction Stop
            if ($process.CommandLine -and (
                $process.CommandLine -match [regex]::Escape($app) -or
                $process.CommandLine -match [regex]::Escape($appLeaf)
            )) {
                return $true
            }
        } catch {
        }
    }

    return $false
}

function Test-ManagedApp {
    param([int]$Port)

    if (-not (Test-ManagedAppProcess -Port $Port)) {
        return $false
    }

    return Test-AppReady -Port $Port
}

function Find-ManagedAppPort {
    param(
        [int]$StartPort,
        [int]$SearchCount
    )

    $endPort = Get-PortSearchEnd -StartPort $StartPort -SearchCount $SearchCount
    $listeningPorts = Get-ListeningPortSet

    for ($candidate = $StartPort; $candidate -le $endPort; $candidate++) {
        if ($listeningPorts.Contains($candidate) -and (Test-ManagedApp -Port $candidate)) {
            return $candidate
        }
    }

    return $null
}

function Find-FreePort {
    param(
        [int]$StartPort,
        [int]$SearchCount
    )

    $endPort = Get-PortSearchEnd -StartPort $StartPort -SearchCount $SearchCount
    $listeningPorts = Get-ListeningPortSet

    for ($candidate = $StartPort; $candidate -le $endPort; $candidate++) {
        if (-not $listeningPorts.Contains($candidate)) {
            return $candidate
        }
    }

    return $null
}

function Resolve-AppPort {
    param(
        [int]$StartPort,
        [int]$SearchCount
    )

    $managedPort = Find-ManagedAppPort -StartPort $StartPort -SearchCount $SearchCount
    if ($null -ne $managedPort) {
        return [pscustomobject]@{
            Port = $managedPort
            HasRunningApp = $true
        }
    }

    $freePort = Find-FreePort -StartPort $StartPort -SearchCount $SearchCount
    if ($null -ne $freePort) {
        return [pscustomobject]@{
            Port = $freePort
            HasRunningApp = $false
        }
    }

    return $null
}

if (-not (Test-Path -LiteralPath $python)) {
    Show-ErrorDialog ".venv\Scripts\python.exe was not found."
    exit 1
}

if (-not (Test-Path -LiteralPath $app)) {
    Show-ErrorDialog "streamlit_app.py was not found."
    exit 1
}

$resolvedPort = Resolve-AppPort -StartPort $Port -SearchCount $MaxPortSearch
if ($null -eq $resolvedPort) {
    $endPort = Get-PortSearchEnd -StartPort $Port -SearchCount $MaxPortSearch
    Show-ErrorDialog "No available port was found between $Port and $endPort."
    exit 1
}

if ($ServerMode) {
    if ($resolvedPort.HasRunningApp) {
        exit 0
    }

    Set-Location -LiteralPath $repoRoot
    & $python -m streamlit run $app --server.address 127.0.0.1 --server.port $resolvedPort.Port --server.headless true
    exit $LASTEXITCODE
}

if ($resolvedPort.HasRunningApp) {
    Start-Process (Get-AppUrl -Port $resolvedPort.Port)
    exit 0
}

$appProcess = Start-AppProcess -Port $resolvedPort.Port

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if ($appProcess.HasExited) {
        break
    }

    $runningPort = Find-ManagedAppPort -StartPort $Port -SearchCount $MaxPortSearch
    if ($null -ne $runningPort) {
        Start-Process (Get-AppUrl -Port $runningPort)
        exit 0
    }
}

$endPort = Get-PortSearchEnd -StartPort $Port -SearchCount $MaxPortSearch
$stderrLog = Join-Path $repoRoot ".lh\streamlit_stderr.log"
if ($appProcess.HasExited) {
    $exitCode = try { $appProcess.ExitCode } catch { "unknown" }
    Show-ErrorDialog "Streamlit exited before startup (exit code: $exitCode). See $stderrLog"
    exit 1
}

Show-ErrorDialog "Timed out waiting for Streamlit to start between ports $Port and $endPort. See $stderrLog"
exit 1
